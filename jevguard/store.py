"""SQLite event store shared by the SQLite sink and the dashboard server."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    stage TEXT, action TEXT, risk REAL, source TEXT,
    enforced INTEGER, blocked INTEGER,
    session_id TEXT, agent TEXT, framework TEXT, tool_name TEXT,
    reason TEXT, text TEXT,
    latency_ms REAL, jev_called INTEGER, cost REAL,
    payload TEXT,
    review_label TEXT, review_note TEXT, reviewed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_action ON events(action, ts);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, ts REAL, status TEXT,
    stage TEXT, tool_name TEXT, session_id TEXT, agent TEXT, summary TEXT,
    payload TEXT, decided_at REAL, decided_by TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY, ts REAL, dataset TEXT, metrics TEXT, cases TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
"""

EVENT_COLUMNS = ["id", "ts", "stage", "action", "risk", "source", "enforced", "blocked", "session_id", "agent",
                 "framework", "tool_name", "reason", "text", "latency_ms", "jev_called", "cost", "payload",
                 "review_label", "review_note", "reviewed_at"]


class EventStore:
    def __init__(self, path: str | Path = "jevguard.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    # --- events -----------------------------------------------------------------------------------
    def add_events(self, events: list[dict[str, Any]]) -> None:
        rows = [(
            e["event_id"], e["ts"], e["stage"], e["action"], e["risk"], e["source"], int(e["enforced"]),
            int(e["blocked"]), e.get("session_id"), e.get("agent"), e.get("framework"), e.get("tool_name"),
            e.get("reason"), e.get("text"), e.get("latency_ms"), int(e.get("jev_called", False)),
            e.get("jev_cost_usd", 0.0), json.dumps(e, default=str),
        ) for e in events]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO events (id, ts, stage, action, risk, source, enforced, blocked, session_id, "
                "agent, framework, tool_name, reason, text, latency_ms, jev_called, cost, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def _event(self, row: sqlite3.Row) -> dict[str, Any]:
        e = json.loads(row["payload"])
        e["review_label"], e["review_note"], e["reviewed_at"] = row["review_label"], row["review_note"], row["reviewed_at"]
        return e

    def list_events(self, *, stage: str | None = None, action: str | None = None, session_id: str | None = None,
                    q: str | None = None, since: float | None = None, limit: int = 200, offset: int = 0,
                    unreviewed_only: bool = False, actions: list[str] | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        for col, val in (("stage", stage), ("action", action), ("session_id", session_id)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if actions:
            where.append(f"action IN ({','.join('?' * len(actions))})")
            args += actions
        if since:
            where.append("ts >= ?")
            args.append(since)
        if q:
            where.append("(text LIKE ? OR reason LIKE ? OR tool_name LIKE ? OR session_id LIKE ? OR agent LIKE ?)")
            args += [f"%{q}%"] * 5
        if unreviewed_only:
            where.append("review_label IS NULL")
        sql = "SELECT * FROM events" + (f" WHERE {' AND '.join(where)}" if where else "")
        sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._conn.execute(sql, [*args, limit, offset]).fetchall()
        return [self._event(r) for r in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def review(self, event_id: str, label: str, note: str | None = None) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE events SET review_label = ?, review_note = ?, reviewed_at = ? WHERE id = ?",
                                     (label, note, time.time(), event_id))
        return cur.rowcount > 0

    def stats(self, since: float, buckets: int = 48) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            c = self._conn
            total = c.execute("SELECT COUNT(*), COALESCE(SUM(cost),0), COALESCE(SUM(jev_called),0), "
                              "COUNT(DISTINCT session_id) FROM events WHERE ts >= ?", (since,)).fetchone()
            by_action = dict(c.execute("SELECT action, COUNT(*) FROM events WHERE ts >= ? GROUP BY action", (since,)).fetchall())
            by_stage = {r[0]: {"total": r[1], "blocked": r[2], "flagged": r[3]} for r in c.execute(
                "SELECT stage, COUNT(*), SUM(action='block'), SUM(action IN ('flag','escalate','redact')) "
                "FROM events WHERE ts >= ? GROUP BY stage", (since,)).fetchall()}
            by_source = dict(c.execute("SELECT source, COUNT(*) FROM events WHERE ts >= ? GROUP BY source", (since,)).fetchall())
            latencies = [r[0] for r in c.execute("SELECT latency_ms FROM events WHERE ts >= ? ORDER BY latency_ms", (since,)).fetchall()]
            width = max((now - since) / buckets, 1)
            series_rows = c.execute(
                "SELECT CAST((ts - ?) / ? AS INTEGER) b, action, COUNT(*) FROM events WHERE ts >= ? GROUP BY b, action",
                (since, width, since)).fetchall()
            payloads = c.execute("SELECT payload FROM events WHERE ts >= ? AND action != 'allow' ORDER BY ts DESC LIMIT 2000",
                                 (since,)).fetchall()
            top_tools = [{"tool": r[0], "total": r[1], "blocked": r[2]} for r in c.execute(
                "SELECT tool_name, COUNT(*), SUM(action='block') FROM events WHERE ts >= ? AND tool_name IS NOT NULL "
                "GROUP BY tool_name ORDER BY 3 DESC, 2 DESC LIMIT 8", (since,)).fetchall()]
            reviewed = dict(c.execute("SELECT review_label, COUNT(*) FROM events WHERE review_label IS NOT NULL "
                                      "AND ts >= ? GROUP BY review_label", (since,)).fetchall())
            pending = c.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0]
        series = [{"t": since + i * width, "allow": 0, "flag": 0, "block": 0, "redact": 0, "escalate": 0} for i in range(buckets)]
        for b, action, n in series_rows:
            if 0 <= b < buckets and action in series[b]:
                series[b][action] += n
        categories: dict[str, int] = {}
        for (p,) in payloads:
            for f in json.loads(p).get("findings", [])[:2]:
                if f["score"] >= 0.5:
                    categories[f["check"]] = categories.get(f["check"], 0) + 1

        def pct(q: float) -> float:
            return round(latencies[min(int(q * len(latencies)), len(latencies) - 1)], 2) if latencies else 0.0

        jev_calls = int(total[2])
        return {
            "total": total[0], "cost_usd": total[1], "jev_calls": jev_calls, "sessions": total[3],
            "saved_calls": total[0] - jev_calls,
            "by_action": by_action, "by_stage": by_stage, "by_source": by_source,
            "latency": {"p50": pct(0.5), "p95": pct(0.95), "p99": pct(0.99)},
            "series": series, "bucket_seconds": width,
            "categories": sorted(({"check": k, "count": v} for k, v in categories.items()), key=lambda d: -d["count"])[:10],
            "top_tools": top_tools, "reviewed": reviewed, "pending_approvals": pending,
        }

    def sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, MIN(ts), MAX(ts), COUNT(*), SUM(action='block'), SUM(action IN ('flag','escalate','redact')), "
                "MAX(risk), GROUP_CONCAT(DISTINCT agent), GROUP_CONCAT(DISTINCT framework), SUM(stage='tool_call') "
                "FROM events WHERE session_id IS NOT NULL GROUP BY session_id ORDER BY MAX(ts) DESC LIMIT ?", (limit,)).fetchall()
        return [{"session_id": r[0], "started": r[1], "last_seen": r[2], "events": r[3], "blocked": r[4], "flagged": r[5],
                 "max_risk": r[6], "agent": r[7], "framework": r[8], "tool_calls": r[9]} for r in rows]

    def calibration(self, bins: int = 10) -> dict[str, Any]:
        """Reliability data: predicted risk vs. how often reviewers confirmed it was really bad."""
        with self._lock:
            rows = self._conn.execute("SELECT risk, review_label FROM events WHERE review_label IS NOT NULL").fetchall()
        buckets = [{"lo": i / bins, "hi": (i + 1) / bins, "n": 0, "positive": 0} for i in range(bins)]
        for risk, label in rows:
            b = buckets[min(int((risk or 0) * bins), bins - 1)]
            b["n"] += 1
            b["positive"] += int(label in ("true_positive", "false_negative"))
        for b in buckets:
            b["observed"] = b["positive"] / b["n"] if b["n"] else None
        n = sum(b["n"] for b in buckets)
        ece = sum(b["n"] / n * abs(b["observed"] - (b["lo"] + b["hi"]) / 2) for b in buckets if b["n"]) if n else None
        return {"bins": buckets, "reviewed": n, "ece": ece}

    # --- approvals ----------------------------------------------------------------------------------
    def create_approval(self, data: dict[str, Any]) -> str:
        approval_id = data.get("id") or uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO approvals (id, ts, status, stage, tool_name, session_id, agent, summary, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (approval_id, time.time(), "pending", data.get("stage"), data.get("tool_name"), data.get("session_id"),
                 data.get("agent"), data.get("summary"), json.dumps(data, default=str)))
        return approval_id

    def decide_approval(self, approval_id: str, approved: bool, by: str = "dashboard", note: str | None = None) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, note = ? WHERE id = ? AND status = 'pending'",
                ("approved" if approved else "rejected", time.time(), by, note, approval_id))
        return cur.rowcount > 0

    def expire_approval(self, approval_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE approvals SET status = 'expired', decided_at = ? WHERE id = ? AND status = 'pending'",
                               (time.time(), approval_id))

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return self._approval(row) if row else None

    def list_approvals(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM approvals", []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY ts DESC LIMIT ?", [*args, limit]).fetchall()
        return [self._approval(r) for r in rows]

    @staticmethod
    def _approval(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["payload"] = json.loads(d["payload"] or "{}")
        return d

    # --- eval runs & key/value ----------------------------------------------------------------------------
    def add_eval_run(self, dataset: str, metrics: dict[str, Any], cases: list[dict[str, Any]]) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute("INSERT INTO eval_runs VALUES (?,?,?,?,?)",
                               (run_id, time.time(), dataset, json.dumps(metrics), json.dumps(cases, default=str)))
        return run_id

    def list_eval_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT id, ts, dataset, metrics FROM eval_runs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "ts": r[1], "dataset": r[2], "metrics": json.loads(r[3])} for r in rows]

    def get_eval_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "ts": r[1], "dataset": r[2], "metrics": json.loads(r[3]), "cases": json.loads(r[4])}

    def get_kv(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, value, time.time()))

    def clear_events(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events")
