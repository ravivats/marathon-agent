# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Small, general tools that the model combines on its own (Fix 4: a light harness).

read, write, edit, list, search and run. Every path stays inside one folder (the agent's
own worktree, Fix 8), and `run` goes through the attempt's shell: this machine or a container
(see sandbox.py). Long outputs are cut short and the full text is saved to a file the
agent can read later (Fix 5: point at the data instead of pasting it).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path

from .sandbox import LocalShell

SKIP_DIRS = {".git", ".marathon", "__pycache__", "node_modules", ".venv", "venv"}


class ToolError(Exception):
    pass


class Workspace:
    def __init__(
        self,
        root: Path,
        protected: tuple[str, ...] = (),
        writable_only: tuple[str, ...] = (),
        output_limit: int = 6000,
        cmd_timeout: int = 300,
        shell=None,
    ):
        self.root = Path(root).resolve()
        self.shell = shell or LocalShell(self.root)
        self.protected = tuple(p.rstrip("/") for p in protected)
        self.writable_only = tuple(p.rstrip("/") for p in writable_only)
        self.output_limit = output_limit
        self.cmd_timeout = cmd_timeout
        self.out_dir = self.root / ".marathon" / "outputs"
        self._n = 0

    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"{rel} is outside the workspace")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    @staticmethod
    def _under(rel: str, prefixes: tuple[str, ...]) -> bool:
        return any(rel == p or rel.startswith(p + "/") for p in prefixes)

    def check_write(self, p: Path) -> None:
        rel = self.rel(p)
        if self._under(rel, self.protected):
            raise ToolError(f"{rel} is read-only")
        if self.writable_only and not self._under(rel, self.writable_only):
            raise ToolError(
                f"you can only write inside: {', '.join(self.writable_only)}"
            )

    def clip(self, text: str, label: str) -> str:
        """Show the start and end of long text, and save the whole thing to a file."""
        if len(text) <= self.output_limit:
            return text
        self._n += 1
        self.out_dir.mkdir(parents=True, exist_ok=True)
        f = self.out_dir / f"{label}-{self._n}.txt"
        f.write_text(text)
        half = self.output_limit // 2
        return (
            f"{text[:half]}\n... [{len(text) - self.output_limit} characters cut] ...\n{text[-half:]}\n"
            f"(Full output saved to {self.rel(f)}. Use read_file with line numbers to see more.)"
        )


def _walk(ws: Workspace, start: Path):
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


# ---------------------------------------------------------------- the tools


def list_files(ws: Workspace, path: str = ".", pattern: str = "*") -> str:
    files = [
        ws.rel(f) for f in _walk(ws, ws.path(path)) if fnmatch.fnmatch(f.name, pattern)
    ]
    if not files:
        return "(no files)"
    more = f"\n... and {len(files) - 300} more" if len(files) > 300 else ""
    return "\n".join(files[:300]) + more


def read_file(ws: Workspace, path: str, start_line: int = 1, end_line: int = 0) -> str:
    p = ws.path(path)
    if not p.is_file():
        raise ToolError(f"{path} does not exist")
    lines = p.read_text(errors="replace").splitlines()
    end = end_line or len(lines)
    body = "\n".join(
        f"{i:5d}  {line}"
        for i, line in enumerate(lines[start_line - 1 : end], start=start_line)
    )
    return ws.clip(body or "(empty file)", "read")


def write_file(ws: Workspace, path: str, content: str) -> str:
    p = ws.path(path)
    ws.check_write(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {path} ({len(content.splitlines())} lines)"


def edit_file(ws: Workspace, path: str, old: str, new: str) -> str:
    p = ws.path(path)
    ws.check_write(p)
    if not p.is_file():
        raise ToolError(f"{path} does not exist")
    text = p.read_text()
    n = text.count(old)
    if n != 1:
        raise ToolError(f"'old' must appear exactly once in {path}, found {n} times")
    p.write_text(text.replace(old, new, 1))
    return f"edited {path}"


def search(ws: Workspace, pattern: str, path: str = ".") -> str:
    rx = re.compile(pattern)
    hits = []
    for f in _walk(ws, ws.path(path)):
        try:
            for i, line in enumerate(f.read_text().splitlines(), start=1):
                if rx.search(line):
                    hits.append(f"{ws.rel(f)}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 100:
                        return "\n".join(hits) + "\n(stopped at 100 matches)"
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(hits) or "(no matches)"


def run(ws: Workspace, command: str, timeout: int = 0) -> str:
    code, out = ws.shell.run(command, timeout or ws.cmd_timeout)
    return ws.clip(f"exit code {code}\n{out}", "run")


FUNCS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "search": search,
    "run": run,
}

_S, _I = {"type": "string"}, {"type": "integer"}
SPECS = {
    "list_files": (
        "List files under a folder. pattern is a glob on the file name, like *.py.",
        {"path": _S, "pattern": _S},
        [],
    ),
    "read_file": (
        "Read a file with line numbers. Use start_line and end_line for big files.",
        {"path": _S, "start_line": _I, "end_line": _I},
        ["path"],
    ),
    "write_file": (
        "Create or overwrite a file.",
        {"path": _S, "content": _S},
        ["path", "content"],
    ),
    "edit_file": (
        "Replace one exact piece of text in a file. 'old' must appear exactly once.",
        {"path": _S, "old": _S, "new": _S},
        ["path", "old", "new"],
    ),
    "search": (
        "Search files with a regular expression. Returns file:line: text.",
        {"pattern": _S, "path": _S},
        ["pattern"],
    ),
    "run": (
        "Run a shell command in your folder. Returns the exit code and output.",
        {"command": _S, "timeout": _I},
        ["command"],
    ),
    # control tools, handled by the loop
    "checkpoint": (
        "Call after a meaningful change. The harness commits your work and runs the tests.",
        {"summary": _S},
        ["summary"],
    ),
    "finish": (
        "Call when you believe the goal is met (or, for a helper, when your job is done).",
        {"summary": _S},
        ["summary"],
    ),
    "ask_subagent": (
        "Hand a focused question to a helper with a fresh, empty context. Give it file paths "
        "to look at, not their contents (a longer brief can go in .marathon/notes/). It answers "
        "in 5 lines or less and saves anything longer to a notes file.",
        {"task": _S, "paths": {"type": "array", "items": _S}},
        ["task"],
    ),
}


def schemas(names: list[str]) -> list[dict]:
    out = []
    for n in names:
        desc, props, required = SPECS[n]
        out.append(
            {
                "type": "function",
                "function": {
                    "name": n,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    },
                },
            }
        )
    return out


def parse_args(tool_call: dict) -> dict:
    raw = tool_call["function"].get("arguments") or "{}"
    args = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    return args


def call_tool(ws: Workspace, name: str, args: dict) -> str:
    fn = FUNCS.get(name)
    if fn is None:
        return f"error: unknown tool {name}"
    try:
        return fn(ws, **args)
    except (ToolError, TypeError, re.error, OSError) as e:
        return f"error: {e}"
