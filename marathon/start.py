# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""The questions before a run, when marathon runs in a terminal.

1. Platform: only the platforms you have an API key for, in .env or the environment.
   With one key, that platform is used without asking.
2. Model: the models on that platform, newest first, or any model name you type.
3. Goal: what the agent should do, in your own words. It's saved as goal.md in the project.

Then a short summary and one last "Start?". Flags answer their question up front: --model skips
1 and 2, --prompt skips 3. With --no-input, or without a terminal, nothing is asked.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .llm import PROVIDERS, list_models, split_spec

PLATFORMS = [
    # provider, name shown, env var with the key, env var with the model to suggest
    ("anthropic", "Anthropic", "ANTHROPIC_API_KEY", "MARATHON_ANTHROPIC_MODEL"),
    ("openai", "OpenAI", "OPENAI_API_KEY", "MARATHON_OPENAI_MODEL"),
    ("openrouter", "OpenRouter", "OPENROUTER_API_KEY", "MARATHON_OPENROUTER_MODEL"),
]
MODEL_VARS = [p[3] for p in PLATFORMS]
SUGGESTED = {"anthropic": "claude-sonnet-5-5"}
SHOWN = 12


class StartError(RuntimeError):
    pass


class Terminal:
    def ask(self, prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            raise StartError("no answer: the input was closed") from None

    def say(self, text: str = "") -> None:
        print(text)


def platforms_with_keys(env) -> list[tuple[str, str, str, str]]:
    return [p for p in PLATFORMS if (env.get(p[2]) or "").strip()]


def choose(io, options: list[str], default: int = 0) -> int:
    for i, text in enumerate(options, start=1):
        io.say(f"  {i}. {text}")
    while True:
        raw = io.ask(f"Choose 1-{len(options)} [{default + 1}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        io.say(f"Type a number from 1 to {len(options)}.")


def pick_model(io, env, cfg: Config, models_for=list_models) -> str:
    """Steps 1 and 2. Returns "provider:model"."""
    keyed = platforms_with_keys(env)
    current, current_model = split_spec(cfg.model, cfg.provider) if cfg.model else ("", "")
    # a model that needs no key (Ollama, or your own server) set in MARATHON_MODEL
    keyless = (cfg.model if cfg.model and current not in [p[0] for p in keyed]
               and (not PROVIDERS.get(current, ("", "", "?"))[2] or cfg.base_url) else "")
    if not keyed and not keyless:
        raise StartError(
            "no API key found. Put one in .env (see .env.example): "
            + ", ".join(p[2] for p in PLATFORMS)
            + ". For a local model, set MARATHON_MODEL=ollama:<model>.")
    labels = [f"{name}  ({key} found)" for _, name, key, _ in keyed]
    if keyless:
        labels.append(f"{keyless}  (from MARATHON_MODEL, no key needed)")
    if len(labels) == 1:
        io.say(f"Platform: {labels[0]}")
        idx = 0
    else:
        io.say("Platforms you have an API key for:")
        default = next((i for i, p in enumerate(keyed) if p[0] == current), 0)
        idx = choose(io, labels, default)
    if keyless and idx == len(keyed):
        return keyless

    provider, name, key_var, model_var = keyed[idx]
    suggested = ((current_model if current == provider else "")
                 or (env.get(model_var) or "").strip() or SUGGESTED.get(provider, ""))
    io.say(f"Looking up models on {name} ...")
    found = models_for(provider, env.get(key_var, ""))
    if found:
        names = (([suggested] if suggested else []) + [m for m in found if m != suggested])[:SHOWN]
        io.say(f"Models on {name}, newest first:")
        for i, m in enumerate(names, start=1):
            io.say(f"  {i}. {m}" + ("  (suggested)" if m == suggested else ""))
        prompt = f"Choose 1-{len(names)}, or type a model name [1]: "
    else:
        io.say(f"Couldn't list the models on {name}. If this keeps happening, check {key_var}.")
        names = [suggested] if suggested else []
        prompt = f"Model [{suggested}]: " if suggested else "Model name: "
    while True:
        raw = io.ask(prompt).strip()
        if not raw and names:
            return f"{provider}:{names[0]}"
        if raw.isdigit() and found and 1 <= int(raw) <= len(names):
            return f"{provider}:{names[int(raw) - 1]}"
        if raw and not (raw.isdigit() and found):
            return f"{provider}:{raw}"
        io.say("Choose a number from the list, or type a model name.")


def read_goal(io, path: Path) -> str | None:
    """Step 3. The goal in the person's own words, or None to keep the goal.md that's there."""
    old = path.read_text().strip() if path.is_file() else ""
    if old:
        lines = old.splitlines()
        io.say(f"{path.name} in this project says:")
        for line in lines[:8]:
            io.say(f"  | {line}")
        if len(lines) > 8:
            io.say("  | ...")
        io.say("Type a new goal to replace it, or press Enter to keep it.")
    else:
        io.say("What should the agent do? Describe the goal in your own words: what to build,")
        io.say("and how you'll know it's done.")
    io.say("Finish with an empty line.")
    while True:
        lines = []
        while True:
            line = io.ask("> ")
            if not line.strip():
                break
            lines.append(line.rstrip())
        text = "\n".join(lines).strip()
        if text:
            return text
        if old:
            return None
        io.say("The goal needs at least one line. Type it, then an empty line.")


def write_goal(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text.strip()
    path.write_text(body + "\n" if body.startswith("#") else f"# Goal\n\n{body}\n")


def summary(cfg: Config, goal: str) -> list[str]:
    first = next((l.strip("# ").strip() for l in goal.splitlines() if l.strip("# ").strip()
                  and l.strip("# ").strip().lower() != "goal"), "")
    roles = ""
    if cfg.helper_model or cfg.judge_model:
        roles = f" (helpers {cfg.helper_model or 'the same'}, judge {cfg.judge_model or 'the same'})"
    where = ("directly on this machine" if cfg.sandbox in ("", "local", "none") else
             f"in {cfg.sandbox} containers from {cfg.image}, "
             f"{'with' if cfg.allow_network else 'no'} network")
    return [
        f"  project   {Path(cfg.repo).resolve()}",
        f"  goal      {first[:70] + ('...' if len(first) > 70 else '')}",
        f"  model     {cfg.model}{roles}",
        f"  commands  {where}",
        f"  budget    {cfg.hours:g} hours or {cfg.max_turns} model turns",
    ]


def setup(cfg: Config, env, prompt: str | None = None, model_given: bool = False, io=None,
          models_for=list_models) -> bool:
    """Ask what isn't settled yet, then confirm. goal.md is written only after a yes."""
    io = io or Terminal()
    goal_path = Path(cfg.repo) / cfg.goal_file
    if not model_given:
        cfg.model = pick_model(io, env, cfg, models_for)
        io.say()
    new_goal = prompt if prompt else read_goal(io, goal_path)
    goal = new_goal if new_goal is not None else goal_path.read_text()
    io.say()
    io.say("Ready to start:")
    for line in summary(cfg, goal):
        io.say(line)
    if io.ask("Start? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        return False
    if new_goal is not None:
        write_goal(goal_path, new_goal)
    return True
