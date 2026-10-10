# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""The sandbox (Fix 8), with a fake Docker-compatible CLI so no container engine is needed.

The fake records every call and runs the command on this machine, in the folder that would be
mounted at /work. That checks the plumbing: which containers start, which commands go where,
and that every container is removed at the end."""

import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

from marathon import demo
from marathon.config import Config
from marathon.loop import Harness
from marathon.sandbox import LocalShell, Sandbox, SandboxError
from tests.helpers import TempRepoCase

FAKE_CLI = r'''#!PYTHON
import json, os, subprocess, sys
from pathlib import Path
state = Path(os.environ["FAKE_CLI_STATE"])
args = sys.argv[1:]
with open(state / "calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\n")
need = os.environ.get("FAKE_REQUIRE_HOST")  # act like an engine that only answers on one socket
if need and os.environ.get("DOCKER_HOST") != need:
    print("failed to connect to the docker API at unix:///var/run/docker.sock", file=sys.stderr)
    sys.exit(1)
def mount(a):
    return a[a.index("-v") + 1].rsplit(":/work", 1)[0]
def command(a):
    return a[a.index("sh") + 2]
if args[0] == "run" and "-d" in args:
    (state / args[args.index("--name") + 1]).write_text(mount(args))
    print("container-id")
elif args[0] == "run":
    sys.exit(subprocess.run(command(args), shell=True, cwd=mount(args)).returncode)
elif args[0] == "exec":
    name = args[args.index("/work") + 1]
    sys.exit(subprocess.run(command(args), shell=True, cwd=(state / name).read_text()).returncode)
'''


class Fake(TempRepoCase):
    def setUp(self):
        super().setUp()
        self.state = self.root / "cli-state"
        self.state.mkdir()
        self.cli = self.root / "fakedocker"
        self.cli.write_text(FAKE_CLI.replace("PYTHON", sys.executable))
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IEXEC)
        patcher = mock.patch.dict(os.environ, {"FAKE_CLI_STATE": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def calls(self):
        f = self.state / "calls.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


class RunArgs(unittest.TestCase):
    def test_no_network_and_your_own_user_by_default(self):
        args = Sandbox("docker", image="img").run_args(Path("/tmp/wt"))
        self.assertIn("--network", args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("-v") + 1], f"{Path('/tmp/wt').resolve()}:/work")
        self.assertIn("--cap-drop", args)
        if hasattr(os, "getuid"):
            self.assertEqual(args[args.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertNotIn("KEY", " ".join(args))

    def test_network_on_request_and_podman_user_mapping(self):
        self.assertNotIn("--network", Sandbox("docker", network=True).run_args(Path(".")))
        args = Sandbox("podman").run_args(Path("."))
        self.assertIn("--userns=keep-id", args)
        self.assertNotIn("--user", args)

    def test_local_is_the_default(self):
        for kind in ("", "local", "none", "LOCAL"):
            self.assertEqual(Sandbox(kind).kind, "local")


class Local(TempRepoCase):
    def test_secrets_are_not_passed_to_commands(self):
        env = {"ANTHROPIC_API_KEY": "sk-a", "MARATHON_MODEL": "m", "GITHUB_TOKEN": "t",
               "PLAIN_SETTING": "visible"}
        with mock.patch.dict(os.environ, env):
            code, out = LocalShell(self.root).run("env", 10)
        self.assertEqual(code, 0)
        self.assertIn("PLAIN_SETTING=visible", out)
        for name in ("ANTHROPIC_API_KEY", "MARATHON_MODEL", "GITHUB_TOKEN"):
            self.assertNotIn(name, out)

    def test_timeout_kills_the_command(self):
        code, out = LocalShell(self.root).run("echo started; sleep 20", 1)
        self.assertEqual(code, 124)
        self.assertIn("timed out after 1s", out)
        self.assertIn("started", out)


class Containers(Fake):
    def test_missing_cli_says_how_to_get_one(self):
        with self.assertRaises(SandboxError) as e:
            Sandbox("no-such-container-cli").check(log=lambda *_: None)
        self.assertIn("colima", str(e.exception).lower())

    def test_one_container_per_attempt_and_a_fresh_one_per_check(self):
        sb = Sandbox(str(self.cli), image="img", run_id="r1")
        sb.check(log=lambda *_: None)
        shell = sb.shell(self.root, "attempt-1")
        self.assertEqual(shell.run("echo hi > made.txt", 10)[0], 0)
        self.assertEqual(shell.run("cat made.txt", 10), (0, "hi\n"))
        self.assertEqual(sb.run_once(self.root, "exit 3", 10)[0], 3)
        sb.close_all()
        calls = self.calls()
        starts = [c for c in calls if c[0] == "run" and "-d" in c]
        self.assertEqual(len(starts), 1)  # the attempt's container started once
        self.assertEqual(starts[0][starts[0].index("--name") + 1], "marathon-r1-attempt-1")
        self.assertEqual(len([c for c in calls if c[0] == "exec"]), 2)
        checks = [c for c in calls if c[0] == "run" and "--rm" in c]
        self.assertEqual(len(checks), 1)
        self.assertIn("none", checks[0])
        self.assertIn(["rm", "-f", "marathon-r1-attempt-1"], calls)

    def colima_case(self, with_socket: bool):
        """A docker CLI whose context points at a missing socket, and Colima running elsewhere."""
        home = self.root / "home"
        sock = home / ".colima" / "default" / "docker.sock"
        if with_socket:
            sock.parent.mkdir(parents=True)
            sock.write_text("")
        bindir = self.root / "bin"
        bindir.mkdir()
        cli = bindir / "docker"  # the Colima fallback is for the docker CLI
        cli.write_text(self.cli.read_text())
        cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
        env = {"HOME": str(home), "FAKE_REQUIRE_HOST": f"unix://{sock}", "DOCKER_HOST": "",
               "COLIMA_HOME": "", "XDG_CONFIG_HOME": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        return str(cli), f"unix://{sock}"

    def test_finds_colima_when_the_docker_context_points_elsewhere(self):
        cli, host = self.colima_case(with_socket=True)
        logged = []
        sb = Sandbox(cli, image="img", run_id="c1")
        sb.check(log=logged.append)
        self.assertEqual(sb.env["DOCKER_HOST"], host)
        self.assertIn("Using Colima's Docker engine", logged[0])
        # every later command reaches the engine too (the fake refuses any other socket)
        shell = sb.shell(self.root, "attempt-1")
        self.assertEqual(shell.run("echo hi", 10), (0, "hi\n"))
        self.assertEqual(sb.run_once(self.root, "exit 3", 10)[0], 3)
        sb.close_all()
        self.assertIn(["rm", "-f", "marathon-c1-attempt-1"], self.calls())

    def test_no_engine_says_how_to_point_docker_at_colima(self):
        cli, _ = self.colima_case(with_socket=False)
        with self.assertRaises(SandboxError) as e:
            Sandbox(cli).check(log=lambda *_: None)
        self.assertIn("docker context use colima", str(e.exception))

    def test_full_run_in_containers(self):
        (self.root / "repo").mkdir()
        repo = self.root / "repo"
        (repo / "goal.md").write_text(demo.GOAL)
        cfg = Config(repo=repo, model="replay", sandbox=str(self.cli), image="img",
                     patience=3, reread_every_steps=2, max_attempts=3)
        h = Harness(cfg, demo.replay(), echo=lambda *_: None)
        self.assertEqual(h.run(), "done")
        calls = self.calls()
        started = [c[c.index("--name") + 1] for c in calls if c[0] == "run" and "-d" in c]
        removed = [c[2] for c in calls if c[:2] == ["rm", "-f"]]
        self.assertEqual(started, [f"marathon-{h.run_id}-attempt-1", f"marathon-{h.run_id}-attempt-2"])
        self.assertEqual(sorted(set(removed)), sorted(started))  # nothing left running
        checks = [c for c in calls if c[0] == "run" and "--rm" in c]
        self.assertGreaterEqual(len(checks), 6)  # every test run had its own container
        # the agent's commands went into its container, never straight to this machine
        execs = [c[-1] for c in calls if c[0] == "exec"]
        self.assertTrue(any("tests/test_slug.py" in e for e in execs))


if __name__ == "__main__":
    unittest.main()
