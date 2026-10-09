# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

import unittest

from marathon.checks import parse_counts, run_tests
from marathon.config import DEFAULT_TEST_CMD
from tests.helpers import TempRepoCase


class ParseCounts(unittest.TestCase):
    def test_unittest_ok(self):
        self.assertEqual(
            parse_counts("....\nRan 4 tests in 0.01s\n\nOK\n", 0), (4, 0, 4)
        )

    def test_unittest_failures_and_errors(self):
        out = "Ran 5 tests in 0.1s\n\nFAILED (failures=2, errors=1)\n"
        self.assertEqual(parse_counts(out, 1), (2, 3, 5))

    def test_unittest_skipped_does_not_count_as_passing(self):
        self.assertEqual(
            parse_counts("Ran 3 tests in 0s\n\nOK (skipped=1)\n", 0), (2, 1, 3)
        )

    def test_pytest(self):
        self.assertEqual(
            parse_counts("==== 3 passed, 2 failed in 0.5s ====", 1), (3, 2, 5)
        )

    def test_unknown_runner_falls_back_to_exit_code(self):
        self.assertEqual(parse_counts("all good", 0), (1, 0, 1))
        self.assertEqual(parse_counts("boom", 2), (0, 1, 1))


class RunTests(TempRepoCase):
    def test_runs_unittest_and_score(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self): self.assertTrue(True)\n"
            "    def test_bad(self): self.assertTrue(False)\n"
        )
        r = run_tests(
            self.root,
            "python3 -m unittest discover -s tests -v",
            60,
            score_cmd="echo latency 12.5 ms",
        )
        self.assertEqual((r.passed, r.total), (1, 2))
        self.assertFalse(r.all_passed)
        self.assertEqual(r.score, 12.5)


    def test_default_command_ignores_files_that_fake_a_pass(self):
        """Fix 9: a unittest.py or sitecustomize.py next to the code can't fake a passing run."""
        (self.root / "tests").mkdir()  # note: no tests/__init__.py needed
        (self.root / "tests" / "test_app.py").write_text(
            "import unittest\nfrom app import f\nclass T(unittest.TestCase):\n"
            "    def test_a(self): self.assertEqual(f(), 1)\n"
            "    def test_b(self): self.assertEqual(f(), 1)\n"
        )
        (self.root / "app.py").write_text("def f():\n    return 0\n")
        (self.root / "unittest.py").write_text(
            "import sys\nprint('Ran 2 tests in 0.001s\\n\\nOK', file=sys.stderr)\nsys.exit(0)\n"
        )
        (self.root / "sitecustomize.py").write_text("raise SystemExit(0)\n")
        r = run_tests(self.root, DEFAULT_TEST_CMD, 60)
        self.assertEqual((r.passed, r.total), (0, 2))
        self.assertFalse(r.all_passed)
        # the plain command is fooled by the same files
        fooled = run_tests(self.root, "python3 -m unittest discover -s tests -v", 60)
        self.assertTrue(fooled.all_passed)
        # an honest fix passes
        (self.root / "unittest.py").unlink()
        (self.root / "sitecustomize.py").unlink()
        (self.root / "app.py").write_text("def f():\n    return 1  # fixed\n")
        self.assertTrue(run_tests(self.root, DEFAULT_TEST_CMD, 60).all_passed)

if __name__ == "__main__":
    unittest.main()
