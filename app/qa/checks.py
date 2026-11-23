"""Deterministic, read-only QA verification primitives.

The helpers in this module are intentionally independent from the graph and
runtime so that the QA node can reuse them later. Everything here uses the
Python standard library only and never writes to the workspace:

* :func:`discover_git_repos` finds bounded git repositories.
* :func:`capture_task_baseline` snapshots HEAD and dirty file hashes at task
  start (no writes; git optional locks are disabled).
* :func:`detect_scope_violations` compares the workspace against a baseline and
  the task's ``target_repo_files_hint`` scope.
* :func:`check_python_syntax` compiles changed Python files in-process.
* :func:`run_verification_commands` runs an allowlisted subset of ``python -m``
  commands with a sanitized environment, a timeout and capped output.

Residual risk: filesystem and git checks are not atomic (TOCTOU); a file or
symlink can change between discovery, hashing and execution. Results are
best-effort evidence for QA, not a security boundary on their own.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path
from typing import Any

#: Directories that are never walked while looking for repositories.
EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        ".agents",
        "__pycache__",
        "dist",
        "build",
    }
)

#: Official bounds: depth counts path segments below the workspace root.
MAX_DISCOVERY_DEPTH = 4
MAX_DISCOVERY_REPOS = 50

#: Files larger than this are fingerprinted with stat metadata instead of a
#: content hash so capture stays bounded.
MAX_HASH_BYTES = 8 * 1024 * 1024

#: Bound for listing every file of a brand new repository.
MAX_NEW_REPO_FILES = 2000

#: Per-stream cap for captured verification output.
MAX_OUTPUT_BYTES = 8 * 1024

#: Per git invocation timeout (seconds).
GIT_TIMEOUT = 30

#: Per verification command timeout (seconds) unless the caller overrides it.
DEFAULT_COMMAND_TIMEOUT = 300

ALLOWED_EXECUTABLE_BASENAMES = frozenset({"python", "python3"})
ALLOWED_MODULES = frozenset({"unittest", "py_compile", "compileall"})
BYTECODE_MODULES = frozenset({"py_compile", "compileall"})
SHELL_METACHARACTERS = (";", "|", "&", ">", "<", "`", "$(", "\n", "\r", "\x00")

_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _sanitized_env() -> dict[str, str]:
    """Return a deterministic environment with no HOME or task-supplied values."""
    env: dict[str, str] = {"PATH": os.environ.get("PATH") or os.defpath}
    for key in ("LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _git_env() -> dict[str, str]:
    env = _sanitized_env()
    # Read-only and non-interactive: never refresh the index, never prompt and
    # ignore host-level git configuration.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _run_git(repo: Path, args: list[str], timeout: int = GIT_TIMEOUT) -> bytes | None:
    """Run git read-only and return raw stdout, or ``None`` on failure."""
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            shell=False,
            cwd=str(repo),
            env=_git_env(),
            capture_output=True,
            text=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or b""


def _git_head_status(repo: Path) -> tuple[str | None, bool]:
    """Return ``(head, failed)`` for a repository.

    ``failed`` is set only when ``rev-parse HEAD`` cannot be read while refs
    exist, i.e. inspection is broken rather than the repository being unborn.
    A repository with no refs at all is genuinely commitless, not a failure.
    """
    raw = _run_git(repo, ["rev-parse", "HEAD"])
    if raw is not None:
        head = raw.decode("ascii", "replace").strip()
        if _SHA_RE.fullmatch(head):
            return head, False
        return None, True
    refs = _run_git(repo, ["for-each-ref", "--count=1", "--format=%(refname)"])
    if refs is None:
        return None, True
    return None, bool(refs.strip())


def _split_z(raw: bytes) -> list[str]:
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0")]


def _status_entries(repo: Path) -> list[tuple[str, str]] | None:
    """Return ``(status, path)`` pairs from ``git status --porcelain -z``.

    Staged rename sources are appended as ``R_FROM`` entries so that the old
    path is snapshotted and checked against the task hints. Copy sources are
    not part of the change and are skipped.
    """
    raw = _run_git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if raw is None:
        return None
    tokens = raw.split(b"\0")
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        record = tokens[i]
        i += 1
        if len(record) < 3:
            continue
        status = record[:2].decode("ascii", "replace")
        path = record[3:].decode("utf-8", "replace")
        source = ""
        if "R" in status or "C" in status:
            # -z renames/copies are followed by the original path.
            if i < len(tokens):
                source = tokens[i].decode("utf-8", "replace")
            i += 1
        if not path or path.endswith("/"):
            # Nested repositories/submodules are reported as a directory entry;
            # discover_git_repos() tracks their files separately.
            continue
        entries.append((status, path))
        if source and "R" in status and not source.endswith("/"):
            entries.append(("R_FROM", source))
    return entries


def _diff_entries(repo: Path, base: str, head: str) -> list[tuple[str, str]] | None:
    """Return ``(status, path)`` pairs for a committed diff (renames split)."""
    if not (_SHA_RE.fullmatch(base or "") and _SHA_RE.fullmatch(head or "")):
        return None
    raw = _run_git(repo, ["diff", "--name-status", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base, head])
    if raw is None:
        return None
    tokens = _split_z(raw)
    entries: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(tokens):
        status = tokens[i].strip()
        path = tokens[i + 1]
        i += 2
        if status and path:
            entries.append((status, path))
    return entries


def _tree_entries(repo: Path, ref: str) -> list[str] | None:
    if not _SHA_RE.fullmatch(ref or ""):
        return None
    raw = _run_git(repo, ["ls-tree", "-r", "--name-only", "-z", ref])
    if raw is None:
        return None
    return [path for path in _split_z(raw) if path]


def _list_repo_files(repo: Path) -> list[str] | None:
    """Tracked plus untracked (non-ignored) files of a repository."""
    raw = _run_git(repo, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if raw is None:
        return None
    return sorted({path for path in _split_z(raw) if path})


# ---------------------------------------------------------------------------
# Repository discovery
# ---------------------------------------------------------------------------


def _within(root: Path, path: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False


def discover_git_repos(
    workspace_root: Path | str,
    max_depth: int = MAX_DISCOVERY_DEPTH,
    max_repos: int = MAX_DISCOVERY_REPOS,
) -> list[str]:
    """Return sorted workspace-relative git repository roots.

    The workspace root repository is reported as ``"."``. ``max_depth`` counts
    path segments below the workspace root (a repository at ``a/b`` has depth
    2); deeper directories are not walked. ``max_repos`` caps the result.
    Symlinks are never followed or reported, and excluded directories
    (``EXCLUDE_DIRS``) are pruned. The walk never leaves ``workspace_root``.
    """
    try:
        root = Path(workspace_root).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return []
    if not root.is_dir():
        return []

    max_depth = max(0, int(max_depth))
    max_repos = max(1, int(max_repos))

    root_git = root / ".git"
    repos: list[str] = []
    if root_git.exists() and not root_git.is_symlink():
        repos.append(".")

    try:
        for dirpath, dirnames, _ in os.walk(root, followlinks=False):
            current = Path(dirpath)
            try:
                rel = current.relative_to(root).as_posix()
            except ValueError:
                dirnames[:] = []
                continue
            depth = 0 if rel == "." else len([p for p in rel.split("/") if p])

            if rel != "." and _within(root, current):
                git_entry = current / ".git"
                try:
                    if git_entry.exists() and not git_entry.is_symlink() and rel not in repos:
                        repos.append(rel)
                        if len(repos) >= max_repos:
                            break
                except OSError:
                    pass

            kept: list[str] = []
            if depth < max_depth:
                for name in sorted(dirnames):
                    if name in EXCLUDE_DIRS:
                        continue
                    child = current / name
                    try:
                        if child.is_symlink() or not _within(root, child):
                            continue
                    except OSError:
                        continue
                    kept.append(name)
            dirnames[:] = kept
    except (OSError, RuntimeError):
        pass

    return sorted(repos[:max_repos])


# ---------------------------------------------------------------------------
# Baseline snapshots
# ---------------------------------------------------------------------------


def _repo_child(repo_abs: Path, rel_path: str) -> Path | None:
    parts = [part for part in rel_path.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        return None
    return repo_abs.joinpath(*parts)


def _snapshot_path(path: Path) -> dict[str, Any]:
    """Fingerprint a path using a content hash with a bounded stat fallback."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {"kind": "deleted"}
    except OSError:
        return {"kind": "unreadable"}

    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            return {"kind": "unreadable"}
        data = target.encode("utf-8", "surrogateescape")
        return {"kind": "sha256", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_HASH_BYTES:
        return {"kind": "stat", "size": st.st_size, "mtime_ns": st.st_mtime_ns}

    digest = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_HASH_BYTES:
                    return {"kind": "stat", "size": st.st_size, "mtime_ns": st.st_mtime_ns}
                digest.update(chunk)
    except OSError:
        return {"kind": "unreadable"}
    return {"kind": "sha256", "sha256": digest.hexdigest(), "size": total}


def _same_snapshot(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if left.get("kind") != right.get("kind"):
        return False
    kind = left.get("kind")
    if kind == "sha256":
        return left.get("sha256") == right.get("sha256") and left.get("size") == right.get("size")
    if kind == "stat":
        return left.get("size") == right.get("size") and left.get("mtime_ns") == right.get("mtime_ns")
    # "deleted" and "unreadable" have no further state to compare.
    return True


def _dirty_snapshot(repo_abs: Path, entries: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for _status, rel_path in entries:
        child = _repo_child(repo_abs, rel_path)
        snapshots[rel_path] = _snapshot_path(child) if child is not None else {"kind": "unreadable"}
    return {path: snapshots[path] for path in sorted(snapshots)}


def capture_task_baseline(workspace_root: Path | str) -> dict[str, Any]:
    """Capture JSON-serializable repo state at task start without writing.

    Each repository entry contains its ``root`` (relative), ``head`` (or
    ``None`` when the repository has no commit), and ``dirty`` snapshots for
    tracked/untracked files reported by ``git status``. Files larger than
    ``MAX_HASH_BYTES`` use ``{"kind": "stat", ...}``; deleted files use
    ``{"kind": "deleted"}``.
    """
    try:
        root = Path(workspace_root).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return _baseline_skipped(workspace_root, "invalid workspace root")
    if not root.is_dir():
        return _baseline_skipped(root, "invalid workspace root")

    repos: list[dict[str, Any]] = []
    inspected = 0
    for rel_root in discover_git_repos(root):
        repo_abs = root if rel_root == "." else root / rel_root
        head, head_failed = _git_head_status(repo_abs)
        entries = _status_entries(repo_abs)
        if entries is None or head_failed:
            error = "git status failed" if entries is None else "git rev-parse failed"
            repos.append(
                {
                    "root": rel_root,
                    "head": head,
                    "dirty": {},
                    "error": error,
                }
            )
            continue
        inspected += 1
        repos.append(
            {
                "root": rel_root,
                "head": head,
                "dirty": _dirty_snapshot(repo_abs, entries),
                "error": None,
            }
        )

    if not repos:
        return _baseline_skipped(root, "no git repositories found")
    if inspected == 0:
        result = _baseline_skipped(root, "git inspection failed for every repository", incomplete=True)
        result["repos"] = repos
        return result

    return {
        "version": 1,
        "workspace_root": str(root),
        "skipped": False,
        "incomplete": inspected < len(repos),
        "reason": None,
        "repos": repos,
    }


def _baseline_skipped(workspace_root: Path | str, reason: str, incomplete: bool = False) -> dict[str, Any]:
    return {
        "version": 1,
        "workspace_root": str(workspace_root),
        "skipped": True,
        "incomplete": incomplete,
        "reason": reason,
        "repos": [],
    }


# ---------------------------------------------------------------------------
# Scope matching and violation detection
# ---------------------------------------------------------------------------


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate ``*`` (single segment) and ``**`` (multi segment) globs."""
    out: list[str] = []
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        if char == "*":
            if i + 1 < length and pattern[i + 1] == "*":
                j = i + 2
                while j < length and pattern[j] == "*":
                    j += 1
                if j < length and pattern[j] == "/":
                    out.append("(?:[^/]+/)*")
                    j += 1
                else:
                    out.append(".*")
                i = j
            else:
                out.append("[^/]*")
                i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _prepare_hints(hints: Any, workspace_root: Path) -> list[tuple[str, Any, str]]:
    """Normalize hints into ``(kind, value, label)`` matchers."""
    if hints is None:
        return []
    if isinstance(hints, str):
        raw_hints: list[Any] = [hints]
    elif isinstance(hints, (list, tuple, set, frozenset)):
        raw_hints = list(hints)
    else:
        raw_hints = [hints]

    prepared: list[tuple[str, Any, str]] = []
    for raw in raw_hints:
        hint = str(raw).strip().replace("\\", "/")
        if not hint:
            continue
        if hint.startswith("/") or re.match(r"^[A-Za-z]:/", hint):
            try:
                hint = Path(hint).resolve(strict=False).relative_to(workspace_root).as_posix()
            except (OSError, RuntimeError, ValueError):
                continue
        while hint.startswith("./"):
            hint = hint[2:]
        if hint == ".":
            prepared.append(("prefix", "", "."))
            continue
        if not hint or hint == "/":
            continue
        if hint.endswith("/"):
            prepared.append(("prefix", hint.rstrip("/"), hint))
        elif "*" in hint or "?" in hint:
            prepared.append(("glob", _glob_to_regex(hint), hint))
        else:
            prepared.append(("exact", hint, hint))
    return prepared


def _matches_hint(prepared: list[tuple[str, Any, str]], candidates: list[str]) -> bool:
    for kind, value, _label in prepared:
        for candidate in candidates:
            if kind == "exact":
                if candidate == value:
                    return True
            elif kind == "prefix":
                if value == "" or candidate == value or candidate.startswith(value + "/"):
                    return True
            elif kind == "glob":
                if value.match(candidate):
                    return True
    return False


def _workspace_path(repo: str, path: str) -> str:
    return path if repo == "." else f"{repo}/{path}"


def _worktree_reason(status: str, previous: Any) -> str:
    if status == "R_FROM":
        return "renamed_from"
    if "D" in status:
        return "deleted"
    if "R" in status:
        return "renamed"
    if status == "??" or "A" in status:
        return "modified" if isinstance(previous, dict) else "added"
    return "modified"


def _commit_reason(status: str) -> str:
    code = status[:1]
    if code == "A":
        return "committed_added"
    if code == "D":
        return "committed_deleted"
    if code in ("M", "T"):
        return "committed_modified"
    return "committed_changed"


def _bounded(paths: list[str], limit: int = MAX_NEW_REPO_FILES) -> tuple[list[str], bool]:
    ordered = sorted(set(paths))
    return ordered[:limit], len(ordered) > limit


def _skipped_result(
    workspace_root: Path | str,
    hints: list[tuple[str, Any, str]],
    reason: str,
    incomplete: bool = False,
) -> dict[str, Any]:
    return {
        "workspace_root": str(workspace_root),
        "skipped": True,
        "incomplete": incomplete,
        "reason": reason,
        "hints": [label for _kind, _value, label in hints],
        "changed": [],
        "violations": [],
        "repositories": {},
        "truncated": False,
    }


def _baseline_repos(baseline: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    repos = baseline.get("repos") if isinstance(baseline, dict) else None
    if isinstance(repos, list):
        for entry in repos:
            if isinstance(entry, dict):
                root = entry.get("root")
                if isinstance(root, str):
                    mapping[root] = entry
    return mapping


def detect_scope_violations(
    workspace_root: Path | str,
    baseline: dict[str, Any] | None,
    hints: Any,
) -> dict[str, Any]:
    """Compare the current workspace to a baseline and the task's file hints.

    Returns ``changed`` entries (``repo``, ``path``, ``workspace_path``,
    ``reason``), ``violations`` for changes not covered by ``hints``, and a
    per-repository summary. An empty hint list means every change is a
    violation. A workspace with no git repositories, a missing baseline, or a
    baseline whose repositories could not be inspected returns ``skipped=True``
    with an explicit ``reason``. Partial inspection failures set
    ``incomplete=True`` so a caller cannot mistake the result for a clean pass.
    """
    try:
        root = Path(workspace_root).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return _skipped_result(workspace_root, [], "invalid workspace root", incomplete=True)
    if not root.is_dir():
        return _skipped_result(root, [], "invalid workspace root", incomplete=True)

    if not isinstance(baseline, dict):
        return _skipped_result(root, [], "no baseline available", incomplete=True)

    prepared_hints = _prepare_hints(hints, root)
    baseline_map = _baseline_repos(baseline)
    current_repos = discover_git_repos(root)
    if not current_repos and not baseline_map:
        return _skipped_result(root, prepared_hints, "no git repositories found")

    changed: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    repositories: dict[str, Any] = {}
    any_truncated = False

    # Baseline roots and discovered roots are both relevant: a repository that
    # disappeared during the task leaves no current directory to inspect.
    for rel_root in sorted(set(current_repos) | set(baseline_map)):
        exists_now = rel_root in current_repos
        repo_abs = root if rel_root == "." else root / rel_root
        base_entry = baseline_map.get(rel_root)
        changes: dict[str, str] = {}
        repo_truncated = False
        repo_error: str | None = None
        head_now: str | None = None

        if not exists_now:
            changes[""] = "repository_missing"
        else:
            head_now, head_failed = _git_head_status(repo_abs)
            baseline_error = base_entry.get("error") if base_entry is not None else None
            if baseline_error:
                repo_error = f"baseline {baseline_error}"
            if head_failed:
                repo_error = repo_error or "git rev-parse failed"

            if base_entry is not None and baseline_error:
                # The baseline is unreliable; comparing against it would
                # fabricate changes. Keep the error so the result is incomplete.
                pass
            elif base_entry is None:
                files = _list_repo_files(repo_abs)
                if files is None:
                    repo_error = repo_error or "git ls-files failed"
                    files = []
                listed, repo_truncated = _bounded(files)
                for path in listed:
                    changes.setdefault(path, "new_repository")
            else:
                base_head = base_entry.get("head")
                base_head = base_head if isinstance(base_head, str) and _SHA_RE.fullmatch(base_head) else None

                if not head_failed and base_head != head_now:
                    if base_head is None and head_now is not None:
                        committed = _tree_entries(repo_abs, head_now)
                        if committed is None:
                            repo_error = repo_error or "git ls-tree failed"
                        else:
                            for path in committed:
                                changes.setdefault(path, "committed_added")
                    elif base_head is not None and head_now is not None:
                        committed = _diff_entries(repo_abs, base_head, head_now)
                        if committed is None:
                            repo_error = repo_error or "git diff failed"
                            # History rewritten; be conservative and list HEAD.
                            committed_paths = _tree_entries(repo_abs, head_now)
                            if committed_paths is None:
                                repo_error = repo_error or "git ls-tree failed"
                            else:
                                for path in committed_paths:
                                    changes.setdefault(path, "committed_changed")
                        else:
                            for status, path in committed:
                                changes.setdefault(path, _commit_reason(status))
                    elif base_head is not None and head_now is None:
                        files = _list_repo_files(repo_abs)
                        if files is None:
                            repo_error = repo_error or "git ls-files failed"
                            files = []
                        listed, repo_truncated = _bounded(files)
                        for path in listed:
                            changes.setdefault(path, "history_reset")

                baseline_dirty = base_entry.get("dirty")
                if not isinstance(baseline_dirty, dict):
                    baseline_dirty = {}
                entries = _status_entries(repo_abs)
                if entries is None:
                    repo_error = repo_error or "git status failed"
                    entries = []

                current_paths: set[str] = set()
                for status, path in entries:
                    current_paths.add(path)
                    child = _repo_child(repo_abs, path)
                    snapshot = _snapshot_path(child) if child is not None else {"kind": "unreadable"}
                    previous = baseline_dirty.get(path)
                    if previous is None or not _same_snapshot(previous, snapshot):
                        changes.setdefault(path, _worktree_reason(status, previous))

                for path in sorted(baseline_dirty):
                    if path in current_paths or path in changes:
                        continue
                    child = _repo_child(repo_abs, path)
                    snapshot = _snapshot_path(child) if child is not None else {"kind": "unreadable"}
                    previous = baseline_dirty.get(path)
                    if not _same_snapshot(previous, snapshot):
                        reason = (
                            "restored"
                            if isinstance(previous, dict) and previous.get("kind") == "deleted"
                            else "modified"
                        )
                        changes.setdefault(path, reason)

        repo_changed: list[str] = []
        repo_violations: list[str] = []
        for path in sorted(changes):
            reason = changes[path]
            workspace_path = rel_root if path == "" else _workspace_path(rel_root, path)
            change_entry = {
                "repo": rel_root,
                "path": path,
                "workspace_path": workspace_path,
                "reason": reason,
            }
            changed.append(change_entry)
            repo_changed.append(path)
            candidates = [workspace_path] if path == "" else [path, workspace_path]
            if not _matches_hint(prepared_hints, candidates):
                violations.append(
                    {
                        "repo": rel_root,
                        "path": path,
                        "workspace_path": workspace_path,
                        "reason": "out_of_scope",
                        "change": reason,
                    }
                )
                repo_violations.append(path)

        repositories[rel_root] = {
            "head": head_now,
            "new_repository": exists_now and base_entry is None,
            "missing": not exists_now,
            "changed": repo_changed,
            "violations": repo_violations,
            "truncated": repo_truncated,
            "error": repo_error,
        }
        any_truncated = any_truncated or repo_truncated

    incomplete = any(info["error"] for info in repositories.values())
    if not changed and repositories and all(info["error"] for info in repositories.values()):
        result = _skipped_result(root, prepared_hints, "git inspection failed for every repository", incomplete=True)
        result["repositories"] = repositories
        return result

    return {
        "workspace_root": str(root),
        "skipped": False,
        "incomplete": incomplete,
        "reason": None,
        "hints": [label for _kind, _value, label in prepared_hints],
        "changed": changed,
        "violations": violations,
        "repositories": repositories,
        "truncated": any_truncated,
    }


# ---------------------------------------------------------------------------
# Python syntax checks
# ---------------------------------------------------------------------------


def _format_syntax_error(path: Path, exc: BaseException) -> str:
    lineno = getattr(exc, "lineno", None) or 0
    message = getattr(exc, "msg", None) or str(exc) or "invalid syntax"
    return f"{path}:{lineno}:{message}"


def check_python_syntax(paths: Any) -> list[str]:
    """Compile ``*.py`` paths in-process and return ``file:line:message`` errors.

    Only Python files are inspected; deleted or non-Python paths are ignored.
    ``compile()`` is used directly, so no ``.pyc`` files are written.
    """
    if isinstance(paths, (str, os.PathLike)):
        candidates: list[Any] = [paths]
    else:
        candidates = list(paths or [])

    errors: list[str] = []
    for raw in candidates:
        path = Path(raw)
        if path.suffix != ".py":
            continue
        try:
            if not path.is_file():
                continue
            with tokenize.open(str(path)) as handle:
                source = handle.read()
        except SyntaxError as exc:
            errors.append(_format_syntax_error(path, exc))
            continue
        except (RecursionError, MemoryError) as exc:
            errors.append(f"{path}:0:{type(exc).__name__}: {exc}")
            continue
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{path}:0:{type(exc).__name__}: {exc}")
            continue
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            errors.append(_format_syntax_error(path, exc))
        except (RecursionError, MemoryError) as exc:
            errors.append(f"{path}:0:{type(exc).__name__}: {exc}")
        except (TypeError, ValueError) as exc:
            errors.append(f"{path}:0:{type(exc).__name__}: {exc}")
    return errors


# ---------------------------------------------------------------------------
# Allowlisted verification commands
# ---------------------------------------------------------------------------


def _blocked_result(command: Any, reason: str) -> dict[str, Any]:
    return {
        "command": "" if command is None else str(command),
        "argv": None,
        "allowed": False,
        "blocked": True,
        "reason": reason,
        "exit_code": 126,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "truncated": False,
    }


def _has_path_separator(value: str) -> bool:
    separators = {os.sep, "/", "\\"}
    if os.altsep:
        separators.add(os.altsep)
    return any(sep in value for sep in separators)


def _allowed_executable_path(executable: str, repo_root: Path) -> bool:
    """Resolve a path-qualified interpreter and require it to be trusted."""
    candidate = Path(executable)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        if not sys.executable:
            return False
        interpreter = Path(sys.executable).resolve(strict=True)
        if resolved == interpreter:
            return True
        prefix = Path(sys.prefix).resolve(strict=True)
        return resolved.is_relative_to(prefix)
    except (OSError, RuntimeError, ValueError):
        return False


_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _compiler_arguments_reason(args: list[str], repo_root: Path) -> str | None:
    """Validate compiler arguments under a conservative policy.

    Compiler flags are unsupported: any argument beginning with ``-`` is
    rejected, including argparse abbreviations such as ``--l``. At least one
    positional path is required, and every path must be relative, free of
    ``..`` and resolve inside ``repo_root`` (so symlinks cannot escape).
    """
    if not args:
        return "compiler command requires at least one path"
    try:
        root = repo_root.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return "cannot resolve repository root"
    for arg in args:
        if arg.startswith("-"):
            return f"compiler flags not allowed: {arg}"
        if not arg:
            return "empty compiler path"
        path = Path(arg)
        if path.is_absolute() or _DRIVE_PATH_RE.match(arg):
            return f"absolute compiler path not allowed: {arg}"
        if ".." in path.parts:
            return f"compiler path may not contain '..': {arg}"
        try:
            resolved = (root / path).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return f"cannot resolve compiler path: {arg}"
        if resolved != root and not resolved.is_relative_to(root):
            return f"compiler path escapes repository: {arg}"
    return None


def _validate_command(command: Any, repo_root: Path) -> tuple[list[str] | None, str | None]:
    """Return ``(argv, None)`` or ``(None, reason)`` without executing anything."""
    if not isinstance(command, str) or not command.strip():
        return None, "empty command"
    for metachar in SHELL_METACHARACTERS:
        if metachar in command:
            return None, f"shell metacharacter not allowed: {metachar!r}"
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return None, f"cannot parse command: {exc}"
    if not argv:
        return None, "empty command"

    executable = argv[0]
    if os.path.basename(executable) not in ALLOWED_EXECUTABLE_BASENAMES:
        return None, f"executable not allowed: {executable}"
    if _has_path_separator(executable) and not _allowed_executable_path(executable, repo_root):
        return None, f"executable path not allowed: {executable}"
    if len(argv) < 3 or argv[1] != "-m":
        return None, "only 'python -m <module>' commands are allowed"
    if argv[2] not in ALLOWED_MODULES:
        return None, f"module not allowed: {argv[2]}"
    if argv[2] in BYTECODE_MODULES:
        reason = _compiler_arguments_reason(argv[3:], repo_root)
        if reason is not None:
            return None, reason
    return argv, None


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_BYTES:
        return text, False
    return text[:MAX_OUTPUT_BYTES], True


def _decode_output(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def _run_command(argv: list[str], cwd: Path, timeout: int, env: dict[str, str]) -> dict[str, Any]:
    stdout = ""
    stderr = ""
    exit_code = 1
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _decode_output(exc.stdout)
        stderr = _decode_output(exc.stderr)
        exit_code = 124
    except FileNotFoundError:
        exit_code = 127
        stderr = f"command not found: {argv[0]}"
    except OSError as exc:
        stderr = f"ERROR: {exc}"

    stdout, stdout_truncated = _truncate(stdout)
    stderr, stderr_truncated = _truncate(stderr)
    return {
        "argv": argv,
        "allowed": True,
        "blocked": False,
        "reason": None,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": stdout_truncated or stderr_truncated,
    }


def _make_pycache_dir(workspace_root: Path) -> str | None:
    """Create a bytecode cache directory outside the workspace, or ``None``.

    A bytecode cache must never land inside the workspace. When no verifiably
    external, writable location can be created, return ``None`` so the caller
    leaves ``PYTHONPYCACHEPREFIX`` unset instead of raising.
    """
    base = Path(tempfile.gettempdir())
    try:
        resolved_ws = workspace_root.resolve(strict=False)
        base = base.resolve(strict=False)
        if base == resolved_ws or base.is_relative_to(resolved_ws):
            base = resolved_ws.parent
        if base == resolved_ws or base.is_relative_to(resolved_ws):
            return None
        cache = Path(tempfile.mkdtemp(prefix="qa-pycache-", dir=str(base)))
        resolved_cache = cache.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved_cache == resolved_ws or resolved_cache.is_relative_to(resolved_ws):
        shutil.rmtree(cache, ignore_errors=True)
        return None
    return str(cache)


def run_verification_commands(
    repo_root: Path | str,
    commands: Any,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> list[dict[str, Any]]:
    """Run allowlisted ``python -m`` commands in ``repo_root``.

    Only ``python``/``python3`` with ``-m unittest``, ``-m py_compile`` or
    ``-m compileall`` is accepted (including venv paths such as
    ``.venv/bin/python``). Commands with shell metacharacters are rejected
    before execution. Each command runs with ``shell=False``, a sanitized
    ``PYTHONDONTWRITEBYTECODE=1`` environment, the given timeout and capped
    output.
    """
    try:
        root = Path(repo_root).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        root = Path(repo_root)
    valid_root = root.is_dir()

    try:
        timeout_s = max(1, int(timeout))
    except (TypeError, ValueError):
        timeout_s = DEFAULT_COMMAND_TIMEOUT

    if isinstance(commands, str):
        command_list: list[Any] = [commands]
    else:
        command_list = list(commands or [])

    results: list[dict[str, Any]] = []
    pycache_dir: str | None = None
    env = _sanitized_env()
    try:
        if valid_root:
            pycache_dir = _make_pycache_dir(root)
            if pycache_dir is not None:
                env["PYTHONPYCACHEPREFIX"] = pycache_dir
        for command in command_list:
            if not valid_root:
                results.append(_blocked_result(command, f"invalid repository root: {repo_root}"))
                continue
            argv, reason = _validate_command(command, root)
            if argv is None:
                results.append(_blocked_result(command, reason or "command not allowed"))
                continue
            if argv[2] in BYTECODE_MODULES and pycache_dir is None:
                results.append(
                    _blocked_result(
                        command,
                        "no safe bytecode cache directory; refusing bytecode compiler",
                    )
                )
                continue
            result = _run_command(argv, root, timeout_s, env)
            results.append({"command": command, **result})
    finally:
        if pycache_dir is not None:
            shutil.rmtree(pycache_dir, ignore_errors=True)
    return results


__all__ = [
    "DEFAULT_COMMAND_TIMEOUT",
    "MAX_HASH_BYTES",
    "MAX_OUTPUT_BYTES",
    "capture_task_baseline",
    "check_python_syntax",
    "detect_scope_violations",
    "discover_git_repos",
    "run_verification_commands",
]
