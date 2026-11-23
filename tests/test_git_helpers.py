"""Tests for git helpers."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from app.tools.policy import detect_owner_repo
from app.tools.shell import git_status, git_diff, git_log, git_show, git_commit


class GitHelpersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.repo, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detect_owner_repo(self):
        (self.repo / "sub").mkdir()
        (self.repo / ".git").mkdir(exist_ok=True)
        self.assertEqual(detect_owner_repo(self.repo / "sub" / "f.txt", self.root), self.repo.resolve())

    def test_status_diff_log_show(self):
        (self.repo / "f.txt").write_text("hello")
        subprocess.run(["git", "add", "f.txt"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "f.txt").write_text("hello2")
        self.assertIn("f.txt", git_status(self.repo))
        self.assertIn("hello", git_diff(self.repo))
        self.assertIn("init", git_log(self.repo, 5))
        self.assertIn("init", git_show(self.repo, "HEAD"))

    def test_commit_approved_false(self):
        (self.repo / "f.txt").write_text("hello")
        out = git_commit("msg", ["f.txt"], self.repo, approved=False)
        self.assertEqual(out, "COMMIT NOT APPROVED")

    def test_commit_approved_true_only_specified(self):
        (self.repo / "f.txt").write_text("hello")
        (self.repo / "g.txt").write_text("other")
        out = git_commit("msg", ["f.txt"], self.repo, approved=True)
        self.assertNotEqual(out, "COMMIT NOT APPROVED")
        # g.txt should remain untracked
        status = git_status(self.repo)
        self.assertIn("g.txt", status)

    def test_commit_rejects_pathspec_magic_and_unsafe_entries(self):
        (self.repo / "foo").write_text("x")
        subprocess.run(["git", "add", "foo"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        cases = [
            [":!foo"],
            [":/foo"],
            ["*"],
            [""],
            ["../x"],
        ]
        for files in cases:
            with self.subTest(files=files):
                (self.repo / "foo").write_text("changed")
                out = git_commit("msg", files, self.repo, approved=True)
                self.assertTrue(out.startswith("ERROR:"), out)
                head_after = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
                ).stdout.strip()
                self.assertEqual(head_before, head_after)

    def test_commit_message_newlines_rejected(self):
        (self.repo / "f.txt").write_text("hello")
        out = git_commit("line1\nline2", ["f.txt"], self.repo, approved=True)
        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("newline", out.lower())

    def test_git_log_n_clamp(self):
        # should not error and should clamp large n to 1000
        out = git_log(self.repo, n=999999)
        self.assertIsInstance(out, str)
        self.assertFalse(out.startswith("ERROR:"), out)


if __name__ == "__main__":
    unittest.main()
