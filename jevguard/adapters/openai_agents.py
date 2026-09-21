"""OpenAI Agents SDK guardrails (``pip install openai-agents``).

    from agents import Agent
    from jevguard.adapters.openai_agents import jev_input_guardrail, jev_output_guardrail

    agent = Agent(name="support", instructions="...",
                  input_guardrails=[jev_input_guardrail(guard)],
                  output_guardrails=[jev_output_guardrail(guard)],
                  tools=[function_tool(guard_tool(guard, lookup_order))])

A tripped guardrail makes the SDK raise ``InputGuardrailTripwireTriggered`` /
``OutputGuardrailTripwireTriggered``; the jevguard decision is in ``output_info``.
Tools are guarded with :func:`jevguard.adapters.generic.guard_tool` before wrapping them in
``function_tool``.
"""

from __future__ import annotations

from typing import Any

from jevguard.engine import Guard
from jevguard.types import GuardContext, Stage


def _require_sdk():
    try:
        import agents  # noqa: F401
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError("jevguard.adapters.openai_agents needs `pip install openai-agents`") from exc
    return agents


def _input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    parts = []
    for item in value or []:
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts += [c.get("text", "") for c in content if isinstance(c, dict)]
    return "\n".join(p for p in parts if p)


def jev_input_guardrail(guard: Guard, *, name: str = "jevguard_input"):
    agents = _require_sdk()

    @agents.input_guardrail(name=name)
    async def _guardrail(ctx: Any, agent: Any, value: Any) -> Any:
        d = await guard.acheck(Stage.INPUT, _input_text(value),
                               GuardContext(framework="openai-agents", agent=getattr(agent, "name", None)))
        return agents.GuardrailFunctionOutput(output_info=d.to_dict(), tripwire_triggered=d.blocked)

    return _guardrail


def jev_output_guardrail(guard: Guard, *, name: str = "jevguard_output"):
    agents = _require_sdk()

    @agents.output_guardrail(name=name)
    async def _guardrail(ctx: Any, agent: Any, output: Any) -> Any:
        text = output if isinstance(output, str) else str(output)
        d = await guard.acheck(Stage.OUTPUT, text,
                               GuardContext(framework="openai-agents", agent=getattr(agent, "name", None)))
        return agents.GuardrailFunctionOutput(output_info=d.to_dict(), tripwire_triggered=d.blocked)

    return _guardrail
