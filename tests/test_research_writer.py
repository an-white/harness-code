import json
import tempfile
import unittest
from pathlib import Path

from app.research.writer import (
    load_research_context,
    project_specs_path,
    render_context_digest,
    render_project_specs_markdown,
    write_project_research,
    write_research_report,
)


class ResearchWriterTest(unittest.TestCase):
    def sample_content(self):
        return {
            "title": "Demo Project",
            "quick_context": "Demo quick context",
            "business": "Does demo things",
            "repositories_structure": ["app", "app/cli.py", "docs"],
            "architecture_data_flow": ["CLI -> graph -> nodes"],
            "domain_model_rules": ["Rule A"],
            "ubiquitous_language": [{"term": "Task", "meaning": "Unit of work", "where_used": "planner"}],
            "acronyms": ["CLI"],
            "stack_versions": ["Python"],
            "data_sources_integrations": ["None"],
            "entry_points_build_test_run": ["python main.py", "pytest -q"],
            "conventions_patterns": ["unittest"],
            "key_files_index": ["app/cli.py", "app/research/writer.py"],
            "risks_debt_gotchas": ["None known"],
            "faq_where_to_find": ["docs/project_specs.md"],
            "sources_research_log": ["scan"],
            "digest": "short digest",
        }

    def test_writes_canonical_specs_metadata_localized_context_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = self.sample_content()
            result = write_project_research(
                root,
                content,
                fingerprint="abc123",
                freshness="fresh",
                head="deadbeef",
                interaction_language="es",
                context_max_chars=8000,
                localize=lambda text, lang: f"[{lang}]\n" + text,
            )
            specs = Path(result["specs_path"])
            loc = Path(result["localized_specs_path"])
            ctx = Path(result["context_path"])
            state_p = Path(result["state_path"])
            self.assertTrue(specs.exists())
            self.assertTrue(loc.exists())
            self.assertTrue(ctx.exists())
            self.assertTrue(state_p.exists())
            specs_text = specs.read_text(encoding="utf-8")
            self.assertIn("## Metadata", specs_text)
            self.assertIn("abc123", specs_text)
            self.assertIn("deadbeef", specs_text)
            self.assertIn("Freshness", specs_text)
            self.assertIn("Language", specs_text)
            self.assertIn("Quick Context for Agents", specs_text)
            self.assertTrue(loc.read_text(encoding="utf-8").startswith("[es]"))
            self.assertEqual(project_specs_path(root, "es").name, "project_specs.es.md")
            state = json.loads(state_p.read_text(encoding="utf-8"))
            self.assertEqual(state["fingerprint"], "abc123")
            self.assertTrue(state["onboarding_done"])
            self.assertEqual(state["paths"]["specs_en"], "docs/project_specs.md")
            self.assertNotIn("localized_specs_pending_update", state)
            loaded = load_research_context(root)
            self.assertTrue(loaded["onboarding_done"])
            self.assertIn("demo", loaded["digest"].lower())

    def test_context_digest_respects_char_limit(self):
        content = self.sample_content()
        content["quick_context"] = "x" * 5000
        content["business"] = "y" * 5000
        digest = render_context_digest(content, {"fingerprint": "f", "freshness": "fresh", "generated_at": "now"}, 300)
        self.assertLessEqual(len(digest), 300)
        self.assertIn("truncated", digest)

    def test_write_research_report_english_and_localized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res = write_research_report(root, "jwt notes", "# Report\nBody", interaction_language="fr", localize=lambda t, l: f"FR::{t}")
            en = Path(res["specs_path"])
            loc = Path(res["localized_specs_path"])
            self.assertTrue(en.exists())
            self.assertEqual(en.name, "jwt-notes.md")
            self.assertTrue(loc.exists())
            self.assertEqual(loc.name, "jwt-notes.fr.md")
            self.assertIn("FR::", loc.read_text(encoding="utf-8"))

    def test_render_canonical_has_required_sections(self):
        md = render_project_specs_markdown(self.sample_content(), {"generated_at": "t", "fingerprint": "fp", "freshness": "fresh", "head": "h"})
        for heading in [
            "## Quick Context for Agents",
            "## Business / What the Project Does",
            "## Repositories / Structure",
            "## Architecture / Data Flow",
            "## Domain Model / Rules",
            "## Ubiquitous Language",
            "## Acronyms",
            "## Stack / Versions",
            "## Data Sources / Integrations",
            "## Entry Points / Build / Test / Run",
            "## Conventions / Patterns",
            "## Key Files Index",
            "## Risks / Debt / Gotchas",
            "## FAQ / Where to Find",
            "## Sources / Research Log",
        ]:
            self.assertIn(heading, md)
        self.assertIn("## Metadata", md)
        self.assertIn("| Key | Value |", md)
        self.assertIn("| Fingerprint | fp |", md)

    def test_render_directory_structure_and_execution_sections_use_code_blocks(self):
        content = self.sample_content()
        md = render_project_specs_markdown(
            content,
            {"generated_at": "t", "fingerprint": "fp", "freshness": "fresh", "head": "h", "workspace_root": "repo"},
        )

        def section(title: str, next_title: str) -> str:
            start = md.index(f"## {title}")
            end = md.index(f"## {next_title}", start)
            return md[start:end]

        repos = section("Repositories / Structure", "Architecture / Data Flow")
        self.assertIn("```text", repos)
        self.assertIn("├──", repos)
        self.assertIn("└──", repos)
        self.assertIn("│", repos)
        self.assertNotIn("\n- ", repos.split("```text", 1)[1])

        key_files = section("Key Files Index", "Risks / Debt / Gotchas")
        self.assertIn("```text", key_files)
        self.assertIn("├──", key_files)
        self.assertIn("└──", key_files)
        self.assertNotIn("\n- ", key_files.split("```text", 1)[1])

        entry = section("Entry Points / Build / Test / Run", "Conventions / Patterns")
        self.assertIn("```text", entry)
        self.assertIn("python main.py", entry)
        self.assertIn("pytest -q", entry)
        self.assertNotIn("\n- python main.py", entry)

    def test_harness_language_localized_updated_when_translation_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res = write_project_research(
                root,
                self.sample_content(),
                fingerprint="fp1",
                head="h1",
                interaction_language="en",
                harness_interaction_language="es",
                localize=lambda text, lang: f"[{lang} translated]\n{text}",
            )
            loc = Path(res["localized_specs_path"])
            self.assertTrue(loc.exists())
            self.assertEqual(loc.name, "project_specs.es.md")
            self.assertIn("[es translated]", loc.read_text(encoding="utf-8"))
            self.assertTrue(res["localized_updated"])
            self.assertIsNone(res["localized_pending_update"])
            state = res["state"]
            self.assertNotIn("localized_specs_pending_update", state)
            self.assertNotIn("localized_specs_pending_notice", state)
            self.assertEqual(state["localized_language"], "es")

    def test_translation_unavailable_keeps_existing_localized_and_records_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loc = project_specs_path(root, "es")
            loc.parent.mkdir(parents=True, exist_ok=True)
            loc.write_text("ES-OLD-CONTENT\n", encoding="utf-8")
            res = write_project_research(
                root,
                self.sample_content(),
                fingerprint="fp2",
                head="h2",
                interaction_language="es",
                localize=None,
            )
            self.assertEqual(loc.read_text(encoding="utf-8"), "ES-OLD-CONTENT\n")
            self.assertFalse(res["localized_updated"])
            pending = res["localized_pending_update"]
            self.assertIsInstance(pending, dict)
            self.assertEqual(pending["language"], "es")
            self.assertIn("project_specs.es.md", pending["path"])
            self.assertTrue(pending["reason"])
            self.assertIn("Update before next task execution", res["localized_notice"])
            state = res["state"]
            self.assertEqual(state["localized_specs_pending_update"]["path"], pending["path"])
            self.assertEqual(state["localized_specs_pending_notice"], res["localized_notice"])

    def test_pending_cleared_on_successful_update_and_other_languages_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            es = project_specs_path(root, "es")
            fr = project_specs_path(root, "fr")
            es.parent.mkdir(parents=True, exist_ok=True)
            es.write_text("ES-OLD\n", encoding="utf-8")
            fr.write_text("FR-OLD\n", encoding="utf-8")
            # first write: unavailable -> pending
            write_project_research(
                root,
                self.sample_content(),
                fingerprint="fp3",
                head="h3",
                interaction_language="es",
                localize=lambda text, lang: (_ for _ in ()).throw(RuntimeError("fail")),
            )
            st = json.loads((root / ".agents/research/state.json").read_text(encoding="utf-8"))
            self.assertIn("localized_specs_pending_update", st)
            self.assertEqual(fr.read_text(encoding="utf-8"), "FR-OLD\n")
            # second write: available -> updated, pending cleared, fr untouched
            res = write_project_research(
                root,
                self.sample_content(),
                fingerprint="fp4",
                head="h4",
                interaction_language="es",
                localize=lambda text, lang: f"[{lang}] {text}",
            )
            self.assertIn("[es]", es.read_text(encoding="utf-8"))
            self.assertEqual(fr.read_text(encoding="utf-8"), "FR-OLD\n")
            self.assertIsNone(res["localized_pending_update"])
            self.assertNotIn("localized_specs_pending_update", res["state"])
            self.assertNotIn("localized_specs_pending_notice", res["state"])

    def test_existing_localized_detected_when_context_language_is_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            es = project_specs_path(root, "es")
            es.parent.mkdir(parents=True, exist_ok=True)
            es.write_text("ES-OLD\n", encoding="utf-8")
            res = write_project_research(
                root,
                self.sample_content(),
                fingerprint="fp5",
                head="h5",
                interaction_language="en",
                harness_interaction_language=None,
                localize=lambda text, lang: f"[{lang}] {text}",
            )
            self.assertEqual(Path(res["localized_specs_path"]).name, "project_specs.es.md")
            self.assertIn("[es]", es.read_text(encoding="utf-8"))
            self.assertTrue(res["localized_updated"])


if __name__ == "__main__":
    unittest.main()
