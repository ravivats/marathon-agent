# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

import shutil
import tempfile
import unittest
from pathlib import Path

from marathon import gitops


class TempRepoCase(unittest.TestCase):
    """Each test gets its own throwaway folder (and a git repo when asked)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="marathon-test-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def make_repo(self, files: dict[str, str]) -> Path:
        for name, text in files.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        gitops.init_repo(self.root)
        gitops.git(self.root, "add", "-A")
        gitops.git(self.root, "commit", "-q", "-m", "start")
        return self.root
