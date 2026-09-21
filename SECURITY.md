# Security Policy

## Supported versions

jevguard is pre-1.0. Fixes land on the latest released version; please upgrade before reporting.

| Version | Supported |
|---|---|
| 0.1.x | yes |

## Reporting a vulnerability

Please report security issues **privately**, not as a public GitHub issue:

- Use [GitHub's private vulnerability reporting](https://github.com/Shubs5758/jevguard-middleware/security/advisories/new), or
- Email **ambavane26@gmail.com** with `jevguard security` in the subject.

Please include what you were running (version, policy, backend), what you expected, what happened,
and a minimal reproduction. You can expect an acknowledgement within a few days.

## Scope

In scope: a bypass of the guard pipeline itself — input that should be blocked by the shipped
policy and is not, a way to make a decision fail *open* silently, a way to smuggle a tool call past
the tool policy, credential or event-log leakage, or an injection into the dashboard.

Out of scope: the accuracy of the Jev model itself, findings that only reproduce against the
offline simulator, and the fact that a degraded window drops to heuristic-level protection — that
is documented behaviour (see "Status and caveats" in the README).

## What jevguard is not

jevguard reduces risk; it does not eliminate it. No guardrail catches every attack. Treat it as one
layer alongside least-privilege tool design, sandboxing and human approval for destructive actions,
and validate it against your own traffic before relying on the numbers.
