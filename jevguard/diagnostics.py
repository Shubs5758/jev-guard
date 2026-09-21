"""`jevguard doctor`: show what jevguard is configured to send, then send one real request.

Most Jev connection problems are one of three things: no key, the key belongs to a gateway but the
URL still points at TypeSafe (or the reverse), or the endpoint does not speak the System One schema.
This tells you which, without printing your key.
"""

from __future__ import annotations

import os
from typing import Any

from jevguard.config import GuardPolicy
from jevguard.env import applied_overrides, find_dotenv
from jevguard.jev.client import (
    JevAuthError, JevError, JevProtocolError, JevRateLimitError, SimulatedJev, TypeSafeJev, key_hint, make_backend,
)
from jevguard.jev.questions import Noul

PROBE = {"probe": Noul("This text mentions a cat")}
PROBE_STATE = "USER MESSAGE:\nthe cat sat on the mat"


async def diagnose(policy: GuardPolicy | None = None, *, backend: Any = None) -> dict[str, Any]:
    policy = policy or GuardPolicy.load()
    js = policy.jev
    key = os.environ.get(js.api_key_env)
    dotenv = find_dotenv()
    report: dict[str, Any] = {
        "env_file": str(dotenv) if dotenv else None,
        "env_overrides": [var for var, _ in applied_overrides()],
        "backend_setting": js.backend,
        "base_url": js.base_url,
        "model": js.model,
        "api_key_env": js.api_key_env,
        "api_key": key_hint(key),
        "extra_headers": sorted(js.headers),
        "policy_mode": policy.mode,
        "fail_mode": policy.fail_mode,
        "status": "unknown",
        "detail": "",
        "hints": [],
    }

    if backend is None:
        try:
            backend = make_backend(js.backend, base_url=js.base_url, model=js.model, timeout_s=js.timeout_s,
                                   api_key_env=js.api_key_env, headers=js.headers) \
                if js.backend in ("api", "auto") else make_backend(js.backend)
        except JevError as exc:
            report.update(status="no_key", detail=str(exc))
            report["hints"].append(f"Set {js.api_key_env} in your .env or environment.")
            return report

    report["backend"] = getattr(backend, "name", "off")
    if backend is None:
        report.update(status="disabled", detail="jev.backend is 'off': every check uses local rules only")
        return report
    if isinstance(backend, SimulatedJev):
        report.update(status="simulator",
                      detail="No API key found, so the offline simulator is in use. It is NOT Jev.")
        report["hints"].append(f"Set {js.api_key_env} to use the real model.")
        return report

    try:
        answers = await backend.evaluate(PROBE_STATE, PROBE)
    except JevAuthError as exc:
        report.update(status="auth_failed", detail=str(exc))
        report["hints"] += [
            f"The key in ${js.api_key_env} was rejected by {js.base_url}.",
            "If the key is for a gateway, set JEVGUARD_JEV_BASE_URL to the gateway's System One endpoint.",
            "If the URL is right, check the key is active and has access to the model "
            f"'{js.model}' (gateways often need a namespaced id).",
        ]
        return report
    except JevRateLimitError as exc:
        report.update(status="rate_limited", detail=str(exc))
        return report
    except JevProtocolError as exc:
        report.update(status="bad_response", detail=str(exc))
        report["hints"] += [
            "The endpoint answered, but not in the System One shape.",
            "jevguard expects {'answers': {'<name>': {'type': 'noul', 'noul': 0.9}}}. "
            "A gateway that only speaks OpenAI chat/completions needs a custom backend (see the README).",
        ]
        return report
    except JevError as exc:
        report.update(status="unreachable", detail=str(exc))
        report["hints"] += [
            "The endpoint did not answer as expected. Check the URL, and any proxy or firewall.",
            "A gateway that only speaks OpenAI chat/completions cannot serve Jev's typed questions; "
            "that needs a custom backend (see the README).",
        ]
        return report

    report.update(status="ok", detail=f"probe answered p={answers['probe'].probability:.2f}, "
                                      f"confidence={answers['probe'].confidence}")
    return report


ICONS = {"ok": "OK", "simulator": "!!", "no_key": "!!", "disabled": "--", "auth_failed": "XX",
         "rate_limited": "!!", "unreachable": "XX", "bad_response": "XX", "unknown": "??"}


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"  .env file        : {report['env_file'] or 'none found'}",
        f"  set by env       : {', '.join(report['env_overrides']) or 'nothing'}",
        f"  endpoint         : {report['base_url']}",
        f"  model            : {report['model']}",
        f"  key variable     : ${report['api_key_env']} ({report['api_key']})",
        f"  extra headers    : {', '.join(report['extra_headers']) or 'none'}",
        f"  backend          : {report.get('backend', '?')} (setting: {report['backend_setting']})",
        f"  policy           : mode={report['policy_mode']}, fail_mode={report['fail_mode']}",
        "",
        f"  {ICONS.get(report['status'], '??')} {report['status'].replace('_', ' ')}",
    ]
    if report["detail"]:
        lines += ["      " + line for line in str(report["detail"]).splitlines()]
    if report["hints"]:
        lines += ["", "  what to try:"] + [f"    - {h}" for h in report["hints"]]
    return "\n".join(lines)
