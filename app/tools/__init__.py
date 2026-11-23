"""Tool layer and permission policy for agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.loader import AgentSpec
from app.config import Settings

from .fs import make_fs_tools
from .policy import (
    PermissionPolicy,
    ShellPolicy,
    build_policy,
    build_shell_policy,
    detect_owner_repo,
    is_safe_path,
)
from .shell import make_git_tools, make_shell_tool
from .web import make_web_tools, sanitize_query, web_access_decision, write_web_audit


def make_toolset(
    spec: AgentSpec,
    workspace_root: Path | str,
    settings: Settings,
    web_consent: dict[str, Any] | None = None,
) -> list[Any]:
    """Return the LangChain tools an agent is allowed to use."""
    root = Path(workspace_root).expanduser().resolve()
    policy = build_policy(spec)
    tools: list[Any] = []

    tools.extend(make_fs_tools(root, policy))

    # Only expose the shell tool when the policy permits at least one command.
    if any(rule.action.lower() == "shell" and rule.effect == "allow" for rule in policy.rules):
        tools.append(make_shell_tool(root, policy, settings.shell_timeout))

    tools.extend(make_git_tools(root, policy))
    tools.extend(make_web_tools(root, policy, settings, web_consent=web_consent))

    return tools


__all__ = [
    "PermissionPolicy",
    "ShellPolicy",
    "build_policy",
    "build_shell_policy",
    "detect_owner_repo",
    "is_safe_path",
    "make_fs_tools",
    "make_git_tools",
    "make_shell_tool",
    "make_toolset",
    "make_web_tools",
    "sanitize_query",
    "web_access_decision",
    "write_web_audit",
]