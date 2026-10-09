# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Settings from .env, the environment and flags. Highest wins: flag > environment > .env > default."""

import argparse
import contextlib
import io
import unittest
from pathlib import Path

from marathon.__main__ import main, make_config
from marathon.config import (DEFAULT_TEST_CMD, ENV, Config, SettingError, load_env_file,
                             read_env_file)
from marathon.start import MODEL_VARS
from tests.helpers import TempRepoCase

ROOT = Path(__file__).resolve().parent.parent
KEYS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"}


class EnvFile(TempRepoCase):
    def write(self, text: str) -> Path:
        p = self.root / "settings.env"
        p.write_text(text)
        return p

    def test_parsing(self):
        p = self.write(
            "# a comment\n\n"
            "MARATHON_PATIENCE=7\n"
            "export MARATHON_MODEL=anthropic:claude-x\n"
            "MARATHON_TEST_CMD='make test  # not a comment inside quotes'\n"
            'MARATHON_IMAGE="python:3.12-slim"\n'
            "MARATHON_SWITCH_AFTER=4   # a comment after the value\n"
            "OPENAI_API_KEY=\n"
        )
        self.assertEqual(read_env_file(p), {
            "MARATHON_PATIENCE": "7",
            "MARATHON_MODEL": "anthropic:claude-x",
            "MARATHON_TEST_CMD": "make test  # not a comment inside quotes",
            "MARATHON_IMAGE": "python:3.12-slim",
            "MARATHON_SWITCH_AFTER": "4",
            "OPENAI_API_KEY": "",
        })

    def test_bad_line(self):
        with self.assertRaises(SettingError) as e:
            read_env_file(self.write("MARATHON_PATIENCE 7\n"))
        self.assertIn("line 1", str(e.exception))

    def test_real_environment_wins_and_empty_values_are_skipped(self):
        env = {"MARATHON_PATIENCE": "3"}
        loaded = load_env_file(self.write("MARATHON_PATIENCE=7\nMARATHON_HOURS=2\nOPENAI_API_KEY=\n"), env)
        self.assertEqual(loaded, ["MARATHON_HOURS"])
        self.assertEqual(env, {"MARATHON_PATIENCE": "3", "MARATHON_HOURS": "2"})


class FromEnv(unittest.TestCase):
    def test_types(self):
        cfg = Config.from_env(".", {"MARATHON_REREAD_EVERY": "25", "MARATHON_TEST_EVERY": "10",
                                    "MARATHON_HOURS": "1.5", "MARATHON_ALLOW_NETWORK": "yes",
                                    "MARATHON_PROTECT": "Makefile, conftest.py",
                                    "MARATHON_SCORE_CMD": "python3 bench.py"})
        self.assertEqual((cfg.reread_every_steps, cfg.test_every, cfg.hours), (25, 10, 1.5))
        self.assertTrue(cfg.allow_network)
        self.assertEqual(cfg.protect, ["Makefile", "conftest.py"])
        self.assertEqual(cfg.score_cmd, "python3 bench.py")

    def test_plain_config_never_reads_the_environment(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"MARATHON_PATIENCE": "2", "MARATHON_SANDBOX": "docker"}):
            cfg = Config(repo=".")
        self.assertEqual((cfg.patience, cfg.sandbox), (25, "local"))

    def test_bad_values_name_the_setting(self):
        with self.assertRaises(SettingError) as e:
            Config.from_env(".", {"MARATHON_PATIENCE": "lots"})
        self.assertIn("MARATHON_PATIENCE", str(e.exception))
        with self.assertRaises(SettingError):
            Config.from_env(".", {"MARATHON_ALLOW_NETWORK": "maybe"})
        with self.assertRaises(SettingError) as e:
            Config.from_env(".", {"MARATHON_TEST_EVERY": "0"}).validate()
        self.assertIn("--test-every", str(e.exception))


class Precedence(TempRepoCase):
    def args(self, **kw):
        base = {"repo": str(self.root / "work"), "env_file": None, "allow_network": False}
        return argparse.Namespace(**{**base, **kw})

    def test_flag_beats_environment_beats_env_file(self):
        f = self.root / ".env"
        f.write_text("MARATHON_PATIENCE=7\nMARATHON_TEST_EVERY=2\nMARATHON_REREAD_EVERY=9\n")
        env = {"MARATHON_REREAD_EVERY": "3"}  # set in the shell
        with contextlib.redirect_stdout(io.StringIO()):
            cfg = make_config(self.args(env_file=str(f), patience=11), env)
        self.assertEqual(cfg.patience, 11)  # flag
        self.assertEqual(cfg.reread_every_steps, 3)  # shell environment
        self.assertEqual(cfg.test_every, 2)  # .env
        self.assertEqual(cfg.max_attempts, 5)  # default

    def test_warns_when_env_file_is_inside_the_agents_repo(self):
        (self.root / "work").mkdir()
        f = self.root / "work" / ".env"
        f.write_text("MARATHON_PATIENCE=7\n")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            make_config(self.args(env_file=str(f)), {})
        self.assertIn("inside the repo the agent works on", err.getvalue())

    def test_missing_env_file_is_a_clear_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["run", "--repo", str(self.root), "--env-file", str(self.root / "nope.env")])
        self.assertEqual(code, 4)
        self.assertIn("no such file", err.getvalue())


class Example(unittest.TestCase):
    def test_env_example_matches_the_code(self):
        values = read_env_file(ROOT / ".env.example")
        unknown = set(values) - set(ENV.values()) - KEYS - set(MODEL_VARS)
        self.assertEqual(unknown, set(), "names in .env.example that the code doesn't read")
        missing = set(ENV.values()) - set(values)
        self.assertEqual(missing, set(), "settings missing from .env.example")
        cfg = Config.from_env(".", values)
        cfg.validate()
        plain = Config(repo=".")
        for name in ENV:  # every number in the example is the real default
            if name not in ("model", "sandbox"):
                self.assertEqual(getattr(cfg, name), getattr(plain, name), name)
        self.assertEqual(cfg.test_cmd, DEFAULT_TEST_CMD)
        self.assertFalse(any(values[k] for k in KEYS), "no real keys in the example")


if __name__ == "__main__":
    unittest.main()
