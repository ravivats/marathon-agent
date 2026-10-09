# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Model clients. Standard library only.

ChatLLM       any OpenAI-compatible /chat/completions endpoint: OpenAI, OpenRouter, Ollama,
              LiteLLM, vLLM.
AnthropicLLM  Claude through Anthropic's own Messages API, with prompt caching.
Router        sends each agent to its own model: the worker, the helpers (test writer and
              ask_subagent) and the judge. A different model for the judge is a cheap way to
              get a second opinion.
build(cfg)    turns "provider:model" settings and API keys from the environment into a Router.

The harness speaks one message format everywhere (OpenAI-style). AnthropicLLM converts on the way
in and out. Also has ScriptedLLM, a fake that plays back canned replies, for tests and the demo.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable

RETRY_CODES = {408, 409, 429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    pass


def clean_message(msg: dict) -> dict:
    """Keep only the fields we send back to the API."""
    out = {"role": "assistant", "content": msg.get("content")}
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        calls.append(
            {
                "id": c.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments") or "{}",
                },
            }
        )
    if calls:
        out["tool_calls"] = calls
    return out


class ChatLLM:
    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        timeout: int = 300,
        retries: int = 6,
    ):
        if not model:
            raise LLMError("No model set. Use --model or the MARATHON_MODEL env var.")
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key
        self.timeout, self.retries = timeout, retries

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        agent: str = "worker",
    ) -> dict:
        body = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = post_json(f"{self.base_url}/chat/completions", body, headers,
                            self.timeout, self.retries)
        try:
            return clean_message(payload["choices"][0]["message"])
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected reply: {str(payload)[:300]}") from e


def post_json(url: str, body: dict, headers: dict, timeout: int, retries: int) -> dict:
    """POST with retries on rate limits, overload and network errors. Honors Retry-After."""
    data = json.dumps(body).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read()[:500].decode(errors="replace")
            wait = e.headers.get("retry-after") if e.headers else None
            e.close()
            if e.code in RETRY_CODES and attempt < retries:
                try:
                    pause = float(wait)
                except (TypeError, ValueError):
                    pause = 2**attempt
                time.sleep(min(60, max(pause, 1)))
                continue
            raise LLMError(f"HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(min(60, 2**attempt))
                continue
            raise LLMError(str(e)) from e
    raise LLMError("unreachable")


# ---------------------------------------------------------------- Anthropic

CACHE = {"type": "ephemeral"}


def to_anthropic(messages: list[dict], tools: list[dict] | None, model: str,
                 max_tokens: int) -> dict:
    """OpenAI-style messages and tools -> a Messages API request body.

    System messages go to `system`. Tool results become tool_result blocks in a user turn, and
    turns from the same role are merged, since the API wants user and assistant to alternate.
    Three cache breakpoints (system, tools, end of the conversation) let each call read the
    long, growing prefix of a marathon run from the cache."""
    system: list[dict] = []
    turns: list[dict] = []

    def add(role: str, blocks: list[dict]) -> None:
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": list(blocks)})

    for m in messages:
        role, content = m.get("role"), m.get("content")
        if role == "system":
            if content and content.strip():
                system.append({"type": "text", "text": content})
        elif role == "user":
            add("user", [{"type": "text", "text": content if (content or "").strip() else "(empty)"}])
        elif role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"],
                          "content": str(content) if str(content or "").strip() else "(no output)"}])
        elif role == "assistant":
            blocks = [{"type": "text", "text": content}] if (content or "").strip() else []
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": c["id"], "name": fn.get("name", ""),
                               "input": args if isinstance(args, dict) else {}})
            add("assistant", blocks or [{"type": "text", "text": "(no reply)"}])
    for t in turns:  # in a user turn, tool results come before any text
        if t["role"] == "user":
            t["content"].sort(key=lambda b: b["type"] != "tool_result")
    body: dict = {"model": model, "max_tokens": max_tokens, "messages": turns}
    if system:
        system[-1]["cache_control"] = CACHE
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t["function"]["name"],
                          "description": t["function"].get("description", ""),
                          "input_schema": t["function"]["parameters"]} for t in tools]
        body["tools"][-1]["cache_control"] = CACHE
    if turns:
        turns[-1]["content"][-1]["cache_control"] = CACHE
    return body


def from_anthropic(payload: dict) -> dict:
    """A Messages API response -> an OpenAI-style assistant message."""
    blocks = payload.get("content") or []
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    calls = [{"id": b["id"], "type": "function",
              "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
             for b in blocks if b.get("type") == "tool_use"]
    if payload.get("stop_reason") == "max_tokens":
        text += "\n(The reply was cut off at max_tokens. Write big files in smaller pieces.)"
    return clean_message({"content": text.strip() or None, "tool_calls": calls})


class AnthropicLLM:
    """Claude through the Messages API. Same interface as ChatLLM."""

    version = "2023-06-01"

    def __init__(self, model: str, base_url: str = "https://api.anthropic.com/v1",
                 api_key: str = "", max_tokens: int = 16000, timeout: int = 300, retries: int = 6):
        if not model:
            raise LLMError("No model set. Use --model or the MARATHON_MODEL env var.")
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key
        self.max_tokens, self.timeout, self.retries = max_tokens, timeout, retries

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 agent: str = "worker") -> dict:
        body = to_anthropic(messages, tools, self.model, self.max_tokens)
        headers = {"content-type": "application/json", "anthropic-version": self.version}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        payload = post_json(f"{self.base_url}/messages", body, headers, self.timeout, self.retries)
        return from_anthropic(payload)


# ---------------------------------------------------------------- providers and routing

PROVIDERS = {
    # name: (API style, default base URL, env var that holds the key)
    "openai": ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "openrouter": ("openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("openai", "http://localhost:11434/v1", ""),
}


def split_spec(spec: str, default_provider: str = "openai") -> tuple[str, str]:
    """"anthropic:claude-x" -> ("anthropic", "claude-x"). Without a known provider in front,
    the whole string is the model name (so "qwen3:8b" stays a model name)."""
    head, sep, rest = spec.partition(":")
    if sep and head.lower() in PROVIDERS:
        return head.lower(), rest
    return default_provider.lower(), spec


def make_llm(spec: str, default_provider: str = "openai", base_url: str = "",
             api_key: str = "", max_tokens: int = 16000):
    provider, model = split_spec(spec, default_provider)
    if provider not in PROVIDERS:
        raise LLMError(f"Unknown provider {provider!r}. Use one of: {', '.join(PROVIDERS)}.")
    style, default_url, key_env = PROVIDERS[provider]
    key = api_key or (os.environ.get(key_env, "") if key_env else "")
    if key_env and not key and not base_url:
        raise LLMError(f"No API key for {provider}. Set {key_env} (or MARATHON_API_KEY).")
    url = base_url or default_url
    if style == "anthropic":
        return AnthropicLLM(model, url, key, max_tokens=max_tokens)
    return ChatLLM(model, url, key)


ROLES = {"worker": "worker", "test_writer": "helper", "subagent": "helper", "judge": "judge"}


class Router:
    """One model per role. Helpers and the judge fall back to the worker's model."""

    def __init__(self, worker, helper=None, judge=None, names: dict | None = None):
        self.by_role = {"worker": worker, "helper": helper or worker, "judge": judge or worker}
        self.names = names or {}

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 agent: str = "worker") -> dict:
        llm = self.by_role[ROLES.get(agent, "worker")]
        return llm.complete(messages, tools=tools, agent=agent)

    def describe(self) -> str:
        return ", ".join(f"{role} {name}" for role, name in self.names.items())


def build(cfg) -> Router:
    """Models from the settings. --base-url and MARATHON_API_KEY apply to every role that
    uses the main model's provider; other providers use their own URL and key env var."""
    if not cfg.model:
        raise LLMError("No model set. Use --model or MARATHON_MODEL, "
                       "for example anthropic:claude-sonnet-5-5.")
    main, _ = split_spec(cfg.model, cfg.provider)

    def make(spec: str):
        provider, _ = split_spec(spec, cfg.provider)
        same = provider == main
        return make_llm(spec, cfg.provider, cfg.base_url if same else "",
                        cfg.api_key if same else "", cfg.max_tokens)

    def full(spec: str) -> str:
        return ":".join(split_spec(spec, cfg.provider))

    names = {"worker": full(cfg.model), "helpers": full(cfg.helper_model or cfg.model),
             "judge": full(cfg.judge_model or cfg.model)}
    return Router(make(cfg.model),
                  make(cfg.helper_model) if cfg.helper_model else None,
                  make(cfg.judge_model) if cfg.judge_model else None,
                  names)


# ---------------------------------------------------------------- offline fake


def call(name: str, **args) -> dict:
    """An assistant message that calls one tool."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def say(text: str) -> dict:
    """An assistant message with plain text."""
    return {"role": "assistant", "content": text}


class ScriptedLLM:
    """Plays back canned replies, one queue per agent ("worker", "test_writer", "judge", "subagent").

    An item is either a message dict or a function(messages) -> message dict.
    """

    def __init__(self, scripts: dict[str, list[dict | Callable[[list[dict]], dict]]]):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.seen: list[tuple[str, list[dict]]] = []

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        agent: str = "worker",
    ) -> dict:
        self.seen.append((agent, [dict(m) for m in messages]))
        queue = self.scripts.get(agent) or []
        if not queue:
            return say("(script finished)")
        item = queue.pop(0)
        return item(messages) if callable(item) else item
