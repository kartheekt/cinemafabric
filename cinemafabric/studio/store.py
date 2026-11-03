"""Job persistence. Each job's full state is a JSON document; the UI polls it."""

import json
import time
from pathlib import Path

from ..db import open_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    state TEXT NOT NULL
);
"""


class JobStore:
    def __init__(self, path: Path):
        self.db = open_db(path, SCHEMA)

    def save(self, state: dict) -> None:
        state["updated"] = time.time()
        self.db.upsert("jobs", {"id": state["id"], "created": state["created"], "updated": state["updated"],
                                "status": state["status"], "title": (state.get("story") or {}).get("title"),
                                "state": json.dumps(state)}, "id")

    def get(self, job_id: str) -> dict | None:
        rows = self.db.query("SELECT state FROM jobs WHERE id = ?", (job_id,))
        return json.loads(rows[0]["state"]) if rows else None

    def delete(self, job_id: str) -> None:
        self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def owner(self, job_id: str) -> str | None:
        rows = self.db.query(f"SELECT {self.db.json_text('state', 'owner')} AS owner FROM jobs WHERE id = ?", (job_id,))
        return rows[0]["owner"] if rows else None

    def is_shared(self, job_id: str) -> bool:
        return bool(self.db.query(f"SELECT 1 FROM jobs WHERE id = ? AND {self.db.json_text('state', 'share.id')} IS NOT NULL",
                                  (job_id,)))

    def active(self, owner: str) -> int:
        return self.db.query(f"SELECT COUNT(*) AS n FROM jobs WHERE {self.db.json_text('state', 'owner')} = ? "
                             "AND status IN ('queued', 'running', 'review')", (owner,))[0]["n"]

    def spent_since(self, owner: str, since: float) -> float:
        rows = self.db.query(f"SELECT COALESCE(SUM({self.db.json_number('state', 'receipt.cost_usd')}), 0) AS usd FROM jobs "
                             f"WHERE {self.db.json_text('state', 'owner')} = ? AND updated >= ?", (owner, since))
        return float(rows[0]["usd"] or 0.0)

    def list(self, limit: int = 50, owner: str | None = None) -> list[dict]:
        if owner is None:
            rows = self.db.query("SELECT state FROM jobs ORDER BY created DESC LIMIT ?", (limit,))
        else:
            rows = self.db.query(f"SELECT state FROM jobs WHERE {self.db.json_text('state', 'owner')} = ? "
                                 f"ORDER BY created DESC LIMIT ?", (owner, limit))
        out = []
        for row in rows:
            try:
                s = json.loads(row["state"])
                opts = s.get("options") or {}
                job_id, created = s["id"], s["created"]
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            legacy = {"trailer30": "30-second trailer", "movie120": "2-minute movie"}.get(opts.get("duration", ""))
            out.append({
                "id": job_id, "created": created, "status": s.get("status", "failed"),
                "title": (s.get("story") or {}).get("title"), "prompt": opts.get("prompt", ""),
                "runtime_label": (s.get("runtime") or {}).get("label") or legacy or opts.get("format", ""),
                "poster": s.get("assets", {}).get("poster"),
                "cost_usd": (s.get("receipt") or {}).get("cost_usd"),
            })
        return out

    def find_shared(self, share_id: str) -> dict | None:
        rows = self.db.query(f"SELECT state FROM jobs WHERE {self.db.json_text('state', 'share.id')} = ?", (share_id,))
        return json.loads(rows[0]["state"]) if rows else None

    def mark_interrupted(self) -> None:
        """Jobs that were running when the studio stopped can be retried from cache."""
        for row in self.db.query("SELECT state FROM jobs WHERE status IN ('queued', 'running', 'review')"):
            s = json.loads(row["state"])
            s["status"] = "failed"
            s["error"] = "studio restarted while this job was running; press Retry to resume from cache"
            for stage in s.get("stages", []):
                if stage.get("status") == "running":
                    stage["status"], stage["detail"], stage["finished"] = "failed", "interrupted", time.time()
            self.save(s)
