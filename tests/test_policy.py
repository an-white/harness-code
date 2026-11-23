"""Tests for :mod:`app.tools.policy`."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agents.loader import PermissionRule
from app.tools.policy import (
    PermissionPolicy,
    ShellPolicy,
    build_policy,
    build_shell_policy,
    detect_owner_repo,
    is_safe_path,
)


class PermissionPolicyTest(unittest.TestCase):
    def test_default_deny(self):
        policy = PermissionPolicy([])
        self.assertFalse(policy.allows("read", "foo.txt"))

    def test_allow_when_matched(self):
        policy = PermissionPolicy([PermissionRule("read", "*", "allow")])
        self.assertTrue(policy.allows("read", "foo.txt"))

    def test_deny_wins_over_allow(self):
        policy = PermissionPolicy(
            [PermissionRule("read", "*", "allow"), PermissionRule("read", "*", "deny")]
        )
        self.assertFalse(policy.allows("read", "foo.txt"))

    def test_case_insensitive_action(self):
        policy = PermissionPolicy([PermissionRule("SHELL", "*", "allow")])
        self.assertTrue(policy.allows("shell", "ls"))
        self.assertTrue(policy.allows("SHELL", "ls"))

    def test_glob_resource_matching(self):
        policy = PermissionPolicy([PermissionRule("read", "*.py", "allow")])
        self.assertTrue(policy.allows("read", "a.py"))
        self.assertFalse(policy.allows("read", "a.txt"))

    def test_fnmatch_case_sensitive(self):
        policy = PermissionPolicy([PermissionRule("read", "*.PY", "allow")])
        self.assertFalse(policy.allows("read", "a.py"))
        self.assertTrue(policy.allows("read", "a.PY"))


class ShellPolicyTest(unittest.TestCase):
    def test_allows_command(self):
        policy = PermissionPolicy([PermissionRule("shell", "echo*", "allow")])
        sp = ShellPolicy(policy)
        self.assertTrue(sp.allows_command("echo hi"))
        self.assertFalse(sp.allows_command("rm -rf /"))

    def test_deny_precedence(self):
        policy = PermissionPolicy(
            [PermissionRule("shell", "*", "allow"), PermissionRule("shell", "rm*", "deny")]
        )
        sp = ShellPolicy(policy)
        self.assertFalse(sp.allows_command("rm foo"))

    def test_wrapper_bypass_path_qualified_denied(self):
        policy = PermissionPolicy(
            [PermissionRule("shell", "*", "allow"), PermissionRule("shell", "bad*", "deny")]
        )
        sp = ShellPolicy(policy)
        self.assertFalse(sp.allows_command("env /usr/bin/bad"))
        self.assertFalse(sp.allows_command("/usr/bin/bad"))
        self.assertFalse(sp.allows_command("env bad x"))

    def test_wrapper_bypass_allowed_command(self):
        policy = PermissionPolicy([PermissionRule("shell", "echo*", "allow")])
        sp = ShellPolicy(policy)
        self.assertTrue(sp.allows_command("env echo hi"))

    def test_wrapper_bypass_sh_c_path_qualified_denied(self):
        policy = PermissionPolicy(
            [PermissionRule("shell", "*", "allow"), PermissionRule("shell", "bad*", "deny")]
        )
        sp = ShellPolicy(policy)
        self.assertFalse(sp.allows_command("sh -c /bin/bad"))


class BuildPolicyTest(unittest.TestCase):
    def test_build_from_spec(self):
        from app.agents.loader import AgentSpec

        spec = AgentSpec(
            name="t",
            description="d",
            mode="",
            color="",
            system_prompt="p",
            permissions=[PermissionRule("read", "*", "allow")],
        )
        policy = build_policy(spec)
        self.assertTrue(policy.allows("read", "x"))
        sp = build_shell_policy(spec)
        self.assertFalse(sp.allows_command("ls"))


class IsSafePathTest(unittest.TestCase):
    def test_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(is_safe_path(root / "a.txt", root))

    def test_outside(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            root = Path(tmp)
            self.assertFalse(is_safe_path(Path(other) / "a.txt", root))

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            root = Path(tmp)
            link = root / "link"
            try:
                link.symlink_to(other)
            except OSError:
                self.skipTest("no symlink")
            self.assertFalse(is_safe_path(link / "a.txt", root))


class DetectOwnerRepoTest(unittest.TestCase):
    def test_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            (repo / "sub").mkdir(parents=True)
            (repo / ".git").mkdir()
            self.assertEqual(detect_owner_repo(repo / "sub" / "f.txt", root), repo.resolve())

    def test_root_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            self.assertEqual(detect_owner_repo(root / "a" / "f.txt", root), root.resolve())

    def test_no_escape(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            root = Path(tmp)
            self.assertEqual(detect_owner_repo(Path(other) / "f.txt", root), root.resolve())


if __name__ == "__main__":
    unittest.main()
