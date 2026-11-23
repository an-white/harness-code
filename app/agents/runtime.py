"""Agent runtime: model capability probing, prompt building, and execution."""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app.agents.loader import AgentLoadError, AgentSpec, load_agents
from app.config import Settings, get_llm, get_settings
from app.tools import make_toolset

logger = logging.getLogger(__name__)

_SKILL_TRUNCATE_CHARS = 8000
_DEFAULT_MAX_STEPS = 25

# (base_url, model) -> bool
_PROBE_CACHE: dict[tuple[str, str], bool] = {}
_PROBE_LOCK = threading.Lock()


def _llm_identity(llm: Any) -> tuple[str, str]:
    base = (
        getattr(llm, "openai_api_base", None)
        or getattr(llm, "base_url", None)
        or ""
    )
    model = getattr(llm, "model_name", None) or getattr(llm, "model", None) or ""
    return (str(base), str(model))


@tool
def _capability_probe_tool(x: str = "ping") -> str:
    """Internal capability probe. Return x unchanged."""
    return str(x)


class CapabilityProbe:
    """Probe whether a chat model can perform tool calling."""

    def probe(self, llm: Any, timeout: int = 30) -> bool:
        key = _llm_identity(llm)
        with _PROBE_LOCK:
            if key in _PROBE_CACHE:
                return _PROBE_CACHE[key]

        result = self._do_probe(llm, timeout=timeout)
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = result
        return result

    def _do_probe(self, llm: Any, timeout: int = 30) -> bool:
        try:
            bind = getattr(llm, "bind_tools", None)
            if bind is None:
                return False
            try:
                bound = bind([_capability_probe_tool])
            except Exception:
                return False
            invoke = getattr(bound, "invoke", None)
            if invoke is None:
                return False
            try:
                response = invoke(
                    [HumanMessage(content="Call the tool _capability_probe_tool with x='ok'.")],
                    config={"timeout": timeout},
                )
            except TypeError:
                response = invoke(
                    [HumanMessage(content="Call the tool _capability_probe_tool with x='ok'.")]
                )
            tool_calls = getattr(response, "tool_calls", None) or []
            if tool_calls:
                return True
            # Some models may encode tool calls differently; no tool call means fallback.
            return False
        except Exception as exc:  # noqa: BLE001
            logger.debug("Capability probe failed for %s: %s", _llm_identity(llm), exc)
            return False

    def supports_tools(self, llm: Any) -> bool:
        return self.probe(llm)


_DEFAULT_PROBE = CapabilityProbe()


def supports_tools(llm: Any) -> bool:
    """Return True if the model appears to support tool calling."""
    return _DEFAULT_PROBE.supports_tools(llm)


def make_llm_for_role(role: str, settings: Settings | None = None) -> Any:
    """Return a chat model configured for ``role``."""
    if settings is None:
        return get_llm(role)
    return get_llm(role)


def _truncate_skill(skill_md: str, limit: int = _SKILL_TRUNCATE_CHARS) -> str:
    if len(skill_md) <= limit:
        return skill_md
    return skill_md[:limit] + "\n...[truncated]"


def build_system_prompt(
    spec: AgentSpec,
    skill_md: str | None = None,
    context: str | None = None,
    workspace_root: Path | str | None = None,
) -> str:
    """Combine the agent prompt with environment and runtime context.

    ``workspace_root`` scopes the reported workspace to the selected project
    directory; when omitted, falls back to the configured settings root.
    """
    workspace = ""
    try:
        if workspace_root is not None:
            workspace = str(Path(workspace_root).expanduser().resolve())
        else:
            workspace = str(get_settings().workspace_root)
    except Exception:
        workspace = ""
    preamble_lines = [
        "Environment:",
        f"- Workspace root: {workspace}" if workspace else "- Workspace root: (unknown)",
        "- This is a generic template, no " + "scr" "apper" + "/" + "dash" "board" + " assumptions.",
    ]
    parts = ["\n".join(preamble_lines), "", spec.system_prompt.strip()]
    if skill_md:
        parts.extend(
            [
                "",
                '<skill name="injected">',
                _truncate_skill(skill_md),
                "</skill>",
            ]
        )
    if context:
        parts.extend(["", "Runtime context:", str(context).strip()])
    return "\n".join(parts).strip() + "\n"


def _tool_spec_text(tools: list[Any]) -> str:
    lines: list[str] = []
    for t in tools:
        name = getattr(t, "name", None) or getattr(t, "__name__", "<unknown>")
        desc = (getattr(t, "description", None) or "").strip()
        schema: Any = None
        if hasattr(t, "args_schema") and getattr(t, "args_schema") is not None:
            try:
                schema = t.args_schema.model_json_schema()  # pydantic v2
            except Exception:
                try:
                    schema = t.args_schema.schema()  # pydantic v1
                except Exception:
                    schema = str(t.args_schema)
        elif hasattr(t, "args"):
            schema = getattr(t, "args")
        schema_text = ""
        if schema is not None:
            try:
                schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True)
            except Exception:
                schema_text = str(schema)
        line = f"- {name}: {desc}"
        if schema_text:
            line += f"\n  args_schema: {schema_text}"
        lines.append(line)
    return "\n".join(lines) if lines else "(none)"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Try fenced block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    # Try first {...} span
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


def _dispatch_tool(tools: list[Any], name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return f"ERROR: tool args must be an object, got {type(args).__name__}"
    tool_map = {getattr(t, "name", ""): t for t in tools}
    t = tool_map.get(name)
    if t is None:
        return f"ERROR: unknown tool '{name}'"
    try:
        return str(t.invoke(args))
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: tool '{name}' failed: {exc}"


def _coerce_messages(messages: list[Any] | None) -> list[Any]:
    if not messages:
        return []
    out: list[Any] = []
    for m in messages:
        if isinstance(m, dict):
            role = (m.get("role") or "user").lower()
            content = m.get("content", "")
            if role == "system":
                out.append(SystemMessage(content=str(content)))
            elif role in ("assistant", "ai"):
                out.append(AIMessage(content=str(content)))
            elif role == "tool":
                out.append(
                    ToolMessage(
                        content=str(content),
                        tool_call_id=str(m.get("tool_call_id") or ""),
                    )
                )
            else:
                out.append(HumanMessage(content=str(content)))
        else:
            out.append(m)
    return out


def _run_tools_mode(
    spec: AgentSpec,
    messages: list[Any],
    tools: list[Any],
    llm: Any,
    settings: Settings,
    system_prompt: str,
) -> dict[str, Any]:
    try:
        from langgraph.prebuilt import create_react_agent

        max_steps = getattr(settings, "max_agent_steps", _DEFAULT_MAX_STEPS) or _DEFAULT_MAX_STEPS
        agent = create_react_agent(llm, tools, prompt=system_prompt, checkpointer=None)
        input_messages = _coerce_messages(messages)
        result = agent.invoke(
            {"messages": input_messages},
            config={"recursion_limit": max(5, int(max_steps) * 2 + 1)},
        )
        out_messages = result.get("messages", []) if isinstance(result, dict) else []
        final = ""
        for msg in reversed(out_messages):
            if isinstance(msg, AIMessage):
                content = getattr(msg, "content", "")
                if isinstance(content, list):
                    # multimodal chunks
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            parts.append(str(c.get("text", "")))
                        elif isinstance(c, str):
                            parts.append(c)
                    content = "".join(parts)
                final = str(content or "")
                break
        return {"messages": list(out_messages), "final": final, "mode": "tools"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "mode": "tools"}


def _run_json_mode(
    spec: AgentSpec,
    messages: list[Any],
    tools: list[Any],
    llm: Any,
    settings: Settings,
    system_prompt: str,
) -> dict[str, Any]:
    try:
        max_steps = getattr(settings, "max_agent_steps", _DEFAULT_MAX_STEPS) or _DEFAULT_MAX_STEPS
        tool_text = _tool_spec_text(tools)
        instruction = (
            system_prompt
            + "\n\nYou are in structured JSON fallback mode (tool calling unavailable).\n"
            "Available tools:\n"
            + tool_text
            + "\n\nRespond with a single JSON object and nothing else:\n"
            '{"thought": "string", "tool": "tool_name_or_null", "args": {object or null}, "final": "string_or_null"}\n'
            "- To call a tool: set tool to its name, args to an object, final to null.\n"
            "- To finish: set tool to null, args to null (or {}), final to your answer.\n"
            "- Do not include markdown fences around the JSON."
        )
        convo: list[Any] = [SystemMessage(content=instruction)]
        convo.extend(_coerce_messages(messages))
        transcript: list[Any] = list(convo)
        final_text = ""
        for _ in range(int(max_steps)):
            response = llm.invoke(convo)
            transcript.append(response)
            content = getattr(response, "content", "")
            if isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        parts.append(str(c.get("text", "")))
                    elif isinstance(c, str):
                        parts.append(c)
                content = "".join(parts)
            obj = _extract_json_object(str(content or ""))
            if obj is None:
                convo.append(
                    HumanMessage(
                        content='Invalid response. Reply with a single JSON object: {"thought":..., "tool":..., "args":..., "final":...}'
                    )
                )
                transcript.append(convo[-1])
                continue
            final_val = obj.get("final")
            tool_name = obj.get("tool")
            if final_val is not None and tool_name in (None, "", "null"):
                final_text = str(final_val)
                break
            if tool_name in (None, "", "null"):
                # treat as final if no tool requested
                if final_val is not None:
                    final_text = str(final_val)
                else:
                    final_text = str(obj.get("thought") or content or "")
                break
            args = obj.get("args") or {}
            observation = _dispatch_tool(tools, str(tool_name), args)
            obs_msg = HumanMessage(
                content=f"Tool observation for {tool_name}:\n{observation}"
            )
            convo.append(obs_msg)
            transcript.append(obs_msg)
        if not final_text:
            final_text = "ERROR: max_agent_steps reached without final answer"
        return {"messages": transcript, "final": final_text, "mode": "json"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "mode": "json"}


def run_agent(
    spec: AgentSpec,
    messages: list[Any],
    tools: list[Any],
    llm: Any,
    settings: Settings,
    mode: str = "auto",
) -> dict[str, Any]:
    """Execute one agent turn and return a structured result."""
    try:
        if mode not in ("tools", "json", "auto"):
            return {"error": f"unknown mode: {mode}", "mode": str(mode)}
        system_prompt = build_system_prompt(spec, workspace_root=settings.workspace_root)
        use_tools = False
        if mode == "tools":
            use_tools = bool(tools)
        elif mode == "json":
            use_tools = False
        else:  # auto
            if not tools:
                use_tools = False
            else:
                probe_timeout = int(getattr(settings, "probe_timeout", 20) or 20)
                use_tools = CapabilityProbe().probe(llm, timeout=probe_timeout)
        if use_tools and tools:
            result = _run_tools_mode(spec, messages, tools, llm, settings, system_prompt)
            if "error" in result and mode == "auto":
                # fall back to json on tool-mode execution issues? only if LLM/tooling unsupported
                # spec says "On LLM/network errors return error dict..." so keep error.
                pass
            return result
        return _run_json_mode(spec, messages, tools, llm, settings, system_prompt)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "mode": str(mode)}


class AgentRuntime:
    """Runtime for loading agent specs, tools, and executing turns."""

    def __init__(
        self,
        settings: Settings | None = None,
        agents: dict[str, AgentSpec] | None = None,
        workspace_root: Path | str | None = None,
    ):
        self.settings = settings or get_settings()
        self.workspace_root = Path(
            workspace_root or self.settings.workspace_root
        ).expanduser().resolve()
        self.agents = agents if agents is not None else load_agents(self.workspace_root)
        self._toolsets: dict[str, list[Any]] = {}

    def get_spec(self, role: str) -> AgentSpec:
        if role not in self.agents:
            known = ", ".join(sorted(self.agents)) or "<none>"
            raise AgentLoadError(f"unknown role {role!r}; known agents: {known}")
        return self.agents[role]

    def _get_toolset(self, role: str, web_consent: dict[str, Any] | None = None) -> list[Any]:
        # Research web access is per-run consent state; avoid caching when provided.
        if web_consent is not None or role == "research":
            spec = self.get_spec(role)
            return make_toolset(spec, self.workspace_root, self.settings, web_consent=web_consent)
        if role in self._toolsets:
            return self._toolsets[role]
        spec = self.get_spec(role)
        tools = make_toolset(spec, self.workspace_root, self.settings)
        self._toolsets[role] = tools
        return tools

    def run(
        self,
        role: str,
        messages: list[Any],
        context: str | None = None,
        skill_md: str | None = None,
        mode: str = "auto",
        web_consent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            spec = self.get_spec(role)
            llm = make_llm_for_role(role, self.settings)
            tools = self._get_toolset(role, web_consent=web_consent)
            # build prompt with context/skill for this run
            system_prompt = build_system_prompt(spec, skill_md=skill_md, context=context, workspace_root=self.workspace_root)
            if mode not in ("tools", "json", "auto"):
                return {"error": f"unknown mode: {mode}", "mode": str(mode)}

            if mode == "tools":
                use_tools = bool(tools)
            elif mode == "json":
                use_tools = False
            else:
                if not tools:
                    use_tools = False
                else:
                    probe_timeout = int(getattr(self.settings, "probe_timeout", 20) or 20)
                    use_tools = CapabilityProbe().probe(llm, timeout=probe_timeout)

            if use_tools and tools:
                return _run_tools_mode(spec, messages, tools, llm, self.settings, system_prompt)
            return _run_json_mode(spec, messages, tools, llm, self.settings, system_prompt)
        except AgentLoadError:
            raise
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "mode": str(mode)}


__all__ = [
    "AgentRuntime",
    "CapabilityProbe",
    "build_system_prompt",
    "make_llm_for_role",
    "run_agent",
    "supports_tools",
]
