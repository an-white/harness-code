import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.cli import _session_startup, main
from app.state import _load_preferences, _load_workspace, _save_preferences, _save_workspace


class DummyRuntime:
    def __init__(self, *args, **kwargs):
        pass


class CLICheckTest(unittest.TestCase):
    def test_check_succeeds_offline(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--check"])
        out = buf.getvalue()
        self.assertEqual(code, 0, out)
        self.assertIn("check: OK", out)
        self.assertIn("agents_loaded:", out)


class CLIResearchFlagTest(unittest.TestCase):
    def test_research_seeds_state_and_requirement_fallback(self):
        captured = {}

        class DummyGraph:
            pass

        def fake_run_graph(app, initial, config, interactive=True):
            captured["app"] = app
            captured["initial"] = dict(initial)
            captured["config"] = dict(config)
            captured["interactive"] = interactive
            return {"phase": "done", "status": "done", "done": True, "blocked": False, "plan": []}

        with mock.patch("app.cli.AgentRuntime", DummyRuntime), mock.patch(
            "app.cli.build_graph", return_value=DummyGraph()
        ) as build_mock, mock.patch("app.cli._run_graph", side_effect=fake_run_graph):
            code = main(
                [
                    "--research",
                    "How is auth implemented?",
                    "--non-interactive",
                    "--skip-onboarding",
                    "--no-web",
                ]
            )

        self.assertEqual(code, 0)
        build_mock.assert_called_once()
        initial = captured["initial"]
        self.assertEqual(initial.get("needs_research"), True)
        self.assertEqual(initial.get("research_question"), "How is auth implemented?")
        self.assertEqual(initial.get("research_return_to"), "plan")
        self.assertEqual(initial.get("requirement"), "Research: How is auth implemented?")
        self.assertEqual(initial.get("web_access"), "deny")

    def test_research_preserves_explicit_requirement(self):
        captured = {}

        def fake_run_graph(app, initial, config, interactive=True):
            captured["initial"] = dict(initial)
            return {"phase": "done", "status": "done", "done": True, "blocked": False, "plan": []}

        with mock.patch("app.cli.AgentRuntime", DummyRuntime), mock.patch(
            "app.cli.build_graph", return_value=object()
        ), mock.patch("app.cli._run_graph", side_effect=fake_run_graph):
            code = main(
                [
                    "Refactor auth",
                    "--research",
                    "How is auth implemented?",
                    "--non-interactive",
                    "--skip-onboarding",
                ]
            )

        self.assertEqual(code, 0)
        initial = captured["initial"]
        self.assertEqual(initial.get("requirement"), "Refactor auth")
        self.assertEqual(initial.get("needs_research"), True)
        self.assertEqual(initial.get("research_question"), "How is auth implemented?")


class SessionStartupWorkspaceIsolationTest(unittest.TestCase):
    def test_state_helpers_resolve_under_given_workspace_root_not_cwd(self):
        with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as cwd_tmp:
            ws = Path(ws_tmp).resolve()
            cwd = Path(cwd_tmp).resolve()
            old_cwd = Path.cwd()
            try:
                os.chdir(cwd)
                prefs = {"interaction_language": "es", "selected_workspace": str(ws)}
                _save_preferences(prefs, ws)
                _save_workspace(str(ws), ws)

                loaded = _load_preferences(ws)
                self.assertEqual(loaded.get("interaction_language"), "es")
                self.assertEqual(_load_workspace(ws), str(ws))

                # Harness state must be scoped to workspace root, not cwd.
                self.assertTrue((ws / ".agents" / "harness" / "preferences.json").exists())
                self.assertFalse((cwd / ".agents" / "harness" / "preferences.json").exists())

                # CWD-scoped reads should not see workspace-scoped state.
                cwd_loaded = _load_preferences(cwd)
                self.assertEqual(cwd_loaded.get("interaction_language"), "en")
            finally:
                os.chdir(old_cwd)

    def test_non_interactive_session_startup_uses_workspace_root_without_prompts(self):
        with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as cwd_tmp:
            ws = Path(ws_tmp).resolve()
            cwd = Path(cwd_tmp).resolve()
            old_cwd = Path.cwd()
            try:
                os.chdir(cwd)
                settings = SimpleNamespace(
                    workspace_root=ws,
                    interaction_language="es",
                )
                with mock.patch("app.cli._read_input", side_effect=AssertionError("prompt called in non-interactive startup")) as read_mock, \
                     mock.patch("app.cli._select_directory", side_effect=AssertionError("directory prompt called")) as sel_mock, \
                     mock.patch("app.cli._initialize_documentation", return_value={"status": "documentation_already_present", "docs_dir": str(ws / ".docs")}), \
                     mock.patch("app.cli._ensure_gitignore_for_agents") as gitignore_mock, \
                     mock.patch("app.cli._manage_specs_versions") as specs_mock, \
                     mock.patch("app.cli.load_preferences", return_value={}):
                    state = _session_startup(settings, non_interactive=True)

                self.assertEqual(state.get("interaction_language"), "es")
                self.assertEqual(state.get("selected_workspace"), str(ws))
                read_mock.assert_not_called()
                sel_mock.assert_not_called()
                gitignore_mock.assert_called_once()
                specs_mock.assert_called_once_with(str(ws), "es")

                # Persisted under workspace root, not cwd.
                persisted = _load_preferences(ws)
                self.assertEqual(persisted.get("interaction_language"), "es")
                self.assertEqual(persisted.get("selected_workspace"), str(ws))
                self.assertFalse((cwd / ".agents" / "harness" / "preferences.json").exists())
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
