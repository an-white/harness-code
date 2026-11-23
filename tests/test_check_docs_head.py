import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

# Import script module helpers for deterministic unit-level checks too.
import importlib.util

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "app" / "tools" / "check_docs_head.py"
_spec = importlib.util.spec_from_file_location("check_docs_head", _SCRIPT_PATH)
cdh = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(cdh)  # noqa: E402


class CheckDocsHeadScriptTest(unittest.TestCase):
    def _run_script(self, cwd: Path, extra_args=None, check=True):
        script = Path(__file__).resolve().parents[1] / "app" / "tools" / "check_docs_head.py"
        args = [sys.executable, str(script), "--workspace", str(cwd), "--json"]
        if extra_args:
            args.extend(extra_args)
        res = subprocess.run(args, capture_output=True, text=True, cwd=str(cwd))
        out = (res.stdout or "").strip()
        payload = json.loads(out) if out else None
        if check:
            self.assertIsInstance(payload, dict, msg=f"stdout={res.stdout!r} stderr={res.stderr!r}")
        return res.returncode, payload, res

    def _init_git_repo(self, path: Path) -> str:
        subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(path), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
        (path / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(path), check=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(path), text=True).strip()
        return head

    def _write_specs(self, ws: Path, head: str, fingerprint: str = "fp123"):
        docs = ws / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        content = (
            "# Project Specs\n\n"
            "## Metadata\n"
            "| Key | Value |\n"
            "| --- | --- |\n"
            f"| Commit/HEAD | {head} |\n"
            f"| Fingerprint | {fingerprint} |\n"
        )
        (docs / "project_specs.md").write_text(content, encoding="utf-8")

    def _write_state(self, ws: Path, head: str, fingerprint: str = "fp123", **extra):
        st_dir = ws / ".agents" / "research"
        st_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "head": head,
            "fingerprint": fingerprint,
            "last_access_head": head,
            "onboarding_done": True,
        }
        data.update(extra)
        (st_dir / "state.json").write_text(json.dumps(data), encoding="utf-8")

    def test_no_docs_first_load_exit_2(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            # No git, no docs/state -> no_docs
            code, payload, _ = self._run_script(ws, extra_args=["--no-fingerprint"])
            self.assertEqual(code, 2, payload)
            self.assertEqual(payload["status"], "no_docs")
            self.assertEqual(payload["exit_code"], 2)
            self.assertIn("first load", payload["message"].lower())

    def test_no_docs_with_git_but_no_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._init_git_repo(ws)
            code, payload, _ = self._run_script(ws, extra_args=["--no-fingerprint"])
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "no_docs")
            self.assertTrue(payload["git"]["available"])
            self.assertIsNotNone(payload["git"]["current_head"])

    def test_aligned_exit_0(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            self._write_specs(ws, head)
            self._write_state(ws, head)
            # Avoid fingerprint coupling to compute_fingerprint internals by skipping fingerprint compare
            code, payload, _ = self._run_script(ws, extra_args=["--no-fingerprint"])
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["status"], "aligned")
            self.assertEqual(payload["exit_code"], 0)
            self.assertEqual(payload["misalignment_reasons"], [])
            self.assertEqual(payload["docs"]["commit_head"], head)
            self.assertEqual(payload["state"]["head"], head)

    def test_misaligned_head_changed_exit_1(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            self._write_specs(ws, head)
            self._write_state(ws, head)
            # advance HEAD
            (ws / "file.txt").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=str(ws), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=str(ws), check=True)
            new_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ws), text=True).strip()
            self.assertNotEqual(head, new_head)
            code, payload, _ = self._run_script(ws, extra_args=["--no-fingerprint"])
            self.assertEqual(code, 1, payload)
            self.assertEqual(payload["status"], "misaligned")
            reasons = payload["misalignment_reasons"]
            self.assertTrue(
                {"docs_head_mismatch", "state_head_mismatch", "state_last_access_head_mismatch"}.issubset(set(reasons)),
                reasons,
            )

    def test_misaligned_docs_only_desalineadas(self):
        # Docs exist but HEAD metadata missing/mismatched relative to state+git -> misaligned reasons
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            # docs missing commit/fingerprint metadata -> docs_metadata_incomplete
            docs = ws / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            (docs / "project_specs.md").write_text("# Specs\n\nNo metadata table\n", encoding="utf-8")
            self._write_state(ws, head)
            code, payload, _ = self._run_script(ws, extra_args=["--no-fingerprint"])
            self.assertEqual(code, 1, payload)
            self.assertEqual(payload["status"], "misaligned")
            self.assertIn("docs_metadata_incomplete", payload["misalignment_reasons"])

    def test_misaligned_state_last_access_only(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            self._write_specs(ws, head)
            self._write_state(ws, head, last_access_head="deadbeef" * 5)
            code, payload, _ = self._run_script(ws, extra_args=["--no-fingerprint"])
            self.assertEqual(code, 1, payload)
            self.assertIn("state_last_access_head_mismatch", payload["misalignment_reasons"])

    def test_workspace_not_found_exit_3(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope"
            script = Path(__file__).resolve().parents[1] / "app" / "tools" / "check_docs_head.py"
            res = subprocess.run(
                [sys.executable, str(script), "--workspace", str(missing), "--json"],
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 3)
            payload = json.loads(res.stdout.strip())
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["reason"], "workspace_not_found")

    def test_json_output_has_expected_keys(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            code, payload, _ = self._run_script(ws, extra_args=["--no-fingerprint"])
            for key in ("status", "exit_code", "message", "workspace", "repo_root", "git", "docs", "state", "misalignment_reasons"):
                self.assertIn(key, payload, key)

    def test_parse_project_specs_metadata_formats(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "project_specs.md"
            # backticks/spacing/case variations
            p.write_text(
                "# Specs\n| `Commit/HEAD` | `abc123` |\n| Fingerprint | fp-xyz |\n",
                encoding="utf-8",
            )
            meta = cdh.parse_project_specs_metadata(p)
            self.assertTrue(meta["exists"])
            self.assertTrue(meta["metadata_found"])
            self.assertEqual(meta["commit_head"], "abc123")
            self.assertEqual(meta["fingerprint"], "fp-xyz")

    def test_normalize_hex_placeholders(self):
        self.assertIsNone(cdh._normalize_hex(None))
        self.assertIsNone(cdh._normalize_hex(""))
        self.assertIsNone(cdh._normalize_hex("n/a"))
        self.assertIsNone(cdh._normalize_hex("unknown"))
        self.assertEqual(cdh._normalize_hex("  abc  "), "abc")

    def test_onboarding_not_done_without_specs_treated_as_first_load(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            # state exists but onboarding not done and no specs -> first_load per script logic
            self._write_state(ws, head, onboarding_done=False)
            # remove specs (not written)
            code, payload, _ = self._run_script(ws, extra_args=["--no-fingerprint"])
            self.assertEqual(code, 2, payload)
            self.assertEqual(payload["status"], "no_docs")

    def test_state_read_error_causes_misaligned_when_docs_exist(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            head = self._init_git_repo(ws)
            self._write_specs(ws, head)
            st_dir = ws / ".agents" / "research"
            st_dir.mkdir(parents=True, exist_ok=True)
            (st_dir / "state.json").write_text("{not-json", encoding="utf-8")
            code, payload, _ = self._run_script(ws, extra_args=["--no-fingerprint"])
            self.assertEqual(code, 1, payload)
            self.assertEqual(payload["status"], "misaligned")
            self.assertIn("state_read_error", payload["misalignment_reasons"])


if __name__ == "__main__":
    unittest.main()
