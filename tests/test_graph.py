"""Tests for the LangGraph team orchestration."""

from __future__ import annotations

import unittest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graph import build_graph
from app.graph.nodes import create_nodes


class StubRuntime:
    """Deterministic runtime stub to exercise the graph without network."""

    def __init__(self, behavior: dict | None = None):
        self.behavior = behavior or {}
        self.calls: list[tuple[str, str]] = []

    def run(self, role, messages, context=None, skill_md=None, mode="auto"):
        prompt = messages[0].content if messages else ""
        self.calls.append((role, prompt))
        role_behavior = self.behavior.get(role)
        if callable(role_behavior):
            return role_behavior(prompt, self)
        if isinstance(role_behavior, dict):
            return dict(role_behavior)
        if isinstance(role_behavior, str):
            return {"final": role_behavior, "mode": "stub"}

        if role == "translator":
            # entry translation/localization prompts vs normalization
            if "Translate the following user content into clear English" in prompt:
                content = prompt.split("Content:\n", 1)[1].strip() if "Content:\n" in prompt else prompt
                translations = {
                    "Algo vago": "Something vague",
                    "alcance completo": "full scope",
                }
                return {"final": translations.get(content, content), "mode": "stub"}
            if "Localize the following user-facing text" in prompt or "Translate/localize these clarification questions" in prompt:
                # keep text/questions unchanged for simplicity
                if "Questions:" in prompt:
                    q = prompt.split("Questions:", 1)[1].strip()
                    return {"final": q, "mode": "stub"}
                if "Text:\n" in prompt:
                    return {"final": prompt.split("Text:\n", 1)[1].strip(), "mode": "stub"}
                return {"final": prompt, "mode": "stub"}
            return {
                "final": '{"normalized_requirements":"Normalized requirement","open_questions":[]}',
                "mode": "stub",
            }
        if role == "project-manager":
            if "ordered implementation plan" in prompt or "Create an ordered implementation plan" in prompt:
                needs_security = bool(self.behavior.get("needs_security", False))
                return {
                    "final": (
                        '{"tasks":[{"id":"task-1","kind":"implementation","executor":"builder","objective":"Do work",'
                        '"acceptance_criteria":["ok"],"target_repo_files_hint":[],"risks":[],"needs_security":'
                        + ("true" if needs_security else "false")
                        + "} ]}"
                    ),
                    "mode": "stub",
                }
            return {
                "final": '{"needs_clarification": false, "questions": [], "summary":"Clear requirement"}',
                "mode": "stub",
            }
        if role == "builder":
            return {"final": "Implemented changes.", "mode": "stub"}
        if role == "reviewer":
            count = sum(1 for r, _ in self.calls if r == "reviewer")
            fail_first = bool(self.behavior.get("review_fail_first"))
            if fail_first and count == 1:
                return {
                    "final": '{"approved": false, "findings":[{"severity":"high","message":"fix needed","blocking":true}]}',
                    "mode": "stub",
                }
            return {"final": '{"approved": true, "findings":[]}', "mode": "stub"}
        if role == "security":
            return {"final": '{"passed": true, "findings":[]}', "mode": "stub"}
        if role == "qa":
            return {"final": '{"passed": true, "results":[{"name":"smoke","status":"pass"}]}', "mode": "stub"}
        return {"final": "ok", "mode": "stub"}


class GraphBuildTest(unittest.TestCase):
    def test_graph_builds(self):
        graph = build_graph(runtime=StubRuntime())
        self.assertIsNotNone(graph)


class HappyPathTest(unittest.TestCase):
    def test_happy_path_flows_to_done(self):
        runtime = StubRuntime()
        graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime)
        config = {"configurable": {"thread_id": "happy"}}
        result = graph.invoke(
            {"requirement": "Add feature X", "max_fix_cycles": 3, "interaction_language": "en", "skip_onboarding": True},
            config=config,
        )
        # commit approval interrupt expected
        interrupts = getattr(graph.get_state(config), "interrupts", None) or []
        self.assertTrue(interrupts)
        result = graph.invoke(Command(resume="approve"), config=config)
        self.assertTrue(result.get("done"))
        self.assertTrue(result.get("commit_requested"))
        self.assertEqual(result.get("status"), "commit_recorded")


class FixCycleTest(unittest.TestCase):
    def test_fix_cycle_routes_back_and_respects_max(self):
        runtime = StubRuntime({"review_fail_first": True})
        graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime)
        config = {"configurable": {"thread_id": "fix"}}
        # First run will hit commit approval only after fix cycle resolves? review fails once then passes.
        result = graph.invoke({"requirement": "Fix bug", "max_fix_cycles": 3, "interaction_language": "en", "skip_onboarding": True}, config=config)
        interrupts = getattr(graph.get_state(config), "interrupts", None) or []
        if interrupts:
            result = graph.invoke(Command(resume="approve"), config=config)
        self.assertTrue(result.get("done"))
        builder_calls = [r for r, _ in runtime.calls if r == "builder"]
        self.assertGreaterEqual(len(builder_calls), 2)

    def test_respects_max_fix_cycles(self):
        runtime = StubRuntime(
            {
                "reviewer": {
                    "final": '{"approved": false, "findings":[{"severity":"high","message":"bad","blocking":true}]}',
                    "mode": "stub",
                }
            }
        )
        graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime)
        config = {"configurable": {"thread_id": "maxfix"}}
        result = graph.invoke({"requirement": "Broken", "max_fix_cycles": 1, "interaction_language": "en", "skip_onboarding": True}, config=config)
        self.assertTrue(result.get("blocked"))
        self.assertEqual(result.get("last_decision"), "blocked")
        self.assertIn("Max fix cycles reached (1/1)", result.get("error") or "")
        self.assertEqual(int(result.get("fix_cycles") or -1), 1)
        self.assertEqual(int(result.get("max_fix_cycles") or -1), 1)
        # decision_reason is not part of TeamState, so assert it on pm_decide's own output.
        decision = create_nodes(runtime=runtime)["pm_decide"](result)
        self.assertIn("Max fix cycles reached (1/1)", decision.get("decision_reason") or "")


class SecurityBranchTest(unittest.TestCase):
    def test_security_branch_taken_when_needed(self):
        runtime = StubRuntime({"needs_security": True})
        graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime)
        config = {"configurable": {"thread_id": "sec"}}
        result = graph.invoke({"requirement": "Auth change", "max_fix_cycles": 3, "interaction_language": "en", "skip_onboarding": True}, config=config)
        interrupts = getattr(graph.get_state(config), "interrupts", None) or []
        if interrupts:
            result = graph.invoke(Command(resume="approve"), config=config)
        roles = [r for r, _ in runtime.calls]
        self.assertIn("security", roles)
        self.assertTrue(result.get("done"))


class IntakeInterruptTest(unittest.TestCase):
    def test_intake_interrupt_resume_path(self):
        runtime = StubRuntime(
            {
                "project-manager": {
                    "final": '{"needs_clarification": true, "questions": ["¿Cuál es el alcance?"]}',
                    "mode": "stub",
                }
            }
        )
        # For plan step we need normal behavior: override by callable based on prompt
        calls = {"intake": 0}

        def pm_behavior(prompt, rt):
            if "Create an ordered implementation plan" in prompt:
                return {
                    "final": '{"tasks":[{"id":"task-1","kind":"implementation","executor":"builder","objective":"Do it","acceptance_criteria":["criterion met"],"target_repo_files_hint":[],"risks":[],"needs_security":false}]}',
                    "mode": "stub",
                }
            calls["intake"] += 1
            if calls["intake"] == 1:
                return {
                    "final": '{"needs_clarification": true, "questions": ["¿Cuál es el alcance?"]}',
                    "mode": "stub",
                }
            return {
                "final": '{"needs_clarification": false, "questions": [], "summary":"Clear requirement"}',
                "mode": "stub",
            }

        runtime.behavior["project-manager"] = pm_behavior
        graph = build_graph(checkpointer=InMemorySaver(), runtime=runtime)
        config = {"configurable": {"thread_id": "intake"}}
        result = graph.invoke(
            {"requirement": "Algo vago", "max_fix_cycles": 2, "interaction_language": "es", "skip_onboarding": True},
            config=config,
        )
        interrupts = getattr(graph.get_state(config), "interrupts", None) or []
        self.assertTrue(interrupts, "expected intake interrupt")
        # resume with clarification
        result = graph.invoke(Command(resume="alcance completo"), config=config)
        interrupts = getattr(graph.get_state(config), "interrupts", None) or []
        # may hit commit approval interrupt
        if interrupts:
            result = graph.invoke(Command(resume="aprobar"), config=config)
        self.assertTrue(result.get("done") or result.get("commit_requested"))
        # Downstream answers must be English-only; originals preserved separately.
        answers = result.get("answers") or []
        self.assertTrue(answers)
        self.assertNotIn("alcance completo", answers[-1])
        raw = result.get("raw_answers") or []
        self.assertIn("alcance completo", raw)


if __name__ == "__main__":
    unittest.main()
