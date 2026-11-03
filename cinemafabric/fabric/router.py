"""The router: pick the best healthy worker for each task, explain why, and fall back on failure."""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

from pydantic import ValidationError

from .backends.base import Backend, BackendError, Offer
from .cache import ResultCache
from .ledger import BudgetExceeded, Ledger
from .registry import Registry
from .types import Candidate, Preference, RouteDecision, TaskKind, TaskRequest, TaskResult

log = logging.getLogger("cinemafabric.router")

PREF_LABEL = {
    Preference.LOCAL_FIRST: "Local-first",
    Preference.BALANCED: "Balanced",
    Preference.CLOUD_FIRST: "Cloud-first",
}


class NoRouteError(RuntimeError):
    pass


@dataclass
class Option:
    backend: Backend
    offer: Offer
    est_cost: float
    expected_s: float

    @property
    def candidate(self) -> Candidate:
        return Candidate(
            worker_id=self.backend.id,
            model=self.offer.model,
            is_cloud=self.backend.is_cloud,
            est_cost_usd=round(self.est_cost, 6),
            expected_s=round(self.expected_s, 1),
        )


def _needs_vision(req: TaskRequest) -> bool:
    return req.kind == TaskKind.TEXT and bool(req.payload.get("images"))


class Router:
    def __init__(self, registry: Registry, ledger: Ledger, cache: ResultCache, licences: set[str] | None = None):
        self.registry = registry
        self.ledger = ledger
        self.cache = cache
        self.licences = licences


    def _score(self, opt: Option, pref: Preference) -> tuple:
        cloud = opt.backend.is_cloud
        # When all else is equal, the GPU with the most memory goes first, whatever order the workers joined in.
        stronger = -(getattr(opt.backend, "memory", None) or {}).get("total_gb", 0.0)
        if pref == Preference.LOCAL_FIRST:
            return (cloud, opt.est_cost, opt.expected_s, stronger)
        if pref == Preference.CLOUD_FIRST:
            return (not cloud, opt.expected_s, opt.est_cost, stronger)
        return (opt.est_cost * 100 + opt.expected_s * 0.02 + (0.3 if cloud else 0.0), stronger)

    def allowed(self, req: TaskRequest, licence: str) -> bool:
        """The profile's licences, minus any this request rules out (a commercial-safe film, say)."""
        return (self.licences is None or licence in self.licences) and licence not in req.exclude_licences

    def rank(self, req: TaskRequest, min_quality: int) -> tuple[list[Option], list[Candidate]]:
        viable: list[Option] = []
        rejected: list[Candidate] = []
        vision = _needs_vision(req)
        for backend in self.registry.backends:
            for offer in backend.offers:
                if offer.kind != req.kind:
                    continue
                opt = Option(backend, offer, offer.estimate(req.payload) if backend.is_cloud else 0.0,
                             self.registry.expected_seconds(backend, offer))
                why = None
                if vision and not offer.vision:
                    why = "no vision support"
                elif not self.allowed(req, offer.licence):
                    why = f"licence {offer.licence} not allowed"
                elif offer.kind in (getattr(backend, "unavailable", None) or {}):
                    why = backend.unavailable[offer.kind]
                elif offer.quality < min_quality:
                    why = f"below {req.tier.value} tier"
                else:
                    ok, detail = self.registry.available(backend.id)
                    if not ok:
                        why = detail
                    elif backend.is_cloud and (limit := self.ledger.limit_reason(opt.est_cost, req.job_id, req.max_job_usd)):
                        why = limit
                if why:
                    rejected.append(opt.candidate.model_copy(update={"rejected": why}))
                else:
                    viable.append(opt)
        viable.sort(key=lambda o: self._score(o, req.preference))
        return viable, rejected

    def cloud_equivalent(self, req: TaskRequest) -> float:
        """What this task would have cost on the cheapest suitable cloud model."""
        vision = _needs_vision(req)
        prices = [
            o.estimate(req.payload)
            for b in self.registry.backends
            if b.is_cloud
            for o in b.offers
            if o.kind == req.kind and o.quality >= min(req.tier.min_quality, 2) and (o.vision or not vision)
            and self.allowed(req, o.licence)
        ]
        return min(prices) if prices else 0.0

    @staticmethod
    def explain(req: TaskRequest, chosen: Option, viable: list[Option], rejected: list[Candidate], degraded: bool) -> str:
        pref = PREF_LABEL[req.preference]
        b, o = chosen.backend, chosen.offer
        if b.is_cloud:
            local_alts = [c for c in rejected if not c.is_cloud]
            local_viable = [v for v in viable if not v.backend.is_cloud]
            if req.preference == Preference.CLOUD_FIRST:
                head = f"{pref}: sent to {o.model} (~${chosen.est_cost:.4f})"
            elif local_viable:
                alt = local_viable[0]
                head = (f"{pref}: {o.model} expected {chosen.expected_s:.0f}s vs {alt.backend.label} "
                        f"{alt.expected_s:.0f}s, worth ~${chosen.est_cost:.4f}")
            elif local_alts:
                head = f"{pref}: no local worker could take it, so {o.model} (~${chosen.est_cost:.4f})"
            else:
                head = f"Only the cloud offers {req.kind.value}: {o.model} (~${chosen.est_cost:.4f})"
        else:
            wait = chosen.expected_s
            head = f"{pref}: {b.label} is local and free (~{wait:.0f}s expected)"
        notes, seen = [], set()
        for c in rejected:
            if c.worker_id not in seen and c.worker_id != b.id:
                seen.add(c.worker_id)
                notes.append(f"{c.worker_id} skipped ({c.rejected})")
        notes = notes[:3]
        if degraded:
            notes.insert(0, f"degraded: nothing at {req.tier.value} tier was available")
        return "; ".join([head] + notes)


    async def run(self, req: TaskRequest) -> TaskResult:
        task_id = uuid.uuid4().hex[:12]
        cloud_equiv = self.cloud_equivalent(req)
        cache_key = self.cache.key(req) if req.cache else None

        hit = self.cache.get(cache_key) if cache_key else None
        if hit and not self.allowed(req, hit.get("licence", "builtin")):
            hit = None
        if hit:
            try:
                decision = RouteDecision(**{"licence": hit.get("licence"), **hit["decision"]})
                output = dict(hit["output"])
            except (ValidationError, KeyError, TypeError):
                log.warning("ignoring unreadable cache entry %s", cache_key)
                hit = None
        if hit:
            decision.reason = f"Cache hit: replayed result first produced on {decision.worker_label}"
            decision.fallbacks = []
            self.ledger.record(task_id, req, decision, status="ok", cloud_equiv_usd=cloud_equiv, cached=True)
            return TaskResult(task_id=task_id, kind=req.kind, output=output, decision=decision,
                              cost_usd=0.0, cloud_equivalent_usd=cloud_equiv, latency_ms=0, cached=True)

        viable, rejected = self.rank(req, req.tier.min_quality)
        degraded = False
        if not viable:
            viable, rejected = self.rank(req, 1)
            degraded = bool(viable)
        if not viable:
            reasons = ", ".join(f"{c.worker_id}: {c.rejected}" for c in rejected) or "no worker offers this task"
            self.ledger.record(task_id, req, None, status="failed", error=f"no route ({reasons})")
            raise NoRouteError(f"No worker can run {req.kind.value} right now ({reasons})")

        fallbacks: list[str] = []
        last_error = ""
        billed = 0.0
        for opt in viable:
            b, o = opt.backend, opt.offer
            ok, why = self.registry.available(b.id)
            if not ok:
                fallbacks.append(f"{b.id} ({why})")
                last_error = last_error or f"{b.id} became unavailable ({why})"
                continue
            st = self.registry.state[b.id]
            decision = RouteDecision(
                worker_id=b.id,
                worker_label=b.label,
                model=o.model,
                is_cloud=b.is_cloud,
                reason=self.explain(req, opt, viable, rejected, degraded),
                candidates=[v.candidate for v in viable] + rejected,
                fallbacks=list(fallbacks),
                licence=o.licence,
            )
            if fallbacks:
                decision.reason = f"Rerouted after {', '.join(fallbacks)} failed. " + decision.reason
            try:
                if b.is_cloud:
                    await self.ledger.reserve(task_id, opt.est_cost, req.job_id, req.max_job_usd)
            except BudgetExceeded as e:
                fallbacks.append(f"{b.id} (budget)")
                last_error = str(e)
                continue
            st.inflight += 1
            try:
                async with st.semaphore:
                    start = time.perf_counter()
                    result = await b.execute(o, req.payload)
                    elapsed = time.perf_counter() - start
            except (TimeoutError, BackendError) as e:
                retryable = getattr(e, "retryable", True)
                billed += getattr(e, "cost_usd", 0.0)
                self.ledger.release(task_id)
                self.registry.record_failure(b.id, str(e), retryable)
                log.warning("task %s failed on %s: %s", task_id, b.id, e)
                fallbacks.append(b.id)
                last_error = str(e)
                continue
            except asyncio.CancelledError:
                self.ledger.release(task_id)
                raise
            except Exception as e:
                self.ledger.release(task_id)
                self.registry.record_failure(b.id, repr(e), False)
                log.exception("task %s crashed on %s", task_id, b.id)
                fallbacks.append(b.id)
                last_error = repr(e)
                continue
            finally:
                st.inflight -= 1
            self.registry.record_success(b.id, o.kind.value, elapsed)
            latency_ms = int(elapsed * 1000)
            self.ledger.record(task_id, req, decision, status="ok", cost_usd=result.cost_usd + billed,
                               cloud_equiv_usd=cloud_equiv, latency_ms=latency_ms)
            if cache_key:
                try:
                    self.cache.put(cache_key, {"output": result.output, "decision": decision.model_dump(), "licence": o.licence})
                except OSError as e:
                    log.warning("couldn't cache task %s: %s", task_id, e)
            return TaskResult(task_id=task_id, kind=req.kind, output=result.output, decision=decision,
                              cost_usd=result.cost_usd + billed, cloud_equivalent_usd=cloud_equiv, latency_ms=latency_ms)

        self.ledger.record(task_id, req, None, status="failed", cost_usd=billed, error=last_error[:500])
        raise NoRouteError(f"All candidates failed for {req.kind.value}: {last_error}")
