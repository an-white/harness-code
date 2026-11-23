"""Graph nodes for the agent team orchestration."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt

from app.agents.runtime import AgentRuntime
from app.config import PROJECT_ROOT, Settings, get_settings
from app.i18n import fixed_language, is_approval, load_preferences, normalize_language, save_preferences, tr
from app.qa.checks import capture_task_baseline, check_python_syntax, detect_scope_violations, run_verification_commands
from app.research.detection import detect_project_state
from app.research.onboarding import run_onboarding
from app.research.writer import load_research_context, read_research_state, write_research_report, write_research_state
from app.state import TeamState
from app.tools.web import web_access_decision, write_web_audit

from .routing import has_blocking_findings


def _run_docs_head_check(workspace_root: Path | str) -> dict[str, Any] | None:
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


def _persist_last_access_head(workspace_root: Path | str, head: str | None) -> None:
    if not head:
        return
    try:
        st = read_research_state(workspace_root)
        if not isinstance(st, dict):
            st = {}
        st["last_access_head"] = str(head)
        write_research_state(workspace_root, st)
    except Exception:
        pass


def _ensure_docs_updated_before_commit(
    workspace_root: Path | str,
    settings: Settings,
    runtime: AgentRuntime | None,
    *,
    interaction_language: str = "en",
) -> dict[str, Any]:
    """Verify docs/HEAD alignment and refresh docs before commit when needed.

    Runs deterministically (no interactive prompts). Never raises; returns status info.
    """
    ws = Path(workspace_root).expanduser().resolve()
    check = _run_docs_head_check(ws)
    if not isinstance(check, dict):
        return {"ok": False, "status": "check_unavailable", "updated": False, "message": "Could not verify docs/HEAD alignment."}

    status = str(check.get("status") or "")
    git_info = check.get("git") or {}
    current_head = git_info.get("current_head") if isinstance(git_info, dict) else None
    needs_update = status in {"no_docs", "misaligned"}
    did_refresh = False
    refresh_status: str | None = None
    refresh_error: str | None = None

    if needs_update:
        try:
            result = run_onboarding(
                ws,
                settings=settings,
                runtime=runtime,
                refresh=True,
                skip=False,
                interaction_language=interaction_language,
            )
            refresh_status = str(result.get("status") or "unknown")
            did_refresh = True
            if refresh_status not in {"ok", "partial", "skipped"} and result.get("synthesis_error"):
                refresh_error = str(result.get("synthesis_error"))
        except Exception as exc:  # noqa: BLE001
            refresh_status = "failed"
            refresh_error = f"{type(exc).__name__}: {exc}"

    # Re-check/persist last_access_head after any refresh attempt.
    head_to_store = current_head
    final_check = check
    if did_refresh:
        recheck = _run_docs_head_check(ws)
        if isinstance(recheck, dict):
            final_check = recheck
            git2 = recheck.get("git") or {}
            if isinstance(git2, dict) and git2.get("current_head"):
                head_to_store = git2.get("current_head")
    _persist_last_access_head(ws, head_to_store)

    final_status = str((final_check or {}).get("status") or status)
    ok = (not needs_update) or (did_refresh and final_status in {"aligned", "misaligned", "no_docs"} and refresh_status in {"ok", "partial", "skipped", None})
    # Consider update successful if refresh ran and produced docs/state (aligned ideal, but partial may still leave artifacts).
    if needs_update and did_refresh and refresh_status in {"ok", "partial", "skipped"}:
        ok = True
    if needs_update and not did_refresh:
        ok = False

    message_parts = []
    if needs_update:
        message_parts.append(f"Docs pre-commit refresh: {refresh_status or 'attempted'}")
        if refresh_error:
            message_parts.append(f"error={refresh_error}")
        message_parts.append(f"alignment={final_status}")
    else:
        message_parts.append(f"Docs pre-commit check: {final_status} (no update needed)")
    if head_to_store:
        message_parts.append(f"last_access_head={head_to_store}")
    return {
        "ok": ok,
        "status": final_status,
        "updated": did_refresh,
        "needs_update": needs_update,
        "refresh_status": refresh_status,
        "error": refresh_error,
        "head": head_to_store,
        "message": "; ".join(message_parts),
    }


def _text_result(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return str(result)
    if result.get("error"):
        return f"ERROR: {result['error']}"
    final = result.get("final")
    if final is not None:
        return str(final)
    return str(result)


def _extract_json(text: str) -> Any | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    for start_c, end_c in (("{", "}"), ("[", "]")):
        start = text.find(start_c)
        end = text.rfind(end_c)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                continue
    return None


def _normalize_questions(obj: Any) -> list[str]:
    if obj is None:
        return []
    if isinstance(obj, str):
        return [obj] if obj.strip() else []
    if isinstance(obj, list):
        out: list[str] = []
        for x in obj:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
            elif isinstance(x, dict):
                q = x.get("question") or x.get("text") or x.get("q")
                if isinstance(q, str) and q.strip():
                    out.append(q.strip())
        return out
    if isinstance(obj, dict):
        for key in ("questions", "open_questions", "preguntas", "clarifications"):
            if key in obj:
                return _normalize_questions(obj[key])
    return []


_VALID_EXECUTORS = {"builder", "research"}
_VALID_KINDS = {"onboarding", "research", "implementation", "test", "docs"}


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(value).strip()] if str(value).strip() else []


def _default_plan(requirement: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "task-1",
            "kind": "implementation",
            "executor": "builder",
            "objective": (requirement or "Implement requested change").strip(),
            "acceptance_criteria": ["Implementation matches the stated requirement."],
            "target_repo_files_hint": [],
            "risks": ["Requirements may need clarification during implementation."],
            "needs_security": False,
        }
    ]


def _normalize_task(item: Any, index: int, requirement: str) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(item, dict):
        errors.append(f"Task {index}: must be an object.")
        item = {"objective": str(item)}
    task = dict(item)
    tid = str(task.get("id") or task.get("task_id") or f"task-{index}").strip() or f"task-{index}"
    kind = str(task.get("kind") or "").strip().lower()
    executor = str(task.get("executor") or "").strip().lower()
    raw_objective = task.get("objective")
    objective = str(raw_objective).strip() if raw_objective is not None else ""
    acceptance = _as_str_list(task.get("acceptance_criteria"))
    files_hint = task.get("target_repo_files_hint", task.get("files", []))
    risks = task.get("risks", [])
    needs_security = task.get("needs_security", False)

    if not str(task.get("id") or task.get("task_id") or "").strip():
        errors.append(f"Task {index} ({tid}): missing required field 'id'.")
    if kind not in _VALID_KINDS:
        errors.append(f"Task {index} ({tid}): invalid or missing 'kind' (expected one of {sorted(_VALID_KINDS)}).")
    if executor not in _VALID_EXECUTORS:
        errors.append(f"Task {index} ({tid}): invalid or missing 'executor' (expected one of {sorted(_VALID_EXECUTORS)}).")
    if not objective:
        errors.append(f"Task {index} ({tid}): missing required field 'objective'.")
        objective = (requirement or "Task").strip()
    if not acceptance:
        errors.append(f"Task {index} ({tid}): 'acceptance_criteria' must be a non-empty list of strings.")
    if not isinstance(files_hint, list):
        errors.append(f"Task {index} ({tid}): 'target_repo_files_hint' must be a list.")
        files_hint = _as_str_list(files_hint)
    else:
        files_hint = [str(v) for v in files_hint]
    if not isinstance(risks, list):
        errors.append(f"Task {index} ({tid}): 'risks' must be a list.")
        risks = _as_str_list(risks)
    else:
        risks = [str(v) for v in risks]
    if not isinstance(needs_security, bool):
        errors.append(f"Task {index} ({tid}): 'needs_security' must be a bool.")
        needs_security = bool(needs_security)

    normalized = {
        "id": tid,
        "kind": kind,
        "executor": executor,
        "objective": objective,
        "acceptance_criteria": acceptance,
        "target_repo_files_hint": files_hint,
        "risks": risks,
        "needs_security": needs_security,
    }
    # preserve extra fields (e.g. research_question) for compatibility/hooks
    for k, v in task.items():
        if k not in normalized and k != "task_id":
            normalized[k] = v
    return normalized, errors


def _ensure_plan(plan: Any, requirement: str) -> tuple[list[dict[str, Any]], list[str]]:
    if isinstance(plan, dict) and "tasks" in plan:
        plan = plan["tasks"]
    if not isinstance(plan, list) or not plan:
        return _default_plan(requirement), []
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, item in enumerate(plan, start=1):
        task, task_errors = _normalize_task(item, i, str(requirement))
        out.append(task)
        errors.extend(task_errors)
    return out, errors


_QA_BLOCKING_SEVERITIES = frozenset({"critical", "high", "blocker", "blocking"})
_CITATION_RE = re.compile(r"(?P<path>[A-Za-z0-9_./\\-]+):(?P<line>[1-9]\d*)")
_QA_EVIDENCE_MAX_FILES = 50
_QA_EVIDENCE_MAX_CHARS = 2000


def _llm_result_is_blocking(result: dict[str, Any]) -> bool:
    """A qualitative QA opinion blocks only when it carries a blocking marker."""
    return bool(
        result.get("blocking")
        or str(result.get("severity", "")).lower() in _QA_BLOCKING_SEVERITIES
        or str(result.get("status", "")).lower() in {"blocked", "blocking"}
    )


def _qa_failed(qa_results: list[dict[str, Any]] | None) -> bool:
    if not qa_results:
        return False
    for r in qa_results:
        if not isinstance(r, dict):
            continue
        if r.get("advisory"):
            continue
        status = str(r.get("status", "")).lower()
        if status not in {"fail", "failed", "error"} and r.get("ok") is not False:
            continue
        if r.get("source") == "deterministic":
            return True
        if status == "error" or _llm_result_is_blocking(r):
            return True
    return False


def _qa_unverifiable(qa_results: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        r
        for r in (qa_results or [])
        if isinstance(r, dict)
        and r.get("source") == "deterministic"
        and str(r.get("status", "")).lower() == "unverifiable"
    ]


def _qa_check(name: str, status: str, details: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "details": details, "source": "deterministic", **extra}


def _changed_file_evidence(
    workspace_root: Path,
    baseline: dict[str, Any] | None,
    hints: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return scope data plus bounded, read-only evidence for changed files."""
    scope = detect_scope_violations(workspace_root, baseline, hints)
    if scope.get("skipped") or scope.get("incomplete") or scope.get("truncated"):
        return scope, []

    baseline_repos = (baseline or {}).get("repos")
    base_repos = {
        entry.get("root"): entry
        for entry in (baseline_repos if isinstance(baseline_repos, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("root"), str)
    }
    evidence: list[dict[str, Any]] = []
    for change in (scope.get("changed") or [])[:_QA_EVIDENCE_MAX_FILES]:
        if not isinstance(change, dict):
            continue
        workspace_path = str(change.get("workspace_path") or "")
        relative = Path(workspace_path)
        if not workspace_path or relative.is_absolute() or ".." in relative.parts:
            continue
        path = workspace_root / relative
        excerpt = ""
        if path.is_file() and not path.is_symlink():
            try:
                excerpt = path.read_text(encoding="utf-8", errors="replace")[:_QA_EVIDENCE_MAX_CHARS]
            except OSError:
                excerpt = "<unreadable>"
        numbered = "\n".join(
            f"{lineno}|{line}"
            for lineno, line in enumerate(excerpt.splitlines(), start=1)
        )
        base_entry = base_repos.get(change.get("repo")) or {}
        dirty = base_entry.get("dirty")
        snapshot = dirty.get(change.get("path")) if isinstance(dirty, dict) else None
        evidence.append(
            {
                "path": workspace_path,
                "change": change.get("reason"),
                "baseline_sha256": snapshot.get("sha256") if isinstance(snapshot, dict) else None,
                "content": numbered,
            }
        )
    return scope, evidence


def _normalized_citation_path(value: str) -> str:
    path = value.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    path = path.lstrip("/")
    if path.startswith("workspace/"):
        path = path[len("workspace/"):]
    return path


def _security_citation_valid(finding: dict[str, Any], evidence: list[dict[str, Any]]) -> bool:
    """Accept a citation only when its path and line exist in real file evidence."""
    for key in ("evidence", "location", "file", "path", "message", "details"):
        match = _CITATION_RE.search(str(finding.get(key) or ""))
        if not match:
            continue
        cited = _normalized_citation_path(match.group("path"))
        line = int(match.group("line"))
        for item in evidence:
            path = _normalized_citation_path(str(item.get("path") or ""))
            if not path or cited != path:
                continue
            for numbered in str(item.get("content") or "").splitlines():
                prefix, separator, _ = numbered.partition("|")
                if separator and prefix.strip().isdigit() and int(prefix.strip()) == line:
                    return True
    return False


def _valid_language_code(lang: str | None) -> str | None:
    norm = normalize_language(lang)
    if not norm:
        return None
    primary = norm.split("-", 1)[0]
    if len(primary) < 2 or len(primary) > 8 or not primary.isalpha():
        return None
    return norm


def _workspace_root(settings: Settings) -> Path:
    return Path(getattr(settings, "workspace_root", Path.cwd()))


def create_nodes(runtime: AgentRuntime | None = None, settings: Settings | None = None) -> dict[str, Callable[[TeamState], dict[str, Any]]]:
    settings = settings or getattr(runtime, "settings", None) or get_settings()

    def _load_digest(state: TeamState) -> str:
        digest = str(state.get("research_context") or "").strip()
        if digest:
            return digest
        try:
            loaded = load_research_context(_workspace_root(settings))
            return str(loaded.get("digest") or "").strip()
        except Exception:
            return ""

    def _run(
        role: str,
        prompt: str,
        context: str | None = None,
        web_consent: dict[str, Any] | None = None,
        state: TeamState | None = None,
    ) -> dict[str, Any]:
        if runtime is None:
            return {"final": prompt, "mode": "stub"}
        # Bounded digest injection for compute-saving context (English digest).
        run_context = context
        try:
            digest = _load_digest(state or {})
            if digest:
                max_chars = int(getattr(settings, "research_context_chars", 8000) or 8000)
                digest = digest[:max(0, max_chars)]
                if run_context:
                    run_context = f"{run_context}\n\nProject context digest (bounded):\n{digest}"
                else:
                    run_context = f"Project context digest (bounded):\n{digest}"
        except Exception:
            run_context = context
        try:
            return runtime.run(
                role,
                [HumanMessage(content=prompt)],
                context=run_context,
                web_consent=web_consent,
            )
        except TypeError:
            # Backward-compatible runtimes without web_consent support.
            return runtime.run(role, [HumanMessage(content=prompt)], context=run_context)

    def _localize_text(text: str, lang: str, st: TeamState | None = None) -> tuple[str, str | None]:
        if not text or not str(text).strip():
            return text, None
        if fixed_language(lang) == "en" and (lang or "en").split("-", 1)[0] == "en":
            return text, None
        prompt = (
            "Localize the following user-facing text into the target language. "
            "Return only the localized text, with no commentary.\n"
            f"Target language: {lang}\n\nText:\n{text}"
        )
        result = _run("translator", prompt, state=st or {})
        out = _text_result(result)
        if "ERROR:" in out or not out.strip():
            return text, tr("translation_fallback_notice", "en")
        return out.strip(), None

    def _localize_questions(questions: list[str], lang: str, st: TeamState | None = None) -> tuple[list[str], str | None]:
        if not questions:
            return questions, None
        if fixed_language(lang) == "en" and (lang or "en").split("-", 1)[0] == "en":
            return questions, None
        prompt = (
            "Translate/localize these clarification questions into the target language for the user. "
            "Return JSON {\"questions\": [\"...\"]} with the same number/order.\n"
            f"Target language: {lang}\nQuestions: {json.dumps(questions, ensure_ascii=False)}"
        )
        result = _run("translator", prompt, state=st or {})
        text_out = _text_result(result)
        data = _extract_json(text_out)
        localized = _normalize_questions(data if data is not None else text_out)
        if len(localized) != len(questions) or any("ERROR:" in q for q in localized):
            # best effort per-question fallback
            out = []
            notice = tr("translation_fallback_notice", "en")
            for q in questions:
                loc, _ = _localize_text(q, lang, st)
                out.append(loc)
            return out, notice
        return localized, None

    _TRANSLATION_UNAVAILABLE_EN = (
        "Translation/localization unavailable. Original non-English content was not "
        "passed downstream. Please provide the requirement or clarification in English."
    )

    def _translate_to_english(text: str, source_lang: str | None, st: TeamState | None = None) -> tuple[str, str | None]:
        value = (text or "").strip()
        if not value:
            return value, None
        src = (source_lang or "en").split("-", 1)[0]
        if src == "en":
            return value, None
        prompt = (
            "Translate the following user content into clear English for an engineering team. "
            "Preserve numbers, identifiers, URLs, file paths, commands, product names, and quoted code. "
            "Return only the English text.\n"
            f"Source language: {source_lang}\n\nContent:\n{value}"
        )
        result = _run("translator", prompt, state=st or {})
        out = _text_result(result)
        if "ERROR:" in out or not out.strip():
            return _TRANSLATION_UNAVAILABLE_EN, tr("translation_fallback_notice", "en")
        return out.strip(), None

    def _ensure_language(state: TeamState) -> tuple[str | None, dict[str, Any]]:
        updates: dict[str, Any] = {}
        lang = _valid_language_code(state.get("interaction_language"))
        prefs = dict(state.get("preferences") or load_preferences(_workspace_root(settings)))
        if not lang:
            pref_lang = _valid_language_code(prefs.get("interaction_language"))
            if pref_lang:
                lang = pref_lang
        if lang:
            try:
                save_preferences(_workspace_root(settings), {"interaction_language": lang})
                prefs["interaction_language"] = lang
            except Exception:
                pass
            updates["interaction_language"] = lang
            updates["preferences"] = prefs
            updates["pending_language_selection"] = False
            updates["language_selection_attempts"] = 0
        return lang, updates

    def translator_entry(state: TeamState) -> dict[str, Any]:
        lang, updates = _ensure_language(state)
        updates.update({"current_role": "translator", "phase": "translate"})

        if not lang:
            # Bounded re-prompt path for invalid/unresolved language selection.
            attempts = int(state.get("language_selection_attempts") or 0)
            max_attempts = 3
            is_retry = bool(state.get("pending_language_selection"))
            message = tr("language_select_message", "en") + " / " + tr("language_select_message", "es")
            if is_retry and state.get("language_notice"):
                message = f"{state.get('language_notice')} {message}"
            answer = interrupt(
                {
                    "type": "language_selection",
                    "message": message,
                    "question": tr("language_select_question", "en"),
                }
            )
            chosen = _valid_language_code(str(answer))
            if not chosen:
                attempts += 1
                notice = tr("invalid_language_notice", "en")
                if attempts >= max_attempts:
                    # Fallback to English after bounded retries; do not persist invalid input.
                    return {
                        **updates,
                        "interaction_language": "en",
                        "preferences": dict(state.get("preferences") or {}),
                        "language_notice": notice + " " + tr("translation_fallback_notice", "en"),
                        "pending_language_selection": False,
                        "language_selection_attempts": 0,
                        "needs_clarification": False,
                        "questions": [],
                        "pending_questions": [],
                        "phase": "intake",
                        "status": "language_ready_fallback",
                    }
                return {
                    **updates,
                    "pending_language_selection": True,
                    "language_selection_attempts": attempts,
                    "language_notice": notice,
                    "needs_clarification": True,
                    "pending_questions": [tr("language_select_question", "en")],
                    "questions": [tr("language_select_question", "en")],
                    "phase": "translate",
                }
            prefs = dict(state.get("preferences") or load_preferences(_workspace_root(settings)))
            prefs["interaction_language"] = chosen
            try:
                save_preferences(_workspace_root(settings), {"interaction_language": chosen})
            except Exception:
                pass
            updates["interaction_language"] = chosen
            updates["preferences"] = prefs
            updates["pending_language_selection"] = False
            updates["language_selection_attempts"] = 0
            lang = chosen

        lang = lang or "en"
        notices = [n for n in [state.get("language_notice")] if n]
        # Preserve originals for audit/non-downstream use only; never inject into prompts.
        raw_requirement = str(state.get("raw_requirement") or state.get("requirement") or "").strip()

        # Translate pending clarification answers to English before merging downstream.
        pending_answers = state.get("pending_user_answers")
        requirement = str(state.get("requirement") or "").strip()
        if pending_answers:
            answers = pending_answers if isinstance(pending_answers, list) else [pending_answers]
            raw_answers_prev = list(state.get("raw_answers") or [])
            raw_answers_prev.extend([str(a) for a in answers])
            translated_answers = []
            for a in answers:
                t, note = _translate_to_english(str(a), lang, state)
                translated_answers.append(t)
                if note and note not in notices:
                    notices.append(note)
            prev = str(state.get("requirement") or "").strip()
            extra = "\n".join(a for a in translated_answers if a.strip())
            requirement = (prev + "\n" + extra).strip() if prev else extra
            updates["raw_answers"] = raw_answers_prev
            updates["pending_user_answers"] = []
            updates["answers"] = list(state.get("answers") or []) + translated_answers

        translated_req, note = _translate_to_english(requirement, lang, state)
        if note and note not in notices:
            notices.append(note)
        updates["raw_requirement"] = raw_requirement
        # Downstream-facing requirement must be English-only fallback on failure, never original.
        updates["requirement"] = translated_req
        updates["language_notice"] = "; ".join(dict.fromkeys(notices)) if notices else None
        updates["needs_clarification"] = False
        updates["questions"] = []
        updates["pending_questions"] = []
        updates["phase"] = "intake"
        updates["status"] = "language_ready"
        return updates

    def pm_intake(state: TeamState) -> dict[str, Any]:
        lang = _valid_language_code(state.get("interaction_language")) or "en"
        updates: dict[str, Any] = {"current_role": "project-manager", "phase": "intake"}

        existing_questions = list(state.get("questions") or state.get("pending_questions") or [])
        requirement = (state.get("requirement") or "").strip()
        clarification_attempts = int(state.get("clarification_attempts") or 0)

        # If clarification questions are pending, interrupt to collect answers in interaction language.
        if existing_questions and (
            state.get("needs_clarification")
            or state.get("status") == "awaiting_clarification"
            or state.get("phase") == "intake"
        ):
            if clarification_attempts >= 3:
                # Too many rounds: accept current requirement and proceed.
                updates.update(
                    {
                        "requirement": requirement,
                        "questions": [],
                        "pending_questions": [],
                        "needs_clarification": False,
                        "phase": "translate",
                        "status": "requirements_ready",
                        "clarification_attempts": 0,
                    }
                )
                return updates
            localized_questions, notice = _localize_questions(existing_questions, lang, state)
            answer = interrupt(
                {
                    "type": "intake_questions",
                    "message": tr("intake_more_info", lang),
                    "questions": localized_questions,
                }
            )
            raw_answers = [str(answer)] if not isinstance(answer, list) else [str(a) for a in answer]
            # route back through translator_entry to translate answers to English
            next_updates: dict[str, Any] = {
                "current_role": "translator",
                "phase": "translate",
                "pending_user_answers": raw_answers,
                "questions": [],
                "pending_questions": [],
                "needs_clarification": False,
                "status": "answers_received",
                "clarification_attempts": clarification_attempts + 1,
            }
            if notice:
                next_updates["language_notice"] = notice
            return next_updates

        # If requirement was already validated/ready, proceed.
        if requirement and not state.get("needs_clarification") and state.get("status") == "requirements_ready":
            updates["phase"] = "translate"
            updates["questions"] = []
            updates["pending_questions"] = []
            updates["clarification_attempts"] = 0
            return updates

        prompt = (
            "You are the PM. Analyze the following requirement. If critical information is missing, "
            "return JSON {\"needs_clarification\": true, \"questions\": [\"...\"]}. "
            "If sufficient, return JSON {\"needs_clarification\": false, \"questions\": [], "
            "\"summary\": \"...\"}.\n\n"
            f"Requirement (English): {requirement or '(empty)'}"
        )
        result = _run("project-manager", prompt, state=state)
        text = _text_result(result)
        data = _extract_json(text)
        questions: list[str] = []
        needs = False
        summary = requirement
        if isinstance(data, dict):
            needs = bool(data.get("needs_clarification") or data.get("missing_info"))
            questions = _normalize_questions(data.get("questions") or data.get("open_questions"))
            summary = str(data.get("summary") or requirement or summary)
        elif not requirement:
            needs = True
            questions = ["What requirement or task do you want to implement?"]

        if needs and questions:
            if clarification_attempts >= 3:
                # Accept after max attempts
                updates.update(
                    {
                        "requirement": summary or requirement,
                        "questions": [],
                        "pending_questions": [],
                        "needs_clarification": False,
                        "phase": "translate",
                        "status": "requirements_ready",
                        "clarification_attempts": 0,
                    }
                )
                return updates
            return {
                "current_role": "project-manager",
                "phase": "intake",
                "questions": questions,
                "pending_questions": questions,
                "needs_clarification": True,
                "requirement": requirement,
                "status": "awaiting_clarification",
                "clarification_attempts": clarification_attempts + 1,
            }

        updates.update(
            {
                "requirement": summary or requirement,
                "questions": [],
                "pending_questions": [],
                "needs_clarification": False,
                "phase": "translate",
                "status": "requirements_ready",
                "clarification_attempts": 0,
            }
        )
        return updates

    def translator_node(state: TeamState) -> dict[str, Any]:
        requirement = (state.get("requirement") or "").strip()
        prompt = (
            "Normalize the following requirements into clear English for an engineering team. "
            "If critical information is missing, return JSON {\"open_questions\": [\"...\"]}. "
            "Otherwise return JSON {\"normalized_requirements\": \"...\", \"open_questions\": []}.\n\n"
            f"Requirements:\n{requirement}"
        )
        result = _run("translator", prompt, state=state)
        text = _text_result(result)
        data = _extract_json(text)
        open_questions: list[str] = []
        normalized = requirement
        if isinstance(data, dict):
            open_questions = _normalize_questions(data.get("open_questions") or data.get("questions"))
            normalized = str(data.get("normalized_requirements") or data.get("normalized") or text or requirement)
        elif text and "ERROR:" not in text:
            normalized = text

        updates: dict[str, Any] = {
            "current_role": "translator",
            "normalized_requirements": normalized,
            "pending_questions": open_questions,
            "phase": "plan" if not open_questions else "intake",
        }
        if open_questions:
            updates["needs_clarification"] = True
            updates["questions"] = open_questions
            updates["status"] = "awaiting_clarification"
        return updates

    def _make_onboarding_task(reason: str | None = None) -> dict[str, Any]:
        obj = "Run project onboarding/research to generate project specs and context digest before implementation planning."
        if reason:
            obj += f" Trigger: {reason}."
        return {
            "id": "onboarding-1",
            "kind": "onboarding",
            "executor": "research",
            "objective": obj,
            "acceptance_criteria": [
                "Project research/onboarding state and context digest are available for planning.",
                "Onboarding artifacts are written/loaded when applicable.",
            ],
            "target_repo_files_hint": ["docs/project_specs.md", ".agents/research/context.md", ".agents/research/state.json"],
            "risks": ["Repository evidence may be incomplete; synthesis can be partial."],
            "needs_security": False,
            "research_question": "Analyze repository structure, business/domain, stack, entry points, conventions and risks for onboarding.",
            "is_onboarding": True,
        }

    def _onboarding_decision(state: TeamState) -> dict[str, Any]:
        try:
            return detect_project_state(
                _workspace_root(settings),
                settings=settings,
                refresh=bool(state.get("refresh_research")),
                skip=bool(state.get("skip_onboarding")),
            )
        except Exception as exc:  # noqa: BLE001
            return {"should_run": False, "reasons": [f"detection_error:{exc}"], "fingerprint": ""}

    def _load_context_updates(state: TeamState) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        try:
            loaded = load_research_context(_workspace_root(settings))
            updates["research_context"] = str(loaded.get("digest") or "")
            updates["research_state"] = dict(loaded.get("state") or {})
            updates["research_fingerprint"] = str(loaded.get("fingerprint") or state.get("research_fingerprint") or "")
            if loaded.get("onboarding_done"):
                updates["onboarding_done"] = True
        except Exception:
            pass
        return updates

    def pm_plan(state: TeamState) -> dict[str, Any]:
        requirement = state.get("normalized_requirements") or state.get("requirement") or ""
        ctx_updates = _load_context_updates(state)
        base: dict[str, Any] = {
            **ctx_updates,
            "current_role": "project-manager",
            "phase": "plan",
            "qa_baseline": {},
            "fix_cycles": int(state.get("fix_cycles") or 0),
            "max_fix_cycles": int(state.get("max_fix_cycles") or settings.max_fix_cycles),
            "max_research_cycles": int(state.get("max_research_cycles") if state.get("max_research_cycles") is not None else getattr(settings, "research_max_cycles", 2) or 0),
        }

        detection = _onboarding_decision(state)
        stored_fp = str(state.get("research_fingerprint") or (state.get("research_state") or {}).get("fingerprint") or "")
        fp = str(detection.get("fingerprint") or "")
        needs_onboarding = bool(detection.get("should_run"))
        # Re-evaluate when explicitly requested or when no context/fingerprint known yet.
        if not needs_onboarding and not (state.get("onboarding_forced") or state.get("onboarding_done")) and not stored_fp and not state.get("skip_onboarding"):
            # detection already considered auto rules; keep as-is.
            pass
        if (state.get("onboarding_forced") or state.get("onboarding_done")) and not state.get("refresh_research"):
            # prevent duplicate forced onboarding in the same flow unless refresh requested
            needs_onboarding = False
        if needs_onboarding and bool(detection.get("existing_project")):
            onboarding_task = _make_onboarding_task(",".join(detection.get("reasons") or []))
            # Preserve PM plan if it already exists? force onboarding first, then re-plan.
            return {
                **base,
                "plan": [onboarding_task],
                "task_index": 0,
                "current_task": onboarding_task,
                "needs_security": False,
                "needs_research": True,
                "research_question": onboarding_task.get("research_question"),
                "onboarding_forced": True,
                "onboarding_done": False,
                "research_return_to": "plan",
                "last_decision": "onboarding",
                "reviewed": False,
                "code_changed_since_review": False,
                "blocked": False,
                "error": None,
                "plan_error": None,
                "status": "onboarding_required",
                "phase": "research",
            }

        # Refresh context after onboarding completion before generating/refining plan.
        prompt = (
            "Create an ordered implementation plan as JSON: {\"tasks\": [{\"id\": \"task-1\", "
            "\"kind\": \"implementation|research|onboarding|test|docs\", "
            "\"executor\": \"builder|research\", "
            "\"objective\": \"...\", \"acceptance_criteria\": [\"...\"], "
            "\"target_repo_files_hint\": [\"...\"], \"risks\": [\"...\"], \"needs_security\": false}]}. "
            "Every task must include id, kind, executor, objective, acceptance_criteria (non-empty), "
            "target_repo_files_hint, risks, needs_security. "
            "Set needs_security true for auth, secrets, permissions, network exposure, or untrusted input handling. "
            "Do not include onboarding tasks unless explicitly required; project onboarding/context is already handled.\n\n"
            f"Requirements:\n{requirement}"
        )
        result = _run("project-manager", prompt, state={**state, **base})
        text_out = _text_result(result)
        data = _extract_json(text_out)
        tasks, errors = _ensure_plan(data, str(requirement))
        # Remove any accidental onboarding tasks from PM plan to avoid loops; onboarding is handled separately.
        tasks = [t for t in tasks if str(t.get("kind") or "").strip().lower() != "onboarding"]
        if not tasks:
            tasks, _ = _ensure_plan(None, str(requirement))
        if errors:
            msg = "Invalid plan schema: " + "; ".join(errors)
            return {
                **base,
                "plan": tasks,
                "task_index": 0,
                "current_task": None,
                "needs_security": bool(state.get("needs_security")),
                "blocked": True,
                "status": "invalid_plan",
                "error": msg,
                "plan_error": msg,
                "last_decision": "blocked",
            }
        current = tasks[0] if tasks else None
        needs_security = any(
            bool(t.get("needs_security")) and str(t.get("executor")) == "builder" for t in tasks
        ) or bool(state.get("needs_security"))
        needs_research = bool(state.get("needs_research")) or any(
            str(t.get("executor")) == "research" for t in tasks
        )
        research_question = state.get("research_question")
        if not research_question and current and str(current.get("executor")) == "research":
            research_question = current.get("research_question") or current.get("objective")
        return {
            **base,
            "plan": tasks,
            "task_index": 0,
            "current_task": current,
            "needs_security": needs_security,
            "needs_research": needs_research,
            "research_question": research_question,
            "reviewed": False,
            "code_changed_since_review": False,
            "blocked": False,
            "error": None,
            "plan_error": None,
            "last_decision": None,
            "status": "plan_ready",
        }

    def skills_setup(state: TeamState) -> dict[str, Any]:
        selected = state.get("selected_skills") or {}
        skills = state.get("skills") or {"status": "pending", "selected": selected}
        task = state.get("current_task") or {}
        executor = str(task.get("executor") or "builder").strip().lower()
        on_demand = bool(state.get("needs_research")) and str(state.get("research_question") or "").strip() and executor != "research" and str(task.get("kind") or "").lower() != "onboarding"
        phase = "research" if executor == "research" or str(task.get("kind") or "").lower() == "onboarding" or on_demand else "build"
        updates: dict[str, Any] = {
            "current_role": "project-manager",
            "phase": phase,
            "selected_skills": selected,
            "skills": skills,
        }
        if on_demand:
            rc = int(state.get("research_cycles") or 0)
            mx = int(state.get("max_research_cycles") if state.get("max_research_cycles") is not None else getattr(settings, "research_max_cycles", 2) or 0)
            if rc >= mx:
                reason = f"Research cycle limit reached ({rc}/{mx}) before on-demand research."
                return {
                    **updates,
                    "phase": "blocked",
                    "blocked": True,
                    "last_decision": "blocked",
                    "status": "blocked",
                    "error": reason,
                    "decision_reason": reason,
                    "research_cycles": rc,
                    "max_research_cycles": mx,
                }
            updates["research_cycles"] = rc + 1
            updates["research_return_to"] = state.get("research_return_to") or "plan"
            updates["last_decision"] = "research"
            updates["decision_reason"] = f"On-demand research requested (cycle {rc + 1}/{mx})."
            updates["max_research_cycles"] = mx
        return updates

    def _planned_web_preview(state: TeamState) -> dict[str, Any]:
        """Return sanitized, non-secret preview info for consent UI/audit."""
        task = state.get("current_task") or {}
        provider = getattr(settings, "websearch_provider", "none")
        configured = bool(getattr(settings, "websearch_endpoint", None)) and provider != "none"
        planned_queries: list[str] = []
        # Only generic preview; never include raw requirement content.
        if configured:
            planned_queries = ["short generic technical query (sanitized)"]
        return {
            "provider": provider if configured else "none",
            "websearch_configured": configured,
            "planned_domains": ["user-provided-or-public-https-domains"],
            "planned_queries_preview": planned_queries,
            "task_kind": task.get("kind"),
        }

    def _resolve_web_consent(state: TeamState) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Resolve per-run web consent (state only). May interrupt once if ask+undecided."""
        updates: dict[str, Any] = {}
        existing = state.get("web_consent")
        if isinstance(existing, dict) and existing.get("decision") in {"allow", "deny", "allow_urls"}:
            return existing, updates

        base = str(state.get("web_access") or getattr(settings, "web_access", "ask") or "ask").strip().lower()
        if base not in {"ask", "allow", "deny"}:
            base = "ask"
        # Explicit non-interactive/fixed decisions (e.g. CLI flags) stored in state.
        if base == "deny":
            consent = {"decision": "deny", "allow_websearch": False, "allowed_urls": []}
            write_web_audit(_workspace_root(settings), {"event": "web_consent", "status": "denied", "scope": "run", "provider": getattr(settings, "websearch_provider", "none")}, consent)
            updates.update({"web_access": "deny", "web_consent": consent, "pending_web_consent": False, "web_allowed_urls": []})
            return consent, updates
        if base == "allow":
            consent = {"decision": "allow", "allow_websearch": True, "allowed_urls": []}
            write_web_audit(_workspace_root(settings), {"event": "web_consent", "status": "allowed", "scope": "run", "provider": getattr(settings, "websearch_provider", "none")}, consent)
            updates.update({"web_access": "allow", "web_consent": consent, "pending_web_consent": False, "web_allowed_urls": []})
            return consent, updates

        preview = _planned_web_preview(state)
        answer = interrupt(
            {
                "type": "web_consent",
                "message": (
                    "Research may access the internet to gather external technical context. "
                    "No secrets, private paths, emails, code, or project content will be sent. "
                    "Choose web access for this run only."
                ),
                "preview": preview,
                "options": [
                    "allow_web_search",
                    "urls_only",
                    "deny",
                ],
                "question": "Allow web search, only user-provided URLs, or no internet?",
            }
        )
        choice = ""
        allowed_urls: list[str] = []
        if isinstance(answer, dict):
            choice = str(answer.get("decision") or answer.get("choice") or answer.get("answer") or "").strip().lower()
            raw_urls = answer.get("allowed_urls") or answer.get("urls") or []
            if isinstance(raw_urls, list):
                allowed_urls = [str(u).strip() for u in raw_urls if str(u).strip()]
            elif isinstance(raw_urls, str) and raw_urls.strip():
                allowed_urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
        else:
            choice = str(answer or "").strip().lower()
        if choice in {"allow", "allow_web_search", "allow_web", "yes", "si", "sí"}:
            consent = {"decision": "allow", "allow_websearch": True, "allowed_urls": allowed_urls}
            status = "allowed"
        elif choice in {"urls_only", "only_urls", "user_urls", "provided_urls", "allow_urls"}:
            consent = {"decision": "allow_urls", "allow_websearch": False, "allowed_urls": allowed_urls}
            status = "urls_only"
        else:
            consent = {"decision": "deny", "allow_websearch": False, "allowed_urls": []}
            status = "denied"
        write_web_audit(
            _workspace_root(settings),
            {
                "event": "web_consent",
                "status": status,
                "scope": "urls_only" if consent["decision"] == "allow_urls" else "run",
                "provider": getattr(settings, "websearch_provider", "none"),
            },
            consent,
        )
        updates.update(
            {
                "web_access": consent["decision"],
                "web_consent": consent,
                "pending_web_consent": False,
                "web_allowed_urls": allowed_urls,
            }
        )
        return consent, updates

    def _slugify(value: str) -> str:
        s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip().lower()).strip("-")
        return s[:60] or "research"

    def research_node(state: TeamState) -> dict[str, Any]:
        """Research/onboarding step with consent-aware execution and bounded routing."""
        task = state.get("current_task") or {}
        kind = str(task.get("kind") or "").strip().lower()
        is_onboarding_task = kind == "onboarding" or bool(task.get("is_onboarding")) or str(task.get("id") or "") == "onboarding-1"
        consent, consent_updates = _resolve_web_consent(state)
        question = (
            state.get("research_question")
            or task.get("research_question")
            or task.get("objective")
            or state.get("normalized_requirements")
            or state.get("requirement")
            or ""
        )
        updates: dict[str, Any] = {
            **consent_updates,
            "current_role": "research",
            "phase": "research",
            "reviewed": False,
            "qa_passed": False,
            "security_passed": False,
        }

        if is_onboarding_task:
            try:
                onboarding_res = run_onboarding(
                    _workspace_root(settings),
                    settings=settings,
                    runtime=runtime,
                    refresh=bool(state.get("refresh_research")),
                    skip=bool(state.get("skip_onboarding")),
                    interaction_language=_valid_language_code(state.get("interaction_language")) or "en",
                )
            except Exception as exc:  # noqa: BLE001
                onboarding_res = {"status": "partial_blocked", "synthesis_error": str(exc)}
            ctx_updates = _load_context_updates(state)
            written = onboarding_res.get("written") or {}
            det = onboarding_res.get("detection") or {}
            st = str(onboarding_res.get("status") or "completed")
            okish = st in {"ok", "partial", "skipped"}
            result = {
                "task_id": task.get("id"),
                "executor": "research",
                "kind": task.get("kind") or "onboarding",
                "question": str(question),
                "status": "completed" if okish else "completed_with_issues",
                "onboarding": True,
                "onboarding_status": st,
                "web_decision": (consent or {}).get("decision"),
                "summary": f"Onboarding {st}. reasons={det.get('reasons')} fingerprint={onboarding_res.get('fingerprint') or det.get('fingerprint')}",
                "sources": [p for p in [written.get("specs_path"), written.get("context_path"), written.get("state_path")] if p],
                "limitations": ([str(onboarding_res.get("synthesis_error"))] if onboarding_res.get("synthesis_error") else []),
            }
            results = list(state.get("research_results") or [])
            results.append(result)
            artifacts = list(state.get("artifacts") or [])
            artifacts.append(json.dumps(result, ensure_ascii=False)[:4000])
            updates.update(ctx_updates)
            updates.update(
                {
                    "research_results": results,
                    "artifacts": artifacts,
                    "needs_research": False,
                    "onboarding_done": bool((ctx_updates.get("research_state") or {}).get("onboarding_done") or det.get("onboarding_done") or okish),
                    "onboarding_forced": True,
                    "status": "onboarding_completed" if okish else "onboarding_completed_partial",
                    "last_decision": "plan",
                    "research_return_to": "plan",
                    "current_task": None,
                }
            )
            return updates

        # On-demand / planned research task
        prompt = (
            "Perform read-only research for the current task using available local tools and evidence. "
            "Use web tools only if consent/policy allows and they are available; if denied or not configured, continue with local evidence. "
            "Return concise JSON with keys: summary, findings (list), sources (list of local paths or sanitized domains), limitations (list), "
            "recommendations (list, optional).\n\n"
            f"Task: {json.dumps(task, ensure_ascii=False)}\nQuestion: {question}"
        )
        run_result = _run("research", prompt, context=str(question), web_consent=consent, state=state)
        text_out = _text_result(run_result)
        data = _extract_json(text_out)
        structured: dict[str, Any] = {}
        if isinstance(data, dict):
            structured = {
                "summary": str(data.get("summary") or text_out or ""),
                "findings": _as_str_list(data.get("findings")) if isinstance(data.get("findings"), list) else ([str(data.get("findings"))] if data.get("findings") else []),
                "sources": _as_str_list(data.get("sources")) if isinstance(data.get("sources"), list) else ([str(data.get("sources"))] if data.get("sources") else []),
                "limitations": _as_str_list(data.get("limitations")) if isinstance(data.get("limitations"), list) else ([str(data.get("limitations"))] if data.get("limitations") else []),
                "recommendations": _as_str_list(data.get("recommendations")) if isinstance(data.get("recommendations"), list) else ([str(data.get("recommendations"))] if data.get("recommendations") else []),
            }
        else:
            structured = {"summary": text_out[:4000], "findings": [text_out[:1000]] if text_out else [], "sources": [], "limitations": []}
        result = {
            "task_id": task.get("id"),
            "executor": "research",
            "kind": task.get("kind") or "research",
            "question": str(question),
            "status": "completed" if "ERROR:" not in text_out else "completed_with_issues",
            "placeholder": False,
            "web_decision": (consent or {}).get("decision"),
            **structured,
        }
        # Persist reusable research report when appropriate via deterministic writer helper.
        try:
            slug = _slugify(str(task.get("id") or question))
            report_md = (
                f"# Research: {str(question)[:120]}\n\n"
                f"## Summary\n{structured.get('summary','')}\n\n"
                f"## Findings\n" + "\n".join(f"- {f}" for f in (structured.get("findings") or []) ) + "\n\n"
                f"## Sources\n" + "\n".join(f"- {s}" for s in (structured.get("sources") or []) ) + "\n\n"
                f"## Limitations\n" + "\n".join(f"- {l}" for l in (structured.get("limitations") or []) ) + "\n"
            )
            written = write_research_report(
                _workspace_root(settings),
                slug,
                report_md,
                interaction_language=_valid_language_code(state.get("interaction_language")) or "en",
            )
            result["report_path"] = written.get("specs_path")
            result["localized_report_path"] = written.get("localized_specs_path")
        except Exception:
            pass

        results = list(state.get("research_results") or [])
        results.append(result)
        artifacts = list(state.get("artifacts") or [])
        artifacts.append(json.dumps(result, ensure_ascii=False)[:4000])
        updates.update(
            {
                "research_results": results,
                "artifacts": artifacts,
                "needs_research": False,
                "research_question": None,
                "status": "research_completed",
                "last_decision": state.get("research_return_to") or "decide",
            }
        )
        return updates

    def builder_node(state: TeamState) -> dict[str, Any]:
        # If commit was requested by PM after user approval, execute git commit
        # and return a done state instead of implementing changes.
        if state.get("commit_requested") and state.get("repo"):
            from app.tools.shell import git_commit

            repo_path = state.get("repo")
            artifacts = state.get("artifacts") or []
            # Use the last artifact or a generic message as the commit message
            commit_message = (
                f"Implement task: {json.dumps(state.get('current_task', {}).get('objective', '')[:80])}"
                if state.get("current_task")
                else "Implement changes"
            )

            # If there are artifacts, use the last one as the file list summary
            if artifacts:
                file_list = [artifacts[-1][:200] if isinstance(artifacts[-1], str) else str(artifacts[-1])[:200]]
            else:
                file_list = []

            try:
                result = git_commit(
                    message=commit_message,
                    files=file_list,
                    repo_path=repo_path,
                    approved=True,
                )
            except Exception as exc:  # noqa: BLE001
                result = f"ERROR: {exc}"

            # Return done state to end the flow after commit
            return {
                "current_role": "project-manager",
                "phase": "done",
                "done": True,
                "status": "commit_recorded",
                "commit_sha": state.get("commit_sha") or "pending-manual-commit",
                "commit_result": result,
            }

        # Snapshot the pre-task workspace once; fix cycles must reuse it so QA
        # compares against the state before the builder started editing.
        baseline = state.get("qa_baseline")
        baseline_update: dict[str, Any] = {}
        if not isinstance(baseline, dict) or not baseline:
            baseline = capture_task_baseline(_workspace_root(settings))
            baseline_update["qa_baseline"] = baseline

        task = state.get("current_task") or {}
        requirement = state.get("normalized_requirements") or state.get("requirement") or ""
        findings = state.get("review_findings") or []
        qa = state.get("qa_results") or []
        security = state.get("security_findings") or []
        prompt = (
            "Implement/fix the current task. Return a concise summary of changes.\n\n"
            f"Task: {json.dumps(task, ensure_ascii=False)}\n"
            f"Requirements: {requirement}\n"
            f"Review findings: {json.dumps(findings, ensure_ascii=False)}\n"
            f"QA results: {json.dumps(qa, ensure_ascii=False)}\n"
            f"Security findings: {json.dumps(security, ensure_ascii=False)}\n"
        )
        result = _run("builder", prompt, state=state)
        text = _text_result(result)
        artifacts = list(state.get("artifacts") or [])
        artifacts.append(text[:4000])
        attempts = int(state.get("attempts") or 0) + 1
        return {
            **baseline_update,
            "current_role": "builder",
            "phase": "build",
            "artifacts": artifacts,
            "attempts": attempts,
            "reviewed": False,
            "code_changed_since_review": True,
            "review_findings": [],
            "qa_results": [],
            "security_findings": [],
            "qa_passed": False,
            "security_passed": False,
        }

    def reviewer_node(state: TeamState) -> dict[str, Any]:
        task = state.get("current_task") or {}
        artifacts = state.get("artifacts") or []
        prompt = (
            "Review the implementation for the current task. Return JSON {\"approved\": bool, "
            "\"findings\": [{\"severity\": \"low|medium|high|critical\", \"message\": \"...\", \"blocking\": bool}]}.\n\n"
            f"Task: {json.dumps(task, ensure_ascii=False)}\n"
            f"Artifacts: {json.dumps(artifacts[-3:], ensure_ascii=False)}\n"
        )
        result = _run("reviewer", prompt, state=state)
        text = _text_result(result)
        data = _extract_json(text)
        findings: list[dict[str, Any]] = []
        approved = "ERROR:" not in text
        if isinstance(data, dict):
            approved = bool(data.get("approved", approved))
            raw = data.get("findings") or []
            if isinstance(raw, list):
                findings = [f for f in raw if isinstance(f, dict)]
        elif "ERROR:" in text:
            findings = [{"severity": "high", "message": text, "blocking": True}]
        return {
            "current_role": "reviewer",
            "phase": "review",
            "review_findings": findings,
            "reviewed": True,
            "code_changed_since_review": False,
            "status": "review_approved" if approved and not has_blocking_findings(findings) else "review_changes_requested",
        }

    def security_node(state: TeamState) -> dict[str, Any]:
        task = state.get("current_task") or {}
        baseline = state.get("qa_baseline") if isinstance(state.get("qa_baseline"), dict) else None
        scope, evidence = _changed_file_evidence(
            _workspace_root(settings), baseline, task.get("target_repo_files_hint") or []
        )
        prompt = (
            "Perform a security review for the current task. Return JSON {\"passed\": bool, "
            "\"findings\": [{\"severity\": \"low|medium|high|critical\", \"message\": \"...\", "
            "\"blocking\": bool, \"evidence\": \"path.py:12\"}]}.\n"
            "Every blocking finding MUST cite a bare repo-relative file:line from the numbered "
            "Files evidence below (only the shown lines, first 2000 characters per file, may be "
            "cited); findings without such evidence are advisory only.\n\n"
            f"Task: {json.dumps(task, ensure_ascii=False)}\n"
            f"Artifacts: {json.dumps((state.get('artifacts') or [])[-3:], ensure_ascii=False)}\n"
            "Changed-file evidence (deterministic, read-only):\n"
            f"Scope: {json.dumps({k: scope.get(k) for k in ('skipped', 'incomplete', 'reason', 'changed')}, ensure_ascii=False)}\n"
            f"Files: {json.dumps(evidence, ensure_ascii=False)}\n"
        )
        result = _run("security", prompt, state=state)
        text = _text_result(result)
        data = _extract_json(text)
        findings: list[dict[str, Any]] = []
        passed = "ERROR:" not in text
        if isinstance(data, dict):
            passed = bool(data.get("passed", passed))
            raw = data.get("findings") or []
            if isinstance(raw, list):
                findings = [f for f in raw if isinstance(f, dict)]
        elif "ERROR:" in text:
            findings = [{"severity": "high", "message": text, "blocking": True}]
            passed = False
        # A speculative verdict cannot block the run: keep it as an advisory
        # finding, but genuine findings with file:line evidence stay blocking.
        normalized: list[dict[str, Any]] = []
        for finding in findings:
            finding = dict(finding)
            if has_blocking_findings([finding]) and not _security_citation_valid(finding, evidence):
                finding["original_severity"] = finding.get("severity")
                finding["severity"] = "low"
                finding["blocking"] = False
                finding["status"] = "advisory"
                finding["details"] = "Blocking security findings require a valid file:line citation; treated as advisory."
            normalized.append(finding)
        needs_security = bool(state.get("needs_security")) or bool(task.get("needs_security"))
        evidence_unavailable = bool(
            scope.get("skipped")
            or scope.get("incomplete")
            or scope.get("truncated")
            or (scope.get("changed") and not evidence)
        )
        if needs_security and not evidence and evidence_unavailable:
            reason = str(
                scope.get("reason") or "no changed-file evidence available for security review"
            )
            normalized.append(
                {
                    "name": "security_evidence",
                    "status": "unverifiable",
                    "severity": "high",
                    "blocking": True,
                    "source": "deterministic",
                    "details": f"Security evidence unverifiable: {reason}",
                }
            )
            passed = False
        blocking = has_blocking_findings(normalized)
        return {
            "current_role": "security",
            "phase": "security",
            "security_findings": normalized,
            "security_passed": bool(passed and not blocking),
        }

    def qa_node(state: TeamState) -> dict[str, Any]:
        task = state.get("current_task") or {}
        baseline = state.get("qa_baseline") if isinstance(state.get("qa_baseline"), dict) else None
        workspace_root = _workspace_root(settings)
        hints = task.get("target_repo_files_hint") or []
        hint_values = hints if isinstance(hints, (list, tuple, set, frozenset)) else [hints]
        has_scope = any(str(hint).strip() for hint in hint_values)
        scope, evidence = _changed_file_evidence(workspace_root, baseline, hints)
        prompt = (
            "Assess QA quality for the current task. Deterministic checks are run separately "
            "by the harness and cannot be overridden. Return JSON {\"passed\": bool, "
            "\"results\": [{\"name\": \"...\", \"status\": \"pass|fail\", \"details\": \"...\"}]}.\n\n"
            f"Task: {json.dumps(task, ensure_ascii=False)}\n"
            f"Artifacts: {json.dumps((state.get('artifacts') or [])[-3:], ensure_ascii=False)}\n"
            f"Changed-file evidence: {json.dumps(evidence, ensure_ascii=False)}\n"
        )
        result = _run("qa", prompt, state=state)
        text = _text_result(result)
        data = _extract_json(text)
        results: list[dict[str, Any]] = []
        if isinstance(data, dict):
            raw = data.get("results") or data.get("qa_results") or []
            if isinstance(raw, list):
                results = [r for r in raw if isinstance(r, dict)]
        elif "ERROR:" in text:
            results = [{"name": "qa", "status": "error", "details": text[:2000]}]
        qualitative: list[dict[str, Any]] = []
        for item in results:
            item = dict(item)
            item["source"] = "llm"
            qualitative.append(item)

        deterministic: list[dict[str, Any]] = []
        unverifiable_reason = ""
        if scope.get("skipped") or scope.get("incomplete") or scope.get("truncated"):
            unverifiable_reason = str(
                scope.get("reason") or "scope comparison incomplete or truncated"
            )
            deterministic.append(_qa_check("scope", "unverifiable", unverifiable_reason))
            deterministic.append(
                _qa_check("python_syntax", "unverifiable", f"Changed files unknown: {unverifiable_reason}")
            )
        else:
            changed = [c for c in (scope.get("changed") or []) if isinstance(c, dict)]
            changed_paths = [str(c.get("workspace_path") or "") for c in changed]
            violations = [str(v.get("workspace_path") or "") for v in (scope.get("violations") or [])]
            if not has_scope:
                deterministic.append(
                    _qa_check(
                        "scope",
                        "skipped",
                        "no scope supplied; out-of-scope changes were not evaluated",
                        changed_files=changed_paths[:_QA_EVIDENCE_MAX_FILES],
                    )
                )
            elif violations:
                deterministic.append(
                    _qa_check(
                        "scope",
                        "fail",
                        f"Out-of-scope changes: {', '.join(violations[:20])}",
                        changed_files=changed_paths[:_QA_EVIDENCE_MAX_FILES],
                    )
                )
            else:
                deterministic.append(
                    _qa_check(
                        "scope",
                        "pass",
                        f"{len(changed)} changed file(s), all within target_repo_files_hint.",
                        changed_files=changed_paths[:_QA_EVIDENCE_MAX_FILES],
                    )
                )
            python_paths: list[str] = []
            for change in changed:
                relative = Path(str(change.get("workspace_path") or ""))
                if relative.suffix != ".py" or relative.is_absolute() or ".." in relative.parts:
                    continue
                candidate = workspace_root / relative
                if candidate.is_file() and not candidate.is_symlink():
                    python_paths.append(str(candidate))
            syntax_errors = check_python_syntax(python_paths)
            if syntax_errors:
                deterministic.append(
                    _qa_check("python_syntax", "fail", "; ".join(syntax_errors[:20]))
                )
            else:
                deterministic.append(
                    _qa_check(
                        "python_syntax",
                        "pass",
                        f"Compiled {len(python_paths)} changed Python file(s) with no syntax errors.",
                    )
                )

        commands = task.get("verification_commands")
        if not commands:
            deterministic.append(
                _qa_check(
                    "verification_commands",
                    "skipped",
                    "No verification commands supplied by the task; none were invented.",
                )
            )
        else:
            try:
                command_results = run_verification_commands(workspace_root, commands)
            except Exception as exc:  # noqa: BLE001
                deterministic.append(
                    _qa_check(
                        "verification_commands",
                        "unverifiable",
                        f"Verification runner unavailable: {type(exc).__name__}: {exc}"[:2000],
                    )
                )
            else:
                failures = [
                    r
                    for r in command_results
                    if r.get("blocked") or r.get("timed_out") or r.get("exit_code") != 0
                ]
                if failures:
                    deterministic.append(
                        _qa_check(
                            "verification_commands",
                            "fail",
                            json.dumps(failures, ensure_ascii=False)[:2000],
                        )
                    )
                else:
                    deterministic.append(
                        _qa_check(
                            "verification_commands",
                            "pass",
                            f"{len(command_results)} task-supplied verification command(s) passed.",
                        )
                    )

        deterministic_usable = any(
            str(r.get("status", "")).lower() in {"pass", "fail"} for r in deterministic
        )
        if deterministic_usable:
            # With real deterministic evidence, qualitative verdicts are advisory
            # and can never force a FAIL/fix cycle.
            for item in qualitative:
                item["status"] = "advisory"
                item["blocking"] = False
                item["advisory"] = True
                item.pop("ok", None)
        else:
            deterministic.append(
                _qa_check(
                    "evidence",
                    "unverifiable",
                    "No deterministic check produced usable evidence; qualitative assessment cannot certify QA.",
                )
            )

        passed = not any(
            str(r.get("status", "")).lower() in {"fail", "failed", "error", "unverifiable"}
            for r in deterministic
        )
        if unverifiable_reason:
            passed = False
        return {
            "current_role": "qa",
            "phase": "qa",
            "qa_results": qualitative + deterministic,
            "qa_passed": passed,
        }

    def _decision_issue_reasons(st: TeamState) -> list[str]:
        reasons: list[str] = []
        if has_blocking_findings(st.get("review_findings")):
            reasons.append("blocking review findings")
        elif not bool(st.get("reviewed")):
            reasons.append("review not completed")
        elif st.get("code_changed_since_review"):
            reasons.append("code changed since last review")
        if has_blocking_findings(st.get("security_findings")):
            reasons.append("blocking security findings")
        elif (bool(st.get("needs_security")) or bool((st.get("current_task") or {}).get("needs_security"))) and not bool(st.get("security_passed")):
            reasons.append("security checks not passed")
        qa_problems = [
            f"QA {r.get('name')}: {str(r.get('details') or '')[:160]}"
            for r in (st.get("qa_results") or [])
            if isinstance(r, dict)
            and r.get("source") == "deterministic"
            and str(r.get("status", "")).lower() in {"fail", "failed", "error", "unverifiable"}
        ]
        if qa_problems:
            reasons.extend(qa_problems)
        elif _qa_failed(st.get("qa_results")) or not bool(st.get("qa_passed")):
            reasons.append("QA failed or not passed")
        return reasons

    def pm_decide(state: TeamState) -> dict[str, Any]:
        current_task = state.get("current_task") or {}
        current_executor = str(current_task.get("executor") or "builder").strip().lower()
        review_blocking = has_blocking_findings(state.get("review_findings"))
        security_blocking = has_blocking_findings(state.get("security_findings"))
        qa_passed = bool(state.get("qa_passed")) and not _qa_failed(state.get("qa_results"))
        # needs_security in state tracks whether any builder task in flow requires security review.
        security_ok = (not bool(state.get("needs_security")) and not bool(current_task.get("needs_security"))) or (
            bool(state.get("security_passed")) and not security_blocking
        )
        review_ok = bool(state.get("reviewed")) and not review_blocking and not state.get("code_changed_since_review")
        fix_cycles = int(state.get("fix_cycles") or 0)
        max_fix = int(state.get("max_fix_cycles") or settings.max_fix_cycles)
        research_cycles = int(state.get("research_cycles") or 0)
        max_research = int(state.get("max_research_cycles") if state.get("max_research_cycles") is not None else getattr(settings, "research_max_cycles", 2) or 0)
        plan = list(state.get("plan") or [])
        task_index = int(state.get("task_index") or 0)
        lang = _valid_language_code(state.get("interaction_language")) or "en"

        # Unverifiable evidence cannot be fixed by another builder turn: stop
        # here with an explicit reason instead of cycling QA/fix forever.
        unverifiable_qa = _qa_unverifiable(state.get("qa_results"))
        if unverifiable_qa and current_executor != "research":
            details = "; ".join(
                f"{r.get('name')}: {r.get('details')}" for r in unverifiable_qa
            )
            reason = f"QA evidence unverifiable; cannot certify this task: {details[:1000]}."
            return {
                "current_role": "project-manager",
                "phase": "blocked",
                "blocked": True,
                "last_decision": "blocked",
                "status": "blocked",
                "error": reason,
                "decision_reason": reason,
                "fix_cycles": fix_cycles,
                "max_fix_cycles": max_fix,
                "research_cycles": research_cycles,
                "max_research_cycles": max_research,
            }

        # Missing security evidence cannot be resolved by another builder turn:
        # block with the explicit reason instead of treating it as a passed gate.
        unverifiable_security = [
            f
            for f in (state.get("security_findings") or [])
            if isinstance(f, dict)
            and str(f.get("status", "")).lower() == "unverifiable"
        ]
        needs_security_now = bool(state.get("needs_security")) or bool(current_task.get("needs_security"))
        if unverifiable_security and needs_security_now and current_executor != "research":
            details = "; ".join(str(f.get("details") or "") for f in unverifiable_security)
            reason = f"Security evidence unverifiable; cannot certify this task: {details[:1000]}."
            return {
                "current_role": "project-manager",
                "phase": "blocked",
                "blocked": True,
                "last_decision": "blocked",
                "status": "blocked",
                "error": reason,
                "decision_reason": reason,
                "fix_cycles": fix_cycles,
                "max_fix_cycles": max_fix,
                "research_cycles": research_cycles,
                "max_research_cycles": max_research,
            }

        # On-demand research signal from PM/state (outside current research task execution).
        if bool(state.get("needs_research")) and current_executor != "research" and not bool(current_task.get("is_onboarding")):
            if research_cycles < max_research:
                next_rc = research_cycles + 1
                reason = f"On-demand research requested (cycle {next_rc}/{max_research})."
                return {
                    "current_role": "project-manager",
                    "phase": "research",
                    "research_cycles": next_rc,
                    "max_research_cycles": max_research,
                    "last_decision": "research",
                    "research_return_to": state.get("research_return_to") or "plan",
                    "status": "research_requested",
                    "decision_reason": reason,
                }
            reason = f"Research cycle limit reached ({research_cycles}/{max_research}); cannot start additional on-demand research."
            return {
                "current_role": "project-manager",
                "phase": "blocked",
                "blocked": True,
                "last_decision": "blocked",
                "status": "blocked",
                "error": reason,
                "decision_reason": reason,
                "research_cycles": research_cycles,
                "max_research_cycles": max_research,
                "fix_cycles": fix_cycles,
                "max_fix_cycles": max_fix,
            }

        # Research tasks bypass builder/reviewer/qa/security chain by default.
        if current_executor == "research":
            research_ok = state.get("status") in {"research_completed", "onboarding_completed", "onboarding_completed_partial"}
            if not research_ok:
                if fix_cycles < max_fix:
                    next_fix = fix_cycles + 1
                    reason = f"Research task incomplete (status={state.get('status')}); requesting retry/fix (fix {next_fix}/{max_fix})."
                    return {
                        "current_role": "project-manager",
                        "phase": "research",
                        "fix_cycles": next_fix,
                        "max_fix_cycles": max_fix,
                        "research_cycles": research_cycles,
                        "max_research_cycles": max_research,
                        "last_decision": "research",
                        "status": "research_fix_requested",
                        "decision_reason": reason,
                    }
                reason = f"Max fix cycles reached ({fix_cycles}/{max_fix}) with incomplete research task (status={state.get('status')})."
                return {
                    "current_role": "project-manager",
                    "phase": "blocked",
                    "blocked": True,
                    "last_decision": "blocked",
                    "status": "blocked",
                    "error": reason,
                    "decision_reason": reason,
                    "fix_cycles": fix_cycles,
                    "max_fix_cycles": max_fix,
                    "research_cycles": research_cycles,
                    "max_research_cycles": max_research,
                }
            # Onboarding/forced research can request re-plan/refinement.
            if str(state.get("last_decision") or state.get("research_return_to") or "") == "plan":
                return {
                    "current_role": "project-manager",
                    "phase": "plan",
                    "last_decision": "plan",
                    "research_return_to": None,
                    "status": "plan_refine_after_research",
                    "decision_reason": "Research/onboarding completed; refining plan with updated context.",
                    "fix_cycles": fix_cycles,
                    "max_fix_cycles": max_fix,
                    "research_cycles": research_cycles,
                    "max_research_cycles": max_research,
                }
        else:
            # If builder changed code after review, must re-review.
            if state.get("code_changed_since_review") and state.get("phase") not in ("build", "review"):
                return {
                    "current_role": "project-manager",
                    "phase": "review",
                    "last_decision": "re_review",
                    "decision_reason": "Code changed since last review; re-review required.",
                    "fix_cycles": fix_cycles,
                    "max_fix_cycles": max_fix,
                    "research_cycles": research_cycles,
                    "max_research_cycles": max_research,
                }

            if not (review_ok and security_ok and qa_passed):
                issue_reasons = _decision_issue_reasons(state)
                issue_text = ", ".join(issue_reasons) if issue_reasons else "unresolved review/QA/security issues"
                if fix_cycles < max_fix:
                    next_fix = fix_cycles + 1
                    reason = f"Fix requested ({next_fix}/{max_fix}): {issue_text}."
                    return {
                        "current_role": "project-manager",
                        "phase": "fix",
                        "fix_cycles": next_fix,
                        "max_fix_cycles": max_fix,
                        "research_cycles": research_cycles,
                        "max_research_cycles": max_research,
                        "last_decision": "fix",
                        "status": "fix_requested",
                        "decision_reason": reason,
                    }
                reason = f"Max fix cycles reached ({fix_cycles}/{max_fix}): {issue_text}."
                return {
                    "current_role": "project-manager",
                    "phase": "blocked",
                    "blocked": True,
                    "last_decision": "blocked",
                    "status": "blocked",
                    "error": reason,
                    "decision_reason": reason,
                    "fix_cycles": fix_cycles,
                    "max_fix_cycles": max_fix,
                    "research_cycles": research_cycles,
                    "max_research_cycles": max_research,
                }

        # current task done, advance
        next_index = task_index + 1
        if next_index < len(plan):
            next_task = plan[next_index]
            next_executor = str(next_task.get("executor") or "builder").strip().lower()
            needs_security = bool(state.get("needs_security")) or (
                bool(next_task.get("needs_security")) and next_executor == "builder"
            )
            next_phase = "research" if next_executor == "research" else "build"
            next_needs_research = bool(state.get("needs_research")) or next_executor == "research"
            next_rq = state.get("research_question") or next_task.get("research_question") or (next_task.get("objective") if next_executor == "research" else None)
            return {
                "current_role": "project-manager",
                "phase": next_phase,
                "task_index": next_index,
                "current_task": next_task,
                "qa_baseline": {},
                "needs_security": needs_security,
                "needs_research": next_needs_research,
                "research_question": next_rq,
                "research_return_to": "plan" if next_needs_research and next_executor != "research" else state.get("research_return_to"),
                "fix_cycles": 0,
                "reviewed": False,
                "review_findings": [],
                "qa_results": [],
                "security_findings": [],
                "last_decision": "research" if next_executor == "research" else "more_tasks",
                "status": "next_task",
            }

        # If commit already approved/requested (e.g. after re-entry), proceed to commit
        # finalization without re-prompting. Docs pre-commit enforcement runs once at approval time.
        if state.get("commit_requested"):
            existing_docs = state.get("docs_precommit") if isinstance(state.get("docs_precommit"), dict) else None
            status = "commit_recorded"
            if isinstance(existing_docs, dict):
                if existing_docs.get("updated"):
                    status = "commit_recorded_docs_updated"
                elif existing_docs.get("error") or existing_docs.get("refresh_status") in {"failed", "partial_blocked"}:
                    status = "commit_recorded_docs_check_failed"
            return {
                "current_role": "project-manager",
                "phase": "done",
                "done": True,
                "commit_requested": True,
                "pending_approval": None,
                "last_decision": "done",
                "status": status,
                "commit_sha": state.get("commit_sha") or "pending-manual-commit",
                "docs_precommit": existing_docs or state.get("docs_precommit"),
                "docs_updated_before_commit": bool(state.get("docs_updated_before_commit")),
                "docs_precommit_status": state.get("docs_precommit_status"),
            }

        # all done, request commit approval (localized)
        # Build a summary of changes for the user notification
        artifacts = state.get("artifacts") or []
        review_findings = state.get("review_findings") or []
        qa_results = state.get("qa_results") or []
        security_findings = state.get("security_findings") or []

        changes_summary: list[str] = []
        if artifacts:
            changes_summary.append(f"Artifacts: {', '.join(artifacts[-5:])}")
        if review_findings:
            changes_summary.append(
                f"Review findings: {len(review_findings)} issue{'s' if len(review_findings) > 1 else ''}"
            )
        if qa_results:
            passed = all(
                str(r.get("status", "")).lower() in {"pass", "completed"} for r in qa_results
            )
            changes_summary.append(f"QA {'passed' if passed else 'failed'}")
        if security_findings:
            changes_summary.append(
                f"Security findings: {len(security_findings)} issue{'s' if len(security_findings) > 1 else ''}"
            )

        summary_text = "; ".join(changes_summary) if changes_summary else ""

        interrupt_message = tr("commit_approval_message", lang)
        if summary_text:
            interrupt_message += f"\nChanges to be committed: {summary_text}"
        interrupt_question = tr("commit_approval_question", lang)

        approval = interrupt(
            {
                "type": "commit_approval",
                "message": interrupt_message,
                "question": interrupt_question,
            }
        )
        approved = False
        if isinstance(approval, dict):
            approved = bool(approval.get("approved") or approval.get("approve"))
            text_ans = str(approval.get("answer") or approval.get("text") or "")
            approved = approved or is_approval(text_ans, lang)
        else:
            approved = is_approval(approval, lang)

        # Before finalizing/recording commit approval, ensure repo docs/state are updated
        # and persisted so updated artifacts can be included in the manual commit.
        docs_precommit: dict[str, Any] | None = None
        if approved:
            if bool(state.get("skip_onboarding")):
                ws = _workspace_root(settings)
                check = _run_docs_head_check(ws)
                git_info = (check or {}).get("git") if isinstance(check, dict) else {}
                head = git_info.get("current_head") if isinstance(git_info, dict) else None
                _persist_last_access_head(ws, head)
                docs_precommit = {
                    "ok": True,
                    "status": str((check or {}).get("status") or "skipped"),
                    "updated": False,
                    "needs_update": False,
                    "refresh_status": "skipped_due_to_skip_onboarding",
                    "error": None,
                    "head": head,
                    "message": "Docs pre-commit refresh skipped (skip_onboarding).",
                }
            else:
                docs_precommit = _ensure_docs_updated_before_commit(
                    _workspace_root(settings),
                    settings,
                    runtime,
                    interaction_language=lang,
                )

        # Set last_decision to differentiate approved from not-approved case
        # This allows route_after_decide to redirect to builder after approval
        if approved:
            last_decision = "commit_approved"
        else:
            last_decision = "done"

        result: dict[str, Any] = {
            "current_role": "project-manager",
            "phase": "commit" if approved else "done",
            "done": not approved,
            "commit_requested": approved,
            "pending_approval": None,
            "last_decision": last_decision,
            "status": "commit_approved" if approved else "done_no_commit",
            "approvals": {**dict(state.get("approvals") or {}), "commit": approved},
        }
        if docs_precommit is not None:
            result["docs_precommit"] = docs_precommit
            result["docs_updated_before_commit"] = bool(docs_precommit.get("updated") or docs_precommit.get("ok"))
            result["docs_precommit_status"] = str(docs_precommit.get("status") or "")
            # Make status explicit for CLI/flow visibility while preserving approval semantics.
            if docs_precommit.get("updated"):
                result["status"] = "commit_approved_docs_updated"
            elif docs_precommit.get("ok"):
                result["status"] = "commit_approved_docs_aligned"
            else:
                result["status"] = "commit_approved_docs_update_failed"
        return result

    def commit_node(state: TeamState) -> dict[str, Any]:
        if not state.get("commit_requested"):
            return {"phase": "done", "done": True, "status": "commit_skipped"}
        # Placeholder: actual git commit integration is out of scope here and must not auto-run.
        return {
            "current_role": "project-manager",
            "phase": "done",
            "done": True,
            "status": "commit_recorded",
            "commit_sha": state.get("commit_sha") or "pending-manual-commit",
        }

    return {
        "translator_entry": translator_entry,
        "pm_intake": pm_intake,
        "translator_node": translator_node,
        "pm_plan": pm_plan,
        "skills_setup": skills_setup,
        "research_node": research_node,
        "builder_node": builder_node,
        "reviewer_node": reviewer_node,
        "security_node": security_node,
        "qa_node": qa_node,
        "pm_decide": pm_decide,
        "commit_node": commit_node,
    }
