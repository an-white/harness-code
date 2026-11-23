"""Integration-ish tests for research agent/tooling/config (no network)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agents.loader import load_agents
from app.config import get_settings
from app.tools import make_toolset


REPO_ROOT = Path(__file__).resolve().parents[1]


def _tool_names(spec_name: str, web_consent=None):
    agents = load_agents(REPO_ROOT)
    settings = get_settings()
    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        pass
    with tempfile.TemporaryDirectory() as tmp:
        tools = make_toolset(agents[spec_name], tmp, settings, web_consent=web_consent)
    return {t.name for t in tools}


class ResearchToolsetTest(unittest.TestCase):
    def setUp(self):
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()

    def test_research_toolset_readonly_fs_and_denied_by_default_web(self):
        names = _tool_names("research", web_consent={"decision": "deny"})
        for expected in ("read_file", "list_dir", "glob_files", "grep_files", "webfetch", "websearch"):
            self.assertIn(expected, names)
        for forbidden in ("write_file", "edit_file", "run_shell", "git_commit", "git_diff", "git_status", "git_log", "git_show"):
            self.assertNotIn(forbidden, names)

    def test_research_read_denies_secrets_noise(self):
        agents = load_agents(REPO_ROOT)
        settings = get_settings()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("SECRET=1")
            (root / "id.pem").write_text("pem")
            (root / "id.key").write_text("key")
            (root / ".venv").mkdir()
            (root / ".venv" / "x.txt").write_text("venv")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "x.txt").write_text("nm")
            (root / "ok.txt").write_text("hello")
            tools = {t.name: t for t in make_toolset(agents["research"], root, settings, web_consent={"decision": "deny"})}
            for path in (".env", "id.pem", "id.key", ".venv/x.txt", "node_modules/x.txt"):
                out = tools["read_file"].invoke({"path": path})
                self.assertIn("permission denied", out, path)
            self.assertEqual(tools["read_file"].invoke({"path": "ok.txt"}), "hello")

    def test_research_web_tools_respect_consent_at_runtime(self):
        agents = load_agents(REPO_ROOT)
        settings = get_settings()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = {t.name: t for t in make_toolset(agents["research"], root, settings, web_consent={"decision": "deny"})}
            self.assertIn("denied", tools["webfetch"].invoke({"url": "https://example.com"}).lower())
            self.assertIn("ERROR", tools["websearch"].invoke({"query": "python http"}))


if __name__ == "__main__":
    unittest.main()
