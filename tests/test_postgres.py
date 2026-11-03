"""The stores on Postgres. Runs only when CF_TEST_DATABASE_URL points at a disposable database."""

import os
import time

import pytest

from cinemafabric.config import get_settings

URL = os.environ.get("CF_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="set CF_TEST_DATABASE_URL to run the Postgres tests")


@pytest.fixture
def pg(monkeypatch, tmp_path):
    import psycopg

    with psycopg.connect(URL, autocommit=True) as conn:
        for table in ("tasks", "prefs", "machines", "jobs", "characters", "users", "sessions"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    monkeypatch.setenv("CF_DATABASE_URL", URL)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_ledger_and_metrics(pg):
    from cinemafabric.db import PgDatabase
    from cinemafabric.fabric import metrics
    from cinemafabric.fabric.ledger import Ledger
    from cinemafabric.fabric.registry import Registry
    from cinemafabric.fabric.types import RouteDecision, TaskKind, TaskRequest

    ledger = Ledger(pg / "f.db", None, None, simulated=False)
    assert isinstance(ledger.db, PgDatabase) and "licence" in ledger.db.columns("tasks")
    req = TaskRequest(kind=TaskKind.TEXT, payload={}, job_id="j1", stage="story")
    decision = RouteDecision(worker_id="cloud", worker_label="Cloud", model="m", is_cloud=True, reason="r", licence="x")
    await ledger.reserve("t1", 0.02, "j1", None)
    ledger.record("t1", req, decision, status="ok", cost_usd=0.015, latency_ms=1200)
    ledger.record("t1", req, decision, status="ok", cost_usd=0.015, latency_ms=1200)
    assert ledger.job_summary("j1")["tasks"] == 1
    assert ledger.decisions("j1")[0]["licence"] == "x"
    ledger.set_limit(5.0)
    assert Ledger(pg / "f.db", None, None, simulated=False).budget_usd == 5.0
    assert 'cinemafabric_spend_usd_total{model="m"} 0.015' in metrics.render(Registry([]), ledger)


def test_jobs_library_machines_and_accounts(pg):
    from cinemafabric.fabric.machines import MachineSpec, MachineStore
    from cinemafabric.studio.accounts import Accounts
    from cinemafabric.studio.library import CharacterLibrary
    from cinemafabric.studio.options import JobOptions
    from cinemafabric.studio.pipeline import new_state
    from cinemafabric.studio.store import JobStore

    machines = MachineStore(pg / "f.db")
    machines.save(MachineSpec(id="w", label="W", kind="gpu-worker", base_url="http://127.0.0.1:1", caps=["tts"]))
    machines.save(MachineSpec(id="w", label="W2", kind="gpu-worker", base_url="http://127.0.0.1:1", caps=["tts"]))
    assert [m.label for m in machines.all()] == ["W2"] and machines.delete("w")

    store = JobStore(pg / "s.db")
    state = new_state("abcdef0123", JobOptions(prompt="A lighthouse keeper hears the sea answer"))
    state.update(owner="u1", status="running", receipt={"cost_usd": 0.25}, share={"id": "s1"})
    store.save(state)
    store.save(state)
    assert store.get("abcdef0123")["owner"] == "u1" and store.owner("abcdef0123") == "u1"
    assert store.is_shared("abcdef0123") and store.find_shared("s1")["id"] == "abcdef0123"
    assert store.active("u1") == 1 and store.spent_since("u1", time.time() - 60) == pytest.approx(0.25)
    assert [j["id"] for j in store.list(owner="u1")] == ["abcdef0123"] and store.list(owner="u2") == []
    store.mark_interrupted()
    assert store.get("abcdef0123")["status"] == "failed"

    sheet = pg / "sheet.png"
    sheet.write_bytes(b"png")
    library = CharacterLibrary(pg / "s.db", pg / "library")
    saved = library.save("Meera", "red ribbon", sheet, "abcdef0123", owner="u1")
    assert library.pick([saved["id"]], "u1")[0]["name"] == "Meera" and library.all("u2") == []

    accounts = Accounts(pg / "s.db")
    accounts.create("ada@example.com", "Ada", "correct horse 1", is_admin=True)
    token = accounts.sign_in("ada@example.com", "correct horse 1")
    assert token and accounts.user_for(token)["is_admin"] and accounts.sign_in("ada@example.com", "nope") is None
