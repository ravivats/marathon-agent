# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

import unittest

from marathon.tools import Workspace, call_tool, schemas
from tests.helpers import TempRepoCase


class Tools(TempRepoCase):
    def ws(self, **kw):
        return Workspace(self.root, **kw)

    def test_paths_stay_inside_the_workspace(self):
        self.assertIn(
            "outside the workspace",
            call_tool(self.ws(), "read_file", {"path": "../etc/passwd"}),
        )

    def test_protected_paths_are_read_only(self):
        ws = self.ws(protected=("tests", "goal.md"))
        self.assertIn(
            "read-only",
            call_tool(ws, "write_file", {"path": "tests/test_x.py", "content": "x"}),
        )
        self.assertIn(
            "read-only",
            call_tool(ws, "write_file", {"path": "goal.md", "content": "x"}),
        )
        self.assertIn(
            "wrote", call_tool(ws, "write_file", {"path": "src/a.py", "content": "x"})
        )

    def test_writable_only(self):
        ws = self.ws(writable_only=("tests",))
        self.assertIn(
            "only write inside",
            call_tool(ws, "write_file", {"path": "a.py", "content": "x"}),
        )
        self.assertIn(
            "wrote",
            call_tool(ws, "write_file", {"path": "tests/test_a.py", "content": "x"}),
        )

    def test_edit_needs_exactly_one_match(self):
        ws = self.ws()
        call_tool(ws, "write_file", {"path": "a.py", "content": "x = 1\nx = 1\n"})
        self.assertIn(
            "found 2 times",
            call_tool(ws, "edit_file", {"path": "a.py", "old": "x = 1", "new": "y"}),
        )
        self.assertIn(
            "edited",
            call_tool(ws, "edit_file", {"path": "a.py", "old": "x = 1\nx", "new": "z"}),
        )

    def test_long_output_goes_to_a_file(self):
        ws = self.ws(output_limit=200)
        out = call_tool(ws, "run", {"command": "python3 -c \"print('x' * 5000)\""})
        self.assertIn("Full output saved to .marathon/outputs/run-1.txt", out)
        self.assertGreater(
            len((self.root / ".marathon/outputs/run-1.txt").read_text()), 5000
        )

    def test_list_search_and_run(self):
        ws = self.ws()
        call_tool(
            ws,
            "write_file",
            {"path": "pkg/mod.py", "content": "def target():\n    pass\n"},
        )
        self.assertIn("pkg/mod.py", call_tool(ws, "list_files", {"pattern": "*.py"}))
        self.assertIn(
            "pkg/mod.py:1:", call_tool(ws, "search", {"pattern": r"def target"})
        )
        self.assertTrue(
            call_tool(ws, "run", {"command": "exit 3"}).startswith("exit code 3")
        )

    def test_bad_arguments_are_reported(self):
        self.assertTrue(
            call_tool(self.ws(), "read_file", {"nope": 1}).startswith("error:")
        )

    def test_schemas_are_openai_style(self):
        s = schemas(["read_file", "checkpoint"])
        self.assertEqual(s[0]["type"], "function")
        self.assertEqual(s[1]["function"]["parameters"]["required"], ["summary"])


if __name__ == "__main__":
    unittest.main()
