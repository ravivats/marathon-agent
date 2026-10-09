# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Helpers that run with a fresh, empty context (Fix 6) and get file paths, not contents (Fix 7):
the test writer, the ask_subagent helper and the clean-context judge (Fix 10)."""
from __future__ import annotations

from pathlib import Path

from . import prompts
from .tools import ToolError, Workspace, call_tool, parse_args, schemas

ANSWER_LINES = 5


def text_of(msg: dict) -> str:
    return (msg.get("content") or "").strip()


def tool_msg(tool_call: dict, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call["id"], "content": content}


def run_agent(llm, agent: str, system: str, task: str, ws: Workspace, tool_names: list[str],
              max_turns: int = 30) -> str:
    """A small loop for helpers. Ends when the helper calls finish or answers in plain text."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    tools = schemas(tool_names + ["finish"])
    for _ in range(max_turns):
        reply = llm.complete(messages, tools=tools, agent=agent)
        messages.append(reply)
        calls = reply.get("tool_calls") or []
        if not calls:
            return text_of(reply)
        for c in calls:
            name = c["function"]["name"]
            try:
                args = parse_args(c)
            except (ValueError, ToolError) as e:
                messages.append(tool_msg(c, f"error: bad arguments: {e}"))
                continue
            if name == "finish":
                return str(args.get("summary", "")).strip()
            out = call_tool(ws, name, args) if name in tool_names else f"error: unknown tool {name}"
            messages.append(tool_msg(c, out))
    return "(helper stopped: turn limit reached)"


def write_tests(llm, ws: Workspace, goal_file: str, tests_dir: str, test_cmd: str, max_turns: int = 40) -> str:
    system = prompts.TEST_WRITER.format(goal_file=goal_file, tests_dir=tests_dir, test_cmd=test_cmd)
    task = f"Write the test suite for the goal in {goal_file}. Put the tests in {tests_dir}/."
    return run_agent(llm, "test_writer", system, task, ws,
                     ["list_files", "read_file", "search", "write_file", "run"], max_turns)


def ask(llm, root: Path, task: str, paths: list[str] | None, n: int = 1,
        output_limit: int = 6000) -> str:
    """Fix 6 and 7: a helper with a fresh context gets a question and file paths (a pointer,
    not the contents). It answers in 5 lines or less. Anything longer goes in a notes file,
    the only file it can write, and the worker gets the path."""
    notes = f".marathon/notes/helper-{n}.md"
    ws = Workspace(root, writable_only=(notes,), output_limit=output_limit)
    q = task.strip()
    if paths:
        q += "\nLook at: " + ", ".join(paths)
    q += f"\nNotes file, if you need more than {ANSWER_LINES} lines: {notes}"
    answer = run_agent(llm, "subagent", prompts.SUBAGENT, q, ws,
                       ["list_files", "read_file", "search", "write_file"], 12)
    lines = [line.rstrip() for line in answer.splitlines() if line.strip()][:ANSWER_LINES]
    reply = "\n".join(lines)[:1000] or "(no answer)"
    if (ws.root / notes).is_file():
        reply += f"\nFull notes: {notes}"
    return reply


def judge(llm, goal: str, diff: str, limit: int = 60000) -> tuple[bool, str]:
    """Fix 10: the judge sees only the goal and the diff. No reasoning, logs or earlier tries."""
    if len(diff) > limit:
        diff = diff[:limit] + "\n... [diff cut short]"
    messages = [{"role": "system", "content": prompts.JUDGE},
                {"role": "user", "content": f"Goal:\n{goal.strip()}\n\nFinal diff:\n{diff or '(empty diff)'}"}]
    text = text_of(llm.complete(messages, tools=None, agent="judge"))
    first = (text.splitlines() or [""])[0].strip().upper()
    return first.startswith("PASS"), text
