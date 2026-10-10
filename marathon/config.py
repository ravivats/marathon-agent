# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Settings for one run. Every knob has a plain default, and every knob can be set three ways.

Highest wins:  command-line flag  >  environment variable  >  .env file  >  default

The .env file (copy .env.example) holds the API keys and the numbers, like how often the goal is
re-read or the tests run. Config() itself never reads the environment, so tests and the demo
always get the plain defaults; the command line uses Config.from_env().
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TEST_CMD = (
    "python3 -I -B -c \"import sys, unittest; sys.path.append('.'); "
    "unittest.main(module=None, argv=['unittest', 'discover', '-s', 'tests', '-v'])\""
)


class SettingError(ValueError):
    pass


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
    score_cmd: str | None = None  # optional: prints a number, higher is better (objective goals)
    min_delta: float = 0.0  # smallest score gain that counts as progress

    # Fix 1: re-read the goal on a schedule
    reread_every_steps: int = 5  # re-read when step % N == 0 (a step = one checkpoint commit)
    reread_every_turns: int = 20  # backup schedule, counted in model turns

    # Fix 2 and 9: run the tests every N steps (1 = at every checkpoint). finish always runs them.
    test_every: int = 1

    # Fix 3: plans are temporary
    switch_after: int = 3  # test runs without progress before the agent is told to switch
    patience: int = 25  # test runs in a row without progress before a fresh start
    max_attempts: int = 5

    # Budgets for an unattended run
    max_turns: int = 600  # model turns across all attempts
    hours: float = 8.0
    force_checkpoint_every: int = 15  # model turns without a checkpoint before the harness makes one

    # Keeping the context small (Fix 5)
    max_history: int = 80  # messages kept before older ones are dropped
    tool_output_limit: int = 6000  # characters shown inline, the rest is saved to a file
    cmd_timeout: int = 300  # seconds for any shell command, tests included

    # Fix 8: where commands run. "local", or a Docker-compatible CLI: docker, podman, nerdctl
    sandbox: str = "local"
    image: str = "python:3.12-slim"
    allow_network: bool = False

    # Models, as "provider:model" (see llm.py). Keys come from ANTHROPIC_API_KEY,
    # OPENAI_API_KEY or OPENROUTER_API_KEY; MARATHON_API_KEY overrides the key for the main model.
    provider: str = "openai"
    model: str = ""
    helper_model: str = ""  # test writer and ask_subagent; "" = same as model
    judge_model: str = ""  # "" = same as model
    base_url: str = ""  # "" = the provider's own
    api_key: str = ""
    max_tokens: int = 16000  # per reply, for providers that need a limit (Anthropic)
    request_timeout: int = 600  # seconds to wait for one model reply; pro models can take minutes

    @property
    def state_dir(self) -> Path:
        return Path(self.repo) / ".marathon"

    @classmethod
    def from_env(cls, repo, env=None) -> "Config":
        """Defaults, then whatever the environment (and so a loaded .env) sets."""
        env = os.environ if env is None else env
        cfg = cls(repo=Path(repo))
        for name, var in ENV.items():
            raw = (env.get(var) or "").strip()
            if raw:
                setattr(cfg, name, _convert(var, raw, getattr(cfg, name)))
        return cfg

    def validate(self) -> None:
        at_least = {"test_every": 1, "patience": 1, "max_attempts": 1, "max_turns": 1,
                    "reread_every_steps": 0, "reread_every_turns": 0, "switch_after": 0,
                    "force_checkpoint_every": 1, "max_history": 4, "cmd_timeout": 1,
                    "tool_output_limit": 200, "max_tokens": 1,
                    "request_timeout": 10}
        for name, low in at_least.items():
            if getattr(self, name) < low:
                raise SettingError(f"{ENV[name]} (--{_flag(name)}) must be at least {low}, "
                                   f"got {getattr(self, name)}")
        if self.hours <= 0:
            raise SettingError(f"MARATHON_HOURS (--hours) must be more than 0, got {self.hours}")


# Setting -> its environment variable (and .env name). Flags use the same words.
ENV = {
    "goal_file": "MARATHON_GOAL_FILE",
    "tests_dir": "MARATHON_TESTS_DIR",
    "test_cmd": "MARATHON_TEST_CMD",
    "protect": "MARATHON_PROTECT",  # comma-separated
    "score_cmd": "MARATHON_SCORE_CMD",
    "min_delta": "MARATHON_MIN_DELTA",
    "reread_every_steps": "MARATHON_REREAD_EVERY",
    "reread_every_turns": "MARATHON_REREAD_EVERY_TURNS",
    "test_every": "MARATHON_TEST_EVERY",
    "switch_after": "MARATHON_SWITCH_AFTER",
    "patience": "MARATHON_PATIENCE",
    "max_attempts": "MARATHON_MAX_ATTEMPTS",
    "max_turns": "MARATHON_MAX_TURNS",
    "hours": "MARATHON_HOURS",
    "force_checkpoint_every": "MARATHON_FORCE_CHECKPOINT_EVERY",
    "max_history": "MARATHON_MAX_HISTORY",
    "tool_output_limit": "MARATHON_TOOL_OUTPUT_LIMIT",
    "cmd_timeout": "MARATHON_CMD_TIMEOUT",
    "sandbox": "MARATHON_SANDBOX",
    "image": "MARATHON_IMAGE",
    "allow_network": "MARATHON_ALLOW_NETWORK",
    "provider": "MARATHON_PROVIDER",
    "model": "MARATHON_MODEL",
    "helper_model": "MARATHON_HELPER_MODEL",
    "judge_model": "MARATHON_JUDGE_MODEL",
    "base_url": "MARATHON_BASE_URL",
    "api_key": "MARATHON_API_KEY",
    "max_tokens": "MARATHON_MAX_TOKENS",
    "request_timeout": "MARATHON_REQUEST_TIMEOUT",
}


def _flag(name: str) -> str:
    return {"reread_every_steps": "reread-every"}.get(name, name.replace("_", "-"))


def _convert(var: str, raw: str, current):
    """Turn the text from the environment into the setting's type, judged by its default."""
    if isinstance(current, bool):
        low = raw.lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise SettingError(f"{var} must be true or false, got {raw!r}")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            raise SettingError(f"{var} must be a whole number, got {raw!r}") from None
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            raise SettingError(f"{var} must be a number, got {raw!r}") from None
    if isinstance(current, list):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


# ---------------------------------------------------------------- .env files

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def read_env_file(path) -> dict[str, str]:
    """NAME=value lines. Blank lines, # comments and a leading `export` are fine. A value can be
    wrapped in single or double quotes; without quotes, ` # ...` at the end is a comment."""
    values = {}
    for n, line in enumerate(Path(path).read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not _NAME.fullmatch(name):
            raise SettingError(f"{path}, line {n}: expected NAME=value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[name] = value
    return values


def load_env_file(path, env=None) -> list[str]:
    """Copy the file's values into the environment. A variable that is already set (and not
    empty) keeps its value, and empty values in the file are skipped. Returns the names set."""
    env = os.environ if env is None else env
    loaded = []
    for name, value in read_env_file(path).items():
        if value and not env.get(name):
            env[name] = value
            loaded.append(name)
    return loaded


def find_env_file(explicit: str | None = None) -> Path | None:
    """--env-file if given, else .env in the current folder, else .env next to this package."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise SettingError(f"--env-file {explicit}: no such file")
        return p
    for p in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if p.is_file():
            return p
    return None
