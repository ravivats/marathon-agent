# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Deterministic checks the agent can't skip (Fix 9). The harness runs them, never the model.
With a container sandbox, every run gets a fresh container (see sandbox.py)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .sandbox import LocalShell


@dataclass
class TestResult:
    passed: int
    failed: int
    total: int
    exit_code: int
    output: str
    score: float | None = None

    @property
    def all_passed(self) -> bool:
        return self.exit_code == 0 and self.failed == 0 and self.total > 0

    def summary(self) -> str:
        s = f"{self.passed}/{self.total} tests pass"
        if self.score is not None:
            s += f", score {self.score:g}"
        return s

    def key(self) -> tuple[int, float]:
        """What counts as progress: more passing tests, then a higher score."""
        return (self.passed, self.score if self.score is not None else 0.0)


def parse_counts(output: str, exit_code: int) -> tuple[int, int, int]:
    """Read pass/fail counts from unittest or pytest output. Falls back to the exit code."""
    m = re.search(r"Ran (\d+) tests?", output)
    if m:  # unittest
        total = int(m.group(1))
        bad = sum(int(n) for n in re.findall(r"(?:failures|errors|skipped)=(\d+)", output.split("Ran ")[-1]))
        return total - bad, bad, total
    counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|errors?|skipped)", output)}
    if counts:  # pytest
        passed = counts.get("passed", 0)
        failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0) + counts.get("skipped", 0)
        return passed, failed, passed + failed
    return (1, 0, 1) if exit_code == 0 else (0, 1, 1)


Runner = Callable[[str, int], "tuple[int, str]"]


def run_tests(cwd: Path, cmd: str, timeout: int = 300, score_cmd: str | None = None,
              runner: Runner | None = None) -> TestResult:
    """Run the tests (and the optional score command). `runner(command, timeout)` decides
    where: by default on this machine, in `cwd`."""
    _sh = runner or (lambda c, t: LocalShell(cwd).run(c, t))
    code, out = _sh(cmd, timeout)
    passed, failed, total = parse_counts(out, code)
    score = None
    if score_cmd:
        _, sout = _sh(score_cmd, timeout)
        nums = re.findall(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", sout)
        score = float(nums[-1]) if nums else None
    return TestResult(passed, failed, total, code, out, score)
