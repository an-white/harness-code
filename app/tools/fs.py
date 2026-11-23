"""Sandboxed filesystem tools for agents.

Residual risk: path checks and file reads are not atomic (TOCTOU). A file or
symlink could change between validation and use; O_NOFOLLOW hardening is out of
scope for this task. Callers must treat results as best-effort.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from .policy import is_safe_path


_MAX_READ_BYTES = 1 * 1024 * 1024
_MAX_GREP_FILES = 500
_MAX_GLOB_RESULTS = 200
_MAX_GREP_MATCHES = 100


def _resolve(path: Any, ws_root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ws_root / candidate
    return candidate


def _has_allow(policy: Any, action: str) -> bool:
    action_l = action.lower()
    for rule in getattr(policy, "rules", []):
        if (str(rule.action).lower() == action_l or str(rule.action) == "*") and str(rule.effect).lower() == "allow":
            return True
    return False


def make_fs_tools(workspace_root: Any, policy: Any) -> list[Any]:
    WS_ROOT = Path(workspace_root).expanduser().resolve()
    tools: list[Any] = []

    if _has_allow(policy, "read"):
        @tool
        def read_file(path: str) -> str:
            """Read a UTF-8 text file inside the workspace."""
            try:
                if not is_safe_path(path, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                target = _resolve(path, WS_ROOT)
                try:
                    resolved = target.resolve(strict=False)
                except (OSError, RuntimeError):
                    resolved = target
                if not is_safe_path(resolved, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                if not resolved.is_file():
                    return f"ERROR: not a file: {path}"
                rel = resolved.relative_to(WS_ROOT).as_posix()
                if not policy.allows("read", rel):
                    return "ERROR: permission denied (read)"
                try:
                    size = resolved.stat().st_size
                except OSError as exc:
                    return f"ERROR: cannot stat file: {exc}"
                if size > _MAX_READ_BYTES:
                    return f"ERROR: file too large ({size} bytes, cap {_MAX_READ_BYTES})"
                try:
                    return resolved.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    return f"ERROR: not valid UTF-8: {exc.reason}"
                except OSError as exc:
                    return f"ERROR: cannot read file: {exc}"
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: {exc}"

        @tool
        def list_dir(path: str = ".") -> str:
            """List directory entries (name + type), sorted."""
            try:
                if not is_safe_path(path, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                target = _resolve(path, WS_ROOT)
                try:
                    resolved = target.resolve(strict=False)
                except (OSError, RuntimeError):
                    resolved = target
                if not is_safe_path(resolved, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                if not resolved.exists():
                    return f"ERROR: path does not exist: {path}"
                if not resolved.is_dir():
                    return f"ERROR: not a directory: {path}"
                rel = resolved.relative_to(WS_ROOT).as_posix() if resolved != WS_ROOT else "."
                if not policy.allows("read", rel):
                    return "ERROR: permission denied (read)"
                try:
                    entries = sorted(resolved.iterdir(), key=lambda p: p.name)
                except OSError as exc:
                    return f"ERROR: cannot list directory: {exc}"
                lines = [f"{p.name} ({'dir' if p.is_dir() else 'file'})" for p in entries]
                return "\n".join(lines) if lines else "(empty)"
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: {exc}"

        tools.extend([read_file, list_dir])

    if _has_allow(policy, "edit"):
        @tool
        def write_file(path: str, content: str) -> str:
            """Write UTF-8 text to a file, creating parent directories."""
            try:
                if not is_safe_path(path, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                target = _resolve(path, WS_ROOT)
                try:
                    resolved = target.resolve(strict=False)
                except (OSError, RuntimeError):
                    resolved = target
                if not is_safe_path(resolved, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                rel = resolved.relative_to(WS_ROOT).as_posix() if resolved != WS_ROOT else "."
                if not policy.allows("edit", rel):
                    return "ERROR: permission denied (edit)"
                try:
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    resolved.write_text(content, encoding="utf-8")
                except OSError as exc:
                    return f"ERROR: cannot write file: {exc}"
                return f"OK: wrote {rel}"
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: {exc}"

        @tool
        def edit_file(path: str, old: str, new: str) -> str:
            """Exact string replace in a file; fails if old missing or ambiguous."""
            try:
                if not is_safe_path(path, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                target = _resolve(path, WS_ROOT)
                try:
                    resolved = target.resolve(strict=False)
                except (OSError, RuntimeError):
                    resolved = target
                if not is_safe_path(resolved, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                if not resolved.is_file():
                    return f"ERROR: not a file: {path}"
                rel = resolved.relative_to(WS_ROOT).as_posix() if resolved != WS_ROOT else "."
                if not policy.allows("edit", rel):
                    return "ERROR: permission denied (edit)"
                try:
                    text = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    return f"ERROR: cannot read file: {exc}"
                count = text.count(old)
                if count == 0:
                    return "ERROR: old string not found"
                if count > 1:
                    return f"ERROR: old string is ambiguous ({count} occurrences)"
                try:
                    resolved.write_text(text.replace(old, new, 1), encoding="utf-8")
                except OSError as exc:
                    return f"ERROR: cannot write file: {exc}"
                return "OK: edited"
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: {exc}"

        tools.extend([write_file, edit_file])

    if _has_allow(policy, "glob"):
        @tool
        def glob_files(pattern: str, root: str = ".") -> str:
            """Glob files under a root; results relative to workspace root (cap 200)."""
            try:
                if not is_safe_path(root, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                base = _resolve(root, WS_ROOT)
                try:
                    base_resolved = base.resolve(strict=False)
                except (OSError, RuntimeError):
                    base_resolved = base
                if not is_safe_path(base_resolved, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                if not policy.allows("glob", pattern):
                    return "ERROR: permission denied (glob)"
                results: list[str] = []
                try:
                    for p in base_resolved.glob(pattern):
                        if not is_safe_path(p, WS_ROOT):
                            continue
                        try:
                            rel = p.resolve().relative_to(WS_ROOT).as_posix()
                        except ValueError:
                            continue
                        results.append(rel)
                        if len(results) >= _MAX_GLOB_RESULTS:
                            break
                except (ValueError, OSError) as exc:
                    return f"ERROR: glob failed: {exc}"
                results = sorted(set(results))
                if not results:
                    return "No matches"
                return "\n".join(results[:_MAX_GLOB_RESULTS])
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: {exc}"

        tools.append(glob_files)

    if _has_allow(policy, "grep"):
        @tool
        def grep_files(pattern: str, root: str = ".", include: str = "*") -> str:
            """Regex search over files; returns file:line: text (cap 100)."""
            try:
                if not is_safe_path(root, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                base = _resolve(root, WS_ROOT)
                try:
                    base_resolved = base.resolve(strict=False)
                except (OSError, RuntimeError):
                    base_resolved = base
                if not is_safe_path(base_resolved, WS_ROOT):
                    return "ERROR: path is outside the workspace or escapes via symlink"
                if not policy.allows("grep", pattern):
                    return "ERROR: permission denied (grep)"
                try:
                    regex = re.compile(pattern)
                except re.error as exc:
                    return f"ERROR: invalid regex: {exc}"
                matches: list[str] = []
                visited = 0
                try:
                    for p in base_resolved.rglob("*"):
                        if visited >= _MAX_GREP_FILES:
                            break
                        if not p.is_file():
                            continue
                        visited += 1
                        if not fnmatch.fnmatchcase(p.name, include):
                            continue
                        if not is_safe_path(p, WS_ROOT):
                            continue
                        try:
                            rel = p.resolve().relative_to(WS_ROOT).as_posix()
                        except ValueError:
                            continue
                        if not policy.allows("read", rel):
                            continue
                        try:
                            size = p.stat().st_size
                        except OSError:
                            continue
                        if size > _MAX_READ_BYTES:
                            continue
                        try:
                            text = p.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            continue
                        for i, line in enumerate(text.splitlines(), 1):
                            if regex.search(line):
                                matches.append(f"{rel}:{i}: {line}")
                                if len(matches) >= _MAX_GREP_MATCHES:
                                    break
                        if len(matches) >= _MAX_GREP_MATCHES:
                            break
                except OSError as exc:
                    return f"ERROR: search failed: {exc}"
                if not matches:
                    return "No matches"
                return "\n".join(matches[:_MAX_GREP_MATCHES])
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: {exc}"

        tools.append(grep_files)

    return tools
