import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.cli import (
    _persist_last_access_head,
    _run_docs_head_check,
    _verify_workspace_docs_alignment,
    main as cli_main,
)
from app.graph.nodes import _ensure_docs_updated_before_commit


class DummyRuntime:
    def __init__(self, *args, **kwargs):
        pass


class DocsHeadHelpersTest(unittest.TestCase):
    def _init_git_repo(self, path: Path) -> str:
        subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(path), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
        (path / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(path), check=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(path), text=True).strip()
        return head

    def _write_aligned_docs(self, ws: Path, head: str):
        docs = ws / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "project_specs.md").write_text(
            "# Project Specs\n\n## Metadata\n| Key | Value |\n| --- | --- |\n"
            f"| Commit/HEAD | {head} |\n| Fingerprint | fp |\n",
            encoding="utf-8",
        )
        st_dir = ws / ".agents" / "research"
        st_dir.mkdir(parents=True, exist_ok=True)
        (st_dir / "state.json").write_text(
            json.dumps({"head": head, "fingerprint": "fp", "last_access_head": head, "onboarding_done": True}),
            encoding="utf-8",
        )

    def test_run_docs_head_check_parses_json(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            # script exists in project; running against empty ws yields no_docs payload
            payload = _run_docs_head_check(ws)
            self.assertIsInstance(payload, dict)
            self.assertIn(payload.get("status"), {"no_docs", "error", "misaligned", "aligned"})

    def test_persist_last_access_head_creates_state(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            _persist_last_access_head(ws, "abc123")
            st_path = ws / ".agents" / "research" / "state.json"
            self.assertTrue(st_path.exists(), "state.json should be created")
            data = json.loads(st_path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("last_access_head"), "abc123")

    def test_verify_non_interactive_no_docs_emits_notice_no_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._init_git_repo(ws)
            settings = SimpleNamespace(workspace_root=ws)
            state = {"interaction_language": "en"}
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch("app.research.onboarding.run_onboarding") as m_onb, \
                 mock.patch("app.cli._read_input") as m_read:
                _verify_workspace_docs_alignment(
                    DummyRuntime(), settings, state,
                    interactive=False, refresh_research=False, skip_onboarding=False,
                )
            out = buf.getvalue()
            # should emit alignment notice, not prompt
            self.assertIn("no_docs", out.lower() + str(m_read.call_args))
            m_read.assert_not_called()
            m_onb.assert_not_called()
            # last_access_head persisted
            st_path = ws / ".agents" / "research" / "state.json"
            self.assertTrue(st_path.exists())
            st = json.loads(st_path.read_text(encoding="utf-8"))
            self.assertTrue(st.get("last_access_head"))

    def test_verify_non_interactive_refresh_research_triggers_onboarding(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._init_git_repo(ws)
            settings = SimpleNamespace(workspace_root=ws)
            state = {"interaction_language": "en"}
            fake_result = {"status": "ok"}
            with mock.patch("app.research.onboarding.run_onboarding", return_value=fake_result) as m_onb, \
                 mock.patch("app.cli._read_input") as m_read, \
                 redirect_stdout(io.StringIO()):
                _verify_workspace_docs_alignment(
                    DummyRuntime(), settings, state,
                    interactive=False, refresh_research=True, skip_onboarding=False,
                )
            m_read.assert_not_called()
            self.assertTrue(m_onb.called, "onboarding should be called in non-interactive refresh mode")
            _, kwargs = m_onb.call_args
            self.assertTrue(kwargs.get("refresh"))

    def test_verify_skip_onboarding_persists_head_no_prompts(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            settings = SimpleNamespace(workspace_root=ws)
            state = {"interaction_language": "en"}
            with mock.patch("app.research.onboarding.run_onboarding") as m_onb, \
                 mock.patch("app.cli._read_input") as m_read, \
                 redirect_stdout(io.StringIO()):
                _verify_workspace_docs_alignment(
                    DummyRuntime(), settings, state,
                    interactive=True, refresh_research=False, skip_onboarding=True,
                )
            m_read.assert_not_called()
            m_onb.assert_not_called()
            st = json.loads((ws / ".agents" / "research" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(st.get("last_access_head"), head)

    def test_verify_interactive_first_load_prompt_yes_runs_onboarding(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._init_git_repo(ws)
            settings = SimpleNamespace(workspace_root=ws)
            state = {"interaction_language": "en"}
            with mock.patch("app.cli._read_input", return_value="s") as m_read, \
                 mock.patch("app.research.onboarding.run_onboarding", return_value={"status": "ok"}) as m_onb, \
                 redirect_stdout(io.StringIO()):
                _verify_workspace_docs_alignment(
                    DummyRuntime(), settings, state,
                    interactive=True, refresh_research=False, skip_onboarding=False,
                )
            self.assertTrue(m_read.called)
            self.assertTrue(m_onb.called)

    def test_verify_interactive_first_load_prompt_no_skips_onboarding(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._init_git_repo(ws)
            settings = SimpleNamespace(workspace_root=ws)
            state = {"interaction_language": "en"}
            with mock.patch("app.cli._read_input", return_value="n"), \
                 mock.patch("app.research.onboarding.run_onboarding") as m_onb, \
                 redirect_stdout(io.StringIO()):
                _verify_workspace_docs_alignment(
                    DummyRuntime(), settings, state,
                    interactive=True, refresh_research=False, skip_onboarding=False,
                )
            m_onb.assert_not_called()

    def test_verify_interactive_misaligned_prompts_update(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            # create misaligned docs/state (old head)
            self._write_aligned_docs(ws, "0" * 40)
            settings = SimpleNamespace(workspace_root=ws)
            state = {"interaction_language": "en"}
            with mock.patch("app.cli._read_input", return_value="n") as m_read, \
                 mock.patch("app.research.onboarding.run_onboarding") as m_onb, \
                 redirect_stdout(io.StringIO()):
                _verify_workspace_docs_alignment(
                    DummyRuntime(), settings, state,
                    interactive=True, refresh_research=False, skip_onboarding=False,
                )
            self.assertTrue(m_read.called, "should prompt on misalignment")
            m_onb.assert_not_called()


class PrecommitDocsUpdateTest(unittest.TestCase):
    def _init_git_repo(self, path: Path) -> str:
        subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(path), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
        (path / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(path), check=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(path), text=True).strip()
        return head

    def _write_aligned_docs(self, ws: Path, head: str):
        docs = ws / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "project_specs.md").write_text(
            "# Project Specs\n\n## Metadata\n| Key | Value |\n| --- | --- |\n"
            f"| Commit/HEAD | {head} |\n| Fingerprint | fp |\n",
            encoding="utf-8",
        )
        st_dir = ws / ".agents" / "research"
        st_dir.mkdir(parents=True, exist_ok=True)
        (st_dir / "state.json").write_text(
            json.dumps({"head": head, "fingerprint": "fp", "last_access_head": head, "onboarding_done": True}),
            encoding="utf-8",
        )

    def test_precommit_aligned_no_refresh_needed(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            self._write_aligned_docs(ws, head)
            settings = SimpleNamespace(workspace_root=ws)
            # Mock check to aligned to avoid fingerprint dependency, and ensure no onboarding call
            aligned_payload = {
                "status": "aligned",
                "git": {"current_head": head},
                "docs": {"exists": True}, "state": {"exists": True},
            }
            with mock.patch("app.graph.nodes._run_docs_head_check", return_value=aligned_payload) as m_check, \
                 mock.patch("app.research.onboarding.run_onboarding") as m_onb, \
                 mock.patch("app.graph.nodes._persist_last_access_head") as m_persist:
                res = _ensure_docs_updated_before_commit(ws, settings, None, interaction_language="en")
            m_onb.assert_not_called()
            self.assertTrue(res.get("ok"))
            self.assertFalse(res.get("updated"))
            self.assertIn("no update needed", res.get("message", "").lower())

    def test_precommit_misaligned_triggers_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            settings = SimpleNamespace(workspace_root=ws)
            misaligned = {"status": "misaligned", "git": {"current_head": head}}
            aligned_after = {"status": "aligned", "git": {"current_head": head}}
            with mock.patch("app.graph.nodes._run_docs_head_check", side_effect=[misaligned, aligned_after]), \
                 mock.patch("app.graph.nodes.run_onboarding", return_value={"status": "ok"}) as m_onb, \
                 mock.patch("app.graph.nodes._persist_last_access_head") as m_persist:
                res = _ensure_docs_updated_before_commit(ws, settings, DummyRuntime(), interaction_language="en")
            self.assertTrue(m_onb.called)
            _, kwargs = m_onb.call_args
            self.assertTrue(kwargs.get("refresh"))
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("updated"))
            self.assertEqual(res.get("refresh_status"), "ok")

    def test_precommit_refresh_failure_reports_error(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            settings = SimpleNamespace(workspace_root=ws)
            misaligned = {"status": "misaligned", "git": {"current_head": head}}
            with mock.patch("app.graph.nodes._run_docs_head_check", side_effect=[misaligned, misaligned]), \
                 mock.patch("app.graph.nodes.run_onboarding", side_effect=RuntimeError("boom")), \
                 mock.patch("app.graph.nodes._persist_last_access_head"):
                res = _ensure_docs_updated_before_commit(ws, settings, DummyRuntime(), interaction_language="en")
            self.assertEqual(res.get("refresh_status"), "failed")
            self.assertIn("boom", str(res.get("error")))
            self.assertIn("refresh", res.get("message", "").lower())

    def test_precommit_check_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            settings = SimpleNamespace(workspace_root=ws)
            with mock.patch("app.graph.nodes._run_docs_head_check", return_value=None):
                res = _ensure_docs_updated_before_commit(ws, settings, None)
            self.assertFalse(res.get("ok"))
            self.assertEqual(res.get("status"), "check_unavailable")


class CLINonInteractiveBehaviorTest(unittest.TestCase):
    def test_check_flag_still_ok(self):
        # Sanity: --check path unaffected
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["--check"])
        self.assertEqual(code, 0)

    def test_non_interactive_research_run_no_prompt_path(self):
        # Existing pattern: --non-interactive with mocked graph should not prompt
        captured = {}

        def fake_run_graph(app, initial, config, interactive=True):
            captured["interactive"] = interactive
            captured["initial"] = dict(initial)
            return {"phase": "done", "status": "done", "done": True, "blocked": False, "plan": []}

        with mock.patch("app.cli.AgentRuntime", DummyRuntime), \
             mock.patch("app.cli.build_graph", return_value=object()), \
             mock.patch("app.cli._run_graph", side_effect=fake_run_graph), \
             mock.patch("app.cli._verify_workspace_docs_alignment") as m_verify, \
             mock.patch("app.cli._offer_research"), \
             mock.patch("app.cli._session_startup", return_value={"interaction_language": "en"}), \
             mock.patch("app.cli._load_workspace", return_value=str(Path.cwd())):
            code = cli_main(["Do thing", "--non-interactive", "--skip-onboarding", "--no-web"])
        self.assertEqual(code, 0)
        self.assertFalse(captured.get("interactive"))
        self.assertTrue(m_verify.called)
        # verify called with interactive=False in non-interactive mode
        _, kwargs = m_verify.call_args
        self.assertFalse(kwargs.get("interactive", True))


if __name__ == "__main__":
    unittest.main()
