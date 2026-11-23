"""Shared state passed between the agents of the graph."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class TeamState(TypedDict, total=False):
    """State shared by every node.

    ``total=False`` because nodes return partial updates, as LangGraph expects.
    """

    messages: Annotated[list, add_messages]

    # Requirements and clarification loop.
    requirement: str
    normalized_requirements: str
    pending_questions: list[str]
    answers: list[str]

    # Task planning and execution.
    plan: list[dict]
    task_index: int
    current_task: dict | None
    attempts: int
    needs_research: bool
    research_question: str | None
    research_results: list[dict]

    # Verification results.
    artifacts: list[str]
    review_findings: list[dict]
    qa_results: list[dict]
    qa_baseline: dict[str, Any]
    security_findings: list[dict]
    needs_security: bool
    approvals: dict[str, bool]

    # Skills, repository context and outcome.
    skills: dict[str, dict]
    repo: str
    commit_sha: str
    status: str

    # Interaction language and localization.
    interaction_language: str
    language_notice: str | None
    preferences: dict[str, Any]
    pending_user_answers: list[str]
    pending_language_selection: bool
    language_selection_attempts: int
    raw_requirement: str
    raw_answers: list[str]

    # Workspace and documentation.
    selected_workspace: str
    docs_definition: dict[str, Any]

    # Web/research consent (per-run, never persisted).
    web_access: str
    web_consent: dict[str, Any]
    pending_web_consent: bool
    web_allowed_urls: list[str]

    # Research/onboarding (R4 hooks; R5 graph integration consumes these).
    research_context: str
    research_state: dict[str, Any]
    research_fingerprint: str
    onboarding_done: bool
    onboarding_forced: bool
    refresh_research: bool
    skip_onboarding: bool
    research_cycles: int
    max_research_cycles: int
    research_return_to: str | None

    # Orchestration / control flow.
    phase: str
    current_role: str
    fix_cycles: int
    max_fix_cycles: int
    questions: list[str]
    pending_approval: str | None
    selected_skills: dict[str, Any]
    commit_requested: bool
    done: bool
    error: str | None
    plan_error: str | None
    needs_clarification: bool
    blocked: bool
    code_changed_since_review: bool
    reviewed: bool
    qa_passed: bool
    security_passed: bool
    last_decision: str
import json
from pathlib import Path
import shutil


def _resolve_workspace_root(workspace_root: str | Path | None = None) -> Path:
    """Resolve the effective workspace root for harness state (defaults to cwd)."""
    if workspace_root is None:
        return Path.cwd().resolve()
    try:
        return Path(workspace_root).expanduser().resolve()
    except Exception:
        return Path.cwd().resolve()


def _preferences_path(workspace_root: str | Path | None = None) -> Path:
    """Harness preferences path under the effective workspace root."""
    return _resolve_workspace_root(workspace_root) / ".agents" / "harness" / "preferences.json"


def _backup_path(workspace_root: str | Path | None = None) -> Path:
    """Harness preferences backup path under the effective workspace root."""
    return _resolve_workspace_root(workspace_root) / ".agents" / "harness" / "preferences_backup.json"


# Backward-compatible cwd-relative defaults (legacy callers). Prefer path helpers.
PREFERENCES_PATH = _preferences_path(None)
BACKUP_PATH = _backup_path(None)


def _load_preferences(workspace_root: str | Path | None = None) -> dict[str, Any]:
    """Cargar state desde file si existe, sino retornar defaults."""
    path = _preferences_path(workspace_root)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            return _default_preferences()
        except (json.JSONDecodeError, OSError):
            pass
    return _default_preferences()


def _save_preferences(state: dict[str, Any], workspace_root: str | Path | None = None) -> None:
    """Guardar state a file (y respaldar si no existe backup)."""
    path = _preferences_path(workspace_root)
    backup_path = _backup_path(workspace_root)
    # Crear directorio si no existe
    path.parent.mkdir(parents=True, exist_ok=True)

    # Respaldar SOLO si no existe backup ya (para no llenar disco)
    if not backup_path.exists() and path.exists():
        shutil.copy2(path, backup_path)

    # Guardar state actual
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _default_preferences() -> dict[str, Any]:
    """State por defecto para nueva sesión."""
    return {
        "interaction_language": "en",
        "preferences": {},
        "last_task_index": 0,
        "skills_installed": [],
        "commit_requested": False,
        "session_notes": ""
    }


def _load_workspace(workspace_root: str | Path | None = None) -> str:
    """Cargar workspace seleccionado desde preferences, '' si no hay ninguno set o no es válido.

    La selección del proyecto es una decisión explícita del usuario: se acepta
    cualquier directorio existente, sin comparar contra raíces del harness.
    """
    preferences = _load_preferences(workspace_root)
    workspace = preferences.get("selected_workspace")
    if workspace:
        ws_path = Path(workspace).expanduser().resolve()
        if ws_path.is_dir():
            return str(ws_path)
    return ""


def _save_workspace(workspace_path: str, workspace_root: str | Path | None = None) -> None:
    """Guardar ruta de workspace seleccionado a preferences."""
    preferences = _load_preferences(workspace_root)
    preferences["selected_workspace"] = workspace_path
    _save_preferences(preferences, workspace_root)


def _initialize_documentation(workspace_path: str) -> dict[str, Any]:
    """Inicializar requisitos de documentación en la carpeta de workspace especificada.

    Crea un directorio .docs/ dentro del workspace_root si no existe
    y un archivo REQUIREMENTS.md con información básica de estructura del proyecto.
    Retorna un dict con las rutas y estado de los archivos creados.
    """
    workspace_root = Path(workspace_path)
    docs_dir = workspace_root / ".docs"

    # No seguir symlinks: un .docs enlazado a otra ubicación escapa al proyecto.
    if docs_dir.is_symlink():
        return {
            "docs_dir": str(docs_dir),
            "project_title": workspace_root.name or "Project",
            "initialized": False,
            "status": "docs_skipped_symlink",
        }

    try:
        docs_dir.mkdir(parents=True, exist_ok=True)

        # Determinar título del proyecto del nombre del workspace
        project_title = workspace_root.name or "Project"

        requirements_path = docs_dir / "REQUIREMENTS.md"

        if requirements_path.exists():
            return {
                "docs_dir": str(docs_dir),
                "requirements_file": ".docs/REQUIREMENTS.md",
                "project_title": project_title,
                "initialized": False,
                "status": "documentation_already_present",
            }

        requirements_content = f"""# Requirements

## Project

- **Title**: {project_title}
- **Workspace**: {workspace_root.name or "."}

## Language

- **Interaction language**: en
- **Selection**: Default interaction language

## Task Tracking

- [ ] Define project scope
- [ ] Identify key deliverables
- [ ] Set milestones

"""

        requirements_path.write_text(requirements_content, encoding="utf-8")
    except OSError as exc:
        return {
            "docs_dir": str(workspace_root / ".docs"),
            "project_title": workspace_root.name or "Project",
            "initialized": False,
            "status": "docs_error",
            "error": str(exc),
        }

    return {
        "docs_dir": str(docs_dir),
        "requirements_file": str(requirements_path),
        "status": "initialized",
        "project_title": project_title,
    }
