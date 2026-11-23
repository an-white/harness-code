"""Agent visual identity helpers (banners, badges, prompt styling).

Foundation for UIX-2/UIX-3: provides an extensible per-role identity registry,
editable ASCII art assets under ``assets/agent_art/<role>.txt``, ANSI-aware
rendering with TTY/fallback behavior, and in-memory caching to avoid repeated
disk reads and unnecessary work.

Color/mouse/inline-edit behavior is intentionally minimal and non-breaking;
prompt/input semantics remain owned by callers. UIX-3 may wire these helpers
into ``_read_input`` and add config knobs (enable/disable colors, mouse) if
needed. Defaults are safe/backward-compatible (no escape codes on non-TTY).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, TextIO

# Project root (repo). Art assets live at <root>/assets/agent_art/.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_ART_DIR = PROJECT_ROOT / "assets" / "agent_art"

ANSI_RESET = "\033[0m"

# Basic ANSI SGR foreground codes (standard 8/16-color palette; widely supported).
_FG_CODES: dict[str, int] = {
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
    "bright_black": 90,
    "bright_red": 91,
    "bright_green": 92,
    "bright_yellow": 93,
    "bright_blue": 94,
    "bright_magenta": 95,
    "bright_cyan": 96,
    "bright_white": 97,
    "orange": 33,  # portable fallback (256-color/truecolor not assumed)
    "gray": 90,
    "grey": 90,
}
_BG_CODES: dict[str, int] = {
    "black": 40,
    "red": 41,
    "green": 42,
    "yellow": 43,
    "blue": 44,
    "magenta": 45,
    "cyan": 46,
    "white": 47,
    "bright_black": 100,
    "bright_red": 101,
    "bright_green": 102,
    "bright_yellow": 103,
    "bright_blue": 104,
    "bright_magenta": 105,
    "bright_cyan": 106,
    "bright_white": 107,
    "orange": 43,
    "gray": 100,
    "grey": 100,
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class AgentIdentity:
    """Visual identity for an agent role."""

    role: str
    label: str
    fg: str
    bg: str | None = None
    banner_color: str | None = None
    art_name: str | None = None

    @property
    def art_path(self) -> Path:
        name = self.art_name or f"{self.role}.txt"
        return AGENT_ART_DIR / name


def _normalize_role(role: str | None) -> str:
    if not role:
        return "default"
    key = str(role).strip().lower().replace("_", "-")
    aliases = {
        "project-manager": "pm",
        "projectmanager": "pm",
    }
    return aliases.get(key, key)


# Extensible registry: add roles here (or extend via helpers) without changing
# call sites. Requested colors: PM white, builder blue, translator gray,
# qa yellow, security red, research orange. Reviewer/unknown get safe neutral.
AGENT_IDENTITIES: dict[str, AgentIdentity] = {
    "pm": AgentIdentity(
        role="pm",
        label="PM",
        fg="white",
        bg="bright_black",
        banner_color="white",
        art_name="pm.txt",
    ),
    "builder": AgentIdentity(
        role="builder",
        label="Builder",
        fg="blue",
        bg="bright_black",
        banner_color="blue",
        art_name="builder.txt",
    ),
    "translator": AgentIdentity(
        role="translator",
        label="Translator",
        fg="gray",
        bg="bright_black",
        banner_color="gray",
        art_name="translator.txt",
    ),
    "qa": AgentIdentity(
        role="qa",
        label="QA",
        fg="yellow",
        bg="bright_black",
        banner_color="yellow",
        art_name="qa.txt",
    ),
    "security": AgentIdentity(
        role="security",
        label="Security",
        fg="red",
        bg="bright_black",
        banner_color="red",
        art_name="security.txt",
    ),
    "research": AgentIdentity(
        role="research",
        label="Research",
        fg="orange",
        bg="bright_black",
        banner_color="orange",
        art_name="research.txt",
    ),
    "reviewer": AgentIdentity(
        role="reviewer",
        label="Reviewer",
        fg="cyan",
        bg="bright_black",
        banner_color="cyan",
        art_name="reviewer.txt",
    ),
}

DEFAULT_IDENTITY = AgentIdentity(
    role="default",
    label="Agent",
    fg="white",
    bg="bright_black",
    banner_color="white",
    art_name=None,
)


def get_identity(role: str | None) -> AgentIdentity:
    """Return the identity for *role* with safe fallback for unknown/None."""
    key = _normalize_role(role)
    return AGENT_IDENTITIES.get(key, DEFAULT_IDENTITY)


def register_identity(identity: AgentIdentity) -> None:
    """Register or replace an identity at runtime (extensibility hook)."""
    AGENT_IDENTITIES[_normalize_role(identity.role)] = identity
    # New identity may change art selection and rendered outputs; invalidate caches.
    clear_render_caches()


def supports_color(stream: TextIO | None = None, *, force: bool | None = None) -> bool:
    """Return True when ANSI styling should be emitted.

    Conservative TTY detection: requires an interactive stream and avoids common
    "dumb"/disabled cases. No color/mouse assumptions for unsupported terminals.
    ``force`` allows explicit callers/tests to override detection.
    """
    if force is not None:
        return bool(force)
    s = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    term = (os.environ.get("TERM") or "").lower()
    if term in ("", "dumb"):
        return False
    try:
        isatty = getattr(s, "isatty", None)
        return bool(isatty and s.isatty())
    except Exception:
        return False


def _sgr(*codes: int) -> str:
    if not codes:
        return ""
    return f"\033[{';'.join(str(c) for c in codes)}m"


def colorize(
    text: str,
    *,
    fg: str | None = None,
    bg: str | None = None,
    bold: bool = False,
    stream: TextIO | None = None,
    force: bool | None = None,
) -> str:
    """Wrap *text* with ANSI SGR codes when supported, else return *text*."""
    if not text or not supports_color(stream, force=force):
        return text
    codes: list[int] = []
    if bold:
        codes.append(1)
    if fg:
        code = _FG_CODES.get(str(fg).strip().lower())
        if code is not None:
            codes.append(code)
    if bg:
        code = _BG_CODES.get(str(bg).strip().lower())
        if code is not None:
            codes.append(code)
    if not codes:
        return text
    return f"{_sgr(*codes)}{text}{ANSI_RESET}"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


@lru_cache(maxsize=32)
def load_agent_art(role: str | None) -> str:
    """Load and cache raw ASCII art for *role* from ``assets/agent_art``.

    Returns empty string when missing/unreadable/empty so callers can no-op.
    Cached in memory to minimize repeated disk reads/resource use.
    """
    key = _normalize_role(role)
    identity = AGENT_IDENTITIES.get(key)
    candidates: list[Path] = []
    if identity is not None:
        candidates.append(identity.art_path)
    # Fallback: direct <role>.txt even if identity uses custom art_name/None.
    candidates.append(AGENT_ART_DIR / f"{key}.txt")
    for path in candidates:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                continue
            # Preserve exact art bytes/content as text; normalize only trailing file newline.
            return text.rstrip("\n")
        except Exception:
            continue
    return ""


def render_banner(role: str | None, *, stream: TextIO | None = None, force_color: bool | None = None) -> str:
    """Return the banner string for *role* (art + role color) or empty string.

    - Loads raw art from ``assets/agent_art/<role>.txt`` (editable, no code changes).
      Raw art loading is cached in memory to avoid repeated disk reads.
    - Applies role/banner color at render time based on current ANSI/TTY capability
      (or forced), so output cannot be stale across environments/contexts.
    - Safe no-op/fallback when missing or unsupported.
    Designed so CLI/UIX-2 can call it on each builder execution cheaply.
    """
    art = load_agent_art(role)
    if not art:
        return ""
    identity = get_identity(role)
    banner_fg = identity.banner_color or identity.fg
    return colorize(art, fg=banner_fg, bold=False, stream=stream, force=force_color)


def display_banner(
    role: str | None,
    *,
    stream: TextIO | None = None,
    force_color: bool | None = None,
) -> str:
    """Convenience: render banner for *role* and write to *stream* if non-empty.

    Returns the rendered banner (possibly empty). Callers (UIX-2) decide when to
    invoke this (e.g. each time builder executes).
    """
    banner = render_banner(role, stream=stream, force_color=force_color)
    if not banner:
        return ""
    out = stream or sys.stdout
    try:
        out.write(banner + "\n")
        out.flush()
    except Exception:
        return banner
    return banner


def format_agent_badge(
    role: str | None,
    *,
    label: str | None = None,
    stream: TextIO | None = None,
    force_color: bool | None = None,
) -> str:
    """Format a colored badge/marker for *role* (UIX-3 hook).

    Example (ANSI): a compact "[Builder]" block with role foreground/background.
    Falls back to plain "[Label]" when color unsupported or role unknown.
    Flexible API: accepts role or None.
    """
    identity = get_identity(role)
    text = f"[{label or identity.label}]"
    if not supports_color(stream, force=force_color):
        return text
    # Badge: high-contrast marker (fg on role-tinted bg surrogate kept simple/portable).
    return colorize(text, fg=identity.fg, bg=identity.bg, bold=True, stream=stream, force=force_color)


def format_prompt_prefix(
    role: str | None,
    prompt: str = "",
    *,
    label: str | None = None,
    stream: TextIO | None = None,
    force_color: bool | None = None,
) -> str:
    """Return a prompt string prefixed with an agent badge (UIX-3 hook)."""
    badge = format_agent_badge(role, label=label, stream=stream, force_color=force_color)
    if not prompt:
        return badge
    return f"{badge} {prompt}"


def format_input_highlight(
    text: str,
    role: str | None = None,
    *,
    stream: TextIO | None = None,
    force_color: bool | None = None,
) -> str:
    """Apply a soft background/highlight style for input prompt identity (UIX-3).

    Uses a subtle ANSI background when supported; returns *text* unchanged on
    non-TTY/unsupported terminals (no escape codes). Kept flexible and optional.
    """
    if not text:
        return text
    if not supports_color(stream, force=force_color):
        return text
    identity = get_identity(role) if role is not None else None
    # Soft, low-risk highlight: dim/bright-black background, keep foreground readable.
    bg = "bright_black"
    fg = identity.fg if identity is not None else None
    return colorize(text, fg=fg, bg=bg, bold=False, stream=stream, force=force_color)


def clear_render_caches() -> None:
    """Clear in-memory render-related caches.

    Clears cached raw art. Styled banner output is rendered on demand (not cached)
    so environment-dependent ANSI/TTY state is always respected; any future render
    caches should be invalidated here as well.
    """
    try:
        load_agent_art.cache_clear()
    except Exception:
        pass
    cache_clear = getattr(render_banner, "cache_clear", None)
    if callable(cache_clear):
        try:
            cache_clear()
        except Exception:
            pass


__all__ = [
    "AGENT_ART_DIR",
    "AGENT_IDENTITIES",
    "ANSI_RESET",
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
