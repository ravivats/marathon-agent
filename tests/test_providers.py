# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Providers and per-role models: the native Anthropic client against a fake Messages API
server, API keys from the environment, and routing each agent to its own model."""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from marathon import demo
from marathon.config import Config
from marathon.llm import (AnthropicLLM, ChatLLM, LLMError, Router, build, call, from_anthropic,
                          make_llm, say, split_spec, to_anthropic)
from marathon.loop import Harness
from tests.helpers import TempRepoCase

NO_KEYS = {k: "" for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
                           "MARATHON_API_KEY")}


def tool_use(id_, name, **args):
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


class FakeAnthropic:
    """Answers POST /v1/messages from queues picked by the system prompt."""

    def __init__(self, replies: dict[str, list], fail_first: int = 0):
        self.replies, self.fail_left, self.requests = replies, fail_first, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, dict(self.headers), body))
                if outer.fail_left:
                    outer.fail_left -= 1
                    self.send_response(529)
                    self.send_header("retry-after", "0")
                    self.end_headers()
                    self.wfile.write(b"overloaded")
                    return
                system = body.get("system", [{"text": ""}])[0]["text"]
                agent = "judge" if system.startswith("You review") else "worker"
                queue = outer.replies.get(agent) or [[{"type": "text", "text": "(nothing left)"}]]
                blocks = queue.pop(0)
                stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
                data = json.dumps({"type": "message", "role": "assistant", "content": blocks,
                                   "stop_reason": stop}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Specs(unittest.TestCase):
    def test_provider_prefix(self):
        self.assertEqual(split_spec("anthropic:claude-sonnet-5-5"), ("anthropic", "claude-sonnet-5-5"))
        self.assertEqual(split_spec("ollama:qwen3:8b"), ("ollama", "qwen3:8b"))
        self.assertEqual(split_spec("qwen3:8b"), ("openai", "qwen3:8b"))  # no known provider
        self.assertEqual(split_spec("gpt-x", "openrouter"), ("openrouter", "gpt-x"))

    def test_keys_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {**NO_KEYS, "ANTHROPIC_API_KEY": "sk-ant"}):
            llm = make_llm("anthropic:claude-x")
            self.assertIsInstance(llm, AnthropicLLM)
            self.assertEqual(llm.api_key, "sk-ant")
            with self.assertRaises(LLMError) as e:
                make_llm("openai:gpt-x")
            self.assertIn("OPENAI_API_KEY", str(e.exception))
            self.assertIsInstance(make_llm("ollama:qwen3:8b"), ChatLLM)  # local, no key
            self.assertIsInstance(make_llm("my-model", base_url="http://localhost:8000/v1"), ChatLLM)
        with self.assertRaises(LLMError):
            make_llm("nope:model")

    def test_roles_get_their_own_models(self):
        env = {**NO_KEYS, "ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-oai"}
        with mock.patch.dict(os.environ, env):
            cfg = Config(repo=".", model="anthropic:claude-big", helper_model="anthropic:claude-small",
                         judge_model="openai:gpt-x", api_key="override")
            r = build(cfg)
        worker, helper, judge = (r.by_role[k] for k in ("worker", "helper", "judge"))
        self.assertEqual((worker.model, helper.model, judge.model), ("claude-big", "claude-small", "gpt-x"))
        self.assertEqual(worker.api_key, "override")  # MARATHON_API_KEY / --api-key: main provider
        self.assertEqual(helper.api_key, "override")
        self.assertEqual(judge.api_key, "sk-oai")  # another provider keeps its own key
        self.assertIn("judge openai:gpt-x", r.describe())

        seen = []

        class Rec:
            def __init__(self, name):
                self.name = name

            def complete(self, messages, tools=None, agent="worker"):
                seen.append((agent, self.name))
                return say("")

        r = Router(Rec("w"), Rec("h"), Rec("j"))
        for agent in ("worker", "test_writer", "subagent", "judge"):
            r.complete([], agent=agent)
        self.assertEqual(seen, [("worker", "w"), ("test_writer", "h"), ("subagent", "h"), ("judge", "j")])

    def test_model_is_required(self):
        with self.assertRaises(LLMError):
            build(Config(repo=".", model=""))


class Convert(unittest.TestCase):
    def test_openai_style_to_messages_api(self):
        a = call("read_file", path="a.py")
        b = call("search", pattern="x")
        msgs = [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "task"},
            {"role": "user", "content": "(older messages dropped)"},
            {"role": "assistant", "content": "looking", "tool_calls": a["tool_calls"] + b["tool_calls"]},
            {"role": "tool", "tool_call_id": a["tool_calls"][0]["id"], "content": "1  x = 1"},
            {"role": "tool", "tool_call_id": b["tool_calls"][0]["id"], "content": ""},
            {"role": "user", "content": "Step 5: re-read the goal"},
        ]
        tools = [{"type": "function", "function": {"name": "read_file", "description": "read",
                                                   "parameters": {"type": "object", "properties": {}}}}]
        body = to_anthropic(msgs, tools, "claude-x", 1000)
        self.assertEqual(body["system"][0]["text"], "be brief")
        self.assertEqual(body["system"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual([t["role"] for t in body["messages"]], ["user", "assistant", "user"])
        self.assertEqual(len(body["messages"][0]["content"]), 2)  # two user messages merged
        assistant = body["messages"][1]["content"]
        self.assertEqual([x["type"] for x in assistant], ["text", "tool_use", "tool_use"])
        self.assertEqual(assistant[1]["input"], {"path": "a.py"})
        last = body["messages"][2]["content"]
        self.assertEqual([x["type"] for x in last], ["tool_result", "tool_result", "text"])
        self.assertEqual(last[0]["tool_use_id"], a["tool_calls"][0]["id"])
        self.assertEqual(last[1]["content"], "(no output)")  # empty results aren't allowed
        self.assertEqual(last[-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["tools"][0]["input_schema"], {"type": "object", "properties": {}})
        self.assertIn("cache_control", body["tools"][-1])
        self.assertEqual(body["max_tokens"], 1000)

    def test_messages_api_reply_to_openai_style(self):
        msg = from_anthropic({"content": [{"type": "text", "text": "ok"},
                                          tool_use("toolu_1", "run", command="ls")],
                              "stop_reason": "tool_use"})
        self.assertEqual(msg["content"], "ok")
        self.assertEqual(msg["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"command": "ls"})
        cut = from_anthropic({"content": [{"type": "text", "text": "half"}], "stop_reason": "max_tokens"})
        self.assertIn("max_tokens", cut["content"])


class Client(unittest.TestCase):
    def test_headers_body_and_retry(self):
        server = FakeAnthropic({"worker": [[{"type": "text", "text": "hi"}]]}, fail_first=1)
        self.addCleanup(server.close)
        with mock.patch("marathon.llm.time.sleep") as sleep:
            reply = AnthropicLLM("claude-x", server.url, "sk-test", max_tokens=123).complete(
                [{"role": "system", "content": "s"}, {"role": "user", "content": "hello"}])
        self.assertEqual(reply["content"], "hi")
        sleep.assert_called_once()
        path, headers, body = server.requests[-1]
        self.assertEqual(path, "/v1/messages")
        headers = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(headers["x-api-key"], "sk-test")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", headers)
        self.assertEqual((body["model"], body["max_tokens"]), ("claude-x", 123))


class FullRunOverAnthropic(TempRepoCase):
    def test_loop_with_the_anthropic_client(self):
        root = self.make_repo({"goal.md": demo.GOAL, "tests/test_slug.py": demo.TESTS})
        server = FakeAnthropic(
            {
                "worker": [
                    [{"type": "text", "text": "Writing it."},
                     tool_use("toolu_a", "write_file", path="slug.py", content=demo.GOOD)],
                    [tool_use("toolu_b", "checkpoint", summary="implement slugify")],
                    [tool_use("toolu_c", "finish", summary="done")],
                ],
                "judge": [[{"type": "text", "text": "PASS"}]],
            }
        )
        self.addCleanup(server.close)
        cfg = Config(repo=root, model="anthropic:claude-x", base_url=server.url, api_key="k")
        self.assertEqual(Harness(cfg, build(cfg), echo=lambda *_: None).run(), "done")
        for _, _, body in server.requests:
            turns = body["messages"]
            self.assertEqual(turns[0]["role"], "user")
            for prev, nxt in zip(turns, turns[1:]):
                self.assertNotEqual(prev["role"], nxt["role"])  # strictly alternating
            for i, t in enumerate(turns):  # every tool_use is answered in the next turn
                for blk in t["content"]:
                    if blk["type"] == "tool_use":
                        ids = [x.get("tool_use_id") for x in turns[i + 1]["content"]]
                        self.assertIn(blk["id"], ids)
        judge = server.requests[-1][2]
        self.assertNotIn("tools", judge)
        self.assertIn("slugify", judge["messages"][0]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
