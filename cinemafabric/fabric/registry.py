"""Worker registry: health probes, live load, latency tracking and a simple circuit breaker."""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from .backends.base import Backend, Offer

log = logging.getLogger("cinemafabric.registry")

CIRCUIT_FAILURES = 2
CIRCUIT_COOLDOWN_S = 30.0
EMA_ALPHA = 0.3
# A worker is marked down only after this many failed probes in a row, so one stall can't take every worker out at once.
PROBE_FAILURES = 2


@dataclass
class WorkerState:
    semaphore: asyncio.Semaphore
    status: str = "unknown"
    detail: str = ""
    enabled: bool = True
    inflight: int = 0
    completed: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    probe_failures: int = 0
    circuit_open_until: float = 0.0
    last_error: str | None = None
    last_probe: float = 0.0
    latency: dict[str, float] = field(default_factory=dict)


class Registry:
    def __init__(self, backends: list[Backend], interval_s: float = 5.0):
        self.backends = backends
        self.by_id = {b.id: b for b in backends}
        self.state = {b.id: WorkerState(asyncio.Semaphore(b.max_concurrency)) for b in backends}
        self.interval_s = interval_s
        self._task: asyncio.Task | None = None

    async def add(self, backend: Backend) -> None:
        """Add (or replace) a machine at runtime and probe it straight away."""
        if backend.id in self.by_id:
            await self.remove(backend.id)
        builtin_at = next((i for i, b in enumerate(self.backends) if b.source == "builtin"), len(self.backends))
        self.backends.insert(builtin_at, backend)
        self.by_id[backend.id] = backend
        self.state[backend.id] = WorkerState(asyncio.Semaphore(backend.max_concurrency))
        await self.probe_all()

    async def remove(self, worker_id: str) -> None:
        backend = self.by_id.pop(worker_id, None)
        if backend is None:
            return
        self.backends.remove(backend)
        self.state.pop(worker_id, None)
        await backend.aclose()

    async def start(self) -> None:
        await self.probe_all()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for b in self.backends:
            await b.aclose()

    async def _loop(self) -> None:
        while True:
            began = time.monotonic()
            await asyncio.sleep(self.interval_s)
            late = time.monotonic() - began - self.interval_s
            if late > 3:
                log.warning("health checks ran %.0f s late: the fabric or this computer stalled", late)
            try:
                await self.probe_all()
            except Exception:
                log.exception("health probe failed")

    async def probe_all(self) -> None:
        backends = list(self.backends)
        results = await asyncio.gather(*(self._probe(b) for b in backends), return_exceptions=True)
        remote = [r for b, r in zip(backends, results, strict=False) if b.source != "builtin" or b.is_cloud]
        if len(remote) > 1 and all(isinstance(r, TimeoutError) for r in remote):
            # Every machine timing out at once says this computer stalled, not that they all failed together.
            log.warning("all %d health checks timed out together; keeping workers as they were", len(remote))
            return
        for b, res in zip(backends, results, strict=False):
            st = self.state.get(b.id)
            if st is None:
                continue
            st.last_probe = time.time()
            if isinstance(res, BaseException):
                healthy, detail = False, f"probe error: {str(res) or type(res).__name__}"
            else:
                healthy, detail = res
            st.probe_failures = 0 if healthy else st.probe_failures + 1
            if not healthy:
                log.warning("probe of %s failed (%d in a row): %s", b.id, st.probe_failures, detail)
            if not st.enabled:
                st.status, st.detail = "disabled", "disabled by operator"
            elif time.time() < st.circuit_open_until:
                st.status, st.detail = "down", f"circuit open after failures: {st.last_error}"
            elif healthy or st.status != "up" or st.probe_failures >= PROBE_FAILURES:
                st.status, st.detail = ("up" if healthy else "down"), detail

    async def _probe(self, backend: Backend) -> tuple[bool, str]:
        return await asyncio.wait_for(backend.probe(), timeout=8.0)

    def available(self, worker_id: str) -> tuple[bool, str]:
        st = self.state.get(worker_id)
        if st is None:
            return False, "removed"
        if not st.enabled:
            return False, "disabled"
        if time.time() < st.circuit_open_until:
            return False, "circuit open"
        if st.status != "up":
            return False, st.detail or st.status
        return True, ""

    def expected_seconds(self, backend: Backend, offer: Offer) -> float:
        st = self.state[backend.id]
        per_task = st.latency.get(offer.kind.value, offer.typical_s)
        queue_wait = st.inflight / backend.max_concurrency * per_task
        return per_task + queue_wait

    def record_success(self, worker_id: str, kind: str, seconds: float) -> None:
        st = self.state.get(worker_id)
        if st is None:
            return
        prev = st.latency.get(kind)
        st.latency[kind] = seconds if prev is None else EMA_ALPHA * seconds + (1 - EMA_ALPHA) * prev
        st.completed += 1
        st.consecutive_failures = 0

    def record_failure(self, worker_id: str, error: str, retryable: bool) -> None:
        st = self.state.get(worker_id)
        if st is None:
            return
        st.failures += 1
        st.last_error = error[:200]
        if not retryable:
            return
        st.consecutive_failures += 1
        if st.consecutive_failures >= CIRCUIT_FAILURES:
            st.circuit_open_until = time.time() + CIRCUIT_COOLDOWN_S
            st.status, st.detail = "down", f"circuit open after failures: {st.last_error}"
            log.warning("circuit opened for %s: %s", worker_id, error)

    def set_enabled(self, worker_id: str, enabled: bool) -> None:
        st = self.state[worker_id]
        st.enabled = enabled
        if enabled:
            st.circuit_open_until = 0.0
            st.consecutive_failures = 0
            st.status, st.detail = "unknown", "re-enabled, probing"
        else:
            st.status, st.detail = "disabled", "disabled by operator"

    def snapshot(self) -> list[dict]:
        out = []
        for b in self.backends:
            st = self.state[b.id]
            out.append(
                {
                    **b.describe(),
                    "status": st.status,
                    "detail": st.detail,
                    "enabled": st.enabled,
                    "inflight": st.inflight,
                    "completed": st.completed,
                    "failures": st.failures,
                    "last_error": st.last_error,
                    "latency_s": {k: round(v, 2) for k, v in st.latency.items()},
                }
            )
        return out
