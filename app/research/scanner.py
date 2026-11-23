"""Deterministic, bounded workspace scanner for research onboarding."""

from __future__ import annotations

import os
import re
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
    ".cache",
    ".idea",
    ".vscode",
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

EXCLUDE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".keystore", ".jks"}

MANIFEST_NAMES = [
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
    "tox.ini",
    "noxfile.py",
]

README_CANDIDATES = ["README.md", "README.rst", "README.txt", "README"]
CI_PATHS = [
    ".github/workflows",
    ".gitlab-ci.yml",
    ".circleci/config.yml",
    "azure-pipelines.yml",
    "Jenkinsfile",
]

CONFIG_CANDIDATES = [
    ".editorconfig",
    ".pre-commit-config.yaml",
    "ruff.toml",
    "mypy.ini",
    "pytest.ini",
    "setup.cfg",
    "pyproject.toml",
    "tsconfig.json",
    "eslint.config.js",
    ".eslintrc",
    ".eslintrc.json",
]

ENTRY_POINT_HINTS = [
    "main.py",
    "app/__init__.py",
    "app/cli.py",
    "app/main.py",
    "src/main.py",
    "src/index.ts",
    "src/index.js",
    "index.ts",
    "index.js",
    "server.py",
    "manage.py",
    "cmd/main.go",
]

ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{2,}\b")
TERM_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{2,}\b")


def _is_excluded_rel(rel_posix: str) -> bool:
    parts = [p for p in rel_posix.split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    if name in EXCLUDE_FILE_NAMES or name.startswith(".env."):
        return True
    if any(name.endswith(s) for s in EXCLUDE_SUFFIXES):
        return True
    for part in parts:
        if part in EXCLUDE_DIRS:
            return True
    # generated research artifacts (avoid self-referential noise/fingerprint coupling in scans)
    if rel_posix == "docs/project_specs.md" or (rel_posix.startswith("docs/project_specs.") and rel_posix.endswith(".md")):
        return True
    if rel_posix == "docs/research" or rel_posix.startswith("docs/research/"):
        return True
    if rel_posix == ".agents/research" or rel_posix.startswith(".agents/research/"):
        return True
    return False


def _safe_rel(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root).as_posix()
    except Exception:
        try:
            return path.relative_to(root).as_posix()
        except Exception:
            return path.name


def _is_safe_file(root: Path, path: Path) -> bool:
    """Return True only for non-symlink files whose resolved path stays within root."""
    try:
        if path.is_symlink():
            return False
        rp = path.resolve(strict=False)
        rr = root.resolve(strict=False)
        return rp == rr or rp.is_relative_to(rr)
    except Exception:
        return False


def _is_safe_dir(root: Path, path: Path) -> bool:
    """Return True only for non-symlink dirs whose resolved path stays within root."""
    try:
        if path.is_symlink():
            return False
        rp = path.resolve(strict=False)
        rr = root.resolve(strict=False)
        return rp == rr or rp.is_relative_to(rr)
    except Exception:
        return False


def _read_text_bounded(path: Path, max_bytes: int) -> str:
    try:
        data = path.read_bytes()[: max(0, int(max_bytes))]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _git_recent_log(repo_root: Path, limit: int = 10) -> list[str]:
    if not (repo_root / ".git").exists():
        return []
    try:
        res = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "log",
                f"-n{max(1, limit)}",
                "--pretty=format:%h %ad %s",
                "--date=short",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode != 0:
            return []
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        return lines[:limit]
    except Exception:
        return []


def _discover_repos(root: Path, max_repos: int = 10) -> list[str]:
    repos: list[str] = []
    git_root = root / ".git"
    if git_root.exists() and not git_root.is_symlink():
        repos.append(".")
    try:
        for dirpath, dirnames, _ in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            rel_posix = "" if rel_dir == "." else Path(rel_dir).as_posix()
            # prune excluded/noise dirs
            pruned = []
            for d in sorted(dirnames):
                rel = f"{rel_posix}/{d}" if rel_posix else d
                if _is_excluded_rel(rel):
                    continue
                pruned.append(d)
            dirnames[:] = pruned
            # detect nested git dirs (but avoid descending into .git itself already excluded)
            for d in list(dirnames):
                rel = f"{rel_posix}/{d}" if rel_posix else d
                p = root / rel
                if p.is_symlink() or not _is_safe_dir(root, p):
                    continue
                gd = p / ".git"
                if gd.exists() and not gd.is_symlink() and rel not in repos:
                    repos.append(rel)
                    if len(repos) >= max_repos:
                        return sorted(repos)
    except Exception:
        pass
    return sorted(repos)


def _collect_tree(root: Path, max_files: int) -> tuple[list[str], list[str], int, int]:
    files: list[str] = []
    dirs: list[str] = []
    bytes_seen = 0
    files_seen = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            rel_posix = "" if rel_dir == "." else Path(rel_dir).as_posix()
            if rel_posix and _is_excluded_rel(rel_posix):
                dirnames[:] = []
                continue
            kept = []
            for d in sorted(dirnames):
                rel = f"{rel_posix}/{d}" if rel_posix else d
                dp = Path(dirpath) / d
                if _is_excluded_rel(rel) or not _is_safe_dir(root, dp):
                    continue
                kept.append(d)
                dirs.append(rel)
            dirnames[:] = kept
            for fname in sorted(filenames):
                rel = f"{rel_posix}/{fname}" if rel_posix else fname
                p = Path(dirpath) / fname
                if _is_excluded_rel(rel) or not _is_safe_file(root, p):
                    continue
                files_seen += 1
                try:
                    bytes_seen += p.stat().st_size
                except Exception:
                    pass
                if len(files) < max_files:
                    files.append(rel)
            # keep deterministic order and bounded dirs list
            if len(dirs) > max_files:
                dirs = dirs[:max_files]
    except Exception:
        pass
    return files, dirs[:max_files], files_seen, bytes_seen


def _extract_heuristics(texts: list[str], max_items: int = 40) -> dict[str, list[str]]:
    combined = "\n".join(texts)
    acronyms = sorted({m.group(0) for m in ACRONYM_RE.finditer(combined) if len(m.group(0)) >= 3})
    # naive term frequency for vocabulary hints
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "will", "have", "has",
        "not", "but", "you", "your", "use", "using", "into", "can", "all", "any", "its", "our", "their",
        "true", "false", "none", "null", "return", "self", "class", "def", "import", "from", "path", "file",
        "test", "tests", "docs", "data", "value", "string", "list", "dict", "json", "type", "name",
    }
    freq: dict[str, int] = {}
    for m in TERM_RE.finditer(combined):
        t = m.group(0)
        low = t.lower()
        if low in stop or low.isdigit() or len(low) < 4:
            continue
        if t.isupper() and len(t) >= 3:
            continue  # acronyms handled separately
        freq[low] = freq.get(low, 0) + 1
    vocab = [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:max_items]]
    # semantics hints: lines with ":" or "->" definitions in docs/config-like snippets
    semantics: list[str] = []
    for line in combined.splitlines():
        s = line.strip()
        if not s or s.startswith("#") and len(s) < 4:
            continue
        if ":" in s and len(s) <= 160 and any(c.isalpha() for c in s):
            semantics.append(s)
        elif "->" in s and len(s) <= 160:
            semantics.append(s)
        if len(semantics) >= max_items:
            break
    return {
        "acronyms": acronyms[:max_items],
        "terms": vocab[:max_items],
        "vocabulary_hints": vocab[:max_items],
        "semantics_hints": semantics[:max_items],
    }


def scan_workspace(workspace_root: Path | str, settings: Any | None = None) -> dict[str, Any]:
    """Produce a bounded, deterministic inventory of the workspace."""
    root = Path(workspace_root).expanduser().resolve()
    max_files = int(getattr(settings, "research_max_files", 40) or 40)
    max_bytes = int(getattr(settings, "research_max_bytes", 200000) or 200000)
    max_files = max(1, min(max_files, 500))
    max_bytes = max(1024, min(max_bytes, 2_000_000))
    per_file_bytes = max(512, min(8000, max_bytes // max(1, max_files)))

    repos = _discover_repos(root)
    sample_files, dirs, files_seen, bytes_seen = _collect_tree(root, max_files=max_files)

    manifests: list[dict[str, Any]] = []
    for name in MANIFEST_NAMES:
        p = root / name
        if p.is_file() and _is_safe_file(root, p):
            manifests.append({"path": name, "size": p.stat().st_size})

    readmes: list[dict[str, Any]] = []
    heuristic_texts: list[str] = []
    budget = max_bytes
    for name in README_CANDIDATES:
        p = root / name
        if not (p.is_file() and _is_safe_file(root, p)):
            continue
        text = _read_text_bounded(p, min(per_file_bytes, budget))
        budget -= len(text.encode("utf-8", errors="ignore"))
        readmes.append({"path": name, "excerpt": text[:2000]})
        heuristic_texts.append(text[:4000])
        if budget <= 0:
            break

    ci_configs: list[str] = []
    for rel in CI_PATHS:
        p = root / rel
        if p.exists() and not p.is_symlink():
            try:
                rp = p.resolve(strict=False)
                rr = root.resolve(strict=False)
                if rp == rr or rp.is_relative_to(rr):
                    ci_configs.append(rel)
            except Exception:
                pass

    config_files: list[str] = []
    for rel in CONFIG_CANDIDATES:
        p = root / rel
        if p.is_file() and _is_safe_file(root, p):
            config_files.append(rel)

    entry_points = []
    for rel in ENTRY_POINT_HINTS:
        p = root / rel
        if p.exists() and not p.is_symlink():
            try:
                rp = p.resolve(strict=False)
                rr = root.resolve(strict=False)
                if rp == rr or rp.is_relative_to(rr):
                    entry_points.append(rel)
            except Exception:
                pass

    recent_git_log = _git_recent_log(root, limit=min(12, max(5, max_files // 4 or 5)))

    # include small snippets from manifests/configs for heuristics (bounded)
    for name in [m["path"] for m in manifests[:10]] + config_files[:10]:
        p = root / name
        if p.is_file() and _is_safe_file(root, p) and budget > 0:
            text = _read_text_bounded(p, min(4000, per_file_bytes, budget))
            budget -= len(text.encode("utf-8", errors="ignore"))
            heuristic_texts.append(text)

    heuristics = _extract_heuristics(heuristic_texts)

    return {
        "workspace_root": str(root),
        "repos": repos,
        "manifests": manifests,
        "directories": sorted(set(dirs))[:max_files],
        "files_sample": sample_files,
        "files_seen": files_seen,
        "bytes_seen": bytes_seen,
        "entry_points": entry_points,
        "readmes": readmes,
        "ci_configs": sorted(set(ci_configs)),
        "config_files": sorted(set(config_files)),
        "recent_git_log": recent_git_log,
        "heuristics": heuristics,
        "limits": {"max_files": max_files, "max_bytes": max_bytes},
    }


__all__ = ["scan_workspace"]
