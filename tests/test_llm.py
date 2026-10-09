# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""The real HTTP client, against a fake OpenAI-compatible server running on localhost."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from marathon import demo
from marathon.config import Config
from marathon.llm import ChatLLM, LLMError, call, say
from marathon.loop import Harness
from tests.helpers import TempRepoCase


class FakeServer:
    """Answers /chat/completions from queues picked by the system prompt."""

    def __init__(self, replies: dict[str, list], fail_first: int = 0):
        self.replies, self.fail_left, self.requests = replies, fail_first, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, dict(self.headers), body))
                if outer.fail_left:
                    outer.fail_left -= 1
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"busy")
                    return
                system = body["messages"][0]["content"]
                agent = "judge" if system.startswith("You review") else "worker"
                queue = outer.replies.get(agent) or [say("(nothing left)")]
                msg = queue.pop(0)
                data = json.dumps({"choices": [{"message": msg}]}).encode()
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


class Client(unittest.TestCase):
    def test_request_shape_and_retry(self):
        server = FakeServer({"worker": [call("list_files")]}, fail_first=2)
        self.addCleanup(server.close)
        llm = ChatLLM("some-model", server.url, "sk-test")
        with mock.patch("marathon.llm.time.sleep"):
            reply = llm.complete(
                [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "list_files", "parameters": {}},
                    }
                ],
            )
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "list_files")
        self.assertEqual(len(server.requests), 3)
        path, headers, body = server.requests[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(body["model"], "some-model")
        self.assertIn("tools", body)

    def test_gives_up_after_retries(self):
        server = FakeServer({}, fail_first=10)
        self.addCleanup(server.close)
        with mock.patch("marathon.llm.time.sleep"):
            with self.assertRaises(LLMError):
                ChatLLM("m", server.url, "", retries=2).complete(
                    [{"role": "user", "content": "hi"}]
                )

    def test_model_is_required(self):
        with self.assertRaises(LLMError):
            ChatLLM("", "http://localhost", "")


class FullRunOverHTTP(TempRepoCase):
    def test_loop_with_the_real_client(self):
        root = self.make_repo({"goal.md": demo.GOAL, "tests/test_slug.py": demo.TESTS})
        server = FakeServer(
            {
                "worker": [
                    call("write_file", path="slug.py", content=demo.GOOD),
                    call("checkpoint", summary="implement slugify"),
                    call("finish", summary="done"),
                ],
                "judge": [say("PASS")],
            }
        )
        self.addCleanup(server.close)
        cfg = Config(repo=root, model="m", base_url=server.url, api_key="k")
        outcome = Harness(
            cfg, ChatLLM(cfg.model, cfg.base_url, cfg.api_key), echo=lambda *_: None
        ).run()
        self.assertEqual(outcome, "done")
        # every tool call the server sent got a matching tool result in the next request
        for _, _, body in server.requests:
            msgs = body["messages"]
            for i, m in enumerate(msgs):
                for c in m.get("tool_calls") or []:
                    self.assertIn(
                        c["id"], [x.get("tool_call_id") for x in msgs[i + 1 :]]
                    )


if __name__ == "__main__":
    unittest.main()
