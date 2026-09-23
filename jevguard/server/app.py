"""jevguard control plane: event ingestion, guard-as-a-service, approvals, evals, policy, dashboard UI.

Run with ``jevguard dashboard`` or ``uvicorn --factory jevguard.server.app:create_app``.

Machine endpoints (``POST /api/events``, ``POST /api/guard``, ``POST /api/approvals``) require
``Authorization: Bearer $JEVGUARD_API_KEY`` when that variable is set.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from sse_starlette.sse import EventSourceResponse

from jevguard.approvals import StoreApprover
from jevguard.config import GuardPolicy
from jevguard.demo import seed
from jevguard.env import applied_overrides, load_dotenv
from jevguard.engine import Guard
from jevguard.evals.runner import arun_eval, available_datasets, load_dataset
from jevguard.jev.questions import CHECKS
from jevguard.sinks import SQLiteSink
from jevguard.store import EventStore
from jevguard.types import GuardContext, Stage

STATIC = Path(__file__).parent / "static"
WINDOWS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


class GuardRequest(BaseModel):
    stage: Stage
    text: str = ""
    context: dict[str, Any] = {}


# camelCase spellings accepted on `context`, for JavaScript and other non-Python callers.
CTX_ALIASES = {"".join(p.capitalize() if i else p for i, p in enumerate(name.split("_"))): name
               for name in GuardContext.__dataclass_fields__ if "_" in name}


class ReviewRequest(BaseModel):
    label: str  # true_positive | false_positive | true_negative | false_negative
    note: str | None = None


class DecideRequest(BaseModel):
    approved: bool
    note: str | None = None
    by: str = "dashboard"


class PolicyRequest(BaseModel):
    yaml: str


class EvalRequest(BaseModel):
    dataset: str = "redteam_v1"


def create_app(db_path: str | None = None, policy_path: str | None = None) -> FastAPI:
    load_dotenv()   # so JEVGUARD_DB / JEVGUARD_API_KEY / the Jev settings can come from .env
    db_path = db_path or os.environ.get("JEVGUARD_DB", "jevguard.db")
    policy_path = policy_path or os.environ.get("JEVGUARD_POLICY")
    api_key = os.environ.get("JEVGUARD_API_KEY")
    store = EventStore(db_path)

    saved = store.get_kv("policy")
    if saved:
        policy = GuardPolicy.load(yaml.safe_load(saved))
    else:
        policy = GuardPolicy.load(policy_path)   # None is fine; env/.env overrides still apply

    sink = SQLiteSink(store)
    # Guard-as-a-service: escalations wait for a human in the Approvals page.
    service_guard = Guard(policy, sinks=[sink], approver=StoreApprover(store, policy.approvals.timeout_s),
                          framework="remote")
    # Playground: same policy, never blocks waiting on a human.
    playground_guard = Guard(policy, backend=service_guard.backend, sinks=[sink], framework="playground")

    app = FastAPI(title="jevguard", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.store = store
    app.state.guard = service_guard

    def require_key(authorization: str | None = Header(default=None)) -> None:
        if api_key and authorization != f"Bearer {api_key}":
            raise HTTPException(401, "invalid or missing API key")

    def ctx_from(data: dict[str, Any]) -> GuardContext:
        # JavaScript callers write camelCase. Unknown keys are dropped, so accepting only snake_case
        # silently guards a tool call with no tool name or args - which allows everything.
        allowed = GuardContext.__dataclass_fields__.keys()
        fields = {CTX_ALIASES.get(k, k): v for k, v in data.items()}
        return GuardContext(**{k: v for k, v in fields.items() if k in allowed})

    # ---- UI ----------------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---- ingestion & guard-as-a-service -------------------------------------------------------------
    @app.post("/api/events", dependencies=[Depends(require_key)])
    async def ingest(payload: dict[str, Any]) -> dict[str, int]:
        events = payload.get("events") or []
        store.add_events(events)
        return {"accepted": len(events)}

    @app.post("/api/guard", dependencies=[Depends(require_key)])
    async def guard_endpoint(req: GuardRequest) -> dict[str, Any]:
        d = await service_guard.acheck(req.stage, req.text, ctx_from(req.context))
        return d.to_dict()

    @app.post("/api/playground")
    async def playground(req: GuardRequest) -> dict[str, Any]:
        ctx = ctx_from(req.context)
        ctx.session_id = ctx.session_id or "playground"
        d = await playground_guard.acheck(req.stage, req.text, ctx)
        return d.to_dict()

    # ---- events ----------------------------------------------------------------------------------------
    @app.get("/api/events")
    def list_events(stage: str | None = None, action: str | None = None, session_id: str | None = None,
                    q: str | None = None, window: str | None = None, limit: int = Query(100, le=1000),
                    offset: int = 0) -> list[dict[str, Any]]:
        since = time.time() - WINDOWS[window] if window in WINDOWS else None
        return store.list_events(stage=stage, action=action, session_id=session_id, q=q, since=since,
                                 limit=limit, offset=offset)

    @app.get("/api/events/{event_id}")
    def get_event(event_id: str) -> dict[str, Any]:
        event = store.get_event(event_id)
        if not event:
            raise HTTPException(404, "event not found")
        return event

    @app.post("/api/events/{event_id}/review")
    def review(event_id: str, req: ReviewRequest) -> dict[str, bool]:
        if req.label not in ("true_positive", "false_positive", "true_negative", "false_negative"):
            raise HTTPException(400, "unknown label")
        if not store.review(event_id, req.label, req.note):
            raise HTTPException(404, "event not found")
        return {"ok": True}

    @app.get("/api/review-queue")
    def review_queue(limit: int = 100) -> list[dict[str, Any]]:
        return store.list_events(actions=["flag", "escalate", "block", "redact"], unreviewed_only=True, limit=limit)

    @app.get("/api/stats")
    def stats(window: str = "24h") -> dict[str, Any]:
        data = store.stats(time.time() - WINDOWS.get(window, 86400))
        data["guard"] = service_guard.status()
        return data

    @app.get("/api/sessions")
    def sessions(limit: int = 100) -> list[dict[str, Any]]:
        return store.sessions(limit)

    @app.get("/api/sessions/{session_id}")
    def session_detail(session_id: str) -> list[dict[str, Any]]:
        return list(reversed(store.list_events(session_id=session_id, limit=500)))

    @app.get("/api/calibration")
    def calibration() -> dict[str, Any]:
        return store.calibration()

    @app.get("/api/stream")
    async def stream(request: Request) -> EventSourceResponse:
        async def gen():
            last = time.time()
            while not await request.is_disconnected():
                events = store.list_events(since=last + 1e-6, limit=200)
                if events:
                    last = max(e["ts"] for e in events)
                    for e in reversed(events):
                        yield {"event": "guard", "data": json.dumps(e, default=str)}
                pending = store.list_approvals("pending", limit=50)
                yield {"event": "heartbeat", "data": json.dumps({"pending_approvals": len(pending)})}
                await asyncio.sleep(1.0)
        return EventSourceResponse(gen())

    # ---- approvals ---------------------------------------------------------------------------------------
    @app.post("/api/approvals", dependencies=[Depends(require_key)])
    def create_approval(payload: dict[str, Any]) -> dict[str, str]:
        return {"id": store.create_approval(payload)}

    @app.get("/api/approvals")
    def list_approvals(status: str | None = None) -> list[dict[str, Any]]:
        return store.list_approvals(status)

    @app.get("/api/approvals/{approval_id}")
    def get_approval(approval_id: str) -> dict[str, Any]:
        a = store.get_approval(approval_id)
        if not a:
            raise HTTPException(404, "approval not found")
        return a

    @app.post("/api/approvals/{approval_id}/decide")
    def decide(approval_id: str, req: DecideRequest) -> dict[str, bool]:
        if not store.decide_approval(approval_id, req.approved, req.by, req.note):
            raise HTTPException(409, "approval is not pending")
        return {"ok": True}

    @app.post("/api/approvals/{approval_id}/expire")
    def expire(approval_id: str) -> dict[str, bool]:
        store.expire_approval(approval_id)
        return {"ok": True}

    # ---- policy ------------------------------------------------------------------------------------------
    @app.get("/api/policy")
    def get_policy() -> dict[str, Any]:
        return {"yaml": service_guard.policy.to_yaml(), "policy": service_guard.policy.model_dump(mode="json"),
                "checks": {name: {"label": c.label, "stages": sorted(s.value for s in c.stages)} for name, c in CHECKS.items()},
                # fields pinned by the environment / .env: editing them here has no effect
                "env_overrides": [{"var": var, "path": path} for var, path in applied_overrides()]}

    @app.put("/api/policy")
    def put_policy(req: PolicyRequest) -> dict[str, Any]:
        try:
            new = GuardPolicy.load(yaml.safe_load(req.yaml) or {})
        except (yaml.YAMLError, ValidationError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        new.version = service_guard.policy.version + 1
        for g in (service_guard, playground_guard):
            g.set_policy(new)
        store.set_kv("policy", new.to_yaml())
        return {"ok": True, "version": new.version}

    # ---- evals --------------------------------------------------------------------------------------------
    @app.get("/api/evals/datasets")
    def datasets() -> list[str]:
        return available_datasets()

    @app.post("/api/evals/run")
    async def run_eval_endpoint(req: EvalRequest) -> dict[str, Any]:
        try:
            cases = load_dataset(req.dataset)
        except FileNotFoundError:
            raise HTTPException(404, "dataset not found")
        eval_guard = Guard(service_guard.policy, backend=service_guard.backend)
        result = await arun_eval(eval_guard, cases)
        run_id = store.add_eval_run(req.dataset, result["metrics"], result["cases"])
        return {"id": run_id, **result}

    @app.get("/api/evals")
    def list_evals() -> list[dict[str, Any]]:
        return store.list_eval_runs()

    @app.get("/api/evals/{run_id}")
    def get_eval(run_id: str) -> dict[str, Any]:
        run = store.get_eval_run(run_id)
        if not run:
            raise HTTPException(404, "eval run not found")
        return run

    # ---- demo & housekeeping --------------------------------------------------------------------------
    @app.post("/api/demo/seed")
    async def demo_seed(sessions: int = 60) -> dict[str, int]:
        demo_guard = Guard(service_guard.policy, backend=service_guard.backend)
        events = await seed(demo_guard, sessions=sessions)
        store.add_events(events)
        return {"events": len(events)}

    @app.delete("/api/events")
    def clear() -> dict[str, bool]:
        store.clear_events()
        return {"ok": True}

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, **service_guard.status()}

    return app
