"""Prometheus metrics for the fabric: tasks, latency, spend, cache use and worker health."""

from collections import defaultdict

from .ledger import Ledger
from .registry import Registry

BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)


def _label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Out:
    def __init__(self):
        self.lines: list[str] = []

    def family(self, name: str, kind: str, help_: str) -> None:
        self.lines += [f"# HELP cinemafabric_{name} {help_}", f"# TYPE cinemafabric_{name} {kind}"]

    def sample(self, name: str, value: float, **labels) -> None:
        inner = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
        self.lines.append(f"cinemafabric_{name}{{{inner}}} {value}" if inner else f"cinemafabric_{name} {value}")


def render(registry: Registry, ledger: Ledger) -> str:
    out = _Out()
    mode = (int(ledger.simulated),)

    out.family("tasks_total", "counter", "Tasks the router finished, by kind, worker and outcome.")
    for r in ledger.db.query(
        """SELECT kind, COALESCE(worker_id, 'none') AS worker, status, is_cloud, cached, COUNT(*) AS n
           FROM tasks WHERE simulated = ? GROUP BY kind, worker, status, is_cloud, cached""", mode):
        out.sample("tasks_total", r["n"], kind=r["kind"], worker=r["worker"], status=r["status"],
                   where="none" if r["worker"] == "none" else "cache" if r["cached"] else "cloud" if r["is_cloud"] else "local")

    out.family("spend_usd_total", "counter", "Estimated cloud spend from list prices, by model.")
    for r in ledger.db.query(
        "SELECT COALESCE(model, 'none') AS model, SUM(cost_usd) AS usd FROM tasks WHERE simulated = ? GROUP BY model", mode):
        out.sample("spend_usd_total", round(r["usd"] or 0.0, 6), model=r["model"])

    out.family("avoided_usd_total", "counter", "What local and cached tasks would have cost in the cloud.")
    avoided = ledger.db.query(
        """SELECT COALESCE(SUM(cloud_equiv_usd), 0) AS usd FROM tasks
           WHERE simulated = ? AND status = 'ok' AND (is_cloud = 0 OR cached = 1)""", mode)[0]["usd"]
    out.sample("avoided_usd_total", round(avoided, 6))

    out.family("task_seconds", "histogram", "Time a worker spent on a task, by kind.")
    counts: dict[str, list[int]] = defaultdict(lambda: [0] * len(BUCKETS))
    sums: dict[str, float] = defaultdict(float)
    totals: dict[str, int] = defaultdict(int)
    for r in ledger.db.query(
        """SELECT kind, latency_ms FROM tasks WHERE simulated = ? AND status = 'ok' AND cached = 0
           ORDER BY ts DESC LIMIT 50000""", mode):
        seconds = r["latency_ms"] / 1000
        sums[r["kind"]] += seconds
        totals[r["kind"]] += 1
        for i, edge in enumerate(BUCKETS):
            if seconds <= edge:
                counts[r["kind"]][i] += 1
    for kind, row in counts.items():
        for edge, n in zip(BUCKETS, row, strict=False):
            out.sample("task_seconds_bucket", n, kind=kind, le=edge)
        out.sample("task_seconds_bucket", totals[kind], kind=kind, le="+Inf")
        out.sample("task_seconds_sum", round(sums[kind], 3), kind=kind)
        out.sample("task_seconds_count", totals[kind], kind=kind)

    out.family("worker_up", "gauge", "1 if the worker is healthy and enabled.")
    out.family("worker_inflight", "gauge", "Tasks running on the worker now.")
    out.family("worker_circuit_open", "gauge", "1 while repeated failures keep the worker out of the pool.")
    out.family("worker_memory_used_bytes", "gauge", "Device memory in use on a GPU worker.")
    out.family("worker_memory_total_bytes", "gauge", "Device memory on a GPU worker.")
    for b in registry.backends:
        st = registry.state.get(b.id)
        if st is None:
            continue
        labels = {"worker": b.id, "where": "cloud" if b.is_cloud else "local"}
        out.sample("worker_up", int(st.status == "up" and st.enabled), **labels)
        out.sample("worker_inflight", st.inflight, **labels)
        out.sample("worker_circuit_open", int(st.status == "down" and "circuit" in st.detail), **labels)
        memory = getattr(b, "memory", None)
        if memory:
            out.sample("worker_memory_used_bytes", int(memory["used_gb"] * 2**30), **labels)
            out.sample("worker_memory_total_bytes", int(memory["total_gb"] * 2**30), **labels)

    out.family("spent_usd", "gauge", "Estimated cloud spend so far.")
    out.sample("spent_usd", round(ledger.spent_usd, 6))
    out.family("reserved_usd", "gauge", "Cloud spend reserved by tasks still running.")
    out.sample("reserved_usd", round(ledger.reserved_usd, 6))
    if ledger.hard_stop_usd:
        out.family("limit_usd", "gauge", "The spending limit in force.")
        out.sample("limit_usd", ledger.hard_stop_usd)
    return "\n".join(out.lines) + "\n"
