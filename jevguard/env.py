"""`.env` support and environment overrides for the policy.

jevguard reads a ``.env`` file (searched from the working directory upwards) and lets any policy
field that is deployment config - keys, URLs, mode, thresholds, tool lists - be set as an
environment variable. Precedence, highest first:

1. real environment variables
2. the ``.env`` file
3. the YAML/dict policy
4. built-in defaults

So a policy file stays a *safe-to-commit* description of your rules, and anything that changes per
environment (or must not be committed) lives in ``.env``. No third-party dependency is needed.

The API key itself is never read into the policy: the policy only holds the *name* of the variable
that carries it (``jev.api_key_env``, default ``TYPESAFE_API_KEY``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

DOTENV_NAME = ".env"


# --- parsing -------------------------------------------------------------------------------------
def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines. Supports comments, blank lines, ``export`` and quoted values."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
        if quoted and value[0] == '"':
            value = value[1:-1].replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
        elif quoted:
            value = value[1:-1]  # single quotes are literal
        else:
            value = value.split(" #")[0].strip()  # trailing comment on an unquoted value
        out[key] = value
    return out


def find_dotenv(start: str | Path | None = None) -> Path | None:
    """``JEVGUARD_ENV_FILE`` if set, otherwise the nearest ``.env`` from ``start`` upwards."""
    explicit = os.environ.get("JEVGUARD_ENV_FILE")
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    here = Path(start or Path.cwd()).resolve()
    for folder in [here, *here.parents]:
        candidate = folder / DOTENV_NAME
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Load a ``.env`` into ``os.environ``. Real environment variables win unless ``override``."""
    if os.environ.get("JEVGUARD_NO_DOTENV"):
        return {}
    found = Path(path) if path else find_dotenv()
    if not found or not found.is_file():
        return {}
    values = parse_dotenv(found.read_text(encoding="utf-8"))
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


# --- value parsers --------------------------------------------------------------------------------
def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _csv(v: str) -> list[str]:
    return [item.strip() for item in v.split(",") if item.strip()]


def _kv(v: str) -> dict[str, str]:
    """``a=1,b=2`` or a JSON object."""
    v = v.strip()
    if v.startswith("{"):
        return {str(k): str(val) for k, val in json.loads(v).items()}
    out = {}
    for pair in _csv(v):
        key, _, value = pair.partition("=")
        if key.strip():
            out[key.strip()] = value.strip()
    return out


def _lower(v: str) -> str:
    return v.strip().lower()


# --- the map ----------------------------------------------------------------------------------------
# (environment variable, dotted path into the policy, parser)
ENV_MAP: list[tuple[str, str, Callable[[str], Any]]] = [
    ("JEVGUARD_POLICY_NAME", "name", str),
    ("JEVGUARD_MODE", "mode", _lower),
    ("JEVGUARD_FAIL_MODE", "fail_mode", _lower),
    ("JEVGUARD_SCOPE", "scope", str),
    ("JEVGUARD_CANARIES", "canaries", _csv),
    ("JEVGUARD_REDACT_LOGGED_TEXT", "redact_logged_text", _bool),
    # Jev connection: the gateway settings from the previous step
    ("JEVGUARD_JEV_BACKEND", "jev.backend", _lower),
    ("JEVGUARD_JEV_BASE_URL", "jev.base_url", str),
    ("JEVGUARD_JEV_MODEL", "jev.model", str),
    ("JEVGUARD_JEV_API_KEY_ENV", "jev.api_key_env", str),
    ("JEVGUARD_JEV_HEADERS", "jev.headers", _kv),
    ("JEVGUARD_JEV_TIMEOUT_S", "jev.timeout_s", float),
    ("JEVGUARD_JEV_CACHE_TTL_S", "jev.cache_ttl_s", float),
    ("JEVGUARD_JEV_RATE_LIMIT_COOLDOWN_S", "jev.rate_limit_cooldown_s", float),
    ("JEVGUARD_JEV_CIRCUIT_FAILURES", "jev.circuit_failures", int),
    # tool governance
    ("JEVGUARD_TOOLS_ALLOW", "tools.allow", _csv),
    ("JEVGUARD_TOOLS_DENY", "tools.deny", _csv),
    ("JEVGUARD_TOOLS_REQUIRE_APPROVAL", "tools.require_approval", _csv),
    ("JEVGUARD_TOOLS_EGRESS_ALLOWLIST", "tools.egress_allowlist", _csv),
    # sessions & approvals
    ("JEVGUARD_SESSION_MAX_TOOL_CALLS", "session.max_tool_calls", int),
    ("JEVGUARD_SESSION_MAX_IDENTICAL_TOOL_CALLS", "session.max_identical_tool_calls", int),
    ("JEVGUARD_SESSION_ESCALATE_AT", "session.escalate_at", float),
    ("JEVGUARD_SESSION_BLOCK_AT", "session.block_at", float),
    ("JEVGUARD_APPROVALS_DEFAULT", "approvals.default", _lower),
    ("JEVGUARD_APPROVALS_TIMEOUT_S", "approvals.timeout_s", float),
]

# Per-stage knobs: JEVGUARD_INPUT_BLOCK=0.8, JEVGUARD_OUTPUT_PII=redact, JEVGUARD_TOOL_CALL_CHECKS=a,b
for _stage in ("input", "tool_call", "tool_result", "retrieval", "output"):
    _s = _stage.upper()
    ENV_MAP += [
        (f"JEVGUARD_{_s}_ENABLED", f"stages.{_stage}.enabled", _bool),
        (f"JEVGUARD_{_s}_BLOCK", f"stages.{_stage}.thresholds.block", float),
        (f"JEVGUARD_{_s}_FLAG", f"stages.{_stage}.thresholds.flag", float),
        (f"JEVGUARD_{_s}_CHECKS", f"stages.{_stage}.checks", _csv),
        (f"JEVGUARD_{_s}_PII", f"stages.{_stage}.pii", _lower),
        (f"JEVGUARD_{_s}_SECRETS", f"stages.{_stage}.secrets", _lower),
        (f"JEVGUARD_{_s}_ESCALATE_LOW_CONFIDENCE", f"stages.{_stage}.escalate_low_confidence", _bool),
    ]


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    keys = path.split(".")
    for key in keys[:-1]:
        nested = target.get(key)
        if not isinstance(nested, dict):
            nested = {}
            target[key] = nested
        target = nested
    target[keys[-1]] = value


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def env_overrides(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The policy fragment described by the current environment."""
    env = os.environ if env is None else env
    out: dict[str, Any] = {}
    for name, path, parse in ENV_MAP:
        raw = env.get(name)
        if raw is None or raw == "":
            continue
        try:
            _set_path(out, path, parse(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{name}={raw!r} is not valid for {path}: {exc}") from exc
    return out


def applied_overrides(env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """``(variable, policy path)`` for every override in effect - the dashboard shows these as pinned."""
    env = os.environ if env is None else env
    return [(name, path) for name, path, _ in ENV_MAP if env.get(name)]


def apply_env(policy_data: dict[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return _merge(policy_data, env_overrides(env))


def env_names() -> list[str]:
    return [name for name, _, _ in ENV_MAP]
