"""Spend ledger and routing log."""

import asyncio
import json
import time
from pathlib import Path

from ..db import open_db
from .types import RouteDecision, TaskRequest

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    job_id TEXT,
    stage TEXT,
    label TEXT,
    kind TEXT NOT NULL,
    worker_id TEXT,
    worker_label TEXT,
    model TEXT,
    is_cloud INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    cloud_equiv_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    simulated INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    fallbacks TEXT,
    error TEXT,
    licence TEXT
);
CREATE INDEX IF NOT EXISTS tasks_job ON tasks(job_id);
CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT);
"""


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: Path, budget_usd: float | None, hard_stop_usd: float | None, simulated: bool):
        self.db = open_db(path, SCHEMA)
        if "licence" not in self.db.columns("tasks"):
            self.db.execute("ALTER TABLE tasks ADD COLUMN licence TEXT")
        saved = self.db.query("SELECT value FROM prefs WHERE key = 'limit'")
        if saved:
            budget_usd, hard_stop_usd = json.loads(saved[0]["value"]), None
        self._apply_limit(budget_usd, hard_stop_usd)
        self.simulated = simulated
        self._reserved: dict[str, tuple[float, str | None]] = {}
        self._job_spent: dict[str, float] = {}
        self._lock = asyncio.Lock()
        rows = self.db.query("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM tasks WHERE simulated = ?", (int(simulated),))
        self.spent_usd = float(rows[0]["s"])

    def _apply_limit(self, budget_usd: float | None, hard_stop_usd: float | None) -> None:
        """The enforced limit is the lower of the two when both are set, or whichever one is."""
        limits = [x for x in (budget_usd, hard_stop_usd) if x]
        self.budget_usd = budget_usd or hard_stop_usd or None
        self.hard_stop_usd = min(limits) if limits else None

    def get_pref(self, key: str) -> str | None:
        rows = self.db.query("SELECT value FROM prefs WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_pref(self, key: str, value: str | None) -> None:
        if value is None:
            self.db.execute("DELETE FROM prefs WHERE key = ?", (key,))
        else:
            self.db.upsert("prefs", {"key": key, "value": value}, "key")

    def set_limit(self, budget_usd: float | None) -> None:
        """Change the overall limit at runtime (None removes it) and remember it across restarts."""
        self._apply_limit(budget_usd, None)
        self.db.upsert("prefs", {"key": "limit", "value": json.dumps(self.budget_usd)}, "key")

    @property
    def reserved_usd(self) -> float:
        return sum(amount for amount, _ in self._reserved.values())

    def job_spent(self, job_id: str) -> float:
        if job_id not in self._job_spent:
            rows = self.db.query("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM tasks WHERE job_id = ? AND simulated = ?",
                                 (job_id, int(self.simulated)))
            self._job_spent[job_id] = float(rows[0]["s"])
        return self._job_spent[job_id]

    def limit_reason(self, est_usd: float, job_id: str | None = None, job_limit: float | None = None) -> str | None:
        """Why a cloud call of est_usd can't go ahead, or None if it can."""
        if self.hard_stop_usd is not None and self.spent_usd + self.reserved_usd + est_usd > self.hard_stop_usd:
            return "spending limit reached"
        if job_id and job_limit:
            in_flight = sum(a for a, j in self._reserved.values() if j == job_id)
            if self.job_spent(job_id) + in_flight + est_usd > job_limit:
                return "this run's spending limit reached"
        return None

    def can_afford(self, est_usd: float, job_id: str | None = None, job_limit: float | None = None) -> bool:
        return self.limit_reason(est_usd, job_id, job_limit) is None

    async def reserve(self, task_id: str, est_usd: float, job_id: str | None = None, job_limit: float | None = None) -> None:
        async with self._lock:
            reason = self.limit_reason(est_usd, job_id, job_limit)
            if reason:
                raise BudgetExceeded(f"{reason}: a ${est_usd:.4f} call would go past it (spent ${self.spent_usd:.4f})")
            amount, _ = self._reserved.get(task_id, (0.0, job_id))
            self._reserved[task_id] = (amount + est_usd, job_id)

    def release(self, task_id: str) -> None:
        self._reserved.pop(task_id, None)

    def record(
        self,
        task_id: str,
        req: TaskRequest,
        decision: RouteDecision | None,
        *,
        status: str,
        cost_usd: float = 0.0,
        cloud_equiv_usd: float = 0.0,
        latency_ms: int = 0,
        cached: bool = False,
        error: str | None = None,
    ) -> None:
        self.release(task_id)
        self.spent_usd += cost_usd
        if req.job_id and cost_usd:
            self._job_spent[req.job_id] = self.job_spent(req.job_id) + cost_usd
        self.db.upsert("tasks", {
            "task_id": task_id,
            "ts": time.time(),
            "job_id": req.job_id,
            "stage": req.stage,
            "label": req.label,
            "kind": req.kind.value,
            "worker_id": decision.worker_id if decision else None,
            "worker_label": decision.worker_label if decision else None,
            "model": decision.model if decision else None,
            "is_cloud": int(decision.is_cloud) if decision else 0,
            "status": status,
            "cost_usd": cost_usd,
            "cloud_equiv_usd": cloud_equiv_usd,
            "latency_ms": latency_ms,
            "cached": int(cached),
            "simulated": int(self.simulated),
            "reason": decision.reason if decision else None,
            "fallbacks": json.dumps(decision.fallbacks) if decision else "[]",
            "error": error,
            "licence": decision.licence if decision else None,
        }, "task_id")

    def _aggregate(self, where: str, params: tuple) -> dict:
        rows = self.db.query(
            f"""SELECT COUNT(*) AS tasks,
                   SUM(CASE WHEN status='ok' AND cached=0 AND is_cloud=0 THEN 1 ELSE 0 END) AS local_tasks,
                   SUM(CASE WHEN status='ok' AND cached=0 AND is_cloud=1 THEN 1 ELSE 0 END) AS cloud_tasks,
                   SUM(CASE WHEN cached=1 THEN 1 ELSE 0 END) AS cache_hits,
                   SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN fallbacks != '[]' THEN 1 ELSE 0 END) AS rerouted,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd,
                   COALESCE(SUM(CASE WHEN is_cloud=0 OR cached=1 THEN cloud_equiv_usd ELSE 0 END), 0) AS avoided_usd
               FROM tasks WHERE {where}""",
            params,
        )
        agg = {k: (v or 0) for k, v in rows[0].items()}
        by_worker = self.db.query(
            f"""SELECT worker_id, worker_label, COUNT(*) AS tasks, COALESCE(SUM(cost_usd),0) AS cost_usd
                FROM tasks WHERE {where} AND status='ok' AND cached=0 GROUP BY worker_id, worker_label ORDER BY tasks DESC""",
            params,
        )
        by_model = self.db.query(
            f"""SELECT model, MAX(is_cloud) AS is_cloud,
                   SUM(CASE WHEN status='ok' AND cached=0 THEN 1 ELSE 0 END) AS tasks,
                   SUM(CASE WHEN status='ok' AND cached=1 THEN 1 ELSE 0 END) AS cached,
                   SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) AS failed,
                   COALESCE(SUM(CASE WHEN status='ok' AND cached=0 THEN cost_usd ELSE 0 END), 0) AS cost_usd,
                   CAST(AVG(CASE WHEN status='ok' AND cached=0 THEN latency_ms END) AS REAL) AS avg_ms
                FROM tasks WHERE {where} AND model IS NOT NULL GROUP BY model ORDER BY cost_usd DESC, tasks DESC""",
            params,
        )
        uses: dict[str, dict[str, set]] = {}
        for row in self.db.query(f"SELECT DISTINCT model, kind, stage FROM tasks WHERE {where} AND status='ok' "
                                 "AND model IS NOT NULL", params):
            u = uses.setdefault(row["model"], {"kinds": set(), "stages": set()})
            u["kinds"].add(row["kind"])
            if row["stage"]:
                u["stages"].add(row["stage"])
        for row in by_model:
            u = uses.get(row["model"], {"kinds": set(), "stages": set()})
            row.update(kinds=sorted(u["kinds"]), stages=sorted(u["stages"]), avg_ms=round(row["avg_ms"] or 0))
        return {**agg, "by_worker": by_worker, "by_model": by_model}

    def summary(self) -> dict:
        agg = self._aggregate("simulated = ?", (int(self.simulated),))
        return {
            "budget_usd": self.budget_usd,
            "hard_stop_usd": self.hard_stop_usd,
            "spent_usd": round(self.spent_usd, 6),
            "reserved_usd": round(self.reserved_usd, 6),
            "remaining_usd": round(self.hard_stop_usd - self.spent_usd, 6) if self.hard_stop_usd else None,
            "simulated": self.simulated,
            **agg,
        }

    def job_summary(self, job_id: str) -> dict:
        return {"job_id": job_id, **self._aggregate("job_id = ? AND simulated = ?", (job_id, int(self.simulated)))}

    def decisions(self, job_id: str | None = None, limit: int = 200) -> list[dict]:
        if job_id:
            rows = self.db.query("SELECT * FROM tasks WHERE job_id = ? ORDER BY ts DESC LIMIT ?", (job_id, limit))
        else:
            rows = self.db.query("SELECT * FROM tasks WHERE simulated = ? ORDER BY ts DESC LIMIT ?", (int(self.simulated), limit))
        for row in rows:
            row["fallbacks"] = json.loads(row["fallbacks"] or "[]")
            row["is_cloud"] = bool(row["is_cloud"])
            row["cached"] = bool(row["cached"])
        return rows
