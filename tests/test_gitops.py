# Copyright (c) 2026 Ravi Vats
# SPDX-License-Identifier: CC-BY-NC-SA-4.0

import unittest

from marathon import gitops
from tests.helpers import TempRepoCase


class StepNumbers(unittest.TestCase):
    def test_message_ends_with_step(self):
        self.assertEqual(
            gitops.step_message("fix parser\nmore detail", 7), "fix parser [step 7]"
        )

    def test_reread_when_step_mod_n_is_zero(self):
        self.assertEqual(
            [s for s in range(1, 13) if gitops.is_reread_step(s, 5)], [5, 10]
        )
        self.assertFalse(gitops.is_reread_step(0, 5))
        self.assertFalse(gitops.is_reread_step(3, 0))


class GitHelpers(TempRepoCase):
    def test_commit_step_and_last_step(self):
        repo = self.make_repo({"a.txt": "a"})
        self.assertEqual(gitops.last_step(repo), 0)
        gitops.commit_step(repo, 1, "one")
        (repo / "a.txt").write_text("b")
        gitops.commit_step(repo, 2, "two")
        gitops.git(
            repo, "commit", "-q", "--allow-empty", "-m", "a commit without a step"
        )
        self.assertEqual(gitops.last_step(repo), 2)

    def test_restore_undoes_edits_adds_and_deletes(self):
        repo = self.make_repo(
            {
                "tests/test_a.py": "A",
                "tests/test_b.py": "B",
                "goal.md": "G",
                "code.py": "x",
            }
        )
        base = gitops.head(repo)
        (repo / "tests/test_a.py").write_text("gutted")
        (repo / "tests/test_b.py").unlink()
        (repo / "tests/test_new.py").write_text("sneaky")
        (repo / "goal.md").write_text("easier goal")
        (repo / "code.py").write_text("y")
        changed = gitops.restore(repo, base, ["tests", "goal.md"])
        self.assertEqual(
            set(changed),
            {"tests/test_a.py", "tests/test_b.py", "tests/test_new.py", "goal.md"},
        )
        self.assertEqual((repo / "tests/test_a.py").read_text(), "A")
        self.assertEqual((repo / "tests/test_b.py").read_text(), "B")
        self.assertFalse((repo / "tests/test_new.py").exists())
        self.assertEqual((repo / "goal.md").read_text(), "G")
        self.assertEqual(
            (repo / "code.py").read_text(), "y"
        )  # normal work is left alone

    def test_worktree_removed_but_branch_kept(self):
        repo = self.make_repo({"a.txt": "a"})
        wt = repo / ".marathon" / "worktrees" / "attempt-1"
        gitops.add_worktree(repo, wt, "marathon/x/attempt-1", gitops.head(repo))
        (wt / "b.txt").write_text("b")
        gitops.commit_step(wt, 1, "work")
        gitops.remove_worktree(repo, wt)
        self.assertFalse(wt.exists())
        self.assertIn(
            "work [step 1]",
            gitops.git(repo, "log", "--format=%s", "marathon/x/attempt-1"),
        )


if __name__ == "__main__":
    unittest.main()
