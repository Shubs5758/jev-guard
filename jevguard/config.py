"""Guard policy: what to check at each stage, and what to do about it.

A policy is plain data (YAML/JSON/dict) so it can be versioned, edited in the dashboard,
and pulled by remote agents from a central server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, Field

from jevguard.types import Stage


class Thresholds(BaseModel):
    flag: float = 0.5
    block: float = 0.85


class StagePolicy(BaseModel):
    enabled: bool = True
    # Jev question packs to ask at this stage (see jevguard.jev.questions.CHECKS).
    checks: list[str] = Field(default_factory=list)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    # Per-check overrides, e.g. {"pii": {"block": 1.01, "flag": 0.4}}
    check_thresholds: dict[str, Thresholds] = Field(default_factory=dict)
    # What to do when PII / secrets are found: redact the text or treat it as a finding.
    pii: Literal["redact", "flag", "block", "ignore"] = "flag"
    secrets: Literal["redact", "flag", "block", "ignore"] = "block"
    # Send low-confidence grey-zone decisions to a human instead of guessing.
    escalate_low_confidence: bool = False
    min_confidence: float = 0.6


class ToolPolicy(BaseModel):
    allow: list[str] = Field(default_factory=list)             # empty = every tool allowed
    deny: list[str] = Field(default_factory=list)
    require_approval: list[str] = Field(default_factory=list)  # human-in-the-loop tools
    # Regexes matched against the JSON-serialised arguments of a specific tool ("*" = any tool)
    arg_deny_patterns: dict[str, list[str]] = Field(default_factory=dict)
    # Hosts tools may contact. Empty = no egress restriction.
    egress_allowlist: list[str] = Field(default_factory=list)


class SessionPolicy(BaseModel):
    max_tool_calls: int = 60
    max_identical_tool_calls: int = 4    # loop detection: same tool + same args
    risk_decay: float = 0.85             # cumulative risk decays each turn
    escalate_at: float = 2.2             # cumulative risk that flips a session to "escalate"
    block_at: float = 3.5                # cumulative risk that blocks the session outright


class JevSettings(BaseModel):
    backend: Literal["auto", "api", "simulated", "off"] = "auto"
    model: str = "jev-latest"
    base_url: str = "https://api.typesafe.ai/v1/systemone"
    # Name of the environment variable holding the key. Point this at your gateway's key
    # (e.g. AI_GATEWAY_API_KEY) when you route Jev through a proxy. The key is never stored
    # in the policy itself, so policies stay safe to commit and to edit in the dashboard.
    api_key_env: str = "TYPESAFE_API_KEY"
    headers: dict[str, str] = Field(default_factory=dict)   # extra headers for a gateway or proxy
    timeout_s: float = 3.0
    rate_limit_cooldown_s: float = 60.0
    auth_retry_s: float = 900.0          # after a 401/403, wait this long before trying again
    circuit_failures: int = 5            # consecutive failures before the circuit opens
    circuit_reset_s: float = 30.0
    cache_ttl_s: float = 300.0
    cache_size: int = 2048
    price_per_million_input_tokens: float = 0.042


class ApprovalSettings(BaseModel):
    # "block": escalations are blocked unless an approver says yes.
    # "allow": escalations pass (recorded for later review).
    default: Literal["block", "allow"] = "block"
    timeout_s: float = 120.0


class GuardPolicy(BaseModel):
    name: str = "default"
    version: int = 1
    mode: Literal["enforce", "shadow"] = "enforce"   # shadow = observe and record only
    fail_mode: Literal["closed", "open"] = "closed"  # when Jev is down and heuristics are unsure
    scope: str | None = None                         # e.g. "customer support for Acme's billing product"
    canaries: list[str] = Field(default_factory=list)
    redact_logged_text: bool = False                 # scrub PII/secrets before events leave the process
    jev: JevSettings = Field(default_factory=JevSettings)
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    session: SessionPolicy = Field(default_factory=SessionPolicy)
    approvals: ApprovalSettings = Field(default_factory=ApprovalSettings)
    stages: dict[Stage, StagePolicy] = Field(default_factory=dict)

    def model_post_init(self, _: Any) -> None:
        # A partial stage (say, only a threshold set in .env) keeps that stage's defaults for
        # everything it did not mention - otherwise tuning one number would silently drop its checks.
        for stage, default in _default_stages().items():
            given = self.stages.get(stage)
            self.stages[stage] = default if given is None else _fill_unset(given, default)

    def stage(self, stage: Stage) -> StagePolicy:
        return self.stages[stage]

    def thresholds_for(self, stage: Stage, check: str) -> Thresholds:
        sp = self.stages[stage]
        return sp.check_thresholds.get(check, sp.thresholds)

    # --- loading / saving -------------------------------------------------------------
    @classmethod
    def load(cls, source: str | Path | dict[str, Any] | None = None, *, env: bool = True) -> "GuardPolicy":
        """Load a policy from YAML, a dict, or nothing at all.

        With ``env=True`` (the default) a ``.env`` file is loaded and any ``JEVGUARD_*`` variables
        override what the file says, so one policy can be deployed to several environments.
        """
        if source is None:
            data: dict[str, Any] = {}
        elif isinstance(source, dict):
            data = dict(source)
        else:
            data = yaml.safe_load(Path(source).read_text(encoding="utf-8")) or {}
        if env:
            from jevguard.env import apply_env, load_dotenv

            load_dotenv()
            data = apply_env(data)
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_yaml(), encoding="utf-8")


T = TypeVar("T", bound=BaseModel)


def _fill_unset(given: T, default: T) -> T:
    """Copy every field the caller did not set explicitly from ``default``, recursively."""
    for name in type(given).model_fields:
        fallback = getattr(default, name)
        if name not in given.model_fields_set:
            setattr(given, name, fallback)
        else:
            current = getattr(given, name)
            if isinstance(current, BaseModel) and isinstance(fallback, BaseModel):
                _fill_unset(current, fallback)
    return given


def _default_stages() -> dict[Stage, StagePolicy]:
    return {
        Stage.INPUT: StagePolicy(
            checks=["prompt_injection", "jailbreak", "harmful_request", "secret_extraction", "intent_risk"],
            pii="flag",
        ),
        Stage.TOOL_CALL: StagePolicy(
            checks=["action_risk", "exfiltration", "goal_misalignment"],
            thresholds=Thresholds(flag=0.45, block=0.8),
            pii="ignore",
        ),
        Stage.TOOL_RESULT: StagePolicy(
            checks=["embedded_instructions", "malicious_content"],
            thresholds=Thresholds(flag=0.45, block=0.8),
            pii="redact",
        ),
        Stage.RETRIEVAL: StagePolicy(
            checks=["embedded_instructions", "malicious_content"],
            thresholds=Thresholds(flag=0.45, block=0.8),
            pii="ignore",
        ),
        Stage.OUTPUT: StagePolicy(
            checks=["harmful_output", "system_prompt_leak", "unsafe_instructions", "groundedness", "off_scope"],
            pii="redact",
        ),
    }
