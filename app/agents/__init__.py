"""Agent specifications loaded from the OpenCode Markdown files."""

from app.agents.loader import (
    AgentLoadError,
    AgentSpec,
    PermissionRule,
    get_agent,
    load_agents,
)
from app.agents.runtime import (
    AgentRuntime,
    CapabilityProbe,
    build_system_prompt,
    make_llm_for_role,
    run_agent,
    supports_tools,
)

__all__ = [
    "AgentLoadError",
    "AgentRuntime",
    "AgentSpec",
    "CapabilityProbe",
    "PermissionRule",
    "build_system_prompt",
    "get_agent",
    "load_agents",
    "make_llm_for_role",
    "run_agent",
    "supports_tools",
]
