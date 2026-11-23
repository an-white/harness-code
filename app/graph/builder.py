"""Build the LangGraph StateGraph for the agent team."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.runtime import AgentRuntime
from app.config import Settings, get_settings
from app.state import TeamState

from .nodes import create_nodes
from .routing import (
    route_after_decide,
    route_after_intake,
    route_after_reviewer,
    route_after_skills_setup,
    route_after_translator,
    route_after_translator_entry,
)


def build_graph(
    checkpointer: Any = None,
    runtime: AgentRuntime | None = None,
    settings: Settings | None = None,
):
    """Build and compile the team orchestration graph.

    ``runtime`` can be injected for tests/stubs. If omitted, nodes will try to
    use a provided runtime-less stub behavior unless a runtime is supplied later.
    """
    settings = settings or getattr(runtime, "settings", None) or get_settings()
    nodes = create_nodes(runtime=runtime, settings=settings)

    graph: StateGraph = StateGraph(TeamState)

    graph.add_node("translator_entry", nodes["translator_entry"])
    graph.add_node("pm_intake", nodes["pm_intake"])
    graph.add_node("translator", nodes["translator_node"])
    graph.add_node("pm_plan", nodes["pm_plan"])
    graph.add_node("skills_setup", nodes["skills_setup"])
    graph.add_node("research", nodes["research_node"])
    graph.add_node("builder", nodes["builder_node"])
    graph.add_node("reviewer", nodes["reviewer_node"])
    graph.add_node("security", nodes["security_node"])
    graph.add_node("qa", nodes["qa_node"])
    graph.add_node("pm_decide", nodes["pm_decide"])
    graph.add_node("commit", nodes["commit_node"])

    graph.add_edge(START, "translator_entry")

    graph.add_conditional_edges(
        "translator_entry",
        route_after_translator_entry,
        {
            "needs_input": "translator_entry",
            "intake": "pm_intake",
            "blocked": END,
        },
    )

    graph.add_conditional_edges(
        "pm_intake",
        route_after_intake,
        {
            "needs_input": "pm_intake",
            "translator_entry": "translator_entry",
            "translator": "translator",
            "blocked": END,
        },
    )

    graph.add_conditional_edges(
        "translator",
        route_after_translator,
        {
            "needs_input": "pm_intake",
            "plan": "pm_plan",
        },
    )

    graph.add_conditional_edges(
        "pm_plan",
        lambda s: "blocked" if s.get("blocked") or s.get("status") == "invalid_plan" else "skills",
        {
            "blocked": END,
            "skills": "skills_setup",
        },
    )
    graph.add_conditional_edges(
        "skills_setup",
        route_after_skills_setup,
        {
            "builder": "builder",
            "research": "research",
            "blocked": END,
        },
    )
    graph.add_edge("builder", "reviewer")
    # On-demand/forced research may return to planning/refinement or to decision.
    graph.add_conditional_edges(
        "research",
        lambda s: "plan" if str(s.get("last_decision") or s.get("research_return_to") or "") == "plan" else "decide",
        {
            "plan": "pm_plan",
            "decide": "pm_decide",
        },
    )

    graph.add_conditional_edges(
        "reviewer",
        route_after_reviewer,
        {
            "security": "security",
            "qa": "qa",
        },
    )

    graph.add_edge("security", "qa")
    graph.add_edge("qa", "pm_decide")

    graph.add_conditional_edges(
        "pm_decide",
        route_after_decide,
        {
            "fix": "builder",
            "re_review": "reviewer",
            "more_tasks": "builder",
            "research": "research",
            "plan": "pm_plan",
            "done": "commit",
            "commit_approved": "commit",
            "blocked": END,
        },
    )

    graph.add_edge("commit", END)

    return graph.compile(checkpointer=checkpointer)
