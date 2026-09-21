"""Where guard events go. Sinks must never slow down or break the agent they observe."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Protocol

import httpx

from jevguard.store import EventStore

log = logging.getLogger("jevguard")


class Sink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


class MemorySink:
    """Keeps events in a list. Handy for tests and notebooks."""

    def __init__(self, maxlen: int = 10_000):
        self.events: list[dict[str, Any]] = []
        self.maxlen = maxlen

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if len(self.events) > self.maxlen:
            del self.events[: len(self.events) - self.maxlen]


class CallbackSink:
    def __init__(self, fn: Callable[[dict[str, Any]], None]):
        self.fn = fn

    def emit(self, event: dict[str, Any]) -> None:
        try:
            self.fn(event)
        except Exception:
            log.exception("jevguard callback sink failed")


class ConsoleSink:
    """Logs non-allow decisions (or everything with ``verbose=True``)."""

    ICON = {"allow": "+", "flag": "!", "redact": "~", "escalate": "^", "block": "x"}  # ASCII: safe on any console

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def emit(self, event: dict[str, Any]) -> None:
        if event["action"] == "allow" and not self.verbose:
            return
        log.warning("%s %-8s %-11s risk=%.2f src=%s %s", self.ICON.get(event["action"], "?"), event["action"],
                    event["stage"], event["risk"], event["source"], event["reason"])


class SQLiteSink:
    """Writes straight into the dashboard's database. Best for single-machine setups."""

    def __init__(self, path_or_store: str | EventStore = "jevguard.db"):
        self.store = path_or_store if isinstance(path_or_store, EventStore) else EventStore(path_or_store)

    def emit(self, event: dict[str, Any]) -> None:
        try:
            self.store.add_events([event])
        except Exception:
            log.exception("jevguard sqlite sink failed")


class HttpSink:
    """Ships events to a remote dashboard in batches from a background thread.

    ``emit`` only enqueues, so a slow or dead dashboard never adds latency to the agent.
    When the queue is full, the oldest events are dropped and counted.
    """

    def __init__(self, url: str = "http://127.0.0.1:7860", *, api_key: str | None = None, batch_size: int = 50,
                 flush_interval_s: float = 0.5, max_queue: int = 20_000):
        self.endpoint = url.rstrip("/") + "/api/events"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_s
        self.dropped = 0
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jevguard-http-sink", daemon=True)
        self._thread.start()

    def emit(self, event: dict[str, Any]) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _drain(self) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        try:
            batch.append(self._q.get(timeout=self.flush_interval_s))
            while len(batch) < self.batch_size:
                batch.append(self._q.get_nowait())
        except queue.Empty:
            pass
        return batch

    def _run(self) -> None:
        with httpx.Client(timeout=5.0, headers=self.headers) as client:
            backoff = 0.5
            while not self._stop.is_set() or not self._q.empty():
                batch = self._drain()
                if not batch:
                    continue
                try:
                    client.post(self.endpoint, json={"events": batch}).raise_for_status()
                    backoff = 0.5
                except Exception as exc:  # dashboard down: keep the agent running, drop this batch
                    self.dropped += len(batch)
                    log.debug("jevguard http sink: %s", exc)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 10)

    def flush(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while not self._q.empty() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(self.flush_interval_s + 0.1)

    def close(self) -> None:
        self.flush()
        self._stop.set()
