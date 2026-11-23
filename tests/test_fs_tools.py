"""Tests for :mod:`app.tools.fs`."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agents.loader import PermissionRule
from app.tools.fs import make_fs_tools
from app.tools.policy import PermissionPolicy


def _tools(root, policy):
    return {t.name: t for t in make_fs_tools(root, policy)}


class FsToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.policy = PermissionPolicy(
            [
                PermissionRule("read", "*", "allow"),
                PermissionRule("edit", "*", "allow"),
                PermissionRule("glob", "*", "allow"),
                PermissionRule("grep", "*", "allow"),
            ]
        )
        self.tools = _tools(self.root, self.policy)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        tools = self.tools
        self.assertEqual(tools["write_file"].invoke({"path": "a.txt", "content": "hello world"}), "OK: wrote a.txt")
        self.assertEqual(tools["read_file"].invoke({"path": "a.txt"}), "hello world")
        self.assertEqual(tools["edit_file"].invoke({"path": "a.txt", "old": "world", "new": "there"}), "OK: edited")
        self.assertEqual(tools["read_file"].invoke({"path": "a.txt"}), "hello there")

    def test_list_dir_sorted(self):
        (self.root / "b.txt").write_text("x")
        (self.root / "a.txt").write_text("x")
        out = self.tools["list_dir"].invoke({"path": "."})
        self.assertTrue(out.startswith("a.txt"))

    def test_glob(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "c.py").write_text("print(1)")
        out = self.tools["glob_files"].invoke({"pattern": "**/*.py", "root": "."})
        self.assertIn("sub/c.py", out)

    def test_grep(self):
        (self.root / "a.txt").write_text("hello\nworld")
        out = self.tools["grep_files"].invoke({"pattern": "hello", "root": ".", "include": "*.txt"})
        self.assertIn("a.txt:1: hello", out)

    def test_edit_ambiguity(self):
        (self.root / "b.txt").write_text("x\nx")
        out = self.tools["edit_file"].invoke({"path": "b.txt", "old": "x", "new": "y"})
        self.assertIn("ambiguous", out)

    def test_edit_not_found(self):
        (self.root / "b.txt").write_text("hello")
        out = self.tools["edit_file"].invoke({"path": "b.txt", "old": "nope", "new": "y"})
        self.assertIn("not found", out)

    def test_path_traversal_rejected(self):
        out = self.tools["read_file"].invoke({"path": "../etc/passwd"})
        self.assertIn("ERROR", out)

    def test_symlink_escape_rejected(self):
        import tempfile as t

        outside = Path(t.mkdtemp())
        (outside / "secret").write_text("secret")
        link = self.root / "link"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("no symlink")
        out = self.tools["read_file"].invoke({"path": "link/secret"})
        self.assertIn("ERROR", out)

    def test_policy_denial_returns_error(self):
        deny_policy = PermissionPolicy([PermissionRule("read", "*", "deny")])
        # no read tool exposed; but write tool? use edit-only policy
        edit_policy = PermissionPolicy([PermissionRule("edit", "*", "allow")])
        tools = _tools(self.root, edit_policy)
        (self.root / "a.txt").write_text("hi")
        # read_file not present
        self.assertNotIn("read_file", tools)
        # policy with no allow rule exposes no write tool
        deny_all = PermissionPolicy([PermissionRule("edit", "*", "deny")])
        tools3 = _tools(self.root, deny_all)
        self.assertNotIn("write_file", tools3)




    def test_edit_deny_specific_not_bypassed_by_allow_star(self):
        policy = PermissionPolicy(
            [
                PermissionRule("edit", "*", "allow"),
                PermissionRule("edit", "secret.txt", "deny"),
            ]
        )
        tools = _tools(self.root, policy)
        out = tools["write_file"].invoke({"path": "secret.txt", "content": "x"})
        self.assertIn("permission denied", out)
        (self.root / "secret.txt").write_text("original")
        out = tools["edit_file"].invoke({"path": "secret.txt", "old": "original", "new": "x"})
        self.assertIn("permission denied", out)


if __name__ == "__main__":
    unittest.main()
