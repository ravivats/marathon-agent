# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Git helpers. Each attempt gets its own worktree and branch (Fix 8), and every checkpoint
is a commit whose message ends in "[step N]", so the step count survives restarts."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

STEP_RE = re.compile(r"\[step (\d+)\]\s*$")


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def is_repo(path: Path) -> bool:
    return (Path(path) / ".git").exists()


def init_repo(path: Path) -> None:
    git(path, "init", "-q")
    ensure_identity(path)


def ensure_identity(path: Path) -> None:
    """Commits need a name and email. Set a local one only if none is configured."""
    if not git(path, "config", "user.name", check=False):
        git(path, "config", "user.name", "marathon-agent")
    if not git(path, "config", "user.email", check=False):
        git(path, "config", "user.email", "marathon-agent@localhost")


def head(cwd: Path) -> str:
    return git(cwd, "rev-parse", "HEAD")


def has_commits(cwd: Path) -> bool:
    return bool(git(cwd, "rev-parse", "--verify", "-q", "HEAD", check=False))


def step_message(message: str, step: int) -> str:
    first = (message.strip().splitlines() or ["checkpoint"])[0][:72]
    return f"{first} [step {step}]"


def commit_step(
    cwd: Path, step: int, message: str, paths: list[str] | None = None
) -> str:
    """Commit everything (or only `paths`) with the step number at the end of the message."""
    git(cwd, "add", "-A", "--", *(paths or ["."]))
    git(cwd, "commit", "-q", "--allow-empty", "-m", step_message(message, step))
    return head(cwd)


def last_step(cwd: Path) -> int:
    """The step number of the most recent "[step N]" commit, or 0."""
    for subject in git(
        cwd, "log", "--format=%s", "-n", "500", check=False
    ).splitlines():
        m = STEP_RE.search(subject)
        if m:
            return int(m.group(1))
    return 0


def is_reread_step(step: int, every: int) -> bool:
    """Fix 1: re-read the goal when step % N == 0."""
    return every > 0 and step > 0 and step % every == 0


def add_worktree(repo: Path, path: Path, branch: str, base: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)


def remove_worktree(repo: Path, path: Path) -> None:
    """Remove the folder. The branch and its commits stay, so you can look at them later."""
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)


def restore(cwd: Path, base: str, paths: list[str]) -> list[str]:
    """Put protected paths (tests, goal) back exactly as they were at `base`.
    Returns the files that had been changed."""
    changed = []
    for line in git(
        cwd, "diff", "--no-renames", "--name-status", base, "--", *paths
    ).splitlines():
        status, name = line.split("\t", 1)
        changed.append(name)
        if status.startswith("A"):
            (Path(cwd) / name).unlink(missing_ok=True)
        else:
            git(cwd, "checkout", base, "--", name)
    untracked = git(
        cwd, "ls-files", "--others", "--exclude-standard", "--", *paths
    ).splitlines()
    for name in untracked:
        changed.append(name)
        (Path(cwd) / name).unlink(missing_ok=True)
    return changed


def diff(cwd: Path, base: str, exclude: list[str]) -> str:
    specs = [f":(exclude){e}" for e in exclude]
    return git(cwd, "diff", base, "HEAD", "--", ".", *specs)


def is_clean(cwd: Path, ignore: tuple[str, ...] = (".marathon",)) -> bool:
    lines = git(cwd, "status", "--porcelain").splitlines()
    return all(any(l[3:].startswith(i) for i in ignore) for l in lines)
