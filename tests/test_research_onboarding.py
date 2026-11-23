import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.research.onboarding import run_onboarding, should_run_onboarding
from app.research.writer import read_research_state


def _settings(**overrides):
    base = {
        "research_onboarding": "auto",
        "research_max_files": 40,
        "research_max_bytes": 100000,
        "research_context_chars": 1200,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeRuntime:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, role, messages, context=None, web_consent=None):
        self.calls.append({"role": role, "context": context})
        if not self.responses:
            return {"final": "{}", "mode": "json"}
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            return {"error": str(item), "mode": "json"}
        if isinstance(item, dict) and ("final" in item or "error" in item):
            return item
        return {"final": item, "mode": "json"}


class ResearchOnboardingTest(unittest.TestCase):
    def _make_project(self, root: Path):
        (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        (root / "README.md").write_text("# Demo\nDemo project for onboarding.\n", encoding="utf-8")
        (root / "app").mkdir()
        (root / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")

    def test_runs_scan_and_synthesis_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_project(root)
            synth = {
                "title": "Demo",
                "quick_context": "Demo quick context from LLM.",
                "business": "Does demo work.",
                "repositories_structure": ["root repo"],
                "architecture_data_flow": ["main flow"],
                "domain_model_rules": ["rule"],
                "ubiquitous_language": [{"term": "Demo", "meaning": "Example", "where_used": "README"}],
                "acronyms": ["CLI"],
                "stack_versions": ["Python"],
                "data_sources_integrations": [],
                "entry_points_build_test_run": ["app/main.py"],
                "conventions_patterns": [],
                "key_files_index": ["README.md"],
                "risks_debt_gotchas": [],
                "faq_where_to_find": ["docs/project_specs.md"],
                "sources_research_log": ["synth"],
                "digest": "compact digest",
            }
            runtime = FakeRuntime([json.dumps(synth)])
            result = run_onboarding(root, settings=_settings(), runtime=runtime, interaction_language="en")
            self.assertIn(result["status"], {"ok", "partial", "partial_blocked"})
            written = result["written"]
            specs = Path(written["specs_path"])
            ctx = Path(written["context_path"])
            state = Path(written["state_path"])
            self.assertTrue(specs.exists())
            self.assertTrue(ctx.exists())
            self.assertTrue(state.exists())
            self.assertIn("Demo quick context", specs.read_text(encoding="utf-8"))
            self.assertLessEqual(len(ctx.read_text(encoding="utf-8")), 1200)
            st = read_research_state(root)
            self.assertTrue(st["onboarding_done"])
            self.assertEqual(st["fingerprint"], result["fingerprint"])
            self.assertTrue(runtime.calls)
            self.assertEqual(runtime.calls[0]["role"], "research")

    def test_invalid_json_retries_then_partial_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_project(root)
            runtime = FakeRuntime(["not json at all", "{still bad"])  # two failed attempts
            result = run_onboarding(root, settings=_settings(), runtime=runtime)
            self.assertEqual(result["status"], "partial_blocked")
            self.assertTrue(Path(result["written"]["specs_path"]).exists())
            self.assertTrue(Path(result["written"]["context_path"]).exists())
            st = read_research_state(root)
            self.assertEqual(st.get("synthesis_status"), "partial_blocked")
            self.assertTrue(st.get("onboarding_done"))

    def test_skip_fresh_and_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_project(root)
            synth = {"quick_context": "q", "business": "b", "digest": "d"}
            runtime = FakeRuntime([json.dumps(synth), json.dumps({**synth, "quick_context": "q2"})])
            first = run_onboarding(root, settings=_settings(), runtime=runtime)
            self.assertNotEqual(first["status"], "skipped")
            # fresh -> skip
            second = run_onboarding(root, settings=_settings(), runtime=runtime)
            self.assertEqual(second["status"], "skipped")
            self.assertIn("fresh", second["reason"]) 
            # refresh forces regeneration
            third = run_onboarding(root, settings=_settings(), runtime=runtime, refresh=True)
            self.assertNotEqual(third["status"], "skipped")

    def test_should_run_helper_and_skip_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_project(root)
            decision = should_run_onboarding(root, settings=_settings())
            self.assertTrue(decision["should_run"])
            skipped = should_run_onboarding(root, settings=_settings(), skip=True)
            self.assertFalse(skipped["should_run"])
            # run respects skip
            res = run_onboarding(root, settings=_settings(), skip=True)
            self.assertEqual(res["status"], "skipped")

    def test_untrusted_scan_delimited_in_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_project(root)
            seen = {}
            def synth(prompt, scan):
                seen["prompt"] = prompt
                return {"quick_context": "q", "business": "b", "digest": "d"}
            run_onboarding(root, settings=_settings(), synthesizer=synth)
            self.assertIn("<untrusted_scan_data>", seen["prompt"])
            self.assertIn("UNTRUSTED DATA", seen["prompt"])
            self.assertIn("</untrusted_scan_data>", seen["prompt"])

    def test_localized_copy_pending_when_translation_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_project(root)
            synth = {"quick_context": "q", "business": "b", "digest": "d"}
            res = run_onboarding(root, settings=_settings(), synthesizer=lambda p, s: synth, interaction_language="es")
            loc = res["written"].get("localized_specs_path")
            self.assertIsNone(loc)
            state = read_research_state(root)
            pending = state.get("localized_specs_pending_update")
            self.assertIsInstance(pending, dict)
            self.assertEqual(pending.get("language"), "es")
            self.assertIn("project_specs.es.md", str(pending.get("path") or ""))
            self.assertTrue(state.get("localized_specs_pending_notice"))

    def test_harness_language_localized_updated_when_translation_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_project(root)
            synth = {"quick_context": "q", "business": "b", "digest": "d"}
            # research synthesis response, then translator response
            runtime = FakeRuntime([json.dumps(synth), "# Especificaciones localizadas\n\nContenido"])
            res = run_onboarding(
                root,
                settings=_settings(),
                runtime=runtime,
                interaction_language="en",
                harness_interaction_language="es",
            )
            written = res["written"]
            loc = written.get("localized_specs_path")
            self.assertIsNotNone(loc)
            self.assertTrue(Path(loc).exists())
            self.assertEqual(Path(loc).name, "project_specs.es.md")
            self.assertIn("Especificaciones localizadas", Path(loc).read_text(encoding="utf-8"))
            state = read_research_state(root)
            self.assertNotIn("localized_specs_pending_update", state)
            self.assertTrue(any(c.get("role") == "translator" for c in runtime.calls))


if __name__ == "__main__":
    unittest.main()
