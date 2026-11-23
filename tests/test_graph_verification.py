"""Offline regression tests for deterministic QA evidence and graph termination."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.config import get_settings
from app.graph import build_graph
from app.graph.nodes import create_nodes
from app.graph.routing import has_blocking_findings
from app.qa.checks import capture_task_baseline

GIT = shutil.which("git") is not None
requires_git = unittest.skipUnless(GIT, "git is not installed")


class VerificationRuntime:
    """Deterministic runtime stub; no network and no real LLM."""

    def __init__(
        self,
        root: Path,
        *,
        write_file: str | None = None,
        llm_qa_fail: bool = False,
        llm_qa_blocking: bool = False,
        empty_hint: bool = False,
        needs_security: bool = False,
        security_response: dict | None = None,
    ):
        self.root = root
        self.write_file = write_file
        self.llm_qa_fail = llm_qa_fail
        self.llm_qa_blocking = llm_qa_blocking
        self.empty_hint = empty_hint
        self.needs_security = needs_security
        self.security_response = security_response
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def run(self, role, messages, context=None, skill_md=None, mode="auto", web_consent=None):
        prompt = messages[0].content if messages else ""
        self.calls.append(role)
        self.prompts.append(prompt)
        if role == "translator":
            if "Translate the following user content" in prompt:
                return {"final": "Create hello world", "mode": "stub"}
            return {
                "final": '{"normalized_requirements":"Create hello world","open_questions":[]}',
                "mode": "stub",
            }
        if role == "project-manager":
            if "implementation plan" in prompt:
                hint = "[]" if self.empty_hint else '["%s"]' % (self.write_file or "hello.py")
                task = (
                    '{"id":"task-1","kind":"implementation","executor":"builder",'
                    '"objective":"Create hello world","acceptance_criteria":["prints hello"],'
                    '"target_repo_files_hint":%s,"risks":[],"needs_security":%s'
                    % (hint, "true" if self.needs_security else "false")
                )
                if self.write_file:
                    task += ',"verification_commands":["%s -m py_compile %s"]' % (
                        sys.executable,
                        self.write_file,
                    )
                return {"final": '{"tasks":[' + task + "}]}", "mode": "stub"}
            return {"final": '{"needs_clarification":false,"questions":[]}', "mode": "stub"}
        if role == "builder":
            if self.write_file:
                content = "def broken(:\n" if self.write_file == "broken.py" else "print('hello world')\n"
                (self.root / self.write_file).write_text(content, encoding="utf-8")
            return {"final": "Implemented.", "mode": "stub"}
        if role == "reviewer":
            return {"final": '{"approved":true,"findings":[]}', "mode": "stub"}
        if role == "qa":
            if self.llm_qa_blocking:
                # A marked "blocking" opinion still has no file:line evidence.
                return {
                    "final": json.dumps(
                        {
                            "passed": False,
                            "results": [
                                {
                                    "name": "vibe",
                                    "status": "fail",
                                    "severity": "high",
                                    "blocking": True,
                                    "details": "style",
                                }
                            ],
                        }
                    ),
                    "mode": "stub",
                }
            if self.llm_qa_fail:
                # A non-blocking opinion must not override deterministic evidence.
                return {
                    "final": '{"passed":false,"results":[{"name":"vibe","status":"fail","details":"style"}]}',
                    "mode": "stub",
                }
            return {"final": '{"passed":true,"results":[]}', "mode": "stub"}
        if role == "security" and self.security_response is not None:
            return self.security_response
        return {"final": "ok", "mode": "stub"}


class VerificationGraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        if GIT:
            subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.settings = replace(
            get_settings(),
            workspace_root=self.root,
            max_fix_cycles=3,
            research_onboarding="never",
            research_max_cycles=1,
            web_access="deny",
            interaction_language="en",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _initial(self, thread: str, **overrides):
        return {
            "requirement": "Create hello world",
            "interaction_language": "en",
            "skip_onboarding": True,
            "max_fix_cycles": 3,
            **overrides,
        }

    def _graph(self, runtime: VerificationRuntime):
        return build_graph(checkpointer=InMemorySaver(), runtime=runtime, settings=self.settings)

    @staticmethod
    def _drain(graph, initial, config) -> tuple[list[str], dict]:
        visited: list[str] = []
        for chunk in graph.stream(initial, config=config, stream_mode="updates"):
            visited.extend(chunk.keys())
        return visited, graph.get_state(config).values or {}

    @requires_git
    def test_hello_world_passes_qa_and_terminates_with_bounded_visits(self):
        runtime = VerificationRuntime(self.root, write_file="hello.py", llm_qa_fail=True)
        graph = self._graph(runtime)
        config = {"configurable": {"thread_id": "hello"}}

        visited, _ = self._drain(graph, self._initial("hello"), config)
        interrupts = getattr(graph.get_state(config), "interrupts", None) or []
        self.assertTrue(interrupts, "expected the commit approval interrupt")
        resumed, final = self._drain(graph, Command(resume="approve"), config)
        visited.extend(resumed)

        self.assertEqual(visited.count("builder"), 1)
        self.assertEqual(visited.count("qa"), 1)
        self.assertLess(visited.index("qa"), visited.index("pm_decide"))
        self.assertEqual(visited[-1], "commit")
        self.assertTrue(final.get("done"))
        self.assertTrue(final.get("qa_passed"))
        self.assertEqual(final.get("commit_sha"), "pending-manual-commit")
        names = [r.get("name") for r in final.get("qa_results") or []]
        self.assertEqual(
            names,
            ["vibe", "scope", "python_syntax", "verification_commands"],
        )
        by_name = {r["name"]: r for r in final["qa_results"]}
        for name in ("scope", "python_syntax", "verification_commands"):
            self.assertEqual(by_name[name]["status"], "pass", by_name[name])
            self.assertEqual(by_name[name]["source"], "deterministic")
        self.assertEqual(by_name["vibe"]["status"], "advisory")
        self.assertEqual(int(final.get("fix_cycles") or 0), 0)

    @requires_git
    def test_empty_scope_hint_skips_scope_but_still_passes_qa(self):
        runtime = VerificationRuntime(
            self.root, write_file="hello.py", empty_hint=True, llm_qa_blocking=True
        )
        graph = self._graph(runtime)
        config = {"configurable": {"thread_id": "empty-hint"}}

        visited, _ = self._drain(graph, self._initial("empty-hint"), config)
        self.assertTrue(getattr(graph.get_state(config), "interrupts", None))
        resumed, final = self._drain(graph, Command(resume="approve"), config)
        visited.extend(resumed)

        self.assertEqual(visited.count("builder"), 1)
        self.assertEqual(visited.count("qa"), 1)
        self.assertEqual(int(final.get("fix_cycles") or 0), 0)
        self.assertTrue(final.get("qa_passed"))
        self.assertTrue(final.get("done"))
        by_name = {r["name"]: r for r in final["qa_results"]}
        self.assertEqual(by_name["scope"]["status"], "skipped")
        self.assertIn("no scope supplied", by_name["scope"]["details"])
        self.assertEqual(by_name["python_syntax"]["status"], "pass")
        self.assertEqual(by_name["verification_commands"]["status"], "pass")
        self.assertEqual(by_name["vibe"]["status"], "advisory")

    @requires_git
    def test_blocking_llm_opinion_cannot_override_passing_deterministic_checks(self):
        runtime = VerificationRuntime(
            self.root, write_file="hello.py", llm_qa_blocking=True
        )
        graph = self._graph(runtime)
        config = {"configurable": {"thread_id": "llm-opinion"}}

        visited, _ = self._drain(graph, self._initial("llm-opinion"), config)
        self.assertTrue(getattr(graph.get_state(config), "interrupts", None))
        _, final = self._drain(graph, Command(resume="approve"), config)

        self.assertTrue(final.get("qa_passed"))
        self.assertEqual(int(final.get("fix_cycles") or 0), 0)
        self.assertEqual(visited.count("builder"), 1)
        self.assertEqual(visited.count("qa"), 1)
        vibe = [r for r in final["qa_results"] if r.get("name") == "vibe"]
        self.assertEqual(len(vibe), 1)
        self.assertEqual(vibe[0]["status"], "advisory")
        self.assertFalse(vibe[0]["blocking"])
        self.assertTrue(vibe[0]["advisory"])

    @requires_git
    def test_failing_syntax_blocks_after_exactly_one_fix_cycle(self):
        runtime = VerificationRuntime(self.root, write_file="broken.py")
        graph = self._graph(runtime)
        config = {"configurable": {"thread_id": "broken"}}
        visited, final = self._drain(
            graph, self._initial("broken", max_fix_cycles=1), config
        )

        self.assertTrue(final.get("blocked"))
        self.assertEqual(visited.count("builder"), 2)
        self.assertEqual(visited.count("qa"), 2)
        self.assertNotIn("commit", visited)
        self.assertFalse(final.get("qa_passed"))
        self.assertEqual(int(final.get("fix_cycles") or -1), 1)
        self.assertIn("Max fix cycles reached (1/1)", final.get("error") or "")
        self.assertIn("QA python_syntax", final.get("error") or "")
        syntax = [r for r in final["qa_results"] if r.get("name") == "python_syntax"]
        self.assertEqual(len(syntax), 1)
        self.assertEqual(syntax[0]["status"], "fail")

    @requires_git
    def test_commit_approval_ends_without_rerunning_builder(self):
        runtime = VerificationRuntime(self.root)
        graph = self._graph(runtime)
        config = {"configurable": {"thread_id": "approval"}}
        visited, _ = self._drain(graph, self._initial("approval"), config)
        self.assertTrue(getattr(graph.get_state(config), "interrupts", None))
        builders_before = runtime.calls.count("builder")
        resumed, final = self._drain(graph, Command(resume="approve"), config)

        self.assertEqual(visited.count("builder"), 1)
        self.assertNotIn("builder", resumed)
        self.assertEqual(resumed[-1], "commit")
        self.assertEqual(runtime.calls.count("builder"), builders_before)
        self.assertTrue(final.get("done"))
        self.assertTrue(final.get("commit_requested"))

    @requires_git
    def test_unverifiable_evidence_blocks_without_fix_cycles(self):
        runtime = VerificationRuntime(self.root, write_file="hello.py")
        graph = self._graph(runtime)
        config = {"configurable": {"thread_id": "unverifiable"}}
        with patch(
            "app.graph.nodes.detect_scope_violations",
            return_value={"skipped": True, "reason": "test: evidence unavailable", "changed": [], "violations": []},
        ):
            visited, final = self._drain(graph, self._initial("unverifiable"), config)

        self.assertTrue(final.get("blocked"))
        self.assertEqual(visited.count("builder"), 1)
        self.assertEqual(visited.count("qa"), 1)
        self.assertNotIn("commit", visited)
        self.assertEqual(int(final.get("fix_cycles") or 0), 0)
        self.assertIn("QA evidence unverifiable", final.get("error") or "")
        statuses = [(r.get("name"), r.get("status")) for r in final["qa_results"]]
        self.assertIn(("scope", "unverifiable"), statuses)
        self.assertIn(("python_syntax", "unverifiable"), statuses)

    @requires_git
    def test_no_deterministic_evidence_is_unverifiable_even_with_blocking_llm_opinion(self):
        runtime = VerificationRuntime(self.root, llm_qa_blocking=True)
        graph = self._graph(runtime)
        config = {"configurable": {"thread_id": "no-evidence"}}
        with patch(
            "app.graph.nodes.detect_scope_violations",
            return_value={"skipped": True, "reason": "test: evidence unavailable", "changed": [], "violations": []},
        ):
            visited, final = self._drain(graph, self._initial("no-evidence"), config)

        self.assertTrue(final.get("blocked"))
        self.assertFalse(final.get("qa_passed"))
        self.assertEqual(int(final.get("fix_cycles") or 0), 0)
        self.assertEqual(visited.count("builder"), 1)
        self.assertEqual(visited.count("qa"), 1)
        statuses = [(r.get("name"), r.get("status")) for r in final["qa_results"]]
        self.assertIn(("evidence", "unverifiable"), statuses)
        self.assertIn("QA evidence unverifiable", final.get("error") or "")

    @requires_git
    def test_security_prompt_uses_files_and_blocking_findings_need_file_line(self):
        baseline = capture_task_baseline(self.root)
        (self.root / "hello.py").write_text("API_KEY = 'secret'\n", encoding="utf-8")
        task = {
            "id": "task-1",
            "kind": "implementation",
            "executor": "builder",
            "objective": "Create hello world",
            "acceptance_criteria": ["ok"],
            "target_repo_files_hint": ["hello.py"],
            "risks": [],
            "needs_security": True,
        }
        state = {"current_task": task, "qa_baseline": baseline, "artifacts": ["summary only"]}

        def check(finding: dict) -> tuple[dict, VerificationRuntime]:
            response = {
                "final": json.dumps({"passed": False, "findings": [finding]}),
                "mode": "stub",
            }
            runtime = VerificationRuntime(self.root, security_response=response)
            result = create_nodes(runtime=runtime, settings=self.settings)["security_node"](state)
            return result, runtime

        speculative, runtime = check({"severity": "high", "message": "hardcoded secret", "blocking": True})
        self.assertIn('"path": "hello.py"', runtime.prompts[0])
        self.assertIn("1|API_KEY", runtime.prompts[0])
        self.assertIn('"evidence": "path.py:12"', runtime.prompts[0])
        self.assertIn("first 2000 characters per file", runtime.prompts[0])
        self.assertFalse(has_blocking_findings(speculative["security_findings"]))
        self.assertEqual(speculative["security_findings"][0]["status"], "advisory")

        for bad_citation in ("fake.py:1", "fake/hello.py:1", "hello.py:99"):
            invalid, _ = check(
                {
                    "severity": "critical",
                    "message": "hardcoded secret",
                    "blocking": True,
                    "evidence": bad_citation,
                }
            )
            self.assertFalse(
                has_blocking_findings(invalid["security_findings"]),
                bad_citation,
            )
            self.assertEqual(invalid["security_findings"][0]["status"], "advisory")

        evidenced, _ = check(
            {
                "severity": "high",
                "message": "hardcoded secret",
                "blocking": True,
                "evidence": "hello.py:1",
            }
        )
        self.assertTrue(has_blocking_findings(evidenced["security_findings"]))
        self.assertFalse(evidenced["security_passed"])

        prefixed, _ = check(
            {
                "severity": "high",
                "message": "hardcoded secret",
                "blocking": True,
                "evidence": "workspace/hello.py:1",
            }
        )
        self.assertTrue(has_blocking_findings(prefixed["security_findings"]))
        self.assertFalse(prefixed["security_passed"])
        self.assertEqual(prefixed["security_findings"][0]["severity"], "high")

    def test_missing_security_evidence_blocks_with_explicit_reason(self):
        runtime = VerificationRuntime(self.root)
        nodes = create_nodes(runtime=runtime, settings=self.settings)
        state = {
            "current_task": {"executor": "builder", "needs_security": True},
            "needs_security": True,
            "qa_baseline": {},
            "artifacts": [],
            "qa_results": [],
            "qa_passed": True,
            "reviewed": True,
            "review_findings": [],
            "code_changed_since_review": False,
            "fix_cycles": 0,
            "max_fix_cycles": 3,
            "research_cycles": 0,
            "max_research_cycles": 1,
        }
        with patch(
            "app.graph.nodes._changed_file_evidence",
            return_value=(
                {
                    "skipped": True,
                    "reason": "test: no changed-file evidence",
                    "changed": [],
                    "violations": [],
                },
                [],
            ),
        ):
            security_update = nodes["security_node"](state)
        state.update(security_update)
        decision = nodes["pm_decide"](state)

        self.assertFalse(security_update["security_passed"])
        self.assertTrue(
            any(
                f.get("status") == "unverifiable" and f.get("details")
                for f in security_update["security_findings"]
            )
        )
        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["last_decision"], "blocked")
        self.assertIn("Security evidence unverifiable", decision["error"])

    @requires_git
    def test_no_security_need_is_unaffected_by_empty_evidence(self):
        runtime = VerificationRuntime(self.root, write_file="hello.py")
        state = {
            "current_task": {"needs_security": False},
            "needs_security": False,
            "qa_baseline": {},
            "artifacts": [],
        }
        with patch(
            "app.graph.nodes._changed_file_evidence",
            return_value=({"skipped": False, "changed": [], "violations": []}, []),
        ):
            result = create_nodes(runtime=runtime, settings=self.settings)["security_node"](state)
        self.assertTrue(result["security_passed"])
        self.assertEqual(result["security_findings"], [])


if __name__ == "__main__":
    unittest.main()
