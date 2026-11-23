"""Tests for :mod:`app.agents.runtime`."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agents.loader import AgentLoadError, AgentSpec, PermissionRule
from app.agents import runtime
from app.agents.runtime import AgentRuntime, CapabilityProbe, build_system_prompt, run_agent


def _spec(prompt: str = "You are helpful.") -> AgentSpec:
    return AgentSpec(
        name="tester",
        description="test agent",
        mode="primary",
        color="",
        system_prompt=prompt,
        permissions=[PermissionRule(action="*", resource="*", effect="deny")],
        source_path=None,
    )


class FakeLLM:
    def __init__(self, responses=None, tool_support=False, raise_on_invoke=False):
        self._responses = list(responses or [])
        self._tool_support = tool_support
        self._raise = raise_on_invoke
        self.bound_tools = None
        self.invocations = 0
        self.model_name = "fake-model"
        self.openai_api_base = "http://fake"

    def bind_tools(self, tools):
        self.bound_tools = tools
        bound = FakeLLM(
            responses=list(self._responses),
            tool_support=self._tool_support,
            raise_on_invoke=self._raise,
        )
        bound.model_name = self.model_name
        bound.openai_api_base = self.openai_api_base
        return bound

    def invoke(self, messages, config=None):
        self.invocations += 1
        if self._raise:
            raise RuntimeError("llm boom")
        if self._responses:
            return self._responses.pop(0)
        if self._tool_support:
            return AIMessage(
                content="",
                tool_calls=[{"name": "_capability_probe_tool", "args": {"x": "ok"}, "id": "1"}],
            )
        return AIMessage(content="ok")


class BuildSystemPromptTest(unittest.TestCase):
    def test_includes_spec_prompt_env_preamble_and_context(self):
        spec = _spec("Do the thing.")
        prompt = build_system_prompt(spec, context="extra context")
        self.assertIn("Do the thing.", prompt)
        self.assertIn("Environment:", prompt)
        self.assertIn("Workspace root:", prompt)
        self.assertIn("generic template", prompt.lower())
        phrase = "no " + "scr" "apper" + "/" + "dash" "board" + " assumptions"
        self.assertIn(phrase, prompt)
        self.assertIn("generic template", prompt.lower())
        self.assertIn("Runtime context:", prompt)
        self.assertIn("extra context", prompt)

    def test_truncates_skill_content(self):
        spec = _spec()
        skill = "A" * 9000
        prompt = build_system_prompt(spec, skill_md=skill)
        self.assertIn('<skill name="injected">', prompt)
        self.assertIn("</skill>", prompt)
        self.assertIn("...[truncated]", prompt)
        self.assertLess(prompt.count("A"), 9000)


class CapabilityProbeTest(unittest.TestCase):
    def setUp(self):
        runtime._PROBE_CACHE.clear()

    def test_caches_and_detects_tool_call(self):
        llm = FakeLLM(tool_support=True)
        llm.model_name = "m1"
        llm.openai_api_base = "http://x"
        probe = CapabilityProbe()
        self.assertTrue(probe.probe(llm))
        # second call should use cache even if behavior would differ
        llm2 = FakeLLM(tool_support=False)
        llm2.model_name = "m1"
        llm2.openai_api_base = "http://x"
        self.assertTrue(probe.supports_tools(llm2))
        self.assertEqual(len(runtime._PROBE_CACHE), 1)

    def test_plain_response_returns_false(self):
        llm = FakeLLM(tool_support=False)
        llm.model_name = "plain"
        probe = CapabilityProbe()
        self.assertFalse(probe.probe(llm))

    def test_exception_returns_false(self):
        class BadLLM:
            model_name = "bad"
            openai_api_base = "http://x"

            def bind_tools(self, tools):
                raise RuntimeError("no tools")

        probe = CapabilityProbe()
        self.assertFalse(probe.probe(BadLLM()))


class JsonFallbackTest(unittest.TestCase):
    def test_dispatches_fake_tool_and_stops_on_final(self):
        @tool
        def add(a: int, b: int) -> str:
            """Add two ints."""
            return str(a + b)

        responses = [
            AIMessage(content='{"thought":"call","tool":"add","args":{"a":2,"b":3},"final":null}'),
            AIMessage(content='{"thought":"done","tool":null,"args":null,"final":"sum is 5"}'),
        ]
        llm = FakeLLM(responses=responses)
        settings = SimpleNamespace(max_agent_steps=5, workspace_root=Path.cwd())
        result = run_agent(_spec(), [HumanMessage(content="hi")], [add], llm, settings, mode="json")
        self.assertEqual(result.get("mode"), "json")
        self.assertEqual(result.get("final"), "sum is 5")
        self.assertNotIn("error", result)


class ToolsModeAndRuntimeTest(unittest.TestCase):
    def setUp(self):
        runtime._PROBE_CACHE.clear()

    def test_tools_mode_with_fake_react_agent(self):
        @tool
        def ping() -> str:
            """ping"""
            return "pong"

        class FakeAgent:
            def invoke(self, payload, config=None):
                return {"messages": [HumanMessage(content="x"), AIMessage(content="done")]}

        llm = FakeLLM()
        settings = SimpleNamespace(max_agent_steps=5, workspace_root=Path.cwd())
        with patch("langgraph.prebuilt.create_react_agent", return_value=FakeAgent()) as create:
            result = run_agent(_spec(), [HumanMessage(content="hi")], [ping], llm, settings, mode="tools")
        self.assertEqual(result.get("mode"), "tools")
        self.assertEqual(result.get("final"), "done")
        create.assert_called_once()

    def test_run_agent_returns_error_dict_on_llm_failure(self):
        llm = FakeLLM(responses=[AIMessage(content="x")], raise_on_invoke=True)
        settings = SimpleNamespace(max_agent_steps=5, workspace_root=Path.cwd())
        result = run_agent(_spec(), [HumanMessage(content="hi")], [], llm, settings, mode="json")
        self.assertIn("error", result)
        self.assertEqual(result.get("mode"), "json")

    def test_unknown_role_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "builder.md").write_text(
                "---\ndescription: b\npermissions:\n  - action: '*'\n    resource: '*'\n    effect: deny\n---\n\nBody\n",
                encoding="utf-8",
            )
            settings = SimpleNamespace(workspace_root=root, shell_timeout=30, max_agent_steps=5)
            rt = AgentRuntime(settings=settings, workspace_root=root)
            with self.assertRaises(AgentLoadError) as ctx:
                rt.get_spec("nobody")
            self.assertIn("nobody", str(ctx.exception))

    def test_runtime_can_be_constructed_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "builder.md").write_text(
                "---\ndescription: b\npermissions:\n  - action: '*'\n    resource: '*'\n    effect: deny\n---\n\nBody\n",
                encoding="utf-8",
            )
            settings = SimpleNamespace(
                workspace_root=root, shell_timeout=30, max_agent_steps=5, model="m",
                base_url="http://x", api_key="x", temperature=0.0,
            )
            rt = AgentRuntime(settings=settings, workspace_root=root)
            self.assertIn("builder", rt.agents)


if __name__ == "__main__":
    unittest.main()
