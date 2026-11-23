import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import cli
from app.i18n import tr


@dataclass
class _Settings:
    workspace_root: Path
    max_fix_cycles: int = 3
    interaction_language: str | None = None


class DummyRuntime:
    def __init__(self, *args, **kwargs):
        pass


class DeepAnalysisCLITest(unittest.TestCase):
    def test_i18n_strings_and_analysis_choice_prompt(self):
        for lang in ("en", "es"):
            self.assertTrue(tr("menu_deep_analysis", lang))
            self.assertTrue(tr("analysis_choice_message", lang))
            self.assertTrue(tr("analysis_choice_normal", lang))
            self.assertTrue(tr("analysis_choice_deep", lang))
            self.assertTrue(tr("analysis_choice_skip", lang))
            self.assertTrue(tr("deep_analysis_retry_prompt", lang))
            self.assertTrue(tr("deep_analysis_ask_after_normal", lang))
        # prompt helper prints choices and parses inputs
        with mock.patch.object(cli, "_read_input", side_effect=["n"]):
            with redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(cli._prompt_analysis_choice("en"), "normal")
            out = buf.getvalue()
        self.assertIn(tr("analysis_choice_message", "en"), out)
        self.assertIn(tr("analysis_choice_deep", "en"), out)
        with mock.patch.object(cli, "_read_input", side_effect=["d"]):
            self.assertEqual(cli._prompt_analysis_choice("en"), "deep")
        with mock.patch.object(cli, "_read_input", side_effect=["s"]):
            self.assertEqual(cli._prompt_analysis_choice("en"), "skip")

    def test_run_deep_analysis_with_loop_retry_then_decline_generates_partial(self):
        settings = SimpleNamespace(workspace_root=Path(tempfile.mkdtemp()))
        # first run insufficient, user chooses retry, second still insufficient, user declines -> partial
        insufficient = {
            "status": "failed",
            "quality_ok": False,
            "quality_errors": ["quality_insufficient"],
            "reason": "quality_insufficient",
            "retryable": True,
        }
        partial = {"status": "incomplete", "written": {"overview.md": "x"}, "paths": {}}
        answers = iter(["s", "n"])  # retry? yes, retry? no
        with mock.patch("app.research.deep_analysis.run_deep_analysis", side_effect=[insufficient, insufficient]) as run_mock, \
             mock.patch("app.research.deep_analysis.generate_partial_artifacts", return_value=partial) as partial_mock, \
             mock.patch.object(cli, "_read_input", side_effect=lambda *a, **k: next(answers)):
            with redirect_stdout(io.StringIO()):
                res = cli._run_deep_analysis_with_loop(object(), settings, {"interaction_language": "en"})
        self.assertEqual(run_mock.call_count, 2)
        partial_mock.assert_called_once()
        self.assertEqual(res.get("status"), "incomplete")

    def test_run_deep_analysis_with_loop_user_quality_rejection_then_retry_success(self):
        settings = SimpleNamespace(workspace_root=Path(tempfile.mkdtemp()))
        ok = {"status": "complete", "quality_ok": True, "quality_errors": [], "written": {"x": "y"}}
        ok2 = {"status": "complete", "quality_ok": True, "quality_errors": [], "written": {"x": "z"}}
        # quality sufficient? n (user rejects) -> retry prompt -> s -> success -> quality sufficient? s
        answers = iter(["n", "s", "s"])
        with mock.patch("app.research.deep_analysis.run_deep_analysis", side_effect=[ok, ok2]) as run_mock, \
             mock.patch.object(cli, "_read_input", side_effect=lambda *a, **k: next(answers)):
            with redirect_stdout(io.StringIO()):
                res = cli._run_deep_analysis_with_loop(object(), settings, {"interaction_language": "en"})
        self.assertEqual(run_mock.call_count, 2)
        self.assertTrue(res.get("quality_ok"))

    def test_verify_workspace_first_load_deep_choice_runs_onboarding_and_deep(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(workspace_root=Path(tmp))
            state = {"interaction_language": "en"}
            runtime = DummyRuntime()
            check = {
                "status": "no_docs",
                "message": "missing",
                "git": {"current_head": "abc123"},
            }
            with mock.patch.object(cli, "_run_docs_head_check", side_effect=[check, check]), \
                 mock.patch.object(cli, "_persist_last_access_head") as persist_mock, \
                 mock.patch.object(cli, "_prompt_analysis_choice", return_value="deep"), \
                 mock.patch("app.research.onboarding.run_onboarding", return_value={"status": "ok", "written": {}}) as onb_mock, \
                 mock.patch.object(cli, "_run_deep_analysis_with_loop", return_value={"status": "complete", "written": {"a": "b"}}) as deep_mock, \
                 mock.patch.object(cli, "_print_onboarding_result"):
                with redirect_stdout(io.StringIO()):
                    cli._verify_workspace_docs_alignment(runtime, settings, state, interactive=True)
            onb_mock.assert_called_once()
            deep_mock.assert_called_once()
            persist_mock.assert_called()

    def test_verify_workspace_first_load_normal_then_followup_deep_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(workspace_root=Path(tmp))
            state = {"interaction_language": "en"}
            runtime = DummyRuntime()
            check = {"status": "no_docs", "message": "missing", "git": {"current_head": "abc123"}}
            # follow-up asks "run deep now?" -> yes
            with mock.patch.object(cli, "_run_docs_head_check", side_effect=[check, check]), \
                 mock.patch.object(cli, "_persist_last_access_head"), \
                 mock.patch.object(cli, "_prompt_analysis_choice", return_value="normal"), \
                 mock.patch("app.research.onboarding.run_onboarding", return_value={"status": "ok", "written": {}}) as onb_mock, \
                 mock.patch.object(cli, "_read_input", return_value="s"), \
                 mock.patch.object(cli, "_run_deep_analysis_with_loop", return_value={"status": "complete"}) as deep_mock, \
                 mock.patch.object(cli, "_print_onboarding_result"):
                with redirect_stdout(io.StringIO()):
                    cli._verify_workspace_docs_alignment(runtime, settings, state, interactive=True)
            onb_mock.assert_called_once()
            deep_mock.assert_called_once()

    def test_offer_research_skip_and_deep_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(workspace_root=Path(tmp))
            runtime = DummyRuntime()
            # skip
            with mock.patch.object(cli, "_prompt_analysis_choice", return_value="skip"), \
                 mock.patch("app.research.onboarding.run_onboarding") as onb_mock, \
                 mock.patch.object(cli, "_run_deep_analysis_with_loop") as deep_mock:
                cli._offer_research(runtime, settings, {"interaction_language": "en"}, interactive=True)
            onb_mock.assert_not_called()
            deep_mock.assert_not_called()
            # deep: runs onboarding base then deep loop
            with mock.patch.object(cli, "_prompt_analysis_choice", return_value="deep"), \
                 mock.patch("app.research.onboarding.run_onboarding", return_value={"status": "ok", "written": {}}) as onb_mock, \
                 mock.patch.object(cli, "_run_deep_analysis_with_loop", return_value={"status": "complete"}) as deep_mock, \
                 mock.patch.object(cli, "_print_onboarding_result"):
                with redirect_stdout(io.StringIO()):
                    cli._offer_research(runtime, settings, {"interaction_language": "en"}, interactive=True)
            onb_mock.assert_called_once()
            deep_mock.assert_called_once()

    def test_main_menu_and_post_task_deep_analysis_options(self):
        # Exercise main initial menu 'd' then 'q', and post-task menu 'd' then 'q'
        captured = {}

        def fake_run_graph(app, initial, config, interactive=True):
            captured["ran"] = True
            return {"phase": "done", "status": "done_no_commit", "done": True, "blocked": False, "plan": [], "interaction_language": "en"}

        settings = _Settings(
            workspace_root=Path.cwd(),
            max_fix_cycles=3,
            interaction_language=None,
        )

        # initial menu: deep analysis, then quit
        answers = iter(["d", "q"])
        with mock.patch("app.cli.start_reader"), \
             mock.patch("app.cli.restore_terminal"), \
             mock.patch("app.cli.get_settings", return_value=settings), \
             mock.patch("app.cli._session_startup", return_value={"interaction_language": "en"}), \
             mock.patch("app.cli._load_workspace", return_value=str(Path.cwd())), \
             mock.patch("app.cli.load_agents", return_value={}), \
             mock.patch("app.cli.AgentRuntime", DummyRuntime), \
             mock.patch("app.cli.build_graph", return_value=object()), \
             mock.patch("app.cli._run_graph", side_effect=fake_run_graph), \
             mock.patch("app.cli._ensure_gitignore_for_agents"), \
             mock.patch("app.cli._verify_workspace_docs_alignment"), \
             mock.patch("app.cli._offer_research"), \
             mock.patch.object(cli, "_run_deep_analysis_with_loop", return_value={"status": "complete"}) as deep_mock, \
             mock.patch.object(cli, "_read_input", side_effect=lambda *a, **k: next(answers)):
            with redirect_stdout(io.StringIO()):
                code = cli.main([])
        self.assertEqual(code, 0)
        deep_mock.assert_called_once()

        # post-task menu path: provide requirement via initial menu start, graph returns done_no_commit, choose deep then quit
        answers = iter(["s", "do thing", "d", "q"])
        with mock.patch("app.cli.start_reader"), \
             mock.patch("app.cli.restore_terminal"), \
             mock.patch("app.cli.get_settings", return_value=settings), \
             mock.patch("app.cli._session_startup", return_value={"interaction_language": "en"}), \
             mock.patch("app.cli._load_workspace", return_value=str(Path.cwd())), \
             mock.patch("app.cli.load_agents", return_value={}), \
             mock.patch("app.cli.AgentRuntime", DummyRuntime), \
             mock.patch("app.cli.build_graph", return_value=object()), \
             mock.patch("app.cli._run_graph", side_effect=fake_run_graph), \
             mock.patch("app.cli._ensure_gitignore_for_agents"), \
             mock.patch("app.cli._verify_workspace_docs_alignment"), \
             mock.patch("app.cli._offer_research"), \
             mock.patch.object(cli, "_run_deep_analysis_with_loop", return_value={"status": "complete"}) as deep_mock, \
             mock.patch.object(cli, "_read_input", side_effect=lambda *a, **k: next(answers)):
            with redirect_stdout(io.StringIO()):
                code = cli.main([])
        self.assertEqual(code, 0)
        deep_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
