import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from cinemafabric import profile as prof
from cinemafabric.config import get_settings
from cinemafabric.fabric import machines, pricing
from cinemafabric.fabric.cache import ResultCache
from cinemafabric.fabric.ledger import Ledger
from cinemafabric.fabric.registry import Registry
from cinemafabric.fabric.router import Router
from cinemafabric.fabric.types import TaskKind, TaskRequest
from cinemafabric.studio.options import JobOptions
from cinemafabric.studio.pipeline import ContentBlocked, Production, credits_text, new_state, together
from cinemafabric.util import b64e, wav_bytes
from tests.test_fabric import FakeBackend, text_offer


@pytest.fixture
def services(tmp_path, monkeypatch):
    for key, value in {"CF_MODE": "mock", "CF_DATA_DIR": str(tmp_path), "CF_FABRIC_TOKEN": "fabric-secret",
                       "CF_JOIN_TOKEN": "join-secret", "CF_PROFILE": "", "CF_ADMIN_PASSWORD": ""}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    prof.profile.cache_clear()
    yield
    get_settings.cache_clear()
    prof.profile.cache_clear()


def test_fabric_api_needs_the_right_token(services):
    from cinemafabric.fabric.app import app

    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/v1/workers").status_code == 401
        assert c.get("/v1/workers", headers={"X-Join-Token": "join-secret"}).status_code == 401
        assert c.get("/v1/workers", headers={"X-Fabric-Token": "fabric-secret"}).status_code == 200
        assert c.post("/v1/tasks", json={"kind": "text.generate", "payload": {}}).status_code == 401


def test_join_token_only_registers(services):
    from cinemafabric.fabric.app import app

    join = {"X-Join-Token": "join-secret"}
    with TestClient(app) as c:
        assert c.get("/v1/policy", headers=join).status_code == 200
        assert c.get("/v1/machines", headers=join).status_code == 401
        assert c.delete("/v1/machines/x", headers=join).status_code == 401
        assert c.put("/v1/settings/api-key", json={"key": "k"}, headers=join).status_code == 401
        metadata = {"label": "x", "kind": "gpu-worker", "base_url": "http://169.254.169.254", "caps": ["tts"]}
        assert c.post("/v1/machines", json=metadata, headers=join).status_code == 422
        other = {"label": "x", "kind": "ollama", "base_url": "http://127.0.0.1:9", "model": "qwen2:7b",
                 "quality": 1, "vision": False}
        assert c.post("/v1/machines", json=other, headers=join).status_code == 422


def test_machine_listing_hides_worker_secrets():
    spec = machines.MachineSpec(id="w", label="w", kind="gpu-worker", base_url="http://127.0.0.1:1", secret="s")
    assert "secret" not in machines.describe(spec)


def test_studio_admin_routes_need_local_access_or_password(services, monkeypatch):
    from cinemafabric.studio.app import app

    with TestClient(app) as c:
        assert c.get("/api/fabric/join").status_code == 403
        assert c.put("/api/fabric/limit", json={"budget_usd": 1}).status_code == 403
        assert c.get("/api/options").status_code == 200
    monkeypatch.setenv("CF_ADMIN_PASSWORD", "pw")
    get_settings.cache_clear()
    with TestClient(app) as c:
        assert c.get("/api/fabric/join").status_code == 401
        res = c.get("/api/fabric/join", headers={"X-Admin-Password": "pw"})
        assert res.status_code == 200
        assert "--token" not in res.json()["command"]
        cross = {"X-Admin-Password": "pw", "Origin": "https://evil.example"}
        assert c.post("/api/fabric/workers/x/disable", headers=cross).status_code == 403
        assert c.get("/api/fabric/join", headers={"X-Admin-Password": "pw", "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_static_files_stay_inside_the_build(tmp_path):
    from cinemafabric.studio.app import static_file

    root = (tmp_path / "dist").resolve()
    root.mkdir()
    (root / "app.js").write_text("ok")
    (tmp_path / ".env").write_text("SECRET=1")
    assert static_file(root, "app.js") == root / "app.js"
    assert static_file(root, "../.env") is None
    assert static_file(root, "..\\.env") is None
    assert static_file(root, "") is None


async def test_check_target_blocks_metadata_and_credentials():
    assert await machines.check_target("http://127.0.0.1:11434/") == "http://127.0.0.1:11434"
    assert await machines.check_target("http://[fe80::1]:11434") == "http://[fe80::1]:11434"
    for bad in ("http://169.254.169.254", "http://[::ffff:169.254.169.254]:80", "http://0.0.0.0:80", "http://u:p@127.0.0.1",
                "file:///etc/passwd", "http://127.0.0.1/?x=1"):
        with pytest.raises(ValueError):
            await machines.check_target(bad)


def test_family_match_is_by_name_segment():
    assert prof.family_allowed("llama3.1:8b", families=["llama"])
    assert prof.family_allowed("muse-glimmer:30b", families=["glimmer"])
    assert not prof.family_allowed("notllama:7b", families=["llama"])
    assert not prof.family_allowed("llama3:8b", families=[])


def test_reservation_covers_reasoning_headroom():
    _, out = pricing.estimate_text_tokens({"prompt": "x", "max_tokens": 100, "reasoning_effort": "high"})
    assert out == 100 + pricing.REASONING_HEADROOM["high"]


def test_transcribe_cost_comes_from_the_audio_not_the_caller():
    audio = b64e(wav_bytes(np.zeros(16000 * 2, dtype=np.float32), 16000))
    assert pricing.wav_seconds(audio) == pytest.approx(2.0, abs=0.01)


async def test_cache_does_not_replay_a_result_from_a_disallowed_licence(tmp_path):
    local = FakeBackend("local", [text_offer("m", 2)])
    registry = Registry([local])
    await registry.probe_all()
    cache = ResultCache(tmp_path / "cache")
    router = Router(registry, Ledger(tmp_path / "f.db", None, None, simulated=False), cache, {"builtin"})
    req = TaskRequest(kind=TaskKind.TEXT, payload={"prompt": "hi"})
    cache.put(cache.key(req), {"output": {"text": "old"}, "decision": {"worker_id": "gone", "worker_label": "gone",
              "model": "nc-model", "is_cloud": False, "reason": ""}, "licence": "cc-by-nc-4.0"})
    result = await router.run(req)
    assert not result.cached and local.calls == 1


async def test_removed_worker_mid_task_does_not_crash_the_router(tmp_path):
    class Slow(FakeBackend):
        async def execute(self, offer, payload):
            await asyncio.sleep(0.05)
            return await super().execute(offer, payload)

    slow = Slow("slow", [text_offer("m", 2)])
    registry = Registry([slow])
    await registry.probe_all()
    router = Router(registry, Ledger(tmp_path / "f.db", None, None, simulated=False), ResultCache(tmp_path / "c"))
    task = asyncio.create_task(router.run(TaskRequest(kind=TaskKind.TEXT, payload={"prompt": "hi"}, cache=False)))
    await asyncio.sleep(0.01)
    await registry.remove("slow")
    assert (await task).output["text"] == "from slow"


async def test_together_cancels_siblings_on_failure():
    finished = []

    async def boom():
        raise RuntimeError("boom")

    async def slow():
        await asyncio.sleep(5)
        finished.append(True)

    start = time.perf_counter()
    with pytest.raises(RuntimeError, match="boom"):
        await together(boom(), slow())
    assert time.perf_counter() - start < 1 and not finished


class FakeFabric:
    def __init__(self, verdict, review=None):
        self.verdict = verdict
        self.review = review
        self.texts = []
        self.reviews = 0

    async def run(self, kind, payload, **kw):
        if kind == TaskKind.TEXT:
            self.reviews += 1
            return SimpleNamespace(output={"json": self.review or {}}, decision=SimpleNamespace(model="reviewer"))
        self.texts.append(payload["text"])
        return SimpleNamespace(output=self.verdict)


def guarded(tmp_path, verdict):
    fabric = FakeFabric(verdict)
    opts = JobOptions(prompt="A lighthouse keeper hears the ocean answer")
    store = SimpleNamespace(save=lambda state: None)
    return Production(new_state("t1", opts), fabric=fabric, store=store, data_dir=tmp_path), fabric  # type: ignore[arg-type]


async def test_long_text_is_checked_in_full(tmp_path):
    p, fabric = guarded(tmp_path, {"safe": True, "categories": []})
    await p.check_content("word " * 3000, None, "Check")
    assert len(fabric.texts) == 4 and sum(map(len, fabric.texts)) == 15000


async def test_unsafe_without_a_category_is_blocked(tmp_path):
    p, _ = guarded(tmp_path, {"safe": False, "categories": []})
    with pytest.raises(ContentBlocked):
        await p.check_content("something", None, "Check")


async def test_redo_note_is_checked_before_drawing(tmp_path):
    p, fabric = guarded(tmp_path, {"safe": False, "categories": ["S10"]})
    p.state["story"] = {"title": "T", "logline": "l", "tagline": "t", "tone": "t", "setting": "s", "music_mood": "m",
                        "characters": [], "beats": ["a"]}
    p.state["shots"] = [{"visual": "a quiet harbour", "camera": "static", "duration": 3, "narration": "",
                         "characters": [], "title_card": ""}]
    await p.redo_shot(0, visual="a flagged redo note", note="")
    assert p.state["status"] == "done" and p.state["error"] and fabric.texts == ["a flagged redo note"]


def test_credits_list_models_and_required_notices():
    text = credits_text({"script": ["llama3.1:8b"], "score": ["musicgen-small"]},
                        {"llama3.1:8b": "llama", "musicgen-small": "cc-by-nc-4.0"})
    assert "llama3.1:8b (llama)" in text
    assert "Built with Llama" in text
    assert "non-commercial" in text


def test_transcribe_estimate_reads_low_sample_rates():
    audio = b64e(wav_bytes(np.zeros(4000 * 3, dtype=np.float32), 4000))
    assert pricing.wav_seconds(audio) == pytest.approx(3.0, abs=0.01)


def test_fabric_caps_streamed_bodies_and_decision_limits(services, monkeypatch):
    from cinemafabric.fabric import app as fabric_app

    monkeypatch.setattr(fabric_app, "MAX_BODY", 1000)
    auth = {"X-Fabric-Token": "fabric-secret"}
    with TestClient(fabric_app.app) as c:
        chunks = (b"x" * 600 for _ in range(4))
        assert c.post("/v1/tasks", content=chunks, headers=auth).status_code == 413
        assert c.get("/v1/decisions", params={"limit": -1}, headers=auth).status_code == 200


async def test_a_blocked_chunk_still_blocks_when_a_later_check_fails(tmp_path):
    from cinemafabric.fabric.client import FabricError

    class Flaky(FakeFabric):
        async def run(self, kind, payload, **kw):
            if self.texts:
                raise FabricError("guard went away")
            return await super().run(kind, payload, **kw)

    p, _ = guarded(tmp_path, {"safe": False, "categories": ["S10"]})
    p.fabric = Flaky({"safe": False, "categories": ["S10"]})
    with pytest.raises(ContentBlocked):
        await p.check_content("word " * 3000, None, "Check")


async def test_a_second_opinion_can_clear_a_false_flag(tmp_path):
    p, fabric = guarded(tmp_path, {"safe": False, "categories": ["S4"]})
    p.fabric = FakeFabric({"safe": False, "categories": ["S4"]},
                          {"harmful": False, "category": "", "reason": "A grandfather teaching a child to sail is harmless."})
    await p.check_content("A ferry pilot teaches his granddaughter to read the river", None, "Check")
    assert p.fabric.reviews == 1 and p.state["checks"][-1]["verdict"] == "cleared on review"


async def test_a_second_opinion_blocks_instructions_hidden_as_fiction(tmp_path):
    p, _ = guarded(tmp_path, {"safe": False, "categories": ["S1"]})
    p.fabric = FakeFabric({"safe": False, "categories": ["S1"]},
                          {"harmful": True, "category": "S9", "reason": "It asks for real instructions for serious harm."})
    with pytest.raises(ContentBlocked, match="serious harm"):
        await p.check_content("A film framed as fiction that gives real step-by-step instructions for serious harm", None,
                              "Check")


async def test_clean_text_skips_the_second_opinion(tmp_path):
    p, _ = guarded(tmp_path, {"safe": True, "categories": []})
    p.fabric = FakeFabric({"safe": True, "categories": []})
    await p.check_content("A quiet harbour at dawn", None, "Check")
    assert p.fabric.reviews == 0


def test_shared_films_get_a_watch_page_and_private_ones_dont(services, tmp_path):
    from cinemafabric.studio.app import app
    from cinemafabric.studio.options import JobOptions
    from cinemafabric.studio.pipeline import new_state

    with TestClient(app) as c:
        state = new_state("film1", JobOptions(prompt="A ferry pilot reads the river at night"))
        state.update(status="done", story={"title": "<River>", "logline": "l", "tagline": "t"})
        state["assets"]["video"] = "/media/film1/film.mp4"
        app.state.store.save(state)
        shared = c.post("/api/jobs/film1/share", json={"public": True}).json()["share"]["id"]
        page = c.get(f"/watch/{shared}")
        assert page.status_code == 200 and "&lt;River&gt;" in page.text and "<River>" not in page.text
        assert "Made with AI" in page.text and 'name="robots" content="noindex"' in page.text
        assert c.get(f"/embed/{shared}").status_code == 200
        c.post("/api/jobs/film1/share", json={"public": False})
        assert c.get(f"/watch/{shared}").status_code == 404


def test_ollama_gate_needs_the_secret_and_only_passes_inference():
    import http.client
    import json as json_
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cinemafabric.node import ollama_proxy

    class FakeOllama(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json_.dumps({"message": {"content": "ok"}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_DELETE = do_POST

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    gate = ThreadingHTTPServer(("127.0.0.1", 0),
                               ollama_proxy.handler(f"http://127.0.0.1:{upstream.server_port}", "s3cret"))
    for server in (upstream, gate):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(method, path, secret=None):
        conn = http.client.HTTPConnection("127.0.0.1", gate.server_port, timeout=5)
        conn.request(method, path, body=b"{}", headers={"X-Worker-Secret": secret} if secret else {})
        status = conn.getresponse().status
        conn.close()
        return status

    try:
        assert call("POST", "/api/chat") == 401
        assert call("POST", "/api/chat", "wrong") == 401
        assert call("POST", "/api/chat", "s3cret") == 200
        assert call("POST", "/api/pull", "s3cret") == 403
        assert call("DELETE", "/api/delete", "s3cret") == 403
    finally:
        gate.shutdown()
        upstream.shutdown()


def test_accounts_keep_people_and_their_films_apart(services, monkeypatch):
    monkeypatch.setenv("CF_ACCOUNTS", "true")
    monkeypatch.setenv("CF_USER_DAILY_USD", "0.4")
    get_settings.cache_clear()
    from cinemafabric.studio.app import app
    from cinemafabric.studio.options import JobOptions
    from cinemafabric.studio.pipeline import new_state

    with TestClient(app) as admin, TestClient(app) as other:
        assert admin.get("/api/auth/me").json() == {"accounts": True, "user": None, "can_sign_up": True}
        assert admin.get("/api/jobs").status_code == 401
        me = admin.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": "correct horse 1"}).json()
        assert me["user"]["is_admin"] and admin.get("/api/jobs").status_code == 200
        assert other.post("/api/auth/sign-up", json={"email": "bo@example.com", "password": "correct horse 2"}).status_code == 403
        assert admin.post("/api/people", json={"email": "bo@example.com", "password": "correct horse 2"}).status_code == 200
        assert other.post("/api/auth/sign-in", json={"email": "bo@example.com", "password": "nope"}).status_code == 401
        assert other.post("/api/auth/sign-in", json={"email": "bo@example.com", "password": "correct horse 2"}).status_code == 200

        state = new_state("aaaaaaaaaa", JobOptions(prompt="Ada's private film about a lighthouse"))
        state["owner"] = me["user"]["id"]
        app.state.store.save(state)
        assert other.get("/api/jobs/aaaaaaaaaa").status_code == 404
        assert other.get("/media/aaaaaaaaaa/film.mp4").status_code == 404
        assert other.get("/api/jobs").json()["jobs"] == []
        assert other.get("/api/people").status_code == 403
        assert other.post("/api/jobs/aaaaaaaaaa/retry", headers={"Origin": "https://evil.example"}).status_code == 403

        created = other.post("/api/jobs", json={"prompt": "Bo's film about a night ferry", "max_cost_usd": 5}).json()
        assert created["options"]["max_cost_usd"] == 0.4 and created["owner"] != me["user"]["id"]
        assert admin.get(f"/api/jobs/{created['id']}").status_code == 200
    get_settings.cache_clear()


def test_shared_films_expose_only_what_the_watch_page_plays(services, monkeypatch):
    monkeypatch.setenv("CF_ACCOUNTS", "true")
    get_settings.cache_clear()
    from cinemafabric.studio.app import app
    from cinemafabric.studio.options import JobOptions
    from cinemafabric.studio.pipeline import new_state

    with TestClient(app) as owner, TestClient(app) as stranger, TestClient(app) as other:
        me = owner.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": "correct horse 1"}).json()
        owner.post("/api/people", json={"email": "bo@example.com", "password": "correct horse 2"})
        other.post("/api/auth/sign-in", json={"email": "bo@example.com", "password": "correct horse 2"})
        media = next(r.app for r in app.routes if getattr(r, "name", "") == "media")
        folder = Path(media.directory) / "bbbbbbbbbb"
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("film.mp4", "film.zip", "script.md"):
            (folder / name).write_bytes(b"x")
        state = new_state("bbbbbbbbbb", JobOptions(prompt="A shared film about a lighthouse"))
        state.update(owner=me["user"]["id"], share={"id": "s", "created": 0})
        app.state.store.save(state)
        assert stranger.get("/media/bbbbbbbbbb/film.mp4").status_code == 200
        assert stranger.get("/media/bbbbbbbbbb/film.zip").status_code == 401
        assert stranger.get("/media/bbbbbbbbbb/script.md").status_code == 401

        sheet = get_settings().data_dir / "sheet.png"
        sheet.write_bytes(b"png")
        saved = app.state.library.save("Meera", "red ribbon", sheet, "bbbbbbbbbb", me["user"]["id"])
        served = Path(next(r.app for r in app.routes if getattr(r, "name", "") == "library").directory)
        served.mkdir(parents=True, exist_ok=True)
        (served / saved["image"].rsplit("/", 1)[-1]).write_bytes(b"png")
        assert owner.get(saved["image"]).status_code == 200
        assert other.get(saved["image"]).status_code == 404
    get_settings.cache_clear()


def test_story_originality_renames_borrowed_names_everywhere():
    from cinemafabric.studio import originality
    from cinemafabric.studio.schemas import Character, Shot, Treatment

    t = Treatment(title="Famous Film", logline="Captain Borrowed wants to fly.", tagline="t", tone="t", setting="s",
                  music_mood="m", characters=[Character(name="Captain Borrowed", look="tall, Captain Borrowed's scar")],
                  beats=["Captain Borrowed flies"])
    shots = [Shot(visual="Captain Borrowed on a broom", narration="Captain Borrowed never lands.",
                  speaker="Captain Borrowed", characters=["Captain Borrowed"]),
             Shot(visual="sky", narration="A line from a famous song.")]
    changed = originality.apply(t, shots, [
        originality.StoryIssue(where="title", original="Famous Film", replacement="Below the Dream"),
        originality.StoryIssue(where="character", original="Captain Borrowed", replacement="Arjun Rao"),
        originality.StoryIssue(where="line", original="A line from a famous song.", replacement="Am I awake, or still up here?"),
        originality.StoryIssue(where="character", original="Nobody Here", replacement="X"),
    ])
    assert len(changed) == 3 and t.title == "Below the Dream" and t.characters[0].name == "Arjun Rao"
    assert "Borrowed" not in t.logline + t.characters[0].look + t.beats[0] + shots[0].visual + shots[0].narration
    assert shots[0].speaker == "Arjun Rao" and shots[0].characters == ["Arjun Rao"]
    assert shots[1].narration == "Am I awake, or still up here?"


async def test_a_borrowed_idea_is_stopped_with_an_original_version(tmp_path):
    p, _ = guarded(tmp_path, {"safe": True, "categories": []})
    p.fabric = FakeFabric({"safe": True, "categories": []},
                          {"original": False, "problems": ["The hero belongs to an existing franchise."],
                           "rewrite": "A shy courier discovers she can climb any wall in Mumbai."})
    with pytest.raises(ContentBlocked, match="climb any wall"):
        await p.idea_originality()
    assert p.state["originality"]["rewrite"].startswith("A shy courier")
    assert p.state["checks"][-1]["verdict"] == "blocked"


async def test_a_picture_that_keeps_failing_is_redrawn_twice_then_held(tmp_path, monkeypatch):
    from cinemafabric.studio import pipeline

    def moderation(level):
        monkeypatch.setattr(pipeline, "profile", lambda: {"safety": {"moderation": level}})

    moderation("best_effort")
    p, _ = guarded(tmp_path, {"safe": True, "categories": []})
    seen = iter([["a real brand or logo"], ["a real brand or logo"], []])
    draws = []

    async def looks(png, what, stage, label):
        return next(seen)

    async def draw(fix):
        draws.append(fix)
        return b"png"

    p._looks = looks
    _, record = await p._clean_picture(draw, "the poster art", "poster")
    assert record["outcome"] == "redrawn" and len(draws) == 3 and draws[1] and draws[2] != draws[1]
    seen = iter([["an existing character"]] * 3)
    draws.clear()
    _, record = await p._clean_picture(draw, "the poster art", "poster")
    assert record["outcome"] == "still flagged" and len(draws) == 3
    p._record_pictures("Picture check: poster", [record])
    assert p.state["checks"][-1]["verdict"] == "still flagged"
    moderation("required")
    seen = iter([["a real, recognisable person"]] * 3)
    with pytest.raises(ContentBlocked, match="redrawn twice"):
        await p._clean_picture(draw, "the poster art", "poster")


def test_flagged_films_cant_be_shared_and_viewers_can_report(services, monkeypatch):
    monkeypatch.setenv("CF_ADMIN_PASSWORD", "pw")
    get_settings.cache_clear()
    from cinemafabric.studio.app import app
    from cinemafabric.studio.options import JobOptions
    from cinemafabric.studio.pipeline import new_state

    with TestClient(app) as c:
        state = new_state("film2", JobOptions(prompt="A ferry pilot reads the river at night"))
        state.update(status="done", story={"title": "River", "logline": "l", "tagline": "t"})
        state["assets"]["video"] = "/media/film2/film.mp4"
        state["checks"] = [{"check": "Picture check: frames", "verdict": "still flagged", "review": "shot 2"}]
        app.state.store.save(state)
        refused = c.post("/api/jobs/film2/share", json={"public": True})
        assert refused.status_code == 409 and "shot 2" in refused.json()["detail"]
        state["checks"][0]["verdict"] = "redrawn"
        app.state.store.save(state)
        shared = c.post("/api/jobs/film2/share", json={"public": True}).json()["share"]["id"]
        assert "Report this film" in c.get(f"/watch/{shared}").text
        assert c.post(f"/api/report/{shared}", json={"reason": "spam"}).status_code == 422
        assert c.post(f"/api/report/{shared}", json={"reason": "copyright", "note": "Looks like an existing studio's film"}).json()["ok"]
        assert c.get("/api/jobs/film2").json()["reports"][0]["label"] == "Copies someone else's work"
        assert c.get("/api/reports").status_code == 401
        assert c.get("/api/reports", headers={"X-Admin-Password": "pw"}).json()["films"][0]["id"] == "film2"
        assert c.post("/api/report/nope", json={"reason": "other"}).status_code == 404


def test_content_credentials_are_signed_and_read_back(services, tmp_path):
    pytest.importorskip("c2pa")
    from PIL import Image

    from cinemafabric.studio.media import credentials

    poster = tmp_path / "poster.png"
    Image.new("RGB", (64, 96), (40, 60, 90)).save(poster)
    own = credentials.sign(poster, "River", get_settings())
    found = credentials.read(poster)
    assert own is False and found and found["state"] == "Valid"
    assert set(found["issues"]) <= {"signingCredential.untrusted"}
    assert (tmp_path / "c2pa" / "c2pa_key.pem").exists()


def test_story_originality_reaches_every_field_and_reports_what_it_could_not_place():
    from cinemafabric.studio import originality
    from cinemafabric.studio.schemas import Character, Shot, Treatment

    t = Treatment(title="Night", logline="Borrowed Hero walks to Borrowed Academy.", tagline="Borrowed Hero returns",
                  tone="t", setting="Borrowed Academy at dusk", music_mood="m",
                  characters=[Character(name="Borrowed Hero", look="small")], beats=["Borrowed Hero leaves Borrowed Academy"])
    shots = [Shot(visual="Borrowed Academy gates", narration="A famous film line."),
             Shot(visual="rain", narration="A famous film line.")]
    unmatched: list[str] = []
    changed = originality.apply(t, shots, [
        originality.StoryIssue(where="character", original="Borrowed Hero", replacement="Tenzin"),
        originality.StoryIssue(where="phrase", original="Borrowed Academy", replacement="the Hill School"),
        originality.StoryIssue(where="line", original="A famous film line.", replacement="Go well."),
        originality.StoryIssue(where="phrase", original="Borrowed Land", replacement="X"),
    ], unmatched)
    assert len(changed) == 3 and unmatched == ["Borrowed Land"]
    assert t.tagline == "Tenzin returns" and t.setting == "the Hill School at dusk" and "Academy" not in t.beats[0]
    assert [s.narration for s in shots] == ["Go well.", "Go well."] and shots[0].visual == "the Hill School gates"


async def test_contradictory_or_empty_originality_verdicts_never_pass(tmp_path, monkeypatch):
    from cinemafabric.studio import pipeline

    monkeypatch.setattr(pipeline, "profile", lambda: {"safety": {"moderation": "best_effort"}})
    p, _ = guarded(tmp_path, {"safe": True, "categories": []})
    p.fabric = FakeFabric({"safe": True, "categories": []}, {"original": False, "problems": [], "rewrite": ""})
    with pytest.raises(ContentBlocked):
        await p.idea_originality()
    p.fabric = FakeFabric({"safe": True, "categories": []}, {})
    await p.idea_originality()
    assert p.state["checks"][-1]["verdict"] == "not checked"
    monkeypatch.setattr(pipeline, "profile", lambda: {"safety": {"moderation": "required"}})
    with pytest.raises(ContentBlocked, match="no verdict"):
        await p.idea_originality()


async def test_a_content_stop_in_an_optional_stage_is_not_swallowed(tmp_path):
    p, _ = guarded(tmp_path, {"safe": True, "categories": []})
    with pytest.raises(ContentBlocked):
        async with p.running("cast", required=False):
            raise ContentBlocked("stop")
    assert p.stage("cast")["status"] == "failed"


async def test_a_redrawn_frame_that_keeps_failing_is_put_back(tmp_path, monkeypatch):
    from cinemafabric.studio import pipeline
    from cinemafabric.studio.schemas import Review

    monkeypatch.setattr(pipeline, "profile", lambda: {"safety": {"moderation": "best_effort"}})
    p, _ = guarded(tmp_path, {"safe": True, "categories": []})
    path = tmp_path / "shot_00.png"
    original = b"\x89PNG old"
    path.write_bytes(original)

    async def frame(i, fix="", fresh=False):
        return b"new"

    async def review(i, at):
        return Review(score=8, real_person=True)

    monkeypatch.setattr(pipeline, "_to_png", lambda data, at: at.write_bytes(data))
    p._frame, p._review = frame, review
    with pytest.raises(ContentBlocked, match="left as it was"):
        await p._checked(0, path, original)
    assert path.read_bytes() == original

    async def clean(i, at):
        return Review(score=8)

    p.state["checks"] = [{"check": "Picture check: frames", "verdict": "still flagged", "review": "", "shots": [0]}]
    p._review = clean
    assert await p._checked(0, path, original) is not None
    assert p.state["checks"][0]["verdict"] == "resolved"


async def test_a_refused_picture_prompt_is_drawn_plainer_then_as_the_setting(tmp_path):
    from cinemafabric.fabric.client import FabricError
    from cinemafabric.studio.schemas import Character, Shot, Treatment

    refusal = FabricError('Meta API HTTP 400: {"error":{"code":"content_policy_violation"}}')
    p, _ = guarded(tmp_path, {"safe": True, "categories": []})
    p.treatment = Treatment(title="T", logline="l", tagline="t", tone="calm", setting="a harbour at dawn", music_mood="m",
                            characters=[Character(name="Asha", look="tall")], beats=["a"])
    p.shots = [Shot(visual="Asha on the pier", camera="static", duration=3, characters=["Asha"])]
    p.shot_prompts = ["a detailed prompt the policy turns down"]
    p.state["shots"] = [{}]
    prompts: list[str] = []

    def refusing(times, error=refusal):
        async def call(kind, payload, stage, label, **kw):
            prompts.append(payload["prompt"])
            if len(prompts) <= times:
                raise error
            return SimpleNamespace(output={"images": [b64e(b"png")]})
        return call

    p.call = refusing(1)
    assert await p._frame(0, fix="keep the design original") == b"png"
    assert p.state["shots"][0]["refused"] == "plainer" and "Asha on the pier" in prompts[-1] and p.shots[0].characters
    assert "keep the design original" in prompts[-1] and "keep the design" not in p.shot_prompts[0]

    prompts.clear()
    p.call = refusing(2)
    assert await p._frame(0) == b"png"
    assert p.state["shots"][0]["refused"] == "setting" and p.shots[0].characters == [] and p.state["shots"][0]["replaced"]
    assert "a harbour at dawn" in prompts[-1]

    prompts.clear()
    p.call = refusing(3)
    with pytest.raises(FabricError, match="content_policy"):
        await p._frame(0)
    prompts.clear()
    p.call = refusing(1, FabricError("worker unreachable"))
    with pytest.raises(FabricError, match="unreachable"):
        await p._frame(0)
    assert len(prompts) == 1


def test_a_film_that_becomes_flagged_stops_being_shown(services):
    from cinemafabric.studio.app import app
    from cinemafabric.studio.options import JobOptions
    from cinemafabric.studio.pipeline import new_state

    with TestClient(app) as c:
        state = new_state("film3", JobOptions(prompt="A ferry pilot reads the river at night"))
        state.update(status="done", story={"title": "River", "logline": "l", "tagline": "t"})
        state["assets"]["video"] = "/media/film3/film.mp4"
        app.state.store.save(state)
        shared = c.post("/api/jobs/film3/share", json={"public": True}).json()["share"]["id"]
        assert c.get(f"/watch/{shared}").status_code == 200
        state = app.state.store.get("film3")
        state["checks"] = [{"check": "Picture check: frames", "verdict": "still flagged", "review": "shot 1"}]
        app.state.store.save(state)
        assert c.get(f"/watch/{shared}").status_code == 404
        assert c.post(f"/api/report/{shared}", json={"reason": "other"}).status_code == 404


def test_a_damaged_local_signing_key_is_replaced(tmp_path):
    pytest.importorskip("c2pa")
    from cinemafabric.studio.media import credentials

    chain, key = credentials.local_chain(tmp_path)
    (tmp_path / "c2pa_key.pem").write_bytes(key[: len(key) // 2])
    chain2, key2 = credentials.local_chain(tmp_path)
    assert key2 != key[: len(key) // 2] and credentials._matches(chain2, key2)
    assert credentials.local_chain(tmp_path) == (chain2, key2)


def test_a_film_being_made_can_be_stopped(services, monkeypatch):
    from cinemafabric.studio import app as studio
    from cinemafabric.studio.pipeline import Production

    async def forever(self):
        self.state["status"] = "running"
        self.state["stages"][0]["status"] = "running"
        await asyncio.sleep(3600)

    monkeypatch.setattr(Production, "run", forever)
    with TestClient(studio.app) as c:
        assert c.post("/api/jobs/nobody/stop").status_code == 404
        state = new_state("film9", JobOptions(prompt="A lighthouse keeper hears the ocean answer"))
        studio.app.state.store.save(state)
        assert c.post("/api/jobs/film9/stop").status_code == 409

        async def begin(work):
            studio._start(state, work)
            await asyncio.sleep(0.05)

        c.portal.call(begin, "run")
        assert c.post("/api/jobs/film9/stop").status_code == 200
        c.portal.call(asyncio.sleep, 0.05)
        saved = c.get("/api/jobs/film9").json()
        assert saved["status"] == "failed" and saved["error"] == "Stopped before it finished."
        assert saved["stages"][0]["status"] == "failed" and saved["stages"][0]["detail"] == "stopped"
        assert "film9" not in studio.app.state.tasks and c.post("/api/jobs/film9/stop").status_code == 409


def test_an_edit_to_a_finished_film_cannot_be_stopped_halfway(services, monkeypatch):
    from cinemafabric.studio import app as studio
    from cinemafabric.studio.pipeline import Production

    async def forever(self, **kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(Production, "redo_shot", forever)
    with TestClient(studio.app) as c:
        state = new_state("film8", JobOptions(prompt="A lighthouse keeper hears the ocean answer"))
        studio.app.state.store.save(state)

        async def begin():
            studio._start(state, "redo")
            await asyncio.sleep(0.05)

        c.portal.call(begin)
        assert c.post("/api/jobs/film8/stop").status_code == 409


async def test_a_poster_that_keeps_failing_becomes_one_with_no_people(tmp_path, monkeypatch):
    from cinemafabric.studio import pipeline, prompts

    monkeypatch.setattr(pipeline, "profile", lambda: {"safety": {"moderation": "required"}})
    p, _ = guarded(tmp_path, {"safe": True, "categories": []})
    drawn = []

    async def draw(fix):
        drawn.append(fix)
        return b"scenery" if fix == prompts.POSTER_NO_PEOPLE else b"person"

    async def looks(png, what, stage, label):
        return [] if png == b"scenery" else ["a real, recognisable person"]

    p._looks = looks
    png, record = await p._clean_picture(draw, "the poster art", "poster", prompts.POSTER_NO_PEOPLE)
    assert png == b"scenery" and record["outcome"] == "redrawn" and drawn[-1] == prompts.POSTER_NO_PEOPLE

    async def always_flagged(png, what, stage, label):
        return ["a real, recognisable person"]

    p._looks = always_flagged
    with pytest.raises(ContentBlocked, match="still showed"):
        await p._clean_picture(draw, "the poster art", "poster", prompts.POSTER_NO_PEOPLE)
    with pytest.raises(ContentBlocked, match="still showed"):
        await p._clean_picture(draw, "a character sheet", "cast")
