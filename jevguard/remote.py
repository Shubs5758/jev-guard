"""Use a central jevguard server as the guard (one policy, one dashboard, many agents/languages).

    guard = RemoteGuard("http://guard.internal:7860")
    d = await guard.acheck_input("...")

``RemoteGuard`` has the same ``acheck*`` / ``check*`` surface as :class:`Guard`, so every
adapter accepts it. If the server is unreachable it falls back to a local heuristics-only guard
(fail-closed on anything suspicious) instead of letting traffic through unchecked.
"""

from __future__ import annotations

from typing import Any

import httpx

from jevguard.engine import Guard, run_sync
from jevguard.types import Action, Decision, Finding, GuardContext, Stage


class RemoteGuard(Guard):
    def __init__(self, url: str = "http://127.0.0.1:7860", *, api_key: str | None = None, timeout_s: float = 5.0,
                 approval_wait_s: float = 150.0,
                 agent: str | None = None, framework: str | None = None):
        super().__init__(backend=None, agent=agent, framework=framework)  # local fallback: heuristics only
        self.base = url.rstrip("/")
        # Escalations make the server wait for a human, so reads may legitimately take minutes.
        self.timeout = httpx.Timeout(timeout_s, read=approval_wait_s)
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def acheck(self, stage: Stage, text: str, ctx: GuardContext | None = None) -> Decision:
        ctx = ctx or GuardContext()
        payload = {"stage": Stage(stage).value, "text": text, "context": {
            k: v for k, v in vars(ctx).items() if v not in (None, {}, [])}}
        payload["context"].setdefault("agent", self.agent)
        payload["context"].setdefault("framework", self.framework)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                resp = await client.post(f"{self.base}/api/guard", json=payload)
                resp.raise_for_status()
                return decision_from_dict(resp.json())
        except Exception:
            return await super().acheck(Stage(stage), text, ctx)

    def check(self, stage: Stage | str, text: str, ctx: GuardContext | None = None) -> Decision:
        return run_sync(self.acheck(Stage(stage), text, ctx))


def decision_from_dict(d: dict[str, Any]) -> Decision:
    findings = [Finding(**{k: f.get(k) for k in ("check", "score", "source", "detail", "confidence", "hard")})
                for f in d.get("findings", [])]
    return Decision(stage=Stage(d["stage"]), action=Action(d["action"]), risk=d["risk"], findings=findings,
                    source=d.get("source", "remote"), enforced=d.get("enforced", True),
                    redacted_text=d.get("redacted_text"), latency_ms=d.get("latency_ms", 0.0),
                    jev_called=d.get("jev_called", False), jev_cost_usd=d.get("jev_cost_usd", 0.0),
                    approval_id=d.get("approval_id"), approved=d.get("approved"), evals=d.get("evals") or {},
                    event_id=d.get("event_id", ""), ts=d.get("ts", 0.0))
