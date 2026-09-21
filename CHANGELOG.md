# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-21

First public release.

### Added

- **Five guarded stages** across the agent loop: user input, tool call, tool result,
  retrieval and model output, each returning `allow · flag · redact · escalate · block`
  with a calibrated risk score and the evidence behind it.
- **`Guard` engine** with a layered pipeline: local heuristics and PII/secret detection,
  deterministic tool policy, then Jev only when nothing cheaper has already decided.
- **Evidence fusion** (noisy-OR per risk group) so a regex hit and a Jev answer that agree compound.
- **Resilience**: per-call timeout, TTL cache, rate-limit cooldown, circuit breaker, and
  configurable fail-closed/fail-open degradation. A missing or unparseable answer degrades to
  heuristics instead of scoring zero; 401/403 stops calling and is logged once.
- **Framework adapters**: LangChain `JevGuardMiddleware`, LangGraph `guard_graph` /
  `guarded_tool_node` / `input_guard_node` / `output_guard_node`, OpenAI Agents SDK guardrails,
  and framework-agnostic `guard_tool` / `guard_llm` / `filter_documents` decorators.
- **Dashboard and guard API** (FastAPI, no build step): overview, live SSE feed, sessions and
  traces, review queue with a calibration diagram, approvals, playground, evals and policy editing.
- **`RemoteGuard`** and a **JavaScript client** (`clients/js/jevguard.mjs`) so non-Python agents
  can share one central policy.
- **Session risk** that accumulates across turns, tool budgets, loop detection and canary tokens.
- **Human-in-the-loop approvals** via the dashboard, a callback or a static rule, plus
  low-confidence escalation for grey-zone decisions.
- **Red-team eval harness** with CI gates: `jevguard eval --min-recall 0.9 --max-fpr 0.02`.
- **Twelve-factor configuration**: `.env` plus `JEVGUARD_*` overrides layered over an optional
  policy YAML; the API key is named by the policy and never stored in it.
- **CLI**: `dashboard`, `scan`, `eval`, `init`, `demo`, `doctor`.
- **Offline simulator** so the whole pipeline runs with no API key.

### Notes

- Published to PyPI as **`jev-guard`**; the import name is `jevguard`.
- The OpenAI Agents SDK adapter is written against the SDK's public guardrail API but is not
  exercised by the test suite, as the package is not installed here.

[Unreleased]: https://github.com/Shubs5758/jev-guard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Shubs5758/jev-guard/releases/tag/v0.1.0
