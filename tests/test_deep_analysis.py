import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.research.deep_analysis import (
    app_flow_path,
    deep_analysis_dir,
    deep_analysis_state_path,
    generate_partial_artifacts,
    read_deep_analysis_state,
    run_deep_analysis,
    user_summary_path,
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return (res.stdout or "").strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")


def _seed_repo(repo: Path) -> None:
    _write(repo / "README.md", "# Demo\n")
    _write(repo / "app" / "main.py", "def main():\n    return 'hi'\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")


def _good_analysis(lang_summary: str = "Resumen") -> dict:
    return {
        "overview": ["Demo project overview."],
        "architecture": ["Simple architecture."],
        "flows": ["CLI -> main flow."],
        "modules": ["app/main.py"],
        "data_and_integrations": ["No external integrations."],
        "risks_and_gaps": ["None critical."],
        "app_flow_text": "Textual flow for demo app.",
        "app_flow_mermaid": "flowchart TD\n  A[Start] --> B[main] --> C[End]",
        "user_summary": lang_summary,
        "evidence": ["README.md", "app/main.py"],
        "coverage_notes": ["scanned sample"],
    }


def _has_metadata(text: str, status: str) -> bool:
    return (
        "## Metadata" in text
        and "| Key | Value |" in text
        and f"| Status | {status} |" in text
        and "Generated (UTC)" in text
        and "Coverage" in text
    )


class DeepAnalysisArtifactsTest(unittest.TestCase):
    def test_runtime_unavailable_generates_required_partial_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            _seed_repo(root)
            res = run_deep_analysis(
                root,
                settings=None,
                runtime=None,
                synthesizer=None,
                interaction_language="es",
                generate_partial_on_failure=True,
            )
            self.assertEqual(res["status"], "incomplete")
            self.assertFalse(res["quality_ok"])
            self.assertIn("runtime_unavailable", res.get("quality_errors") or [])
            da = deep_analysis_dir(root)
            required = [
                "README.md",
                "overview.md",
                "architecture.md",
                "flows.md",
                "modules.md",
                "data_and_integrations.md",
                "risks_and_gaps.md",
            ]
            for name in required:
                p = da / name
                self.assertTrue(p.exists(), name)
                self.assertTrue(_has_metadata(p.read_text(encoding="utf-8"), "incomplete"), name)
            app = app_flow_path(root)
            self.assertTrue(app.exists())
            self.assertTrue(_has_metadata(app.read_text(encoding="utf-8"), "incomplete"))
            us = user_summary_path(root, "es")
            self.assertTrue(us.exists(), "user_summary should exist for non-English workspace")
            ust = us.read_text(encoding="utf-8")
            self.assertTrue(_has_metadata(ust, "incomplete"))
            self.assertIn("Pending / Incomplete", ust)
            self.assertIn("es", ust)
            st = read_deep_analysis_state(root)
            self.assertEqual(st.get("status"), "incomplete")
            self.assertTrue(st.get("analyzed_head"))
            self.assertIn("limitations", st)

    def test_quality_insufficient_retry_exhausted_generates_partial_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            _seed_repo(root)
            calls = {"n": 0}

            def bad_synth(prompt, scan):
                calls["n"] += 1
                return {"overview": [], "app_flow_mermaid": "not-mermaid"}  # invalid/insufficient

            res = run_deep_analysis(
                root,
                runtime=object(),
                synthesizer=bad_synth,
                interaction_language="en",
                generate_partial_on_failure=True,
                max_attempts=2,
            )
            self.assertEqual(calls["n"], 2)
            self.assertEqual(res["status"], "incomplete")
            self.assertFalse(res["quality_ok"])
            self.assertTrue(res.get("retryable"))
            self.assertTrue(deep_analysis_dir(root,).joinpath("README.md").exists() if False else (deep_analysis_dir(root) / "README.md").exists())
            self.assertTrue(_has_metadata(app_flow_path(root).read_text(encoding="utf-8"), "incomplete"))

    def test_user_rejection_path_signaled_and_partial_generation_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            _seed_repo(root)
            # run_deep_analysis signals rejection without generating partials by itself
            rej = run_deep_analysis(
                root,
                runtime=object(),
                synthesizer=lambda p, s: _good_analysis(),
                quality_rejection_reason="user_rejected_quality",
            )
            self.assertEqual(rej["status"], "failed")
            self.assertIn("user_rejected", rej.get("quality_errors") or [])
            self.assertTrue(rej.get("retryable"))
            # CLI/helper flow can then produce partial/incomplete artifacts for stop/decline
            partial = generate_partial_artifacts(
                root,
                reason="user_rejected",
                settings=None,
                interaction_language="en",
            )
            self.assertEqual(partial["status"], "incomplete")
            self.assertTrue((deep_analysis_dir(root) / "overview.md").exists())
            self.assertTrue(_has_metadata(app_flow_path(root).read_text(encoding="utf-8"), "incomplete"))

    def test_non_english_user_summary_pending_when_translation_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            _seed_repo(root)
            # runtime without translator/localize success -> translation unavailable
            runtime = mock.Mock()

            def _run(role, messages, context=None):
                if role == "research":
                    return {"final": json.dumps(_good_analysis("English summary"))}
                # translator unavailable/fails
                return {"error": "unavailable", "final": ""}

            runtime.run.side_effect = _run
            res = run_deep_analysis(
                root,
                runtime=runtime,
                synthesizer=None,
                interaction_language="es",
            )
            self.assertEqual(res["status"], "complete_with_pending_summary")
            self.assertTrue(res["quality_ok"])
            # required EN artifacts and app_flow must still be generated
            for name in [
                "README.md",
                "overview.md",
                "architecture.md",
                "flows.md",
                "modules.md",
                "data_and_integrations.md",
                "risks_and_gaps.md",
            ]:
                p = deep_analysis_dir(root) / name
                self.assertTrue(p.exists(), name)
                self.assertTrue(_has_metadata(p.read_text(encoding="utf-8"), "complete"), name)
            self.assertTrue(_has_metadata(app_flow_path(root).read_text(encoding="utf-8"), "complete"))
            us = user_summary_path(root, "es")
            self.assertTrue(us.exists())
            ust = us.read_text(encoding="utf-8")
            self.assertTrue(_has_metadata(ust, "incomplete"))
            self.assertIn("Pending / Incomplete", ust)
            self.assertIsNotNone(res.get("user_summary_pending"))

    def test_non_english_user_summary_generated_when_localization_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            _seed_repo(root)
            res = run_deep_analysis(
                root,
                runtime=object(),
                synthesizer=lambda p, s: _good_analysis("Resumen base"),
                interaction_language="es",
                localize=lambda text, lang: f"[{lang}] " + text,
            )
            self.assertEqual(res["status"], "complete")
            self.assertTrue(res["quality_ok"])
            us = user_summary_path(root, "es")
            self.assertTrue(us.exists())
            ust = us.read_text(encoding="utf-8")
            self.assertIn("[es]", ust)
            self.assertNotIn("Pending / Incomplete", ust)
            self.assertIsNone(res.get("user_summary_pending"))


class DeepAnalysisGitAndOnboardingTest(unittest.TestCase):
    def test_git_change_detection_regenerates_only_impacted_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            _seed_repo(root)
            first = run_deep_analysis(
                root,
                runtime=object(),
                synthesizer=lambda p, s: _good_analysis(),
                interaction_language="en",
            )
            self.assertIn(first["status"], ("complete", "complete_with_pending_summary"))
            st1 = read_deep_analysis_state(root)
            head1 = st1.get("analyzed_head")
            self.assertTrue(head1)
            flows_mtime = (deep_analysis_dir(root) / "flows.md").stat().st_mtime_ns
            overview_mtime = (deep_analysis_dir(root) / "overview.md").stat().st_mtime_ns
            # change a flow-related source file and commit
            _write(root / "app" / "main.py", "def main():\n    return 'v2'\n")
            _git(root, "add", "app/main.py")
            _git(root, "commit", "-m", "change flow")
            head2 = _git(root, "rev-parse", "HEAD")
            self.assertNotEqual(head1, head2)
            # ensure mtimes can differ if rewritten
            os.utime(deep_analysis_dir(root) / "flows.md", ns=(flows_mtime - 10**9, flows_mtime - 10**9))
            os.utime(deep_analysis_dir(root) / "overview.md", ns=(overview_mtime - 10**9, overview_mtime - 10**9))
            second = run_deep_analysis(
                root,
                runtime=object(),
                synthesizer=lambda p, s: _good_analysis("v2"),
                interaction_language="en",
            )
            self.assertIn(second["status"], ("complete", "complete_with_pending_summary"))
            cov = second.get("coverage") or {}
            self.assertIn("app/main.py", cov.get("changed_paths_since_previous_analysis") or [])
            self.assertEqual(cov.get("change_detection_error"), None)
            regen = set(cov.get("regenerated_artifacts") or [])
            self.assertIn("flows.md", regen)
            self.assertIn("app_flow", regen)
            self.assertNotEqual(set(cov.get("regenerated_groups") or []), {"all"})
            changed_paths_detected = cov.get("changed_paths_since_previous_analysis") or []
            self.assertIn("app/main.py", changed_paths_detected)
            self.assertIsNone(cov.get("change_detection_error"))
            st2 = read_deep_analysis_state(root)
            self.assertEqual(st2.get("analyzed_head"), head2)

    def test_preserves_onboarding_artifacts_unless_improved_info_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            _seed_repo(root)
            # seed onboarding artifacts
            specs = root / "docs" / "project_specs.md"
            ctx = root / ".agents" / "research" / "context.md"
            stp = root / ".agents" / "research" / "state.json"
            _write(specs, "# Specs\nOLD\n")
            _write(ctx, "OLD CONTEXT\n")
            _write(stp, json.dumps({"fingerprint": "old", "onboarding_done": True}, indent=2) + "\n")
            before_specs = specs.read_text(encoding="utf-8")
            before_ctx = ctx.read_text(encoding="utf-8")
            # no onboarding_updates -> should not overwrite
            run_deep_analysis(
                root,
                runtime=object(),
                synthesizer=lambda p, s: _good_analysis(),
                interaction_language="en",
            )
            self.assertEqual(specs.read_text(encoding="utf-8"), before_specs)
            self.assertEqual(ctx.read_text(encoding="utf-8"), before_ctx)
            # explicit improved onboarding update -> may update/preserve coherently
            improved = dict(_good_analysis())
            improved["onboarding_updates"] = {
                "project_specs_content": {
                    "title": "Demo Improved",
                    "quick_context": "Improved context",
                    "business": "Improved business",
                    "digest": "Improved digest",
                }
            }
            res = run_deep_analysis(
                root,
                runtime=object(),
                synthesizer=lambda p, s: improved,
                interaction_language="en",
                refresh=True,
            )
            self.assertIsNotNone(res.get("onboarding_written"))
            self.assertTrue(specs.exists())
            self.assertIn("Improved", specs.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
