import asyncio

import pytest

from cinemafabric.fabric.backends.base import Backend, BackendError, ExecResult, Offer
from cinemafabric.fabric.cache import ResultCache
from cinemafabric.fabric.ledger import BudgetExceeded, Ledger
from cinemafabric.fabric.registry import Registry
from cinemafabric.fabric.router import NoRouteError, Router
from cinemafabric.fabric.types import Preference, TaskKind, TaskRequest, Tier


class FakeBackend(Backend):
    def __init__(self, id, offers, is_cloud=False, fail=False, healthy=True):
        self.id = id
        self.label = id
        self.location = "cloud" if is_cloud else "pc"
        self.device = "test"
        self.is_cloud = is_cloud
        self.max_concurrency = 1
        self.offers = offers
        self.fail = fail
        self.healthy = healthy
        self.calls = 0

    async def probe(self):
        return self.healthy, "ok" if self.healthy else "down"

    async def execute(self, offer, payload):
        self.calls += 1
        if self.fail:
            raise BackendError("boom")
        return ExecResult({"text": f"from {self.id}"}, offer.estimate(payload))


def text_offer(model, quality, cost=0.0, typical_s=10.0, vision=False):
    return Offer(TaskKind.TEXT, model, quality, vision=vision, estimate=lambda p: cost, typical_s=typical_s)


async def make_router(tmp_path, backends, hard_stop: float | None = 140.0):
    registry = Registry(backends)
    await registry.probe_all()
    ledger = Ledger(tmp_path / "f.db", 150.0, hard_stop, simulated=False)
    return Router(registry, ledger, ResultCache(tmp_path / "cache")), ledger, registry


def req(**kw):
    base = {"kind": TaskKind.TEXT, "payload": {"prompt": "hi"}, "cache": False}
    base.update(kw)
    return TaskRequest(**base)


async def test_local_first_prefers_free_local(tmp_path):
    local = FakeBackend("mac", [text_offer("glimmer", 2)])
    cloud = FakeBackend("cloud", [text_offer("spark", 2, cost=0.01, typical_s=5)], is_cloud=True)
    router, ledger, _ = await make_router(tmp_path, [local, cloud])
    result = await router.run(req())
    assert result.decision.worker_id == "mac"
    assert result.cost_usd == 0
    assert result.cloud_equivalent_usd == pytest.approx(0.01)


async def test_tier_filters_low_quality_local(tmp_path):
    small = FakeBackend("llama", [text_offer("llama8b", 1)])
    cloud = FakeBackend("cloud", [text_offer("spark", 2, cost=0.01)], is_cloud=True)
    router, _, _ = await make_router(tmp_path, [small, cloud])
    result = await router.run(req(tier=Tier.STANDARD))
    assert result.decision.worker_id == "cloud"
    assert "below standard tier" in result.decision.reason


async def test_degrades_when_nothing_meets_tier(tmp_path):
    small = FakeBackend("llama", [text_offer("llama8b", 1)])
    router, _, _ = await make_router(tmp_path, [small])
    result = await router.run(req(tier=Tier.PREMIUM))
    assert result.decision.worker_id == "llama"
    assert "degraded" in result.decision.reason


async def test_failover_to_next_candidate(tmp_path):
    broken = FakeBackend("mac", [text_offer("glimmer", 2)], fail=True)
    cloud = FakeBackend("cloud", [text_offer("spark", 2, cost=0.01)], is_cloud=True)
    router, ledger, registry = await make_router(tmp_path, [broken, cloud])
    result = await router.run(req())
    assert result.decision.worker_id == "cloud"
    assert result.decision.fallbacks == ["mac"]
    assert ledger.spent_usd == pytest.approx(0.01)
    await router.run(req())
    assert registry.available("mac")[0] is False
    await router.run(req())
    assert broken.calls == 2


async def test_cloud_first(tmp_path):
    local = FakeBackend("mac", [text_offer("glimmer", 2)])
    cloud = FakeBackend("cloud", [text_offer("spark", 2, cost=0.01)], is_cloud=True)
    router, _, _ = await make_router(tmp_path, [local, cloud])
    result = await router.run(req(preference=Preference.CLOUD_FIRST))
    assert result.decision.worker_id == "cloud"


async def test_vision_requires_vision_offer(tmp_path):
    blind = FakeBackend("llama", [text_offer("llama8b", 2)])
    seeing = FakeBackend("mac", [text_offer("glimmer", 2, vision=True, typical_s=60)])
    router, _, _ = await make_router(tmp_path, [blind, seeing])
    result = await router.run(req(payload={"prompt": "look", "images": ["AAAA"]}))
    assert result.decision.worker_id == "mac"


async def test_budget_hard_stop_blocks_cloud(tmp_path):
    cloud = FakeBackend("cloud", [text_offer("spark", 2, cost=1.0)], is_cloud=True)
    router, ledger, _ = await make_router(tmp_path, [cloud], hard_stop=1.5)
    await router.run(req())
    with pytest.raises(NoRouteError, match="spending limit"):
        await router.run(req())
    assert ledger.spent_usd == pytest.approx(1.0)


async def test_per_run_limit_only_stops_that_run(tmp_path):
    cloud = FakeBackend("cloud", [text_offer("spark", 2, cost=0.4)], is_cloud=True)
    router, _, _ = await make_router(tmp_path, [cloud], hard_stop=None)
    await router.run(req(job_id="a", max_job_usd=0.5))
    with pytest.raises(NoRouteError, match="this run's spending limit"):
        await router.run(req(job_id="a", max_job_usd=0.5))
    await router.run(req(job_id="b", max_job_usd=0.5))


async def test_limit_set_from_ui_survives_restart(tmp_path):
    ledger = Ledger(tmp_path / "f.db", None, None, simulated=False)
    ledger.set_limit(25.0)
    assert Ledger(tmp_path / "f.db", None, None, simulated=False).budget_usd == 25.0
    ledger.set_limit(None)
    assert Ledger(tmp_path / "f.db", 150.0, None, simulated=False).budget_usd is None


async def test_no_limit_by_default(tmp_path):
    ledger = Ledger(tmp_path / "f.db", None, None, simulated=False)
    await ledger.reserve("big", 10_000.0)
    assert ledger.can_afford(1e9)
    assert ledger.summary()["budget_usd"] is None and ledger.summary()["remaining_usd"] is None


async def test_reservations_prevent_concurrent_overspend(tmp_path):
    ledger = Ledger(tmp_path / "f.db", 150.0, 1.0, simulated=False)
    await ledger.reserve("a", 0.6)
    with pytest.raises(BudgetExceeded):
        await ledger.reserve("b", 0.6)
    ledger.release("a")
    await ledger.reserve("b", 0.6)


async def test_cache_replays_for_free(tmp_path):
    cloud = FakeBackend("cloud", [text_offer("spark", 2, cost=0.01)], is_cloud=True)
    router, ledger, _ = await make_router(tmp_path, [cloud])
    first = await router.run(req(cache=True))
    second = await router.run(req(cache=True))
    assert cloud.calls == 1
    assert second.cached and second.cost_usd == 0
    assert second.output == first.output
    assert ledger.summary()["cache_hits"] == 1


async def test_spend_persists_across_restarts(tmp_path):
    cloud = FakeBackend("cloud", [text_offer("spark", 2, cost=0.25)], is_cloud=True)
    router, _, _ = await make_router(tmp_path, [cloud])
    await router.run(req())
    reopened = Ledger(tmp_path / "f.db", 150.0, 140.0, simulated=False)
    assert reopened.spent_usd == pytest.approx(0.25)
    assert Ledger(tmp_path / "f.db", 150.0, 140.0, simulated=True).spent_usd == 0


async def test_queue_depth_shifts_balanced_choice(tmp_path):
    local = FakeBackend("mac", [text_offer("glimmer", 2, typical_s=30)])
    cloud = FakeBackend("cloud", [text_offer("spark", 2, cost=0.001, typical_s=20)], is_cloud=True)
    router, _, registry = await make_router(tmp_path, [local, cloud])
    viable, _ = router.rank(req(preference=Preference.BALANCED), 2)
    assert viable[0].backend.id == "mac"
    registry.state["mac"].inflight = 3
    viable, _ = router.rank(req(preference=Preference.BALANCED), 2)
    assert viable[0].backend.id == "cloud"
    await asyncio.sleep(0)


def test_usage_measures_the_first_session_only():
    from cinemafabric.tools.evaluate import usage

    def task(end, seconds, cloud=False, worker="gpu0"):
        return {"ts": end, "latency_ms": seconds * 1000, "status": "ok", "cached": False, "worker_id": worker,
                "is_cloud": cloud, "worker_label": worker, "model": "muse-image-1.0" if cloud else None}

    rows = [task(10, 10), task(15, 10, worker="gpu1"), task(18, 10, cloud=True), task(40, 10),
            task(900, 5)]  # a redo long after the film finished
    u = usage(rows)
    assert u["span"] == 40 and u["peak"] == 3
    assert u["local_busy"] == 25 / 40 and u["local_rows"] == 2 and u["cloud_rows"] == 1
    assert usage([])["span"] == 0


async def test_one_failed_probe_does_not_take_a_worker_down():
    backend = FakeBackend("gpu", [text_offer("m", 2)])
    registry = Registry([backend])
    await registry.probe_all()
    assert registry.available("gpu") == (True, "")
    backend.healthy = False
    await registry.probe_all()
    assert registry.available("gpu") == (True, "")
    await registry.probe_all()
    assert registry.available("gpu") == (False, "down")
    backend.healthy = True
    await registry.probe_all()
    assert registry.available("gpu") == (True, "")
    backend.healthy = False
    await registry.probe_all()
    backend.healthy = True
    await registry.probe_all()
    backend.healthy = False
    await registry.probe_all()
    assert registry.available("gpu") == (True, "")


async def test_a_worker_that_never_answered_is_not_given_the_benefit_of_the_doubt():
    backend = FakeBackend("gpu", [text_offer("m", 2)], healthy=False)
    registry = Registry([backend])
    await registry.probe_all()
    assert registry.available("gpu") == (False, "down")


async def test_the_stronger_gpu_goes_first_and_the_other_helps_when_it_is_busy(tmp_path):
    weak, strong = FakeBackend("weak", [text_offer("m", 2)]), FakeBackend("strong", [text_offer("m", 2)])
    weak.memory, strong.memory = {"total_gb": 8.0}, {"total_gb": 12.0}
    router, _, registry = await make_router(tmp_path, [weak, strong])
    assert (await router.run(req())).decision.worker_id == "strong"
    registry.state["strong"].latency.clear()
    registry.state["strong"].inflight = 1
    assert (await router.run(req())).decision.worker_id == "weak"


async def test_a_stall_on_this_computer_does_not_take_every_worker_down():
    class Stuck(FakeBackend):
        async def probe(self):
            raise TimeoutError

    a, b = Stuck("a", [text_offer("m", 2)]), Stuck("b", [text_offer("m", 2)])
    registry = Registry([a, b])
    registry.state["a"].status = registry.state["b"].status = "up"
    for _ in range(3):
        await registry.probe_all()
    assert registry.available("a") == (True, "") and registry.available("b") == (True, "")


async def test_one_worker_timing_out_alone_still_goes_down():
    class Stuck(FakeBackend):
        async def probe(self):
            raise TimeoutError

    bad, good = Stuck("bad", [text_offer("m", 2)]), FakeBackend("good", [text_offer("m", 2)])
    registry = Registry([bad, good])
    registry.state["bad"].status = "up"
    for _ in range(2):
        await registry.probe_all()
    assert registry.available("bad")[0] is False and registry.available("good") == (True, "")


async def test_a_dropped_connection_is_retried_on_a_read():
    import httpx

    from cinemafabric.fabric.client import FabricClient

    calls = []

    def respond(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ReadError("dropped")
        return httpx.Response(200, json={"ok": True})

    client = FabricClient("http://fabric", "token")
    client._client = httpx.AsyncClient(base_url="http://fabric", transport=httpx.MockTransport(respond))
    assert await client.get("/v1/ledger") == {"ok": True} and len(calls) == 2
    await client.aclose()
