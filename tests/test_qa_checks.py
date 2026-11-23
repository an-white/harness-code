"""Tests for :mod:`app.qa.checks`."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.qa.checks as checks
from app.qa.checks import (
    MAX_OUTPUT_BYTES,
    capture_task_baseline,
    check_python_syntax,
    detect_scope_violations,
    discover_git_repos,
    run_verification_commands,
)

GIT = shutil.which("git") is not None
requires_git = unittest.skipUnless(GIT, "git is not installed")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "qa@example.com")
    _git(path, "config", "user.name", "QA")
    return path


def _commit(repo: Path, message: str = "commit") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry(baseline: dict, root: str) -> dict:
    for entry in baseline["repos"]:
        if entry["root"] == root:
            return entry
    raise AssertionError(f"missing baseline repo {root!r}: {baseline}")


def _changed(result: dict) -> set[tuple[str, str]]:
    return {(item["repo"], item["path"]) for item in result["changed"]}


def _violations(result: dict) -> set[tuple[str, str]]:
    return {(item["repo"], item["path"]) for item in result["violations"]}


def _pycache_dirs() -> set[Path]:
    base = Path(tempfile.gettempdir())
    return {path for path in base.glob("qa-pycache-*") if path.is_dir()}


class DiscoverGitReposTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_root_and_nested_repos_and_prunes_excluded_dirs(self):
        (self.root / ".git").mkdir()
        for name in ("a", "b", "target"):
            (self.root / name / ".git").mkdir(parents=True)
        for name in (".venv/lib", "node_modules/pkg", ".agents/x", "__pycache__/x", "dist/x", "build/x"):
            (self.root / name / ".git").mkdir(parents=True)

        self.assertEqual(discover_git_repos(self.root), [".", "a", "b", "target"])

    def test_skips_symlinked_repositories_and_never_escapes(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_repo = Path(outside.name) / "outside"
        (outside_repo / ".git").mkdir(parents=True)

        (self.root / "real" / ".git").mkdir(parents=True)
        try:
            os.symlink(outside_repo, self.root / "escape")
            os.symlink(self.root / "real", self.root / "alias")
        except OSError:
            self.skipTest("symlinks are not available")

        repos = discover_git_repos(self.root)
        self.assertEqual(repos, ["real"])

    def test_depth_and_repo_bounds(self):
        (self.root / "d1" / "d2" / "deep" / ".git").mkdir(parents=True)
        self.assertEqual(discover_git_repos(self.root, max_depth=2), [])
        self.assertEqual(discover_git_repos(self.root, max_depth=3), ["d1/d2/deep"])

        for name in ("a", "b", "c"):
            (self.root / name / ".git").mkdir(parents=True)
        self.assertEqual(discover_git_repos(self.root, max_repos=2), ["a", "b"])

    def test_invalid_root_returns_empty(self):
        self.assertEqual(discover_git_repos(self.root / "missing"), [])


@requires_git
class CaptureBaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_captures_head_and_hashes_dirty_and_untracked_files(self):
        repo = _init_repo(self.root / "repo")
        _write(repo / "tracked.txt", "one\n")
        _commit(repo)
        _write(repo / "tracked.txt", "two\n")
        _write(repo / "new.txt", "brand new\n")

        baseline = capture_task_baseline(self.root)
        entry = _entry(baseline, "repo")
        self.assertEqual(entry["head"], _git(repo, "rev-parse", "HEAD").strip())
        self.assertEqual(set(entry["dirty"]), {"tracked.txt", "new.txt"})
        self.assertEqual(entry["dirty"]["tracked.txt"], {
            "kind": "sha256",
            "sha256": _sha256("two\n"),
            "size": len("two\n"),
        })
        self.assertEqual(entry["dirty"]["new.txt"]["sha256"], _sha256("brand new\n"))
        json.dumps(baseline)

    def test_large_files_use_stat_fallback(self):
        repo = _init_repo(self.root / "repo")
        _write(repo / "big.bin", "0123456789")
        with patch.object(checks, "MAX_HASH_BYTES", 4):
            baseline = capture_task_baseline(self.root)
        snapshot = _entry(baseline, "repo")["dirty"]["big.bin"]
        self.assertEqual(snapshot["kind"], "stat")
        self.assertEqual(snapshot["size"], 10)
        self.assertIn("mtime_ns", snapshot)
        json.dumps(baseline)

    def test_deleted_dirty_file_is_handled(self):
        repo = _init_repo(self.root / "repo")
        _write(repo / "gone.txt", "bye\n")
        _commit(repo)
        (repo / "gone.txt").unlink()

        baseline = capture_task_baseline(self.root)
        self.assertEqual(_entry(baseline, "repo")["dirty"]["gone.txt"], {"kind": "deleted"})
        json.dumps(baseline)

    def test_repository_without_head(self):
        repo = _init_repo(self.root / "fresh")
        _write(repo / "draft.py", "value = 1\n")

        baseline = capture_task_baseline(self.root)
        entry = _entry(baseline, "fresh")
        self.assertIsNone(entry["head"])
        self.assertEqual(entry["dirty"]["draft.py"]["kind"], "sha256")

    def test_baseline_marks_incomplete_when_a_repository_fails(self):
        good = _init_repo(self.root / "good")
        _write(good / "a.txt", "a\n")
        bad = _init_repo(self.root / "bad")
        _write(bad / "b.txt", "b\n")

        real_run_git = checks._run_git

        def fake_run_git(repo, args, timeout=checks.GIT_TIMEOUT):
            if Path(repo).name == "bad" and args and args[0] == "status":
                return None
            return real_run_git(repo, args, timeout=timeout)

        with patch.object(checks, "_run_git", side_effect=fake_run_git):
            baseline = capture_task_baseline(self.root)

        self.assertFalse(baseline["skipped"])
        self.assertTrue(baseline["incomplete"])
        self.assertEqual(_entry(baseline, "bad")["error"], "git status failed")
        self.assertIsNone(_entry(baseline, "good")["error"])

    def test_baseline_skips_when_every_repository_fails(self):
        repo = _init_repo(self.root / "repo")
        _write(repo / "a.txt", "a\n")

        with patch.object(checks, "_run_git", return_value=None):
            baseline = capture_task_baseline(self.root)

        self.assertTrue(baseline["skipped"])
        self.assertTrue(baseline["incomplete"])
        self.assertIn("inspection failed", baseline["reason"])
        self.assertEqual(baseline["repos"][0]["error"], "git status failed")

    def test_baseline_marks_rev_parse_failure_with_refs_incomplete(self):
        good = _init_repo(self.root / "good")
        _write(good / "a.txt", "a\n")
        _commit(good)
        bad = _init_repo(self.root / "bad")
        _write(bad / "b.txt", "b\n")
        _commit(bad)

        real_run_git = checks._run_git

        def fake_run_git(repo, args, timeout=checks.GIT_TIMEOUT):
            if Path(repo).name == "bad":
                if args[:2] == ["rev-parse", "HEAD"]:
                    return None
                if args and args[0] == "for-each-ref":
                    return b"refs/heads/main\n"
            return real_run_git(repo, args, timeout=timeout)

        with patch.object(checks, "_run_git", side_effect=fake_run_git):
            baseline = capture_task_baseline(self.root)

        self.assertFalse(baseline["skipped"])
        self.assertTrue(baseline["incomplete"])
        self.assertEqual(_entry(baseline, "bad")["error"], "git rev-parse failed")
        self.assertIsNone(_entry(baseline, "good")["error"])


class NoGitWorkspaceTest(unittest.TestCase):
    def test_capture_and_detect_skip_without_git_repositories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "notes.txt", "hello\n")
            baseline = capture_task_baseline(root)
            self.assertTrue(baseline["skipped"])
            self.assertEqual(baseline["reason"], "no git repositories found")
            self.assertEqual(baseline["repos"], [])

            result = detect_scope_violations(root, baseline, ["notes.txt"])
            self.assertTrue(result["skipped"])
            self.assertEqual(result["reason"], "no git repositories found")
            self.assertEqual(result["changed"], [])
            self.assertEqual(result["violations"], [])
            json.dumps(result)


@requires_git
class DetectScopeViolationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _init_repo(self.root / "repo")
        _write(self.repo / "app" / "a.py", "a = 1\n")
        _write(self.repo / "app" / "sub" / "b.py", "b = 1\n")
        _write(self.repo / "docs" / "c.md", "doc\n")
        _commit(self.repo)
        self.baseline = capture_task_baseline(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_repo_relative_hint_allows_change(self):
        _write(self.repo / "app" / "a.py", "a = 2\n")
        result = detect_scope_violations(self.root, self.baseline, ["app/a.py"])
        self.assertEqual(_changed(result), {("repo", "app/a.py")})
        self.assertEqual(result["changed"][0]["reason"], "modified")
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["repositories"]["repo"]["changed"], ["app/a.py"])

    def test_exact_workspace_relative_hint_allows_change(self):
        _write(self.repo / "app" / "a.py", "a = 2\n")
        result = detect_scope_violations(self.root, self.baseline, ["repo/app/a.py"])
        self.assertEqual(result["violations"], [])

    def test_out_of_scope_change_is_a_violation(self):
        _write(self.repo / "docs" / "c.md", "changed doc\n")
        result = detect_scope_violations(self.root, self.baseline, ["app/a.py"])
        self.assertEqual(_violations(result), {("repo", "docs/c.md")})
        violation = result["violations"][0]
        self.assertEqual(violation["repo"], "repo")
        self.assertEqual(violation["path"], "docs/c.md")
        self.assertEqual(violation["workspace_path"], "repo/docs/c.md")
        self.assertEqual(violation["reason"], "out_of_scope")
        self.assertEqual(violation["change"], "modified")

    def test_single_segment_glob(self):
        _write(self.repo / "app" / "a.py", "a = 2\n")
        _write(self.repo / "app" / "sub" / "b.py", "b = 2\n")
        result = detect_scope_violations(self.root, self.baseline, ["app/*.py"])
        self.assertEqual(_changed(result), {("repo", "app/a.py"), ("repo", "app/sub/b.py")})
        self.assertEqual(_violations(result), {("repo", "app/sub/b.py")})

    def test_multi_segment_glob(self):
        _write(self.repo / "app" / "a.py", "a = 2\n")
        _write(self.repo / "app" / "sub" / "b.py", "b = 2\n")
        result = detect_scope_violations(self.root, self.baseline, ["app/**/*.py"])
        self.assertEqual(result["violations"], [])

    def test_trailing_slash_prefix(self):
        _write(self.repo / "app" / "a.py", "a = 2\n")
        _write(self.repo / "app" / "sub" / "b.py", "b = 2\n")
        result = detect_scope_violations(self.root, self.baseline, ["app/"])
        self.assertEqual(result["violations"], [])

    def test_empty_hints_flag_every_change(self):
        _write(self.repo / "app" / "a.py", "a = 2\n")
        _write(self.repo / "docs" / "c.md", "changed doc\n")
        result = detect_scope_violations(self.root, self.baseline, [])
        self.assertEqual(_violations(result), {("repo", "app/a.py"), ("repo", "docs/c.md")})

        result_none = detect_scope_violations(self.root, self.baseline, None)
        self.assertEqual(_violations(result_none), {("repo", "app/a.py"), ("repo", "docs/c.md")})

    def test_preexisting_dirty_file_untouched_is_not_reported(self):
        _write(self.repo / "docs" / "c.md", "already dirty\n")
        baseline = capture_task_baseline(self.root)
        _write(self.repo / "app" / "a.py", "a = 2\n")

        result = detect_scope_violations(self.root, baseline, ["app/a.py"])
        self.assertEqual(_changed(result), {("repo", "app/a.py")})
        self.assertEqual(result["violations"], [])

    def test_preexisting_dirty_file_modified_is_reported(self):
        _write(self.repo / "docs" / "c.md", "already dirty\n")
        baseline = capture_task_baseline(self.root)
        _write(self.repo / "app" / "a.py", "a = 2\n")
        _write(self.repo / "docs" / "c.md", "modified again\n")

        result = detect_scope_violations(self.root, baseline, ["app/a.py"])
        self.assertEqual(_changed(result), {("repo", "app/a.py"), ("repo", "docs/c.md")})
        self.assertEqual(_violations(result), {("repo", "docs/c.md")})

    def test_commit_during_task_is_detected(self):
        _write(self.repo / "app" / "new.py", "new = 1\n")
        _commit(self.repo, "add new module")

        result = detect_scope_violations(self.root, self.baseline, ["app/new.py"])
        self.assertEqual(_changed(result), {("repo", "app/new.py")})
        self.assertEqual(result["changed"][0]["reason"], "committed_added")
        self.assertEqual(result["violations"], [])

        out_of_scope = detect_scope_violations(self.root, self.baseline, ["app/a.py"])
        self.assertEqual(_violations(out_of_scope), {("repo", "app/new.py")})

    def test_repository_without_head(self):
        fresh = _init_repo(self.root / "fresh")
        _write(fresh / "draft.py", "value = 1\n")
        baseline = capture_task_baseline(self.root)

        _write(fresh / "draft.py", "value = 2\n")
        result = detect_scope_violations(self.root, baseline, ["fresh/draft.py"])
        self.assertEqual(_changed(result), {("fresh", "draft.py")})
        self.assertEqual(result["changed"][0]["reason"], "modified")
        self.assertEqual(result["violations"], [])

        _commit(fresh)
        after_commit = detect_scope_violations(self.root, baseline, [])
        self.assertEqual(_changed(after_commit), {("fresh", "draft.py")})
        self.assertEqual(after_commit["changed"][0]["reason"], "committed_added")
        self.assertEqual(_violations(after_commit), {("fresh", "draft.py")})

    def test_new_repository_is_reported(self):
        new_repo = _init_repo(self.root / "newrepo")
        _write(new_repo / "tool.py", "tool = 1\n")
        _commit(new_repo)

        result = detect_scope_violations(self.root, self.baseline, [])
        self.assertEqual(_changed(result), {("newrepo", "tool.py")})
        self.assertEqual(result["changed"][0]["reason"], "new_repository")
        self.assertTrue(result["repositories"]["newrepo"]["new_repository"])
        self.assertEqual(_violations(result), {("newrepo", "tool.py")})

    def test_new_nested_repository_is_reported_without_parent_noise(self):
        nested = _init_repo(self.repo / "nested")
        _write(nested / "tool.py", "tool = 1\n")
        _commit(nested)

        result = detect_scope_violations(self.root, self.baseline, ["repo/nested/tool.py"])
        self.assertEqual(_changed(result), {("repo/nested", "tool.py")})
        self.assertEqual(result["violations"], [])
        self.assertNotIn(("repo", "nested/"), _changed(result))
        self.assertTrue(result["repositories"]["repo/nested"]["new_repository"])

    def test_nested_repository_changes(self):
        nested = _init_repo(self.repo / "nested")
        _write(nested / "tool.py", "tool = 1\n")
        _commit(nested)
        baseline = capture_task_baseline(self.root)

        _write(self.repo / "app" / "a.py", "a = 2\n")
        _write(nested / "tool.py", "tool = 2\n")
        result = detect_scope_violations(self.root, baseline, ["app/a.py", "repo/nested/tool.py"])
        self.assertEqual(_changed(result), {("repo", "app/a.py"), ("repo/nested", "tool.py")})
        self.assertEqual(result["violations"], [])

    def test_deleted_file_is_detected(self):
        (self.repo / "docs" / "c.md").unlink()
        result = detect_scope_violations(self.root, self.baseline, [])
        self.assertEqual(_changed(result), {("repo", "docs/c.md")})
        self.assertEqual(result["changed"][0]["reason"], "deleted")
        self.assertEqual(_violations(result), {("repo", "docs/c.md")})

    def test_missing_baseline_is_skipped(self):
        result = detect_scope_violations(self.root, None, [])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "no baseline available")
        json.dumps(result)

    def test_detection_marks_incomplete_when_every_repository_is_uninspectable(self):
        with patch.object(checks, "_run_git", return_value=None):
            result = detect_scope_violations(self.root, self.baseline, ["app/a.py"])

        self.assertTrue(result["skipped"])
        self.assertTrue(result["incomplete"])
        self.assertIn("inspection failed", result["reason"])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["violations"], [])
        self.assertIsNotNone(result["repositories"]["repo"]["error"])

    def test_detection_marks_incomplete_when_a_repository_is_uninspectable(self):
        nested = _init_repo(self.repo / "nested")
        _write(nested / "tool.py", "tool = 1\n")
        _commit(nested)
        baseline = capture_task_baseline(self.root)
        _write(self.repo / "app" / "a.py", "a = 2\n")

        real_run_git = checks._run_git

        def fake_run_git(repo, args, timeout=checks.GIT_TIMEOUT):
            if Path(repo).name == "nested":
                return None
            return real_run_git(repo, args, timeout=timeout)

        with patch.object(checks, "_run_git", side_effect=fake_run_git):
            result = detect_scope_violations(self.root, baseline, ["app/a.py"])

        self.assertFalse(result["skipped"])
        self.assertTrue(result["incomplete"])
        self.assertEqual(_changed(result), {("repo", "app/a.py")})
        self.assertIsNotNone(result["repositories"]["repo/nested"]["error"])

    def test_commit_inspection_failure_is_not_a_clean_pass(self):
        nested = _init_repo(self.repo / "nested")
        _write(nested / "tool.py", "tool = 1\n")
        _commit(nested)
        baseline = capture_task_baseline(self.root)

        _write(self.repo / "app" / "new.py", "new = 1\n")
        _git(self.repo, "add", "app/new.py")
        _git(self.repo, "commit", "-q", "-m", "add new module")

        with patch.object(checks, "_diff_entries", return_value=None), patch.object(
            checks, "_tree_entries", return_value=None
        ):
            result = detect_scope_violations(self.root, baseline, ["app/new.py"])

        self.assertFalse(result["skipped"])
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["repositories"]["repo"]["error"], "git diff failed")
        self.assertIsNone(result["repositories"]["repo/nested"]["error"])

    def test_partial_inspection_failure_with_no_changes_is_incomplete(self):
        other = _init_repo(self.root / "other")
        _write(other / "tool.py", "tool = 1\n")
        _commit(other)
        baseline = capture_task_baseline(self.root)

        real_run_git = checks._run_git

        def fake_run_git(repo, args, timeout=checks.GIT_TIMEOUT):
            if Path(repo).name == "other":
                return None
            return real_run_git(repo, args, timeout=timeout)

        with patch.object(checks, "_run_git", side_effect=fake_run_git):
            result = detect_scope_violations(self.root, baseline, [])

        self.assertFalse(result["skipped"])
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["changed"], [])
        self.assertIsNotNone(result["repositories"]["other"]["error"])
        self.assertIsNone(result["repositories"]["repo"]["error"])

    def test_rev_parse_failure_with_refs_is_not_a_clean_pass(self):
        good = _init_repo(self.root / "good")
        _write(good / "tool.py", "tool = 1\n")
        _commit(good)
        baseline = capture_task_baseline(self.root)

        real_run_git = checks._run_git

        def fake_run_git(repo, args, timeout=checks.GIT_TIMEOUT):
            if Path(repo).name == "repo":
                if args[:2] == ["rev-parse", "HEAD"]:
                    return None
                if args and args[0] == "for-each-ref":
                    return b"refs/heads/main\n"
            return real_run_git(repo, args, timeout=timeout)

        with patch.object(checks, "_run_git", side_effect=fake_run_git):
            result = detect_scope_violations(self.root, baseline, [])

        self.assertFalse(result["skipped"])
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["repositories"]["repo"]["error"], "git rev-parse failed")
        self.assertIsNone(result["repositories"]["good"]["error"])

    def test_baseline_error_skips_comparison_and_reports_incomplete(self):
        good = _init_repo(self.root / "good")
        _write(good / "tool.py", "tool = 1\n")
        _commit(good)

        real_run_git = checks._run_git

        def fake_run_git(repo, args, timeout=checks.GIT_TIMEOUT):
            if Path(repo).name == "repo":
                if args[:2] == ["rev-parse", "HEAD"]:
                    return None
                if args and args[0] == "for-each-ref":
                    return b"refs/heads/main\n"
            return real_run_git(repo, args, timeout=timeout)

        with patch.object(checks, "_run_git", side_effect=fake_run_git):
            baseline = capture_task_baseline(self.root)
        self.assertEqual(_entry(baseline, "repo")["error"], "git rev-parse failed")

        _write(self.repo / "app" / "new.py", "new = 1\n")
        _git(self.repo, "add", "app/new.py")
        _git(self.repo, "commit", "-q", "-m", "add new module")

        result = detect_scope_violations(self.root, baseline, [])
        self.assertFalse(result["skipped"])
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["violations"], [])
        self.assertIn("baseline git rev-parse failed", result["repositories"]["repo"]["error"])
        self.assertIsNone(result["repositories"]["good"]["error"])

    def test_staged_rename_reports_source_path(self):
        _git(self.repo, "mv", "docs/c.md", "app/c.md")
        result = detect_scope_violations(self.root, self.baseline, ["app/*"])

        self.assertIn(("repo", "app/c.md"), _changed(result))
        self.assertIn(("repo", "docs/c.md"), _changed(result))
        source = next(item for item in result["changed"] if item["path"] == "docs/c.md")
        self.assertEqual(source["reason"], "renamed_from")
        self.assertEqual(_violations(result), {("repo", "docs/c.md")})

    def test_deleted_baseline_repository_is_reported(self):
        nested = _init_repo(self.repo / "nested")
        _write(nested / "tool.py", "tool = 1\n")
        _commit(nested)
        baseline = capture_task_baseline(self.root)

        shutil.rmtree(nested)
        result = detect_scope_violations(self.root, baseline, ["app/*.py"])

        self.assertIn(("repo/nested", ""), _changed(result))
        entry = next(item for item in result["changed"] if item["repo"] == "repo/nested")
        self.assertEqual(entry["reason"], "repository_missing")
        self.assertEqual(entry["workspace_path"], "repo/nested")
        self.assertIn(("repo/nested", ""), _violations(result))
        self.assertTrue(result["repositories"]["repo/nested"]["missing"])

    def test_result_is_json_serializable(self):
        _write(self.repo / "docs" / "c.md", "changed\n")
        result = detect_scope_violations(self.root, self.baseline, ["app/*.py"])
        json.dumps(result)


class CheckPythonSyntaxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_files_return_no_errors(self):
        good = self.root / "good.py"
        _write(good, "def ok():\n    return 1\n")
        self.assertEqual(check_python_syntax([good]), [])

    def test_syntax_error_reports_file_line_message(self):
        bad = self.root / "bad.py"
        _write(bad, "value = 1\nif value\n    pass\n")
        errors = check_python_syntax([bad])
        self.assertEqual(len(errors), 1)
        prefix = f"{bad}:2:"
        self.assertTrue(errors[0].startswith(prefix), errors[0])
        self.assertTrue(errors[0][len(prefix):].strip(), errors[0])

    def test_multiple_errors_and_non_python_paths(self):
        first = self.root / "first.py"
        second = self.root / "second.py"
        _write(first, "def broken(:\n    pass\n")
        _write(second, "value = 1\n")
        _write(self.root / "notes.txt", "not python\n")
        errors = check_python_syntax([first, self.root / "notes.txt", self.root / "missing.py", second])
        self.assertEqual(len(errors), 1)
        _write(second, "def other(:\n    pass\n")
        errors = check_python_syntax([first, second])
        self.assertEqual(len(errors), 2)

    def test_no_bytecode_is_written(self):
        good = self.root / "good.py"
        _write(good, "value = 1\n")
        check_python_syntax([good])
        self.assertEqual(list(self.root.rglob("*.pyc")), [])
        self.assertFalse((self.root / "__pycache__").exists())

    def test_recursion_error_is_reported_as_evidence(self):
        good = self.root / "good.py"
        _write(good, "value = 1\n")
        with patch("builtins.compile", side_effect=RecursionError("deep")):
            errors = check_python_syntax([good])

        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(f"{good}:0:RecursionError:"), errors[0])


class RunVerificationCommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _write(self.repo / "tests" / "__init__.py", "")
        venv_bin = self.repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        try:
            os.symlink(sys.executable, venv_bin / "python")
        except OSError as exc:
            self.skipTest(f"cannot create interpreter symlink: {exc}")
        self.python = ".venv/bin/python"

    def tearDown(self):
        self.tmp.cleanup()

    def _add_test_module(self, name: str, body: str) -> None:
        _write(self.repo / "tests" / f"{name}.py", body)

    def test_runs_allowed_unittest(self):
        self._add_test_module(
            "test_ok",
            "import unittest\n\n"
            "class OkTest(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
        )
        before = _pycache_dirs()
        results = run_verification_commands(
            self.repo, [f"{self.python} -m unittest tests.test_ok"], timeout=60
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["allowed"])
        self.assertFalse(results[0]["blocked"])
        self.assertEqual(results[0]["exit_code"], 0)
        self.assertIn("OK", results[0]["stderr"])
        self.assertEqual(_pycache_dirs() - before, set())

    def test_allows_py_compile_and_compileall(self):
        _write(self.repo / "app" / "x.py", "value = 1\n")
        before = _pycache_dirs()
        results = run_verification_commands(
            self.repo,
            [
                f"{self.python} -m py_compile app/x.py",
                f"{self.python} -m compileall app",
            ],
            timeout=60,
        )
        self.assertEqual([item["exit_code"] for item in results], [0, 0])
        self.assertEqual(list(self.repo.rglob("*.pyc")), [])
        self.assertEqual(list(self.repo.rglob("__pycache__")), [])
        self.assertEqual(_pycache_dirs() - before, set())

    def test_bytecode_compilers_blocked_without_safe_cache(self):
        self._add_test_module(
            "test_ok",
            "import unittest\n\n"
            "class OkTest(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        pass\n",
        )
        _write(self.repo / "module.py", "value = 1\n")

        with patch.object(checks, "_make_pycache_dir", return_value=None):
            results = run_verification_commands(
                self.repo,
                [
                    f"{self.python} -m py_compile module.py",
                    f"{self.python} -m compileall .",
                    f"{self.python} -m unittest tests.test_ok",
                ],
                timeout=60,
            )

        self.assertTrue(results[0]["blocked"], results[0])
        self.assertIn("bytecode cache", results[0]["reason"])
        self.assertTrue(results[1]["blocked"], results[1])
        self.assertIn("bytecode cache", results[1]["reason"])
        self.assertTrue(results[2]["allowed"], results[2])
        self.assertEqual(results[2]["exit_code"], 0, results[2]["stderr"])
        self.assertEqual(list(self.repo.rglob("*.pyc")), [])
        self.assertEqual(list(self.repo.rglob("__pycache__")), [])

    def test_rejects_compiler_flags_and_unsafe_paths(self):
        _write(self.repo / "app" / "x.py", "value = 1\n")
        absolute = str(self.repo / "app" / "x.py")
        cases = [
            (f"{self.python} -m compileall --l .", "flags not allowed"),
            (f"{self.python} -m compileall --legacy .", "flags not allowed"),
            (f"{self.python} -m compileall -b .", "flags not allowed"),
            (f"{self.python} -m compileall -qb .", "flags not allowed"),
            (f"{self.python} -m compileall", "requires at least one path"),
            (f"{self.python} -m py_compile -d out.pyc app/x.py", "flags not allowed"),
            (f"{self.python} -m py_compile --dest=out.pyc app/x.py", "flags not allowed"),
            (f"{self.python} -m py_compile -qd out.pyc app/x.py", "flags not allowed"),
            (f"{self.python} -m py_compile {shlex.quote(absolute)}", "absolute compiler path"),
            (f"{self.python} -m py_compile ../outside.py", "may not contain '..'"),
        ]
        commands = [command for command, _reason in cases]
        results = run_verification_commands(self.repo, commands, timeout=5)
        for (command, reason), result in zip(cases, results):
            self.assertTrue(result["blocked"], (command, result))
            self.assertFalse(result["allowed"])
            self.assertIsNone(result["argv"])
            self.assertEqual(result["exit_code"], 126)
            self.assertEqual(result["stdout"], "")
            self.assertIn(reason, result["reason"], result)
        self.assertEqual(list(self.repo.rglob("*.pyc")), [])
        self.assertEqual(list(self.repo.rglob("__pycache__")), [])

    def test_rejects_compiler_symlink_escape(self):
        _write(self.repo / "app" / "x.py", "value = 1\n")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_file = Path(outside.name) / "outside.py"
        _write(outside_file, "outside = 1\n")
        link = self.repo / "link.py"
        try:
            os.symlink(outside_file, link)
        except OSError as exc:
            self.skipTest(f"cannot create symlink: {exc}")

        results = run_verification_commands(
            self.repo, [f"{self.python} -m py_compile link.py"], timeout=5
        )
        self.assertTrue(results[0]["blocked"], results[0])
        self.assertIn("escapes repository", results[0]["reason"])
        self.assertFalse(outside_file.parent.joinpath("__pycache__").exists())
        self.assertEqual(list(self.repo.rglob("*.pyc")), [])
        self.assertEqual(list(self.repo.rglob("__pycache__")), [])

    def test_allows_absolute_interpreter_path(self):
        self._add_test_module(
            "test_ok",
            "import unittest\n\n"
            "class OkTest(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        pass\n",
        )
        command = f"{shlex.quote(sys.executable)} -m unittest tests.test_ok"
        results = run_verification_commands(self.repo, [command], timeout=60)
        self.assertTrue(results[0]["allowed"], results[0])
        self.assertFalse(results[0]["blocked"])
        self.assertEqual(results[0]["exit_code"], 0, results[0]["stderr"])

    def test_rejects_fake_local_interpreter_named_python(self):
        sentinel = self.repo / "executed.txt"
        fake = self.repo / "python"
        fake.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
        fake.chmod(0o755)

        results = run_verification_commands(self.repo, ["./python -m unittest"], timeout=5)
        self.assertTrue(results[0]["blocked"], results[0])
        self.assertFalse(results[0]["allowed"])
        self.assertIn("executable path not allowed", results[0]["reason"])
        self.assertFalse(sentinel.exists())

    def test_rejects_missing_interpreter_path(self):
        results = run_verification_commands(
            self.repo, ["./missing-dir/python -m unittest"], timeout=5
        )
        self.assertTrue(results[0]["blocked"], results[0])
        self.assertIn("executable path not allowed", results[0]["reason"])

    def test_rejects_parent_relative_and_absolute_untrusted_interpreter(self):
        sentinel = self.repo / "executed.txt"
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        untrusted = Path(outside.name) / "python"
        untrusted.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
        untrusted.chmod(0o755)
        relative = os.path.relpath(untrusted, self.repo)
        self.assertTrue(relative.startswith(".."))

        commands = [
            f"{shlex.quote(relative)} -m unittest",
            f"{shlex.quote(str(untrusted))} -m unittest",
        ]
        results = run_verification_commands(self.repo, commands, timeout=5)
        for result in results:
            self.assertTrue(result["blocked"], result)
            self.assertFalse(result["allowed"])
            self.assertIn("executable path not allowed", result["reason"])
        self.assertFalse(sentinel.exists())

    def test_rejects_repo_local_symlink_to_untrusted_binary(self):
        sentinel = self.repo / "executed.txt"
        target = self.repo / "untrusted-python"
        target.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
        target.chmod(0o755)
        link = self.repo / "tools" / "python"
        link.parent.mkdir(parents=True)
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"cannot create symlink: {exc}")

        results = run_verification_commands(
            self.repo, ["tools/python -m unittest"], timeout=5
        )
        self.assertTrue(results[0]["blocked"], results[0])
        self.assertFalse(results[0]["allowed"])
        self.assertIn("executable path not allowed", results[0]["reason"])
        self.assertFalse(sentinel.exists())

    def test_pycache_dir_stays_outside_workspace_when_tempdir_is_inside(self):
        inside = self.repo / "tmp"
        inside.mkdir()
        with patch.object(checks.tempfile, "gettempdir", return_value=str(inside)):
            cache = checks._make_pycache_dir(self.repo)

        self.assertIsNotNone(cache)
        self.addCleanup(shutil.rmtree, cache, ignore_errors=True)
        cache_path = Path(cache).resolve(strict=False)
        workspace = self.repo.resolve(strict=False)
        self.assertFalse(cache_path == workspace or cache_path.is_relative_to(workspace))

    def test_pycache_dir_failure_does_not_raise_or_set_prefix(self):
        with patch.object(checks.tempfile, "mkdtemp", side_effect=OSError("boom")):
            cache = checks._make_pycache_dir(self.repo)
            self.assertIsNone(cache)
            results = run_verification_commands(
                self.repo, ["bash -m unittest"], timeout=5
            )
        self.assertTrue(results[0]["blocked"])

    def test_rejects_shell_metacharacters_before_execution(self):
        sentinel = self.repo / "sentinel.txt"
        commands = [
            f"{self.python} -m unittest; touch {sentinel}",
            f"{self.python} -m unittest | cat",
            f"{self.python} -m unittest && touch {sentinel}",
            f"{self.python} -m unittest > {sentinel}",
            f"{self.python} -m unittest < input.txt",
            f"{self.python} -m unittest `touch {sentinel}`",
            f"{self.python} -m unittest $(touch {sentinel})",
        ]
        results = run_verification_commands(self.repo, commands, timeout=5)
        self.assertEqual(len(results), len(commands))
        for result in results:
            self.assertFalse(result["allowed"], result)
            self.assertTrue(result["blocked"])
            self.assertEqual(result["exit_code"], 126)
            self.assertIsNone(result["argv"])
        self.assertFalse(sentinel.exists())

    def test_rejects_other_executables_and_modules(self):
        commands = [
            "bash -m unittest",
            "/bin/sh -m unittest",
            "git status",
            "python -c 'print(1)'",
            "python -m http.server",
            "python -m unittests",
            "python unittest",
            "python3",
            "",
        ]
        before = _pycache_dirs()
        results = run_verification_commands(self.repo, commands, timeout=5)
        for result in results:
            self.assertFalse(result["allowed"], result)
            self.assertTrue(result["blocked"])
            self.assertIsNotNone(result["reason"])
            self.assertEqual(result["stdout"], "")
        self.assertEqual(_pycache_dirs() - before, set())

    def test_timeout_is_enforced(self):
        self._add_test_module(
            "test_slow",
            "import time\nimport unittest\n\n"
            "class SlowTest(unittest.TestCase):\n"
            "    def test_slow(self):\n"
            "        time.sleep(10)\n",
        )
        before = _pycache_dirs()
        results = run_verification_commands(
            self.repo, [f"{self.python} -m unittest tests.test_slow"], timeout=1
        )
        self.assertTrue(results[0]["timed_out"])
        self.assertEqual(results[0]["exit_code"], 124)
        self.assertEqual(_pycache_dirs() - before, set())

    def test_output_is_truncated(self):
        self._add_test_module(
            "test_noisy",
            "import unittest\n\n"
            "class NoisyTest(unittest.TestCase):\n"
            "    def test_noisy(self):\n"
            "        print('x' * 20000)\n",
        )
        results = run_verification_commands(
            self.repo, [f"{self.python} -m unittest tests.test_noisy"], timeout=60
        )
        self.assertTrue(results[0]["truncated"])
        self.assertLessEqual(len(results[0]["stdout"]), MAX_OUTPUT_BYTES)
        self.assertLessEqual(len(results[0]["stderr"]), MAX_OUTPUT_BYTES)

    def test_sanitized_environment_and_no_bytecode(self):
        self._add_test_module(
            "test_env",
            "import os\nimport unittest\n\n"
            "class EnvTest(unittest.TestCase):\n"
            "    def test_env(self):\n"
            "        self.assertEqual(os.environ.get('PYTHONDONTWRITEBYTECODE'), '1')\n"
            "        self.assertNotIn('SECRET_TOKEN', os.environ)\n"
            "        self.assertNotIn('HOME', os.environ)\n"
            "        self.assertNotIn('PYTHONPATH', os.environ)\n",
        )
        os.environ["SECRET_TOKEN"] = "leak-me"
        os.environ["PYTHONPATH"] = "/nonexistent-qa-path"
        self.addCleanup(os.environ.pop, "SECRET_TOKEN", None)
        self.addCleanup(os.environ.pop, "PYTHONPATH", None)

        results = run_verification_commands(
            self.repo, [f"{self.python} -m unittest tests.test_env"], timeout=60
        )
        self.assertEqual(results[0]["exit_code"], 0, results[0]["stderr"])
        self.assertEqual(list(self.repo.rglob("*.pyc")), [])

    def test_invalid_repository_root_is_blocked(self):
        results = run_verification_commands(
            self.repo / "missing", [f"{self.python} -m unittest"], timeout=5
        )
        self.assertTrue(results[0]["blocked"])
        self.assertIn("invalid repository root", results[0]["reason"])

    def test_commands_continue_after_a_blocked_command(self):
        self._add_test_module(
            "test_ok",
            "import unittest\n\n"
            "class OkTest(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        pass\n",
        )
        results = run_verification_commands(
            self.repo,
            ["bash -m unittest", f"{self.python} -m unittest tests.test_ok"],
            timeout=60,
        )
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["blocked"])
        self.assertTrue(results[1]["allowed"])
        self.assertEqual(results[1]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
