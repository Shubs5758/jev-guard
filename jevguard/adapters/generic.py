"""Framework-free decorators. Use these with CrewAI, AutoGen, LlamaIndex, smolagents, MCP servers,
or plain SDK code - anything where a tool is a Python function and a model call is a function.

    @guard_tool(guard)
    def run_sql(query: str) -> str: ...

    @guard_llm(guard)
    def ask(prompt: str) -> str:
        return client.chat.completions.create(...).choices[0].message.content
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, Literal, TypeVar

from jevguard.engine import Guard
from jevguard.types import Action, Decision, GuardBlocked, GuardContext, Stage

F = TypeVar("F", bound=Callable[..., Any])
OnBlock = Literal["raise", "message"]


def _bound_args(fn: Callable[..., Any], args: tuple, kwargs: dict) -> dict[str, Any]:
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}
    except TypeError:
        return {"args": list(args), **kwargs}


def _blocked(d: Decision, on_block: OnBlock, what: str) -> str:
    if on_block == "raise":
        raise GuardBlocked(d)
    return f"[{what} blocked by security policy: {d.reason}]"


def guard_tool(guard: Guard, fn: F | None = None, *, name: str | None = None, on_block: OnBlock = "message",
               check_result: bool = True, framework: str = "python", session_id: Callable[[], str | None] | None = None):
    """Wrap a tool function: check its arguments before it runs and its result after."""

    def decorate(func: F) -> F:
        tool_name = name or func.__name__

        def ctx(args: tuple, kwargs: dict, **extra: Any) -> GuardContext:
            return GuardContext(tool_name=tool_name, framework=framework,
                                session_id=session_id() if session_id else None, **extra)

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                d = await guard.acheck(Stage.TOOL_CALL, "", ctx(args, kwargs, tool_args=_bound_args(func, args, kwargs)))
                if d.blocked:
                    return _blocked(d, on_block, "tool call")
                result = await func(*args, **kwargs)
                if check_result and isinstance(result, str):
                    r = await guard.acheck(Stage.TOOL_RESULT, result, ctx(args, kwargs))
                    if r.blocked:
                        return _blocked(r, on_block, "tool output")
                    if r.action == Action.REDACT and r.redacted_text is not None and r.enforced:
                        return r.redacted_text
                return result
            return awrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            d = guard.check(Stage.TOOL_CALL, "", ctx(args, kwargs, tool_args=_bound_args(func, args, kwargs)))
            if d.blocked:
                return _blocked(d, on_block, "tool call")
            result = func(*args, **kwargs)
            if check_result and isinstance(result, str):
                r = guard.check(Stage.TOOL_RESULT, result, ctx(args, kwargs))
                if r.blocked:
                    return _blocked(r, on_block, "tool output")
                if r.action == Action.REDACT and r.redacted_text is not None and r.enforced:
                    return r.redacted_text
            return result
        return wrapper  # type: ignore[return-value]

    return decorate(fn) if fn is not None else decorate


def guard_llm(guard: Guard, fn: F | None = None, *, on_block: OnBlock = "message", framework: str = "python",
              block_message: str = "I can't help with that request."):
    """Wrap ``fn(prompt: str, ...) -> str``: check the prompt going in and the text coming out."""

    def decorate(func: F) -> F:
        def prompt_of(args: tuple, kwargs: dict) -> str:
            if args and isinstance(args[0], str):
                return args[0]
            return str(kwargs.get("prompt") or kwargs.get("input") or kwargs.get("text") or "")

        def swap_prompt(args: tuple, kwargs: dict, new: str) -> tuple[tuple, dict]:
            if args and isinstance(args[0], str):
                return (new, *args[1:]), kwargs
            for k in ("prompt", "input", "text"):
                if k in kwargs:
                    return args, {**kwargs, k: new}
            return args, kwargs

        def on_input(d: Decision) -> str | None:
            if d.blocked:
                return block_message if on_block == "message" else _blocked(d, on_block, "prompt")
            return None

        def on_output(d: Decision, out: str) -> str:
            if d.blocked:
                return block_message if on_block == "message" else _blocked(d, on_block, "response")
            if d.action == Action.REDACT and d.redacted_text is not None and d.enforced:
                return d.redacted_text
            return out

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                prompt = prompt_of(args, kwargs)
                d = await guard.acheck(Stage.INPUT, prompt, GuardContext(framework=framework))
                if (early := on_input(d)) is not None:
                    return early
                if d.action == Action.REDACT and d.redacted_text is not None and d.enforced:
                    args, kwargs = swap_prompt(args, kwargs, d.redacted_text)
                out = await func(*args, **kwargs)
                if not isinstance(out, str):
                    return out
                return on_output(await guard.acheck(Stage.OUTPUT, out, GuardContext(framework=framework, user_goal=prompt)), out)
            return awrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            prompt = prompt_of(args, kwargs)
            d = guard.check(Stage.INPUT, prompt, GuardContext(framework=framework))
            if (early := on_input(d)) is not None:
                return early
            if d.action == Action.REDACT and d.redacted_text is not None and d.enforced:
                args, kwargs = swap_prompt(args, kwargs, d.redacted_text)
            out = func(*args, **kwargs)
            if not isinstance(out, str):
                return out
            return on_output(guard.check(Stage.OUTPUT, out, GuardContext(framework=framework, user_goal=prompt)), out)
        return wrapper  # type: ignore[return-value]

    return decorate(fn) if fn is not None else decorate


def filter_documents(guard: Guard, docs: list[Any], *, text_of: Callable[[Any], str] | None = None) -> list[Any]:
    """RAG hygiene: drop retrieved documents that carry injected instructions; redact the rest if needed.
    Works with LangChain ``Document``, LlamaIndex nodes, or plain strings."""
    text_of = text_of or (lambda d: getattr(d, "page_content", None) or getattr(d, "text", None) or str(d))
    kept = []
    for doc in docs:
        d = guard.check(Stage.RETRIEVAL, text_of(doc))
        if d.blocked:
            continue
        if d.action == Action.REDACT and d.redacted_text is not None and isinstance(doc, str):
            doc = d.redacted_text
        elif d.action == Action.REDACT and d.redacted_text is not None and hasattr(doc, "page_content"):
            doc.page_content = d.redacted_text
        kept.append(doc)
    return kept
