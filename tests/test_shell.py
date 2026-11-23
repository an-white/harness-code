"""Tests for :mod:`app.tools.shell`."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.agents.loader import PermissionRule
from app.tools.policy import PermissionPolicy
from app.tools.shell import run_shell, make_shell_tool


class ShellTest(unittest.TestCase):
    def setUp(self):
        self._old_omniroute_key = os.environ.get("OMNIROUTE_API_KEY")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        if "OMNIROUTE_API_KEY" in os.environ:
            del os.environ["OMNIROUTE_API_KEY"]
        if self._old_omniroute_key is not None:
            os.environ["OMNIROUTE_API_KEY"] = self._old_omniroute_key

    def test_allowed_command(self):
        policy = PermissionPolicy([PermissionRule("shell", "*", "allow")])
        r = run_shell("echo hi", cwd=str(self.root), policy=policy, timeout=5)
        self.assertEqual(r["exit_code"], 0)
        self.assertFalse(r["blocked"])
        self.assertIn("hi", r["stdout"])

    def test_denied_command(self):
        policy = PermissionPolicy([PermissionRule("shell", "*", "deny")])
        sentinel = self.root / "sentinel.txt"
        r = run_shell(f"touch {sentinel}", cwd=str(self.root), policy=policy, timeout=5)
        self.assertTrue(r["blocked"])
        self.assertFalse(sentinel.exists())

    def test_timeout(self):
        policy = PermissionPolicy([PermissionRule("shell", "*", "allow")])
        r = run_shell("sleep 5", cwd=str(self.root), policy=policy, timeout=1)
        self.assertTrue(r["timed_out"])

    def test_env_no_secret(self):
        policy = PermissionPolicy([PermissionRule("shell", "*", "allow")])
        os.environ["OMNIROUTE_API_KEY"] = "secret123"
        r = run_shell("env", cwd=str(self.root), policy=policy, timeout=5, env={"OMNIROUTE_API_KEY": "secret123"})
        self.assertNotIn("secret123", r["stdout"])

    def test_truncation(self):
        policy = PermissionPolicy([PermissionRule("shell", "*", "allow")])
        r = run_shell('python3 -c "print(\'a\'*10000)"', cwd=str(self.root), policy=policy, timeout=5)
        self.assertLessEqual(len(r["stdout"]), 8192)

    def test_run_git_output_truncation(self):
        import subprocess as sp
        from unittest.mock import patch

        from app.tools.shell import _MAX_OUTPUT, _run_git

        big = "x" * (_MAX_OUTPUT + 500)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            fake = sp.CompletedProcess(args=["git", "status"], returncode=0, stdout=big, stderr="")
            with patch("app.tools.shell.subprocess.run", return_value=fake):
                out = _run_git(["status"], repo)
        self.assertLessEqual(len(out), _MAX_OUTPUT)

    def test_make_shell_tool(self):
        policy = PermissionPolicy([PermissionRule("shell", "*", "allow")])
        tool = make_shell_tool(self.root, policy, 5)
        self.assertEqual(tool.name, "run_shell")


if __name__ == "__main__":
    unittest.main()
