import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cinemafabric.fabric.cache import ResultCache
from cinemafabric.fabric.ledger import BudgetExceeded, Ledger
from cinemafabric.fabric.registry import Registry
from cinemafabric.fabric.router import NoRouteError, Router
from cinemafabric.fabric.types import TaskKind, TaskRequest
from cinemafabric.studio.options import JobOptions
from cinemafabric.studio.pipeline import Production, new_state
from cinemafabric.studio.schemas import Treatment
from cinemafabric.studio.store import JobStore
from tests.test_fabric import FakeBackend, text_offer


async def router_for(tmp_path, backends):
    registry = Registry(backends)
    await registry.probe_all()
    return Router(registry, Ledger(tmp_path / "f.db", None, None, simulated=False), ResultCache(tmp_path / "c")), registry


def task():
    return TaskRequest(kind=TaskKind.TEXT, payload={"prompt": "hi"}, cache=False)


async def test_a_crash_on_one_request_does_not_trip_the_circuit(tmp_path):
    class Picky(FakeBackend):
        async def execute(self, offer, payload):
            if payload.get("bad"):
                raise KeyError("image")
            return await super().execute(offer, payload)

    worker = Picky("w", [text_offer("m", 2)])
    router, registry = await router_for(tmp_path, [worker])
    for _ in range(3):
        with pytest.raises(NoRouteError):
            await router.run(TaskRequest(kind=TaskKind.TEXT, payload={"bad": True}, cache=False))
    assert registry.available("w")[0]
    assert (await router.run(task())).decision.worker_id == "w"


async def test_an_unreadable_cache_entry_is_a_miss(tmp_path):
    worker = FakeBackend("w", [text_offer("m", 2)])
    router, _ = await router_for(tmp_path, [worker])
    req = TaskRequest(kind=TaskKind.TEXT, payload={"prompt": "hi"})
    router.cache.put(router.cache.key(req), {"output": {}, "decision": {"worker_id": "x"}})
    result = await router.run(req)
    assert not result.cached and worker.calls == 1


async def test_a_capability_whose_model_failed_to_load_is_skipped(tmp_path):
    broken = FakeBackend("broken", [text_offer("m", 2)])
    broken.unavailable = {TaskKind.TEXT: "model failed to load"}
    spare = FakeBackend("spare", [text_offer("m", 1)])
    router, _ = await router_for(tmp_path, [broken, spare])
    result = await router.run(task())
    assert result.decision.worker_id == "spare" and broken.calls == 0


async def test_latency_excludes_time_spent_queued(tmp_path):
    class Slow(FakeBackend):
        async def execute(self, offer, payload):
            await asyncio.sleep(0.1)
            return await super().execute(offer, payload)

    router, registry = await router_for(tmp_path, [Slow("w", [text_offer("m", 2)])])
    await asyncio.gather(router.run(task()), router.run(task()))
    assert registry.state["w"].latency["text.generate"] < 0.18


async def test_a_stop_without_a_budget_is_still_enforced(tmp_path):
    ledger = Ledger(tmp_path / "f.db", None, 0.05, simulated=False)
    assert ledger.summary()["remaining_usd"] == pytest.approx(0.05)
    with pytest.raises(BudgetExceeded):
        await ledger.reserve("t", 0.10, None, None)


def test_one_bad_job_row_does_not_break_the_list(tmp_path):
    store = JobStore(tmp_path / "s.db")
    good = new_state("good", JobOptions(prompt="A ferry captain finds a stowaway"))
    store.save(good)
    store.db.execute("INSERT INTO jobs (id, created, updated, status, state) VALUES ('bad', 1, 1, 'done', ?)",
                     (json.dumps({"id": "bad", "created": 1, "status": "done"}),))
    store.db.execute("INSERT INTO jobs (id, created, updated, status, state) VALUES ('worse', 1, 1, 'done', 'not json')")
    assert {j["id"] for j in store.list()} == {"good", "bad"}


def test_interrupted_jobs_do_not_leave_a_stage_running(tmp_path):
    store = JobStore(tmp_path / "s.db")
    state = new_state("j", JobOptions(prompt="A ferry captain finds a stowaway"))
    state["status"] = "running"
    state["stages"][5]["status"] = "running"
    store.save(state)
    store.mark_interrupted()
    after = store.get("j")
    assert after["status"] == "failed"
    assert all(s["status"] != "running" for s in after["stages"])


def production(tmp_path, fabric=None):
    opts = JobOptions(prompt="A lighthouse keeper hears the ocean answer")
    store = SimpleNamespace(save=lambda state: None)
    return Production(new_state("t1", opts), fabric=fabric, store=store, data_dir=tmp_path)  # type: ignore[arg-type]


async def test_a_review_without_a_score_does_not_pass_the_frame(tmp_path):
    from PIL import Image

    class Silent:
        async def run(self, kind, payload, **kw):
            return SimpleNamespace(output={"json": {}})

    p = production(tmp_path, Silent())
    p.shots = [SimpleNamespace(visual="a pier", camera="static")]  # type: ignore[list-item]
    p.shot_prompts = ["a pier at dusk"]
    frame = tmp_path / "f.png"
    Image.new("RGB", (8, 8)).save(frame)
    with pytest.raises(ValueError):
        await p._review(0, frame)


def test_script_approval_only_applies_once(tmp_path):
    p = production(tmp_path)
    p.treatment = Treatment(title="T", logline="l", tagline="t", tone="t", setting="s", music_mood="m",
                            characters=[], beats=["a"])
    p.state["status"] = "review"
    p.apply_edits([{"visual": "first edit", "duration": 3}])
    with pytest.raises(ValueError):
        p.apply_edits([{"visual": "second edit", "duration": 3}])
    assert p.shots[0].visual == "first edit"


def test_redo_starts_from_the_shot_prompt_not_the_wrapped_one(tmp_path):
    p = production(tmp_path)
    p.state["story"] = {"title": "T", "logline": "l", "tagline": "t", "tone": "t", "setting": "s", "music_mood": "m",
                        "characters": [], "beats": ["a"]}
    p.state["shots"] = [{"visual": "a quiet harbour", "camera": "static", "duration": 3, "narration": "",
                         "characters": [], "title_card": "", "prompt": "WRAPPED a quiet harbour, cinematic",
                         "shot_prompt": "a quiet harbour"}]
    p.restore()
    assert p.shot_prompts == ["a quiet harbour"]


def test_director_keeps_only_edits_that_fit_the_film():
    from cinemafabric.studio import director
    from cinemafabric.studio.schemas import Shot

    shots = [Shot(visual="a pier at dusk", duration=3), Shot(title_card="THIS WINTER", duration=2)]
    plan = director.Plan(reply="ok", edits=[
        director.Edit(op="narration", shot=1, text="  The tide  keeps   secrets. "),
        director.Edit(op="narration", shot=1, text="duplicate"),
        director.Edit(op="visual", shot=2, text="title cards have no picture"),
        director.Edit(op="duration", shot=2, seconds=30),
        director.Edit(op="redraw", shot=9, text="no such shot"),
        director.Edit(op="title", shot=4, text="Low Water"),
        director.Edit(op="music", shot=0, text=""),
    ])
    kept = director.validate(plan, shots)
    assert [(e.op, e.shot) for e in kept] == [("narration", 1), ("duration", 2), ("title", 0)]
    assert kept[0].text == "The tide keeps secrets." and kept[1].seconds == 6.0
    assert director.estimate([director.Edit(op="redraw", shot=1), director.Edit(op="title", text="x")]) > 0


def test_suggested_ideas_lose_title_scaffolding():
    from cinemafabric.studio.suggest import _bare

    assert _bare("Short film: 'Rainy Night' - A taxi driver crosses a flooded city.") == "A taxi driver crosses a flooded city."
    assert _bare('Trailer: "Reef" — A diver finds a door.') == "A diver finds a door."
    assert _bare("A plain idea stays as it is.") == "A plain idea stays as it is."


async def test_suggested_settings_are_clamped_to_the_catalog():
    from cinemafabric.studio import suggest

    class Fabric:
        async def run(self, kind, payload, **kw):
            json_ = {"format": "trailer", "seconds": 900, "genre": "Western", "style": "anime", "aspect": "4:3",
                     "reason": "Because."}
            return SimpleNamespace(output={"json": json_}, decision=SimpleNamespace(model="m", worker_label="w"))

    s = await suggest.settings(Fabric(), "A tea-estate radio operator hears tomorrow's storm")  # type: ignore[arg-type]
    assert s["seconds"] == 90 and s["genre"] is None and s["aspect"] is None and s["style"] == "anime"


async def test_a_billed_but_rejected_answer_still_counts_as_spend(tmp_path):
    from cinemafabric.fabric.backends.base import BackendError

    class Rambling(FakeBackend):
        async def execute(self, offer, payload):
            self.calls += 1
            raise BackendError("no valid JSON", retryable=False, cost_usd=0.02)

    cloud = Rambling("cloud", [text_offer("m", 2, cost=0.01)], is_cloud=True)
    router, _ = await router_for(tmp_path, [cloud])
    with pytest.raises(NoRouteError):
        await router.run(task())
    assert router.ledger.spent_usd == pytest.approx(0.02)


async def test_metrics_cover_tasks_latency_spend_and_workers(tmp_path):
    from cinemafabric.fabric import metrics

    local = FakeBackend("local", [text_offer("m", 2)])
    cloud = FakeBackend("cloud", [text_offer("c", 3, cost=0.01)], is_cloud=True)
    router, registry = await router_for(tmp_path, [local, cloud])
    await router.run(task())
    from cinemafabric.fabric.types import Tier

    await router.run(TaskRequest(kind=TaskKind.TEXT, payload={"prompt": "x"}, tier=Tier.PREMIUM, cache=False))
    text = metrics.render(registry, router.ledger)
    assert 'cinemafabric_tasks_total{kind="text.generate",worker="local",status="ok",where="local"} 1' in text
    assert 'cinemafabric_spend_usd_total{model="c"} 0.01' in text
    assert 'cinemafabric_task_seconds_count{kind="text.generate"} 2' in text
    assert 'cinemafabric_worker_up{worker="cloud",where="cloud"} 1' in text
    assert "# TYPE cinemafabric_task_seconds histogram" in text


def test_vtt_tracks_and_language_choices(tmp_path):
    from cinemafabric.studio.media import render

    render.write_vtt([(0.0, 1.5, "नमस्ते"), (61.25, 63.0, "दूसरी पंक्ति")], tmp_path / "s.vtt")
    text = (tmp_path / "s.vtt").read_text(encoding="utf-8")
    assert text.startswith("WEBVTT") and "00:01:01.250 --> 00:01:03.000" in text and "नमस्ते" in text
    JobOptions(prompt="A ferry pilot reads the river", subtitle_languages=["hi", "te"]).validate_choices()
    with pytest.raises(ValueError):
        JobOptions(prompt="A ferry pilot reads the river", subtitle_languages=["xx"]).validate_choices()


def test_cached_input_is_billed_at_the_cached_rate():
    from cinemafabric.fabric import pricing

    full = pricing.text_cost("muse-spark-1.3", 1_000_000, 0)
    cached = pricing.text_cost("muse-spark-1.3", 1_000_000, 0, cached_tokens=1_000_000)
    assert full == pytest.approx(1.25) and cached == pytest.approx(0.15)


def test_library_characters_keep_their_name_look_and_sheet(tmp_path):
    from cinemafabric.studio.library import CharacterLibrary

    lib = CharacterLibrary(tmp_path / "s.db", tmp_path / "library")
    sheet = tmp_path / "meera.png"
    sheet.write_bytes(b"png")
    saved = lib.save("Meera", "girl, 11, white school dress, red ribbon", sheet, "job1")
    assert lib.save("Meera", "girl, 11, white school dress, red ribbon", sheet, "job2")["id"] == saved["id"]
    picked = lib.pick([saved["id"]])
    assert picked[0]["name"] == "Meera" and Path(picked[0]["sheet"]).read_bytes() == b"png"
    with pytest.raises(KeyError):
        lib.pick(["nope"])
    assert lib.delete(saved["id"]) and lib.all() == []


def test_treatment_prompt_names_returning_characters():
    from cinemafabric.studio import prompts

    text = prompts.treatment(JobOptions(prompt="A girl crosses the river alone"), [{"name": "Meera", "look": "red ribbon"}])
    assert "RETURNING CHARACTERS" in text and "Meera: red ribbon" in text and "at most 2 new" in text


def test_media_bucket_upload_and_fallback(tmp_path, monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cinemafabric.config import Settings
    from cinemafabric.studio.media.storage import MediaFiles, MediaStore

    for key, value in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(key, value)
    with moto.mock_aws():
        boto3.client("s3").create_bucket(Bucket="films")
        job = tmp_path / "jobs" / "j1"
        (job / "frames").mkdir(parents=True)
        (job / "film.mp4").write_bytes(b"mp4")
        (job / "frames" / "shot_00.png").write_bytes(b"png")
        (job / "audio").mkdir()
        (job / "audio" / "mix.wav").write_bytes(b"scratch")
        store = MediaStore(Settings(media_bucket="films"))
        assert store.upload_job(job, "j1") == 2
        keys = {o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="films")["Contents"]}
        assert keys == {"jobs/j1/film.mp4", "jobs/j1/frames/shot_00.png"}

        app = FastAPI()
        app.mount("/media", MediaFiles(directory=tmp_path / "jobs", store=store))
        c = TestClient(app)
        assert c.get("/media/j1/film.mp4").content == b"mp4"
        (job / "film.mp4").unlink()
        moved = c.get("/media/j1/film.mp4", follow_redirects=False)
        assert moved.status_code == 307 and "jobs/j1/film.mp4" in moved.headers["location"]
        assert c.get("/media/j1/../../secret.txt", follow_redirects=False).status_code == 404


def test_translations_that_echo_or_use_the_wrong_script_are_rejected():
    from cinemafabric.studio.pipeline import translation_problem

    source = ["I left, and I should've stayed.", "Hold tight.", "We'll make it."]
    echoed = [f"<idea>{i}. {line}</idea>" for i, line in enumerate(source, 1)]
    assert "untranslated" in translation_problem(source, echoed, "ta")
    assert translation_problem(source, ["Me fui.", "Agárrate."], "es").startswith("expected 3")
    assert "script" in translation_problem(source, ["Naan ponen.", "Pidi.", "Seiyalaam."], "ta")
    assert translation_problem(source, ["நான் போனேன்.", "இறுக்கமாகப் பிடி.", "நாம் சேர்வோம்."], "ta") is None
    assert translation_problem(source, ["1. Me fui.", "Agárrate fuerte.", "Lo lograremos."], "es") is None


def test_hindi_with_dashes_but_english_words_is_rejected():
    from cinemafabric.studio.pipeline import translation_problem

    source = ["I left, and I should've stayed.", "Hold on tight now.", "We will make it.", "Don't look back."]
    english = ["I went away — I should've stayed.", "Grip it hard — now.", "We'll get there.", "Never look back."]
    assert "script" in translation_problem(source, english, "hi")
    assert translation_problem(["Ana.", "No.", "Taxi."], ["Ana.", "No.", "Taxi."], "es") is None


async def test_a_harmful_later_part_still_blocks(tmp_path):
    from cinemafabric.studio.pipeline import MODERATION_CHUNK, ContentBlocked
    from tests.test_security import FakeFabric

    p = production(tmp_path)

    class PerChunk(FakeFabric):
        async def run(self, kind, payload, **kw):
            if kind == TaskKind.TEXT:
                harmful = "HARM STEPS" in payload["prompt"]
                return SimpleNamespace(output={"json": {"harmful": harmful, "category": "S9" if harmful else "",
                                                        "reason": "harmful steps" if harmful else "fiction"}},
                                       decision=SimpleNamespace(model="reviewer"))
            return SimpleNamespace(output={"safe": False, "categories": ["S1"]})

    p.fabric = PerChunk({})
    text = "a heist thriller " * (MODERATION_CHUNK // 17 * 2) + "HARM STEPS"
    with pytest.raises(ContentBlocked, match="harmful steps"):
        await p.check_content(text, None, "Check")


def test_voices_read_ages_not_stray_digits(tmp_path):
    p = production(tmp_path)
    p.treatment = Treatment(title="T", logline="l", tagline="t", tone="t", setting="s", music_mood="m", beats=["a"],
                            characters=[{"name": "Ravi", "look": "a 7-year-old boy in a red cap"},
                                        {"name": "Asha", "look": "a woman in her 70s with grey hair"},
                                        {"name": "Dev", "look": "a man, about 35, wearing a size 6 ring"}])
    v = p.voices()
    assert v["ravi"]["speed"] == 0.97 and v["ravi"]["pitch"] > 1.15
    assert v["asha"]["speed"] == 0.88
    assert v["dev"]["speed"] == 0.97 and v["dev"]["pitch"] < 0.9


async def test_a_failed_note_leaves_the_film_as_it_was(tmp_path):
    from PIL import Image

    from cinemafabric.fabric.client import FabricError

    class Broken:
        async def run(self, kind, payload, **kw):
            raise FabricError("image service down")

        async def get(self, path, **kw):
            return {"cost_usd": 0.0}

    p = production(tmp_path, Broken())
    p.state.update(status="done", story={"title": "T", "logline": "l", "tagline": "t", "tone": "t", "setting": "s",
                                         "music_mood": "m", "characters": [], "beats": ["a"]})
    p.state["shots"] = [{"index": 0, "visual": "a quiet harbour", "camera": "static", "duration": 3, "narration": "",
                         "characters": [], "title_card": "", "image": "/media/x/frames/shot_00.png"}]
    (p.dir / "frames").mkdir(parents=True, exist_ok=True)
    frame = p.dir / "frames" / "shot_00.png"
    Image.new("RGB", (4, 4), "blue").save(frame)
    original = frame.read_bytes()
    p.state["director"] = {"turns": [], "pending": {"note": "n", "reply": "r", "at": 0, "estimate_usd": 0.01,
                                                     "edits": [{"op": "visual", "shot": 1, "text": "a stormy harbour"}]}}
    await p.apply_plan()
    assert p.state["status"] == "done" and "nothing was changed" in p.state["error"]
    assert p.state["shots"][0]["visual"] == "a quiet harbour" and frame.read_bytes() == original
    assert p.state["director"]["pending"] and p.state["director"]["turns"][-1]["applied"] is False


async def test_one_based_shot_prompts_land_on_the_right_shots(tmp_path):
    from cinemafabric.studio.schemas import Shot

    class Writer:
        async def run(self, kind, payload, **kw):
            prompts = [{"index": n + 1, "prompt": f"rich prompt for shot number {n + 1} with lots of detail"} for n in range(3)]
            return SimpleNamespace(output={"json": {"prompts": prompts}}, decision=SimpleNamespace(model="m", worker_label="w"))

    p = production(tmp_path, Writer())
    p.treatment = Treatment(title="T", logline="l", tagline="t", tone="t", setting="s", music_mood="m", characters=[], beats=["a"])
    p.shots = [Shot(visual=f"shot {n}") for n in range(3)]
    await p.design_shots()
    assert [sp.split(" with")[0] for sp in p.shot_prompts] == [f"rich prompt for shot number {n}" for n in (1, 2, 3)]


async def test_every_returning_character_gets_their_sheet(tmp_path):
    from cinemafabric.studio.schemas import Shot

    p = production(tmp_path)
    names = ["Meera", "Ravi", "Asha"]
    sheets = []
    for n in names:
        sheet = tmp_path / f"{n}.png"
        sheet.write_bytes(b"png")
        sheets.append({"id": n, "name": n, "look": "x", "sheet": str(sheet)})
    p.state["library_cast"] = sheets
    p.treatment = Treatment(title="T", logline="l", tagline="t", tone="t", setting="s", music_mood="m", beats=["a"],
                            characters=[{"name": n, "look": "x"} for n in names])
    p.shots = [Shot(visual="all three on the boat", characters=names)]
    await p.cast()
    assert sorted(c["name"] for c in p.state["cast"]) == sorted(names) and all(c["returning"] for c in p.state["cast"])


async def test_unclear_lines_are_revoiced_and_the_better_take_kept(tmp_path):
    import numpy as np

    from cinemafabric.studio.schemas import Shot
    from cinemafabric.util import b64e, wav_bytes

    class Studio:
        def __init__(self):
            self.heard = 0

        async def run(self, kind, payload, **kw):
            if kind == TaskKind.TRANSCRIBE:
                self.heard += 1
                clear = self.heard > 2
                return SimpleNamespace(output={"text": "hold tight now and never let go" if clear else "old ice"})
            audio = b64e(wav_bytes(np.zeros(8000, dtype=np.float32), 16000))
            return SimpleNamespace(output={"audio": audio, "duration_s": 0.5})

    p = production(tmp_path, Studio())
    p.shots = [Shot(visual="a boat", narration="Hold tight now and never let go.")]
    (p.dir / "audio").mkdir(parents=True, exist_ok=True)
    line = p.dir / "audio" / "vo_00.wav"
    line.write_bytes(wav_bytes(np.ones(8000, dtype=np.float32) * 0.1, 16000))
    p.voice_lines = [(0, line, 0.5)]
    await p.qa()
    qa = p.state["qa"]
    assert qa["first_similarity"] < 0.5 and qa["similarity"] > 0.9 and qa["redone_lines"][0]["shot"] == 1


def test_picture_check_records_what_it_found(tmp_path):
    from cinemafabric.studio.schemas import Review

    assert Review(score=8, brand_logo=True).concerns == ["a real brand or logo"]
    p = production(tmp_path)
    p.picture_check(6, {}, {})
    p.picture_check(6, {2: ["a real brand or logo"]}, {})
    p.picture_check(6, {2: ["a real brand or logo"]}, {2: ["a real brand or logo"]})
    assert [c["verdict"] for c in p.state["checks"]] == ["passed", "redrawn", "still flagged"]
    assert "shot 3" in p.state["checks"][-1]["review"]


def test_passport_facts_come_from_the_receipt_and_routing_log():
    from cinemafabric.studio.media import passport

    state = {"story": {"title": "Night River"}, "runtime": {"label": "20-second trailer"}, "options": {"voice_language": "hi"},
             "checks": [{"verdict": "passed"}, {"verdict": "redrawn"}], "watermark": {"verified": True},
             "assets": {"subtitle_tracks": [{"label": "English"}]}}
    receipt = {"cost_usd": 0.1, "avoided_usd": 0.02, "local_tasks": 14, "cloud_tasks": 17, "cache_hits": 2}
    decisions = [
        {"status": "ok", "worker_label": "Desktop · RTX 5070", "is_cloud": False, "model": "llama3.1:8b", "fallbacks": [], "reason": ""},
        {"status": "ok", "worker_label": "Meta Model API · Muse", "is_cloud": True, "model": "muse-image-1.0", "fallbacks": [],
         "reason": "Local-first: ...; mac skipped (disabled)"},
    ]
    f = passport.facts(state, receipt, decisions)
    assert f["machines"] == ["Desktop", "Meta Model API"] and f["moved"] == 1 and f["failed"] == 0
    assert f["tasks"] == 31 and f["checks_ok"] == 2 and f["llama"] and f["all_cloud"] == pytest.approx(0.12)
    assert passport.render(None, f, "Hindi").size == (1200, 630)


async def test_a_commercial_safe_request_never_uses_non_commercial_models(tmp_path):
    from cinemafabric.fabric.backends.base import Offer

    nc = FakeBackend("musicgen", [Offer(TaskKind.MUSIC, "musicgen", 2, licence="cc-by-nc-4.0")])
    ok = FakeBackend("synth", [Offer(TaskKind.MUSIC, "procedural", 1, licence="builtin")])
    router, _ = await router_for(tmp_path, [nc, ok])
    normal = await router.run(TaskRequest(kind=TaskKind.MUSIC, payload={}, cache=False))
    safe = await router.run(TaskRequest(kind=TaskKind.MUSIC, payload={}, cache=False, exclude_licences=["cc-by-nc-4.0"]))
    assert normal.decision.worker_id == "musicgen" and safe.decision.worker_id == "synth"
    assert any(c.rejected and "licence" in c.rejected for c in safe.decision.candidates)
