"""Stable fingerprinting for research cache freshness."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable

EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "target",
    "out",
    ".next",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    ".agents",
    ".ssh",
    ".aws",
    "proc",
    "sys",
    "dev",
}

EXCLUDE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
}

EXCLUDE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".crt", ".cer"}

MANIFEST_NAMES = (
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
)

KEY_ENTRY_NAMES = (
    "README.md",
    "README.rst",
    "README.txt",
    "main.py",
    "app/__init__.py",
    "app/cli.py",
    "src/main.py",
    "src/index.ts",
    "src/index.js",
    "index.ts",
    "index.js",
)


def _is_generated_research_artifact(rel_posix: str, name: str) -> bool:
    if rel_posix == "docs/project_specs.md":
        return True
    if rel_posix.startswith("docs/project_specs.") and name.endswith(".md"):
        return True
    if rel_posix == "docs/research" or rel_posix.startswith("docs/research/"):
        return True
    if rel_posix == ".agents/research" or rel_posix.startswith(".agents/research/"):
        return True
    return False


def _is_excluded_path(rel_posix: str, name: str) -> bool:
    if name in EXCLUDE_FILE_NAMES or name.startswith(".env."):
        return True
    if any(name.endswith(s) for s in EXCLUDE_SUFFIXES):
        return True
    parts = rel_posix.split("/") if rel_posix else []
    for part in parts:
        if part in EXCLUDE_DIRS:
            return True
    if _is_generated_research_artifact(rel_posix, name):
        return True
    return False


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_text(text: str) -> str:
    return _hash_bytes(text.encode("utf-8", errors="replace"))


def _is_safe_file(root: Path, path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        rp = path.resolve(strict=False)
        rr = root.resolve(strict=False)
        return rp == rr or rp.is_relative_to(rr)
    except Exception:
        return False


def _is_safe_dir(root: Path, path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        rp = path.resolve(strict=False)
        rr = root.resolve(strict=False)
        return rp == rr or rp.is_relative_to(rr)
    except Exception:
        return False


def _git_head(root: Path) -> str:
    git_dir = root / ".git"
    if not git_dir.exists():
        return ""
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            return (res.stdout or "").strip()
    except Exception:
        pass
    # fallback read
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="ignore").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_path = git_dir / ref
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8", errors="ignore").strip()
        return head
    except Exception:
        return ""


def _iter_tree_entries(root: Path, max_files: int) -> list[str]:
    files: list[tuple[str, int]] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir_posix = "" if rel_dir == "." else Path(rel_dir).as_posix()
            kept_dirs = []
            for d in sorted(dirnames):
                rel = f"{rel_dir_posix}/{d}" if rel_dir_posix else d
                dp = Path(dirpath) / d
                if _is_excluded_path(rel, d) or not _is_safe_dir(root, dp):
                    continue
                kept_dirs.append(d)
            dirnames[:] = kept_dirs
            for fname in sorted(filenames):
                rel = f"{rel_dir_posix}/{fname}" if rel_dir_posix else fname
                p = Path(dirpath) / fname
                if _is_excluded_path(rel, fname) or not _is_safe_file(root, p):
                    continue
                try:
                    st = p.stat()
                    files.append((rel, st.st_size))
                except Exception:
                    continue
                if len(files) >= max_files:
                    break
            if len(files) >= max_files:
                break
    except Exception:
        pass
    files.sort()
    dirs: set[str] = set()
    for rel, _ in files:
        parts = rel.split("/")[:-1]
        cur = []
        for part in parts:
            cur.append(part)
            dirs.add("/".join(cur))
    entries = [f"D|{d}" for d in sorted(dirs)]
    entries.extend(f"F|{rel}|{size}" for rel, size in files[:max_files])
    return entries[: max_files + len(dirs)]


def _read_file_hash(path: Path, max_bytes: int) -> str:
    try:
        data = path.read_bytes()
        if len(data) > max_bytes:
            data = data[:max_bytes] + b"\n...[truncated]"
        return _hash_bytes(data)
    except Exception:
        return ""


def compute_fingerprint(
    workspace_root: Path | str,
    settings: Any | None = None,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Compute a stable fingerprint of the workspace for onboarding freshness."""
    root = Path(workspace_root).expanduser().resolve()
    if max_files is None:
        max_files = int(getattr(settings, "research_max_files", 40) or 40) * 25
    if max_bytes is None:
        max_bytes = min(int(getattr(settings, "research_max_bytes", 200000) or 200000), 200000)
    max_files = max(50, min(int(max_files), 5000))

    head = _git_head(root)
    tree_entries = _iter_tree_entries(root, max_files=max_files)
    tree_hash = _hash_text("\n".join(tree_entries))

    manifest_hashes: dict[str, str] = {}
    for name in MANIFEST_NAMES:
        p = root / name
        if p.is_file() and _is_safe_file(root, p):
            manifest_hashes[name] = _read_file_hash(p, max_bytes)

    key_hashes: dict[str, str] = {}
    for rel in KEY_ENTRY_NAMES:
        p = root / rel
        if p.is_file() and _is_safe_file(root, p):
            key_hashes[rel] = _read_file_hash(p, max_bytes)

    payload = {
        "head": head,
        "manifests": manifest_hashes,
        "keys": key_hashes,
        "tree": tree_hash,
    }
    fingerprint = _hash_text(
        json_dumps_stable(payload)
    )
    return {
        "fingerprint": fingerprint,
        "head": head,
        "manifests": sorted(manifest_hashes.keys()),
        "key_entries": sorted(key_hashes.keys()),
        "tree_hash": tree_hash,
        "tree_entries_count": len(tree_entries),
    }


def json_dumps_stable(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["compute_fingerprint", "json_dumps_stable"]
