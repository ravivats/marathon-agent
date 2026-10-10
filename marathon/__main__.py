# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Command line.

python3 -m marathon run --repo PATH  asks for the platform, the model and the goal, then runs
python3 -m marathon init PATH        make a project folder with a goal.md to fill in yourself
python3 -m marathon demo             replay a prepared run in a throwaway repo (offline, no API key)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import gitops
from .config import Config, SettingError, find_env_file, load_env_file

FLAGS = (
    "goal_file",
    "tests_dir",
    "test_cmd",
    "score_cmd",
    "min_delta",
    "reread_every_steps",
    "reread_every_turns",
    "test_every",
    "patience",
    "switch_after",
    "max_attempts",
    "max_turns",
    "hours",
    "force_checkpoint_every",
    "max_history",
    "cmd_timeout",
    "provider",
    "model",
    "helper_model",
    "judge_model",
    "base_url",
    "api_key",
    "max_tokens",
    "request_timeout",
    "sandbox",
    "image",
    "protect",
)

GOAL_TEMPLATE = """\
# Goal

Say what "done" looks like, in plain words.

## Must
- ...

## How to check
Inputs and the outputs you expect. The test writer turns these into tests.
"""


def cmd_init(args) -> int:
    root = Path(args.path)
    root.mkdir(parents=True, exist_ok=True)
    goal = root / "goal.md"
    if not goal.exists():
        goal.write_text(GOAL_TEMPLATE)
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(".marathon/\n__pycache__/\n")
    if not gitops.is_repo(root):
        gitops.init_repo(root)
        gitops.git(root, "add", "-A")
        gitops.git(root, "commit", "-q", "-m", "marathon: start")
    print(
        f"Ready: {root}\nEdit {goal}, then run:\n  python3 -m marathon run --repo {root}"
    )
    return 0


def cmd_run(args) -> int:
    from .llm import LLMError, build
    from .loop import Harness
    from .sandbox import SandboxError

    from . import start

    try:
        cfg = make_config(args)
        if sys.stdin.isatty() and not args.no_input:
            if not start.setup(cfg, os.environ, prompt=args.prompt, model_given=bool(args.model)):
                print("Not started.")
                return 0
        elif args.prompt:
            start.write_goal(Path(cfg.repo) / cfg.goal_file, args.prompt)
    except SettingError as e:
        print(f"Settings error: {e}", file=sys.stderr)
        return 4
    except start.StartError as e:
        print(f"Can't start: {e}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\nNot started.")
        return 130
    try:
        llm = build(cfg)
        print(f"Models: {llm.describe()}")
        outcome = Harness(cfg, llm).run()
    except LLMError as e:
        print(f"Model error: {e}", file=sys.stderr)
        return 2
    except SandboxError as e:
        print(f"Sandbox error: {e}", file=sys.stderr)
        return 3
    return 0 if outcome == "done" else 1


def make_config(args, env=None) -> Config:
    """Flags beat environment variables, which beat .env, which beats the defaults."""
    env_file = find_env_file(getattr(args, "env_file", None))
    if env_file:
        loaded = load_env_file(env_file, env)
        print(f"Settings: {len(loaded)} values from {env_file}")
        repo = Path(args.repo).resolve()
        if repo == env_file.resolve().parent or repo in env_file.resolve().parents:
            print(f"Warning: {env_file} is inside the repo the agent works on. With --sandbox local "
                  "its commands could read your API keys there. Keep .env in the marathon-agent "
                  "folder, or pass --env-file.", file=sys.stderr)
    cfg = Config.from_env(args.repo, env)
    for name in FLAGS:
        value = getattr(args, name, None)
        if value is not None:
            setattr(cfg, name, value)
    if getattr(args, "allow_network", False):
        cfg.allow_network = True
    cfg.validate()
    return cfg


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="marathon", description="A bare-bones loop for long-running agents."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="make a project folder with a goal.md")
    i.add_argument("path")
    i.set_defaults(fn=cmd_init)

    r = sub.add_parser("run", help="run the loop on a repo with a goal.md")
    r.add_argument("--repo", required=True, help="the project folder (created if it's new)")
    r.add_argument(
        "--prompt",
        help="the goal, in your own words, instead of being asked (saved as goal.md)",
    )
    r.add_argument(
        "--no-input",
        dest="no_input",
        action="store_true",
        help="ask nothing: use MARATHON_MODEL and goal.md as they are (for unattended runs)",
    )
    r.add_argument(
        "--env-file",
        dest="env_file",
        help="settings and API keys (default: .env here, or in the marathon-agent folder)",
    )
    r.add_argument("--goal-file", dest="goal_file")
    r.add_argument("--tests-dir", dest="tests_dir")
    r.add_argument(
        "--test-cmd",
        dest="test_cmd",
        help="default: an isolated unittest run of tests/ (see config.py)",
    )
    r.add_argument(
        "--protect",
        action="append",
        metavar="PATH",
        help="another read-only file or folder, like Makefile or conftest.py (repeat for more)",
    )
    r.add_argument(
        "--score-cmd",
        dest="score_cmd",
        help="optional command that prints a number, higher is better",
    )
    r.add_argument("--min-delta", dest="min_delta", type=float)
    r.add_argument(
        "--reread-every",
        dest="reread_every_steps",
        type=int,
        help="re-read the goal when step %% N == 0",
    )
    r.add_argument("--reread-every-turns", dest="reread_every_turns", type=int)
    r.add_argument(
        "--test-every",
        dest="test_every",
        type=int,
        help="run the tests every N steps (1 = every checkpoint); finish always runs them",
    )
    r.add_argument(
        "--patience", type=int, help="checkpoints without progress before a fresh start"
    )
    r.add_argument(
        "--switch-after",
        dest="switch_after",
        type=int,
        help="checkpoints without progress before the agent is told to switch approach (3)",
    )
    r.add_argument("--max-attempts", dest="max_attempts", type=int)
    r.add_argument("--max-turns", dest="max_turns", type=int)
    r.add_argument("--hours", type=float)
    r.add_argument("--force-checkpoint-every", dest="force_checkpoint_every", type=int)
    r.add_argument("--max-history", dest="max_history", type=int)
    r.add_argument("--cmd-timeout", dest="cmd_timeout", type=int)
    r.add_argument(
        "--sandbox",
        help="where commands run: local (default), docker, podman or nerdctl. "
        "Colima works through docker.",
    )
    r.add_argument("--image", help="container image for --sandbox (python:3.12-slim)")
    r.add_argument(
        "--allow-network",
        dest="allow_network",
        action="store_true",
        help="let commands in the container use the network (off by default)",
    )
    r.add_argument(
        "--provider",
        help="default provider for model names without one: openai, anthropic, openrouter, ollama",
    )
    r.add_argument(
        "--model", help='"provider:model", like anthropic:claude-sonnet-5-5 (skips the questions)'
    )
    r.add_argument(
        "--helper-model",
        dest="helper_model",
        help="model for the test writer and ask_subagent (default: --model)",
    )
    r.add_argument(
        "--judge-model", dest="judge_model", help="model for the judge (default: --model)"
    )
    r.add_argument("--base-url", dest="base_url", help="for a custom OpenAI-compatible server")
    r.add_argument(
        "--api-key",
        dest="api_key",
        help="better: set ANTHROPIC_API_KEY, OPENAI_API_KEY or MARATHON_API_KEY",
    )
    r.add_argument("--max-tokens", dest="max_tokens", type=int, help="per reply, for Anthropic (16000)")
    r.add_argument(
        "--request-timeout",
        dest="request_timeout",
        type=int,
        help="seconds to wait for one model reply (600); pro models can take minutes",
    )
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("demo", help="replay a prepared run in a throwaway repo (offline, no API key)")
    d.set_defaults(fn=lambda a: __import__("marathon.demo", fromlist=["main"]).main())

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
