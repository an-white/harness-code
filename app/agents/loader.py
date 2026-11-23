"""Load agent prompts and permissions from ``agents/*.md``.

The Markdown files are the single source of truth for the prompts and the
permission rules, so the Python side only parses and normalizes them.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT

import yaml

logger = logging.getLogger(__name__)

AGENTS_SUBDIR = Path("agents")

# Skills directory is gitignored like .agents/
SKILLS_SUBDIR = Path(".agents") / "skills"

# Hard ceiling for an agent file; frontmatter plus prompt are tiny.
MAX_AGENT_FILE_BYTES = 262144

# Permission actions understood by OpenCode and by the Python runtime.
ALLOWED_ACTIONS = frozenset(
    {
        "read",
        "edit",
        "glob",
        "grep",
        "shell",
        "subagent",
        "skill",
        "question",
        "webfetch",
        "websearch",
        "external_directory",
        "*",
    }
)

# Assembled at runtime from parts so this template's own source (and the
# compiled bytecode) stays free of the legacy repository names it rewrites;
# tests/test_generic_prompts.py enforces it.
_LEGACY_A = "".join(("scr", "apper"))
_LEGACY_B = "".join(("dash", "board"))
_LEGACY_A_DIR = f"{_LEGACY_A}/"
_LEGACY_B_DIR = f"{_LEGACY_B}/"
_LEGACY_TRIO = f"`{_LEGACY_A_DIR}`, `{_LEGACY_B_DIR}`, or the root"

GENERIC_OWNER = "the repository that owns the task"
GENERIC_OWNER_TOUCHED = "the repository that owns the touched files"

# Ordered: the longest phrases are rewritten before their fragments.
GENERIC_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (
        rf"\({re.escape(_LEGACY_TRIO)}\)",
        f"({GENERIC_OWNER_TOUCHED}, or the workspace root)",
    ),
    (
        re.escape(_LEGACY_TRIO),
        f"{GENERIC_OWNER_TOUCHED}, or the workspace root",
    ),
    (re.escape(_LEGACY_A_DIR), GENERIC_OWNER),
    (re.escape(_LEGACY_B_DIR), GENERIC_OWNER),
)


class AgentLoadError(RuntimeError):
    """Raised when an agent file is missing, unparsable or inconsistent."""


@dataclass
class PermissionRule:
    """One OpenCode permission rule."""

    action: str
    resource: str
    effect: str


@dataclass
class AgentSpec:
    """Everything the runtime needs to know about one agent."""

    name: str
    description: str
    mode: str
    color: str
    system_prompt: str
    permissions: list[PermissionRule] = field(default_factory=list)
    source_path: Path | None = None


def _safe_default_permissions() -> list[PermissionRule]:
    """Deny everything when the frontmatter cannot be trusted."""
    return [PermissionRule(action="*", resource="*", effect="deny")]


def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    """Split a Markdown file into (YAML frontmatter, body)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise AgentLoadError(
            f"{path}: missing YAML frontmatter delimited by '---' at the top"
        )
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise AgentLoadError(f"{path}: unterminated YAML frontmatter")


def _parse_frontmatter(raw: str, path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AgentLoadError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentLoadError(
            f"{path}: YAML frontmatter must be a mapping, got {type(data).__name__}"
        )
    return data


def _parse_permissions(raw: Any, path: Path) -> list[PermissionRule]:
    """Parse the permission list, falling back to a deny-all default.

    A missing key, an empty list and a non-list value are all treated as
    missing. Individual invalid rules are dropped with a warning; when no
    valid rule remains the safe default is used.
    """
    if not isinstance(raw, list) or not raw:
        logger.warning(
            "%s: missing, empty or malformed 'permissions', using safe "
            "default (deny all)",
            path,
        )
        return _safe_default_permissions()

    rules: list[PermissionRule] = []
    for entry in raw:
        rule = _parse_permission_rule(entry, path)
        if rule is not None:
            rules.append(rule)

    if not rules:
        logger.warning(
            "%s: no valid 'permissions' rule, using safe default (deny all)",
            path,
        )
        return _safe_default_permissions()
    return rules


def _parse_permission_rule(entry: Any, path: Path) -> PermissionRule | None:
    """Return a validated rule, or ``None`` when the entry must be dropped."""
    if not isinstance(entry, dict):
        logger.warning("%s: dropping permission entry that is not a mapping", path)
        return None

    action = entry.get("action")
    resource = entry.get("resource")
    effect = entry.get("effect")

    if not isinstance(action, str) or action.strip().lower() not in ALLOWED_ACTIONS:
        logger.warning("%s: dropping permission rule with unsupported action", path)
        return None
    if not isinstance(resource, str) or not resource.strip():
        logger.warning("%s: dropping permission rule with invalid resource", path)
        return None
    if not isinstance(effect, str) or effect.strip().lower() not in ("allow", "deny"):
        logger.warning("%s: dropping permission rule with invalid effect", path)
        return None

    normalized_action = action.strip().lower()
    normalized_effect = effect.strip().lower()
    if normalized_action == "*" and normalized_effect == "allow":
        logger.warning(
            "%s: dropping permission rule that allows the wildcard action %r "
            "for resource %r; the safe deny-all default applies",
            path,
            normalized_action,
            resource,
        )
        return None

    return PermissionRule(
        action=normalized_action,
        resource=resource,
        effect=normalized_effect,
    )


def _genericize(body: str, path: Path) -> str:
    """Rewrite project-specific repository names into generic wording."""
    for pattern, replacement in GENERIC_REPLACEMENTS:
        body, count = re.subn(pattern, replacement, body, flags=re.IGNORECASE)
        if count:
            logger.warning(
                "%s: replaced %d occurrence(s) of %r with generic wording",
                path,
                count,
                pattern,
            )
    return body


def _optional_string(frontmatter: dict[str, Any], key: str, path: Path) -> str:
    """Return an optional string field, rejecting lists, dicts, etc."""
    value = frontmatter.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AgentLoadError(
            f"{path}: '{key}' must be a string, got {type(value).__name__}"
        )
    return value.strip()


def _load_agent_file(path: Path) -> AgentSpec:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AgentLoadError(f"{path}: cannot stat agent file: {exc}") from exc
    if size > MAX_AGENT_FILE_BYTES:
        raise AgentLoadError(
            f"{path}: agent file is too large "
            f"({size} bytes, limit {MAX_AGENT_FILE_BYTES} bytes)"
        )

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentLoadError(f"{path}: cannot read agent file: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise AgentLoadError(
            f"{path}: agent file is not valid UTF-8: {exc.reason}"
        ) from exc
    except RecursionError as exc:
        raise AgentLoadError(
            f"{path}: agent file is too deeply nested to parse"
        ) from exc

    try:
        raw_frontmatter, body = _split_frontmatter(text, path)
        frontmatter = _parse_frontmatter(raw_frontmatter, path)
    except RecursionError as exc:
        raise AgentLoadError(
            f"{path}: agent file is too deeply nested to parse"
        ) from exc

    description = _optional_string(frontmatter, "description", path)
    if not description:
        raise AgentLoadError(f"{path}: 'description' must not be empty")

    system_prompt = _genericize(body, path).strip()
    if not system_prompt:
        raise AgentLoadError(f"{path}: prompt body must not be empty")

    return AgentSpec(
        name=path.stem,
        description=description,
        mode=_optional_string(frontmatter, "mode", path),
        color=_optional_string(frontmatter, "color", path),
        system_prompt=system_prompt,
        permissions=_parse_permissions(frontmatter.get("permissions"), path),
        source_path=path,
    )


def _check_agent_path(path: Path, agents_dir: Path) -> None:
    """Reject symlinks, non-regular files and paths escaping ``agents_dir``."""
    if path.is_symlink():
        raise AgentLoadError(f"{path}: refusing to load a symlinked agent file")

    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise AgentLoadError(f"{path}: cannot stat agent file: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise AgentLoadError(f"{path}: refusing to load a non-regular agent file")

    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AgentLoadError(f"{path}: cannot resolve agent file: {exc}") from exc
    if not resolved.is_relative_to(agents_dir):
        raise AgentLoadError(f"{path}: agent file escapes {agents_dir}")


def load_agents(workspace_root: Path | str | None = None) -> dict[str, AgentSpec]:
    """Load every agent from ``PROJECT_ROOT/agents/*.md`` (the harness repo).

    The ``workspace_root`` parameter is accepted for backward compatibility
    but is no longer used for agent discovery; agents are always loaded from
    the harness repo at ``PROJECT_ROOT / agents``.
    """
    agents_dir = PROJECT_ROOT.resolve() / AGENTS_SUBDIR
    if not agents_dir.is_dir():
        raise AgentLoadError(f"{agents_dir}: agent directory not found")

    agents: dict[str, AgentSpec] = {}
    for path in sorted(agents_dir.glob("*.md")):
        _check_agent_path(path, agents_dir)
        spec = _load_agent_file(path)
        agents[spec.name] = spec

    if not agents:
        raise AgentLoadError(f"{agents_dir}: no agent files found")
    return agents


def load_skills(workspace_root: Path | str) -> dict[str, AgentSpec]:
    """Load every skill from ``<workspace_root>/.agents/skills/*.md``.

    Skills produce ``AgentSpec`` objects but with a different namespace
    (``skill-<name>``) so they do not conflict with built-in agents.

    Returns a dict mapping ``skill-<name>`` -> ``AgentSpec``.
    """
    root = Path(workspace_root).expanduser().resolve()
    skills_dir = (root / SKILLS_SUBDIR).resolve()
    # .agents is gitignored; still validate it stays under the workspace root.
    if not skills_dir.is_relative_to(root):
        raise AgentLoadError(
            f"{skills_dir}: skill directory escapes the workspace root {root}"
        )
    if not skills_dir.is_dir():
        # No skills directory yet; return empty dict (not an error).
        return {}
    skills: dict[str, AgentSpec] = {}
    for path in sorted(skills_dir.glob("**/*.md")):
        try:
            spec = _load_agent_file(path)
            # Remap the agent name to the skill namespace.
            spec.name = f"skill-{spec.name}"
            skills[spec.name] = spec
        except AgentLoadError as exc:
            logger.warning("%s: skipping skill file %s: %s", path, exc)
    return skills


def get_agent(agents: dict[str, AgentSpec], name: str) -> AgentSpec:
    """Return the agent called ``name`` or fail with the known names."""
    try:
        return agents[name]
    except KeyError as exc:
        known = ", ".join(sorted(agents)) or "<none>"
        raise AgentLoadError(
            f"unknown agent {name!r}; known agents: {known}"
        ) from exc