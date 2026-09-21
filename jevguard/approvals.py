"""Human-in-the-loop approval for escalated decisions (risky tool calls, grey-zone verdicts)."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Protocol, Union

import httpx

from jevguard.store import EventStore


class Approver(Protocol):
    async def request(self, request: dict[str, Any]) -> bool: ...


class StaticApprover:
    """Always answers the same way. ``StaticApprover(True)`` is useful in tests and shadow rollouts."""

    def __init__(self, approve: bool):
        self.approve = approve

    async def request(self, request: dict[str, Any]) -> bool:
        return self.approve


class CallbackApprover:
    """Delegates to your own function (Slack button, CLI prompt, ticketing system...)."""

    def __init__(self, fn: Callable[[dict[str, Any]], Union[bool, Awaitable[bool]]]):
        self.fn = fn

    async def request(self, request: dict[str, Any]) -> bool:
        result = self.fn(request)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)


class StoreApprover:
    """Creates a pending approval in a local EventStore and waits for someone to decide it in the dashboard."""

    def __init__(self, store: EventStore, timeout_s: float = 120.0, poll_s: float = 0.5):
        self.store, self.timeout_s, self.poll_s = store, timeout_s, poll_s

    async def request(self, request: dict[str, Any]) -> bool:
        approval_id = self.store.create_approval(request)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while loop.time() < deadline:
            status = (self.store.get_approval(approval_id) or {}).get("status")
            if status in ("approved", "rejected"):
                return status == "approved"
            await asyncio.sleep(self.poll_s)
        self.store.expire_approval(approval_id)
        return False


class DashboardApprover:
    """Same as StoreApprover, but talks to a remote dashboard over HTTP."""

    def __init__(self, url: str = "http://127.0.0.1:7860", timeout_s: float = 120.0, poll_s: float = 1.0,
                 api_key: str | None = None):
        self.base = url.rstrip("/")
        self.timeout_s, self.poll_s = timeout_s, poll_s
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def request(self, request: dict[str, Any]) -> bool:
        async with httpx.AsyncClient(timeout=5.0, headers=self.headers) as client:
            try:
                resp = await client.post(f"{self.base}/api/approvals", json=request)
                resp.raise_for_status()
                approval_id = resp.json()["id"]
            except Exception:
                return False  # cannot reach a human: do not approve
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout_s
            while loop.time() < deadline:
                try:
                    status = (await client.get(f"{self.base}/api/approvals/{approval_id}")).json().get("status")
                except Exception:
                    status = None
                if status in ("approved", "rejected"):
                    return status == "approved"
                await asyncio.sleep(self.poll_s)
            try:
                await client.post(f"{self.base}/api/approvals/{approval_id}/expire")
            except Exception:
                pass
            return False
