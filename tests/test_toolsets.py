"""Tests for agent toolsets and permission wiring."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agents.loader import load_agents
from app.config import get_settings
from app.tools import make_toolset

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tool_names(spec_name: str) -> set[str]:
    agents = load_agents(REPO_ROOT)
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        tools = make_toolset(agents[spec_name], tmp, settings)
    return {t.name for t in tools}


class AgentToolsetTest(unittest.TestCase):
    def test_builder_toolset(self):
        names = _tool_names("builder")
        for expected in (
            "read_file",
            "list_dir",
            "write_file",
            "edit_file",
            "glob_files",
            "grep_files",
            "run_shell",
            "git_commit",
        ):
            self.assertIn(expected, names)

    def test_reviewer_toolset_readonly_fs(self):
        names = _tool_names("reviewer")
        for expected in (
            "read_file",
            "list_dir",
            "glob_files",
            "grep_files",
            "run_shell",
            "git_diff",
            "git_status",
        ):
            self.assertIn(expected, names)
        for forbidden in ("write_file", "edit_file", "git_commit", "git_log", "git_show"):
            self.assertNotIn(forbidden, names)

    def test_qa_toolset_readonly_fs(self):
        names = _tool_names("qa")
        for expected in (
            "read_file",
            "list_dir",
            "glob_files",
            "grep_files",
            "run_shell",
            "git_diff",
            "git_status",
            "git_log",
            "git_show",
        ):
            self.assertIn(expected, names)
        for forbidden in ("write_file", "edit_file", "git_commit"):
            self.assertNotIn(forbidden, names)

    def test_security_toolset_readonly_fs_no_shell(self):
        names = _tool_names("security")
        for expected in ("read_file", "list_dir", "glob_files", "grep_files"):
            self.assertIn(expected, names)
        for forbidden in (
            "write_file",
            "edit_file",
            "run_shell",
            "git_diff",
            "git_status",
            "git_log",
            "git_show",
            "git_commit",
        ):
            self.assertNotIn(forbidden, names)

    def test_no_fs_tools_for_pm_and_translator(self):
        fs = {"read_file", "list_dir", "write_file", "edit_file", "glob_files", "grep_files"}
        for name in ("project-manager", "translator"):
            names = _tool_names(name)
            self.assertEqual(names & fs, set(), name)
            self.assertNotIn("run_shell", names)


class SecurityReadDenyTest(unittest.TestCase):
    def test_security_read_denies_still_block(self):
        agents = load_agents(REPO_ROOT)
        settings = get_settings()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "secret.env").write_text("SECRET=1")
            ssh = root / ".ssh"
            ssh.mkdir()
            (ssh / "id_rsa").write_text("key")
            aws = root / ".aws"
            aws.mkdir()
            (aws / "credentials").write_text("cred")
            (root / "ok.txt").write_text("hello")
            tools = {t.name: t for t in make_toolset(agents["security"], root, settings)}
            for path in ("secret.env", ".ssh/id_rsa", ".aws/credentials"):
                out = tools["read_file"].invoke({"path": path})
                self.assertIn("permission denied", out, path)
            self.assertEqual(tools["read_file"].invoke({"path": "ok.txt"}), "hello")
            grep_env = tools["grep_files"].invoke({"pattern": "SECRET", "root": ".", "include": "*"})
            self.assertNotIn("secret.env", grep_env)
            grep_key = tools["grep_files"].invoke({"pattern": "key", "root": ".", "include": "*"})
            self.assertNotIn("id_rsa", grep_key)


if __name__ == "__main__":
    unittest.main()
