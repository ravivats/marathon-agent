# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""The demo replays a prepared run in a throwaway repo, so it runs offline with no API key.

It shows the harness end to end:
  - a separate test-writer agent writes the tests first
  - attempt 1 follows a bad plan, is told to switch approach after 2 checkpoints without
    progress, even tries to edit the tests (the harness undoes it), makes no progress for
    `patience` checkpoints and gets dropped
  - attempt 2 starts in a fresh worktree, asks a helper with a fresh context (a 5-line answer,
    the rest in a notes file), makes progress, re-reads the goal every 2 steps, and finishes
    with tests passing and a judge PASS
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from . import gitops
from .config import Config
from .llm import ReplayLLM, call, say
from .loop import Harness

GOAL = """\
# Goal

Write `slugify(text)` in `slug.py`.

- lowercase everything
- keep only letters, digits and spaces
- join the words with "-"
- no leading, trailing or double "-"
"""

TESTS = """\
import unittest

from slug import slugify


class TestSlugify(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(slugify("Hello"), "hello")

    def test_spaces_become_dashes(self):
        self.assertEqual(slugify("long running agents"), "long-running-agents")

    def test_drops_punctuation(self):
        self.assertEqual(slugify("Don't lose the plot!"), "dont-lose-the-plot")

    def test_collapses_and_trims(self):
        self.assertEqual(slugify("  many   spaces  "), "many-spaces")


if __name__ == "__main__":
    unittest.main()
"""

GOOD = """\
import re


def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    return "-".join(text.split())
"""


def replay() -> ReplayLLM:
    """The prepared run: each agent's moves, in order."""
    return ReplayLLM(
        {
            "test_writer": [
                call("write_file", path="tests/test_slug.py", content=TESTS),
                call("finish", summary="4 tests for slugify"),
            ],
            "worker": [
                # attempt 1: a bad plan
                call(
                    "write_file",
                    path="PROGRESS.md",
                    content="- [ ] replace spaces with underscores\n",
                ),
                call(
                    "write_file",
                    path="slug.py",
                    content='def slugify(text):\n    return text.replace(" ", "_")\n',
                ),
                call("checkpoint", summary="replace spaces"),
                call("edit_file", path="slug.py", old='"_"', new='"__"'),
                call("checkpoint", summary="try double underscores"),
                call(
                    "run", command="echo 'import unittest' > tests/test_slug.py"
                ),  # tries to gut the tests
                call("checkpoint", summary="simplify the tests"),
                # attempt 2: a fresh start
                call(
                    "write_file",
                    path="PROGRESS.md",
                    content="- [ ] lowercase\n- [ ] keep letters, digits, spaces\n- [ ] join words with -\n",
                ),
                call(
                    "ask_subagent",
                    task="What does the test suite expect from slugify?",
                    paths=["tests/test_slug.py"],
                ),
                call(
                    "write_file",
                    path="slug.py",
                    content='def slugify(text: str) -> str:\n    return "-".join(text.lower().split(" "))\n',
                ),
                call("checkpoint", summary="lowercase and dashes"),
                call("write_file", path="slug.py", content=GOOD),
                call(
                    "write_file",
                    path="PROGRESS.md",
                    content="- [x] lowercase\n- [x] keep letters, digits, spaces\n- [x] join words with -\n",
                ),
                call("checkpoint", summary="drop punctuation and extra spaces"),
                call("run", command="python3 -m unittest discover -s tests"),
                call("finish", summary="slugify meets the goal"),
            ],
            "subagent": [
                call("read_file", path="tests/test_slug.py"),
                call(
                    "write_file",
                    path=".marathon/notes/helper-1.md",
                    content="# What the tests expect\n\n- 'Hello' -> 'hello'\n"
                    "- 'long running agents' -> 'long-running-agents'\n"
                    "- \"Don't lose the plot!\" -> 'dont-lose-the-plot'\n"
                    "- '  many   spaces  ' -> 'many-spaces'\n",
                ),
                say(
                    "Lowercase, words joined by '-', punctuation dropped, repeated and edge spaces removed.\n"
                    "Examples for each test are in the notes file."
                ),
            ],
            "judge": [
                say("PASS\nAll four requirements in the goal are covered by the diff.")
            ],
        }
    )


def main(keep: bool = True) -> int:
    root = Path(tempfile.mkdtemp(prefix="marathon-demo-"))
    (root / "goal.md").write_text(GOAL)
    print(f"Demo repo: {root}\n")
    cfg = Config(
        repo=root,
        patience=3,
        switch_after=2,
        reread_every_steps=2,
        max_attempts=3,
        model="replay",
    )
    outcome = Harness(cfg, replay()).run()
    print(f"\nOutcome: {outcome}\n\nBranches and commits:")
    print(gitops.git(root, "log", "--all", "--graph", "--format=%h %s %d"))
    print(f"\nEvent log: {root / '.marathon' / 'log.jsonl'}")
    return 0 if outcome == "done" else 1
