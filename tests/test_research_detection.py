import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.research.detection import detect_project_state


def _settings(**overrides):
    base = {
        "research_onboarding": "auto",
        "research_max_files": 40,
        "research_max_bytes": 200000,
        "research_context_chars": 8000,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


class ResearchDetectionTest(unittest.TestCase):
    def test_non_existing_scaffold_not_onboarded_in_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            (root / "docs").mkdir()
            state = detect_project_state(root, settings=_settings(research_onboarding="auto"))
            self.assertFalse(state["existing_project"])
            self.assertFalse(state["should_run"])
            self.assertIn("not_existing_project", state["reasons"])

    def test_existing_project_first_run_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            state = detect_project_state(root, settings=_settings(research_onboarding="auto"))
            self.assertTrue(state["existing_project"])
            self.assertTrue(state["first_run"])
            self.assertTrue(state["should_run"])
            self.assertIn("first_run", state["reasons"])

    def test_fresh_cache_skips_and_stale_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git(root, "init")
            _git(root, "config", "user.email", "t@example.com")
            _git(root, "config", "user.name", "Test")
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            _git(root, "add", "README.md")
            _git(root, "commit", "-m", "init")
            (root / "docs").mkdir()
            (root / ".agents" / "research").mkdir(parents=True)
            # first detection
            state1 = detect_project_state(root, settings=_settings())
            fp = state1["fingerprint"]
            (root / "docs" / "project_specs.md").write_text("# specs\n", encoding="utf-8")
            (root / ".agents" / "research" / "context.md").write_text("digest\n", encoding="utf-8")
            (root / ".agents" / "research" / "state.json").write_text(
                json.dumps({"fingerprint": fp, "onboarding_done": True, "fresh": True}),
                encoding="utf-8",
            )
            fresh = detect_project_state(root, settings=_settings())
            self.assertFalse(fresh["should_run"])
            self.assertTrue(fresh["fresh"])
            self.assertIn("fresh", fresh["reasons"])

            # change file -> stale fingerprint
            (root / "README.md").write_text("hello changed\n", encoding="utf-8")
            stale = detect_project_state(root, settings=_settings())
            self.assertTrue(stale["should_run"])
            self.assertIn("stale_fingerprint", stale["reasons"])

    def test_modes_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"name":"x"}', encoding="utf-8")
            # never
            never = detect_project_state(root, settings=_settings(research_onboarding="never"))
            self.assertFalse(never["should_run"])
            self.assertIn("mode_never", never["reasons"])
            # always
            always = detect_project_state(root, settings=_settings(research_onboarding="always"))
            self.assertTrue(always["should_run"])
            self.assertIn("mode_always", always["reasons"])
            # refresh overrides freshness
            refresh = detect_project_state(root, settings=_settings(research_onboarding="auto"), refresh=True)
            self.assertTrue(refresh["should_run"])
            self.assertIn("refresh_requested", refresh["reasons"])
            # skip overrides
            skip = detect_project_state(root, settings=_settings(research_onboarding="always"), skip=True)
            self.assertFalse(skip["should_run"])
            self.assertIn("skip_flag", skip["reasons"])


if __name__ == "__main__":
    unittest.main()
