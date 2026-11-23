"""Command-line entry point for the agent team graph."""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.loader import load_agents
from app.agents.runtime import AgentRuntime
from app.config import PROJECT_ROOT, get_settings
from app.graph import build_graph
from app.i18n import load_preferences, normalize_language, save_preferences, tr
from app.input_reader import (
    ShutdownRequested,
    _agent_running,
    _cancel_event,
    _browse_directory,
    _read_input,
    _shutdown_event,
    add_to_history,
    get_active_agent,
    restore_terminal,
    set_active_agent,
    start_reader,
)
from app.ui import display_banner
from app.tools import make_toolset
from app.state import (
    _backup_path,
    _default_preferences,
    _initialize_documentation,
    _load_preferences,
    _load_workspace,
    _preferences_path,
    _save_preferences,
    _save_workspace,
)
import shutil


def _valid_language_code(lang: str | None) -> str | None:
    norm = normalize_language(lang)
    if not norm:
        return None
    if any(token in norm for token in ("/", "\\", "..", "\x00")):
        return None
    if not all(ch.isalnum() or ch == "-" for ch in norm):
        return None
    if norm.startswith("-") or norm.endswith("-") or "--" in norm:
        return None
    primary = norm.split("-", 1)[0]
    if len(primary) < 2 or len(primary) > 8 or not primary.isalpha():
        return None
    return norm


def _format_interrupt(value: Any) -> str:
    if isinstance(value, dict):
        msg = value.get("message") or "Se requiere entrada del usuario."
        questions = value.get("questions") or []
        lines = [str(msg)]
        for i, q in enumerate(questions, start=1):
            lines.append(f"  {i}. {q}")
        if value.get("question") and not questions:
            lines.append(f"  - {value['question']}")
        return "\n".join(lines)
    return str(value)


def _truncate_text(text: Any, limit: int = 180) -> str:
    s = str(text or "").strip().replace("\n", " ")
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 3)].rstrip() + "..."


def _summarize_review_findings(findings: Any, max_items: int = 3) -> str | None:
    if not isinstance(findings, list) or not findings:
        return None
    blocking = 0
    parts: list[str] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "info").lower()
        msg = _truncate_text(f.get("message") or f.get("detail") or f.get("title") or "", 120)
        is_blocking = bool(f.get("blocking")) or sev in {"high", "critical"}
        if is_blocking:
            blocking += 1
        label = f"{sev}{'/blocking' if is_blocking else ''}"
        if msg:
            parts.append(f"{label}: {msg}")
        else:
            parts.append(label)
        if len(parts) >= max_items:
            break
    summary = f"{len(findings)} hallazgos" + (f", {blocking} bloqueantes" if blocking else "")
    if parts:
        summary += " [" + "; ".join(parts) + ("]" if len(findings) <= max_items else "; ...]")
    return summary


def _summarize_security_findings(findings: Any, max_items: int = 3) -> str | None:
    if not isinstance(findings, list) or not findings:
        return None
    blocking = 0
    parts: list[str] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "info").lower()
        msg = _truncate_text(f.get("message") or f.get("detail") or f.get("title") or "", 120)
        is_blocking = bool(f.get("blocking")) or sev in {"high", "critical"}
        if is_blocking:
            blocking += 1
        label = f"{sev}{'/blocking' if is_blocking else ''}"
        parts.append(f"{label}: {msg}" if msg else label)
        if len(parts) >= max_items:
            break
    summary = f"{len(findings)} hallazgos" + (f", {blocking} bloqueantes" if blocking else "")
    if parts:
        summary += " [" + "; ".join(parts) + ("]" if len(findings) <= max_items else "; ...]")
    return summary


def _summarize_qa_results(results: Any, max_items: int = 3) -> str | None:
    if not isinstance(results, list) or not results:
        return None
    failed = 0
    parts: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        status = str(r.get("status") or ("fail" if r.get("ok") is False else "unknown")).lower()
        name = _truncate_text(r.get("name") or r.get("check") or "qa", 60)
        details = _truncate_text(r.get("details") or r.get("message") or r.get("error") or "", 100)
        is_fail = status in {"fail", "failed", "error"} or r.get("ok") is False
        if is_fail:
            failed += 1
            parts.append(f"{name}:{status}" + (f" ({details})" if details else ""))
        if len(parts) >= max_items:
            break
    summary = f"{len(results)} resultados" + (f", {failed} fallidos" if failed else "")
    if parts:
        summary += " [" + "; ".join(parts) + ("]" if failed <= max_items else "; ...]")
    elif failed == 0:
        summary += " [ok]"
    return summary


def _format_cycle_info(state: dict[str, Any] | None, update: dict[str, Any] | None = None) -> str | None:
    state = state or {}
    update = update or {}
    parts: list[str] = []
    fix = update.get("fix_cycles", state.get("fix_cycles"))
    max_fix = update.get("max_fix_cycles", state.get("max_fix_cycles"))
    if fix is not None and max_fix is not None:
        parts.append(f"fix={fix}/{max_fix}")
    rc = update.get("research_cycles", state.get("research_cycles"))
    mrc = update.get("max_research_cycles", state.get("max_research_cycles"))
    if rc is not None and mrc is not None:
        parts.append(f"research={rc}/{mrc}")
    return " ".join(parts) if parts else None


def _print_node_diagnostics(node: str, update: dict[str, Any], state: dict[str, Any]) -> None:
    if not isinstance(update, dict):
        return
    decision = update.get("last_decision")
    if decision:
        print(f"  decision={decision}")
    reason = update.get("decision_reason") or update.get("error")
    if reason:
        print(f"  razon={_truncate_text(reason, 240)}")
    cycles = _format_cycle_info(state, update)
    if cycles:
        print(f"  ciclos: {cycles}")
    # Findings context: prefer update values (fresh), fallback to state for visibility.
    review = update.get("review_findings") if "review_findings" in update else state.get("review_findings")
    if review is not None:
        rs = _summarize_review_findings(review)
        if rs:
            print(f"  review: {rs}")
    qa = update.get("qa_results") if "qa_results" in update else state.get("qa_results")
    if qa is not None:
        qs = _summarize_qa_results(qa)
        if qs:
            print(f"  qa: {qs}")
    sec = update.get("security_findings") if "security_findings" in update else state.get("security_findings")
    if sec is not None:
        ss = _summarize_security_findings(sec)
        if ss:
            print(f"  security: {ss}")
    if update.get("plan_error"):
        print(f"  plan_error={_truncate_text(update.get('plan_error'), 240)}")


# Node -> UI role mapping (extensible; used for banners and prompt context).
_NODE_ROLE_MAP: dict[str, str] = {
    "builder": "builder",
    "reviewer": "reviewer",
    "security": "security",
    "qa": "qa",
    "research": "research",
    "research_node": "research",
    "translator": "translator",
    "translator_node": "translator",
    "translator_entry": "translator",
    "pm_intake": "pm",
    "pm_plan": "pm",
    "skills_setup": "pm",
    "pm_decide": "pm",
    "commit": "pm",
    "commit_node": "pm",
}

# Roles that display a banner on execution (extensible for future agents).
_BANNER_ON_EXEC_ROLES: set[str] = {"builder"}


def _node_to_role(node: str | None) -> str:
    """Map a graph node name to a UI agent role (defaults to pm)."""
    if not node:
        return "pm"
    return _NODE_ROLE_MAP.get(str(node), "pm")


def _run_graph(app: Any, initial: dict[str, Any], config: dict[str, Any], interactive: bool = True) -> dict[str, Any]:
    payload: Any = initial
    last_state: dict[str, Any] = {}
    prev_agent = get_active_agent()
    current_role = "pm"
    try:
        while True:
            for event in app.stream(payload, config=config, stream_mode="updates"):
                if _cancel_event.is_set():
                    # Cancel at step boundary: the checkpointer holds the last
                    # completed node's state.
                    print("\nEjecución cancelada por el usuario (ESC×2).")
                    break
                if isinstance(event, dict):
                    for node, update in event.items():
                        current_role = _node_to_role(str(node))
                        set_active_agent(current_role)
                        # Show configured role banners each time the agent executes.
                        # stream_mode="updates" emits once per node execution/step.
                        if current_role in _BANNER_ON_EXEC_ROLES:
                            display_banner(current_role)
                        print(f"[nodo] {node}")
                        if isinstance(update, dict):
                            phase = update.get("phase")
                            status = update.get("status")
                            if phase or status:
                                print(f"  fase={phase} estado={status}")
                            # Surface decision/fix/block diagnostics for observability.
                            # Read latest snapshot context so summaries reflect current state.
                            snap_values = {}
                            try:
                                snap_values = app.get_state(config).values or {}
                            except Exception:
                                snap_values = last_state if isinstance(last_state, dict) else {}
                            _print_node_diagnostics(str(node), update, snap_values if isinstance(snap_values, dict) else {})
                    last_state = app.get_state(config).values or last_state
                else:
                    print(f"[evento] {event}")
            snapshot = app.get_state(config)
            interrupts = list(getattr(snapshot, "interrupts", None) or [])
            state_values = getattr(snapshot, "values", None) or last_state
            if not interrupts:
                return state_values if isinstance(state_values, dict) else {}
            if not interactive:
                print("Interrupción pendiente en modo no interactivo.")
                return state_values if isinstance(state_values, dict) else {}
            values = [getattr(i, "value", i) for i in interrupts]
            print(_format_interrupt(values[0] if len(values) == 1 else values))
            # Associate interrupt prompt with current node/role context when feasible.
            set_active_agent(current_role or "pm")
            try:
                answer = _read_input("Respuesta> ", use_history=True, agent_role=current_role).strip()
            except EOFError:
                print("\nEntrada cerrada.")
                return state_values if isinstance(state_values, dict) else {}
            payload = Command(resume=answer if len(values) == 1 else [answer] * len(values))
    finally:
        set_active_agent(prev_agent)


def _resolve_initial_language(cli_language: str | None, settings) -> tuple[str | None, dict[str, Any]]:
    prefs = load_preferences(Path(settings.workspace_root))
    if cli_language:
        # Validate CLI language with strict graph-equivalent rule before using/persisting.
        return _valid_language_code(cli_language), prefs
    if settings.interaction_language:
        return _valid_language_code(settings.interaction_language), prefs
    pref_lang = _valid_language_code(prefs.get("interaction_language"))
    if pref_lang:
        return pref_lang, prefs
    return None, prefs


_FORBIDDEN_ROOTS = {"/", "/bin", "/boot", "/dev", "/etc", "/home",
                    "/lib", "/lib32", "/lib64", "/proc", "/root",
                    "/run", "/sbin", "/sys", "/usr", "/var"}


def _select_directory(lang: str, state_workspace_root: str | Path | None = None) -> str:
    """Ask user to select or confirm workspace directory.

    Harness selection state is stored under *state_workspace_root* (effective
    workspace-root context) to avoid cwd-relative contamination.
    """
    current_ws = _load_workspace(state_workspace_root)
    ws_prompt = f"Ruta actual: {current_ws or '<ninguna>'}" if current_ws else "No hay directorio de trabajo seleccionado."

    message = (
        tr("dir_select_message", lang) + "\n" +
        ws_prompt + "\n" +
        tr("dir_select_question", lang)
    )

    print(message)
    print("  [s] Select current directory")
    print("  [c] Custom directory")
    print("  [n] New directory (create)")
    print(f"  [e] {tr('dir_browse_option', lang)}")

    while True:
        try:
            answer = _read_input("Respuesta> ", use_history=True).strip().lower()
        except ShutdownRequested:
            print("\nCerrando...")
            raise
        except (EOFError, OSError):
            answer = "s"

        if answer in ("s", "select") or not answer:
            # Use current directory or default to cwd
            # Selecting the project root is the user's explicit consent; no
            # containment check here (it applies to operational paths only).
            ws = current_ws or str(Path.cwd())
            # Validate it's a directory
            if not Path(ws).is_dir():
                print(f"Directorio no existe: {ws}, usando directorio actual.")
                ws = str(Path.cwd())
            if str(Path(ws).expanduser().resolve()) in _FORBIDDEN_ROOTS:
                print(f"Directorio de sistema no permitido como proyecto: {ws}")
                ws = str(Path.cwd())
            _save_workspace(ws, state_workspace_root)
            return ws
        elif answer == "c":
            # Custom directory: any existing directory is accepted as the new
            # project root (user-consented selection), except system dirs.
            try:
                custom = _read_input("Ingresa la ruta del directorio: ", use_history=True).strip()
            except ShutdownRequested:
                print("\nCerrando...")
                raise
            except (EOFError, OSError):
                custom = ""
            custom_path = Path(custom).expanduser().resolve() if custom else None
            if custom_path and str(custom_path) in _FORBIDDEN_ROOTS:
                print(f"Directorio de sistema no permitido como proyecto: {custom_path}")
                print("Directorio inválido, usando directorio actual.")
                ws = str(Path.cwd())
                _save_workspace(ws, state_workspace_root)
                return ws
            if custom_path and custom_path.is_dir():
                _save_workspace(str(custom_path), state_workspace_root)
                return str(custom_path)
            else:
                print("Directorio inválido, usando directorio actual.")
                ws = str(Path.cwd())
                _save_workspace(ws, state_workspace_root)
                return ws
        elif answer == "e":
            try:
                browsed = _browse_directory(current_ws or Path.cwd(), lang)
            except ShutdownRequested:
                print("\nCerrando...")
                raise
            except (EOFError, KeyboardInterrupt, OSError):
                browsed = None
            if browsed is not None:
                try:
                    br = Path(browsed).expanduser().resolve()
                except Exception:
                    br = None
                if br and br.is_dir():
                    if str(br) in _FORBIDDEN_ROOTS:
                        print(f"Directorio de sistema no permitido como proyecto: {br}")
                    else:
                        _save_workspace(str(br), state_workspace_root)
                        return str(br)
                print("Directorio inválido.")
                continue
            else:
                print(tr("dir_browse_cancelled", lang))
                continue
        elif answer == "n":
            # New directory: created anywhere the user can write (explicit choice);
            # operational containment comes from the effective workspace_root
            # passed to runtime/tools (effective_settings); selecting the
            # project is user consent.
            try:
                new_dir = _read_input("Nombre para nuevo directorio: ", use_history=True).strip()
            except ShutdownRequested:
                print("\nCerrando...")
                raise
            except (EOFError, OSError):
                new_dir = ""
            if new_dir:
                new_path = Path(new_dir).expanduser().resolve()
                if str(new_path) in _FORBIDDEN_ROOTS:
                    print(f"Directorio de sistema no permitido como proyecto: {new_path}")
                    ws = str(Path.cwd())
                    _save_workspace(ws, state_workspace_root)
                    return ws
                try:
                    new_path.mkdir(parents=True, exist_ok=True)
                except (OSError, FileExistsError) as exc:
                    print(f"No se pudo crear el directorio: {exc}")
                    ws = str(Path.cwd())
                    _save_workspace(ws, state_workspace_root)
                    return ws
                _save_workspace(str(new_path), state_workspace_root)
                return str(new_path)
            else:
                ws = str(Path.cwd())
                _save_workspace(ws, state_workspace_root)
                return ws
        else:
            print("Opción inválida, seleccione s/c/n/e.")


def _run_check(settings) -> tuple[bool, list[str]]:
    """Validate environment/config and agent loading without running the graph."""
    messages: list[str] = []
    ok = True
    try:
        ws = Path(getattr(settings, "workspace_root", Path.cwd())).expanduser().resolve()
        messages.append(f"workspace_root: {ws}")
        agents = load_agents(ws)
        messages.append(f"agents_loaded: {len(agents)} ({', '.join(sorted(agents))})")
        runtime = AgentRuntime(settings=settings, agents=agents, workspace_root=ws)
        for role in sorted(agents):
            try:
                spec = runtime.get_spec(role)
                # Basic toolset build check (no network, no LLM probe).
                consent = {"decision": "deny", "allow_websearch": False, "allowed_urls": []} if role == "research" else None
                toolset = make_toolset(spec, ws, settings, web_consent=consent)
                messages.append(f"toolset_ok: {role} ({len(toolset)} tools)")
            except Exception as exc:  # noqa: BLE001
                ok = False
                messages.append(f"toolset_error: {role}: {exc}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        messages.append(f"check_error: {exc}")
    return ok, messages


def _log_failure(where: str, exc: BaseException | None = None, message: str | None = None) -> None:
    """Append a timestamped failure entry to .agents/harness/session.log.

    Never raises: any IO error is swallowed so logging can never break the
    program.
    """
    try:
        log_dir = Path(".agents/harness")
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        log_path = log_dir / "session.log"
        lines: list[str] = [f"{datetime.now().isoformat()} where={where}"]
        if message:
            lines.append(f"message: {message}")
        if exc is not None:
            lines.append(f"error_type: {type(exc).__name__}")
            lines.append(f"error_repr: {exc!r}")
            lines.append(f"error_message: {exc}")
            try:
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=5))
                if tb.strip():
                    lines.append("traceback:")
                    lines.append(tb.rstrip())
            except Exception:  # noqa: BLE001
                pass
        with log_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _manage_specs_versions(workspace_root: str, interaction_language: str) -> None:
    """
    Gestiona el versionado de project_specs.md según el idioma.

    Reglas:
    - Si interaction_language es 'en': project_specs.md SÍ se versiona (include in git)
    - Si interaction_language NO es 'en': project_specs.md localized NO se versiona (exclude from git)

    Additionally ensures the files exist minimally.
    """
    safe_lang = _valid_language_code(interaction_language)
    if not safe_lang:
        return

    workspace_path = Path(workspace_root).expanduser().resolve()
    docs_dir = (workspace_path / 'docs').resolve()
    if docs_dir != workspace_path and workspace_path not in docs_dir.parents:
        return
    specs_english = docs_dir / 'project_specs.md'
    specs_localized = (docs_dir / f'project_specs.{safe_lang}.md').resolve()
    if docs_dir not in specs_localized.parents:
        return
    gitignore = workspace_path / '.gitignore'

    # Obtener contenido actual de .gitignore
    gitignore_text = ''
    if gitignore.exists():
        try:
            gitignore_text = gitignore.read_text(encoding='utf-8')
        except Exception:  # noqa: BLE001
            gitignore_text = ''

    if safe_lang == 'en':
        # EN: project_specs.md SÍ se versiona
        specs_english.parent.mkdir(parents=True, exist_ok=True)
        if not specs_english.exists():
            specs_english.write_text('# Project Specs (English)\n\n*English version of project specifications*', encoding='utf-8')

        # Quitar patrón de localized de .gitignore si existe
        if 'project_specs.' in gitignore_text:
            lines = gitignore_text.splitlines()
            filtered = [l for l in lines if 'project_specs' not in l]
            gitignore_text = '\n'.join(filtered) + '\n' if filtered else ''
            try:
                gitignore.write_text(gitignore_text, encoding='utf-8')
            except Exception:  # noqa: BLE001
                pass
    else:
        # NO EN: project_specs.md localized NO se versiona
        specs_localized.parent.mkdir(parents=True, exist_ok=True)
        if not specs_localized.exists():
            specs_localized.write_text(
                f'# Project Specs - {safe_lang.upper()} Version\n\n'
                f'*Archivo localizado automáticamente*\n',
                encoding='utf-8'
            )

        # Agregar patrón de exclusion a .gitignore
        pattern = f'docs/project_specs.{safe_lang}.md'
        if pattern not in gitignore_text:
            if gitignore_text and not gitignore_text.endswith('\n'):
                gitignore_text += '\n'
            gitignore_text += pattern + '\n'
            try:
                gitignore.write_text(gitignore_text, encoding='utf-8')
            except Exception:  # noqa: BLE001
                pass


_FALLBACK_GITIGNORE_ENTRIES = [
    "venv/",
    ".venv/",
    ".env",
    ".env.*",
    "!.env.example",
    "*.pem",
    "*.key",
    "__pycache__/",
    "*.pyc",
    ".agents/",
    "*.db",
    ".pytest_cache/",
]


def _normalize_gitignore_entry(entry: str) -> str:
    return entry.strip().rstrip("/").strip().lower()


def _ensure_gitignore_for_agents(workspace_path) -> None:
    """Ensure the harness's standard gitignore entries exist in workspace ``.gitignore``.

    Never raises: any IO error is swallowed so this can never break startup.
    """
    try:
        if not workspace_path:
            ws = Path.cwd()
        else:
            ws = Path(workspace_path)
        ws = ws.expanduser().resolve()
    except Exception:  # noqa: BLE001
        try:
            ws = Path.cwd().resolve()
        except Exception:  # noqa: BLE001
            return

    try:
        # Ensure the ignored directory actually exists.
        try:
            (ws / ".agents").mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

        # Determine harness entries.
        entries: list[str] | None = None
        try:
            pg = Path(PROJECT_ROOT) / ".gitignore"
            if pg.exists() and pg.is_file():
                parsed = [
                    line.strip()
                    for line in pg.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if parsed:
                    entries = parsed
        except Exception:  # noqa: BLE001
            entries = None
        if not entries:
            entries = list(_FALLBACK_GITIGNORE_ENTRIES)

        gitignore = ws / ".gitignore"
        try:
            if gitignore.exists():
                try:
                    text = gitignore.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    text = ""
                lines = text.splitlines()

                def _present(entry: str) -> bool:
                    ne = _normalize_gitignore_entry(entry)
                    if not ne:
                        return True
                    for line in lines:
                        s = line.strip()
                        if not s:
                            continue
                        nl = s.rstrip("/").strip().lower()
                        if nl == ne:
                            return True
                    return False

                missing = [e for e in entries if not _present(e)]
                if missing:
                    if text and not text.endswith("\n"):
                        text += "\n"
                    for e in missing:
                        text += e + "\n"
                    try:
                        gitignore.write_text(text, encoding="utf-8")
                    except Exception:  # noqa: BLE001
                        pass
            else:
                try:
                    content = "".join(e + "\n" for e in entries)
                    gitignore.write_text(content, encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _session_startup(
    settings: Any | None = None,
    *,
    non_interactive: bool = False,
    cli_language: str | None = None,
) -> dict[str, Any]:
    """Manejar inicio de sesión.

    In non-interactive mode: no interactive prompts/menus. Uses the effective
    workspace root (settings.workspace_root) deterministically and loads/saves
    harness preferences scoped to that workspace, preventing cwd contamination.

    In interactive mode: preserves session/workspace selection UX, but scopes
    harness state to the effective workspace-root context.
    """
    try:
        effective_root = None
        if settings is not None:
            effective_root = getattr(settings, "workspace_root", None)
        if effective_root is None:
            effective_root = Path.cwd()
        effective_root = Path(effective_root).expanduser().resolve()

        if non_interactive:
            # Deterministic, workspace-scoped startup: no prompts/blocking.
            preferences = _load_preferences(effective_root)
            prefs_i18n = load_preferences(effective_root)
            # Resolve language from CLI > settings > prefs (workspace-scoped).
            resolved_lang = _valid_language_code(cli_language)
            if not resolved_lang and settings is not None:
                resolved_lang = _valid_language_code(getattr(settings, "interaction_language", None))
            if not resolved_lang:
                resolved_lang = _valid_language_code(preferences.get("interaction_language"))
            if not resolved_lang:
                resolved_lang = _valid_language_code(prefs_i18n.get("interaction_language"))
            if not resolved_lang:
                resolved_lang = "en"
            preferences["interaction_language"] = resolved_lang

            ws_path = str(effective_root)
            preferences["selected_workspace"] = ws_path
            # Ensure workspace artifacts/docs/gitignore exist (non-interactive safe).
            try:
                doc_result = _initialize_documentation(ws_path)
                if doc_result.get("status") not in ("documentation_already_present", "docs_skipped_symlink"):
                    print(f"Documentación inicializada en: {doc_result.get('docs_dir')}")
            except Exception:
                pass
            _ensure_gitignore_for_agents(ws_path)
            try:
                _manage_specs_versions(ws_path, resolved_lang)
            except Exception:
                pass
            # Persist deterministic workspace/language selection for this workspace.
            try:
                _save_workspace(ws_path, effective_root)
                _save_preferences(preferences, effective_root)
            except Exception:
                pass
            return preferences

        # Interactive startup: workspace-scoped state, existing UX preserved.
        preferences = _load_preferences(effective_root)

        # Get interaction language from preferences
        lang = preferences.get("interaction_language", "en")

        # Directory selection: check if there's a current workspace in this context
        current_ws = _load_workspace(effective_root)
        if not current_ws:
            ws_path = _select_directory(lang, effective_root)
            doc_result = _initialize_documentation(ws_path)
            print(f"Documentación inicializada en: {doc_result.get('docs_dir')}")
            _ensure_gitignore_for_agents(ws_path)
            # Mantener la copia in-memory de preferences sincronizada con el proyecto seleccionado.
            preferences["selected_workspace"] = ws_path
            # Nueva carpeta de proyecto seleccionada en esta sesión.
            preferences["_workspace_new"] = True
        else:
            ws_path = current_ws
            print(f"{tr('session_workspace', lang)}: {ws_path}")

        # Ensure interaction language is set in preferences
        if not preferences.get("interaction_language"):
            preferences["interaction_language"] = lang

        # Manage spec versions based on language
        _manage_specs_versions(ws_path or '', lang)

        # Always show session choices, regardless of saved state
        session_options = [
            f"  {tr('session_resume', lang)}",
            f"  {tr('session_new', lang)}",
            f"  {tr('session_restore', lang)}",
        ]
        # Add change directory option if workspace exists
        if current_ws:
            session_options.insert(1, f"  (c) {tr('dir_select_message', lang)}")

        print(tr("session_resume_message", lang))
        print(f"  - {tr('session_language', lang)}: {preferences.get('interaction_language')}")
        print(f"  - {tr('session_task_index', lang)}: {preferences.get('last_task_index')}")
        print(tr("session_choose", lang))
        print("\n".join(session_options))
        try:
            answer = _read_input("Respuesta> ", use_history=True).strip().lower()
        except ShutdownRequested:
            print("\nCerrando...")
            raise
        except (EOFError, OSError):
            answer = "n"

        if answer in ("s", "resume"):
            _ensure_gitignore_for_agents(ws_path)
            last_task_index = preferences.get("last_task_index") or 0
            if last_task_index > 0:
                print(tr("session_pending_summary", lang).format(index=last_task_index))
                try:
                    cont = _read_input(tr("session_continue_question", lang), use_history=True).strip().lower()
                except ShutdownRequested:
                    print("\nCerrando...")
                    raise
                except (EOFError, OSError):
                    cont = ""
                if cont not in ("s", "si", "sí", "y", "yes"):
                    _save_preferences(preferences, effective_root)
                    return _default_preferences()
            return preferences
        elif answer == "n":
            # Nueva sesión - pero primero respaldar
            _save_preferences(preferences, effective_root)  # Esto crea backup automático
            # Reiniciar preferences a defaults
            return _default_preferences()
        elif answer == "c":
            # Cambiar directorio
            ws_path = _select_directory(lang, effective_root)
            doc_result = _initialize_documentation(ws_path)
            print(f"Documentación inicializada en: {doc_result.get('docs_dir')}")
            _ensure_gitignore_for_agents(ws_path)
            _save_workspace(ws_path, effective_root)
            # Mantener la copia in-memory de preferences sincronizada con el proyecto seleccionado.
            preferences["selected_workspace"] = ws_path
            # Carpeta de proyecto nueva/cambiada en esta sesión.
            preferences["_workspace_new"] = True
            preferences["_dir_changed"] = True
            return preferences
        elif answer == "b":
            # Restaurar desde backup (workspace-scoped)
            backup_path = _backup_path(effective_root)
            prefs_path = _preferences_path(effective_root)
            if backup_path.exists():
                try:
                    restored = json.loads(backup_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    return _default_preferences()
                if not isinstance(restored, dict):
                    return _default_preferences()
                lang_val = _valid_language_code(str(restored.get("interaction_language", "")))
                if lang_val:
                    restored["interaction_language"] = lang_val
                else:
                    restored["interaction_language"] = "en"
                # Mover backup a preferences principal
                try:
                    prefs_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_path, prefs_path)
                except Exception:
                    pass
                return restored
            else:
                # No hay backup, crear nueva
                return _default_preferences()
        else:
            # Unrecognized input: default to starting a fresh session
            return _default_preferences()

    except ShutdownRequested:
        raise
    except EOFError:
        raise
    except Exception as exc:  # noqa: BLE001
        _log_failure("session_startup", exc=exc)
        raise

def _run_docs_head_check(workspace_root: str | Path) -> dict[str, Any] | None:
    """Run app/tools/check_docs_head.py for *workspace_root* and parse JSON."""
    ws = str(Path(workspace_root).expanduser().resolve())
    script = Path(PROJECT_ROOT) / "app" / "tools" / "check_docs_head.py"
    try:
        if not script.is_file():
            return None
        res = subprocess.run(
            [sys.executable, str(script), "--workspace", ws, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        out = (res.stdout or "").strip()
        if not out:
            return None
        data = json.loads(out)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _persist_last_access_head(workspace_root: str | Path, head: str | None) -> None:
    """Persist last_access_head in .agents/research/state.json (best effort)."""
    if not head:
        return
    try:
        from app.research.writer import read_research_state, write_research_state

        st = read_research_state(workspace_root)
        if not isinstance(st, dict):
            st = {}
        st["last_access_head"] = str(head)
        write_research_state(workspace_root, st)
    except Exception:
        pass


def _print_onboarding_result(result: dict[str, Any]) -> None:
    status = str(result.get("status") or "unknown")
    print(f"Research status: {status}")
    written = result.get("written") if isinstance(result, dict) else None
    notice = None
    if isinstance(written, dict):
        notice = written.get("localized_notice")
        if not notice:
            st = written.get("state")
            if isinstance(st, dict):
                notice = st.get("localized_specs_pending_notice")
    if not notice and isinstance(result, dict):
        st = result.get("state")
        if isinstance(st, dict):
            notice = st.get("localized_specs_pending_notice")
    if notice:
        print(str(notice))


def _run_deep_analysis_with_loop(
    runtime: Any,
    effective_settings: Any,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Run deep analysis interactively with valuation/retry loop.

    On stop without successful completion, generates partial/incomplete artifacts
    via DA-2 helpers (when possible) and returns the last result.
    """
    lang = state.get("interaction_language", "en") or "en"
    ws = getattr(effective_settings, "workspace_root", None)
    if ws is None:
        return None
    try:
        from app.research.deep_analysis import generate_partial_artifacts, run_deep_analysis
    except Exception as exc:  # noqa: BLE001
        print(tr("deep_analysis_failed_notice", lang).format(error=type(exc).__name__))
        return None

    last_result: dict[str, Any] | None = None
    while True:
        if _shutdown_event.is_set():
            raise ShutdownRequested()
        print(tr("deep_analysis_running", lang))
        _agent_running.set()
        try:
            result = run_deep_analysis(
                ws,
                settings=effective_settings,
                runtime=runtime,
                interaction_language=lang,
            )
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "failed",
                "quality_ok": False,
                "quality_errors": ["runtime_error"],
                "reason": str(exc),
                "retryable": True,
            }
        finally:
            _agent_running.clear()
            _cancel_event.clear()
        last_result = result if isinstance(result, dict) else {"status": "failed", "quality_ok": False}

        status = str(last_result.get("status") or "")
        quality_ok = bool(last_result.get("quality_ok"))
        qerrs = list(last_result.get("quality_errors") or [])
        reason = str(last_result.get("reason") or (qerrs[0] if qerrs else "unknown"))

        # Automatic success path: ask user for quality valuation.
        if quality_ok and status in ("complete", "complete_with_pending_summary"):
            print(tr("deep_analysis_done", lang))
            try:
                val = _read_input(tr("deep_analysis_quality_question", lang), use_history=True).strip().lower()
            except ShutdownRequested:
                # best-effort partials on abrupt stop
                try:
                    generate_partial_artifacts(ws, reason="cancelled", settings=effective_settings, interaction_language=lang)
                    print(tr("deep_analysis_partial_notice", lang))
                except Exception:
                    pass
                raise
            except (EOFError, OSError):
                val = "s"
            if val in ("s", "y", "yes", "si", "sí"):
                return last_result
            # user rejected quality -> retry loop
            last_result = {**last_result, "quality_ok": False, "quality_errors": [*qerrs, "user_rejected"], "reason": "user_rejected"}

        # Failure / quality insufficient (automatic or user rejection): warn and ask retry.
        if "runtime_unavailable" in qerrs or reason == "runtime_unavailable":
            print(tr("deep_analysis_failed_notice", lang).format(error=reason))
            prompt_key = "deep_analysis_retry_unavailable"
        else:
            print(tr("deep_analysis_failed_notice", lang).format(error=reason))
            prompt_key = "deep_analysis_retry_prompt"
        try:
            retry_ans = _read_input(tr(prompt_key, lang), use_history=True).strip().lower()
        except ShutdownRequested:
            try:
                generate_partial_artifacts(ws, reason=reason or "cancelled", settings=effective_settings, interaction_language=lang)
                print(tr("deep_analysis_partial_notice", lang))
            except Exception:
                pass
            raise
        except (EOFError, OSError):
            retry_ans = "n"

        if retry_ans in ("s", "y", "yes", "si", "sí"):
            continue

        # Stop without successful completion: ensure partial/incomplete artifacts generated.
        try:
            partial = generate_partial_artifacts(
                ws,
                reason=reason or "stopped",
                settings=effective_settings,
                interaction_language=lang,
            )
            last_result = partial if isinstance(partial, dict) else last_result
            print(tr("deep_analysis_partial_notice", lang))
        except Exception:
            pass
        return last_result


def _prompt_analysis_choice(lang: str) -> str:
    """Return 'normal', 'deep', or 'skip'."""
    print(tr("analysis_choice_message", lang))
    print(f"  {tr('analysis_choice_normal', lang)}")
    print(f"  {tr('analysis_choice_deep', lang)}")
    print(f"  {tr('analysis_choice_skip', lang)}")
    while True:
        try:
            ans = _read_input("> ", use_history=True).strip().lower()
        except ShutdownRequested:
            raise
        except (EOFError, OSError):
            return "skip"
        if ans in ("n", "normal", "onboarding"):
            return "normal"
        if ans in ("d", "deep", "profundo"):
            return "deep"
        if ans in ("s", "skip", "omitir"):
            return "skip"
        # be tolerant: also allow 'o' not necessary
        print(tr("invalid_option", lang))


def _verify_workspace_docs_alignment(
    runtime: Any,
    effective_settings: Any,
    state: dict[str, Any],
    *,
    interactive: bool = True,
    refresh_research: bool = False,
    skip_onboarding: bool = False,
) -> None:
    """Verify docs/HEAD alignment on workspace load (T2).

    Interactive: prompt to analyze on first load, or update on misalignment.
    Non-interactive: emit notices and continue (honor --refresh-research).
    Always persists last_access_head when a current HEAD is known.
    """
    ws = Path(getattr(effective_settings, "workspace_root", Path.cwd())).expanduser().resolve()
    lang = state.get("interaction_language", "en") or "en"
    check = _run_docs_head_check(ws)
    if not isinstance(check, dict):
        return

    status = str(check.get("status") or "")
    message = str(check.get("message") or "")
    git_info = check.get("git") or {}
    current_head = git_info.get("current_head") if isinstance(git_info, dict) else None
    is_first_load = status == "no_docs"
    is_misaligned = status == "misaligned"

    # Honor skip-onboarding: record last access and avoid prompts/refresh.
    if skip_onboarding:
        _persist_last_access_head(ws, current_head)
        return

    did_refresh = False
    if interactive:
        if is_first_load:
            try:
                choice = _prompt_analysis_choice(lang)
            except ShutdownRequested:
                print("\nCerrando...")
                raise

            def _run_normal_first_load() -> None:
                nonlocal did_refresh
                print(tr("research_running", lang))
                _agent_running.set()
                try:
                    from app.research.onboarding import run_onboarding

                    result = run_onboarding(
                        ws,
                        settings=effective_settings,
                        runtime=runtime,
                        refresh=bool(refresh_research),
                        skip=False,
                        interaction_language=state.get("interaction_language", "en"),
                    )
                    _print_onboarding_result(result)
                    did_refresh = True
                except Exception as exc:  # noqa: BLE001
                    print(tr("research_failed_notice", lang).format(error=type(exc).__name__))
                finally:
                    _agent_running.clear()
                    _cancel_event.clear()

            if choice == "skip":
                pass
            elif choice == "deep":
                _run_normal_first_load()
                res = _run_deep_analysis_with_loop(runtime, effective_settings, state)
                if res and res.get("written"):
                    did_refresh = True
            else:  # normal
                _run_normal_first_load()
                try:
                    ans = _read_input(tr("deep_analysis_ask_after_normal", lang), use_history=True).strip().lower()
                except ShutdownRequested:
                    print("\nCerrando...")
                    raise
                except (EOFError, OSError):
                    ans = "n"
                if ans in ("s", "y", "yes", "si", "sí"):
                    res = _run_deep_analysis_with_loop(runtime, effective_settings, state)
                    if res and res.get("written"):
                        did_refresh = True
        elif is_misaligned:
            print(tr("docs_misaligned_notice", lang).format(status=status, message=message))
            print(tr("docs_misaligned_question", lang))
            try:
                answer = _read_input("Respuesta> ", use_history=True).strip().lower()
            except ShutdownRequested:
                print("\nCerrando...")
                raise
            except (EOFError, OSError):
                answer = "n"
            if answer in ("s", "y", "yes", "si", "sí"):
                print(tr("research_running", lang))
                _agent_running.set()
                try:
                    from app.research.onboarding import run_onboarding

                    result = run_onboarding(
                        ws,
                        settings=effective_settings,
                        runtime=runtime,
                        refresh=True,
                        skip=False,
                        interaction_language=state.get("interaction_language", "en"),
                    )
                    _print_onboarding_result(result)
                    did_refresh = True
                except Exception as exc:  # noqa: BLE001
                    print(tr("research_failed_notice", lang).format(error=type(exc).__name__))
                finally:
                    _agent_running.clear()
                    _cancel_event.clear()
    else:
        # Non-interactive: no prompts; honor --refresh-research, else emit avisos.
        if refresh_research and (is_first_load or is_misaligned):
            _agent_running.set()
            try:
                from app.research.onboarding import run_onboarding

                result = run_onboarding(
                    ws,
                    settings=effective_settings,
                    runtime=runtime,
                    refresh=True,
                    skip=False,
                    interaction_language=state.get("interaction_language", "en"),
                )
                print(tr("docs_misaligned_notice", lang).format(status=status, message=f"refreshed: {result.get('status')}"))
                _print_onboarding_result(result)
                did_refresh = True
            except Exception as exc:  # noqa: BLE001
                print(tr("research_failed_notice", lang).format(error=type(exc).__name__))
            finally:
                _agent_running.clear()
                _cancel_event.clear()
        elif is_first_load or is_misaligned:
            print(tr("docs_misaligned_notice", lang).format(status=status, message=message))

    # Persist last_access_head on every load/verification when known.
    # Re-check current head if refresh changed state; fallback to prior check.
    head_to_store = current_head
    if did_refresh:
        recheck = _run_docs_head_check(ws)
        if isinstance(recheck, dict):
            git2 = recheck.get("git") or {}
            if isinstance(git2, dict) and git2.get("current_head"):
                head_to_store = git2.get("current_head")
    _persist_last_access_head(ws, head_to_store)


def _offer_research(runtime: Any, effective_settings: Any, state: dict[str, Any], interactive: bool = True) -> None:
    """Offer analysis for a new workspace (interactive): normal/deep/skip.

    Deep runs onboarding base as appropriate then deep analysis. Normal runs
    onboarding then asks if deep analysis is desired. Skip preserves current
    behavior (no analysis). Propagates ShutdownRequested.
    """
    if not interactive:
        return
    lang = state.get("interaction_language", "en") or "en"
    try:
        choice = _prompt_analysis_choice(lang)
    except ShutdownRequested:
        print("\nCerrando...")
        raise

    if choice == "skip":
        return

    def _run_normal() -> dict[str, Any] | None:
        print(tr("research_running", lang))
        _agent_running.set()
        try:
            from app.research.onboarding import run_onboarding

            result = run_onboarding(
                effective_settings.workspace_root,
                settings=effective_settings,
                runtime=runtime,
                refresh=False,
                skip=False,
                interaction_language=state.get("interaction_language", "en"),
            )
            _print_onboarding_result(result)
            return result
        except Exception as exc:  # noqa: BLE001
            print(tr("research_failed_notice", lang).format(error=type(exc).__name__))
            return None
        finally:
            _agent_running.clear()
            _cancel_event.clear()

    if choice == "deep":
        # Onboarding base as appropriate per DA-2, then deep analysis.
        _run_normal()
        _run_deep_analysis_with_loop(runtime, effective_settings, state)
        return

    # normal
    _run_normal()
    try:
        ans = _read_input(tr("deep_analysis_ask_after_normal", lang), use_history=True).strip().lower()
    except ShutdownRequested:
        print("\nCerrando...")
        raise
    except (EOFError, OSError):
        ans = "n"
    if ans in ("s", "y", "yes", "si", "sí"):
        _run_deep_analysis_with_loop(runtime, effective_settings, state)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python main.py``."""
    parser = argparse.ArgumentParser(description="CLI de orquestación del equipo de agentes.")
    parser.add_argument("requirement", nargs="?", help="Requerimiento inicial (opcional).")
    parser.add_argument("--language", help="Interaction language code (e.g. en, es).")
    parser.add_argument("--non-interactive", action="store_true", help="No pedir entrada en interrupciones.")
    web_group = parser.add_mutually_exclusive_group()
    web_group.add_argument("--allow-web", action="store_true", help="Allow research web access for this run (non-interactive consent).")
    web_group.add_argument("--no-web", action="store_true", help="Deny research web access for this run.")
    parser.add_argument("--refresh-research", action="store_true", help="Force project research/onboarding regeneration for this run.")
    parser.add_argument("--skip-onboarding", action="store_true", help="Skip research onboarding for this run.")
    parser.add_argument("--research", help="On-demand research question for this run (seeds needs_research).")
    parser.add_argument("--check", action="store_true", help="Validate environment/config and agent loading without running the graph.")
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
        # ESC×2 reader: active only on a tty (non-tty falls back to input()).
        start_reader()
        # CLI/menu/general prompts default to PM unless tied to a specific agent step.
        set_active_agent("pm")
        state = _session_startup(
            settings,
            non_interactive=bool(getattr(args, "non_interactive", False)),
            cli_language=getattr(args, "language", None),
        )

        # Handle directory change from session menu (interactive only): re-present
        # the session menu if the user changed the workspace directory.
        if not getattr(args, "non_interactive", False):
            max_dir_changes = 2
            dir_changes = 0
            while state.get("_dir_changed") and dir_changes < max_dir_changes:
                dir_changes += 1
                state = _session_startup(
                    settings,
                    non_interactive=False,
                    cli_language=getattr(args, "language", None),
                )
        # Read + clear transient flags before passing state to the graph.
        workspace_new = bool(state.get("_workspace_new"))
        state = {k: v for k, v in state.items() if k not in ("_dir_changed", "_workspace_new")}

        # Effective workspace: deterministic in non-interactive mode (WORKSPACE_ROOT/
        # settings.workspace_root). In interactive mode, use selected workspace from
        # workspace-scoped harness preferences, falling back to settings.workspace_root.
        if getattr(args, "non_interactive", False):
            selected_ws = str(Path(getattr(settings, "workspace_root", Path.cwd())).expanduser().resolve())
        else:
            selected_ws = _load_workspace(getattr(settings, "workspace_root", None))
            if not selected_ws:
                # fallback to in-memory selection or settings root
                selected_ws = state.get("selected_workspace") or str(
                    Path(getattr(settings, "workspace_root", Path.cwd())).expanduser().resolve()
                )
        effective_settings = dataclasses.replace(settings, workspace_root=Path(selected_ws).expanduser().resolve())

        if getattr(args, "check", False):
            ok, messages = _run_check(effective_settings)
            for msg in messages:
                print(msg)
            print("check: OK" if ok else "check: FAILED")
            return 0 if ok else 1

        try:
            runtime = AgentRuntime(settings=effective_settings, workspace_root=effective_settings.workspace_root)
        except Exception as exc:  # noqa: BLE001
            print(f"Error al cargar el runtime de agentes: {exc}", file=sys.stderr)
            return 1
        graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime, settings=effective_settings)

        research_question = (getattr(args, "research", None) or "").strip() or None

        # Docs/HEAD alignment verification on every effective workspace load.
        if workspace_new:
            # Keep explicit "new workspace" research consent prompt for this session.
            _offer_research(runtime, effective_settings, state, interactive=not args.non_interactive)
            _verify_workspace_docs_alignment(
                runtime,
                effective_settings,
                state,
                interactive=not args.non_interactive,
                refresh_research=bool(getattr(args, "refresh_research", False)),
                skip_onboarding=bool(getattr(args, "skip_onboarding", False)),
            )
        else:
            _verify_workspace_docs_alignment(
                runtime,
                effective_settings,
                state,
                interactive=not args.non_interactive,
                refresh_research=bool(getattr(args, "refresh_research", False)),
                skip_onboarding=bool(getattr(args, "skip_onboarding", False)),
            )

        # Initial menu: visible after workspace is selected/confirmed, before
        # entering the main requirement/graph loop. Skipped in non-interactive mode.
        initial_requirement = ""
        if not args.non_interactive:
            menu_back_commands = {"b", "back", "menu", "/menu", "/back"}
            while True:
                lang = state.get("interaction_language", "en") or "en"
                print()
                print(tr("post_task_prompt", lang))
                print("  (s) Start / continue")
                print(f"  {tr('menu_deep_analysis', lang)}")
                print("  (l) Change language")
                print("  (w) Change workspace")
                print("  (q) Quit")
                try:
                    choice = _read_input("> ", use_history=True).strip().lower()
                except EOFError:
                    choice = "s"
                except ShutdownRequested:
                    raise

                if choice in ("", "s", "start", "continue", "iniciar", "comenzar"):
                    try:
                        req_input = _read_input(tr("enter_requirement_prompt", lang), use_history=True).strip()
                    except EOFError:
                        req_input = ""
                    except ShutdownRequested:
                        raise
                    if not req_input or req_input.strip().lower() in menu_back_commands:
                        continue
                    initial_requirement = req_input
                    break
                elif choice in ("d", "deep", "profundo", "analisis", "análisis"):
                    _run_deep_analysis_with_loop(runtime, effective_settings, state)
                    continue
                elif choice in ("l", "language", "idioma"):
                    try:
                        new_lang = _read_input(tr("enter_language_prompt", lang), use_history=True).strip()
                    except (EOFError, OSError):
                        new_lang = ""
                    resolved = _valid_language_code(new_lang)
                    if resolved:
                        state["interaction_language"] = resolved
                        try:
                            save_preferences(Path(effective_settings.workspace_root), {"interaction_language": resolved})
                        except Exception:
                            pass
                        try:
                            _manage_specs_versions(str(effective_settings.workspace_root), resolved)
                        except Exception:
                            pass
                    else:
                        print(tr("invalid_language_notice", lang), file=sys.stderr)
                elif choice in ("w", "workspace", "directorio"):
                    state_ctx = getattr(settings, "workspace_root", None)
                    try:
                        new_ws = _select_directory(lang, state_ctx)
                    except ShutdownRequested:
                        raise
                    old_ws = str(Path(effective_settings.workspace_root).expanduser().resolve())
                    try:
                        resolved_ws = str(Path(new_ws).expanduser().resolve())
                    except Exception:
                        resolved_ws = new_ws
                    if resolved_ws != old_ws:
                        try:
                            doc_result = _initialize_documentation(resolved_ws)
                            print(f"Documentación inicializada en: {doc_result.get('docs_dir')}")
                        except Exception:
                            pass
                        _ensure_gitignore_for_agents(resolved_ws)
                        try:
                            _manage_specs_versions(resolved_ws, state.get("interaction_language", "en") or "en")
                        except Exception:
                            pass
                        state["selected_workspace"] = resolved_ws
                        _save_workspace(resolved_ws, state_ctx)
                        effective_settings = dataclasses.replace(
                            settings, workspace_root=Path(resolved_ws)
                        )
                        try:
                            runtime = AgentRuntime(
                                settings=effective_settings,
                                workspace_root=effective_settings.workspace_root,
                            )
                        except Exception as exc:  # noqa: BLE001
                            print(f"Error al cargar el runtime de agentes: {exc}", file=sys.stderr)
                            return 1
                        graph = build_graph(
                            checkpointer=InMemorySaver(),
                            runtime=runtime,
                            settings=effective_settings,
                        )
                        _offer_research(runtime, effective_settings, state, interactive=True)
                        _verify_workspace_docs_alignment(
                            runtime,
                            effective_settings,
                            state,
                            interactive=True,
                            refresh_research=bool(getattr(args, "refresh_research", False)),
                            skip_onboarding=bool(getattr(args, "skip_onboarding", False)),
                        )
                elif choice in ("q", "quit", "salir"):
                    return 0
                else:
                    print(tr("invalid_option", lang))

        # Interactive loop: requirement -> graph -> summary, repeatedly, until
        # shutdown (ESC×2 while idle), KeyboardInterrupt or EOF. --check and
        # --non-interactive remain single-pass.
        cli_requirement = (args.requirement or "").strip()
        if not cli_requirement and initial_requirement:
            cli_requirement = initial_requirement
        run_number = 0
        while True:
            if _shutdown_event.is_set():
                print("Cerrando la aplicación.")
                return 0
            requirement = ""
            if cli_requirement:
                # CLI-provided requirement runs on the first iteration only.
                requirement = cli_requirement
                cli_requirement = ""
            elif run_number == 0 and research_question:
                requirement = f"Research: {research_question}"
            if not requirement and not args.non_interactive:
                try:
                    requirement = _read_input("Describe el requerimiento: ", use_history=True).strip()
                except EOFError:
                    # Keep current EOF behavior: exit gracefully.
                    print("No se proporcionó requerimiento.", file=sys.stderr)
                    return 1
                # ShutdownRequested propagates to the top-level handler.
            if not requirement:
                if args.non_interactive:
                    print("No se proporcionó requerimiento.", file=sys.stderr)
                    return 1
                continue  # empty input: re-prompt
            # Apply research seeding only for the first iteration
            is_first_iteration = (run_number == 0)
            run_number += 1
            # Fresh thread_id per iteration: InMemorySaver state must not leak.
            config = {"configurable": {"thread_id": f"cli-thread-{run_number}"}}
            initial = {
                "requirement": requirement,
                "max_fix_cycles": effective_settings.max_fix_cycles,
                "fix_cycles": 0,
                "interaction_language": state.get("interaction_language", "en"),
                "preferences": state,
            }
            # Fixed per-run web access decision from CLI flags (not persisted);
            # parsed once, applied on EVERY iteration.
            if args.allow_web:
                initial["web_access"] = "allow"
                initial["web_consent"] = {"decision": "allow", "allow_websearch": True, "allowed_urls": []}
                initial["pending_web_consent"] = False
            elif args.no_web:
                initial["web_access"] = "deny"
                initial["web_consent"] = {"decision": "deny", "allow_websearch": False, "allowed_urls": []}
                initial["pending_web_consent"] = False
            if getattr(args, "refresh_research", False):
                initial["refresh_research"] = True
            if getattr(args, "skip_onboarding", False):
                initial["skip_onboarding"] = True
            if is_first_iteration and research_question:
                initial["needs_research"] = True
                initial["research_question"] = research_question
                initial["research_return_to"] = "plan"
            # Allow CLI to override interaction language if valid
            if args.language:
                resolved = _valid_language_code(args.language)
                if resolved:
                    initial["interaction_language"] = resolved
                    # Persist explicit CLI choice only when strictly valid.
                    try:
                        save_preferences(Path(effective_settings.workspace_root), {"interaction_language": resolved})
                    except Exception:
                        pass
                else:
                    # Invalid CLI language - print notice but keep state's language
                    print(tr("invalid_language_notice", "en"), file=sys.stderr)
            # State's interaction_language already set from _session_startup()
            _agent_running.set()
            try:
                final_state = _run_graph(graph, initial, config, interactive=not args.non_interactive)
            finally:
                _agent_running.clear()
                _cancel_event.clear()
            
            # Offer options after task completion (non-interactive bypasses this)
            if not args.non_interactive:
                # Check if we should show the post-task menu
                status = final_state.get('status')
                if status in ('next_task', 'done_no_commit', 'commit_skipped'):
                    # Task completed - offer options
                    lang = _valid_language_code(final_state.get('interaction_language')) or state.get('interaction_language') or 'en'
                    while True:
                        print()
                        print(tr('task_completed', lang))
                        print('  (n) Next task')
                        print(f"  {tr('menu_deep_analysis', lang)}")
                        print('  (l) Change language')
                        print('  (w) Change workspace')
                        print('  (q) Quit')
                        try:
                            choice = _read_input('> ', use_history=True).strip().lower()
                        except EOFError:
                            break
                        except ShutdownRequested:
                            raise
                        
                        if choice == '' or choice in ('n', 'next'):
                            break  # Continue to next task
                        elif choice in ('d', 'deep', 'profundo', 'analisis', 'análisis'):
                            _run_deep_analysis_with_loop(runtime, effective_settings, state)
                            continue
                        elif choice in ('l', 'language'):
                            try:
                                new_lang = _read_input(tr('enter_language_prompt', lang), use_history=True).strip()
                            except (EOFError, OSError):
                                new_lang = ""
                            resolved = _valid_language_code(new_lang)
                            if resolved:
                                state['interaction_language'] = resolved
                                try:
                                    save_preferences(Path(effective_settings.workspace_root), {'interaction_language': resolved})
                                except Exception:
                                    pass
                                try:
                                    _manage_specs_versions(str(effective_settings.workspace_root), resolved)
                                except Exception:
                                    pass
                                lang = resolved
                            else:
                                print(tr('invalid_language_notice', lang), file=sys.stderr)
                        elif choice in ('w', 'workspace'):
                            state_ctx = getattr(settings, "workspace_root", None)
                            try:
                                new_ws = _select_directory(lang, state_ctx)
                            except ShutdownRequested:
                                raise
                            old_ws = str(Path(effective_settings.workspace_root).expanduser().resolve())
                            try:
                                resolved_ws = str(Path(new_ws).expanduser().resolve())
                            except Exception:
                                resolved_ws = new_ws
                            if resolved_ws != old_ws:
                                try:
                                    doc_result = _initialize_documentation(resolved_ws)
                                    print(f"Documentación inicializada en: {doc_result.get('docs_dir')}")
                                except Exception:
                                    pass
                                _ensure_gitignore_for_agents(resolved_ws)
                                try:
                                    _manage_specs_versions(resolved_ws, state.get('interaction_language', 'en') or 'en')
                                except Exception:
                                    pass
                                state['selected_workspace'] = resolved_ws
                                _save_workspace(resolved_ws, state_ctx)
                                effective_settings = dataclasses.replace(
                                    settings, workspace_root=Path(resolved_ws)
                                )
                                try:
                                    runtime = AgentRuntime(
                                        settings=effective_settings,
                                        workspace_root=effective_settings.workspace_root,
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    print(f"Error al cargar el runtime de agentes: {exc}", file=sys.stderr)
                                    return 1
                                graph = build_graph(
                                    checkpointer=InMemorySaver(),
                                    runtime=runtime,
                                    settings=effective_settings,
                                )
                                _offer_research(runtime, effective_settings, state, interactive=True)
                                _verify_workspace_docs_alignment(
                                    runtime,
                                    effective_settings,
                                    state,
                                    interactive=True,
                                    refresh_research=bool(getattr(args, "refresh_research", False)),
                                    skip_onboarding=bool(getattr(args, "skip_onboarding", False)),
                                )
                        elif choice in ('q', 'quit'):
                            return 0
                        else:
                            print(tr("invalid_option", lang))
                    else:
                        # Not a task completion state, print normal summary
                        print()
            print("\n=== Resumen final ===")
            print(f"fase: {final_state.get('phase')}")
            print(f"estado: {final_state.get('status')}")
            print(f"hecho: {final_state.get('done')}")
            print(f"bloqueado: {final_state.get('blocked')}")
            if final_state.get("last_decision"):
                print(f"decision: {final_state.get('last_decision')}")
            cycles = _format_cycle_info(final_state)
            if cycles:
                print(f"ciclos: {cycles}")
            if final_state.get("error"):
                print(f"error: {final_state.get('error')}")
            if final_state.get("decision_reason") and final_state.get("decision_reason") != final_state.get("error"):
                print(f"razon: {_truncate_text(final_state.get('decision_reason'), 240)}")
            review_summary = _summarize_review_findings(final_state.get("review_findings"))
            if review_summary:
                print(f"review: {review_summary}")
            qa_summary = _summarize_qa_results(final_state.get("qa_results"))
            if qa_summary:
                print(f"qa: {qa_summary}")
            sec_summary = _summarize_security_findings(final_state.get("security_findings"))
            if sec_summary:
                print(f"security: {sec_summary}")
            docs_precommit = final_state.get("docs_precommit")
            if isinstance(docs_precommit, dict):
                print(f"docs_precommit: {docs_precommit.get('message') or docs_precommit.get('status')}")
                if docs_precommit.get("head"):
                    print(f"docs_last_access_head: {docs_precommit.get('head')}")
            plan = final_state.get("plan") or []
            print(f"tareas: {len(plan)}")
            if args.non_interactive:
                return 0 if not final_state.get("blocked") and not final_state.get("error") else 2
    except ShutdownRequested:
        _log_failure("main.shutdown_requested")
        print("\nCerrando la aplicación.")
        return 0
    except KeyboardInterrupt:
        _log_failure("main.keyboard_interrupt")
        print("\nInterrumpido por el usuario.")
        return 130
    except Exception as exc:  # noqa: BLE001
        _log_failure("main.exception", exc=exc)
        print(f"Error ejecutando el grafo: {exc}", file=sys.stderr)
        return 1
    finally:
        restore_terminal()