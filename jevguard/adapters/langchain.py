"""LangChain v1 agent middleware.

    from langchain.agents import create_agent
    from jevguard import Guard
    from jevguard.adapters.langchain import JevGuardMiddleware

    agent = create_agent(model, tools, middleware=[JevGuardMiddleware(Guard())])

What gets checked:

- ``wrap_model_call``: the user's message before the model sees it (INPUT), and the model's
  answer before the user sees it (OUTPUT, with recent tool output as grounding)
- ``wrap_tool_call``: every tool call before it executes (TOOL_CALL) and every tool result
  before it re-enters the context (TOOL_RESULT - indirect prompt injection)

Both sync (``invoke``) and async (``ainvoke``/``astream``) agents are supported.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage

from jevguard.engine import Guard, run_sync
from jevguard.jev.questions import Choice
from jevguard.types import Action, Decision, GuardBlocked, GuardContext, Stage

DEFAULT_BLOCK_MESSAGE = "I can't help with that request."


# --- message helpers (shared with the LangGraph adapter) --------------------------------------------
def message_text(msg: BaseMessage | Any) -> str:
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content or "")


def last_human(messages: list[AnyMessage]) -> tuple[int, HumanMessage] | tuple[None, None]:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return i, messages[i]
    return None, None


def recent_tool_output(messages: list[AnyMessage], limit: int = 12_000) -> str | None:
    """Tool results since the last user turn: what the model's answer should be grounded in."""
    idx, _ = last_human(messages)
    chunks = [message_text(m) for m in messages[(idx or 0):] if isinstance(m, ToolMessage)]
    text = "\n---\n".join(c for c in chunks if c)
    return text[-limit:] if text else None


def current_session_id() -> str | None:
    try:
        from langgraph.config import get_config

        cfg = get_config().get("configurable", {})
        return cfg.get("thread_id") or cfg.get("session_id")
    except Exception:
        return None


def _state_messages(state: Any) -> list[AnyMessage]:
    if isinstance(state, dict):
        return list(state.get("messages", []))
    return list(getattr(state, "messages", []) or [])


# --- middleware --------------------------------------------------------------------------------------
class JevGuardMiddleware(AgentMiddleware):
    """Guards every model call and tool call in a LangChain ``create_agent`` agent."""

    def __init__(self, guard: Guard | None = None, *, check_input: bool = True, check_output: bool = True,
                 check_tool_calls: bool = True, check_tool_results: bool = True, raise_on_block: bool = False,
                 block_message: str = DEFAULT_BLOCK_MESSAGE, agent_name: str | None = None,
                 framework: str = "langchain"):
        super().__init__()
        self.guard = guard or Guard()
        self.check_input, self.check_output = check_input, check_output
        self.check_tool_calls, self.check_tool_results = check_tool_calls, check_tool_results
        self.raise_on_block = raise_on_block
        self.block_message = block_message
        self.agent_name = agent_name
        self.framework = framework
        self.last_decisions: list[Decision] = []

    @property
    def name(self) -> str:  # middleware names must be unique per agent
        return "JevGuardMiddleware"

    def _ctx(self, **kw: Any) -> GuardContext:
        return GuardContext(session_id=current_session_id(), agent=self.agent_name, framework=self.framework, **kw)

    def _note(self, d: Decision) -> Decision:
        self.last_decisions = (self.last_decisions + [d])[-50:]
        if d.blocked and self.raise_on_block:
            raise GuardBlocked(d)
        return d

    # ---- model call: input + output --------------------------------------------------------------
    def _input_plan(self, request: ModelRequest) -> tuple[int, HumanMessage] | None:
        if not self.check_input or not request.messages or not isinstance(request.messages[-1], HumanMessage):
            return None  # only fresh user turns; tool results are checked in wrap_tool_call
        return len(request.messages) - 1, request.messages[-1]

    def _apply_input(self, request: ModelRequest, idx: int, msg: HumanMessage, d: Decision) -> ModelRequest | ModelResponse:
        if d.blocked:
            return ModelResponse(result=[AIMessage(content=self.block_message, response_metadata={"jevguard": d.to_dict()})])
        if d.action == Action.REDACT and d.redacted_text is not None and d.enforced:
            messages = list(request.messages)
            messages[idx] = HumanMessage(content=d.redacted_text, id=msg.id, name=msg.name)
            return request.override(messages=messages)
        return request

    def _unwrap(self, response: Any) -> tuple[ModelResponse, Callable[[ModelResponse], Any]]:
        if isinstance(response, AIMessage):
            return ModelResponse(result=[response]), lambda r: r
        if hasattr(response, "model_response"):  # ExtendedModelResponse
            def rewrap(r: ModelResponse, _orig: Any = response) -> Any:
                _orig.model_response = r
                return _orig
            return response.model_response, rewrap
        return response, lambda r: r

    def _outputs(self, response: ModelResponse) -> list[tuple[int, AIMessage, str]]:
        if not self.check_output:
            return []
        return [(i, m, message_text(m)) for i, m in enumerate(response.result)
                if isinstance(m, AIMessage) and message_text(m).strip()]

    def _apply_output(self, response: ModelResponse, i: int, msg: AIMessage, d: Decision) -> None:
        if d.blocked:
            response.result[i] = AIMessage(content=self.block_message, id=msg.id,
                                           response_metadata={"jevguard": d.to_dict()})
        elif d.action == Action.REDACT and d.redacted_text is not None and d.enforced:
            response.result[i] = msg.model_copy(update={"content": d.redacted_text})

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> Any:
        plan = self._input_plan(request)
        if plan:
            idx, msg = plan
            d = self._note(self.guard.check(Stage.INPUT, message_text(msg), self._ctx()))
            request_or_response = self._apply_input(request, idx, msg, d)
            if isinstance(request_or_response, ModelResponse):
                return request_or_response
            request = request_or_response
        response, rewrap = self._unwrap(handler(request))
        _, user = last_human(request.messages)
        for i, m, text in self._outputs(response):
            ctx = self._ctx(user_goal=message_text(user) if user else None, grounding=recent_tool_output(request.messages))
            self._apply_output(response, i, m, self._note(self.guard.check(Stage.OUTPUT, text, ctx)))
        return rewrap(response)

    async def awrap_model_call(self, request: ModelRequest,
                               handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> Any:
        plan = self._input_plan(request)
        if plan:
            idx, msg = plan
            d = self._note(await self.guard.acheck(Stage.INPUT, message_text(msg), self._ctx()))
            request_or_response = self._apply_input(request, idx, msg, d)
            if isinstance(request_or_response, ModelResponse):
                return request_or_response
            request = request_or_response
        response, rewrap = self._unwrap(await handler(request))
        _, user = last_human(request.messages)
        for i, m, text in self._outputs(response):
            ctx = self._ctx(user_goal=message_text(user) if user else None, grounding=recent_tool_output(request.messages))
            self._apply_output(response, i, m, self._note(await self.guard.acheck(Stage.OUTPUT, text, ctx)))
        return rewrap(response)

    # ---- tool call: arguments + result ---------------------------------------------------------------
    def _call_ctx(self, request: Any) -> GuardContext:
        tc = request.tool_call
        _, user = last_human(_state_messages(request.state))
        return self._ctx(tool_name=tc.get("name"), tool_args=dict(tc.get("args") or {}),
                         user_goal=message_text(user) if user else None)

    @staticmethod
    def _blocked_tool_message(request: Any, d: Decision) -> ToolMessage:
        tc = request.tool_call
        return ToolMessage(content=f"Tool call blocked by security policy: {d.reason}. Do not retry it.",
                           tool_call_id=tc.get("id") or "", name=tc.get("name"), status="error",
                           response_metadata={"jevguard": d.to_dict()})

    @staticmethod
    def _apply_result(result: Any, d: Decision) -> Any:
        if d.blocked:
            return result.model_copy(update={"content": f"[tool output withheld by security policy: {d.reason}]"})
        if d.action == Action.REDACT and d.redacted_text is not None and d.enforced:
            return result.model_copy(update={"content": d.redacted_text})
        return result

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        ctx = self._call_ctx(request)
        if self.check_tool_calls:
            d = self._note(self.guard.check(Stage.TOOL_CALL, "", ctx))
            if d.blocked:
                return self._blocked_tool_message(request, d)
        result = handler(request)
        if self.check_tool_results and isinstance(result, ToolMessage):
            rctx = self._ctx(tool_name=ctx.tool_name, user_goal=ctx.user_goal)
            result = self._apply_result(result, self._note(self.guard.check(Stage.TOOL_RESULT, message_text(result), rctx)))
        return result

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        ctx = self._call_ctx(request)
        if self.check_tool_calls:
            d = self._note(await self.guard.acheck(Stage.TOOL_CALL, "", ctx))
            if d.blocked:
                return self._blocked_tool_message(request, d)
        result = await handler(request)
        if self.check_tool_results and isinstance(result, ToolMessage):
            rctx = self._ctx(tool_name=ctx.tool_name, user_goal=ctx.user_goal)
            result = self._apply_result(result, self._note(await self.guard.acheck(Stage.TOOL_RESULT, message_text(result), rctx)))
        return result


class JevModelRouterMiddleware(AgentMiddleware):
    """Lets Jev pick which model handles each turn, e.g. a cheap model for small talk and a
    frontier model for hard reasoning. Falls back to the agent's own model when Jev is unsure.

        JevModelRouterMiddleware(guard, routes={
            "simple": (cheap_model, "Greetings, short factual questions, formatting"),
            "complex": (strong_model, "Multi-step reasoning, coding, analysis"),
        })
    """

    def __init__(self, guard: Guard, routes: dict[str, tuple[Any, str]], *, min_confidence: float = 0.6):
        super().__init__()
        self.guard = guard
        self.routes = routes
        self.min_confidence = min_confidence
        self.last_route: str | None = None
        self._question = Choice("Which kind of model this request needs", {k: desc for k, (_, desc) in routes.items()})

    @property
    def name(self) -> str:
        return "JevModelRouterMiddleware"

    def _pick(self, answers: dict[str, Any]) -> Any | None:
        a = answers.get("route")
        if a is None or a.choice not in self.routes:
            return None
        conf = a.confidence if a.confidence is not None else a.probabilities.get(a.choice, 0.0)
        if conf < self.min_confidence:
            return None
        self.last_route = a.choice
        model = self.routes[a.choice][0]
        if isinstance(model, str):
            from langchain.chat_models import init_chat_model

            model = init_chat_model(model)
        return model

    def _state(self, request: ModelRequest) -> str | None:
        _, user = last_human(request.messages)
        return f"USER MESSAGE:\n{message_text(user)}" if user else None

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        state = self._state(request)
        if state:
            try:
                model = self._pick(run_sync(self.guard.aclassify(state, {"route": self._question})))
                if model is not None:
                    request = request.override(model=model)
            except Exception:
                pass  # routing is an optimisation; never fail the turn because of it
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]) -> Any:
        state = self._state(request)
        if state:
            try:
                model = self._pick(await self.guard.aclassify(state, {"route": self._question}))
                if model is not None:
                    request = request.override(model=model)
            except Exception:
                pass
        return await handler(request)
