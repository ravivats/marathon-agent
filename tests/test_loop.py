# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""End-to-end tests of the loop with a scripted model. No network."""

import unittest

from marathon import demo, gitops
from marathon.config import Config
from marathon.llm import ScriptedLLM, call, say
from marathon.loop import Harness
from tests.helpers import TempRepoCase

GOOD = demo.GOOD
BAD = "def slugify(text):\n    return text\n"


def harness(root, llm, **kw):
    cfg = Config(repo=root, model="scripted", **kw)
    return Harness(cfg, llm, echo=lambda *_: None)


class Loop(TempRepoCase):
    def repo_with_tests(self):
        return self.make_repo({"goal.md": demo.GOAL, "tests/test_slug.py": demo.TESTS})

    def kinds(self, h, kind):
        return [e for e in h.events if e["kind"] == kind]

    def test_demo_script_end_to_end(self):
        (self.root / "goal.md").write_text(demo.GOAL)
        llm = demo.script()
        h = harness(self.root, llm, patience=3, reread_every_steps=2, max_attempts=3)
        self.assertEqual(h.run(), "done")
        # the test writer ran first, in its own context
        self.assertEqual(llm.seen[0][0], "test_writer")
        # attempt 1 was dropped, attempt 2 finished
        self.assertEqual(len(self.kinds(h, "drop")), 1)
        self.assertEqual([e["attempt"] for e in self.kinds(h, "attempt")], [1, 2])
        branches = gitops.git(self.root, "branch", "--format=%(refname:short)")
        self.assertIn(f"marathon/{h.run_id}/attempt-1", branches)
        self.assertIn(f"marathon/{h.run_id}/attempt-2", branches)
        # the goal was re-read at step 2 in both attempts (step % 2 == 0)
        self.assertEqual(
            [(e["attempt"], e["step"]) for e in self.kinds(h, "reread")],
            [(1, 2), (2, 2)],
        )
        # the gutted test never made it into a commit
        tip = gitops.git(
            self.root, "show", f"marathon/{h.run_id}/attempt-1:tests/test_slug.py"
        )
        self.assertEqual(tip.strip(), demo.TESTS.strip())

    def test_test_writer_commits_only_tests(self):
        (self.root / "goal.md").write_text(demo.GOAL)
        llm = ScriptedLLM(
            {
                "test_writer": [
                    call("write_file", path="tests/test_slug.py", content=demo.TESTS),
                    call("run", command="echo 'def slugify(t): return t' > slug.py"),
                    call("finish", summary="tests"),
                ],
                "worker": [call("list_files")],
            }
        )
        h = harness(self.root, llm, max_turns=1)
        h.run()
        files = gitops.git(
            self.root, "ls-tree", "-r", "--name-only", f"marathon/{h.run_id}/tests"
        )
        self.assertEqual(
            files.splitlines(), [".gitignore", "goal.md", "tests/test_slug.py"]
        )

    def test_reread_puts_goal_and_checklist_at_the_end(self):
        root = self.repo_with_tests()
        llm = ScriptedLLM(
            {
                "worker": [
                    call(
                        "write_file",
                        path="PROGRESS.md",
                        content="- [ ] my checklist item\n",
                    ),
                    call("checkpoint", summary="one"),
                    call("checkpoint", summary="two"),
                    say("thinking"),
                ]
            }
        )
        h = harness(root, llm, reread_every_steps=2, max_turns=4, patience=10)
        self.assertEqual(h.run(), "budget")
        last_worker_call = [m for a, m in llm.seen if a == "worker"][-1]
        reread = [
            m["content"]
            for m in last_worker_call
            if m["role"] == "user" and "re-read the goal" in m["content"]
        ]
        self.assertEqual(len(reread), 1)
        self.assertIn("Write `slugify(text)`", reread[0])
        self.assertIn("my checklist item", reread[0])

    def test_gives_up_after_max_attempts(self):
        root = self.repo_with_tests()
        llm = ScriptedLLM({"worker": [call("checkpoint", summary="nothing")] * 20})
        h = harness(root, llm, patience=2, max_attempts=2)
        self.assertEqual(h.run(), "gave_up")
        self.assertEqual(len(self.kinds(h, "drop")), 2)
        self.assertIn("Attempt 2", (root / ".marathon" / "attempts.md").read_text())

    def test_finish_is_rejected_while_tests_fail(self):
        root = self.repo_with_tests()
        llm = ScriptedLLM(
            {
                "worker": [
                    call("write_file", path="slug.py", content=BAD),
                    call("finish", summary="done?"),
                    call("write_file", path="slug.py", content=GOOD),
                    call("finish", summary="done"),
                ],
                "judge": [say("PASS")],
            }
        )
        h = harness(root, llm)
        self.assertEqual(h.run(), "done")
        self.assertIn("finish rejected", self.kinds(h, "finish")[0]["msg"])
        tool_results = [
            m["content"]
            for a, ms in llm.seen
            if a == "worker"
            for m in ms
            if m["role"] == "tool"
        ]
        self.assertTrue(any(t.startswith("Not done yet") for t in tool_results))

    def test_judge_sees_only_goal_and_diff(self):
        root = self.repo_with_tests()
        llm = ScriptedLLM(
            {
                "worker": [
                    call("write_file", path="slug.py", content=GOOD),
                    call("write_file", path="PROGRESS.md", content="secret reasoning"),
                    call("finish", summary="I am very sure this is done"),
                    call("finish", summary="fixed"),
                ],
                "judge": [say("FAIL\nThe README is missing."), say("PASS")],
            }
        )
        h = harness(root, llm)
        self.assertEqual(h.run(), "done")
        judge_msgs = [ms for a, ms in llm.seen if a == "judge"][0]
        self.assertEqual(len(judge_msgs), 2)
        text = judge_msgs[1]["content"]
        self.assertIn("slugify", text)
        self.assertNotIn("secret reasoning", text)
        self.assertNotIn("very sure", text)
        tool_results = [
            m["content"]
            for a, ms in llm.seen
            if a == "worker"
            for m in ms
            if m["role"] == "tool"
        ]
        self.assertTrue(any("README is missing" in t for t in tool_results))

    def test_harness_checkpoints_when_the_agent_forgets(self):
        root = self.repo_with_tests()
        llm = ScriptedLLM({"worker": [call("list_files")] * 7})
        h = harness(root, llm, force_checkpoint_every=3, max_turns=7, patience=10)
        self.assertEqual(h.run(), "budget")
        forced = [
            e for e in self.kinds(h, "checkpoint") if "made by the harness" in e["msg"]
        ]
        self.assertEqual(len(forced), 2)

    def test_trim_never_starts_with_a_tool_result(self):
        root = self.repo_with_tests()
        h = harness(root, ScriptedLLM({}), max_history=6)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
        for i in range(10):
            msgs.append(call("list_files"))
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": msgs[-1]["tool_calls"][0]["id"],
                    "content": "x",
                }
            )
        out = h.trim(msgs)
        self.assertLessEqual(len(out), 6)
        self.assertEqual([m["role"] for m in out[:3]], ["system", "user", "user"])
        self.assertNotEqual(out[3]["role"], "tool")


if __name__ == "__main__":
    unittest.main()
