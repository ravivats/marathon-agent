# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

import unittest

from marathon.checks import parse_counts, run_tests
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


if __name__ == "__main__":
    unittest.main()
