"""The production pipeline: prompt -> story -> script -> storyboard -> poster -> sound -> film."""

import asyncio
import contextlib
import copy
import difflib
import hashlib
import io
import itertools
import json
import logging
import re
import time
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image
from pydantic import BaseModel, ValidationError

from ..fabric.client import FabricClient, FabricError
from ..fabric.types import Preference, TaskKind, Tier
from ..profile import catalog, profile
from ..util import b64d, b64e, read_wav, wav_bytes
from . import agents, director, originality, prompts
from .media import audio as audio_mix
from .media import provenance, render
from .options import ANIMATED_STYLES, ASPECTS, FINAL_CARD_S, LANGUAGES, VOICE_LANGUAGES, JobOptions, runtime, video_size
from .prompts import STRICT_FIX, STRICTER_FIX
from .schemas import (
    PICTURE_FIELDS,
    PICTURE_SCHEMA,
    REVIEW_SCHEMA,
    SAFETY_REVIEW_SCHEMA,
    SCRIPT_SCHEMA,
    SHOT_PROMPTS_SCHEMA,
    TRANSLATION_SCHEMA,
    TREATMENT_SCHEMA,
    Character,
    PictureCheck,
    Review,
    Scene,
    Script,
    Shot,
    Treatment,
)
from .store import JobStore

log = logging.getLogger("cinemafabric.studio")

STAGES = [
    ("safety", "Content check"),
    ("story", "Story"),
    ("script", "Script"),
    ("shots", "Shot prompts"),
    ("cast", "Characters"),
    ("frames", "Frames"),
    ("review", "Frame check"),
    ("poster", "Poster"),
    ("depth", "Depth and cutouts"),
    ("voice", "Narration"),
    ("score", "Music"),
    ("qa", "Narration check"),
    ("edit", "Edit"),
    ("package", "Package"),
]
REVIEW_PASS = 5
SHOT_PROMPT_BATCH = 12
MUSIC_SECTION_S = 30.0
MUSIC_OVERLAP_S = 5.0
MUSIC_MAX_SECTIONS = 16
MODERATION_CHUNK = 4000
NON_COMMERCIAL = ["cc-by-nc-4.0"]
TRAILER_S = 30.0
TRAILER_SHOT_S = 2.6
TRAILER_LINES = 3
QA_PASS = 0.85
QA_LINE_PASS = 0.7


class StageSkipped(Exception):
    pass


async def together(*aws):
    """Like asyncio.gather, but the first failure cancels the rest so nothing keeps spending."""
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(a) for a in aws]
    except BaseExceptionGroup as group:
        raise group.exceptions[0] from None
    return [t.result() for t in tasks]


def runtime_state(opts: JobOptions, seconds: int, reason: str = "") -> dict:
    rt = runtime(opts.format, seconds)
    return {"seconds": rt.seconds, "label": rt.label, "shots": rt.shots,
            "decided_by": "story" if opts.length == "auto" else "you", "reason": reason}


def new_state(job_id: str, opts: JobOptions) -> dict:
    return {
        "id": job_id,
        "created": time.time(),
        "updated": time.time(),
        "status": "queued",
        "error": None,
        "options": opts.model_dump(mode="json"),
        "runtime": runtime_state(opts, opts.target_seconds) | ({"decided_by": "pending"} if opts.length == "auto" else {}),
        "stages": [{"id": s, "label": label, "status": "pending", "detail": "", "started": None, "finished": None}
                   for s, label in STAGES],
        "story": None,
        "shots": [],
        "cast": [],
        "assets": {},
        "qa": None,
        "checks": [],
        "watermark": None,
        "receipt": None,
    }


class Production:
    def __init__(self, state: dict, fabric: FabricClient, store: JobStore, data_dir: Path,
                 slot: asyncio.Semaphore | None = None):
        self.state = state
        self.slot = slot
        self.approved = asyncio.Event()
        self.work = "run"
        self.id = state["id"]
        self.opts = JobOptions.model_validate(state["options"])
        self.fabric = fabric
        self.store = store
        self.dir = data_dir / "jobs" / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.aspect = ASPECTS[self.opts.aspect]
        self.crew = getattr(self.opts, "agents", True)
        self.video = video_size(self.opts.aspect, getattr(self.opts, "resolution", "1080p"))
        self.fps = getattr(self.opts, "fps", 24)
        self.tune = "animation" if self.opts.style in ANIMATED_STYLES else "film"
        self.runtime = runtime(self.opts.format, self.opts.target_seconds)
        self.treatment: Treatment | None = None
        self.shots: list[Shot] = []
        self.shot_prompts: list[str] = []
        self.sheets: dict[str, str] = {}
        self.voice_lines: list[tuple[int, Path, float]] = []
        self.score_path: Path | None = None
        self._last_save = 0.0


    def save(self, force: bool = True) -> None:
        now = time.time()
        if force or now - self._last_save > 0.5:
            self._last_save = now
            self.store.save(self.state)

    def stage(self, sid: str) -> dict:
        return next(s for s in self.state["stages"] if s["id"] == sid)

    def set_stage(self, sid: str, status: str, detail: str | None = None) -> None:
        st = self.stage(sid)
        st["status"] = status
        if detail is not None:
            st["detail"] = detail
        if status == "running" and not st["started"]:
            st["started"] = time.time()
        if status in ("done", "skipped", "failed"):
            st["finished"] = time.time()
        self.save()

    @contextlib.asynccontextmanager
    async def running(self, sid: str, required: bool = True):
        self.set_stage(sid, "running")
        try:
            yield
        except StageSkipped as e:
            self.set_stage(sid, "skipped", str(e))
        except ContentBlocked:
            self.set_stage(sid, "failed", "stopped by a content check")
            raise
        except Exception as e:
            if required:
                self.set_stage(sid, "failed", str(e)[:300])
                raise
            log.warning("optional stage %s failed: %s", sid, e)
            self.set_stage(sid, "skipped", f"skipped: {str(e)[:200]}")
        else:
            if self.stage(sid)["status"] == "running":
                self.set_stage(sid, "done")

    def url(self, path: Path) -> str:
        return f"/media/{self.id}/{path.relative_to(self.dir).as_posix()}?v={int(path.stat().st_mtime)}"

    async def call(self, kind: TaskKind, payload: dict, stage: str, label: str, tier: Tier = Tier.STANDARD,
                   fresh: bool = False, preference: Preference | None = None):
        return await self.fabric.run(kind, payload, tier=tier, preference=preference or self.opts.preference, job_id=self.id,
                                     stage=stage, label=label, cache=not (self.opts.fresh or fresh),
                                     max_job_usd=self.opts.max_cost_usd,
                                     exclude_licences=NON_COMMERCIAL if getattr(self.opts, "commercial_safe", False) else None)

    async def structured(self, schema: dict, prompt: str, model: type[BaseModel], stage: str, label: str,
                         tier: Tier, max_tokens: int, check=None):
        attempt_prompt, last = prompt, ""
        for attempt in range(3):
            payload = {"system": prompts.WRITER_SYSTEM, "prompt": attempt_prompt, "json_schema": schema,
                       "max_tokens": max_tokens, "temperature": 0.9 if attempt == 0 else 0.7}
            res = await self.call(TaskKind.TEXT, payload, stage, label if attempt == 0 else f"{label} (retry {attempt})", tier)
            try:
                obj = model.model_validate(res.output.get("json"))
                if check:
                    check(obj)
                return obj
            except (ValidationError, ValueError, TypeError) as e:
                last = str(e)[:400]
                attempt_prompt = f"{prompt}\n\nYour previous answer was rejected: {last}\nReturn corrected JSON."
        raise RuntimeError(f"{label}: no valid answer after 3 attempts ({last})")


    async def story(self) -> None:
        async with self.running("story"):
            tier = Tier.PREMIUM if self.opts.premium_writing else Tier.STANDARD
            returning = self.state.get("library_cast") or []
            t = await self.structured(TREATMENT_SCHEMA, prompts.treatment(self.opts, returning, self.state.get("previously")),
                                      Treatment, "story",
                                      "Treatment", tier, 1200 + 80 * self.runtime.beats[1] + 3 * self.opts.target_seconds)
            saved = [Character(name=c["name"], look=c["look"]) for c in returning]
            names = {c.name.lower() for c in saved}
            t.characters = (saved + [c for c in t.characters if c.name.lower() not in names])[:3]
            self.treatment = t
            if self.opts.length == "auto":
                seconds = t.runtime_seconds or self.opts.target_seconds
                self.runtime = runtime(self.opts.format, seconds)
                self.state["runtime"] = runtime_state(self.opts, self.runtime.seconds, t.runtime_reason.strip())
            self.state["story"] = t.model_dump()
            self.set_stage("story", "running", f"{t.title} · {self.runtime.label}")

    async def script(self) -> None:
        assert self.treatment
        async with self.running("script"):
            tier = Tier.PREMIUM if self.opts.premium_writing else Tier.STANDARD
            script = await self.structured(SCRIPT_SCHEMA, prompts.script(self.opts, self.treatment, self.runtime), Script,
                                           "script", "Script", tier, 600 + 160 * self.runtime.shots,
                                           check=self._check_script)
            self.shots = self._normalise(script)
            if self.crew:
                await self.script_doctor()
                self.shots = self._normalise(script)
                self._reconcile_doctor()
                if self.opts.format == "movie":
                    await self.story_editor()
                    self.shots = self._normalise(script)
            kept = {id(s) for s in self.shots}
            (self.dir / "script.md").write_text(prompts.screenplay_markdown(self.treatment, Script(scenes=[
                s.model_copy(update={"shots": [x for x in s.shots if id(x) in kept]}) for s in script.scenes
            ]), self.opts, self.runtime), encoding="utf-8")
            self.state["shots"] = [
                {"index": i, **s.model_dump(), "image": None, "mask": None, "review": None, "prompt": None}
                for i, s in enumerate(self.shots)
            ]
            self.state["assets"]["script"] = self.url(self.dir / "script.md")
            self.set_stage("script", "running", f"{len(self.shots)} shots")

    def _check_script(self, script: Script) -> None:
        usable = [s for _, s in script.flat_shots() if s.visual.strip() and not s.title_card.strip()]
        minimum = max(4, int(self.runtime.shots * 0.5))
        if len(usable) < minimum:
            raise ValueError(f"only {len(usable)} image shots (non-title-card shots with a visual); need at least {minimum}")

    def _normalise(self, script: Script) -> list[Shot]:
        max_cards = self.runtime.max_cards
        longest = 6.0 if self.opts.format == "trailer" else agents.VOICED_MAX_S
        names = [c.name for c in self.treatment.characters] if self.treatment else []
        shots, cards = [], 0
        for _, s in script.flat_shots():
            if s.title_card.strip():
                if cards >= max_cards:
                    continue
                cards += 1
                s.title_card, s.visual = s.title_card.strip().upper()[:40], ""
                s.duration = min(max(s.duration, 1.2), 2.2)
            elif not s.visual.strip():
                continue
            else:
                s.duration = min(max(s.duration, 1.5), longest)
                speaker, s.narration = agents.split_speaker(s.narration, names)
                if speaker and not s.speaker and speaker.lower() not in ("narrator", "voice-over", "vo"):
                    s.speaker = speaker
                if not agents.speaker_fits(s.speaker, s.narration):
                    s.speaker = "narrator"
            shots.append(s)
        target = self.runtime.seconds - FINAL_CARD_S
        for _ in range(3):
            image_total = sum(s.duration for s in shots if not s.title_card)
            card_total = sum(s.duration for s in shots if s.title_card)
            factor = (target - card_total) / max(image_total, 0.1)
            for s in shots:
                if not s.title_card:
                    s.duration = round(min(max(s.duration * factor, 1.5), longest), 2)
        spoken = [s for s in shots if s.narration.strip()]
        keep = self.runtime.lines[1]
        if len(spoken) > keep:
            for s in spoken[keep - 1 : -1]:
                s.narration = ""
        return shots

    async def design_shots(self) -> None:
        assert self.treatment
        async with self.running("shots", required=False):
            self.shot_prompts = [prompts.fallback_shot_prompt(self.opts, self.treatment, s) for s in self.shots]
            image_shots = [(i, s) for i, s in enumerate(self.shots) if not s.title_card]
            batches = [image_shots[k : k + SHOT_PROMPT_BATCH] for k in range(0, len(image_shots), SHOT_PROMPT_BATCH)]
            improved = 0
            for n, batch in enumerate(batches):
                label = "Shot prompts" + (f" ({n + 1}/{len(batches)})" if len(batches) > 1 else "")
                try:
                    res = await self.structured(SHOT_PROMPTS_SCHEMA, prompts.shot_prompts(self.opts, self.treatment, [s for _, s in batch]),
                                                _ShotPrompts, "shots", label, Tier.DRAFT, 200 + 160 * len(batch))
                except Exception as e:
                    log.warning("shot prompt batch %s failed: %s", n, e)
                    continue
                indices = [p.index for p in res.prompts]
                shift = 1 if sorted(indices) == list(range(1, len(batch) + 1)) else 0
                by_index = {p.index - shift: p.prompt for p in res.prompts} if sorted(indices) in (
                    list(range(len(batch))), list(range(1, len(batch) + 1))) else {}
                for local, (i, _) in enumerate(batch):
                    text = by_index.get(local) or (res.prompts[local].prompt if local < len(res.prompts) else "")
                    if len(text) > 30:
                        self.shot_prompts[i] = f"{text}. {prompts.STYLES[self.opts.style]['prompt']}"
                        improved += 1
                self.set_stage("shots", "running", f"{improved} of {len(image_shots)} rewritten")
        if len(self.shot_prompts) != len(self.shots):
            self.shot_prompts = [prompts.fallback_shot_prompt(self.opts, self.treatment, s) for s in self.shots]

    async def cast(self) -> None:
        assert self.treatment
        async with self.running("cast", required=False):
            counts = {c.name: sum(c.name in s.characters for s in self.shots) for c in self.treatment.characters}
            saved = {c["name"] for c in self.state.get("library_cast") or []}
            appearing = [c for c in self.treatment.characters if counts.get(c.name, 0) > 0]
            main = [c for c in appearing if c.name in saved] + [c for c in appearing if c.name not in saved][:2]
            main = main[:3]
            if not main:
                raise StageSkipped("none of the characters appear on screen")
            cast_dir = self.dir / "cast"
            cast_dir.mkdir(exist_ok=True)
            for old in cast_dir.glob("*.png"):
                if old.stem not in {_slug(c.name) for c in main}:
                    old.unlink(missing_ok=True)

            returning = {c["name"]: Path(c["sheet"]) for c in self.state.get("library_cast") or []}

            async def one(c):
                if c.name in returning and returning[c.name].exists():
                    path = cast_dir / f"{_slug(c.name)}.png"
                    path.write_bytes(returning[c.name].read_bytes())
                    self.sheets[c.name] = b64e(path.read_bytes())
                    self.state["cast"].append({"name": c.name, "look": c.look, "image": self.url(path), "returning": True})
                    self.save()
                    return
                async def draw(fix: str) -> bytes:
                    prompt = prompts.character_sheet(self.opts, c.name, c.look) + (f" Important: {fix}." if fix else "")
                    res = await self.call(TaskKind.IMAGE, {"prompt": prompt, "size": "1024x1024", "n": 1}, "cast",
                                          f"Character sheet: {c.name}" + (" (redrawn)" if fix else ""), fresh=bool(fix))
                    return b64d(res.output["images"][0])

                raw, record = await self._clean_picture(draw, f"the character sheet for {c.name}", "cast")
                records.append(record)
                path = cast_dir / f"{_slug(c.name)}.png"
                png = _to_png(raw, path)
                self.sheets[c.name] = b64e(png)
                self.state["cast"].append({"name": c.name, "look": c.look, "image": self.url(path)})
                self.save()

            records: list[dict] = []
            await together(*(one(c) for c in main))
            self._record_pictures("Picture check: character sheets", records)

    async def frames(self) -> None:
        assert self.treatment
        frame_dir = self.dir / "frames"
        frame_dir.mkdir(exist_ok=True)
        image_idx = [i for i, s in enumerate(self.shots) if not s.title_card]
        for folder in ("frames", "masks", "depth"):
            for old in (self.dir / folder).glob("shot_*.png"):
                if not old.stem[5:].isdigit() or int(old.stem[5:]) not in image_idx:
                    old.unlink(missing_ok=True)
        done = 0
        sem = asyncio.Semaphore(4)
        review_sem = asyncio.Semaphore(3)
        self.set_stage("frames", "running", f"0/{len(image_idx)}")
        self.set_stage("review", "running", "waiting on frames")
        reviewed, regenerated = 0, 0
        generate = self._frame

        async def one(i: int) -> None:
            nonlocal done, reviewed, regenerated
            path = frame_dir / f"shot_{i:02}.png"
            async with sem:
                _to_png(await generate(i), path)
            done += 1
            self.state["shots"][i]["image"] = self.url(path)
            self.set_stage("frames", "running", f"{done}/{len(image_idx)}")
            async with review_sem:
                try:
                    review = await self._review(i, path)
                except Exception as e:
                    log.warning("review of shot %s failed: %s", i, e)
                    unchecked.append(i)
                    return
                reviewed += 1
                self.state["shots"][i]["review"] = review.model_dump()
                concerns = review.concerns
                if concerns:
                    flagged[i] = concerns
                if (self.opts.critic and review.score < REVIEW_PASS) or concerns:
                    first = review.score
                    fixes = [STRICT_FIX + "; " + (review.issue or "keep the shot as described"), STRICTER_FIX] \
                        if concerns else [review.issue or "match the shot description more closely"]
                    for fix in fixes:
                        async with sem:
                            _to_png(await generate(i, fix=fix), path)
                        regenerated += 1
                        with contextlib.suppress(Exception):
                            review = await self._review(i, path)
                        if not review.concerns:
                            break
                    if review.concerns:
                        review = await self._replace_with_setting(i, path)
                        if not review.concerns:
                            replaced.append(i)
                    self.state["shots"][i]["image"] = self.url(path)
                    self.state["shots"][i]["review"] = {**review.model_dump(), "regenerated": True, "first_score": first,
                                                        "rechecked": review.score != first or bool(review.issue)}
                    if review.concerns:
                        still[i] = review.concerns
                self.set_stage("review", "running", f"{reviewed} checked, {regenerated} redone")

        flagged: dict[int, list[str]] = {}
        still: dict[int, list[str]] = {}
        replaced: list[int] = []
        unchecked: list[int] = []
        try:
            await together(*(one(i) for i in image_idx))
        except Exception as e:
            self.set_stage("frames", "failed", str(e)[:300])
            self.set_stage("review", "failed", "frames failed")
            raise
        if reviewed or unchecked:
            self.picture_check(reviewed, flagged, still, replaced, unchecked)
        self.set_stage("frames", "done")
        self.set_stage("review", "done")
        if unchecked and profile()["safety"]["moderation"] == "required":
            raise ContentBlocked("The picture check is required, but these frames couldn't be checked: "
                                 + ", ".join(f"shot {i + 1}" for i in sorted(unchecked)))
        if still and profile()["safety"]["moderation"] == "required":
            raise ContentBlocked("Some frames still look like real people, brands or existing characters after being "
                                 "redrawn, so the film was stopped: " + ", ".join(f"shot {i + 1}" for i in sorted(still)))

    def picture_check(self, reviewed: int, flagged: dict[int, list[str]], still: dict[int, list[str]],
                      replaced: list[int] | None = None, unchecked: list[int] | None = None) -> None:
        """Record what the frame check found about real people, brands, existing characters and stray writing."""
        found = sorted({c for cs in flagged.values() for c in cs})
        swapped = f" Replaced with a shot of the setting: {', '.join(f'shot {i + 1}' for i in sorted(replaced))}." \
            if replaced else ""
        if still:
            verdict = "still flagged"
            note = "Still flagged after redrawing: " + "; ".join(f"shot {i + 1} ({', '.join(c)})" for i, c in still.items())
        elif flagged:
            verdict = "redrawn"
            note = (f"Redrawn to be original: shot{'s' if len(flagged) > 1 else ''} "
                    f"{', '.join(str(i + 1) for i in sorted(flagged))}.{swapped}")
        else:
            verdict = "passed"
            note = f"None of the {reviewed} frames showed a real person, a brand, an existing character or stray writing."
        if unchecked:
            verdict = verdict if still else "not checked"
            note += f" Not checked, as no vision model answered: {', '.join(f'shot {i + 1}' for i in sorted(unchecked))}."
        self.state.setdefault("checks", []).append({
            "check": "Picture check: frames", "by": "frame check (vision model)", "verdict": verdict,
            "categories": found, "review": note, "chunks": reviewed, "shots": sorted(still), "at": time.time(),
        })

    def _clear_flag(self, i: int) -> None:
        """A shot that was flagged has been redrawn and now passes: its old flag no longer holds the film back."""
        for c in self.state.get("checks", []):
            if i in c.get("shots", []):
                c["shots"].remove(i)
                c["review"] = f"{c.get('review', '')} Shot {i + 1} was redrawn and now passes.".strip()
                if not c["shots"] and c["verdict"] == "still flagged":
                    c["verdict"] = "resolved"

    async def _looks(self, png: bytes, what: str, stage: str, label: str) -> list[str] | None:
        """What a picture shows that it shouldn't; None if no vision model could check it."""
        img = Image.open(io.BytesIO(png)).convert("RGB")
        img.thumbnail((768, 768))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        payload = {"prompt": prompts.picture_check(what), "images": [b64e(buf.getvalue())],
                   "json_schema": PICTURE_SCHEMA, "max_tokens": 200, "temperature": 0.1}
        try:
            res = await self.call(TaskKind.TEXT, payload, stage, label, fresh=True)
            answer = res.output.get("json")
            if not isinstance(answer, dict) or not set(PICTURE_FIELDS) <= set(answer):
                raise ValueError("the checker didn't answer the picture questions")
            return PictureCheck.model_validate(answer).concerns
        except Exception as e:
            log.warning("picture check of %s failed: %s", what, e)
            return None

    async def _clean_picture(self, draw, what: str, stage: str, last_resort: str = "") -> tuple[bytes, dict]:
        """Draw a picture and check it, redrawing twice with stricter instructions while it shows something it
        shouldn't, then once more with last_resort if there is one. Returns the picture and what happened."""
        png = await draw("")
        record: dict = {"what": what, "concerns": [], "outcome": "passed"}
        for n, fix in enumerate([STRICT_FIX, STRICTER_FIX, None]):
            concerns = await self._looks(png, what, stage, f"Check {what}" + (f" (take {n + 1})" if n else ""))
            if concerns is None:
                record["outcome"] = "not checked"
                if profile()["safety"]["moderation"] == "required":
                    self._record_pictures(f"Picture check: {what}", [record])
                    raise ContentBlocked(f"The picture check is required, but no vision model could check {what}.")
                break
            if not concerns:
                record["outcome"] = "redrawn" if n else "passed"
                break
            record["concerns"] = sorted(set(record["concerns"]) | set(concerns))
            if fix is None:
                record["outcome"] = "still flagged"
                if last_resort:
                    replacement = await draw(last_resort)
                    if await self._looks(replacement, what, stage, f"Check {what} (last resort)") == []:
                        png, record["outcome"] = replacement, "redrawn"
                break
            png = await draw(fix)
        if record["outcome"] == "still flagged" and profile()["safety"]["moderation"] == "required":
            self._record_pictures(f"Picture check: {what}", [record])
            raise ContentBlocked(f"{what[0].upper()}{what[1:]} still showed {', '.join(record['concerns'])} after "
                                 "being redrawn twice, so the film was stopped.")
        return png, record

    def _record_pictures(self, check: str, records: list[dict]) -> None:
        if not records:
            return
        still = [r for r in records if r["outcome"] == "still flagged"]
        redrawn = [r for r in records if r["outcome"] == "redrawn"]
        unchecked = [r for r in records if r["outcome"] == "not checked"]
        verdict = "still flagged" if still else "redrawn" if redrawn else "not checked" if unchecked else "passed"
        note = ("; ".join(f"{r['what']}: {r['outcome']} ({', '.join(r['concerns'])})" for r in still + redrawn)
                or ("No vision model was available to check it." if unchecked else
                    "Nothing showed a real person, a brand, an existing character or stray writing."))
        self.state.setdefault("checks", []).append({
            "check": check, "by": "picture check (vision model)", "verdict": verdict,
            "categories": sorted({c for r in records for c in r["concerns"]}), "review": note, "chunks": len(records),
            "at": time.time(),
        })

    async def _replace_with_setting(self, i: int, path: Path) -> Review:
        """Last resort for a frame that keeps failing: the same moment shown as the place, with nobody in it."""
        assert self.treatment
        shot = self.shots[i]
        shot.visual = f"{self.treatment.setting}, the place itself with nobody in frame, no signs or logos"
        shot.characters = []
        self.state["shots"][i].update({"visual": shot.visual, "characters": [], "replaced": True})
        self.shot_prompts[i] = prompts.fallback_shot_prompt(self.opts, self.treatment, shot)
        _to_png(await self._frame(i, fix=STRICT_FIX, fresh=True), path)
        try:
            return await self._review(i, path)
        except Exception:
            return Review(real_person=True)

    async def idea_originality(self) -> None:
        """Stop ideas built on someone else's work, and offer an original version of the same idea."""
        if profile()["safety"]["moderation"] == "off":
            return
        payload = {"system": prompts.WRITER_SYSTEM, "json_schema": originality.IDEA_SCHEMA, "max_tokens": 600,
                   "temperature": 0.2, "prompt": originality.idea_prompt(self.opts.prompt)}
        try:
            res = await self.call(TaskKind.TEXT, payload, "safety", "Check the idea is original")
            check = originality.IdeaCheck.model_validate(res.output.get("json") or {})
        except Exception as e:
            log.warning("originality check failed: %s", e)
            if profile()["safety"]["moderation"] == "required":
                raise ContentBlocked("The originality check is required, but no model could run it.") from e
            self._record_originality("not checked", "No model was available to check it.")
            return
        problems = [p.strip() for p in check.problems if p.strip()]
        if check.original is None and not problems:
            if profile()["safety"]["moderation"] == "required":
                raise ContentBlocked("The originality check is required, but the checker gave no verdict.")
            self._record_originality("not checked", "The checker gave no verdict.")
            return
        if check.original is True and not problems:
            self._record_originality("passed", "Nothing in the idea belongs to an existing work or a real person.")
            return
        problems = problems or ["The checker judged that the idea builds on an existing work."]
        rewrite = " ".join(check.rewrite.split())[:1200]
        self.state["originality"] = {"problems": problems, "rewrite": rewrite}
        self._record_originality("blocked", " ".join(problems))
        self.set_stage("safety", "failed", "the idea builds on existing work")
        raise ContentBlocked("This idea builds on someone else's work: " + " ".join(problems)
                             + (f' An original version: "{rewrite}"' if rewrite else ""))

    async def story_originality(self) -> None:
        """Rename borrowed titles and names, and rewrite quoted lines, before anything is drawn or spoken."""
        if profile()["safety"]["moderation"] == "off":
            return
        assert self.treatment
        check = "Originality check: title, names, lines"
        changed: list[str] = []
        unmatched: list[str] = []
        for n in (1, 2):
            payload = {"system": prompts.WRITER_SYSTEM, "json_schema": originality.STORY_SCHEMA, "max_tokens": 1200,
                       "temperature": 0.2, "prompt": originality.story_prompt(self.treatment, self.shots)}
            label = "Check the story is original" + (", second look" if n > 1 else "")
            try:
                res = await self.call(TaskKind.TEXT, payload, "safety", label, fresh=n > 1)
                issues = originality.StoryCheck.model_validate(res.output.get("json") or {}).issues
            except Exception as e:
                log.warning("story originality check failed: %s", e)
                if profile()["safety"]["moderation"] == "required":
                    raise ContentBlocked("The originality check is required, but no model could check the story.") from e
                if n == 1:
                    self._record_originality("not checked", "No model was available to check the story.", check)
                    return
                break
            found = originality.apply(self.treatment, self.shots, issues, unmatched)
            changed += found
            if not found:
                break
        if changed:
            self.state["story"] = self.treatment.model_dump()
            for i, s in enumerate(self.shots):
                self.state["shots"][i].update({"visual": s.visual, "narration": s.narration, "characters": s.characters,
                                               "speaker": s.speaker})
            self.write_script()
        note = ("Changed: " + "; ".join(changed) + ".") if changed else "The story, names and lines are all original."
        if unmatched:
            note += " Flagged but not found in the story, so left alone: " + ", ".join(dict.fromkeys(unmatched)) + "."
        self._record_originality("renamed" if changed else "allowed with notes" if unmatched else "passed", note, check)

    def write_script(self) -> None:
        """The downloadable screenplay, from the shots as they stand now."""
        assert self.treatment
        path = self.dir / "script.md"
        path.write_text(prompts.screenplay_markdown(self.treatment, Script(scenes=[Scene(heading="Script", shots=list(self.shots))]),
                                                    self.opts, self.runtime), encoding="utf-8")
        self.state["assets"]["script"] = self.url(path)

    def _record_originality(self, verdict: str, note: str, check: str = "Originality check: the idea") -> None:
        self.state.setdefault("checks", []).append({
            "check": check, "by": "originality check (language model)", "verdict": verdict, "categories": [],
            "review": note, "chunks": 1, "at": time.time(),
        })
        self.save()

    async def _frame(self, i: int, fix: str = "", fresh: bool = False) -> bytes:
        """Generate one storyboard frame, anchored on character sheets when the shot has characters."""
        assert self.treatment
        shot = self.shots[i]
        refs = [self.sheets[n] for n in shot.characters if n in self.sheets][:2]
        base = self.shot_prompts[i] + (f" Important fix: {fix}." if fix else "")
        prompt = prompts.frame_prompt(self.opts, self.treatment, shot, base, bool(refs))
        payload = {"prompt": prompt, "size": self.aspect["image"], "n": 1}
        label = f"Frame {i + 1}" + (" (redone)" if fix or fresh else "")
        self.state["shots"][i]["prompt"] = prompt
        self.state["shots"][i]["shot_prompt"] = self.shot_prompts[i]
        if refs:
            try:
                res = await self.call(TaskKind.IMAGE, {**payload, "references": refs}, "frames", label, fresh=fresh)
                return b64d(res.output["images"][0])
            except FabricError as e:
                log.warning("frame %s with references failed (%s); retrying without", i, e)
        try:
            res = await self.call(TaskKind.IMAGE, payload, "frames", label, fresh=fresh)
        except FabricError as e:
            if not refused(e):
                raise
            return await self._redraw_refused(i, payload, label, e, fix)
        return b64d(res.output["images"][0])

    async def _redraw_refused(self, i: int, payload: dict, label: str, error: FabricError, fix: str = "") -> bytes:
        """The picture model's content policy turned the prompt down: draw the shot plainly as the script describes it,
        then, if that is turned down too, the setting with nobody in it. The shot records which one was used."""
        assert self.treatment
        shot = self.shots[i]
        setting = shot.model_copy(update={"visual": f"{self.treatment.setting}, the place itself with nobody in frame, "
                                                    "no signs or logos", "characters": []})
        for how, version in (("plainer", shot), ("setting", setting)):
            log.warning("frame %s was refused by the picture model's policy (%s); drawing the %s version", i, error, how)
            base = prompts.fallback_shot_prompt(self.opts, self.treatment, version)
            prompt = prompts.frame_prompt(self.opts, self.treatment, version,
                                          base + (f" Important fix: {fix}." if fix else ""), False)
            try:
                res = await self.call(TaskKind.IMAGE, {**payload, "prompt": prompt}, "frames", f"{label} ({how})", fresh=True)
            except FabricError as e:
                if not refused(e):
                    raise
                error = e
                continue
            if version is setting:
                self.shots[i] = setting
                self.state["shots"][i].update({"visual": setting.visual, "characters": [], "replaced": True})
            self.shot_prompts[i] = base
            self.state["shots"][i].update({"prompt": prompt, "shot_prompt": base, "refused": how})
            return b64d(res.output["images"][0])
        raise error

    async def _review(self, i: int, path: Path) -> Review:
        img = Image.open(path).convert("RGB")
        img.thumbnail((768, 768))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        payload = {"prompt": prompts.review(self.shots[i], self.shot_prompts[i]), "images": [b64e(buf.getvalue())],
                   "json_schema": REVIEW_SCHEMA, "max_tokens": 300, "temperature": 0.2}
        res = await self.call(TaskKind.TEXT, payload, "review", f"Review frame {i + 1}")
        answer = res.output.get("json")
        if not isinstance(answer, dict) or "score" not in answer:
            raise ValueError("the reviewer returned no score")
        if not set(PICTURE_FIELDS) <= set(answer):
            raise ValueError("the reviewer didn't answer the picture questions")
        return Review.model_validate(answer)

    async def poster(self) -> None:
        assert self.treatment
        async with self.running("poster"):
            refs = list(self.sheets.values())[:2]

            async def draw(fix: str) -> bytes:
                prompt = prompts.poster_art(self.opts, self.treatment) + (f" Important: {fix}." if fix else "")
                payload = {"prompt": prompt, "size": "1024x1536", "n": 1}
                label = "Poster art" + (" (redrawn)" if fix else "")
                res = None
                if refs and fix != prompts.POSTER_NO_PEOPLE:
                    with contextlib.suppress(FabricError):
                        res = await self.call(TaskKind.IMAGE, {**payload, "references": refs}, "poster", label, fresh=bool(fix))
                if res is None:
                    res = await self.call(TaskKind.IMAGE, payload, "poster", label, fresh=bool(fix))
                return b64d(res.output["images"][0])

            raw, record = await self._clean_picture(draw, "the poster art", "poster", prompts.POSTER_NO_PEOPLE)
            self._record_pictures("Picture check: poster", [record])
            _to_png(raw, self.dir / "poster_art.png")
            await self._typeset_poster()

    async def _typeset_poster(self) -> None:
        """Lay the title, tagline and credits over the existing poster art."""
        assert self.treatment
        from .media import typography

        t = self.treatment
        starring = " and ".join(c.name for c in t.characters[:2])
        used, _ = await self.models_used()
        made = [f"{role} {', '.join(used[s])}" for s, role in (("script", "written with"), ("frames", "storyboard"))
                if used.get(s)]
        presenter = profile().get("export", {}).get("presenter", "")
        credits = "   ".join(([f"{presenter} presents"] if presenter else []) +
                             ([f"starring {starring}"] if starring else []) + made)
        img = await asyncio.to_thread(typography.poster, Image.open(self.dir / "poster_art.png"), t.title,
                                      t.tagline or t.logline, credits, provenance.LABEL)
        path = self.dir / "poster.png"
        path.write_bytes(provenance.png_bytes(img, t.title))
        self.state["assets"]["poster"] = self.url(path)

    async def depth(self) -> None:
        async with self.running("depth", required=False):
            mask_dir = self.dir / "masks"
            mask_dir.mkdir(exist_ok=True)
            pictures = [i for i, s in enumerate(self.shots) if not s.title_card]
            layered = 0
            for n, i in enumerate(pictures):
                layered += await self._depth_map(i)
                self.set_stage("depth", "running", f"depth for {n + 1} of {len(pictures)}")
            self.state["depth_layers"] = layered
            targets = [i for i, s in enumerate(self.shots)
                       if not s.title_card and s.camera in render.PARALLAX_MOVES and subject_for(s)]
            if not targets:
                if layered:
                    self.set_stage("depth", "running", f"{layered} shots in depth layers")
                    return
                raise StageSkipped("no shots need it")
            sem = asyncio.Semaphore(2)
            done = 0

            async def one(i: int):
                nonlocal done
                frame = self.dir / "frames" / f"shot_{i:02}.png"
                concept = subject_for(self.shots[i])
                async with sem:
                    try:
                        res = await self.call(TaskKind.SEGMENT, {"image": b64e(frame.read_bytes()), "concept": concept},
                                              "depth", f"Matte shot {i + 1}")
                    except FabricError as e:
                        log.info("no matte for shot %s: %s", i, e)
                        return
                path = mask_dir / f"shot_{i:02}.png"
                path.write_bytes(b64d(res.output["mask"]))
                self.state["shots"][i]["mask"] = self.url(path)
                done += 1
                self.set_stage("depth", "running", f"{done} of {len(targets)}")

            await together(*(one(i) for i in targets))

    async def _depth_map(self, i: int, fresh: bool = False) -> int:
        """Ask a local depth model for one frame's depth; 1 if it worked, 0 if no machine could."""
        path = self.dir / "depth" / f"shot_{i:02}.png"
        try:
            res = await self.call(TaskKind.DEPTH, {"image": b64e((self.dir / "frames" / f"shot_{i:02}.png").read_bytes())},
                                  "depth", f"Depth shot {i + 1}", fresh=fresh)
            data = b64d(res.output["depth"])
        except (FabricError, KeyError) as e:
            log.info("no depth for shot %s: %s", i, e)
            path.unlink(missing_ok=True)
            return 0
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        return 1

    async def voice(self) -> None:
        async with self.running("voice", required=False):
            lines = [(i, s.narration.strip()) for i, s in enumerate(self.shots) if s.narration.strip()]
            if not self.opts.narration or not lines:
                raise StageSkipped("turned off" if not self.opts.narration else "the script has no voice-over")
            if getattr(self.opts, "commercial_safe", False):
                raise StageSkipped("commercial-safe: the lines are shown as captions, since the voice models are "
                                   "licensed for non-commercial use only")
            audio_dir = self.dir / "audio"
            audio_dir.mkdir(exist_ok=True)
            voices = self.voices()
            spoken = await self._spoken_lines(lines)
            for n, (i, text) in enumerate(lines):
                voice = voices.get(self.shots[i].speaker.strip().lower(), voices["narrator"])
                res = await self.call(TaskKind.TTS, {"text": spoken.get(i, text), "speed": voice["speed"],
                                                     "pitch": voice["pitch"], "language": self._voice_code()},
                                      "voice", f"Line {n + 1} ({voice['name']})")
                path = audio_dir / f"vo_{i:02}.wav"
                path.write_bytes(b64d(res.output["audio"]))
                self.voice_lines.append((i, path, float(res.output.get("duration_s", 0))))
                self.set_stage("voice", "running", f"{n + 1} of {len(lines)} lines")

    def _voice_code(self) -> str:
        code = getattr(self.opts, "voice_language", "en")
        return VOICE_LANGUAGES[code][1] if code in VOICE_LANGUAGES and code != "en" and self.state.get("voice_text") else "eng"

    async def _spoken_lines(self, lines: list[tuple[int, str]]) -> dict[int, str]:
        """The voice-over in the chosen language, keyed by shot; empty means English."""
        code = getattr(self.opts, "voice_language", "en")
        self.state["voice_text"] = {}
        if code == "en":
            return {}
        name = VOICE_LANGUAGES[code][0]
        translated = await self._translate([t for _, t in lines], code, "voice", f"Voice-over in {name}")
        if translated is None:
            self.state["voice_note"] = f"The {name} translation didn't pass the check, so the voice-over is in English."
            return {}
        spoken = {i: t for (i, _), t in zip(lines, translated, strict=False)}
        self.state["voice_text"] = {str(i): t for i, t in spoken.items()}
        return spoken

    def voices(self) -> dict[str, dict]:
        """One voice per speaker: the narrator, and each character pitched by who they are."""
        voices = {"narrator": {"name": "narrator", "pitch": 1.0, "speed": 0.95}}
        for c in (self.treatment.characters if self.treatment else []):
            look = f" {c.look.lower()} "
            if any(w in look for w in (" girl", " boy", " child", " kid", " teen")):
                pitch = 1.22
            elif any(w in look for w in (" woman", " female", " she ", " grandmother", " mother", " lady")):
                pitch = 1.12
            elif any(w in look for w in (" man", " male", " he ", " grandfather", " father")):
                pitch = 0.86
            else:
                pitch = 1.06 if len(voices) % 2 else 0.92
            age = re.search(r"(\d{1,3})[- ]?(?:year|yr)s?[- ]old|\bin (?:his|her|their) (\d0)s\b|\b(\d{1,2})s?\b(?= ?years)", look)
            years = int(next(g for g in age.groups() if g)) if age else None
            if years is not None and years < 13:
                pitch = 1.22
            old = (years is not None and years >= 60) or any(
                w in look for w in (" elderly", " grey-haired", " gray-haired", " white-haired", " retired", " old man",
                                    " old woman", " grandfather", " grandmother"))
            voices[c.name.lower()] = {"name": c.name, "pitch": pitch * (0.95 if old else 1.0), "speed": 0.88 if old else 0.97}
        self.state["voices"] = {v["name"]: {"pitch": round(v["pitch"], 2), "speed": v["speed"]} for v in voices.values()}
        return voices

    async def score(self) -> None:
        assert self.treatment
        async with self.running("score", required=False):
            total = sum(s.duration for s in self.shots) + FINAL_CARD_S
            path = self.dir / "audio" / "score.wav"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(await self._score_in_sections(total))
            self.score_path = path

    async def _score_in_sections(self, total: float) -> bytes:
        """A score that follows the story: each section takes its cue from that stage of the film and continues from the
        end of the last one. A worker that returns the whole length in one go (the built-in score) is taken as it is."""
        assert self.treatment
        cues = self.treatment.music_cues or [""]
        sections: list[np.ndarray] = []
        rate, made, n = 0, 0.0, 0
        while made < total - 1:
            cue = cues[min(int(made / total * len(cues)), len(cues) - 1)]
            payload: dict = {"prompt": prompts.music(self.treatment, self.opts, cue), "duration_s": total - made}
            if sections:
                payload["continue_from"] = b64e(wav_bytes(sections[-1][-int(MUSIC_OVERLAP_S * rate):], rate))
            label = "Score" if total <= MUSIC_SECTION_S else f"Score, section {n + 1}"
            try:
                samples, sr = read_wav(b64d((await self.call(TaskKind.MUSIC, payload, "score", label)).output["audio"]))
                if audio_mix.is_silent(samples):
                    log.warning("score section %s came out silent; trying once more", n + 1)
                    samples, sr = read_wav(b64d((await self.call(TaskKind.MUSIC, payload, "score", label + " (again)",
                                                                 fresh=True)).output["audio"]))
            except FabricError:
                if not sections:
                    raise
                log.warning("score section %s failed; the mix repeats what there is", n + 1)
                break
            if not audio_mix.is_silent(samples):
                rate = rate or sr
                sections.append(audio_mix.to_rate(samples, sr, rate) if sr != rate else samples)
                made += len(samples) / sr
                self.set_stage("score", "running", f"{min(made, total):.0f} of {total:.0f} s")
            n += 1
            if n >= MUSIC_MAX_SECTIONS:
                break
        if not sections:
            raise RuntimeError("the music model returned only silence")
        if made < total - 1:
            self.set_stage("score", "running", f"{made:.0f} of {total:.0f} s made; the mix repeats it")
        return wav_bytes(audio_mix.join_score(sections, sr=rate), rate)

    async def qa(self) -> None:
        async with self.running("qa", required=False):
            if not self.voice_lines:
                raise StageSkipped("no voice-over to check")
            heard, ratio = await self._hear([p for _, p, _ in self.voice_lines], self._expected(), "Transcribe narration")
            self.state["qa"] = {"transcript": heard, "similarity": round(ratio, 3)}
            if ratio < QA_PASS:
                redone = await self._revoice_unclear()
                if redone:
                    heard, ratio = await self._hear([p for _, p, _ in self.voice_lines], self._expected(),
                                                    "Transcribe narration again")
                    self.state["qa"] = {"transcript": heard, "similarity": round(ratio, 3),
                                        "first_similarity": self.state["qa"]["similarity"], "redone_lines": redone}
            self.set_stage("qa", "running", f"{ratio:.0%} of words matched"
                           + (f" after re-voicing {len(self.state['qa']['redone_lines'])}" if "redone_lines" in self.state["qa"]
                              else ""))

    def _expected(self, only: int | None = None) -> str:
        spoken = self.state.get("voice_text") or {}
        return " ".join(spoken.get(str(i)) or self.shots[i].narration for i, _, _ in self.voice_lines
                        if only is None or i == only)

    async def _hear(self, paths: list[Path], expected: str, label: str) -> tuple[str, float]:
        """Transcribe voice-over audio and score how much of the expected text came through."""
        chunks, sr = [], 16000
        for path in paths:
            samples, rate = read_wav(path.read_bytes())
            chunks += [audio_mix.to_rate(samples, rate, sr), np.zeros(int(0.4 * sr), dtype=np.float32)]
        payload = {"audio": b64e(wav_bytes(np.concatenate(chunks), sr)), "duration_s": sum(len(c) for c in chunks) / sr}
        if self.state.get("voice_text"):
            payload["language"] = VOICE_LANGUAGES[self.opts.voice_language][0]
        res = await self.call(TaskKind.TRANSCRIBE, payload, "qa", label)
        heard = res.output.get("text", "")
        return heard, difflib.SequenceMatcher(None, _words(expected), _words(heard)).ratio()

    async def _revoice_unclear(self) -> list[dict]:
        """Speak unclear lines again, a little slower; a new take is kept only if more of it is heard."""
        voices, redone = self.voices(), []
        for n, (i, path, _) in enumerate(list(self.voice_lines)):
            _, before = await self._hear([path], self._expected(only=i), f"Check line {n + 1}")
            if before >= QA_LINE_PASS:
                continue
            voice = voices.get(self.shots[i].speaker.strip().lower(), voices["narrator"])
            text = (self.state.get("voice_text") or {}).get(str(i)) or self.shots[i].narration
            res = await self.call(TaskKind.TTS, {"text": text, "speed": round(voice["speed"] * 0.88, 2), "pitch": voice["pitch"],
                                                 "language": self._voice_code()}, "qa", f"Re-voice line {n + 1}", fresh=True)
            take = path.with_name(f"{path.stem}_take2.wav")
            take.write_bytes(b64d(res.output["audio"]))
            _, after = await self._hear([take], self._expected(only=i), f"Check line {n + 1} again")
            if after > before:
                take.replace(path)
                self.voice_lines = [(j, p, float(res.output.get("duration_s", s)) if j == i else s)
                                    for j, p, s in self.voice_lines]
                redone.append({"shot": i + 1, "before": round(before, 3), "after": round(after, 3)})
            else:
                take.unlink(missing_ok=True)
        return redone

    def timeline(self) -> tuple[list[render.Clip], list[tuple[float, float, str]], list[tuple[float, Path]], list[float], float]:
        assert self.treatment
        clips, starts, t = [], [], 0.0
        impacts = []
        for i, s in enumerate(self.shots):
            starts.append(t)
            if s.title_card:
                clips.append(render.Clip(s.duration, card_text=s.title_card))
                impacts.append(t)
            else:
                mask = self.dir / "masks" / f"shot_{i:02}.png"
                depth = self.dir / "depth" / f"shot_{i:02}.png"
                clips.append(render.Clip(s.duration, s.camera, self.dir / "frames" / f"shot_{i:02}.png",
                                         mask if mask.exists() else None, depth=depth if depth.exists() else None))
            t += s.duration
        impacts.append(t)
        subs, placed, cursor = [], [], 0.0
        for i, path, seconds in self.voice_lines:
            start = max(starts[i] + 0.15, cursor + 0.2)
            placed.append((start, path))
            subs.append((start, start + seconds, self.shots[i].narration.strip()))
            cursor = start + seconds
        if not self.voice_lines and self.opts.narration:
            subs = [(starts[i] + 0.2, starts[i] + s.duration - 0.1, s.narration.strip())
                    for i, s in enumerate(self.shots) if s.narration.strip() and s.duration > 0.6]
        body = max(t, cursor + 0.3)
        if body > t:
            last = max((k for k, c in enumerate(clips) if c.image), default=len(clips) - 1)
            clips[last].duration += body - t
        clips.append(render.Clip(FINAL_CARD_S, card_text=self.treatment.title, card_sub=self.treatment.tagline,
                                 card_note=f"{provenance.LABEL} · {provenance.TOOL}"))
        return clips, subs, placed, impacts, body + FINAL_CARD_S

    async def edit(self) -> None:
        async with self.running("edit"):
            clips, subs, placed, impacts, total = self.timeline()
            narration = []
            for start, path in placed:
                samples, sr = read_wav(path.read_bytes())
                narration.append((start, samples, sr))
            score = read_wav(self.score_path.read_bytes()) if self.score_path else None
            mixed = await asyncio.to_thread(audio_mix.mix, total, narration, score, impacts)
            mix_path = self.dir / "audio" / "mix.wav"
            mix_path.parent.mkdir(exist_ok=True)
            mix_path.write_bytes(wav_bytes(mixed, audio_mix.OUT_SR))
            await self.watermark(mix_path)
            out = self.dir / "film.mp4"
            for stale in ("commentary", "commentary_vtt", "exports", "trailer"):
                self.state["assets"].pop(stale, None)
            for old in stale_versions(self.dir):
                old.unlink()
            loop = asyncio.get_running_loop()

            def progress(p: float):
                st = self.stage("edit")
                st["detail"] = f"rendering, {p:.0%}"
                loop.call_soon_threadsafe(self.save, False)

            await asyncio.to_thread(render.render_film, clips, self.video, mix_path, out, subs, self.fps, 0.3, progress,
                                    provenance.ffmpeg_tags(self.treatment.title if self.treatment else ""), tune=self.tune)
            if subs:
                render.write_srt(subs, self.dir / "subtitles.srt")
                self.state["assets"]["subtitles"] = self.url(self.dir / "subtitles.srt")
                await self.translate_subtitles(subs)
            self.state["assets"]["video"] = self.url(out)
            self.state["assets"]["video_seconds"] = round(total, 1)
            self.set_stage("edit", "running", f"{total:.0f}s at {self.video[0]}x{self.video[1]}, {self.fps} fps")

    async def translate_subtitles(self, subs: list[tuple[float, float, str]]) -> None:
        """Caption tracks in the languages the user picked; a language that can't be translated is skipped, not fatal."""
        assert self.treatment
        tracks = []
        english = self.dir / "subtitles.en.vtt"
        render.write_vtt(subs, english)
        tracks.append({"lang": "en", "label": "English", "vtt": self.url(english), "srt": self.state["assets"]["subtitles"]})
        lines = [text for _, _, text in subs]
        skipped: list[str] = []
        for old in [*self.dir.glob("subtitles.*.srt"), *self.dir.glob("subtitles.*.vtt")]:
            if old.name != "subtitles.en.vtt":
                old.unlink(missing_ok=True)
        for code in getattr(self.opts, "subtitle_languages", []):
            language = LANGUAGES[code]
            translated = await self._translate(lines, code, "edit", f"Subtitles in {language}")
            if translated is None:
                skipped.append(language)
                continue
            timed = [(a, b, text) for (a, b, _), text in zip(subs, translated, strict=False)]
            srt, vtt = self.dir / f"subtitles.{code}.srt", self.dir / f"subtitles.{code}.vtt"
            render.write_srt(timed, srt)
            render.write_vtt(timed, vtt)
            tracks.append({"lang": code, "label": language, "vtt": self.url(vtt), "srt": self.url(srt)})
        self.state["assets"]["subtitle_tracks"] = tracks
        self.state["assets"]["subtitles_skipped"] = skipped

    async def _translate(self, lines: list[str], code: str, stage: str, label: str) -> list[str] | None:
        """Translate lines, checked; a local model first, then one retry in the cloud. None if neither passes."""
        assert self.treatment
        language = LANGUAGES.get(code) or VOICE_LANGUAGES[code][0]
        prompt = prompts.translate(lines, language, self.treatment)
        for attempt in range(2):
            payload = {"system": prompts.TRANSLATOR_SYSTEM, "prompt": prompt, "json_schema": TRANSLATION_SCHEMA,
                       "max_tokens": 200 + 120 * len(lines), "temperature": 0.2}
            try:
                res = await self.call(TaskKind.TEXT, payload, stage, label + (" (retry in the cloud)" if attempt else ""),
                                      fresh=bool(attempt), preference=Preference.CLOUD_FIRST if attempt else None)
            except FabricError as e:
                log.info("%s: attempt %d failed: %s", label, attempt + 1, e)
                continue
            found = (res.output.get("json") or {}).get("lines", [])
            problem = translation_problem(lines, found, code)
            if problem is None:
                clean = [_clean_line(x) for x in found]
                return await self.review_translation(lines, clean, code, stage) if self.crew else clean
            log.info("%s rejected: %s", label, problem)
            prompt = f"{prompts.translate(lines, language, self.treatment)}\n\nYour last answer was rejected: {problem}."
        return None

    async def watermark(self, mix_path: Path) -> None:
        """Mark the soundtrack with AudioSeal. The 16 bits come from the job id, so the film can be traced back."""
        bits = [int(b) for b in f"{int(hashlib.sha256(self.id.encode()).hexdigest(), 16) & 0xFFFF:016b}"]
        try:
            res = await self.call(TaskKind.WATERMARK, {"audio": b64e(mix_path.read_bytes()), "bits": bits}, "edit",
                                  "Watermark soundtrack", fresh=True)
            marked = b64d(res.output["audio"])
            detected = float(res.output.get("detected", 0.0))
        except (FabricError, KeyError, TypeError, ValueError) as e:
            log.info("soundtrack not watermarked: %s", e)
            self.state["watermark"] = {"method": "AudioSeal", "applied": False, "reason": str(e)[:200]}
            return
        mix_path.write_bytes(marked)
        self.state["watermark"] = {
            "method": "AudioSeal", "applied": True, "model": res.output.get("model"), "payload": "".join(map(str, bits)),
            "detection": round(detected, 3), "verified": detected > 0.9 and bool(res.output.get("bits_match")),
        }

    async def package(self) -> None:
        async with self.running("package"):
            await self.write_records()
            await self.make_passport()
            if self.opts.format == "movie":
                try:
                    await self.cut_trailer()
                except Exception as e:
                    log.warning("no trailer cut for %s: %s", self.id, e)
            await self.sign(["film.mp4", "trailer.mp4", "poster.png"])
            zip_path = await asyncio.to_thread(write_zip, self.dir, self.treatment.title if self.treatment else self.id)
            self.state["assets"]["zip"] = self.url(zip_path)
            await self.upload()

    async def upload(self) -> None:
        """Copy the finished files to the media bucket, if one is configured; a failure is noted, not fatal."""
        from ..config import get_settings
        from .media.storage import media_store

        bucket = media_store(get_settings())
        if bucket is None:
            return
        try:
            count = await asyncio.to_thread(bucket.upload_job, self.dir, self.id)
            self.state["storage"] = {"bucket": bucket.bucket, "files": count, "at": time.time()}
        except Exception as e:
            log.warning("upload of job %s failed: %s", self.id, e)
            self.state["storage"] = {"bucket": bucket.bucket, "error": str(e)[:200]}


    async def write_records(self) -> None:
        """story.json, CREDITS.txt and manifest.json, from the film as it stands and its full routing log."""
        story = {"treatment": self.state["story"], "shots": [s.model_dump() for s in self.shots], "qa": self.state["qa"],
                 "options": self.state["options"]}
        (self.dir / "story.json").write_text(json.dumps(story, indent=2), encoding="utf-8")
        used, licences = await self.models_used()
        (self.dir / "CREDITS.txt").write_text(credits_text(used, licences), encoding="utf-8")
        (self.dir / "manifest.json").write_text(json.dumps(self.manifest(used, licences), indent=2), encoding="utf-8")

    def manifest(self, used: dict[str, list[str]], licences: dict[str, str]) -> dict:
        """What made this film and what checked it, for anyone the files are passed on to."""
        names = dict(STAGES)
        reviews = [{"shot": s["index"] + 1, "score": s["review"]["score"], "redone": bool(s["review"].get("regenerated"))}
                   for s in self.state["shots"] if s.get("review")]
        return {
            "generator": provenance.TOOL,
            "label": provenance.LABEL,
            "digital_source_type": provenance.SOURCE_TYPE,
            "job_id": self.id,
            "created": self.state["created"],
            "title": self.treatment.title if self.treatment else None,
            "format": self.state["runtime"].get("label"),
            "models": [{"stage": names.get(stage, stage), "model": m, "licence": licences.get(m, "unknown")}
                       for stage, models in used.items() for m in models],
            "checks": self.state.get("checks", []),
            "frame_reviews": reviews,
            "narration_check": self.state.get("qa"),
            "watermark": self.state.get("watermark"),
            "content_credentials": {k: v for k, v in (self.state.get("credentials") or {}).items() if k != "signed"}
            | {"files": sorted((self.state.get("credentials") or {}).get("signed", {}))},
            "agents": [{k: v for k, v in turn.items() if k != "at"} for turn in self.state.get("agents", [])],
        }

    async def make_passport(self) -> None:
        """A one-card summary of the film from its ledger receipt and routing log; skipped if those can't be read."""
        from .media import passport

        try:
            receipt = await self.fabric.get(f"/v1/ledger/jobs/{self.id}")
            decisions = (await self.fabric.get("/v1/decisions", job_id=self.id, limit=10000))["decisions"]
        except Exception as e:
            log.info("no passport for %s: %s", self.id, e)
            return
        f = passport.facts(self.state, receipt, decisions)
        voice = VOICE_LANGUAGES.get(f["voice"], ("English",))[0]
        img = await asyncio.to_thread(passport.render, self.dir / "poster_art.png", f, voice)
        path = self.dir / "passport.png"
        path.write_bytes(provenance.png_bytes(img, f"{f['title']} · Production Passport"))
        self.state["assets"]["passport"] = self.url(path)
        self.state["passport"] = f

    async def models_used(self) -> tuple[dict[str, list[str]], dict[str, str]]:
        """Models each stage actually ran on (from the routing log) and their licences."""
        try:
            decisions = (await self.fabric.get("/v1/decisions", job_id=self.id, limit=10000))["decisions"]
            workers = (await self.fabric.get("/v1/workers"))["workers"]
        except Exception:
            return {}, {}
        licences = {o["model"]: o["licence"] for w in workers for o in w["offers"]}
        used: dict[str, list[str]] = {}
        for d in reversed(decisions):
            if d.get("status") == "ok" and d.get("model") and d.get("stage"):
                models = used.setdefault(d["stage"], [])
                if d["model"] not in models:
                    models.append(d["model"])
                if d.get("licence"):
                    licences[d["model"]] = d["licence"]
        return used, {m: licences.get(m, "unknown") for ms in used.values() for m in ms}

    async def run(self) -> None:
        self.state["status"] = "running"
        self.state["error"] = None
        self.save()
        try:
            async with self.running("safety"):
                await self.check_content(self.opts.prompt, None, "Check the idea")
                await self.idea_originality()
            await self.story()
            await self.script()
            assert self.treatment
            await self.story_originality()
            await self.check_content(_story_text(self.treatment) + "\n" + _script_text(self.shots), self.opts.prompt,
                                     "Check the story and script")
            if self.opts.review_script:
                await self.wait_for_review()
                await self.check_content(_script_text(self.shots), self.opts.prompt, "Check the edited script")
            await self.design_shots()
            await self.check_content("\n".join(self.shot_prompts), self.opts.prompt, "Check the picture prompts")

            async def visual_track():
                await self.cast()
                await self.frames()
                if self.crew:
                    await self.continuity()
                await together(self.poster(), self.depth())

            async def audio_track():
                await self.voice()
                await together(self.score(), self.qa())

            await together(visual_track(), audio_track())
            if self.crew:
                await self.editor()
            await self.edit()
            await self.package()
            if self.crew:
                await self.test_screening()
                await self.release()
            self.state["status"] = "done"
        except Exception as e:
            log.exception("job %s failed", self.id)
            self.state["status"] = "failed"
            self.state["error"] = _friendly_error(e, self.opts)
        finally:
            with contextlib.suppress(Exception):
                self.state["receipt"] = await self.fabric.get(f"/v1/ledger/jobs/{self.id}")
            self.save()


    def stopped(self) -> None:
        """Someone stopped the film: keep what finished, and show the rest as stopped."""
        for st in self.state["stages"]:
            if st["status"] == "running":
                st["status"], st["detail"] = "failed", "stopped"
        self.state["status"] = "failed"
        self.state["error"] = "Stopped before it finished."
        with contextlib.suppress(Exception):
            self.save()

    async def check_content(self, text: str, context: str | None, label: str) -> None:
        safety = profile()["safety"]
        st = self.stage("safety")
        if safety["moderation"] == "off":
            self.set_stage("safety", "skipped", "turned off")
            return
        chunks = [text[i:i + MODERATION_CHUNK] for i in range(0, len(text), MODERATION_CHUNK)] or [""]
        flagged: list[str] = []
        suspect: list[str] = []
        unsafe = False
        failure: FabricError | None = None
        for n, chunk in enumerate(chunks):
            part = label if len(chunks) == 1 else f"{label} ({n + 1}/{len(chunks)})"
            try:
                res = await self.call(TaskKind.MODERATE, {"text": chunk, "context": context}, "safety", part)
            except FabricError as e:
                failure = e
                break
            found = res.output.get("categories", [])
            flagged += [c for c in found if c not in flagged]
            if found or res.output.get("safe") is False:
                suspect.append(chunk)
            unsafe = unsafe or res.output.get("safe") is False
        blocked = [c for c in flagged if c in set(safety["block"])]
        if unsafe and not flagged:
            blocked = ["an unlisted category"]
        review = await self._review_chunks(suspect, flagged) if suspect and not failure else None
        if review is not None:
            blocked = [review["category"] or "an unlisted category"] if review["harmful"] else []
        cleared = (review is not None and not review["harmful"]
                   and ((unsafe and not flagged) or any(c in set(safety["block"]) for c in flagged)))
        verdict = ("blocked" if blocked else "not checked" if failure else "cleared on review" if cleared
                   else "allowed with notes" if flagged else "passed")
        self.state.setdefault("checks", []).append({
            "check": label, "by": "Llama Guard" + (f" + {review['by']}" if review else ""), "verdict": verdict,
            "categories": [GUARD_CATEGORIES.get(c, c) for c in flagged], "review": review["reason"] if review else "",
            "chunks": len(chunks), "at": time.time(),
        })
        if blocked:
            self.set_stage("safety", "failed", f"{label}: blocked")
        if failure and not blocked:
            if safety["moderation"] == "required":
                raise ContentBlocked("The content check is required, but no Llama Guard machine is available.") from failure
            if st["status"] == "running":
                raise StageSkipped("no content checker available, so it wasn't checked") from failure
            st["detail"] = f"{label}: not checked, no content checker was available"
            self.save()
            return
        if blocked:
            names = ", ".join((GUARD_CATEGORIES.get(c) or c) for c in blocked)
            what = "the idea" if context is None else "the script"
            if review is not None:
                raise ContentBlocked(f"The content check stopped {what} ({names.lower()}): {review['reason']} "
                                     "Try rephrasing it.")
            raise ContentBlocked(f"Llama Guard flagged {what} for {names.lower()}. Try rephrasing it.")
        noted = ", ".join((GUARD_CATEGORIES.get(c) or c).lower() for c in flagged)
        what = "idea" if context is None else "idea and script"
        st["detail"] = f"{what} OK" + (f" (noted: {noted}, allowed for fiction)" if noted else "")
        self.save()


    async def _review_chunks(self, chunks: list[str], flagged: list[str]) -> dict | None:
        """Each flagged part gets its own second opinion; one harmful part blocks, and a missing review keeps Guard's call."""
        reviews = [await self._second_opinion(chunk, flagged) for chunk in chunks]
        if any(r is None for r in reviews):
            return None
        harmful = [r for r in reviews if r and r["harmful"]]
        return (harmful or reviews)[0]

    async def _second_opinion(self, text: str, flagged: list[str]) -> dict | None:
        """A larger model looks again at anything Llama Guard flags: fiction about dark things is fine, real harm isn't."""
        if not profile()["safety"].get("second_opinion", True):
            return None
        names = [GUARD_CATEGORIES.get(c, c) for c in flagged]
        payload = {"prompt": prompts.safety_review(text[: MODERATION_CHUNK * 2], names, GUARD_CATEGORIES),
                   "json_schema": SAFETY_REVIEW_SCHEMA, "max_tokens": 300, "temperature": 0.0}
        try:
            res = await self.call(TaskKind.TEXT, payload, "safety", "Second opinion on a flag")
            data = res.output.get("json") or {}
            harmful = data.get("harmful")
            if not isinstance(harmful, bool):
                return None
        except FabricError as e:
            log.info("no second opinion available: %s", e)
            return None
        category = str(data.get("category", "")).strip().upper()
        return {"harmful": harmful, "category": category if category in GUARD_CATEGORIES else "",
                "reason": " ".join(str(data.get("reason", "")).split())[:240], "by": res.decision.model}

    async def wait_for_review(self) -> None:
        self.state["status"] = "review"
        self.set_stage("script", "done", f"{len(self.shots)} shots, waiting for you to check them")
        if self.slot:
            self.slot.release()
        try:
            await self.approved.wait()
        finally:
            if self.slot:
                await self.slot.acquire()
        self.state["status"] = "running"
        self.set_stage("script", "done", f"{len(self.shots)} shots, checked by you")

    def apply_edits(self, shots: list[dict]) -> None:
        if self.state["status"] != "review":
            raise ValueError("this run isn't waiting for a script review")
        edited: list[Shot] = []
        for raw in shots:
            s = Shot.model_validate({k: v for k, v in raw.items() if k in Shot.model_fields})
            s.visual, s.narration, s.characters = s.visual[:1000], s.narration[:400], [c[:60] for c in s.characters[:6]]
            s.title_card = s.title_card.strip().upper()[:40]
            if s.title_card:
                s.visual, s.duration = "", min(max(s.duration, 1.0), 4.0)
            elif not s.visual.strip():
                continue
            else:
                s.duration = min(max(s.duration, 1.0), 10.0)
            edited.append(s)
        if not any(not s.title_card for s in edited):
            raise ValueError("keep at least one shot with a picture")
        assert self.treatment
        self.state["status"] = "running"
        self.shots = edited
        self.state["shots"] = [
            {"index": i, **s.model_dump(), "image": None, "mask": None, "review": None, "prompt": None}
            for i, s in enumerate(edited)
        ]
        (self.dir / "script.md").write_text(prompts.screenplay_markdown(
            self.treatment, Script(scenes=[Scene(heading="Edited script", shots=edited)]), self.opts, self.runtime
        ), encoding="utf-8")
        self.state["assets"]["script"] = self.url(self.dir / "script.md")
        self.save()
        self.approved.set()

    def restore(self) -> None:
        """Rebuild in-memory state from a finished job so one shot can be redone."""
        s = self.state
        self.treatment = Treatment.model_validate(s["story"])
        self.runtime = runtime(self.opts.format, (s.get("runtime") or {}).get("seconds") or self.opts.target_seconds)
        self.shots = [Shot.model_validate({k: v for k, v in sh.items() if k in Shot.model_fields}) for sh in s["shots"]]
        self.shot_prompts = [sh.get("shot_prompt") or prompts.fallback_shot_prompt(self.opts, self.treatment, shot)
                             for sh, shot in zip(s["shots"], self.shots, strict=False)]
        for c in s.get("cast", []):
            path = self.dir / "cast" / f"{_slug(c['name'])}.png"
            if path.exists():
                self.sheets[c["name"]] = b64e(path.read_bytes())
        self.voice_lines = []
        for i, shot in enumerate(self.shots):
            path = self.dir / "audio" / f"vo_{i:02}.wav"
            if shot.narration.strip() and path.exists():
                samples, sr = read_wav(path.read_bytes())
                self.voice_lines.append((i, path, len(samples) / sr))
        score = self.dir / "audio" / "score.wav"
        self.score_path = score if score.exists() else None

    async def redo_shot(self, i: int, visual: str | None = None, note: str = "") -> None:
        self.state["status"] = "running"
        self.state["error"] = None
        self.save()
        try:
            self.restore()
            shot = self.shots[i]
            if shot.title_card:
                raise ValueError("title cards don't have a picture to redo")
            assert self.treatment
            asked = f"{visual or ''} {note}".strip()
            if asked:
                await self.check_content(asked, self.opts.prompt, f"Check the redo of shot {i + 1}")
            if visual and visual.strip() and visual.strip() != shot.visual:
                shot.visual = visual.strip()
                self.state["shots"][i]["visual"] = shot.visual
                self.shot_prompts[i] = prompts.fallback_shot_prompt(self.opts, self.treatment, shot)
            await self._redraw(i, note.strip())
            await self.edit()
            await self.package()
            self.state["status"] = "done"
        except ContentBlocked as e:
            self.set_stage("frames", "done", f"shot {i + 1} kept as it was")
            self.state["status"] = "done"
            self.state["error"] = str(e)
        except Exception as e:
            log.exception("redo of shot %s in job %s failed", i, self.id)
            if self.stage("frames")["status"] == "running":
                self.set_stage("frames", "failed", f"shot {i + 1} couldn't be redone")
            self.state["status"] = "failed"
            self.state["error"] = f"Redoing shot {i + 1} failed: {str(e)[:400]}"
        finally:
            with contextlib.suppress(Exception):
                self.state["receipt"] = await self.fabric.get(f"/v1/ledger/jobs/{self.id}")
            self.save()


    async def _checked(self, i: int, path: Path, before: bytes | None) -> Review | None:
        """Check a redrawn frame like the first run did; redraw twice more if it fails, and put the old frame back
        (and stop) if it still does."""
        required = profile()["safety"]["moderation"] == "required"
        try:
            review = await self._review(i, path)
        except Exception as e:
            if required:
                if before is not None:
                    path.write_bytes(before)
                raise ContentBlocked(f"The picture check is required, but the new shot {i + 1} couldn't be checked.") from e
            return None
        for fix in (STRICT_FIX, STRICTER_FIX):
            if not review.concerns:
                break
            _to_png(await self._frame(i, fix=fix, fresh=True), path)
            with contextlib.suppress(Exception):
                review = await self._review(i, path)
        if review.concerns:
            if before is not None:
                path.write_bytes(before)
            raise ContentBlocked(f"The new picture for shot {i + 1} still showed {', '.join(review.concerns)} after "
                                 "two redraws, so the shot was left as it was.")
        self._clear_flag(i)
        return review

    async def _redraw(self, i: int, note: str = "") -> None:
        """Draw one shot again, then remake its cutout the same way the first run did."""
        shot = self.shots[i]
        self.set_stage("frames", "running", f"redrawing shot {i + 1}")
        path = self.dir / "frames" / f"shot_{i:02}.png"
        before = path.read_bytes() if path.exists() else None
        _to_png(await self._frame(i, fix=note, fresh=True), path)
        review = await self._checked(i, path, before)
        self.state["shots"][i].update({"image": self.url(path), "review": review.model_dump() if review else None})
        self.set_stage("frames", "done", f"shot {i + 1} redrawn")
        mask = self.dir / "masks" / f"shot_{i:02}.png"
        mask.unlink(missing_ok=True)
        self.state["shots"][i]["mask"] = None
        await self._depth_map(i, fresh=True)
        concept = subject_for(shot)
        if shot.camera in render.PARALLAX_MOVES and concept:
            with contextlib.suppress(FabricError):
                res = await self.call(TaskKind.SEGMENT, {"image": b64e(path.read_bytes()), "concept": concept},
                                      "depth", f"Matte shot {i + 1}", fresh=True)
                mask.parent.mkdir(exist_ok=True)
                mask.write_bytes(b64d(res.output["mask"]))
                self.state["shots"][i]["mask"] = self.url(mask)

    async def _revoice(self, i: int) -> None:
        path = self.dir / "audio" / f"vo_{i:02}.wav"
        self.voice_lines = [v for v in self.voice_lines if v[0] != i]
        text = self.shots[i].narration.strip()
        if not text:
            path.unlink(missing_ok=True)
            return
        voices = self.voices()
        voice = voices.get(self.shots[i].speaker.strip().lower(), voices["narrator"])
        if self.state.get("voice_text"):
            translated = await self._translate([text], self.opts.voice_language, "voice", f"Line for shot {i + 1}")
            if translated:
                text = translated[0]
                self.state["voice_text"][str(i)] = text
        res = await self.call(TaskKind.TTS, {"text": text, "speed": voice["speed"], "pitch": voice["pitch"],
                                             "language": self._voice_code()}, "voice",
                              f"Line for shot {i + 1} ({voice['name']})", fresh=True)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b64d(res.output["audio"]))
        self.voice_lines = sorted(self.voice_lines + [(i, path, float(res.output.get("duration_s", 0)))])

    def agent_log(self, agent: str, summary: str, changes: list[str] | None = None, res=None, score: int | None = None,
                  after: int | None = None, cost: float | None = None) -> None:
        """What an agent saw, decided and changed, kept on the job for the Agents panel and the manifest."""
        spent = cost if cost is not None else (res.cost_usd if res is not None else 0.0)
        self.state.setdefault("agents", []).append({
            "agent": agent, "summary": summary.strip(), "changes": changes or [], "score": score, "after": after,
            "by": f"{res.decision.model} on {res.decision.worker_label}" if res is not None else "",
            "cost_usd": round(spent, 5), "at": time.time(),
        })
        self.save()

    async def ask(self, schema: dict, prompt: str, model, stage: str, label: str, image: Image.Image | None = None,
                  max_tokens: int = 1200, tier: Tier = Tier.STANDARD, temperature: float = 0.3):
        """One structured question to a model (with a picture for vision agents); returns the parsed answer and result."""
        payload: dict = {"json_schema": schema, "max_tokens": max_tokens, "temperature": temperature, "prompt": prompt}
        if image is None:
            payload["system"] = prompts.WRITER_SYSTEM
        else:
            buf = io.BytesIO()
            image.convert("RGB").save(buf, "JPEG", quality=85)
            payload["images"] = [b64e(buf.getvalue())]
        res = await self.call(TaskKind.TEXT, payload, stage, label, tier, fresh=True)
        return model.model_validate(res.output.get("json") or {}), res

    async def script_doctor(self) -> None:
        """Critique the script and rewrite it, up to two rounds, before anything is drawn or spoken."""
        assert self.treatment
        kind = "trailer" if self.opts.format == "trailer" else "short film"
        for n in (1, 2):
            try:
                report, res = await self.ask(agents.DOCTOR_SCHEMA, agents.doctor_prompt(self.treatment, self.shots, kind),
                                             agents.DoctorReport, "script", f"Script doctor, round {n}")
            except Exception as e:
                log.warning("script doctor failed: %s", e)
                return
            names = [c.name for c in self.treatment.characters]
            changes = agents.apply_doctor(self.shots, report.edits, names) if report.score < agents.SCREENING_PASS else []
            notes = " ".join(x.strip() for x in report.notes[:3] if x.strip())
            self.agent_log("Script doctor", notes or f"Scored the script {report.score}/10.", changes, res,
                           score=report.score)
            if not changes:
                return

    async def story_editor(self) -> None:
        """The strongest writer rewrites the whole voice-over as one piece, so that it flows and grips."""
        assert self.treatment
        try:
            report, res = await self.ask(agents.STORY_EDITOR_SCHEMA, agents.story_editor_prompt(self.treatment, self.shots),
                                         agents.StoryEdit, "script", "Story editor", max_tokens=400 + 70 * len(self.shots),
                                         tier=Tier.PREMIUM, temperature=0.7)
        except Exception as e:
            log.warning("story editor failed: %s", e)
            return
        changes = agents.apply_story_edit(self.shots, report.lines)
        self.agent_log("Story editor", report.notes.strip() or f"Rewrote {len(changes)} lines.", changes, res)

    def _reconcile_doctor(self) -> None:
        """The script is tidied after the doctor's edits (lengths rescaled, extra lines trimmed); keep its log honest."""
        for turn in self.state.get("agents", []):
            if turn["agent"] != "Script doctor" or not turn["changes"]:
                continue
            for k, change in enumerate(turn["changes"]):
                m = re.match(r'shot (\d+): line → "(.*)"$', change)
                if m and int(m.group(1)) <= len(self.shots) and self.shots[int(m.group(1)) - 1].narration != m.group(2):
                    turn["changes"][k] = change + " (later trimmed: the film can only carry so many lines)"
            if any(re.match(r"shot \d+: [\d.]+s → ", c) for c in turn["changes"]) and "rescaled" not in turn["summary"]:
                turn["summary"] += " Shot lengths were then rescaled so the film keeps its length."

    async def continuity(self) -> None:
        """Compare every frame of each character with their reference sheet and redraw the ones that drifted."""
        assert self.treatment
        redraws, changes, res, cost, checked, total = 0, [], None, 0.0, 0, 0
        for c in self.treatment.characters:
            sheet = self.dir / "cast" / f"{_slug(c.name)}.png"
            shots = [i for i, s in enumerate(self.shots) if c.name in s.characters and not s.title_card
                     and (self.dir / "frames" / f"shot_{i:02}.png").exists()]
            if not sheet.exists() or len(shots) < 2:
                continue
            total += len(shots)
            for first in range(0, len(shots), agents.CONTINUITY_SHEET):
                batch = shots[first:first + agents.CONTINUITY_SHEET]
                tiles = [("REF", sheet)] + [(str(i + 1), self.dir / "frames" / f"shot_{i:02}.png") for i in batch]
                try:
                    report, res = await self.ask(agents.CONTINUITY_SCHEMA,
                                                 agents.continuity_prompt(c.name, c.look, [i + 1 for i in batch]),
                                                 agents.ContinuityReport, "review", f"Continuity: {c.name}",
                                                 image=agents.contact_sheet(tiles))
                except Exception as e:
                    log.warning("continuity check for %s failed: %s", c.name, e)
                    continue
                cost += res.cost_usd
                checked += len(batch)
                for m in report.mismatched:
                    i = m.shot - 1
                    if i not in batch or redraws >= agents.MAX_CONTINUITY_REDRAWS:
                        continue
                    path = self.dir / "frames" / f"shot_{i:02}.png"
                    before = path.read_bytes()
                    try:
                        _to_png(await self._frame(i, fix=f"{c.name} must look exactly like the reference sheet: {c.look}",
                                                  fresh=True), path)
                        review = await self._review(i, path)
                    except Exception as e:
                        log.warning("continuity redraw of shot %s failed: %s", i, e)
                        path.write_bytes(before)
                        continue
                    redraws += 1
                    if review.concerns:
                        path.write_bytes(before)
                        changes.append(f"shot {i + 1}: kept the old frame (the redraw failed the picture check)")
                    else:
                        self.state["shots"][i].update({"image": self.url(path), "review": review.model_dump()})
                        changes.append(f"shot {i + 1}: redrawn so {c.name} matches the sheet "
                                       f"({m.problem.strip() or 'looked different'})")
        if res is not None:
            seen = f"Checked {checked} of {total} character appearances against the reference sheets"
            summary = f"{seen}; {redraws} frame{'s' if redraws != 1 else ''} redrawn." if redraws else \
                f"{seen}; every one matched."
            self.agent_log("Continuity supervisor", summary, changes, res, cost=cost)

    async def editor(self) -> None:
        """Set each shot's length and camera move now that the voice-over lengths are known."""
        voice = {i: seconds for i, _, seconds in self.voice_lines}
        target = self.runtime.seconds - FINAL_CARD_S
        try:
            plan, res = await self.ask(agents.EDITOR_SCHEMA, agents.editor_prompt(self.shots, voice, target),
                                       agents.EditorPlan, "edit", "Editor: pacing and camera")
        except Exception as e:
            log.warning("editor failed: %s", e)
            return
        changes = agents.apply_editor(self.shots, plan.edits, voice, target)
        for i, s in enumerate(self.shots):
            self.state["shots"][i].update({"duration": s.duration, "camera": s.camera})
        self.agent_log("Editor", plan.notes or "Kept the cut as planned.", changes, res)

    async def review_translation(self, source: list[str], translated: list[str], code: str, stage: str) -> list[str]:
        """Check each translated line keeps the English meaning; fix the ones that drift, if the fix passes the checks."""
        language = LANGUAGES.get(code) or VOICE_LANGUAGES[code][0]
        try:
            review, res = await self.ask(agents.TRANSLATION_REVIEW_SCHEMA,
                                         agents.translation_review_prompt(language, source, translated),
                                         agents.TranslationReview, stage, f"Translation review: {language}")
        except Exception as e:
            log.warning("translation review (%s) failed: %s", language, e)
            return translated
        fixed, changes = list(translated), []
        for line in review.lines:
            if line.faithful or not line.fix.strip() or not 1 <= line.n <= len(fixed):
                continue
            candidate = list(fixed)
            candidate[line.n - 1] = line.fix
            if translation_problem(source, candidate, code) is None:
                fixed = [_clean_line(x) for x in candidate]
                changes.append(f"line {line.n} retranslated")
        self.agent_log(f"Translation reviewer ({language})",
                       f"{len(source) - len(changes)} of {len(source)} lines kept as translated.", changes, res)
        return fixed

    async def test_screening(self) -> None:
        """A critic watches the cut; if it falls short, its note goes to the director agent, which fixes the film."""
        assert self.treatment
        frames = [(str(i + 1), self.dir / "frames" / f"shot_{i:02}.png") for i, s in enumerate(self.shots)
                  if not s.title_card and (self.dir / "frames" / f"shot_{i:02}.png").exists()][:16]
        if len(frames) < 3:
            return
        self.set_stage("package", "running", "test screening")

        async def watch(label: str):
            heard = (self.state.get("qa") or {}).get("similarity")
            seconds = self.state["assets"].get("video_seconds") or self.runtime.seconds
            return await self.ask(agents.SCREENING_SCHEMA,
                                  agents.screening_prompt(self.treatment, self.shots, heard, seconds),
                                  agents.Screening, "director", label, image=agents.contact_sheet(frames, 256))

        try:
            report, res = await watch("Test screening")
        except Exception as e:
            log.warning("test screening failed: %s", e)
            self.set_stage("package", "done", "")
            return
        summary = " ".join(x for x in (report.works, report.problem) if x.strip())
        if report.score >= agents.SCREENING_PASS or not report.note.strip():
            self.agent_log("Test screening", summary or f"Scored the cut {report.score}/10.", [], res, score=report.score)
            self.set_stage("package", "done", "")
            return
        self.agent_log("Test screening", f"{summary} Note for the director: {report.note}", [], res, score=report.score)
        try:
            pending = await self.direct(report.note)
        except Exception as e:
            log.warning("director couldn't plan the screening note: %s", e)
            self.set_stage("package", "done", "")
            return
        if not pending["edits"] or pending["estimate_usd"] > agents.POLISH_BUDGET_USD:
            self.state["director"]["pending"] = None
            self.agent_log("Director", "Planned nothing within the polish budget, so the cut stays as it is.")
            self.set_stage("package", "done", "")
            return
        await self.apply_plan()
        turns = self.state["director"]["turns"]
        if turns:
            turns[-1]["from"] = "test screening"
        applied = bool(turns and turns[-1].get("applied"))
        self.state["status"] = "running"
        if not applied:
            self.state["error"] = None
        self.set_stage("package", "done", "")
        if not applied:
            self.agent_log("Director", "The polish didn't apply, so the first cut was kept.")
            return
        try:
            again, res2 = await watch("Test screening, second look")
            after = again.score
        except Exception:
            res2, after = None, None
        self.agent_log("Director", pending.get("reply") or "Applied the screening note.",
                       [e["summary"] for e in pending["edits"]], res2, score=report.score, after=after)

    async def release(self) -> None:
        """Release copy for YouTube and Instagram, and a thumbnail from the shot the agent picks."""
        assert self.treatment
        image_shots = [i for i, s in enumerate(self.shots) if not s.title_card]
        if not image_shots:
            return
        voice = VOICE_LANGUAGES.get(getattr(self.opts, "voice_language", "en"), ("English",))[0]
        used, _ = await self.models_used()
        roles = {"story": "story", "script": "script", "frames": "pictures", "voice": "voices", "score": "music",
                 "depth": "depth and cutouts", "qa": "narration check", "safety": "content checks"}
        made_with = "; ".join(f"{role}: {', '.join(used[stage])}" for stage, role in roles.items() if used.get(stage))
        made_with = (made_with or "AI models") + "; cut, mixed and labelled by CinemaFabric, on local machines and the Meta Model API"
        try:
            copy, res = await self.ask(agents.RELEASE_SCHEMA,
                                       agents.release_prompt(self.treatment, self.runtime.label, self.opts.genre, voice,
                                                             len(self.shots), made_with),
                                       agents.Release, "package", "Release copy", max_tokens=900)
        except Exception as e:
            log.warning("release copy failed: %s", e)
            return
        pick = copy.thumbnail_shot - 1 if copy.thumbnail_shot - 1 in image_shots else image_shots[0]
        thumb = self.dir / "thumbnail.jpg"
        frame = Image.open(self.dir / "frames" / f"shot_{pick:02}.png").convert("RGB")
        render.cover(frame, (1280, 720)).resize((1280, 720), Image.Resampling.LANCZOS).save(thumb, quality=90)
        (self.dir / "release.md").write_text(agents.release_markdown(copy, self.treatment.title), encoding="utf-8")
        self.state["release"] = {**copy.model_dump(), "thumbnail_shot": pick + 1}
        self.state["assets"]["release"] = self.url(self.dir / "release.md")
        self.state["assets"]["thumbnail"] = self.url(thumb)
        await self.sign(["thumbnail.jpg"])
        self.agent_log("Release writer", f'"{copy.youtube_title}", thumbnail from shot {pick + 1}.',
                       [f"{len(copy.hashtags)} hashtags", "YouTube description", "Instagram caption"], res)
        await self.write_records()
        zip_path = await asyncio.to_thread(write_zip, self.dir, self.treatment.title)
        self.state["assets"]["zip"] = self.url(zip_path)
        self.save()

    async def sign(self, names: list[str]) -> None:
        """Embed signed Content Credentials (C2PA) in finished files; a failure is noted, never fatal."""
        from ..config import get_settings
        from .media import credentials

        info = self.state.setdefault("credentials", {"signed": {}, "own_certificate": False})
        if not credentials.available():
            info["reason"] = "the c2pa extra isn't installed"
            return
        title = self.treatment.title if self.treatment else self.id
        for name in names:
            path = self.dir / name
            if not path.exists() or info["signed"].get(name) == path.stat().st_mtime:
                continue
            try:
                info["own_certificate"] = await asyncio.to_thread(credentials.sign, path, title, get_settings())
                info["signed"][name] = path.stat().st_mtime
                info.pop("reason", None)
            except Exception as e:
                log.warning("couldn't sign %s for %s: %s", name, self.id, e)
                info["signed"].pop(name, None)
                info["reason"] = f"{name}: {str(e)[:160]}"
        film = self.dir / "film.mp4"
        current = film.exists() and info["signed"].get("film.mp4") == film.stat().st_mtime
        found = await asyncio.to_thread(credentials.read, film) if current else None
        info["verified"] = bool(found and found["state"] in ("Valid", "Trusted"))

    async def cut_trailer(self) -> None:
        """A trailer cut from the finished movie's own shots, voice and score; nothing new is generated."""
        assert self.treatment
        voiced = {i: seconds for i, _, seconds in self.voice_lines}
        picks = trailer_picks(self.shots, voiced)
        if len(picks) < 3:
            return
        paths = {i: path for i, path, _ in self.voice_lines}
        clips, narration, subs, impacts, t = [], [], [], [], 0.0
        middle = len(picks) // 2
        for n, (i, seconds, speaks) in enumerate(picks):
            if n == middle and self.treatment.tagline:
                clips.append(render.Clip(1.8, card_text=self.treatment.tagline))
                impacts.append(t)
                t += 1.8
            s, mask, depth = self.shots[i], self.dir / "masks" / f"shot_{i:02}.png", self.dir / "depth" / f"shot_{i:02}.png"
            clips.append(render.Clip(seconds, s.camera, self.dir / "frames" / f"shot_{i:02}.png",
                                     mask if mask.exists() else None, depth=depth if depth.exists() else None))
            if speaks and i in paths:
                samples, sr = read_wav(paths[i].read_bytes())
                narration.append((t + 0.15, samples, sr))
                subs.append((t + 0.15, t + 0.15 + voiced[i], s.narration.strip()))
            t += seconds
        impacts.append(t)
        clips.append(render.Clip(FINAL_CARD_S, card_text=self.treatment.title, card_sub="The full film is out now",
                                 card_note=f"{provenance.LABEL} · {provenance.TOOL}"))
        total = t + FINAL_CARD_S
        score = read_wav(self.score_path.read_bytes()) if self.score_path else None
        mixed = await asyncio.to_thread(audio_mix.mix, total, narration, score, impacts)
        mix_path = self.dir / "audio" / "trailer_mix.wav"
        mix_path.write_bytes(wav_bytes(mixed, audio_mix.OUT_SR))
        mark = self.state.get("watermark")
        await self.watermark(mix_path)
        if mark is not None:
            self.state["watermark"] = mark
        out = self.dir / "trailer.mp4"
        await asyncio.to_thread(render.render_film, clips, self.video, mix_path, out, subs, self.fps, 0.3, None,
                                provenance.ffmpeg_tags(f"{self.treatment.title} (trailer)"), tune=self.tune)
        self.state["assets"]["trailer"] = self.url(out)

    async def commentary(self) -> None:
        """Record a director's commentary over the finished cut, as a second version of the film."""
        from . import commentary

        self.state["status"] = "running"
        self.state["error"] = None
        self.save()
        try:
            self.restore()
            assert self.treatment
            if getattr(self.opts, "commercial_safe", False):
                raise ValueError("the commentary voice comes from a model licensed for non-commercial use only")
            mix_path, film = self.dir / "audio" / "mix.wav", self.dir / "film.mp4"
            if not mix_path.exists() or not film.exists():
                raise ValueError("the film's soundtrack is missing; render it again first")
            decisions = (await self.fabric.get("/v1/decisions", job_id=self.id, limit=10000))["decisions"]
            source = (self.state.get("variant_of") or {}).get("job")
            if source:
                decisions += (await self.fabric.get("/v1/decisions", job_id=source, limit=10000))["decisions"]
            starts = list(itertools.accumulate([0.0] + [s.duration for s in self.shots[:-1]]))
            facts = commentary.shot_facts(self.shots, decisions)
            payload = {"system": prompts.WRITER_SYSTEM, "json_schema": commentary.SCHEMA, "max_tokens": 1500,
                       "temperature": 0.6,
                       "prompt": commentary.prompt(self.treatment, self.shots, starts, facts, self.state.get("receipt") or {})}
            lines = []
            for attempt in range(2):
                res = await self.call(TaskKind.TEXT, payload, "commentary",
                                      "Write the commentary" + (" (retry)" if attempt else ""), fresh=bool(attempt))
                with contextlib.suppress(ValidationError):
                    lines = commentary.validate(commentary.Commentary.model_validate(res.output.get("json") or {}),
                                                self.shots, facts)
                if lines:
                    break
            if not lines:
                raise ValueError("the writer had nothing to say about this film")
            await self.check_content("\n".join(line.text for line in lines), self.opts.prompt, "Check the commentary")
            base, sr = read_wav(mix_path.read_bytes())
            total = len(base) / sr
            spoken, subs, said, cursor = [], [], [], 0.0
            for n, line in enumerate(lines, 1):
                start = max(starts[line.shot - 1] + 0.2, cursor + 0.3)
                if start > min(total - 1.0, starts[line.shot - 1] + self.shots[line.shot - 1].duration):
                    continue
                res = await self.call(TaskKind.TTS, {"text": line.text, "speed": 1.0, "pitch": 0.94, "language": "eng"},
                                      "commentary", f"Commentary line {n} (shot {line.shot})")
                samples, rate = read_wav(b64d(res.output["audio"]))
                cursor = start + len(samples) / rate
                spoken.append((start, samples, rate))
                subs.append((start, min(total, cursor), line.text))
                said.append({"shot": line.shot, "start": round(start, 2), "text": line.text})
            track = await asyncio.to_thread(audio_mix.over, base, sr, spoken)
            track_path = self.dir / "audio" / "commentary.wav"
            track_path.write_bytes(wav_bytes(track, sr))
            out = self.dir / "commentary.mp4"
            await asyncio.to_thread(render.replace_audio, film, track_path, out,
                                    provenance.ffmpeg_tags(f"{self.treatment.title} (director's commentary)"))
            render.write_vtt(subs, self.dir / "commentary.vtt")
            self.state["assets"]["commentary"] = self.url(out)
            self.state["assets"]["commentary_vtt"] = self.url(self.dir / "commentary.vtt")
            await self.sign(["commentary.mp4"])
            self.state["assets"]["zip"] = self.url(await asyncio.to_thread(write_zip, self.dir, self.treatment.title))
            await self.upload()
            self.state["commentary"] = said
            with contextlib.suppress(Exception):
                self.state["receipt"] = await self.fabric.get(f"/v1/ledger/jobs/{self.id}")
        except Exception as e:
            log.exception("commentary for job %s failed", self.id)
            self.state["error"] = f"Commentary: {str(e)[:300]}"
        self.state["status"] = "done"
        self.save()

    async def _job_cost(self) -> float:
        with contextlib.suppress(Exception):
            return float((await self.fabric.get(f"/v1/ledger/jobs/{self.id}")).get("cost_usd") or 0.0)
        return 0.0

    async def direct(self, note: str, from_shot: int = 1) -> dict:
        """Plan edits for a director's note. Nothing changes until the plan is applied."""
        self.restore()
        assert self.treatment
        await self.check_content(note, self.opts.prompt, "Check the director's note")
        notes = self.state.setdefault("director", {"turns": [], "pending": None})
        payload = {"system": prompts.WRITER_SYSTEM, "json_schema": director.PLAN_SCHEMA, "max_tokens": 1200,
                   "temperature": 0.4, "prompt": director.prompt(self.treatment, self.shots, note, notes["turns"])}
        plan, res, last, planning = None, None, "", 0.0
        for attempt in range(2):
            res = await self.call(TaskKind.TEXT, payload, "director", "Plan the note" + (" (retry)" if attempt else ""),
                                  fresh=True)
            planning += res.cost_usd
            try:
                plan = director.Plan.model_validate(res.output.get("json"))
                break
            except ValidationError as e:
                last = str(e)[:300]
        if plan is None or res is None:
            raise RuntimeError(f"couldn't plan that note ({last})")
        edits = director.validate(plan, self.shots)
        if from_shot > 1:
            edits = [e for e in edits if e.shot >= from_shot or e.op == "tagline"]
        if edits:
            await self.check_content(director.checked_text(edits), self.opts.prompt, "Check the planned edits")
        notes["pending"] = {
            "note": note, "reply": plan.reply.strip(), "at": time.time(),
            "edits": [e.model_dump() | {"summary": e.describe()} for e in edits],
            "estimate_usd": director.estimate(edits),
            "planning_usd": round(planning, 6),
            "planned_by": f"{res.decision.model} on {res.decision.worker_label}",
        }
        self.save()
        return notes["pending"]

    async def apply_plan(self) -> None:
        """Apply a planned note all-or-nothing: if any step fails, the film, files and text go back as they were."""
        notes = self.state.setdefault("director", {"turns": [], "pending": None})
        pending = notes.get("pending")
        if not pending:
            self.state["error"] = "there's no plan to apply"
            self.save()
            return
        snapshot = copy.deepcopy({k: self.state.get(k) for k in ("shots", "story", "assets", "stages", "watermark",
                                                                   "voice_text", "release", "credentials")})
        backup: dict[Path, bytes | None] = {}
        self.state["status"] = "running"
        self.state["error"] = None
        self.save()
        before = await self._job_cost()
        try:
            self.restore()
            assert self.treatment
            edits = director.validate(director.Plan(edits=[director.Edit.model_validate(raw) for raw in pending["edits"]]),
                                      self.shots)
            redraw: list[tuple[int, str]] = []
            revoice: list[int] = []
            rescore = retitle = False
            for e in edits:
                i = e.shot - 1
                if e.op == "narration":
                    speaker, self.shots[i].narration = agents.split_speaker(e.text, [c.name for c in self.treatment.characters])
                    if speaker and speaker.lower() not in ("narrator", "voice-over", "vo"):
                        self.shots[i].speaker = speaker
                    revoice.append(i)
                elif e.op == "visual":
                    self.shots[i].visual = e.text
                    named = [c for c in self.shots[i].characters
                             if re.search(rf"\b{re.escape(c.lower())}\b", e.text.lower())]
                    self.shots[i].characters = named or self.shots[i].characters
                    self.shot_prompts[i] = prompts.fallback_shot_prompt(self.opts, self.treatment, self.shots[i])
                    redraw.append((i, ""))
                elif e.op == "redraw":
                    redraw.append((i, e.text))
                elif e.op == "duration":
                    self.shots[i].duration = e.seconds
                elif e.op in ("title", "tagline"):
                    setattr(self.treatment, e.op, e.text)
                    retitle = True
                elif e.op == "music":
                    self.treatment.music_mood = e.text
                    rescore = True
            for i, s in enumerate(self.shots):
                self.state["shots"][i].update({"visual": s.visual, "narration": s.narration, "duration": s.duration,
                                               "characters": s.characters, "speaker": s.speaker})
            self.state["story"] = self.treatment.model_dump()
            touched = [self.dir / "film.mp4", self.dir / "poster.png", self.dir / "audio" / "score.wav", *stale_versions(self.dir),
                       self.dir / "script.md", self.dir / "release.md", self.dir / "thumbnail.jpg",
                       *(self.dir / d / f"shot_{i:02}.png" for i, _ in redraw for d in ("frames", "masks", "depth")),
                       *(self.dir / "audio" / f"vo_{i:02}.wav" for i in revoice)]
            backup = {p: (p.read_bytes() if p.exists() else None) for p in touched}
            for i, fix in redraw:
                await self._redraw(i, fix)
            for i in revoice:
                await self._revoice(i)
            spoken = {j: seconds for j, _, seconds in self.voice_lines}
            for i in revoice:
                if i in spoken and self.shots[i].duration < spoken[i] + 0.5:
                    self.shots[i].duration = round(min(agents.VOICED_MAX_S, spoken[i] + 0.5), 1)
                    self.state["shots"][i]["duration"] = self.shots[i].duration
            if rescore:
                await self.score()
                if self.stage("score")["status"] != "done":
                    raise RuntimeError("the new score couldn't be made")
            if retitle:
                await self._typeset_poster()
            self.write_script()
            stale_release = retitle and bool(self.state.get("release"))
            if stale_release:
                for name in ("release.md", "thumbnail.jpg"):
                    (self.dir / name).unlink(missing_ok=True)
                self.state.pop("release", None)
                for asset in ("release", "thumbnail"):
                    self.state["assets"].pop(asset, None)
            await self.edit()
            await self.package()
            if stale_release and self.crew:
                await self.release()
            spent = await self._job_cost() - before + pending.get("planning_usd", 0.0)
            notes["turns"].append({**pending, "applied": True, "cost_usd": round(spent, 4)})
            notes["pending"] = None
            self.state["status"] = "done"
        except Exception as e:
            log.exception("director plan for job %s failed", self.id)
            for path, data in backup.items():
                with contextlib.suppress(OSError):
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(data)
            self.state.update(snapshot)
            spent = await self._job_cost() - before
            notes["turns"].append({**pending, "applied": False, "cost_usd": round(spent, 4), "error": str(e)[:200]})
            self.state["status"] = "done"
            cost = f"It cost ${spent:.4f} before failing. " if spent > 0 else ""
            self.state["error"] = (f"The note couldn't be applied, so nothing was changed ({str(e)[:300]}). "
                                   f"{cost}You can try again or discard it.")
        finally:
            with contextlib.suppress(Exception):
                self.state["receipt"] = await self.fabric.get(f"/v1/ledger/jobs/{self.id}")
            self.save()


class ContentBlocked(RuntimeError):
    pass


GUARD_CATEGORIES = {
    "S1": "Violent crimes", "S2": "Non-violent crimes", "S3": "Sex-related crimes", "S4": "Child sexual exploitation",
    "S5": "Defamation", "S6": "Specialized advice", "S7": "Privacy", "S8": "Intellectual property",
    "S9": "Indiscriminate weapons", "S10": "Hate", "S11": "Suicide and self-harm", "S12": "Sexual content",
    "S13": "Elections", "S14": "Code interpreter abuse",
}


def _friendly_error(e: Exception, opts: JobOptions) -> str:
    text = str(e)
    if "this run's spending limit" in text:
        return (f"This run reached its ${opts.max_cost_usd:g} limit. Raise or remove the limit and press Retry; "
                "steps that already finished are reused for free.")
    if "spending limit reached" in text:
        return "The overall spending limit has been reached. Raise it in the Machines panel, then press Retry."
    return text[:500]


SUBJECTS = ("lighthouse", "boat", "ship", "car", "truck", "train", "bus", "bicycle", "motorcycle", "plane", "robot",
            "house", "tower", "bridge", "statue", "tree", "horse", "dog", "cat", "bird", "whale", "lantern", "door",
            "window", "table", "chair", "phone", "clock", "cup", "mug", "book", "notebook", "lamp", "guitar")


def refused(e: Exception) -> bool:
    """The picture model turned the prompt down under its content policy, rather than failing."""
    text = str(e).lower()
    return "content_policy" in text or "content management policy" in text


def subject_for(shot: Shot) -> str | None:
    """A short noun phrase for SAM 3/3.1 to segment, or None when the shot has no clear foreground subject."""
    if shot.characters:
        return "person"
    words = _words(shot.visual)
    if any(w in ("figure", "silhouette", "man", "woman", "girl", "boy", "child", "people", "crowd") for w in words):
        return "person"
    for word in words:
        if word in SUBJECTS or (word.endswith("s") and word[:-1] in SUBJECTS):
            return word if word in SUBJECTS else word[:-1]
    return None


def _clean_line(text) -> str:
    text = re.sub(r"</?idea>", "", " ".join(str(text).split()))
    return re.sub(r"^\d+[.)]\s*", "", text).strip()


SCRIPTS = {
    "hi": [(0x0900, 0x097F)], "mr": [(0x0900, 0x097F)], "bn": [(0x0980, 0x09FF)], "gu": [(0x0A80, 0x0AFF)],
    "ta": [(0x0B80, 0x0BFF)], "te": [(0x0C00, 0x0C7F)], "kn": [(0x0C80, 0x0CFF)], "ml": [(0x0D00, 0x0D7F)],
    "ar": [(0x0600, 0x06FF)], "ja": [(0x3040, 0x30FF), (0x4E00, 0x9FFF)], "ko": [(0xAC00, 0xD7AF), (0x1100, 0x11FF)],
}


def translation_problem(source: list[str], found: list, code: str) -> str | None:
    """Why a translation can't be used, or None. Catches echoes, wrong counts and the wrong script."""
    lines = [_clean_line(x) for x in found]
    if len(lines) != len(source) or not all(lines):
        return f"expected {len(source)} lines, got {len(lines)}"
    long = [(a, b) for a, b in zip(source, lines, strict=False) if len(a.split()) >= 3]
    same = sum(a.strip().lower() == b.strip().lower() for a, b in long)
    if long and same > len(long) // 3:
        return f"{same} of {len(long)} lines were left untranslated"
    if code in SCRIPTS:
        letters = [ch for ch in "".join(lines) if ch.isalpha()]
        native = sum(any(lo <= ord(ch) <= hi for lo, hi in SCRIPTS[code]) for ch in letters)
        if not letters or native / len(letters) < 0.6:
            return "most of it isn't written in the language's own script"
    return None


def credits_text(used: dict[str, list[str]], licences: dict[str, str]) -> str:
    names = dict(STAGES)
    lines = ["Made with CinemaFabric. Pictures, voices and music in this film were generated by AI models.", "",
             "Models used:"]
    for stage, models in used.items():
        lines.append(f"  {names.get(stage, stage)}: " + ", ".join(f"{m} ({licences.get(m, 'unknown')})" for m in models))
    notices = catalog().get("notices", {})
    shown = [notices[lic] for lic in dict.fromkeys(licences.values()) if lic in notices]
    if shown:
        lines += ["", "Notices:"] + [f"  {n}" for n in shown]
    return "\n".join(lines) + "\n"


def stale_versions(folder: Path) -> list[Path]:
    """Versions made from the current cut, which no longer match once it's re-rendered."""
    return [p for p in [folder / "commentary.mp4", folder / "commentary.vtt", folder / "trailer.mp4", *folder.glob("film_*.mp4")]
            if p.exists()]


def trailer_picks(shots: list[Shot], voiced: dict[int, float], target: float = TRAILER_S) -> list[tuple[int, float, bool]]:
    """Shots for a trailer cut from a movie: spread over the first three quarters so the ending isn't given away,
    shorter than in the movie, and a few long enough to carry their voice-over line."""
    pictures = [i for i, s in enumerate(shots) if not s.title_card]
    pool = [i for i in pictures if i < len(shots) * 0.75] or pictures
    if not pool:
        return []
    count = max(1, min(len(pool), round(target / TRAILER_SHOT_S)))
    chosen = [pool[int(k * len(pool) / count)] for k in range(count)]
    picks, total, lines = [], 0.0, 0
    for i in chosen:
        seconds = min(shots[i].duration, TRAILER_SHOT_S)
        speaks = i in voiced and lines < TRAILER_LINES and voiced[i] + 0.4 <= 5.0
        if speaks:
            seconds, lines = max(seconds, voiced[i] + 0.4), lines + 1
        if picks and total + seconds > target:
            break
        picks.append((i, round(seconds, 2), speaks))
        total += seconds
    return picks


def write_zip(folder: Path, title: str) -> Path:
    """Everything a finished film has so far: the cut, any commentary and platform versions, art, text and frames."""
    for old in folder.glob("*.zip"):
        old.unlink()
    zip_path = folder / f"{_slug(title)}.zip"
    include = ["film.mp4", "trailer.mp4", "commentary.mp4", "poster.png", "poster_art.png", "script.md", "story.json", "subtitles.srt",
               "commentary.vtt", "CREDITS.txt", "manifest.json", "passport.png", "release.md", "thumbnail.jpg"]
    include += sorted(p.name for p in folder.glob("subtitles.*.srt")) + sorted(p.name for p in folder.glob("film_*.mp4"))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in include:
            p = folder / name
            if p.exists():
                z.write(p, name)
        for sub in ("frames", "cast"):
            for p in sorted((folder / sub).glob("*.png")):
                z.write(p, f"{sub}/{p.name}")
    return zip_path


def _story_text(t: Treatment) -> str:
    people = "; ".join(f"{c.name}: {c.look}" for c in [*t.characters, *t.places, *t.props])
    return "\n".join([t.title, t.tagline, t.logline, t.story, " ".join(t.beats), t.music_mood, " ".join(t.music_cues), people])


def _script_text(shots: list[Shot]) -> str:
    return "\n".join(f"{s.title_card or s.visual} {s.narration}".strip() for s in shots)


class _ShotPrompt(BaseModel):
    index: int = 0
    prompt: str = ""


class _ShotPrompts(BaseModel):
    prompts: list[_ShotPrompt]


def _slug(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")[:40] or "film"


def _words(text: str) -> list[str]:
    return "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in text).split()


def _to_png(data: bytes, path: Path) -> bytes:
    png = provenance.png_bytes(Image.open(io.BytesIO(data)).convert("RGB"))
    path.write_bytes(png)
    return png
