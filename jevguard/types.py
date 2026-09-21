"""Core value types shared by the engine, adapters, sinks and dashboard."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Stage(str, Enum):
    """Where in the agent loop a check runs."""

    INPUT = "input"              # user prompt before it reaches the model
    TOOL_CALL = "tool_call"      # a tool call the model wants to execute
    TOOL_RESULT = "tool_result"  # data a tool returned (indirect prompt injection lives here)
    RETRIEVAL = "retrieval"      # RAG documents before they enter the context
    OUTPUT = "output"            # the model's answer before it reaches the user


class Action(str, Enum):
    """What the guard decided. Ordered from least to most severe."""

    ALLOW = "allow"
    FLAG = "flag"          # let it through, but record it for review
    REDACT = "redact"      # let a sanitised version through
    ESCALATE = "escalate"  # needs a human (or a stronger model) to approve
    BLOCK = "block"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]

    @classmethod
    def worst(cls, *actions: "Action") -> "Action":
        return max(actions, key=lambda a: a.severity) if actions else cls.ALLOW


# Findings that explain an escalation better than any score does; always listed first in `reason`.
PINNED_CHECKS = frozenset({"approval_required", "low_confidence", "human_review"})

_SEVERITY = {Action.ALLOW: 0, Action.FLAG: 1, Action.REDACT: 2, Action.ESCALATE: 3, Action.BLOCK: 4}


@dataclass
class Finding:
    """One piece of evidence behind a decision (a Jev answer, a regex hit, a policy rule)."""

    check: str
    score: float                     # 0..1, probability-like risk
    source: str                      # "jev" | "heuristics" | "policy" | "session"
    detail: str = ""
    confidence: float | None = None  # Jev's calibrated confidence, when available
    hard: bool = False               # a hard finding blocks regardless of thresholds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GuardContext:
    """Everything the engine knows about the call being checked."""

    session_id: str | None = None
    agent: str | None = None
    framework: str | None = None
    user_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    user_goal: str | None = None       # latest user request, used to detect goal hijacking
    grounding: str | None = None       # retrieved context, used for groundedness checks
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """The result of one guard check."""

    stage: Stage
    action: Action
    risk: float
    findings: list[Finding] = field(default_factory=list)
    source: str = "heuristics"         # jev | heuristics | heuristics_degraded | policy | cache
    enforced: bool = True              # False in shadow mode: the action is only recorded
    redacted_text: str | None = None
    latency_ms: float = 0.0
    jev_called: bool = False
    jev_cost_usd: float = 0.0
    approval_id: str | None = None
    approved: bool | None = None       # outcome of a human approval for ESCALATE decisions
    evals: dict[str, float] = field(default_factory=dict)  # optional quality scores on outputs
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    ts: float = field(default_factory=time.time)

    @property
    def blocked(self) -> bool:
        """True when the caller must stop the content (enforced block or rejected escalation)."""
        if not self.enforced:
            return False
        return self.action == Action.BLOCK or (self.action == Action.ESCALATE and self.approved is not True)

    @property
    def allowed(self) -> bool:
        return not self.blocked

    @property
    def top_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: f.score, reverse=True)

    @property
    def reason(self) -> str:
        """A short human-readable summary. Jev returns numbers, not prose, so we compose one."""
        if self.action == Action.ALLOW:
            return "No risk above thresholds"
        pinned = [f for f in self.findings if f.check in PINNED_CHECKS]
        top = [f for f in self.top_findings if f.score >= 0.3 and f not in pinned][:3] or             [f for f in self.top_findings if f not in pinned][:1]
        if pinned:
            return "; ".join([f.detail for f in pinned] + [f"{f.check} ({f.score:.0%})" for f in top if f.score >= 0.3])
        parts = [f"{f.check} ({f.score:.0%}{', ' + f.detail if f.detail else ''})" for f in top]
        return "; ".join(parts) if parts else self.action.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "ts": self.ts,
            "stage": self.stage.value,
            "action": self.action.value,
            "risk": round(self.risk, 4),
            "source": self.source,
            "enforced": self.enforced,
            "blocked": self.blocked,
            "reason": self.reason,
            "findings": [f.to_dict() for f in self.top_findings],
            "redacted_text": self.redacted_text,
            "latency_ms": round(self.latency_ms, 2),
            "jev_called": self.jev_called,
            "jev_cost_usd": self.jev_cost_usd,
            "approval_id": self.approval_id,
            "approved": self.approved,
            "evals": self.evals,
        }


class GuardBlocked(Exception):
    """Raised by adapters configured with ``raise_on_block=True``."""

    def __init__(self, decision: Decision):
        self.decision = decision
        super().__init__(f"[jevguard] {decision.stage.value} blocked: {decision.reason}")
