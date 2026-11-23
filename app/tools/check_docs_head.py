#!/usr/bin/env python3
"""Check repository HEAD/fingerprint alignment with research docs/state.

Deterministic CLI utility for detecting documentation/research-state drift.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def _add_workspace_to_syspath(workspace_root: Path) -> None:
    root_str = str(workspace_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def _run_git(args: list[str], cwd: Path, timeout: int = 5) -> tuple[int, str, str]:
    try:
        res = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return res.returncode, (res.stdout or "").strip(), (res.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "git executable not found"
    except subprocess.TimeoutExpired:
        return 124, "", "git command timed out"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def get_git_head(repo_root: Path) -> dict[str, Any]:
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return {"available": False, "head": None, "reason": "not_a_git_repo"}
    rc, out, err = _run_git(["rev-parse", "--verify", "HEAD"], repo_root)
    if rc == 0 and out:
        return {"available": True, "head": out, "reason": None}
    # unborn HEAD or other issue
    rc_ref, out_ref, _ = _run_git(["symbolic-ref", "--quiet", "HEAD"], repo_root)
    if rc_ref == 0 and out_ref:
        return {"available": True, "head": None, "reason": "unborn_head", "ref": out_ref}
    return {"available": True, "head": None, "reason": "head_unavailable", "error": err or "unknown"}


def parse_project_specs_metadata(specs_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": specs_path.is_file(),
        "path": str(specs_path),
        "commit_head": None,
        "fingerprint": None,
        "metadata_found": False,
    }
    if not specs_path.is_file():
        return result
    try:
        text = specs_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        result["read_error"] = f"{type(e).__name__}: {e}"
        return result

    # Parse markdown table rows like: | Commit/HEAD | <sha> |
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = cells[0].strip().strip("`")
        value = cells[1].strip().strip("`")
        key_norm = re.sub(r"\s+", " ", key).lower()
        if key_norm in {"commit/head", "commit", "head"}:
            result["commit_head"] = value or None
            result["metadata_found"] = True
        elif key_norm == "fingerprint":
            result["fingerprint"] = value or None
            result["metadata_found"] = True
    return result


def _normalize_hex(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # allow placeholders/unknown markers to be treated as missing
    lowered = s.lower()
    if lowered in {"", "none", "null", "(none)", "n/a", "na", "unknown"}:
        return None
    return s


def load_state(state_path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "exists": state_path.is_file(),
        "path": str(state_path),
        "data": {},
        "read_error": None,
    }
    if not state_path.is_file():
        return info
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        info["data"] = data if isinstance(data, dict) else {}
    except Exception as e:
        info["read_error"] = f"{type(e).__name__}: {e}"
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify current git HEAD/fingerprint against docs/project_specs.md metadata "
            "and .agents/research/state.json to detect research/docs misalignment."
        )
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace/repository path to check (default: current working directory).",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit JSON output (default). Accepted for CLI compatibility.",
    )
    parser.add_argument(
        "--no-fingerprint",
        action="store_true",
        help="Skip current fingerprint computation/comparison.",
    )
    args = parser.parse_args(argv)

    try:
        workspace_root = Path(args.workspace).expanduser().resolve()
        if not workspace_root.exists():
            payload = {
                "status": "error",
                "reason": "workspace_not_found",
                "workspace": str(workspace_root),
                "checks": {},
                "misalignment_reasons": [],
                "message": "Workspace path does not exist.",
            }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 3

        _add_workspace_to_syspath(workspace_root)

        # Import project helpers after sys.path setup
        try:
            from app.research.detection import detect_repo_root
        except Exception:
            def detect_repo_root(wr: Path | str, start: Path | str | None = None) -> Path:  # type: ignore
                return Path(wr).expanduser().resolve()

        try:
            from app.research.fingerprint import compute_fingerprint
        except Exception:
            compute_fingerprint = None  # type: ignore

        try:
            from app.research.writer import project_specs_path, research_state_path
        except Exception:
            def project_specs_path(wr: Path | str, lang: str | None = None) -> Path:  # type: ignore
                return Path(wr) / "docs" / "project_specs.md"
            def research_state_path(wr: Path | str) -> Path:  # type: ignore
                return Path(wr) / ".agents" / "research" / "state.json"

        repo_root = detect_repo_root(workspace_root, workspace_root)
        specs_path = project_specs_path(workspace_root, "en")
        state_path = research_state_path(workspace_root)

        docs_meta = parse_project_specs_metadata(specs_path)
        state_info = load_state(state_path)
        state = state_info.get("data") or {}

        git_info = get_git_head(repo_root)
        current_head = git_info.get("head")

        docs_head = _normalize_hex(docs_meta.get("commit_head"))
        docs_fp = _normalize_hex(docs_meta.get("fingerprint"))
        state_head = _normalize_hex(state.get("head"))
        state_last_access_head = _normalize_hex(state.get("last_access_head"))
        state_fp = _normalize_hex(state.get("fingerprint"))
        onboarding_done = bool(state.get("onboarding_done")) if isinstance(state, dict) else False

        current_fingerprint: str | None = None
        fingerprint_error: str | None = None
        if not args.no_fingerprint and compute_fingerprint is not None:
            try:
                fp_info = compute_fingerprint(workspace_root)
                current_fingerprint = _normalize_hex((fp_info or {}).get("fingerprint"))
            except Exception as e:
                fingerprint_error = f"{type(e).__name__}: {e}"
        elif not args.no_fingerprint and compute_fingerprint is None:
            fingerprint_error = "compute_fingerprint unavailable"

        has_specs = bool(docs_meta.get("exists"))
        has_state = bool(state_info.get("exists"))
        has_any_docs_artifact = has_specs or has_state

        misalignment_reasons: list[str] = []
        notes: list[str] = []

        # First-load/no prior onboarding/docs case (b)
        first_load = (not has_specs and not has_state) or (not onboarding_done and not has_specs)

        # HEAD comparisons (when both sides available)
        if git_info.get("available") and current_head:
            if docs_head and docs_head != current_head:
                misalignment_reasons.append("docs_head_mismatch")
            if state_head and state_head != current_head:
                misalignment_reasons.append("state_head_mismatch")
            if state_last_access_head and state_last_access_head != current_head:
                misalignment_reasons.append("state_last_access_head_mismatch")
        elif git_info.get("available") and not current_head:
            notes.append(f"git_head_unavailable:{git_info.get('reason')}")
        else:
            notes.append(f"git_unavailable:{git_info.get('reason')}")

        # Fingerprint comparisons (docs/state not corresponding to current)
        if current_fingerprint:
            if docs_fp and docs_fp != current_fingerprint:
                misalignment_reasons.append("docs_fingerprint_mismatch")
            if state_fp and state_fp != current_fingerprint:
                misalignment_reasons.append("state_fingerprint_mismatch")

        # Incomplete metadata with existing docs can indicate docs don't correspond reliably
        if has_specs and not docs_head and not docs_fp:
            misalignment_reasons.append("docs_metadata_incomplete")
        if has_state and not state_head and not state_fp:
            notes.append("state_metadata_incomplete")

        if docs_meta.get("read_error"):
            misalignment_reasons.append("docs_read_error")
        if state_info.get("read_error"):
            misalignment_reasons.append("state_read_error")
        if fingerprint_error:
            notes.append(f"fingerprint_check_unavailable:{fingerprint_error}")

        # De-duplicate reasons preserving order
        seen = set()
        deduped_reasons: list[str] = []
        for r in misalignment_reasons:
            if r not in seen:
                deduped_reasons.append(r)
                seen.add(r)
        misalignment_reasons = deduped_reasons

        if not has_any_docs_artifact or first_load:
            status = "no_docs"
            message = "No prior onboarding/docs found (first load)."
            exit_code = 2
        elif misalignment_reasons:
            status = "misaligned"
            message = "Documentation/state does not align with current repository HEAD/fingerprint."
            exit_code = 1
        else:
            status = "aligned"
            message = "Documentation/state aligns with current repository HEAD/fingerprint."
            exit_code = 0

        payload = {
            "status": status,
            "exit_code": exit_code,
            "message": message,
            "workspace": str(workspace_root),
            "repo_root": str(repo_root),
            "git": {
                "available": bool(git_info.get("available")),
                "current_head": current_head,
                "reason": git_info.get("reason"),
                "error": git_info.get("error"),
                "ref": git_info.get("ref"),
            },
            "docs": {
                "path": str(specs_path),
                "exists": has_specs,
                "commit_head": docs_head,
                "fingerprint": docs_fp,
                "metadata_found": bool(docs_meta.get("metadata_found")),
                "read_error": docs_meta.get("read_error"),
            },
            "state": {
                "path": str(state_path),
                "exists": has_state,
                "head": state_head,
                "last_access_head": state_last_access_head,
                "fingerprint": state_fp,
                "onboarding_done": onboarding_done,
                "read_error": state_info.get("read_error"),
            },
            "current_fingerprint": current_fingerprint,
            "misalignment_reasons": misalignment_reasons,
            "notes": notes,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return exit_code

    except Exception as e:
        payload = {
            "status": "error",
            "exit_code": 3,
            "reason": "unhandled_exception",
            "message": f"{type(e).__name__}: {e}",
        }
        try:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        except Exception:
            print('{"status":"error","exit_code":3,"reason":"unhandled_exception"}')
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
