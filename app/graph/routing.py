"""Conditional routing helpers for the team graph."""

from __future__ import annotations

from typing import Any

from app.state import TeamState


_ONBOARDING_DECISION = "onboarding"


def route_after_translator_entry(state: TeamState) -> str:
    if state.get("needs_clarification") or state.get("pending_questions") or state.get("questions"):
        return "needs_input"
    if state.get("blocked"):
        return "blocked"
    return "intake"


def route_after_intake(state: TeamState) -> str:
    if state.get("pending_user_answers"):
        return "translator_entry"
    if state.get("needs_clarification") or state.get("questions"):
        return "needs_input"
    if state.get("blocked"):
        return "blocked"
    return "translator"


def route_after_translator(state: TeamState) -> str:
    if state.get("pending_questions") or state.get("needs_clarification"):
        return "needs_input"
    if str(state.get("last_decision") or "") == _ONBOARDING_DECISION:
        return "research"
    return "plan"


def route_after_reviewer(state: TeamState) -> str:
    if state.get("needs_security"):
        return "security"
    return "qa"


def route_after_skills_setup(state: TeamState) -> str:
    if state.get("blocked") or state.get("status") == "blocked":
        return "blocked"
    task = state.get("current_task") or {}
    executor = str(task.get("executor") or "").strip().lower()
    kind = str(task.get("kind") or "").strip().lower()
    if kind == "onboarding" or executor == "research":
        return "research"
    # On-demand research signal: route to research before executing builder tasks.
    if bool(state.get("needs_research")) and str(state.get("research_question") or "").strip():
        return "research"
    if executor == "builder":
        return "builder"
    # Invalid/missing executor should have been blocked at plan validation, but fail closed.
    return "blocked"


def route_after_decide(state: TeamState) -> str:
    decision = str(state.get("last_decision") or "")
    if decision in {"onboarding", "research"}:
        cycles = int(state.get("research_cycles") or 0)
        maximum = int(state.get("max_research_cycles") or 0)
        return "blocked" if cycles > maximum else "research"
    if decision == "plan":
        return "plan"
    if decision in {"fix", "re_review"}:
        cycles = int(state.get("fix_cycles") or 0)
        maximum = int(state.get("max_fix_cycles") or 0)
        return "blocked" if cycles > maximum else decision
    if decision in {"more_tasks", "done", "commit_approved", "blocked"}:
        return decision
    if state.get("blocked"):
        return "blocked"
    if state.get("done"):
        return "done"
    cycles = int(state.get("fix_cycles") or 0)
    maximum = int(state.get("max_fix_cycles") or 0)
    return "blocked" if cycles > maximum else "fix"


def has_blocking_findings(findings: list[dict[str, Any]] | None) -> bool:
    if not findings:
        return False
    for f in findings:
        sev = str(f.get("severity", "")).lower()
        status = str(f.get("status", "")).lower()
        blocking = f.get("blocking")
        if blocking is True:
            return True
        if sev in {"critical", "high", "blocker", "blocking"}:
            return True
        if status in {"fail", "failed", "blocker", "blocking"}:
            return True
    return False
