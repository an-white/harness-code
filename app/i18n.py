"""Fixed UI strings and language helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SUPPORTED_FIXED = {"en", "es"}
DEFAULT_LANGUAGE = "en"

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "language_select_message": "Select the interaction language for this project.",
        "language_select_question": "Enter a language code (e.g. en, es, fr).",
        "intake_more_info": "I need more information to continue.",
        "commit_approval_message": "All tasks are green. Approve commit?",
        "commit_approval_question": "Reply 'approve' to authorize the commit, or anything else to skip it.",
        "translation_fallback_notice": "Translation/localization unavailable for the requested language; using English.",
        "invalid_language_notice": "Invalid language value. Please provide a valid language code.",
        "post_task_prompt": "What would you like to do next?",
        "enter_language_prompt": "Enter new language code:",
        "enter_workspace_prompt": "Enter new workspace path:",
        "enter_requirement_prompt": "Describe the requirement (type 'b' to go back to menu):",
        "session_resume_message": "Found saved session from previous run.",
        "session_workspace": "Workspace",
        "session_language": "Language",
        "session_task_index": "Last task index",
        "session_pending_summary": "Pending tasks from task #{index}",
        "session_continue_question": "Do you want to continue? (s/n)",
        "session_choose": "Choose:",
        "session_resume": "(s) Resume previous session",
        "session_new": "(n) New session (backup previous first)",
        "session_restore": "(b) Restore from backup",
        "dir_select_message": "Select workspace directory for this project.",
        "dir_select_question": "Enter workspace path (or press Enter for current directory):",
        "research_offer_question": "Run research to analyze the repo and the current state of the tasks defined in it? (s/n)",
        "research_running": "Running research...",
        "research_skipped_notice": "Research skipped: {reason}",
        "research_failed_notice": "Research failed: {error}",
        "docs_first_load_question": "No prior analysis/docs found for this workspace. Run analysis/onboarding now? (s/n)",
        "docs_misaligned_question": "Project docs/state appear out of sync with current HEAD/fingerprint. Update documentation now? (s/n)",
        "docs_misaligned_notice": "Docs/state alignment check: {status}. {message}",
        "docs_last_access_updated_notice": "Recorded last accessed HEAD: {head}",
        "docs_precommit_updated_notice": "Documentation updated before commit so it can be included in git.",
        "docs_precommit_aligned_notice": "Documentation already aligned before commit.",
        "docs_precommit_failed_notice": "Documentation could not be fully updated before commit: {error}",
        "action_choose": "Choose:",
        "action_new": "(n) New task",
        "action_resume": "(r) Resume previous",
        "action_analyze": "(a) Analyze project",
        "docs_definition_message": "Defining required documentation in {folder}...",
        "dir_browse_option": "(e) Browse directories",
        "dir_browse_cancelled": "Browse cancelled.",
        "dir_browse_title": "Browsing directories: {path}",
        "dir_browse_help": "Arrows: move | Right: enter dir | Enter: select | q/ESC: cancel",
        "invalid_option": "Invalid option, try again.",
        "task_completed": "Task completed.",
        "menu_deep_analysis": "(d) Deep analysis",
        "analysis_choice_message": "How do you want to analyze this workspace?",
        "analysis_choice_normal": "(n) Normal analysis (onboarding)",
        "analysis_choice_deep": "(d) Deep analysis",
        "analysis_choice_skip": "(s) Skip analysis",
        "deep_analysis_running": "Running deep analysis...",
        "deep_analysis_failed_notice": "Deep analysis failed: {error}",
        "deep_analysis_quality_question": "Is the deep analysis result quality sufficient? (s/n)",
        "deep_analysis_retry_prompt": "Retry deep analysis? (s/n) (choose 'n' to generate partial/incomplete artifacts and continue)",
        "deep_analysis_retry_unavailable": "Deep analysis is unavailable (missing runtime/LLM). Retry? (s/n)",
        "deep_analysis_partial_notice": "Generated partial/incomplete deep analysis artifacts due to stop/failure.",
        "deep_analysis_done": "Deep analysis completed.",
        "deep_analysis_ask_after_normal": "Do you want to run deep analysis now? (s/n)",
    },
    "es": {
        "language_select_message": "Selecciona el idioma de interacción para este proyecto.",
        "language_select_question": "Ingresa un código de idioma (p. ej. en, es, fr).",
        "intake_more_info": "Necesito más información para continuar.",
        "commit_approval_message": "Todas las tareas están en verde. ¿Aprobar commit?",
        "commit_approval_question": "Responde 'aprobar' para autorizar el commit, o cualquier otra cosa para omitirlo.",
        "translation_fallback_notice": "La traducción/localización no está disponible para el idioma solicitado; se usará inglés.",
        "invalid_language_notice": "Valor de idioma inválido. Proporciona un código de idioma válido.",
        "post_task_prompt": "¿Qué te gustaría hacer a continuación?",
        "enter_language_prompt": "Ingresa el nuevo código de idioma:",
        "enter_workspace_prompt": "Ingresa la nueva ruta de workspace:",
        "enter_requirement_prompt": "Describe el requerimiento (escribe 'b' para volver al menú):",
        "session_resume_message": "Se encontró una sesión guardada de una ejecución anterior.",
        "session_workspace": "Espacio de trabajo",
        "session_language": "Idioma",
        "session_task_index": "Índice de tarea anterior",
        "session_pending_summary": "Tareas pendientes desde la tarea #{index}",
        "session_continue_question": "¿Desea continuar? (s/n)",
        "session_choose": "Escoge:",
        "session_resume": "(s) Reanudar sesión anterior",
        "session_new": "(n) Nueva sesión (hacer backup primero)",
        "session_restore": "(b) Restaurar desde backup",
        "dir_select_message": "Selecciona el directorio de trabajo para este proyecto.",
        "dir_select_question": "Ruta de workspace (o Enter para el directorio actual):",
        "research_offer_question": "¿Ejecutar research para analizar el repo y el estado actual de las tareas definidas en él? (s/n)",
        "research_running": "Ejecutando research...",
        "research_skipped_notice": "Research omitido: {reason}",
        "research_failed_notice": "Research falló: {error}",
        "docs_first_load_question": "No se encontró análisis/documentación previa para este workspace. ¿Ejecutar análisis/onboarding ahora? (s/n)",
        "docs_misaligned_question": "La documentación/estado del proyecto parece desalineada con el HEAD/fingerprint actual. ¿Actualizar la documentación ahora? (s/n)",
        "docs_misaligned_notice": "Verificación de alineación de docs/estado: {status}. {message}",
        "docs_last_access_updated_notice": "HEAD de último acceso registrado: {head}",
        "docs_precommit_updated_notice": "Documentación actualizada antes del commit para que pueda incluirse en git.",
        "docs_precommit_aligned_notice": "La documentación ya estaba alineada antes del commit.",
        "docs_precommit_failed_notice": "No se pudo actualizar completamente la documentación antes del commit: {error}",
        "action_choose": "Escoge:",
        "action_new": "(n) Nueva tarea",
        "action_resume": "(r) Reanudar anterior",
        "action_analyze": "(a) Analizar proyecto",
        "docs_definition_message": "Definiendo documentación requerida en {folder}...",
        "dir_browse_option": "(e) Explorar directorios",
        "dir_browse_cancelled": "Exploración cancelada.",
        "dir_browse_title": "Explorando directorios: {path}",
        "dir_browse_help": "Flechas: mover | Derecha: entrar dir | Enter: seleccionar | q/ESC: salir",
        "invalid_option": "Opción no válida, intenta de nuevo.",
        "task_completed": "Tarea completada.",
        "menu_deep_analysis": "(d) Análisis profundo",
        "analysis_choice_message": "¿Cómo deseas analizar este espacio de trabajo?",
        "analysis_choice_normal": "(n) Análisis normal (onboarding)",
        "analysis_choice_deep": "(d) Análisis profundo",
        "analysis_choice_skip": "(s) Omitir análisis",
        "deep_analysis_running": "Ejecutando análisis profundo...",
        "deep_analysis_failed_notice": "Análisis profundo falló: {error}",
        "deep_analysis_quality_question": "¿La calidad del resultado del análisis profundo es suficiente? (s/n)",
        "deep_analysis_retry_prompt": "¿Reintentar el análisis profundo? (s/n) (elige 'n' para generar artefactos parciales/incompletos y continuar)",
        "deep_analysis_retry_unavailable": "El análisis profundo no está disponible (runtime/LLM ausente). ¿Reintentar? (s/n)",
        "deep_analysis_partial_notice": "Se generaron artefactos de análisis profundo parciales/incompletos debido a detención/fallo.",
        "deep_analysis_done": "Análisis profundo completado.",
        "deep_analysis_ask_after_normal": "¿Deseas ejecutar análisis profundo ahora? (s/n)",
    },
}

APPROVE_WORDS = {
    "en": {"approve", "approved", "yes", "y", "true", "1"},
    "es": {"aprobar", "aprobado", "si", "sí", "yes", "y", "true", "1"},
}


def normalize_language(value: str | None) -> str | None:
    if value is None:
        return None
    lang = str(value).strip().lower().replace("_", "-")
    if not lang:
        return None
    # keep primary subtag for fixed strings, but allow full tag in state
    return lang


def fixed_language(lang: str | None) -> str:
    norm = normalize_language(lang) or DEFAULT_LANGUAGE
    primary = norm.split("-", 1)[0]
    return primary if primary in SUPPORTED_FIXED else DEFAULT_LANGUAGE


def tr(key: str, lang: str | None = None) -> str:
    bucket = STRINGS[fixed_language(lang)]
    return bucket.get(key) or STRINGS[DEFAULT_LANGUAGE].get(key) or key


def is_approval(text: Any, lang: str | None = None) -> bool:
    value = str(text or "").strip().lower()
    words = set(APPROVE_WORDS["en"])
    words.update(APPROVE_WORDS[fixed_language(lang)])
    return value in words


def preferences_path(workspace_root: Path) -> Path:
    return workspace_root / ".agents" / "harness" / "preferences.json"


def load_preferences(workspace_root: Path) -> dict[str, Any]:
    path = preferences_path(workspace_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_preferences(workspace_root: Path, prefs: dict[str, Any]) -> None:
    path = preferences_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_preferences(workspace_root)
    merged = {**existing, **prefs}
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
