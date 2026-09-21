"""LangGraph integrations.

**Already have a compiled graph?** Wrap it - no rewiring, same ``invoke``/``stream`` API:

    from jevguard.adapters.langgraph import guard_graph

    graph = builder.compile()
    graph = guard_graph(graph, guard)      # guards input, output and tool calls inside
    graph.invoke({"messages": [("user", "...")]}, {"configurable": {"thread_id": "s-1"}})

**Building the graph yourself?** Use the nodes, which give you control over routing:

    builder.add_node("guard_in", input_guard_node(guard, next_node="agent"))
    builder.add_node("agent", call_model)
    builder.add_node("tools", guarded_tool_node(tools, guard))
    builder.add_node("guard_out", output_guard_node(guard, next_node=END))

Guard nodes route with ``Command(goto=...)``, so give them no static outgoing edge.
``create_agent`` graphs should use :class:`JevGuardMiddleware` instead.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Iterator, Sequence

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, convert_to_messages
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.graph import END
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from jevguard.adapters.langchain import (
    DEFAULT_BLOCK_MESSAGE, JevGuardMiddleware, current_session_id, last_human, message_text, recent_tool_output,
)
from jevguard.engine import Guard
from jevguard.types import Action, Decision, GuardContext, Stage

log = logging.getLogger("jevguard")


def guarded_tool_node(tools: Sequence[Any], guard: Guard, *, check_results: bool = True, **tool_node_kwargs: Any) -> ToolNode:
    """A ``ToolNode`` that checks every call before it runs and every result before it returns."""
    mw = JevGuardMiddleware(guard, check_tool_results=check_results, framework="langgraph")
    return ToolNode(tools, wrap_tool_call=mw.wrap_tool_call, awrap_tool_call=mw.awrap_tool_call, **tool_node_kwargs)


def _ctx(**kw: Any) -> GuardContext:
    return GuardContext(session_id=current_session_id(), framework="langgraph", **kw)


def input_guard_node(guard: Guard, *, next_node: str, on_block: str = END, block_message: str = DEFAULT_BLOCK_MESSAGE):
    """Checks the latest user message. Blocked turns get a refusal and jump to ``on_block``."""

    async def jevguard_input(state: dict[str, Any]) -> Command:
        _, user = last_human(state["messages"])
        if user is None:
            return Command(goto=next_node)
        d = await guard.acheck(Stage.INPUT, message_text(user), _ctx())
        if d.blocked:
            return Command(goto=on_block, update={"messages": [AIMessage(content=block_message,
                                                                         response_metadata={"jevguard": d.to_dict()})]})
        if d.action == Action.REDACT and d.redacted_text is not None and d.enforced:
            # add_messages replaces a message that has the same id
            return Command(goto=next_node, update={"messages": [HumanMessage(content=d.redacted_text, id=user.id)]})
        return Command(goto=next_node)

    return jevguard_input


def output_guard_node(guard: Guard, *, next_node: str = END, block_message: str = DEFAULT_BLOCK_MESSAGE):
    """Checks the latest AI answer and replaces it in place when it is blocked or redacted."""

    async def jevguard_output(state: dict[str, Any]) -> Command:
        messages = state["messages"]
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or not message_text(last).strip():
            return Command(goto=next_node)
        _, user = last_human(messages)
        d = await guard.acheck(Stage.OUTPUT, message_text(last), _ctx(
            user_goal=message_text(user) if user else None, grounding=recent_tool_output(messages)))
        if d.blocked:
            return Command(goto=next_node, update={"messages": [AIMessage(content=block_message, id=last.id,
                                                                          response_metadata={"jevguard": d.to_dict()})]})
        if d.action == Action.REDACT and d.redacted_text is not None and d.enforced:
            return Command(goto=next_node, update={"messages": [last.model_copy(update={"content": d.redacted_text})]})
        return Command(goto=next_node)

    return jevguard_output


# --- guarding an already-compiled graph -------------------------------------------------------------

def _iter_tool_nodes(obj: Any, seen: set[int] | None = None) -> Iterator[ToolNode]:
    """Walk a compiled graph (its nodes, sequences and subgraphs) yielding every ToolNode."""
    seen = seen if seen is not None else set()
    if obj is None or id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, ToolNode):
        yield obj
        return
    for attr in ("bound", "node"):
        child = getattr(obj, attr, None)
        if child is not None and not isinstance(child, (str, bytes)):
            yield from _iter_tool_nodes(child, seen)
    for attr in ("nodes", "steps"):
        children = getattr(obj, attr, None)
        if isinstance(children, dict):
            children = list(children.values())
        if isinstance(children, (list, tuple)):
            for child in children:
                yield from _iter_tool_nodes(child, seen)


def attach_tool_guard(graph: Any, guard: Guard, *, check_results: bool = True, agent: str | None = None) -> int:
    """Guard the tool calls of an already-compiled graph, in place.

    ``ToolNode`` reads its wrapper at call time, so this works on a graph that is already running.
    A wrapper the node already had is kept and runs inside ours. Returns how many nodes were patched.
    """
    mw = JevGuardMiddleware(guard, check_input=False, check_output=False, check_tool_results=check_results,
                            agent_name=agent, framework="langgraph")
    patched = 0
    for node in _iter_tool_nodes(graph):
        if getattr(node, "_jevguard_patched", False):
            continue
        prev_sync, prev_async = node._wrap_tool_call, node._awrap_tool_call
        if prev_sync is not None and prev_async is None:
            log.warning("jevguard: %r has a sync-only wrap_tool_call; it will not run on async paths", node)

        def sync_wrapper(request, handler, _prev=prev_sync):
            return mw.wrap_tool_call(request, (lambda req: _prev(req, handler)) if _prev else handler)

        async def async_wrapper(request, handler, _prev=prev_async):
            return await mw.awrap_tool_call(request, (lambda req: _prev(req, handler)) if _prev else handler)

        node._wrap_tool_call = sync_wrapper
        node._awrap_tool_call = async_wrapper
        node._jevguard_patched = True
        patched += 1
    return patched


class GuardedGraph(Runnable):
    """A compiled LangGraph graph with jevguard wrapped around it.

    Keeps the graph's own API (``invoke``, ``ainvoke``, ``stream``, ``astream``, ``get_state``, ...) and adds:

    - the latest user message is checked before the graph runs; a blocked turn returns a refusal and
      the graph is never invoked
    - the final AI answer is checked before it reaches the caller (redacted or replaced)
    - tool calls and tool results inside the graph are checked by :func:`attach_tool_guard`

    ``thread_id`` from the config becomes the jevguard session id. AI messages that carry tool calls
    are never rewritten (that would orphan their tool results); their tool calls are guarded at the
    tool node instead.
    """

    def __init__(self, graph: Any, guard: Guard, *, messages_key: str = "messages",
                 block_message: str = DEFAULT_BLOCK_MESSAGE, check_input: bool = True, check_output: bool = True,
                 guard_tools: bool = True, check_tool_results: bool = True, persist_edits: bool = True,
                 agent: str | None = None):
        self.graph = graph
        self.guard = guard
        self.messages_key = messages_key
        self.block_message = block_message
        self.check_input, self.check_output = check_input, check_output
        self.persist_edits = persist_edits
        self.agent = agent
        self.name = f"guarded_{getattr(graph, 'name', None) or 'graph'}"
        self.last_decisions: list[Decision] = []
        self.tool_nodes_guarded = attach_tool_guard(graph, guard, check_results=check_tool_results,
                                                    agent=agent) if guard_tools else 0

    def __getattr__(self, item: str) -> Any:  # delegate get_state, update_state, nodes, checkpointer, ...
        return getattr(self.__dict__["graph"], item)

    # ---- context helpers ---------------------------------------------------------------------------
    def _session(self, config: RunnableConfig | None) -> str | None:
        cfg = (config or {}).get("configurable") or {}
        return cfg.get("thread_id") or cfg.get("session_id") or current_session_id()

    def _ctx(self, config: RunnableConfig | None, **kw: Any) -> GuardContext:
        return GuardContext(session_id=self._session(config), agent=self.agent, framework="langgraph", **kw)

    def _note(self, d: Decision) -> Decision:
        self.last_decisions = (self.last_decisions + [d])[-50:]
        return d

    # ---- input --------------------------------------------------------------------------------------
    def _input_messages(self, value: Any) -> list[AnyMessage] | None:
        raw = value.get(self.messages_key) if isinstance(value, dict) else value
        if not isinstance(raw, (list, tuple)) or not raw:
            return None
        try:
            return list(convert_to_messages(raw))
        except Exception:
            return [m for m in raw if isinstance(m, BaseMessage)] or None

    def _replace_messages(self, value: Any, messages: list[AnyMessage]) -> Any:
        return {**value, self.messages_key: messages} if isinstance(value, dict) else messages

    def _refusal(self, value: Any, messages: list[AnyMessage], d: Decision) -> Any:
        ai = AIMessage(content=self.block_message, response_metadata={"jevguard": d.to_dict()})
        return self._replace_messages(value, [*messages, ai])

    def _pre(self, value: Any):
        if not self.check_input:
            return None, None, None
        messages = self._input_messages(value)
        if not messages:
            return None, None, None
        idx, user = last_human(messages)
        return messages, idx, user

    def _apply_input(self, value: Any, messages: list[AnyMessage], idx: int, user: HumanMessage,
                     d: Decision) -> tuple[Any, Any | None]:
        if d.blocked:
            return value, self._refusal(value, messages, d)
        if d.action == Action.REDACT and d.redacted_text is not None and d.enforced:
            patched = list(messages)
            patched[idx] = HumanMessage(content=d.redacted_text, id=user.id, name=user.name)
            return self._replace_messages(value, patched), None
        return value, None

    def _guard_input(self, value: Any, config: RunnableConfig | None) -> tuple[Any, Any | None]:
        messages, idx, user = self._pre(value)
        if user is None:
            return value, None
        d = self._note(self.guard.check(Stage.INPUT, message_text(user), self._ctx(config)))
        return self._apply_input(value, messages, idx, user, d)

    async def _aguard_input(self, value: Any, config: RunnableConfig | None) -> tuple[Any, Any | None]:
        messages, idx, user = self._pre(value)
        if user is None:
            return value, None
        d = self._note(await self.guard.acheck(Stage.INPUT, message_text(user), self._ctx(config)))
        return self._apply_input(value, messages, idx, user, d)

    # ---- output -------------------------------------------------------------------------------------
    def _output_target(self, result: Any):
        """Find the final answer in a result or stream chunk (state dict, bare list, or {node: update})."""
        containers: list[Any] = []
        if isinstance(result, dict):
            containers.append(result)
            containers += [v for v in result.values() if isinstance(v, dict)]
        elif isinstance(result, list):
            containers.append({self.messages_key: result})
        for container in containers:
            messages = container.get(self.messages_key)
            if not isinstance(messages, list):
                continue
            for i in range(len(messages) - 1, -1, -1):
                m = messages[i]
                # only the final answer: messages with tool calls are guarded at the tool node
                if isinstance(m, AIMessage) and not m.tool_calls and message_text(m).strip():
                    return container, messages, i, m
        return None

    def _out_ctx(self, messages: list[AnyMessage], config: RunnableConfig | None) -> GuardContext:
        _, user = last_human(messages)
        return self._ctx(config, user_goal=message_text(user) if user else None,
                         grounding=recent_tool_output(messages))

    def _apply_output(self, target, d: Decision, config: RunnableConfig | None) -> None:
        container, messages, i, msg = target
        if d.blocked:
            replacement = AIMessage(content=self.block_message, id=msg.id, response_metadata={"jevguard": d.to_dict()})
        elif d.action == Action.REDACT and d.redacted_text is not None and d.enforced:
            replacement = msg.model_copy(update={"content": d.redacted_text})
        else:
            return
        patched = list(messages)
        patched[i] = replacement
        container[self.messages_key] = patched
        if self.persist_edits and msg.id and config:
            # Keep the checkpoint in step with what the caller was given, so the next turn does not
            # read back the unguarded answer (add_messages replaces a message with the same id).
            try:
                self.graph.update_state(config, {self.messages_key: [replacement]})
            except Exception as exc:
                log.debug("jevguard: could not persist the guarded answer: %s", exc)

    def _guard_output(self, result: Any, config: RunnableConfig | None) -> Any:
        target = self._output_target(result) if self.check_output else None
        if target is None:
            return result
        d = self._note(self.guard.check(Stage.OUTPUT, message_text(target[3]), self._out_ctx(target[1], config)))
        self._apply_output(target, d, config)
        return result

    async def _aguard_output(self, result: Any, config: RunnableConfig | None) -> Any:
        target = self._output_target(result) if self.check_output else None
        if target is None:
            return result
        d = self._note(await self.guard.acheck(Stage.OUTPUT, message_text(target[3]), self._out_ctx(target[1], config)))
        self._apply_output(target, d, config)
        return result

    # ---- Runnable API ---------------------------------------------------------------------------------
    def invoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        value, refusal = self._guard_input(input, config)
        if refusal is not None:
            return refusal
        return self._guard_output(self.graph.invoke(value, config, **kwargs), config)

    async def ainvoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        value, refusal = await self._aguard_input(input, config)
        if refusal is not None:
            return refusal
        return await self._aguard_output(await self.graph.ainvoke(value, config, **kwargs), config)

    def stream(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Iterator[Any]:
        value, refusal = self._guard_input(input, config)
        if refusal is not None:
            yield refusal
            return
        # Guardable chunks are held back one step so the last one is checked before it is yielded.
        pending = None
        for chunk in self.graph.stream(value, config, **kwargs):
            if self._output_target(chunk) is None:
                if pending is not None:
                    yield pending
                    pending = None
                yield chunk
            else:
                if pending is not None:
                    yield pending
                pending = chunk
        if pending is not None:
            yield self._guard_output(pending, config)

    async def astream(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> AsyncIterator[Any]:
        value, refusal = await self._aguard_input(input, config)
        if refusal is not None:
            yield refusal
            return
        pending = None
        async for chunk in self.graph.astream(value, config, **kwargs):
            if self._output_target(chunk) is None:
                if pending is not None:
                    yield pending
                    pending = None
                yield chunk
            else:
                if pending is not None:
                    yield pending
                pending = chunk
        if pending is not None:
            yield await self._aguard_output(pending, config)


def guard_graph(graph: Any, guard: Guard, **kwargs: Any) -> GuardedGraph:
    """Wrap a compiled graph so ``invoke``/``stream`` are guarded and its tool calls are checked.

        graph = guard_graph(builder.compile(checkpointer=saver), guard)
        graph.invoke({"messages": [("user", "...")]}, {"configurable": {"thread_id": "s-1"}})

    Use this *or* :class:`JevGuardMiddleware` on a ``create_agent`` graph, not both.
    """
    return GuardedGraph(graph, guard, **kwargs)
