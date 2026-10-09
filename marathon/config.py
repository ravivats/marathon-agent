# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Settings for one run. Every knob has a plain default. Override with CLI flags or env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    repo: Path  # the project the agent works on (a git repo with goal.md)
    goal_file: str = "goal.md"
    tests_dir: str = "tests"
    test_cmd: str = "python3 -m unittest discover -s tests -v"
    score_cmd: str | None = (
        None  # optional: prints a number, higher is better (objective goals)
    )
    min_delta: float = 0.0  # smallest score gain that counts as progress

    # Fix 1: re-read the goal on a schedule
    reread_every_steps: int = (
        5  # re-read when step % N == 0 (a step = one checkpoint commit)
    )
    reread_every_turns: int = 20  # backup schedule, counted in model turns

    # Fix 3: plans are temporary
    patience: int = 25  # checkpoints in a row with no progress before a fresh start
    max_attempts: int = 5

    # Budgets for an unattended run
    max_turns: int = 600  # model turns across all attempts
    hours: float = 8.0
    force_checkpoint_every: int = (
        15  # model turns without a checkpoint before the harness makes one
    )

    # Keeping the context small (Fix 5)
    max_history: int = 80  # messages kept before older ones are dropped
    tool_output_limit: int = (
        6000  # characters shown inline, the rest is saved to a file
    )
    cmd_timeout: int = 300  # seconds for any shell command, tests included

    # Model: any OpenAI-compatible /chat/completions endpoint
    model: str = field(default_factory=lambda: _env("MARATHON_MODEL"))
    base_url: str = field(
        default_factory=lambda: _env("MARATHON_BASE_URL", "https://api.openai.com/v1")
    )
    api_key: str = field(
        default_factory=lambda: _env("MARATHON_API_KEY") or _env("OPENAI_API_KEY")
    )

    @property
    def state_dir(self) -> Path:
        return Path(self.repo) / ".marathon"
