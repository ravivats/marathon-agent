# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""OpenAI's Responses API, against a stand-in server that follows its documented format."""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from marathon import demo
from marathon.config import Config
from marathon.llm import (ChatLLM, ResponsesLLM, build, call, from_responses, make_llm,
                          to_responses)
from marathon.loop import Harness
from tests.helpers import TempRepoCase


def fcall(n, name, **args):
    """A reasoning item and the function call it led to, as a reasoning model returns them."""
    return [{"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"enc-{n}"},
            {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": name,
             "arguments": json.dumps(args), "status": "completed"}]


def text(t):
    return [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": t, "annotations": []}]}]


class FakeResponses:
    def __init__(self, replies, refuse_encrypted=False):
        self.replies, self.refuse_encrypted, self.requests = replies, refuse_encrypted, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, dict(self.headers), body))
                if outer.refuse_encrypted and "include" in body:
                    data = json.dumps({"error": {"message": "Encrypted content is not supported "
                                                 "with this model.", "param": "include"}}).encode()
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                agent = "judge" if body.get("instructions", "").startswith("You review") else "worker"
                queue = outer.replies.get(agent) or [text("(nothing left)")]
                data = json.dumps({"id": "resp_1", "object": "response", "status": "completed",
                                   "output": queue.pop(0)}).encode()
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


class Convert(unittest.TestCase):
    def test_chat_style_to_responses(self):
        a = call("read_file", path="a.py")
        msgs = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "task"},
                a, {"role": "tool", "tool_call_id": a["tool_calls"][0]["id"], "content": ""}]
        tools = [{"type": "function", "function": {"name": "read_file", "description": "read",
                                                   "parameters": {"type": "object"}}}]
        body = to_responses(msgs, tools, "gpt-x")
        self.assertEqual(body["instructions"], "be brief")
        self.assertIs(body["store"], False)
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertEqual([i.get("type", "message") for i in body["input"]],
                         ["message", "function_call", "function_call_output"])
        self.assertEqual(body["input"][1]["call_id"], body["input"][2]["call_id"])
        self.assertEqual(body["input"][2]["output"], "(no output)")
        self.assertEqual(body["tools"][0], {"type": "function", "name": "read_file",
                                            "description": "read",
                                            "parameters": {"type": "object"}, "strict": False})

    def test_reply_keeps_the_raw_items_for_next_time(self):
        out = fcall(1, "run", command="ls")
        msg = from_responses({"status": "completed", "output": out})
        self.assertEqual(msg["tool_calls"][0]["id"], "call_1")
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"command": "ls"})
        self.assertEqual(msg["_items"], out)
        body = to_responses([{"role": "user", "content": "t"}, msg], None, "gpt-x")
        self.assertEqual(body["input"][1:], out)  # sent back exactly as it came
        cut = from_responses({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                              "output": [{"type": "reasoning", "id": "rs_9", "summary": []}]})
        self.assertIn("max_output_tokens", cut["content"])
        self.assertNotIn("_items", cut)  # a lone reasoning item can't be sent back

    def test_chat_completions_never_sees_the_raw_items(self):
        seen = {}
        with mock.patch("marathon.llm.post_json", side_effect=lambda url, body, *a: seen.update(body)
                        or {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}):
            ChatLLM("m", "http://x/v1", "").complete(
                [{"role": "assistant", "content": "hi", "_items": [{"type": "reasoning"}]}])
        self.assertNotIn("_items", seen["messages"][0])

    def test_openai_uses_responses_and_custom_servers_use_chat(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            self.assertIsInstance(make_llm("openai:gpt-5.5-pro"), ResponsesLLM)
            self.assertIsInstance(make_llm("gpt-x", base_url="http://localhost:8000/v1"), ChatLLM)
            self.assertIsInstance(make_llm("openrouter:x/y", api_key="k"), ChatLLM)


class Client(unittest.TestCase):
    def test_request_and_encrypted_reasoning_fallback(self):
        server = FakeResponses({"worker": [text("hello")]}, refuse_encrypted=True)
        self.addCleanup(server.close)
        llm = ResponsesLLM("gpt-x", server.url, "sk-test")
        with mock.patch("marathon.llm.time.sleep"):
            reply = llm.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(reply["content"], "hello")
        self.assertFalse(llm.reasoning)  # retried without encrypted reasoning
        path, headers, body = server.requests[-1]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual({k.lower(): v for k, v in headers.items()}["authorization"], "Bearer sk-test")
        self.assertNotIn("include", body)


class FullRunOverResponses(TempRepoCase):
    def test_loop_with_the_responses_client(self):
        root = self.make_repo({"goal.md": demo.GOAL, "tests/test_slug.py": demo.TESTS})
        server = FakeResponses({
            "worker": [fcall(1, "write_file", path="slug.py", content=demo.GOOD),
                       fcall(2, "checkpoint", summary="implement slugify"),
                       fcall(3, "finish", summary="done")],
            "judge": [text("PASS")],
        })
        self.addCleanup(server.close)
        cfg = Config(repo=root, model="openai:gpt-5.5-pro", api_key="k")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            llm = build(cfg)
        for role in llm.by_role.values():  # the stand-in server instead of api.openai.com
            role.base_url = server.url
        self.assertEqual(Harness(cfg, llm, echo=lambda *_: None).run(), "done")
        worker = [b for _, _, b in server.requests if "tools" in b]
        last = worker[-1]["input"]
        for i, item in enumerate(last):
            if item.get("type") == "function_call":  # answered right after, by call_id
                outputs = [x["call_id"] for x in last[i + 1:] if x.get("type") == "function_call_output"]
                self.assertIn(item["call_id"], outputs)
        reasoning = [x for x in last if x.get("type") == "reasoning"]
        self.assertEqual([r["encrypted_content"] for r in reasoning], ["enc-1", "enc-2"])
        judge = server.requests[-1][2]
        self.assertNotIn("tools", judge)
        self.assertIn("slugify", judge["input"][0]["content"])


if __name__ == "__main__":
    unittest.main()
