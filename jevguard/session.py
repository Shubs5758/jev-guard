"""Per-session state: cumulative risk, tool budgets, and loop detection.

Single messages can look innocent while a conversation escalates step by step (the
"crescendo" pattern), and agents can get stuck calling the same tool forever. Both are only
visible across calls, so the guard keeps a small amount of state per session.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from jevguard.config import SessionPolicy


@dataclass
class SessionState:
    session_id: str
    risk: float = 0.0
    peak_risk: float = 0.0
    tool_calls: int = 0
    events: int = 0
    blocked: int = 0
    call_counts: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


def call_fingerprint(tool_name: str | None, args: Any) -> str:
    blob = json.dumps({"t": tool_name, "a": args}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


class SessionTracker:
    def __init__(self, policy: SessionPolicy, max_sessions: int = 10_000):
        self.policy = policy
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[str, SessionState] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, session_id: str) -> SessionState:
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                state = SessionState(session_id)
                self._sessions[session_id] = state
                if len(self._sessions) > self.max_sessions:
                    self._sessions.popitem(last=False)
            else:
                self._sessions.move_to_end(session_id)
            return state

    def tool_call_problems(self, session_id: str, tool_name: str | None, args: Any) -> list[tuple[str, str]]:
        """Register a tool call; return ``(check, detail)`` pairs for budget or loop violations."""
        state = self.get(session_id)
        problems: list[tuple[str, str]] = []
        with self._lock:
            state.tool_calls += 1
            fp = call_fingerprint(tool_name, args)
            state.call_counts[fp] = state.call_counts.get(fp, 0) + 1
            if state.tool_calls > self.policy.max_tool_calls:
                problems.append(("tool_budget", f"{state.tool_calls} tool calls > limit {self.policy.max_tool_calls}"))
            if state.call_counts[fp] > self.policy.max_identical_tool_calls:
                problems.append(("tool_loop", f"identical {tool_name} call repeated {state.call_counts[fp]}x"))
        return problems

    def record(self, session_id: str, risk: float, blocked: bool) -> str:
        """Fold a decision's risk into the session. Returns "ok", "escalate" or "block"."""
        state = self.get(session_id)
        with self._lock:
            state.risk = state.risk * self.policy.risk_decay + (risk if risk >= 0.3 else 0.0)
            state.peak_risk = max(state.peak_risk, state.risk)
            state.events += 1
            state.blocked += int(blocked)
            state.last_seen = time.time()
            if state.risk >= self.policy.block_at:
                return "block"
            if state.risk >= self.policy.escalate_at:
                return "escalate"
            return "ok"

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
