# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""The questions before a run: platforms with keys, the model, the goal, and the final yes."""

import contextlib
import io
import unittest
from unittest import mock

from marathon import llm, start
from marathon.__main__ import main
from marathon.config import Config
from tests.helpers import TempRepoCase


class Answers:
    """A terminal that answers from a list and records everything shown."""

    def __init__(self, *answers):
        self.answers, self.shown = list(answers), []

    def ask(self, prompt):
        self.shown.append(prompt)
        if not self.answers:
            raise AssertionError(f"asked more than expected: {prompt!r}")
        return self.answers.pop(0)

    def say(self, text=""):
        self.shown.append(text)

    @property
    def text(self):
        return "\n".join(self.shown)


def models(found):
    return lambda provider, key: found.get(provider, [])


class Platform(TempRepoCase):
    def cfg(self, **kw):
        return Config(repo=self.root / "proj", **kw)

    def test_only_platforms_with_a_key_are_offered(self):
        env = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "OPENROUTER_API_KEY": ""}
        t = Answers("2", "")
        spec = start.pick_model(t, env, self.cfg(), models({"openai": ["gpt-new", "gpt-old"]}))
        self.assertEqual(spec, "openai:gpt-new")
        self.assertIn("Anthropic  (ANTHROPIC_API_KEY found)", t.text)
        self.assertIn("OpenAI  (OPENAI_API_KEY found)", t.text)
        self.assertNotIn("OpenRouter", t.text)
        self.assertIn("1. gpt-new", t.text)

    def test_one_key_needs_no_platform_question(self):
        t = Answers("my-model")
        spec = start.pick_model(t, {"OPENAI_API_KEY": "o"}, self.cfg(), models({}))
        self.assertEqual(spec, "openai:my-model")
        self.assertIn("Platform: OpenAI", t.text)
        self.assertIn("Couldn't list the models", t.text)

    def test_no_key_is_a_clear_error(self):
        with self.assertRaises(start.StartError) as e:
            start.pick_model(Answers(), {}, self.cfg(), models({}))
        self.assertIn("ANTHROPIC_API_KEY", str(e.exception))

    def test_suggestions_come_from_env(self):
        env = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "MARATHON_OPENAI_MODEL": "gpt-mine"}
        # MARATHON_MODEL picks the first choice for the platform; Enter accepts both suggestions
        t = Answers("", "")
        spec = start.pick_model(t, env, self.cfg(model="openai:gpt-x"),
                                models({"openai": ["gpt-new"]}))
        self.assertEqual(spec, "openai:gpt-x")
        t = Answers("2", "")
        spec = start.pick_model(t, env, self.cfg(), models({"openai": ["gpt-new"]}))
        self.assertEqual(spec, "openai:gpt-mine")  # MARATHON_OPENAI_MODEL
        self.assertIn("gpt-mine  (suggested)", t.text)

    def test_a_local_model_needs_no_key(self):
        t = Answers()
        spec = start.pick_model(t, {}, self.cfg(model="ollama:qwen3:8b"), models({}))
        self.assertEqual(spec, "ollama:qwen3:8b")

    def test_bad_answers_are_asked_again(self):
        t = Answers("9", "x", "1", "5", "2")
        env = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}
        spec = start.pick_model(t, env, self.cfg(), models({"anthropic": ["claude-b"]}))
        # "9" is out of range, "x" too, then Anthropic; "5" isn't on the list, then 2 = claude-b
        self.assertEqual(spec, "anthropic:claude-b")


class Goal(TempRepoCase):
    def test_goal_is_written_only_after_yes(self):
        cfg = Config(repo=self.root / "proj")
        env = {"ANTHROPIC_API_KEY": "a"}
        t = Answers("", "Make a thing", "that does stuff", "", "n")
        self.assertFalse(start.setup(cfg, env, io=t, models_for=models({})))
        self.assertFalse((self.root / "proj" / "goal.md").exists())
        t = Answers("", "Make a thing", "that does stuff", "", "")
        self.assertTrue(start.setup(cfg, env, io=t, models_for=models({})))
        self.assertEqual((self.root / "proj" / "goal.md").read_text(),
                         "# Goal\n\nMake a thing\nthat does stuff\n")
        self.assertEqual(cfg.model, "anthropic:claude-sonnet-5-5")
        self.assertIn("goal      Make a thing", t.text)

    def test_existing_goal_is_kept_on_enter(self):
        (self.root / "goal.md").write_text("# Goal\n\nKeep me\n")
        cfg = Config(repo=self.root, model="openai:m")
        t = Answers("", "y")
        self.assertTrue(start.setup(cfg, {}, model_given=True, io=t))
        self.assertEqual((self.root / "goal.md").read_text(), "# Goal\n\nKeep me\n")
        self.assertIn("| Keep me", t.text)

    def test_empty_goal_is_asked_again(self):
        t = Answers("", "  ", "Real goal", "")
        self.assertEqual(start.read_goal(t, self.root / "goal.md"), "Real goal")

    def test_flags_skip_the_questions(self):
        cfg = Config(repo=self.root, model="openai:m")
        t = Answers("y")  # only "Start?" is asked
        self.assertTrue(start.setup(cfg, {}, prompt="From the flag", model_given=True, io=t))
        self.assertIn("From the flag", (self.root / "goal.md").read_text())


class ModelLists(unittest.TestCase):
    def test_openai_newest_chat_models_first(self):
        data = {"data": [{"id": "gpt-a", "created": 1}, {"id": "text-embedding-3", "created": 9},
                         {"id": "gpt-b-realtime", "created": 8}, {"id": "gpt-c", "created": 5},
                         {"id": "o9-mini", "created": 3}, {"id": "whisper-1", "created": 7}]}
        with mock.patch.object(llm, "get_json", return_value=data):
            self.assertEqual(llm.list_models("openai", "k"), ["gpt-c", "o9-mini", "gpt-a"])

    def test_openrouter_only_models_with_tools(self):
        data = {"data": [{"id": "x/a", "created": 2, "supported_parameters": ["tools"]},
                         {"id": "x/b", "created": 3, "supported_parameters": ["temperature"]}]}
        with mock.patch.object(llm, "get_json", return_value=data):
            self.assertEqual(llm.list_models("openrouter", "k"), ["x/a"])

    def test_unreachable_platform_gives_an_empty_list(self):
        with mock.patch.object(llm, "get_json", side_effect=OSError("blocked")):
            self.assertEqual(llm.list_models("anthropic", "k"), [])


class CommandLine(TempRepoCase):
    def test_no_input_with_prompt_writes_the_goal(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                mock.patch.dict("os.environ", {"MARATHON_MODEL": ""}):
            code = main(["run", "--repo", str(self.root / "p"), "--no-input",
                         "--env-file", str(self.write_env()), "--prompt", "Do the thing"])
        self.assertEqual(code, 2)  # no model set, so it stops before running
        self.assertIn("No model set", err.getvalue())
        self.assertIn("Do the thing", (self.root / "p" / "goal.md").read_text())

    def write_env(self):
        p = self.root / "test.env"
        p.write_text("MARATHON_PATIENCE=5\n")
        return p


if __name__ == "__main__":
    unittest.main()
