# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

"""Where shell commands run (Fix 8: each agent gets its own dev environment).

local      Commands run on this machine, in the attempt's worktree. This is the default.
container  Each attempt gets its own long-lived container for the agent's commands, and every
           test run gets a fresh, throwaway container. Nothing the agent does inside its own
           container can change how the tests run. There is no network unless you allow it.

Any Docker-compatible CLI works: docker (with Docker Desktop, Colima, OrbStack or plain Docker
Engine), podman, or nerdctl (Colima's containerd runtime). API keys never reach the commands:
the local shell drops them from the environment, and containers get only HOME.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import uuid
from pathlib import Path

DEFAULT_IMAGE = "python:3.12-slim"
LOCAL = ("", "local", "none")
SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|^MARATHON_", re.I)


class SandboxError(RuntimeError):
    pass


def safe_env() -> dict[str, str]:
    """This process's environment without secrets, for commands the agent can influence."""
    return {k: v for k, v in os.environ.items() if not SECRET.search(k)}


def execute(args, cwd: Path | None, timeout: int, shell: bool = False,
            env: dict | None = None) -> tuple[int, str, bool]:
    """Run a command, stdout and stderr merged. Returns (exit code, output, timed out).
    On a timeout the whole process group is killed, so child processes don't linger."""
    p = subprocess.Popen(args, shell=shell, cwd=cwd, env=env, text=True, errors="replace",
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            p.kill()
        out, _ = p.communicate()
        return 124, f"timed out after {timeout}s\n{out or ''}", True


class LocalShell:
    """Runs commands on this machine, in one folder, without secrets in the environment."""

    kind = "local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def run(self, command: str, timeout: int) -> tuple[int, str]:
        code, out, _ = execute(command, self.root, timeout, shell=True, env=safe_env())
        return code, out

    def close(self) -> None:
        pass


class ContainerShell:
    """One container for one attempt. It starts on the first command and keeps its state
    (installed packages, files outside the worktree) until the attempt ends."""

    kind = "container"

    def __init__(self, sandbox: "Sandbox", root: Path, name: str):
        self.sb, self.root, self.name, self.started = sandbox, Path(root), name, False

    def start(self) -> None:
        argv = [self.sb.cli, "run", "-d", "--name", self.name, *self.sb.run_args(self.root),
                self.sb.image, "sleep", "infinity"]
        code, out, _ = execute(argv, None, 180)
        if code != 0:
            raise SandboxError(f"could not start container {self.name}: {out.strip()[-500:]}")
        self.started = True

    def run(self, command: str, timeout: int) -> tuple[int, str]:
        if not self.started:
            try:
                self.start()
            except SandboxError as e:
                return 125, f"error: {e}"
        argv = [self.sb.cli, "exec", "-w", "/work", self.name, "sh", "-c", command]
        code, out, timed_out = execute(argv, None, timeout)
        if timed_out:  # the command may still be running inside: throw the container away
            self.close()
            out += "\n(The container was restarted. Anything installed in it is gone.)"
        return code, out

    def close(self) -> None:
        if self.started:
            execute([self.sb.cli, "rm", "-f", self.name], None, 60)
            self.started = False


class Sandbox:
    """Makes shells for attempts and fresh environments for test runs."""

    def __init__(self, kind: str = "local", image: str = DEFAULT_IMAGE, network: bool = False,
                 run_id: str = ""):
        self.cli = None if (kind or "").lower() in LOCAL else kind
        self.image, self.network = image or DEFAULT_IMAGE, network
        self.prefix = f"marathon-{run_id or uuid.uuid4().hex[:8]}"
        self.shells: list = []
        self._checks = 0

    @property
    def kind(self) -> str:
        return "container" if self.cli else "local"

    def describe(self) -> str:
        if not self.cli:
            return "on this machine"
        net = "with network" if self.network else "no network"
        return f"{Path(self.cli).name} containers from {self.image}, {net}"

    def run_args(self, root: Path) -> list[str]:
        args = ["-v", f"{Path(root).resolve()}:/work", "-w", "/work", "-e", "HOME=/tmp",
                "--cap-drop", "ALL"]
        if not self.network:
            args += ["--network", "none"]
        if Path(self.cli or "").name == "podman":
            args += ["--userns=keep-id"]  # files in the worktree stay yours
        elif hasattr(os, "getuid"):
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        return args

    def check(self, log=print) -> None:
        """Fail early, with a clear message, if containers can't run."""
        if not self.cli:
            return
        name = Path(self.cli).name
        if not shutil.which(self.cli):
            raise SandboxError(
                f"{name} not found. Install Docker Desktop, or Colima with: brew install colima docker "
                f"&& colima start. Podman works too: --sandbox podman.")
        code, out, _ = execute([self.cli, "info"], None, 60)
        if code != 0:
            raise SandboxError(f"`{name} info` failed, so no container engine is running. "
                               f"With Colima, run: colima start\n{out.strip()[-400:]}")
        code, _, _ = execute([self.cli, "image", "inspect", self.image], None, 60)
        if code != 0:
            log(f"Pulling {self.image} (once) ...")
            code, out, _ = execute([self.cli, "pull", self.image], None, 900)
            if code != 0:
                raise SandboxError(f"could not pull {self.image}: {out.strip()[-400:]}")

    def shell(self, root: Path, label: str):
        sh = ContainerShell(self, root, f"{self.prefix}-{label}") if self.cli else LocalShell(root)
        self.shells.append(sh)
        return sh

    def run_once(self, root: Path, command: str, timeout: int) -> tuple[int, str]:
        """Run one command in a fresh environment. Every test run goes through here (Fix 9)."""
        if not self.cli:
            return LocalShell(root).run(command, timeout)
        self._checks += 1
        name = f"{self.prefix}-check-{self._checks}"
        argv = [self.cli, "run", "--rm", "--name", name, *self.run_args(root), self.image,
                "sh", "-c", command]
        code, out, timed_out = execute(argv, None, timeout)
        if timed_out:
            execute([self.cli, "rm", "-f", name], None, 60)
        return code, out

    def close_all(self) -> None:
        for sh in self.shells:
            sh.close()
        self.shells = []
