"""Jev backends: the real TypeSafe API and an offline simulator.

``JevBackend.evaluate(state, questions)`` returns ``{name: JevAnswer}``. The engine never
looks at the wire format, so swapping the API for the simulator (or a future SDK) is a
one-line change.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Protocol

import httpx

from jevguard.heuristics import scan_local
from jevguard.jev.questions import Choice, JevAnswer, Noul, Question, Score
from jevguard.types import Stage


class JevError(Exception):
    """Any failure talking to Jev."""


class JevRateLimitError(JevError):
    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(f"Jev rate limited (retry after {retry_after}s)")


class JevProtocolError(JevError):
    """The endpoint answered, but not in the System One shape (wrong API, or a changed schema)."""


class JevAuthError(JevError):
    """401/403. Retrying will not help, so the engine stops calling until it is fixed."""

    def __init__(self, status: int, base_url: str, api_key_env: str, key_hint: str, detail: str = ""):
        self.status, self.base_url, self.api_key_env = status, base_url, api_key_env
        super().__init__(
            f"Jev rejected the credentials ({status}) at {base_url}\n"
            f"  key came from ${api_key_env} ({key_hint})\n"
            f"  If that key belongs to a gateway, point jevguard at the gateway too:\n"
            f"    JEVGUARD_JEV_BASE_URL=<your gateway's System One endpoint>\n"
            f"    JEVGUARD_JEV_API_KEY_ENV=<the variable holding the gateway key>\n"
            f"  Run `jevguard doctor` to see exactly what is being sent."
            + (f"\n  server said: {detail}" if detail else ""))


def key_hint(key: str | None) -> str:
    """A safe fingerprint of a key for logs: never the key itself."""
    if not key:
        return "not set"
    return f"{key[:3]}..{key[-2:]}, {len(key)} chars" if len(key) > 8 else f"{len(key)} chars"


class JevBackend(Protocol):
    name: str

    async def evaluate(self, state: str, questions: dict[str, Question]) -> dict[str, JevAnswer]: ...


def _f(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def parse_answer(question: Question, raw: Any) -> JevAnswer:
    """Normalise one answer across the field names TypeSafe and the gateways use.

    A missing value raises rather than defaulting to zero: a zero would read as "no risk" and
    silently wave everything through, which is the worst possible failure for a guardrail.
    """
    if not isinstance(raw, dict):
        raw = {"value": raw}
    confidence = _f(raw.get("confidence"))
    if isinstance(question, Noul):
        # "noul" is the field the TypeSafe API and Vercel's TypeSafe-compatible API return.
        p = _f(next((raw[k] for k in ("noul", "probability", "p", "true", "value", "answer") if k in raw), None))
        if p is None:
            raise JevProtocolError(f"no probability in Noul answer: {raw!r}")
        return JevAnswer("noul", probability=p, confidence=confidence if confidence is not None else p)
    if isinstance(question, Choice):
        probs = raw.get("probabilities") or raw.get("distribution") or raw.get("probs") or {}
        if isinstance(probs, list):  # [{"option": "a", "probability": 0.3}, ...]
            probs = {d.get("option") or d.get("choice"): d.get("probability") for d in probs}
        probs = {str(k): float(v) for k, v in probs.items() if _f(v) is not None}
        choice = raw.get("choice") or raw.get("selected") or raw.get("answer")
        if choice is None and probs:
            choice = max(probs, key=probs.get)
        if choice is None and not probs:
            raise JevProtocolError(f"no choice in Choice answer: {raw!r}")
        return JevAnswer("choice", choice=choice, probabilities=probs, confidence=confidence)
    score = _f(next((raw[k] for k in ("score", "value", "answer") if k in raw), None))
    if score is None:
        raise JevProtocolError(f"no score in Score answer: {raw!r}")
    return JevAnswer("score", score=score, confidence=confidence)


class TypeSafeJev:
    """HTTP client for ``POST /v1/systemone``."""

    name = "jev-api"

    def __init__(self, api_key: str | None = None, *, base_url: str = "https://api.typesafe.ai/v1/systemone",
                 model: str = "jev-latest", timeout_s: float = 3.0, api_key_env: str = "TYPESAFE_API_KEY",
                 headers: dict[str, str] | None = None, transport: Any = None):
        self.api_key_env = api_key_env
        self.api_key = api_key or os.environ.get(api_key_env)
        if not self.api_key:
            raise JevError(f"{api_key_env} is not set")
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s
        # Extra headers let you go through a gateway or proxy that wants its own auth or routing headers.
        self.headers = {"Authorization": f"Bearer {self.api_key}", **(headers or {})}
        self.transport = transport
        self.last_cost: float | None = None      # real cost of the last call, when the API reports one
        self.last_usage: dict[str, Any] | None = None
        # httpx.AsyncClient is bound to the loop it was first used on, so keep one per loop.
        self._clients: dict[int, httpx.AsyncClient] = {}

    def _client(self) -> httpx.AsyncClient:
        loop_id = id(asyncio.get_running_loop())
        client = self._clients.get(loop_id)
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout_s, headers=self.headers, transport=self.transport)
            self._clients[loop_id] = client
        return client

    def _payload(self, state: str, questions: dict[str, Question]) -> dict[str, Any]:
        return {"model": self.model, "state": state,
                "questions": {name: q.to_api() for name, q in questions.items()}}

    async def evaluate(self, state: str, questions: dict[str, Question]) -> dict[str, JevAnswer]:
        payload = self._payload(state, questions)
        try:
            resp = await self._client().post(self.base_url, json=payload)
        except httpx.HTTPError as exc:
            raise JevError(f"Jev request failed: {exc}") from exc
        if resp.status_code == 429:
            raise JevRateLimitError(_f(resp.headers.get("retry-after")))
        if resp.status_code in (401, 403):
            raise JevAuthError(resp.status_code, self.base_url, self.api_key_env,
                               key_hint(self.api_key), resp.text[:200])
        if resp.status_code >= 400:
            raise JevError(f"Jev returned {resp.status_code} from {self.base_url}: {resp.text[:300]}")
        body = resp.json()
        answers = body.get("answers") or body.get("results")
        if not isinstance(answers, dict):
            # Some gateways report failures with a 200 and an error body. Treating that as
            # "no findings" would silently allow everything, so it is an error here.
            error = body.get("error")
            message = (error.get("message") if isinstance(error, dict) else error) or body
            if isinstance(error, dict) and "rate_limit" in str(error.get("type", "")):
                raise JevRateLimitError()
            raise JevProtocolError(f"unexpected response from {self.base_url}: {str(message)[:200]}")
        # Gateways report what the call actually cost; prefer that over our token estimate.
        # (Success bodies use provider_metadata; some error bodies use providerMetadata.)
        meta = body.get("provider_metadata") or body.get("providerMetadata") or {}
        self.last_cost = _f((meta.get("gateway") or {}).get("cost"))
        self.last_usage = body.get("usage")
        parsed = {name: parse_answer(q, answers.get(name)) for name, q in questions.items() if name in answers}
        if questions and not parsed:
            raise JevProtocolError(f"none of the questions were answered by {self.base_url}: got {list(answers)[:5]}")
        return parsed


class VercelGatewayJev(TypeSafeJev):
    """Jev through the Vercel AI Gateway (``POST /v4/ai/evaluation-model``).

    A Vercel key (``vck_...``) is not a TypeSafe key, so the direct API rejects it. The gateway
    speaks the same typed questions, with three differences this class handles:

    - the model is a header (``ai-model-id``), not a body field, and is namespaced (``typesafe-ai/jev``)
    - the protocol and spec versions are headers too
    - a Noul goes on the wire as the gateway's ``boolean`` question type

    ``disallow_prompt_training`` is on by default and works on every Vercel plan. Zero data
    retention is Pro/Enterprise only, so it is opt-in: the gateway 403s without the plan.
    """

    name = "jev-gateway"

    BASE_URL = "https://ai-gateway.vercel.sh/v4/ai/evaluation-model"
    MODEL = "typesafe-ai/jev"
    PROTOCOL_VERSION = "0.0.1"
    SPEC_VERSION = "4"

    def __init__(self, api_key: str | None = None, *, base_url: str = BASE_URL, model: str = MODEL,
                 api_key_env: str = "AI_GATEWAY_API_KEY", disallow_prompt_training: bool = True,
                 zero_data_retention: bool = False, headers: dict[str, str] | None = None, **kwargs: Any):
        self.provider_options: dict[str, Any] = {}
        if disallow_prompt_training or zero_data_retention:
            self.provider_options = {"gateway": {
                **({"disallowPromptTraining": True} if disallow_prompt_training else {}),
                **({"zeroDataRetention": True} if zero_data_retention else {}),
            }}
        super().__init__(api_key, base_url=base_url, model=model, api_key_env=api_key_env, headers={
            "ai-gateway-protocol-version": self.PROTOCOL_VERSION,
            "ai-gateway-auth-method": "api-key",
            "ai-evaluation-model-specification-version": self.SPEC_VERSION,
            "ai-model-id": model,
            **(headers or {}),
        }, **kwargs)

    def _payload(self, state: str, questions: dict[str, Question]) -> dict[str, Any]:
        wire = {}
        for name, question in questions.items():
            body = question.to_api()
            if body.get("type") == "noul":
                body["type"] = "boolean"   # the gateway's name for the same question
                body.pop("criteria", None)
            wire[name] = body
        payload: dict[str, Any] = {"state": state, "questions": wire}
        if self.provider_options:
            payload["providerOptions"] = self.provider_options
        return payload


class SimulatedJev:
    """Offline stand-in used when no API key is configured.

    It is NOT Jev. It derives probability-shaped answers from the local rule set plus a small
    keyword lexicon, so the whole pipeline (thresholds, dashboard, evals, approvals) can be
    exercised without network access. Decisions made with it are labelled ``jev-sim``.
    """

    name = "jev-sim"

    LEXICON: dict[str, list[str]] = {
        "prompt_injection": [r"ignore .{0,20}instructions", r"system prompt", r"you are now", r"new instructions",
                             r"override", r"act as (the )?(admin|developer|system)"],
        "jailbreak": [r"\bDAN\b", r"no (restrictions|limits|rules)", r"developer mode", r"pretend", r"roleplay",
                      r"hypothetical", r"uncensored"],
        "secret_extraction": [r"system prompt", r"api key", r"password", r"credentials", r"hidden instructions",
                              r"repeat .{0,20}above"],
        "harmful": [r"\b(bomb|explosive|nerve agent|ransomware|keylogger|malware|phishing|steal|hack into|"
                    r"credit card numbers|meth|untraceable|poison|kill (him|her|them)|botnet|ddos)\b"],
        "action": [r"\b(rm|del|drop|truncate|delete|format|shutdown|transfer|wire|payment|chmod|sudo)\b"],
        "exfil": [r"https?://", r"\b(send|upload|post|email|forward)\b.{0,40}\b(key|secret|password|token|file|"
                  r"data|contents|conversation)\b"],
        "embedded": [r"\b(assistant|ai|agent)\b.{0,40}\b(must|should|now)\b", r"ignore .{0,20}instructions",
                     r"do not tell the user", r"call the .{0,20} tool", r"<!--.*-->"],
        "unsafe": [r"step \d|first,.{0,40}then", r"\b(mix|combine|synthesi[sz]e|detonat|inject)\w*\b"],
    }

    def __init__(self, latency_s: float = 0.0):
        self.latency_s = latency_s

    def _p(self, key: str, text: str, base: float = 0.04) -> float:
        hits = sum(1 for pat in self.LEXICON[key] if re.search(pat, text, re.I | re.S))
        return min(0.97, base + 0.3 * hits)

    async def evaluate(self, state: str, questions: dict[str, Question]) -> dict[str, JevAnswer]:
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        stage = Stage.OUTPUT if "AI RESPONSE:" in state else Stage.TOOL_CALL if "TOOL CALL:" in state else \
            Stage.TOOL_RESULT if "(untrusted data)" in state else Stage.INPUT
        local = scan_local(state, stage)
        by_check = {h.check: max(h.score, 0) for h in local.hits}
        body = state.split("AI RESPONSE:", 1)[-1] if stage == Stage.OUTPUT else state
        out: dict[str, JevAnswer] = {}
        for name, q in questions.items():
            if name in ("prompt_injection", "jailbreak", "secret_extraction"):
                p = max(self._p(name, body), by_check.get(name, 0) * 0.95)
            elif name in ("harmful_request", "harmful_output", "intent_risk"):
                p = max(self._p("harmful", body), by_check.get("prompt_injection", 0) * 0.6)
            elif name == "action_risk":
                p = max(self._p("action", body, 0.08), by_check.get("destructive_command", 0),
                        by_check.get("credential_access", 0) * 0.9)
            elif name == "exfiltration":
                p = max(self._p("exfil", body, 0.03) * 0.7, by_check.get("exfiltration", 0))
            elif name in ("embedded_instructions", "malicious_content"):
                p = max(self._p("embedded", body), by_check.get("prompt_injection", 0))
            elif name == "unsafe_instructions":
                p = self._p("unsafe", body) * max(self._p("harmful", body), 0.1) * 2
            elif name == "system_prompt_leak":
                p = max(0.03, by_check.get("system_prompt_leak", 0) * 0.9)
            else:
                p = 0.05
            p = max(0.0, min(0.99, p))
            out[name] = self._shape(q, p)
        return out

    @staticmethod
    def _shape(q: Question, p: float) -> JevAnswer:
        confidence = round(0.55 + abs(p - 0.5) * 0.85, 3)
        if isinstance(q, Noul):
            prob = 1.0 - p if q.invert else p
            return JevAnswer("noul", probability=round(prob, 4), confidence=confidence)
        if isinstance(q, Choice):
            risky = [k for k in q.criteria if k not in q.safe]
            safe = [k for k in q.criteria if k in q.safe]
            probs = {k: round((1 - p) / max(len(safe), 1), 4) for k in safe}
            if risky:
                probs[risky[0 if "cybercrime" not in risky else risky.index("cybercrime")]] = round(p * 0.8, 4)
                rest = [k for k in risky if k not in probs]
                for k in rest:
                    probs[k] = round(p * 0.2 / max(len(rest), 1), 4)
            return JevAnswer("choice", choice=max(probs, key=probs.get), probabilities=probs, confidence=confidence)
        return JevAnswer("score", score=round(p * (len(q.levels) - 1), 3), confidence=confidence)


GATEWAY_KEY_ENV = "AI_GATEWAY_API_KEY"

# Policy defaults that describe the TypeSafe API; the gateway has its own, so don't carry these over.
_TYPESAFE_DEFAULTS = {"base_url": "https://api.typesafe.ai/v1/systemone", "model": "jev-latest",
                      "api_key_env": "TYPESAFE_API_KEY"}


def _gateway_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if _TYPESAFE_DEFAULTS.get(k) != v}


def make_backend(kind: str = "auto", **kwargs: Any) -> JevBackend | None:
    """``auto`` uses the real API when a key env var is set, otherwise the simulator.

    A Vercel AI Gateway key (``$AI_GATEWAY_API_KEY``) selects the gateway backend; ``vercel``
    asks for it outright.
    """
    if kind == "off":
        return None
    if kind == "simulated":
        return SimulatedJev()
    if kind in ("vercel", "gateway"):
        return VercelGatewayJev(**_gateway_kwargs(kwargs))
    key_env = kwargs.get("api_key_env") or "TYPESAFE_API_KEY"
    if kind == "api" or (kind == "auto" and os.environ.get(key_env)):
        return TypeSafeJev(**kwargs)
    if kind == "auto" and os.environ.get(GATEWAY_KEY_ENV):
        return VercelGatewayJev(**_gateway_kwargs(kwargs))
    return SimulatedJev()
