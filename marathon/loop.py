# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""The main loop.

One run:
  1. Make sure the goal is committed. If there are no tests yet, a separate test-writer agent
     writes them in its own worktree (Fix 6), and that commit becomes the base for every attempt.
  2. Up to `max_attempts` attempts. Each one gets a fresh worktree and branch, its own container
     when a sandbox is set (Fix 8), and a fresh context. The worker calls `checkpoint` after
     meaningful changes. The harness undoes edits to protected files, commits with "[step N]"
     in the message and runs the tests itself, in a fresh container when sandboxed (Fix 9).
  3. Every N steps (step % N == 0) the goal and the checklist go back in front of the model (Fix 1, 2).
  4. After `switch_after` checkpoints without progress the agent is told to note why and switch
     approach. If `patience` checkpoints in a row bring no progress, the plan is dropped: the
     worktree is removed, a short note is kept, and the next attempt starts fresh (Fix 3).
  5. `finish` only counts when the tests pass and a clean-context judge says PASS (Fix 10).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import agents, gitops, prompts
from .checks import TestResult, run_tests
from .config import Config
from .sandbox import Sandbox
from .tools import ToolError, Workspace, call_tool, parse_args, schemas

WORKER_TOOLS = [
    "list_files",
    "read_file",
    "write_file",
    "edit_file",
    "search",
    "run",
    "ask_subagent",
    "checkpoint",
    "finish",
]


def user(text: str) -> dict:
    return {"role": "user", "content": text}


def tail(text: str, n: int = 2500) -> str:
    return text if len(text) <= n else "...\n" + text[-n:]


@dataclass
class Attempt:
    number: int
    branch: str
    path: Path
    ws: Workspace
    step: int
    best: TestResult
    last: TestResult
    stall: int = 0
    turns: int = 0
    turns_since_checkpoint: int = 0


class Harness:
    def __init__(self, cfg: Config, llm, echo=print):
        self.cfg, self.llm, self.echo = cfg, llm, echo
        self.repo = Path(cfg.repo).resolve()
        self.state_dir = self.repo / ".marathon"
        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        self.started = time.time()
        self.turns = 0
        self.notes: list[str] = []
        self.events: list[dict] = []
        self.base = ""
        self.helpers = 0
        self.sandbox = Sandbox(cfg.sandbox, cfg.image, cfg.allow_network, self.run_id)

    @property
    def protected(self) -> list[str]:
        """Files the agent can't change: the tests, the goal and anything in --protect (Fix 9)."""
        return [self.cfg.tests_dir, self.cfg.goal_file, *self.cfg.protect]

    # ------------------------------------------------------------ bookkeeping
    def log(self, kind: str, msg: str = "", **data) -> None:
        event = {"t": round(time.time() - self.started, 1), "kind": kind, **data}
        if msg:
            event["msg"] = msg
            self.echo(msg)
        self.events.append(event)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_dir / "log.jsonl", "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def save_state(self, a: Attempt | None = None, outcome: str = "running") -> None:
        state = {
            "run_id": self.run_id,
            "base": self.base,
            "turns": self.turns,
            "outcome": outcome,
            "notes": self.notes,
        }
        if a:
            state.update(
                attempt=a.number,
                branch=a.branch,
                step=a.step,
                stall=a.stall,
                best=a.best.summary(),
                last=a.last.summary(),
            )
        (self.state_dir / "state.json").write_text(json.dumps(state, indent=2))

    def out_of_budget(self) -> bool:
        return (
            self.turns >= self.cfg.max_turns
            or time.time() - self.started > self.cfg.hours * 3600
        )

    def read(self, root: Path, name: str) -> str:
        p = root / name
        return p.read_text(errors="replace") if p.is_file() else ""

    # ------------------------------------------------------------ setup
    def prepare(self) -> None:
        cfg, repo = self.cfg, self.repo
        if not repo.is_dir():
            raise SystemExit(f"{repo} does not exist")
        fresh = not gitops.is_repo(repo)
        if fresh:
            gitops.init_repo(repo)
        gitops.ensure_identity(repo)
        if not (repo / cfg.goal_file).is_file():
            raise SystemExit(
                f"No {cfg.goal_file} in {repo}. Write the goal there first."
            )
        gi = repo / ".gitignore"
        text = gi.read_text() if gi.exists() else ""
        missing = [p for p in (".marathon/", "__pycache__/") if p not in text.split()]
        if missing:
            gi.write_text(
                text
                + ("\n" if text and not text.endswith("\n") else "")
                + "\n".join(missing)
                + "\n"
            )
        if fresh:
            gitops.git(repo, "add", "-A")
        else:
            gitops.git(repo, "add", cfg.goal_file, ".gitignore")
        if gitops.git(repo, "diff", "--cached", "--name-only"):
            gitops.git(repo, "commit", "-q", "-m", "marathon: goal and settings")
        if not gitops.is_clean(repo):
            self.log(
                "warn",
                "Note: uncommitted changes in the repo are not copied into the agent's worktrees.",
            )
        self.sandbox.check(log=lambda m: self.log("sandbox", m))
        if self.sandbox.kind == "local":
            self.log("sandbox", "Commands run directly on this machine. For long unattended runs, "
                     "use --sandbox docker (or podman).")
        else:
            self.log("sandbox", f"Commands run in {self.sandbox.describe()}.")

    def has_tests(self, root: Path, tracked: bool) -> bool:
        if tracked:
            return bool(gitops.git(root, "ls-files", "--", self.cfg.tests_dir))
        d = root / self.cfg.tests_dir
        return d.is_dir() and any(p.is_file() for p in d.rglob("*"))

    def ensure_tests(self) -> str:
        """Fix 6 and 7: a separate agent, with a fresh context, writes the tests from the goal."""
        cfg = self.cfg
        if self.has_tests(self.repo, tracked=True):
            return gitops.head(self.repo)
        path = self.state_dir / "worktrees" / "tests"
        gitops.remove_worktree(self.repo, path)
        branch = f"marathon/{self.run_id}/tests"
        gitops.add_worktree(self.repo, path, branch, gitops.head(self.repo))
        ws = Workspace(
            path,
            protected=(cfg.goal_file, *cfg.protect, ".git"),
            writable_only=(cfg.tests_dir,),
            output_limit=cfg.tool_output_limit,
            cmd_timeout=cfg.cmd_timeout,
            shell=self.sandbox.shell(path, "tests"),
        )
        self.log(
            "tests",
            f"No tests yet. A separate test-writer agent is writing them on {branch}.",
        )
        try:
            summary = agents.write_tests(
                self.llm, ws, cfg.goal_file, cfg.tests_dir, cfg.test_cmd
            )
        finally:
            self.sandbox.close_all()
        if not self.has_tests(path, tracked=False):
            raise SystemExit(
                "The test writer didn't write any tests. Add tests yourself, or try another model."
            )
        # only the tests go into the base commit, whatever else the test writer touched
        gitops.commit_step(
            path,
            0,
            "tests: " + (summary or "suite for the goal"),
            paths=[cfg.tests_dir],
        )
        base = gitops.head(path)
        gitops.remove_worktree(self.repo, path)
        self.log(
            "tests", f"Tests committed: {summary or 'done'}", branch=branch, base=base
        )
        return base

    # ------------------------------------------------------------ the run
    def run(self) -> str:
        try:
            self.prepare()
            self.base = self.ensure_tests()
            outcome = "gave_up"
            for n in range(1, self.cfg.max_attempts + 1):
                result = self.run_attempt(n)
                if result in ("done", "budget"):
                    outcome = result
                    break
        finally:
            self.sandbox.close_all()  # no container outlives the run, even on Ctrl-C
        self.save_state(outcome=outcome)
        if outcome == "gave_up":
            self.log(
                "end",
                f"Stopped after {self.cfg.max_attempts} attempts without reaching the goal. "
                f"Notes are in {self.state_dir / 'attempts.md'}.",
            )
        return outcome

    def run_attempt(self, n: int) -> str:
        cfg = self.cfg
        path = self.state_dir / "worktrees" / f"attempt-{n}"
        gitops.remove_worktree(self.repo, path)
        branch = f"marathon/{self.run_id}/attempt-{n}"
        gitops.add_worktree(self.repo, path, branch, self.base)
        self.sandbox.close_all()  # the last attempt's container goes before this one starts
        ws = Workspace(
            path,
            protected=(*self.protected, ".marathon/outputs", ".git"),
            output_limit=cfg.tool_output_limit,
            cmd_timeout=cfg.cmd_timeout,
            shell=self.sandbox.shell(path, f"attempt-{n}"),
        )
        baseline = self.measure(path)
        a = Attempt(n, branch, path, ws, gitops.last_step(path), baseline, baseline)
        self.log(
            "attempt",
            f"Attempt {n} starts on {branch}. Right now: {baseline.summary()}.",
            attempt=n,
            branch=branch,
        )
        goal = self.read(path, cfg.goal_file)
        messages = [
            {
                "role": "system",
                "content": prompts.WORKER.format(
                    goal_file=cfg.goal_file,
                    tests_dir=cfg.tests_dir,
                    patience=cfg.patience,
                ),
            },
            user(
                prompts.start_message(
                    n,
                    branch,
                    cfg.goal_file,
                    goal,
                    cfg.test_cmd,
                    baseline.summary(),
                    self.notes,
                )
            ),
        ]
        tools = schemas(WORKER_TOOLS)

        while True:
            if self.out_of_budget():
                self.save_state(a, "budget")
                self.log(
                    "budget",
                    f"Out of budget. Work so far is on {branch}, folder {path}.",
                )
                return "budget"
            self.turns += 1
            a.turns += 1
            a.turns_since_checkpoint += 1
            if (
                cfg.reread_every_turns
                and a.turns > 1
                and a.turns % cfg.reread_every_turns == 0
            ):
                messages.append(user(self.reread(a, f"turn {a.turns}")))
            messages = self.trim(messages)

            reply = self.llm.complete(messages, tools=tools, agent="worker")
            messages.append(reply)
            calls = reply.get("tool_calls") or []
            if not calls:
                messages.append(
                    user(
                        "Keep going. Use the tools, call checkpoint after a meaningful change, "
                        "and call finish when the goal is met."
                    )
                )

            reread_due, done = False, False
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = parse_args(c)
                except (ValueError, ToolError) as e:
                    messages.append(agents.tool_msg(c, f"error: bad arguments: {e}"))
                    continue
                if name == "checkpoint":
                    out = self.checkpoint(a, str(args.get("summary") or "checkpoint"))
                    reread_due = reread_due or gitops.is_reread_step(
                        a.step, cfg.reread_every_steps
                    )
                elif name == "finish":
                    out, done = self.finish(a, str(args.get("summary") or "done"))
                    reread_due = reread_due or gitops.is_reread_step(
                        a.step, cfg.reread_every_steps
                    )
                elif name == "ask_subagent":
                    self.helpers += 1
                    out = agents.ask(
                        self.llm,
                        path,
                        str(args.get("task", "")),
                        args.get("paths"),
                        n=self.helpers,
                        output_limit=cfg.tool_output_limit,
                    )
                else:
                    out = call_tool(ws, name, args)
                messages.append(agents.tool_msg(c, out))
                if done:
                    break

            if done:
                gitops.remove_worktree(self.repo, path)
                self.save_state(a, "done")
                self.log(
                    "done",
                    f"Done at step {a.step}: {a.last.summary()}, judge PASS. "
                    f"The work is on branch {branch}. To use it: git merge {branch}",
                )
                return "done"

            if (
                a.turns_since_checkpoint >= cfg.force_checkpoint_every
                and a.stall < cfg.patience
            ):
                out = self.checkpoint(a, "harness checkpoint", forced=True)
                messages.append(
                    user(
                        "You haven't called checkpoint for a while, so the harness made one: "
                        + out
                    )
                )
                reread_due = reread_due or gitops.is_reread_step(
                    a.step, cfg.reread_every_steps
                )

            if a.stall >= cfg.patience:
                self.drop(a)
                return "stalled"

            if reread_due:
                messages.append(user(self.reread(a, f"step {a.step}")))

    # ------------------------------------------------------------ the pieces
    def measure(self, path: Path) -> TestResult:
        """Run the tests: on this machine, or in a fresh container for every run."""
        return run_tests(
            path, self.cfg.test_cmd, self.cfg.cmd_timeout, self.cfg.score_cmd,
            runner=lambda cmd, timeout: self.sandbox.run_once(path, cmd, timeout),
        )

    def record(self, a: Attempt, r: TestResult) -> bool:
        """Is this progress? More passing tests, or the same count and a higher score."""
        a.last = r
        (p, s), (bp, bs) = r.key(), a.best.key()
        improved = p > bp or (p == bp and s > bs + self.cfg.min_delta)
        if improved:
            a.best, a.stall = r, 0
        else:
            a.stall += 1
        return improved

    def commit(self, a: Attempt, message: str) -> list[str]:
        """Undo any change to the protected files, then commit with the step number."""
        a.step += 1
        restored = gitops.restore(a.path, self.base, self.protected)
        gitops.commit_step(a.path, a.step, message)
        a.turns_since_checkpoint = 0
        return restored

    def test_report(self, a: Attempt, r: TestResult) -> str:
        if r.all_passed:
            return ""
        f = a.ws.out_dir / f"tests-step-{a.step}.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(r.output)
        return f"\nEnd of the test output (full output in {a.ws.rel(f)}):\n{tail(r.output)}"

    def checkpoint(self, a: Attempt, summary: str, forced: bool = False) -> str:
        restored = self.commit(a, summary)
        r = self.measure(a.path)
        improved = self.record(a, r)
        status = (
            "progress" if improved else f"no progress {a.stall}/{self.cfg.patience}"
        )
        self.log(
            "checkpoint",
            f"[attempt {a.number} | step {a.step}] {r.summary()} ({status})"
            + (" [made by the harness]" if forced else "")
            + (
                f" [undid edits to read-only files: {', '.join(restored)}]"
                if restored
                else ""
            ),
            attempt=a.number,
            step=a.step,
            passed=r.passed,
            total=r.total,
            score=r.score,
            improved=improved,
            stall=a.stall,
            restored=restored,
        )
        if gitops.is_reread_step(a.step, self.cfg.reread_every_steps):
            self.log(
                "reread",
                f"[attempt {a.number} | step {a.step}] step % {self.cfg.reread_every_steps} == 0: "
                "the goal goes back in front of the agent",
                attempt=a.number,
                step=a.step,
            )
        self.save_state(a)
        out = f"Committed as step {a.step}. {r.summary()}. "
        out += (
            "That's progress."
            if improved
            else f"No progress: {a.stall}/{self.cfg.patience} checkpoints in a row."
        )
        n = self.cfg.switch_after
        if not improved and n and a.stall % n == 0 and a.stall < self.cfg.patience:
            # Fix 3, enforced by the harness: don't leave "3 fails, switch" to the model alone
            out += (f"\n{a.stall} checkpoints in a row without progress. Write in PROGRESS.md why "
                    "this approach isn't working, then switch to a different one.")
            self.log(
                "switch",
                f"[attempt {a.number} | step {a.step}] {a.stall} checkpoints without progress: "
                "told the agent to note why and switch approach",
                attempt=a.number,
                step=a.step,
            )
        if restored:
            out += f"\nChanges to read-only files were undone: {', '.join(restored)}."
        return out + self.test_report(a, r)

    def finish(self, a: Attempt, summary: str) -> tuple[str, bool]:
        restored = self.commit(a, "finish: " + summary)
        r = self.measure(a.path)
        self.record(a, r)
        note = (
            f"\nChanges to read-only files were undone: {', '.join(restored)}."
            if restored
            else ""
        )
        if not r.all_passed:
            self.log(
                "finish",
                f"[attempt {a.number} | step {a.step}] finish rejected: {r.summary()}",
                attempt=a.number,
                step=a.step,
            )
            self.save_state(a)
            return (
                f"Not done yet: {r.summary()}. The goal needs every test to pass.{note}"
                + self.test_report(a, r)
            ), False
        d = gitops.diff(a.path, self.base, exclude=["PROGRESS.md", self.cfg.tests_dir])
        ok, verdict = agents.judge(self.llm, self.read(a.path, self.cfg.goal_file), d)
        self.log(
            "judge",
            f"[attempt {a.number} | step {a.step}] tests pass, judge says {'PASS' if ok else 'FAIL'}",
            attempt=a.number,
            step=a.step,
            verdict=verdict[:500],
        )
        if ok:
            return "All tests pass and the judge says PASS. Done.", True
        if (
            a.stall == 0
        ):  # tests passing counted as progress, but the judge disagrees: count it as a try
            a.stall = 1
        self.save_state(a)
        return (
            f"All tests pass, but the judge says it isn't done:\n{verdict}\nFix this, then call finish again.{note}",
            False,
        )

    def reread(self, a: Attempt, why: str) -> str:
        """Fix 1 and 2: read the goal from disk again and put it, with the checklist, at the end."""
        self.log("reread_sent", why=why, attempt=a.number, step=a.step)
        return prompts.reread_message(
            a.step,
            self.cfg.goal_file,
            self.read(a.path, self.cfg.goal_file),
            self.read(a.path, "PROGRESS.md"),
            a.last.summary(),
            a.best.summary(),
            a.stall,
            self.cfg.patience,
        )

    def drop(self, a: Attempt) -> None:
        """Fix 3: the plan was temporary. Keep a short note and the branch, remove the folder."""
        plan = " / ".join(
            l.strip("-* ").strip()
            for l in self.read(a.path, "PROGRESS.md").splitlines()
            if l.strip()
        )
        note = (
            f"Attempt {a.number} ({a.branch}) dropped after {a.stall} checkpoints with no progress. "
            f"Best: {a.best.summary()}. Its checklist: {plan[:300] or '(none)'}"
        )
        self.notes.append(note)
        with open(self.state_dir / "attempts.md", "a") as f:
            f.write(f"- {note}\n")
        self.log(
            "drop",
            f"[attempt {a.number}] No progress in {a.stall} checkpoints. Dropping this plan. "
            "The next attempt starts in a fresh worktree.",
            attempt=a.number,
            branch=a.branch,
        )
        self.save_state(a, "stalled")
        gitops.remove_worktree(self.repo, a.path)

    def trim(self, messages: list[dict]) -> list[dict]:
        """Fix 5: keep the context small. Keep the system prompt and the task, drop the oldest turns."""
        limit = self.cfg.max_history
        if len(messages) <= limit:
            return messages
        start = len(messages) - (limit - 3)
        while start < len(messages) and messages[start]["role"] == "tool":
            start += 1
        self.log("trim", dropped=start - 2)
        return (
            messages[:2]
            + [
                user(
                    "(Older messages were dropped to keep the context small. "
                    "The goal is above. Your checklist is in PROGRESS.md.)"
                )
            ]
            + messages[start:]
        )
