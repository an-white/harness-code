"""Tests for strict PM task schema validation and executor routing (R2)."""

from __future__ import annotations

import json
import unittest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graph import build_graph
from app.graph.nodes import create_nodes


class RoutingStubRuntime:
    """Deterministic runtime stub for schema/routing tests (no network)."""

    def __init__(self, plan_payload):
        self.plan_payload = plan_payload
        self.calls: list[tuple[str, str]] = []

    def run(self, role, messages, context=None, skill_md=None, mode="auto"):
        prompt = messages[0].content if messages else ""
        self.calls.append((role, prompt))

        if role == "translator":
            if "Translate the following user content into clear English" in prompt:
                content = prompt.split("Content:\n", 1)[1].strip() if "Content:\n" in prompt else prompt
                return {"final": content, "mode": "stub"}
            if "Localize the following user-facing text" in prompt:
                return {"final": prompt.split("Text:\n", 1)[1].strip(), "mode": "stub"}
            if "Translate/localize these clarification questions" in prompt:
                raw = prompt.split("Questions:", 1)[1].strip() if "Questions:" in prompt else "[]"
                return {"final": raw, "mode": "stub"}
            return {
                "final": '{"normalized_requirements":"Normalized requirement","open_questions":[]}',
                "mode": "stub",
            }
        if role == "project-manager":
            if "Create an ordered implementation plan" in prompt:
                payload = self.plan_payload(prompt) if callable(self.plan_payload) else self.plan_payload
                if isinstance(payload, str):
                    return {"final": payload, "mode": "stub"}
                return {"final": json.dumps(payload), "mode": "stub"}
            return {
                "final": '{"needs_clarification": false, "questions": [], "summary":"Clear requirement"}',
                "mode": "stub",
            }
        if role == "builder":
            return {"final": "Implemented changes.", "mode": "stub"}
        if role == "reviewer":
            return {"final": '{"approved": true, "findings":[]}', "mode": "stub"}
        if role == "security":
            return {"final": '{"passed": true, "findings":[]}', "mode": "stub"}
        if role == "qa":
            return {"final": '{"passed": true, "results":[{"name":"smoke","status":"pass"}]}', "mode": "stub"}
        return {"final": "ok", "mode": "stub"}


def _run_graph(runtime, state):
    graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime)
    config = {"configurable": {"thread_id": state.get("_thread", "t")}}
    init = dict(state)
    init.pop("_thread", None)
    result = graph.invoke(init, config=config)
    # auto-approve commit if interrupted
    while True:
        interrupts = getattr(graph.get_state(config), "interrupts", None) or []
        if not interrupts:
            break
        result = graph.invoke(Command(resume="approve"), config=config)
    return result, runtime, graph, config


class SchemaValidationTest(unittest.TestCase):
    def test_invalid_executor_blocks_with_actionable_state(self):
        plan = {
            "tasks": [
                {
                    "id": "task-1",
                    "kind": "implementation",
                    "executor": "unknown",
                    "objective": "Do thing",
                    "acceptance_criteria": ["ok"],
                    "target_repo_files_hint": [],
                    "risks": [],
                    "needs_security": False,
                }
            ]
        }
        result, _, _, _ = _run_graph(
            RoutingStubRuntime(plan),
            {"requirement": "Req", "interaction_language": "en", "max_fix_cycles": 2, "_thread": "badexec", "skip_onboarding": True},
        )
        self.assertTrue(result.get("blocked"))
        self.assertEqual(result.get("status"), "invalid_plan")
        self.assertEqual(result.get("last_decision"), "blocked")
        self.assertIn("executor", (result.get("plan_error") or "").lower())
        self.assertIn("executor", (result.get("error") or "").lower())

    def test_missing_required_fields_blocks(self):
        plan = {
            "tasks": [
                {
                    "id": "task-1",
                    "kind": "implementation",
                    "executor": "builder",
                    "objective": "",
                    "acceptance_criteria": [],
                    "target_repo_files_hint": "nope",
                    "risks": [],
                    "needs_security": "nope",
                }
            ]
        }
        result, _, _, _ = _run_graph(
            RoutingStubRuntime(plan),
            {"requirement": "Req", "interaction_language": "en", "max_fix_cycles": 2, "_thread": "missing", "skip_onboarding": True},
        )
        self.assertTrue(result.get("blocked"))
        self.assertEqual(result.get("status"), "invalid_plan")
        err = result.get("plan_error") or ""
        self.assertIn("objective", err)
        self.assertIn("acceptance_criteria", err)
        self.assertIn("target_repo_files_hint", err)
        self.assertIn("needs_security", err)


class BuilderRoutingTest(unittest.TestCase):
    def test_builder_task_follows_existing_verification_chain(self):
        plan = {
            "tasks": [
                {
                    "id": "task-1",
                    "kind": "implementation",
                    "executor": "builder",
                    "objective": "Implement feature",
                    "acceptance_criteria": ["criterion"],
                    "target_repo_files_hint": ["app/x.py"],
                    "risks": [],
                    "needs_security": False,
                }
            ]
        }
        result, runtime, _, _ = _run_graph(
            RoutingStubRuntime(plan),
            {"requirement": "Req", "interaction_language": "en", "max_fix_cycles": 2, "_thread": "builder", "skip_onboarding": True},
        )
        self.assertFalse(result.get("blocked"))
        roles = [r for r, _ in runtime.calls]
        self.assertIn("builder", roles)
        self.assertIn("reviewer", roles)
        self.assertIn("qa", roles)
        self.assertNotIn("security", roles)
        self.assertNotIn("research", roles)
        # chain order evidence: builder before reviewer before qa
        self.assertLess(roles.index("builder"), roles.index("reviewer"))
        self.assertLess(roles.index("reviewer"), roles.index("qa"))
        self.assertEqual((result.get("current_task") or {}).get("executor"), "builder")
        self.assertEqual((result.get("current_task") or {}).get("kind"), "implementation")


class ResearchRoutingTest(unittest.TestCase):
    def test_research_task_routes_through_research_to_pm_decide(self):
        plan = {
            "tasks": [
                {
                    "id": "task-r",
                    "kind": "research",
                    "executor": "research",
                    "objective": "Investigate API options",
                    "acceptance_criteria": ["Findings recorded"],
                    "target_repo_files_hint": [],
                    "risks": [],
                    "needs_security": False,
                    "research_question": "Which API fits?",
                }
            ]
        }
        result, runtime, _, _ = _run_graph(
            RoutingStubRuntime(plan),
            {"requirement": "Req", "interaction_language": "en", "max_fix_cycles": 2, "_thread": "research", "skip_onboarding": True},
        )
        self.assertFalse(result.get("blocked"))
        roles = [r for r, _ in runtime.calls]
        # no code review chain by default
        self.assertNotIn("builder", roles)
        self.assertNotIn("reviewer", roles)
        self.assertNotIn("qa", roles)
        self.assertNotIn("security", roles)
        self.assertEqual(result.get("current_role"), "project-manager")
        self.assertTrue(result.get("done") or result.get("commit_requested"))
        research_results = result.get("research_results") or []
        self.assertTrue(research_results)
        self.assertEqual(research_results[-1].get("status"), "completed")
        # real research execution path (non-placeholder) in R5 integration
        self.assertFalse(research_results[-1].get("placeholder", False))
        self.assertEqual(research_results[-1].get("status"), "completed")
        self.assertEqual(research_results[-1].get("task_id"), "task-r")

    def test_needs_research_hooks_representable_in_state(self):
        # Plan/state can carry research hooks; R5 routes on-demand research when signaled.
        plan = {
            "tasks": [
                {
                    "id": "task-1",
                    "kind": "implementation",
                    "executor": "builder",
                    "objective": "Implement feature",
                    "acceptance_criteria": ["ok"],
                    "target_repo_files_hint": [],
                    "risks": [],
                    "needs_security": False,
                }
            ]
        }
        result, runtime, _, _ = _run_graph(
            RoutingStubRuntime(plan),
            {
                "requirement": "Req",
                "interaction_language": "en",
                "max_fix_cycles": 2,
                "needs_research": True,
                "research_question": "What library should be used?",
                "_thread": "hooks",
                "skip_onboarding": True,
            },
        )
        # On-demand research should be triggered and bounded, with findings recorded.
        self.assertIn("research_question", result)
        self.assertGreaterEqual(int(result.get("research_cycles") or 0), 1)
        self.assertTrue(result.get("research_results"))
        roles = [r for r, _ in runtime.calls]
        self.assertIn("research", roles)


class DecisionDiagnosticsTest(unittest.TestCase):
    def test_skills_setup_on_demand_research_limit_blocks_with_reason_and_cycles(self):
        # On-demand research requested but limit already reached: skills_setup must block
        # with explicit decision_reason and cycle counters (no research execution).
        plan = {
            "tasks": [
                {
                    "id": "task-1",
                    "kind": "implementation",
                    "executor": "builder",
                    "objective": "Implement feature",
                    "acceptance_criteria": ["ok"],
                    "target_repo_files_hint": [],
                    "risks": [],
                    "needs_security": False,
                }
            ]
        }
        runtime = RoutingStubRuntime(plan)
        result, runtime, _, _ = _run_graph(
            runtime,
            {
                "requirement": "Req",
                "interaction_language": "en",
                "max_fix_cycles": 2,
                "needs_research": True,
                "research_question": "Need external info",
                "research_cycles": 2,
                "max_research_cycles": 2,
                "_thread": "research-limit",
                "skip_onboarding": True,
            },
        )
        self.assertTrue(result.get("blocked"))
        self.assertEqual(result.get("last_decision"), "blocked")
        self.assertEqual(result.get("status"), "blocked")
        self.assertIn("Research cycle limit reached (2/2)", result.get("error") or "")
        # decision_reason is not part of TeamState; assert it on pm_decide's own output.
        decision = create_nodes(runtime=runtime)["pm_decide"](result)
        self.assertIn("Research cycle limit reached (2/2)", decision.get("decision_reason") or "")
        self.assertEqual(int(result.get("research_cycles") or -1), 2)
        self.assertEqual(int(result.get("max_research_cycles") or -1), 2)
        roles = [r for r, _ in runtime.calls]
        self.assertNotIn("research", roles)
        self.assertNotIn("builder", roles)

    def test_pm_decide_on_demand_research_limit_blocks_with_reason_and_cycles(self):
        # Call pm_decide directly: a graph-level request at the cycle limit is
        # rejected earlier by skills_setup, so it never reaches this branch.
        runtime = RoutingStubRuntime({"tasks": []})
        state = {
            "current_task": {"id": "task-1", "executor": "builder", "needs_security": False},
            "needs_research": True,
            "research_question": "Need external info",
            "research_cycles": 1,
            "max_research_cycles": 1,
            "reviewed": True,
            "review_findings": [],
            "qa_passed": True,
            "qa_results": [],
            "needs_security": False,
            "security_passed": True,
            "security_findings": [],
            "fix_cycles": 0,
            "max_fix_cycles": 2,
        }
        result = create_nodes(runtime=runtime)["pm_decide"](state)
        self.assertTrue(result.get("blocked"))
        self.assertEqual(result.get("last_decision"), "blocked")
        self.assertEqual(result.get("status"), "blocked")
        self.assertIn("Research cycle limit reached (1/1)", result.get("decision_reason") or "")
        self.assertIn("Research cycle limit reached (1/1)", result.get("error") or "")
        self.assertEqual(int(result.get("research_cycles") or -1), 1)
        self.assertEqual(int(result.get("max_research_cycles") or -1), 1)


if __name__ == "__main__":
    unittest.main()
