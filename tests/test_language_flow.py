"""Tests for translator entry, language resolution, and localization (R1)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app import config
from app.config import get_settings
from app.graph import build_graph
from app.i18n import load_preferences, save_preferences


class LangStubRuntime:
    """Deterministic runtime stub for language/translation behaviors."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def run(self, role, messages, context=None, skill_md=None, mode="auto"):
        prompt = messages[0].content if messages else ""
        self.calls.append((role, prompt))
        if role == "translator":
            if "Translate the following user content into clear English" in prompt:
                content = prompt.split("Content:\n", 1)[1].strip()
                # simple fake translation for tests
                translations = {
                    "Hola mundo": "Hello world",
                    "Hola": "Hello",
                    "alcance completo": "full scope",
                }
                if content in translations:
                    return {"final": translations[content], "mode": "stub"}
                # Unknown content: return English-safe fallback marker for deterministic tests.
                return {"final": f"EN:{content}", "mode": "stub"}
            if "Localize the following user-facing text" in prompt:
                text = prompt.split("Text:\n", 1)[1].strip()
                return {"final": f"[es]{text}", "mode": "stub"}
            if "Translate/localize these clarification questions" in prompt:
                # preserve structure
                if "Questions:" in prompt:
                    raw = prompt.split("Questions:", 1)[1].strip()
                    qs = json.loads(raw)
                    return {"final": json.dumps({"questions": [f"[es]{q}" for q in qs]}), "mode": "stub"}
            return {"final": '{"normalized_requirements":"Normalized requirement","open_questions":[]}', "mode": "stub"}
        if role == "project-manager":
            if "Create an ordered implementation plan" in prompt:
                return {
                    "final": '{"tasks":[{"id":"task-1","kind":"implementation","executor":"builder","objective":"Do work","acceptance_criteria":["ok"],"target_repo_files_hint":[],"risks":[],"needs_security":false}]}',
                    "mode": "stub",
                }
            # intake analysis: if already clarified in English, proceed
            if "full scope" in prompt or "Hello world" in prompt:
                return {"final": '{"needs_clarification": false, "questions": [], "summary":"Clear requirement"}', "mode": "stub"}
            return {"final": '{"needs_clarification": true, "questions": ["What is the scope?"]}', "mode": "stub"}
        if role == "builder":
            return {"final": "Implemented changes.", "mode": "stub"}
        if role == "reviewer":
            return {"final": '{"approved": true, "findings":[]}', "mode": "stub"}
        if role == "security":
            return {"final": '{"passed": true, "findings":[]}', "mode": "stub"}
        if role == "qa":
            return {"final": '{"passed": true, "results":[{"name":"smoke","status":"pass"}]}', "mode": "stub"}
        return {"final": "ok", "mode": "stub"}


class LanguagePrecedenceTest(unittest.TestCase):
    def setUp(self):
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()

    def test_cli_over_env_over_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_preferences(root, {"interaction_language": "fr"})
            with patch.dict(os.environ, {"INTERACTION_LANGUAGE": "de"}, clear=False), \
                 patch.object(config, "_FILE_ENV", {"INTERACTION_LANGUAGE": "de"}):
                get_settings.cache_clear()
                # simulate precedence as CLI would
                from app.cli import _resolve_initial_language
                settings = get_settings()
                # override workspace root behavior for this test via direct call expectations
                lang_cli, _ = _resolve_initial_language("es", settings)
                self.assertEqual(lang_cli, "es")
            with patch.dict(os.environ, {"INTERACTION_LANGUAGE": "de"}, clear=False), \
                 patch.object(config, "_FILE_ENV", {"INTERACTION_LANGUAGE": "de"}):
                get_settings.cache_clear()
                settings = get_settings()
                lang_env, _ = _resolve_initial_language(None, settings)
                # settings.interaction_language from env/file resolved in get_settings
                self.assertEqual(settings.interaction_language, "de")
                self.assertEqual(lang_env, "de")

    def test_preferences_used_when_no_cli_or_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_preferences(root, {"interaction_language": "pt"})
            with patch.dict(os.environ, {}, clear=True), patch.object(config, "_FILE_ENV", {}):
                get_settings.cache_clear()
                settings = get_settings()
                settings.workspace_root = root
                from app.cli import _resolve_initial_language
                lang, prefs = _resolve_initial_language(None, settings)
                self.assertEqual(lang, "pt")
                self.assertEqual(prefs.get("interaction_language"), "pt")


class PreferencesPersistenceTest(unittest.TestCase):
    def test_entry_persists_resolved_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"WORKSPACE_ROOT": str(root)}, clear=True), patch.object(config, "_FILE_ENV", {}):
                get_settings.cache_clear()
                settings = get_settings()
                runtime = LangStubRuntime()
                graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime, settings=settings)
                cfg = {"configurable": {"thread_id": "persist"}}
                result = graph.invoke(
                    {"requirement": "Hola mundo", "interaction_language": "es", "max_fix_cycles": 2},
                    config=cfg,
                )
                prefs = load_preferences(root)
                self.assertEqual(prefs.get("interaction_language"), "es")
                self.assertEqual(result.get("interaction_language"), "es")


class EntryFlowAndLocalizationTest(unittest.TestCase):
    def test_starts_at_translator_entry_and_translates_to_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if shutil.which("git"):
                subprocess.run(["git", "init", "-q"], cwd=root, check=False)
            with patch.dict(os.environ, {"WORKSPACE_ROOT": str(root), "WEB_ACCESS": "deny"}, clear=True), patch.object(config, "_FILE_ENV", {}):
                get_settings.cache_clear()
                settings = get_settings()
                runtime = LangStubRuntime()
                graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime, settings=settings)
                cfg = {"configurable": {"thread_id": "entry"}}
                result = graph.invoke(
                    {"requirement": "Hola", "interaction_language": "es", "max_fix_cycles": 2},
                    config=cfg,
                )
                # intake interrupt should be localized to es
                interrupts = getattr(graph.get_state(cfg), "interrupts", None) or []
                self.assertTrue(interrupts)
                val = getattr(interrupts[0], "value", interrupts[0])
                self.assertEqual(val.get("type"), "intake_questions")
                self.assertEqual(val.get("message"), "Necesito más información para continuar.")
                self.assertTrue(all(q.startswith("[es]") for q in val.get("questions", [])))
                # Downstream-facing requirement must be English-only (translated), never original.
                self.assertEqual(result.get("requirement"), "Hello")
                self.assertEqual(result.get("raw_requirement"), "Hola")
                # continue flow with answer translated to English
                result = graph.invoke(Command(resume="alcance completo"), config=cfg)
                interrupts = getattr(graph.get_state(cfg), "interrupts", None) or []
                if interrupts:
                    val = getattr(interrupts[0], "value", interrupts[0])
                    self.assertEqual(val.get("type"), "commit_approval")
                    self.assertIn("Aprobar commit", val.get("message", ""))
                    result = graph.invoke(Command(resume="aprobar"), config=cfg)
                self.assertTrue(result.get("done") or result.get("commit_requested"))
                # Downstream content remains English-only; PM summary may normalize requirement text.
                self.assertEqual(result.get("requirement"), "Clear requirement")
                self.assertIn("full scope", result.get("answers") or [])
                self.assertIn("alcance completo", result.get("raw_answers") or [])
                self.assertEqual(result.get("normalized_requirements"), "Normalized requirement")

    def test_language_selection_interrupt_when_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"WORKSPACE_ROOT": str(root)}, clear=True), patch.object(config, "_FILE_ENV", {}):
                get_settings.cache_clear()
                settings = get_settings()
                runtime = LangStubRuntime()
                graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime, settings=settings)
                cfg = {"configurable": {"thread_id": "langsel"}}
                result = graph.invoke({"requirement": "Hello", "max_fix_cycles": 2}, config=cfg)
                interrupts = getattr(graph.get_state(cfg), "interrupts", None) or []
                self.assertTrue(interrupts)
                val = getattr(interrupts[0], "value", interrupts[0])
                self.assertEqual(val.get("type"), "language_selection")


class FallbackNoticeTest(unittest.TestCase):
    def test_translation_failure_falls_back_to_english_with_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class FailRuntime(LangStubRuntime):
                def run(self, role, messages, context=None, skill_md=None, mode="auto"):
                    prompt = messages[0].content if messages else ""
                    if role == "translator" and "Translate the following user content into clear English" in prompt:
                        return {"error": "boom", "mode": "stub"}
                    return super().run(role, messages, context=context, skill_md=skill_md, mode=mode)

            with patch.dict(os.environ, {"WORKSPACE_ROOT": str(root)}, clear=True), patch.object(config, "_FILE_ENV", {}):
                get_settings.cache_clear()
                settings = get_settings()
                runtime = FailRuntime()
                graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime, settings=settings)
                cfg = {"configurable": {"thread_id": "fallback"}}
                result = graph.invoke(
                    {"requirement": "Bonjour", "interaction_language": "fr", "max_fix_cycles": 2},
                    config=cfg,
                )
                notice = result.get("language_notice") or ""
                self.assertIn("English", notice)
                req = result.get("requirement") or ""
                self.assertNotEqual(req, "Bonjour")
                self.assertNotIn("Bonjour", req)
                self.assertIn("Translation", req)
                self.assertEqual(result.get("raw_requirement"), "Bonjour")


if __name__ == "__main__":
    unittest.main()
