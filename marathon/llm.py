# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""A tiny client for any OpenAI-compatible /chat/completions endpoint.

Works with OpenAI, OpenRouter, LiteLLM, vLLM, Ollama (http://localhost:11434/v1) and
Anthropic's OpenAI-compatible endpoint. Standard library only.

Also has ScriptedLLM, a fake that plays back canned replies, for tests and the offline demo.
"""

from __future__ import annotations

import json
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
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=data, headers=headers
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.load(resp)
                return clean_message(payload["choices"][0]["message"])
            except urllib.error.HTTPError as e:
                detail = e.read()[:500].decode(errors="replace")
                e.close()
                if e.code in RETRY_CODES and attempt < self.retries:
                    time.sleep(min(60, 2**attempt))
                    continue
                raise LLMError(f"HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.retries:
                    time.sleep(min(60, 2**attempt))
                    continue
                raise LLMError(str(e)) from e
        raise LLMError("unreachable")


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
