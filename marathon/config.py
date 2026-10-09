# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Settings for one run. Every knob has a plain default. Override with CLI flags or env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


DEFAULT_TEST_CMD = (
    "python3 -I -B -c \"import sys, unittest; sys.path.append('.'); "
    "unittest.main(module=None, argv=['unittest', 'discover', '-s', 'tests', '-v'])\""
)


@dataclass
class Config:
    repo: Path  # the project the agent works on (a git repo with goal.md)
    goal_file: str = "goal.md"
    tests_dir: str = "tests"
    # Fix 9: an isolated run. -I keeps the repo folder off the import path at start-up, so a file
    # like unittest.py or sitecustomize.py in the repo can't fake a pass; the repo goes on the
    # END of the path, after the standard library. -B writes no bytecode caches.
    test_cmd: str = DEFAULT_TEST_CMD
    protect: list[str] = field(default_factory=list)  # more read-only paths, like Makefile
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
    switch_after: int = 3  # after this many, the harness tells the agent to note why and switch
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

    # Fix 8: where commands run. "local", or a Docker-compatible CLI: docker, podman, nerdctl
    sandbox: str = field(default_factory=lambda: _env("MARATHON_SANDBOX", "local"))
    image: str = field(default_factory=lambda: _env("MARATHON_IMAGE", "python:3.12-slim"))
    allow_network: bool = False

    # Models, as "provider:model" (see llm.py). Keys come from ANTHROPIC_API_KEY,
    # OPENAI_API_KEY or OPENROUTER_API_KEY; MARATHON_API_KEY overrides the key for the main model.
    provider: str = field(default_factory=lambda: _env("MARATHON_PROVIDER", "openai"))
    model: str = field(default_factory=lambda: _env("MARATHON_MODEL"))
    helper_model: str = field(default_factory=lambda: _env("MARATHON_HELPER_MODEL"))  # test writer, ask_subagent
    judge_model: str = field(default_factory=lambda: _env("MARATHON_JUDGE_MODEL"))
    base_url: str = field(default_factory=lambda: _env("MARATHON_BASE_URL"))  # "" = the provider's own
    api_key: str = field(default_factory=lambda: _env("MARATHON_API_KEY"))
    max_tokens: int = 16000  # per reply, for providers that need a limit (Anthropic)

    @property
    def state_dir(self) -> Path:
        return Path(self.repo) / ".marathon"
