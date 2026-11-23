"""Configuration loaded from the environment, plus the LLM factory."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from langchain_openai import ChatOpenAI

# Kept in sync with the previous flat ``config.py``.
REQUEST_TIMEOUT = 120
MAX_RETRIES = 3

DEFAULT_BASE_URL = "http://localhost:20128/v1"
DEFAULT_MODEL = "auto"
DEFAULT_TEMPERATURE = 0.3
DEFAULT_API_KEY = "sk-dummy"

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ``.env`` is read without touching ``os.environ``: the values are used as a
# fallback for this process only, so importing this module never leaks secrets
# into child processes.
_FILE_ENV: dict[str, str | None] = dict(dotenv_values(PROJECT_ROOT / ".env"))

# Deep analysis limits are configurable via .env and unlimited/no-limit by
# default. For the numeric DEEP_ANALYSIS_MAX_* / DEEP_ANALYSIS_TIMEOUT_SECONDS
# settings, a value of 0 (or an empty/unset value, or the strings "unlimited"/
# "none") means "no effective limit". DEEP_ANALYSIS_UNLIMITED defaults to true
# and acts as the global unlimited/no-limit switch for deep analysis workflows.
_DEEP_ANALYSIS_UNLIMITED_TOKENS = {"", "0", "unlimited", "none", "nolimit", "no-limit", "inf"}

# Role name -> optional environment variable holding that role's model.
ROLE_MODEL_ENV_VARS = {
    "pm": "PM_MODEL",
    "builder": "BUILDER_MODEL",
    "reviewer": "REVIEWER_MODEL",
    "qa": "QA_MODEL",
    "security": "SECURITY_MODEL",
    "translator": "TRANSLATOR_MODEL",
    "research": "RESEARCH_MODEL",
}

# Alternative spellings accepted by ``get_llm`` and ``Settings.model_for``.
ROLE_ALIASES = {
    "project-manager": "pm",
    "project_manager": "pm",
    "projectmanager": "pm",
}

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

_VALID_RESEARCH_ONBOARDING = {"auto", "always", "never"}
_VALID_WEB_ACCESS = {"ask", "allow", "deny"}
_VALID_WEBSEARCH_PROVIDER = {"none", "searxng", "tavily", "brave"}


def _raw_env(name: str) -> str | None:
    """Return ``name`` from the process environment, then from ``.env``."""
    value = os.environ.get(name)
    if value is not None:
        return value
    return _FILE_ENV.get(name)


def _env_str(name: str, default: str) -> str:
    value = _raw_env(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _raw_env(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = _raw_env(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _raw_env(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean")


def _require_min(name: str, value: int | float, minimum: int | float) -> None:
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")


def _require_max(name: str, value: int | float, maximum: int | float) -> None:
    if value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")


@dataclass
class Settings:
    """Resolved runtime settings. Never contains anything that may be logged."""

    base_url: str
    api_key: str = field(repr=False)
    model: str
    temperature: float
    pm_model: str
    builder_model: str
    reviewer_model: str
    qa_model: str
    security_model: str
    translator_model: str
    research_model: str
    workspace_root: Path
    max_fix_cycles: int
    max_agent_steps: int
    shell_timeout: int
    interaction_language: str | None
    # Install missing skills automatically. Per-agent skill installation still
    # always requires explicit human approval, regardless of this flag.
    skills_auto_install: bool
    research_onboarding: str
    research_max_files: int
    research_max_bytes: int
    research_context_chars: int
    research_max_cycles: int
    web_access: str
    webfetch_timeout: int
    webfetch_max_bytes: int
    webfetch_allowed_domains: list[str]
    websearch_provider: str
    websearch_endpoint: str | None
    websearch_api_key: str | None = field(default=None, repr=False)
    # Deep analysis workflow limits (DA). All unlimited/no-limit by default.
    # Numeric fields use 0 to mean unlimited/no effective limit.
    deep_analysis_unlimited: bool = True
    deep_analysis_max_files: int = 0
    deep_analysis_max_passes: int = 0
    deep_analysis_max_chars: int = 0
    deep_analysis_timeout_seconds: int = 0
    # CLI/UI presentation and input UX (safe defaults; colors/mouse degrade on
    # non-TTY/unsupported terminals). UI_MOUSE defaults to True to favor requested
    # inline cursor-positioning functionality while remaining best-effort.
    ui_colors: bool = True
    ui_mouse: bool = True

    def model_for(self, role: str | None = None) -> str:
        """Return the model configured for ``role``, falling back to ``model``."""
        if not role:
            return self.model
        key = role.strip().lower()
        key = ROLE_ALIASES.get(key, key)
        if key not in ROLE_MODEL_ENV_VARS:
            return self.model
        return getattr(self, f"{key}_model")


def _env_choice(name: str, default: str, valid: set[str]) -> str:
    value = _env_str(name, default).strip().lower()
    if value not in valid:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(valid))}")
    return value


def _env_csv(name: str) -> list[str]:
    raw = _raw_env(name)
    if raw is None or not raw.strip():
        return []
    items: list[str] = []
    for part in raw.split(","):
        item = part.strip().lower()
        if item and item not in items:
            items.append(item)
    return items


def _env_unlimited_int(name: str, default: int = 0) -> int:
    """Return a non-negative int where 0 means unlimited/no effective limit.

    Empty/unset values, 0, and tokens like "unlimited"/"none"/"no-limit"/"inf"
    all resolve to 0. Positive integers set an explicit limit.
    """
    raw = _raw_env(name)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in _DEEP_ANALYSIS_UNLIMITED_TOKENS:
        return 0
    try:
        value = int(token)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a non-negative integer or unlimited"
        ) from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer or unlimited")
    return value


def _role_models(default_model: str) -> dict[str, str]:
    """Build the per-role model fields from ``ROLE_MODEL_ENV_VARS``."""
    return {
        f"{role}_model": _env_str(env_var, default_model)
        for role, env_var in ROLE_MODEL_ENV_VARS.items()
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Resolve the settings once per process."""
    model = _env_str("OMNIROUTE_MODEL", DEFAULT_MODEL)
    workspace_root = Path(
        _env_str("WORKSPACE_ROOT", str(Path.cwd()))
    ).expanduser().resolve()

    temperature = _env_float("TEMPERATURE", DEFAULT_TEMPERATURE)
    _require_min("TEMPERATURE", temperature, 0.0)
    _require_max("TEMPERATURE", temperature, 2.0)

    max_fix_cycles = _env_int("MAX_FIX_CYCLES", 3)
    _require_min("MAX_FIX_CYCLES", max_fix_cycles, 0)

    max_agent_steps = _env_int("MAX_AGENT_STEPS", 25)
    _require_min("MAX_AGENT_STEPS", max_agent_steps, 1)

    shell_timeout = _env_int("SHELL_TIMEOUT", 120)
    _require_min("SHELL_TIMEOUT", shell_timeout, 1)

    interaction_language_raw = _raw_env("INTERACTION_LANGUAGE")
    interaction_language = (
        interaction_language_raw.strip()
        if interaction_language_raw and interaction_language_raw.strip()
        else None
    )

    research_max_files = _env_int("RESEARCH_MAX_FILES", 40)
    _require_min("RESEARCH_MAX_FILES", research_max_files, 1)
    research_max_bytes = _env_int("RESEARCH_MAX_BYTES", 200000)
    _require_min("RESEARCH_MAX_BYTES", research_max_bytes, 1)
    research_context_chars = _env_int("RESEARCH_CONTEXT_CHARS", 8000)
    _require_min("RESEARCH_CONTEXT_CHARS", research_context_chars, 1)
    research_max_cycles = _env_int("RESEARCH_MAX_CYCLES", 2)
    _require_min("RESEARCH_MAX_CYCLES", research_max_cycles, 0)
    webfetch_timeout = _env_int("WEBFETCH_TIMEOUT", 20)
    _require_min("WEBFETCH_TIMEOUT", webfetch_timeout, 1)
    webfetch_max_bytes = _env_int("WEBFETCH_MAX_BYTES", 512000)
    _require_min("WEBFETCH_MAX_BYTES", webfetch_max_bytes, 1)

    research_onboarding = _env_choice(
        "RESEARCH_ONBOARDING", "auto", _VALID_RESEARCH_ONBOARDING
    )
    web_access = _env_choice("WEB_ACCESS", "ask", _VALID_WEB_ACCESS)
    websearch_provider = _env_choice(
        "WEBSEARCH_PROVIDER", "none", _VALID_WEBSEARCH_PROVIDER
    )
    websearch_endpoint_raw = _raw_env("WEBSEARCH_ENDPOINT")
    websearch_endpoint = (
        websearch_endpoint_raw.strip()
        if websearch_endpoint_raw and websearch_endpoint_raw.strip()
        else None
    )
    websearch_api_key_raw = _raw_env("WEBSEARCH_API_KEY")
    websearch_api_key = (
        websearch_api_key_raw.strip()
        if websearch_api_key_raw and websearch_api_key_raw.strip()
        else None
    )

    # Deep analysis limits: unlimited/no-limit by default. Numeric values use 0
    # to mean unlimited/no effective limit (see _env_unlimited_int).
    deep_analysis_unlimited = _env_bool("DEEP_ANALYSIS_UNLIMITED", True)
    deep_analysis_max_files = _env_unlimited_int("DEEP_ANALYSIS_MAX_FILES", 0)
    deep_analysis_max_passes = _env_unlimited_int("DEEP_ANALYSIS_MAX_PASSES", 0)
    deep_analysis_max_chars = _env_unlimited_int("DEEP_ANALYSIS_MAX_CHARS", 0)
    deep_analysis_timeout_seconds = _env_unlimited_int(
        "DEEP_ANALYSIS_TIMEOUT_SECONDS", 0
    )

    # UI colors/mouse: configurable with safe, functionality-favoring defaults.
    ui_colors = _env_bool("UI_COLORS", True)
    ui_mouse = _env_bool("UI_MOUSE", True)

    return Settings(
        base_url=_env_str("OMNIROUTE_BASE_URL", DEFAULT_BASE_URL),
        api_key=_env_str("OMNIROUTE_API_KEY", DEFAULT_API_KEY),
        model=model,
        temperature=temperature,
        **_role_models(model),
        workspace_root=workspace_root,
        max_fix_cycles=max_fix_cycles,
        max_agent_steps=max_agent_steps,
        shell_timeout=shell_timeout,
        interaction_language=interaction_language,
        skills_auto_install=_env_bool("SKILLS_AUTO_INSTALL", True),
        research_onboarding=research_onboarding,
        research_max_files=research_max_files,
        research_max_bytes=research_max_bytes,
        research_context_chars=research_context_chars,
        research_max_cycles=research_max_cycles,
        web_access=web_access,
        webfetch_timeout=webfetch_timeout,
        webfetch_max_bytes=webfetch_max_bytes,
        webfetch_allowed_domains=_env_csv("WEBFETCH_ALLOWED_DOMAINS"),
        websearch_provider=websearch_provider,
        websearch_endpoint=websearch_endpoint,
        websearch_api_key=websearch_api_key,
        deep_analysis_unlimited=deep_analysis_unlimited,
        deep_analysis_max_files=deep_analysis_max_files,
        deep_analysis_max_passes=deep_analysis_max_passes,
        deep_analysis_max_chars=deep_analysis_max_chars,
        deep_analysis_timeout_seconds=deep_analysis_timeout_seconds,
        ui_colors=ui_colors,
        ui_mouse=ui_mouse,
    )


def get_llm(role: str | None = None) -> ChatOpenAI:
    """Build the chat model pointing at OmniRoute.

    ``role`` selects a per-role model (``pm``, ``builder``, ...); unknown roles
    or ``None`` use ``OMNIROUTE_MODEL``. The API key is handed to the client
    only and is never logged.
    """
    settings = get_settings()
    return ChatOpenAI(
        model=settings.model_for(role),
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=settings.temperature,
        request_timeout=REQUEST_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
