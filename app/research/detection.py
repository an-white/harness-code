"""Project detection, first-run and freshness helpers for research onboarding."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

def _detect_owner_repo(path: Any, workspace_root: Any) -> Path:
    """Nearest ancestor containing .git within workspace_root (no app.tools import)."""
    try:
        root = Path(workspace_root).expanduser().resolve()
    except Exception:
        return Path(workspace_root)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except Exception:
        resolved = candidate
    if not resolved.is_relative_to(root):
        resolved = root
    cur = resolved.parent if resolved.is_file() else resolved
    while True:
        if not cur.is_relative_to(root):
            cur = root
        try:
            if (cur / ".git").exists():
                return cur
        except Exception:
            pass
        if cur == root:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return root

from .fingerprint import compute_fingerprint

MANIFEST_NAMES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
    "Makefile",
    "CMakeLists.txt",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yaml",
}

HARNESS_ROOT_PATHS = {
    ".agents",
    ".gitignore",
    ".opencode",
    "agents",
    "AGENTS.md",
    "README.md",
    "app",
    "docs",
    "main.py",
    "tests",
}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _has_any_git_commit(repo_root: Path) -> bool:
    git_dir = repo_root / ".git"
    try:
        if not git_dir.exists():
            return False
        # Prefer git metadata call with short timeout/safe handling (like scanner/fingerprint).
        try:
            res = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, Exception):
            # fall through to safe filesystem fallback
            pass
        head = git_dir / "HEAD"
        try:
            if head.is_file():
                content = head.read_text(encoding="utf-8", errors="ignore").strip()
                if content and not content.startswith("ref:"):
                    return True
                if content.startswith("ref:"):
                    ref = content.split(":", 1)[1].strip()
                    if (git_dir / ref).is_file():
                        return True
            refs = git_dir / "refs"
            if refs.exists():
                for _ in refs.rglob("*"):
                    return True
        except Exception:
            return False
    except Exception:
        return False
    return False


def _find_manifests(root: Path) -> list[str]:
    found: list[str] = []
    try:
        for name in sorted(MANIFEST_NAMES):
            p = root / name
            if p.is_file():
                found.append(name)
    except Exception:
        pass
    return found


def _has_code_outside_scaffold(root: Path) -> bool:
    try:
        for child in root.iterdir():
            name = child.name
            if name in {".git", ".agents", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}:
                continue
            if name.startswith(".env"):
                continue
            if name not in HARNESS_ROOT_PATHS:
                return True
            # manifests under root (not part of scaffold set above except some) count as existing signal
            if child.is_file() and name in MANIFEST_NAMES:
                return True
    except Exception:
        return False
    # also check common manifest locations missed above
    return bool(_find_manifests(root))


def detect_repo_root(workspace_root: Path | str, start: Path | str | None = None) -> Path:
    root = Path(workspace_root).expanduser().resolve()
    base = Path(start) if start is not None else root
    if not base.is_absolute():
        base = root / base
    try:
        resolved = base.resolve(strict=False)
    except Exception:
        resolved = base
    return _detect_owner_repo(resolved, root)


def detect_project_state(
    workspace_root: Path | str,
    settings: Any | None = None,
    *,
    refresh: bool = False,
    skip: bool = False,
) -> dict[str, Any]:
    """Detect onboarding applicability, first-run status and freshness."""
    root = Path(workspace_root).expanduser().resolve()
    repo_root = detect_repo_root(root, root)
    research_dir = root / ".agents" / "research"
    state_path = research_dir / "state.json"
    context_path = research_dir / "context.md"
    docs_specs = root / "docs" / "project_specs.md"
    state = _read_json(state_path) or {}

    manifests = _find_manifests(root)
    git_commits = _has_any_git_commit(repo_root) if (repo_root / ".git").exists() else False
    existing_project = bool(git_commits or manifests or _has_code_outside_scaffold(root))
    has_specs = docs_specs.is_file()
    has_context = context_path.is_file()
    has_state = state_path.is_file()

    fp_info = compute_fingerprint(root, settings=settings)
    fingerprint = str(fp_info.get("fingerprint") or "")
    stored_fp = str(state.get("fingerprint") or "")
    fresh = bool(fingerprint and stored_fp and fingerprint == stored_fp and has_specs and has_context)

    first_run = not (has_state and has_specs)
    onboarding_done = bool(state.get("onboarding_done")) and has_specs

    mode = "auto"
    if settings is not None:
        mode = str(getattr(settings, "research_onboarding", "auto") or "auto").strip().lower()
    if mode not in {"auto", "always", "never"}:
        mode = "auto"

    reasons: list[str] = []
    if skip or mode == "never":
        should_run = False
        if skip:
            reasons.append("skip_flag")
        if mode == "never":
            reasons.append("mode_never")
    elif refresh or mode == "always":
        should_run = True
        if refresh:
            reasons.append("refresh_requested")
        if mode == "always":
            reasons.append("mode_always")
    elif mode == "auto":
        if not existing_project:
            should_run = False
            reasons.append("not_existing_project")
        elif not onboarding_done or first_run or not fresh:
            should_run = True
            if not onboarding_done or first_run:
                reasons.append("first_run")
            if onboarding_done and not fresh:
                reasons.append("stale_fingerprint")
        else:
            should_run = False
            reasons.append("fresh")
    else:
        should_run = False
        reasons.append("disabled")

    return {
        "workspace_root": str(root),
        "repo_root": str(repo_root),
        "git_repo": (repo_root / ".git").exists(),
        "git_commits": git_commits,
        "manifests": manifests,
        "existing_project": existing_project,
        "first_run": first_run,
        "onboarding_done": onboarding_done,
        "fresh": fresh,
        "fingerprint": fingerprint,
        "stored_fingerprint": stored_fp,
        "mode": mode,
        "refresh_requested": bool(refresh),
        "skip_requested": bool(skip),
        "should_run": bool(should_run),
        "reasons": reasons,
        "paths": {
            "state": str(state_path),
            "context": str(context_path),
            "specs": str(docs_specs),
        },
        "fingerprint_info": fp_info,
    }


__all__ = ["detect_project_state", "detect_repo_root"]
