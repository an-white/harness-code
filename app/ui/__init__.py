"""UI helpers for agent identity, banners, and prompt styling."""

from app.ui.agent_ui import (
    AGENT_ART_DIR,
    AGENT_IDENTITIES,
    AgentIdentity,
    DEFAULT_IDENTITY,
    clear_render_caches,
    colorize,
    display_banner,
    format_agent_badge,
    format_input_highlight,
    format_prompt_prefix,
    get_identity,
    load_agent_art,
    register_identity,
    render_banner,
    strip_ansi,
    supports_color,
)

__all__ = [
    "AGENT_ART_DIR",
    "AGENT_IDENTITIES",
    "AgentIdentity",
    "DEFAULT_IDENTITY",
    "clear_render_caches",
    "colorize",
    "display_banner",
    "format_agent_badge",
    "format_input_highlight",
    "format_prompt_prefix",
    "get_identity",
    "load_agent_art",
    "register_identity",
    "render_banner",
    "strip_ansi",
    "supports_color",
]
