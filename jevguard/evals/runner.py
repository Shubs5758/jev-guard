"""Offline evaluation: run a labelled dataset through a guard and measure how well it blocks.

Dataset format (JSONL), one case per line:

    {"id": "x1", "stage": "input", "text": "...", "expected": "block" | "allow",
     "category": "prompt_injection", "tool_name": "...", "tool_args": {...}, "user_goal": "..."}

A case counts as *caught* when the guard blocks it (``blocked`` is True). Flags are reported
separately so you can see how much the review queue would have picked up.

A run where Jev did not answer - rate limit, open circuit, bad credentials, no backend - scores the
local heuristics rather than the guard, and would otherwise look like a real result. Those cases are
counted in ``degraded_cases`` and the run is marked ``degraded``; ``jevguard eval`` fails on it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jevguard.engine import Guard
from jevguard.types import GuardContext, Stage

DATASETS_DIR = Path(__file__).parent / "datasets"


def available_datasets() -> list[str]:
    return sorted(p.stem for p in DATASETS_DIR.glob("*.jsonl"))


def load_dataset(name_or_path: str | Path) -> list[dict[str, Any]]:
    path = Path(name_or_path)
    if not path.exists():
        path = DATASETS_DIR / f"{name_or_path}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass
class _Counts:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    def add(self, expected_block: bool, blocked: bool) -> None:
        if expected_block and blocked:
            self.tp += 1
        elif expected_block:
            self.fn += 1
        elif blocked:
            self.fp += 1
        else:
            self.tn += 1

    def metrics(self) -> dict[str, float | int]:
        p = self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0
        r = self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0
        n = self.tp + self.fp + self.tn + self.fn
        return {
            "n": n, "tp": self.tp, "fp": self.fp, "tn": self.tn, "fn": self.fn,
            "precision": round(p, 4), "recall": round(r, 4),
            "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0,
            "fpr": round(self.fp / (self.fp + self.tn), 4) if self.fp + self.tn else 0.0,
            "accuracy": round((self.tp + self.tn) / n, 4) if n else 0.0,
        }


async def arun_eval(guard: Guard, cases: list[dict[str, Any]]) -> dict[str, Any]:
    overall = _Counts()
    per_stage: dict[str, _Counts] = {}
    per_category: dict[str, _Counts] = {}
    results, latencies = [], []
    flagged_misses = 0
    degraded_cases = 0
    jev_before, cost_before = guard.counters["jev_calls"], guard.counters["cost_usd"]
    started = time.perf_counter()
    for case in cases:
        stage = Stage(case["stage"])
        ctx = GuardContext(tool_name=case.get("tool_name"), tool_args=case.get("tool_args"),
                           user_goal=case.get("user_goal"), grounding=case.get("grounding"),
                           session_id=None, framework="eval")
        d = await guard.acheck(stage, case.get("text", ""), ctx)
        expected_block = case["expected"] == "block"
        blocked = d.action.value == "block" or d.blocked
        overall.add(expected_block, blocked)
        per_stage.setdefault(stage.value, _Counts()).add(expected_block, blocked)
        per_category.setdefault(case.get("category", "other"), _Counts()).add(expected_block, blocked)
        if expected_block and not blocked and d.action.value != "allow":
            flagged_misses += 1
        if d.source == "heuristics_degraded":
            degraded_cases += 1
        latencies.append(d.latency_ms)
        results.append({
            "id": case.get("id"), "stage": stage.value, "category": case.get("category"),
            "expected": case["expected"], "action": d.action.value, "risk": d.risk, "source": d.source,
            "correct": expected_block == blocked, "reason": d.reason, "latency_ms": round(d.latency_ms, 2),
            "text": (case.get("text") or json.dumps(case.get("tool_args") or {}))[:300], "tool_name": case.get("tool_name"),
        })
    latencies.sort()

    def pct(q: float) -> float:
        return round(latencies[min(int(q * len(latencies)), len(latencies) - 1)], 2) if latencies else 0.0

    metrics = {
        **overall.metrics(),
        "flagged_misses": flagged_misses,
        # Cases Jev never answered (circuit open, rate limit, auth failure, no backend). Those were
        # scored on local rules alone, so the run measures the heuristics, not the guard - see `degraded`.
        "degraded_cases": degraded_cases,
        "degraded_frac": round(degraded_cases / len(cases), 4) if cases else 0.0,
        "degraded": degraded_cases > 0,
        "latency_p50_ms": pct(0.5), "latency_p95_ms": pct(0.95),
        "jev_calls": guard.counters["jev_calls"] - jev_before,
        "cost_usd": round(guard.counters["cost_usd"] - cost_before, 8),
        "backend": getattr(guard.backend, "name", "off"),
        "duration_s": round(time.perf_counter() - started, 3),
        "per_stage": {k: v.metrics() for k, v in per_stage.items()},
        "per_category": {k: v.metrics() for k, v in per_category.items()},
    }
    return {"metrics": metrics, "cases": results}


def run_eval(guard: Guard, cases: list[dict[str, Any]]) -> dict[str, Any]:
    from jevguard.engine import run_sync

    return run_sync(arun_eval(guard, cases))
