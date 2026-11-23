"""The template must stay free of project-specific repository names."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "agents"

# Assembled at runtime from parts so this file does not trip its own scan.
FORBIDDEN = ("".join(("scr", "apper")), "".join(("dash", "board")))

REQUIRED_AGENT_FILES = (
    "project-manager.md",
    "translator.md",
    "builder.md",
    "qa.md",
    "security.md",
)


class GenericPromptsTest(unittest.TestCase):
    def test_required_agent_files_exist(self):
        for name in REQUIRED_AGENT_FILES:
            with self.subTest(file=name):
                self.assertTrue((AGENTS_DIR / name).is_file())

    def test_agent_markdown_has_no_project_specific_names(self):
        paths = sorted(AGENTS_DIR.glob("*.md"))
        self.assertTrue(paths, "no agent Markdown files found")
        for path in paths:
            text = path.read_text(encoding="utf-8").lower()
            for token in FORBIDDEN:
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, text)

    def test_python_sources_have_no_project_specific_names(self):
        paths = sorted(REPO_ROOT.glob("app/**/*.py"))
        paths += sorted(REPO_ROOT.glob("tests/**/*.py"))
        self.assertTrue(paths, "no Python sources found")
        for path in paths:
            text = path.read_text(encoding="utf-8").lower()
            for token in FORBIDDEN:
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
