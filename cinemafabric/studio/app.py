"""Studio service (FastAPI): job API, media files, fabric dashboard proxy and the web UI."""

import asyncio
import datetime as dt
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from ..config import get_settings
from ..fabric import machines, pricing
from ..fabric.client import FabricClient, FabricError
from ..profile import cloud_model
from . import director, share, suggest
from .accounts import Accounts
from .library import CharacterLibrary
from .media import export as exporter
from .media.storage import MediaFiles, media_store
from .options import FORMATS, JobOptions, Runtime, catalog, runtime
from .pipeline import SHOT_PROMPT_BATCH, ContentBlocked, Production, new_state, write_zip
from .store import JobStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cinemafabric.studio")

FRONTEND_DIST = Path(os.environ.get("CF_FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist")).resolve()
MAX_WAITING_JOBS = 20
SUGGESTIONS_PER_MINUTE = 20
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
COOKIE = "cf_session"
OPEN_PREFIXES = ("/api/auth/", "/api/options", "/api/report/")
SHARED_KINDS = {"mp4", "png", "vtt"}
JOB_PATH = re.compile(r"^/(?:api/jobs|media)/([0-9a-f]{10})(?:/|$)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    (settings.data_dir / "jobs").mkdir(parents=True, exist_ok=True)
    store = JobStore(settings.path("studio.db"))
    store.mark_interrupted()
    app.state.settings = settings
    app.state.store = store
    app.state.library = CharacterLibrary(settings.path("studio.db"), settings.data_dir / "library")
    app.state.accounts = Accounts(settings.path("studio.db")) if settings.accounts else None
    app.state.fabric = FabricClient(settings.fabric_url, machines.fabric_token(settings))
    app.state.tasks = {}
    app.state.planning = set()
    app.state.productions = {}
    app.state.slot = asyncio.Semaphore(max(1, settings.parallel_productions))
    yield
    for task in app.state.tasks.values():
        task.cancel()
    await app.state.fabric.aclose()
    store.db.close()


app = FastAPI(title="CinemaFabric · Studio", version="0.1.0", lifespan=lifespan)
app.mount("/media", MediaFiles(directory=get_settings().data_dir / "jobs", check_dir=False,
                               store=media_store(get_settings())), name="media")
app.mount("/library", StaticFiles(directory=get_settings().data_dir / "library", check_dir=False), name="library")


def _cross_site(request: Request) -> bool:
    origin = request.headers.get("origin")
    return request.headers.get("sec-fetch-site") == "cross-site" or bool(origin and urlparse(origin).netloc != request.url.netloc)


def _user(request: Request) -> dict | None:
    return getattr(request.state, "user", None)


@app.middleware("http")
async def people(request: Request, call_next):
    """With accounts on: sign-in for the API and files, and each film visible only to its owner (or shared)."""
    accounts = getattr(app.state, "accounts", None)
    if accounts is None:
        return await call_next(request)
    path = request.url.path
    if request.method not in ("GET", "HEAD", "OPTIONS") and _cross_site(request):
        return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    user = accounts.user_for(request.cookies.get(COOKIE))
    request.state.user = user
    if path.startswith(("/api/", "/media/", "/library/")) and not path.startswith(OPEN_PREFIXES):
        job = JOB_PATH.match(path)
        if (job and path.startswith("/media/") and path.rsplit(".", 1)[-1] in SHARED_KINDS
                and app.state.store.is_shared(job.group(1))):
            return await call_next(request)
        if user is None:
            return JSONResponse({"detail": "sign in first"}, status_code=401)
        if job and not user["is_admin"] and app.state.store.owner(job.group(1)) != user["id"]:
            return JSONResponse({"detail": "job not found"}, status_code=404)
        if (path.startswith("/library/") and not user["is_admin"]
                and app.state.library.owner_of(path.rsplit("/", 1)[-1]) != user["id"]):
            return JSONResponse({"detail": "not found"}, status_code=404)
    return await call_next(request)


def _allowance(request: Request, opts: JobOptions | None = None) -> None:
    """Per-person limits when accounts are on: films running at once, and spend per day."""
    user, s = _user(request), app.state.settings
    if user is None or user["is_admin"]:
        return
    if app.state.store.active(user["id"]) >= s.user_max_active:
        raise HTTPException(429, f"you already have {s.user_max_active} films in progress; wait for one to finish")
    midnight = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
    left = (user["daily_usd"] if user["daily_usd"] is not None else s.user_daily_usd) - app.state.store.spent_since(
        user["id"], midnight)
    if left <= 0.001:
        raise HTTPException(429, "you've used today's allowance; it resets at midnight")
    if opts is not None:
        opts.max_cost_usd = round(min(opts.max_cost_usd or left, left), 4)


def require_admin(request: Request) -> None:
    """Machine, key and limit changes: from this computer, or anywhere with CF_ADMIN_PASSWORD."""
    if _cross_site(request):
        raise HTTPException(403, "cross-site request refused")
    if app.state.accounts is not None:
        user = _user(request)
        if not user or not user["is_admin"]:
            raise HTTPException(403, "only an admin can change this")
        return
    password = app.state.settings.admin_password
    if password:
        given = request.headers.get("x-admin-password") or ""
        if not hmac.compare_digest(given.encode(), password.encode()):
            raise HTTPException(401, "admin password required")
    elif (request.client.host if request.client else "") not in LOOPBACK:
        raise HTTPException(403, "settings can only be changed from the computer running CinemaFabric "
                                 "(or set CF_ADMIN_PASSWORD to allow it from elsewhere)")


ADMIN = [Depends(require_admin)]


def _admin_if_accounts(request: Request) -> None:
    if app.state.accounts is not None:
        require_admin(request)


ADMIN_IF_ACCOUNTS = [Depends(_admin_if_accounts)]


def _check_room() -> None:
    if len(app.state.tasks) >= MAX_WAITING_JOBS:
        raise HTTPException(429, "too many films are already queued; try again when one finishes")


def _start(state: dict, work: str = "run", **kwargs) -> None:
    """Run a production (or a single-shot redo) in the background, one at a time."""
    production = Production(state, app.state.fabric, app.state.store, app.state.settings.data_dir, slot=app.state.slot)
    production.work = work

    async def runner():
        acquired = False
        try:
            await app.state.slot.acquire()
            acquired = True
            if work == "run":
                await production.run()
            elif work == "direct":
                await production.apply_plan()
            elif work == "commentary":
                await production.commentary()
            else:
                await production.redo_shot(**kwargs)
        except asyncio.CancelledError:
            if work == "run":
                production.stopped()
            raise
        finally:
            if acquired:
                app.state.slot.release()

    task = asyncio.create_task(runner())
    app.state.tasks[state["id"]] = task
    app.state.productions[state["id"]] = production

    def done(_):
        app.state.tasks.pop(state["id"], None)
        app.state.productions.pop(state["id"], None)

    task.add_done_callback(done)


@app.get("/api/options")
async def options():
    return {**catalog(), "mode": app.state.settings.mode, "cloud_text_model": cloud_model("text"),
            "exports": [{"id": k, "label": v["label"]} for k, v in exporter.PRESETS.items()]}


def _owner(request: Request) -> str | None:
    user = _user(request)
    return user["id"] if user else None


@app.post("/api/jobs")
async def create_job(opts: JobOptions, request: Request):
    try:
        opts.validate_choices()
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    _check_room()
    _allowance(request, opts)
    try:
        cast = app.state.library.pick(opts.cast_ids, _owner(request))
    except KeyError as e:
        raise HTTPException(422, str(e).strip("'")) from e
    state = new_state(uuid.uuid4().hex[:10], opts)
    state["library_cast"] = cast
    state["owner"] = _owner(request)
    app.state.store.save(state)
    _start(state)
    return state


@app.get("/api/jobs")
async def list_jobs(request: Request):
    user = _user(request)
    return {"jobs": app.state.store.list(owner=None if user is None or user["is_admin"] else user["id"])}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    state = app.state.store.get(job_id)
    if not state:
        raise HTTPException(404, "job not found")
    return state


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str, request: Request):
    old = app.state.store.get(job_id)
    if not old:
        raise HTTPException(404, "job not found")
    if job_id in app.state.tasks:
        raise HTTPException(409, "job is still running")
    _check_room()
    _allowance(request)
    opts = JobOptions.model_validate({**old["options"], "fresh": False})
    state = new_state(job_id, opts)
    state["created"] = old["created"]
    state["library_cast"] = old.get("library_cast") or []
    state["previously"] = old.get("previously")
    state["owner"] = old.get("owner")
    app.state.store.save(state)
    _start(state)
    return state


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    """Stop a film that's still being made. Edits to a finished film can't be stopped, since they could leave it half-changed."""
    if not app.state.store.get(job_id):
        raise HTTPException(404, "job not found")
    task, production = app.state.tasks.get(job_id), app.state.productions.get(job_id)
    if not task or not production or production.work != "run":
        raise HTTPException(409, "this film isn't being made right now")
    task.cancel()
    return {"ok": True}


class NextEpisode(BaseModel):
    idea: str = Field(default="", max_length=600)


@app.post("/api/jobs/{job_id}/next")
async def next_episode(job_id: str, body: NextEpisode, request: Request):
    """A new film that carries on from a finished one, with the same cast, sheets and settings."""
    old = app.state.store.get(job_id)
    if not old or old.get("status") != "done" or not old.get("story"):
        raise HTTPException(409, "only a finished film can have a next episode")
    folder = (app.state.settings.data_dir / "jobs" / job_id / "cast").resolve()
    cast = []
    for c in old.get("cast", []):
        sheet = (folder / c["image"].split("/cast/", 1)[-1].split("?")[0]).resolve()
        if sheet.is_relative_to(folder) and sheet.exists():
            cast.append({"name": c["name"], "look": c["look"], "sheet": str(sheet)})
    story = old["story"]
    idea = body.idea.strip() if len(body.idea.strip()) >= 8 else f"What happens next in {story['title']}"
    opts = JobOptions.model_validate({**old["options"], "prompt": idea, "fresh": False, "cast_ids": []})
    _check_room()
    _allowance(request, opts)
    state = new_state(uuid.uuid4().hex[:10], opts)
    state["library_cast"] = cast
    episode = ((old.get("previously") or {}).get("episode") or 1) + 1
    state["previously"] = {"job": job_id, "title": story["title"], "logline": story["logline"],
                           "beats": story.get("beats") or [], "episode": episode}
    state["owner"] = _owner(request)
    app.state.store.save(state)
    _start(state)
    return state


class Approval(BaseModel):
    shots: list[dict] = Field(max_length=200)


@app.post("/api/jobs/{job_id}/approve")
async def approve_script(job_id: str, body: Approval):
    production = app.state.productions.get(job_id)
    if production is None:
        raise HTTPException(409, "this run isn't waiting for a script review")
    try:
        production.apply_edits(body.shots)
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, str(e)) from e
    return production.state


class Redo(BaseModel):
    visual: str | None = Field(default=None, max_length=1000)
    note: str = Field(default="", max_length=300)


@app.post("/api/jobs/{job_id}/shots/{index}/redo")
async def redo_shot(job_id: str, index: int, body: Redo, request: Request):
    state = app.state.store.get(job_id)
    if not state:
        raise HTTPException(404, "job not found")
    if job_id in app.state.tasks:
        raise HTTPException(409, "this run is still going; wait for it to finish")
    if not (0 <= index < len(state["shots"])) or not state["shots"][index].get("image"):
        raise HTTPException(422, "that shot has no picture to redo")
    _check_room()
    _allowance(request)
    _start(state, "redo", i=index, visual=body.visual, note=body.note)
    return state


class DirectorNote(BaseModel):
    note: str = Field(min_length=3, max_length=600)


def _finished(job_id: str) -> dict:
    state = app.state.store.get(job_id)
    if not state:
        raise HTTPException(404, "job not found")
    if job_id in app.state.tasks or job_id in app.state.planning:
        raise HTTPException(409, "this film is busy; wait for it to finish")
    if state["status"] not in ("done", "failed") or not state["assets"].get("video"):
        raise HTTPException(409, "notes work on a finished film")
    return state


@app.post("/api/jobs/{job_id}/director")
async def director_note(job_id: str, body: DirectorNote):
    state = _finished(job_id)
    production = Production(state, app.state.fabric, app.state.store, app.state.settings.data_dir)
    app.state.planning.add(job_id)
    try:
        await production.direct(body.note)
    except ContentBlocked as e:
        raise HTTPException(422, str(e)) from e
    except (FabricError, RuntimeError) as e:
        raise HTTPException(502, f"couldn't plan that note: {str(e)[:300]}") from e
    finally:
        app.state.planning.discard(job_id)
    return production.state


@app.post("/api/jobs/{job_id}/director/apply")
async def director_apply(job_id: str, request: Request):
    state = _finished(job_id)
    if not (state.get("director") or {}).get("pending"):
        raise HTTPException(409, "there's no plan to apply")
    _check_room()
    _allowance(request)
    _start(state, "direct")
    return state


@app.post("/api/jobs/{job_id}/commentary")
async def record_commentary(job_id: str, request: Request):
    state = _finished(job_id)
    if state["status"] != "done":
        raise HTTPException(409, "commentary needs a finished film")
    _check_room()
    _allowance(request)
    _start(state, "commentary")
    return state


class Ending(BaseModel):
    idea: str = Field(default="", max_length=600)


@app.post("/api/jobs/{job_id}/ending")
async def alternate_ending(job_id: str, body: Ending, request: Request):
    """A copy of a finished film with a new ending: the earlier shots, voices and score are reused as they are."""
    old = _finished(job_id)
    if old["status"] != "done":
        raise HTTPException(409, "only a finished film can get another ending")
    _check_room()
    variant_opts = JobOptions.model_validate(old["options"])
    _allowance(request, variant_opts)
    new_id = uuid.uuid4().hex[:10]
    jobs = app.state.settings.data_dir / "jobs"
    await asyncio.to_thread(shutil.copytree, jobs / job_id, jobs / new_id,
                            ignore=shutil.ignore_patterns("exports", "film_*.mp4", "commentary.*", "trailer.mp4",
                                                          "release.md", "thumbnail.jpg"))
    state = json.loads(json.dumps(old).replace(f"/media/{job_id}/", f"/media/{new_id}/"))
    for inherited in ("share", "reports", "release", "agents", "credentials", "commentary"):
        state.pop(inherited, None)
    state["options"] = variant_opts.model_dump(mode="json")
    for asset in ("exports", "release", "thumbnail", "trailer", "commentary", "commentary_vtt"):
        state["assets"].pop(asset, None)
    state.update(id=new_id, created=time.time(), owner=_owner(request), director={"turns": [], "pending": None},
                 variant_of={"job": job_id, "title": old["story"]["title"], "idea": body.idea.strip()})
    production = Production(state, app.state.fabric, app.state.store, app.state.settings.data_dir)
    production.restore()
    first, last = director.ending_range(production.shots)
    app.state.planning.add(new_id)
    try:
        pending = await production.direct(director.ending_note(body.idea, first, last), from_shot=first)
        if not pending["edits"]:
            raise RuntimeError(pending["reply"] or "the director didn't change anything")
    except Exception as e:
        app.state.store.delete(new_id)
        await asyncio.to_thread(shutil.rmtree, jobs / new_id, True)
        if isinstance(e, ContentBlocked):
            raise HTTPException(422, str(e)) from e
        raise HTTPException(502, f"couldn't plan another ending: {str(e)[:300]}") from e
    finally:
        app.state.planning.discard(new_id)
    _start(production.state, "direct")
    return production.state


@app.post("/api/jobs/{job_id}/director/discard")
async def director_discard(job_id: str):
    state = _finished(job_id)
    if state.get("director"):
        state["director"]["pending"] = None
        app.state.store.save(state)
    return state


_recent: dict[str, deque] = defaultdict(deque)


def _rate_limited(request: Request) -> None:
    now, seen = time.monotonic(), _recent[request.client.host if request.client else ""]
    while seen and now - seen[0] > 60:
        seen.popleft()
    if len(seen) >= SUGGESTIONS_PER_MINUTE:
        raise HTTPException(429, "slow down a little; suggestions are limited to a few a minute")
    seen.append(now)


class IdeaText(BaseModel):
    idea: str = Field(min_length=8, max_length=1200)


async def _suggest(call):
    try:
        return await call
    except (FabricError, ValidationError) as e:
        raise HTTPException(503, f"no model could make a suggestion right now ({str(e)[:160]})") from e


class SignUp(BaseModel):
    email: str = Field(max_length=200)
    name: str = Field(default="", max_length=80)
    password: str = Field(min_length=1, max_length=200)


class SignIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


def _accounts() -> Accounts:
    if app.state.accounts is None:
        raise HTTPException(404, "accounts are turned off on this studio")
    return app.state.accounts


def _signed_in(request: Request, token: str, user: dict) -> Response:
    response = JSONResponse({"user": user})
    response.set_cookie(COOKIE, token, max_age=14 * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    return response


@app.get("/api/auth/me")
async def me(request: Request):
    accounts = app.state.accounts
    return {"accounts": accounts is not None, "user": _user(request),
            "can_sign_up": accounts is not None and (accounts.count() == 0 or app.state.settings.signup == "open")}


@app.post("/api/auth/sign-up", dependencies=[Depends(_rate_limited)])
async def sign_up(body: SignUp, request: Request):
    accounts = _accounts()
    first = accounts.count() == 0
    if not first and app.state.settings.signup != "open":
        raise HTTPException(403, "ask an admin for an account")
    try:
        user = await asyncio.to_thread(accounts.create, body.email, body.name, body.password, first)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _signed_in(request, await asyncio.to_thread(accounts.sign_in, body.email, body.password) or "", user)


@app.post("/api/auth/sign-in", dependencies=[Depends(_rate_limited)])
async def sign_in(body: SignIn, request: Request):
    accounts = _accounts()
    token = await asyncio.to_thread(accounts.sign_in, body.email, body.password)
    if not token:
        raise HTTPException(401, "wrong email or password")
    return _signed_in(request, token, accounts.user_for(token) or {})


@app.post("/api/auth/sign-out")
async def sign_out(request: Request):
    _accounts().sign_out(request.cookies.get(COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE)
    return response


@app.get("/api/people", dependencies=ADMIN)
async def people_list():
    return {"people": _accounts().all()}


@app.post("/api/people", dependencies=ADMIN)
async def people_add(body: SignUp):
    try:
        return await asyncio.to_thread(_accounts().create, body.email, body.name, body.password)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/api/suggest/ideas", dependencies=[Depends(_rate_limited)])
async def suggest_ideas(fresh: bool = False):
    return await _suggest(suggest.ideas(app.state.fabric, fresh))


@app.post("/api/suggest/polish", dependencies=[Depends(_rate_limited)])
async def suggest_polish(body: IdeaText):
    return await _suggest(suggest.polish(app.state.fabric, body.idea))


@app.post("/api/suggest/settings", dependencies=[Depends(_rate_limited)])
async def suggest_settings(body: IdeaText):
    return await _suggest(suggest.settings(app.state.fabric, body.idea))


@app.get("/api/jobs/{job_id}/decisions")
async def job_decisions(job_id: str):
    return await app.state.fabric.get("/v1/decisions", job_id=job_id, limit=500)


@app.get("/api/estimate")
async def estimate(format: str = "trailer", length: str = "fixed", seconds: int | None = None,
                   preference: str = "local_first", premium_script: bool = False, critic: bool = True,
                   narration: bool = True):
    if format not in FORMATS:
        raise HTTPException(422, "unknown format")
    try:
        workers = {w["id"]: w for w in (await app.state.fabric.get("/v1/workers"))["workers"]}
    except Exception:
        workers = {}
    f = FORMATS[format]
    args = (workers, preference, premium_script, critic, narration)
    note = "Based on which machines are up right now. Anything already cached is free."
    if length == "auto":
        low = _estimate(runtime(format, f["min"]), *args)
        high = _estimate(runtime(format, f["max"]), *args)
        return {"lines": high["lines"], "total_usd": high["total_usd"], "min_usd": low["total_usd"],
                "max_usd": high["total_usd"], "label": f"{f['min']}–{f['max']} s, picked by the story",
                "assumes": note + " The breakdown shows the longest case."}
    rt = runtime(format, seconds or f["default"])
    return {**_estimate(rt, *args), "label": rt.label, "assumes": note}


def _local(workers: dict, kind: str, quality: int = 1, vision: bool = False) -> str | None:
    for w in workers.values():
        if w.get("is_cloud") or w.get("status") != "up" or w.get("id") == "fabric-synth":
            continue
        if any(o["kind"] == kind and o["quality"] >= quality and (o["vision"] or not vision) for o in w["offers"]):
            return w["label"]
    return None


def _estimate(rt: Runtime, workers: dict, preference: str, premium_script: bool, critic: bool, narration: bool) -> dict:
    cloud_first = preference == "cloud_first"
    image_shots = rt.shots - rt.max_cards
    lines = []

    def line(item: str, count: int, kind: str, cloud_usd: float, model: str, quality: int = 2, vision: bool = False):
        where = None if cloud_first else _local(workers, kind, quality, vision)
        lines.append({"item": item, "count": count, "where": where or model, "usd": 0.0 if where else cloud_usd})

    premium_script = premium_script or rt.format == "movie"
    text_model = cloud_model("text_premium" if premium_script else "text")
    line("Story and script", 2, "text.generate", 2 * pricing.text_cost(text_model, 2500, 600 + 160 * rt.shots),
         text_model, 3 if premium_script else 2)
    if rt.format == "movie":
        line("Story editor", 1, "text.generate", pricing.text_cost(text_model, 3000, 400 + 70 * rt.shots), text_model, 3)
    line("Shot prompts", math.ceil(image_shots / SHOT_PROMPT_BATCH), "text.generate",
         pricing.text_cost(cloud_model("text"), 1500, 200 + 160 * SHOT_PROMPT_BATCH), cloud_model("text"), 1)
    if critic:
        line("Frame checks", image_shots, "text.generate", image_shots * pricing.text_cost(cloud_model("text"), 1400, 150),
             cloud_model("text"), 2, vision=True)
    images = image_shots + 3 + (math.ceil(image_shots * 0.2) if critic else 0)
    lines.append({"item": "Images (frames, characters, poster)", "count": images, "where": cloud_model("image"),
                  "usd": images * pricing.IMAGE_PRICE})
    mattes = image_shots // 2
    line("Cutouts for parallax", mattes, "image.segment", mattes * pricing.SAM_IMAGE_PRICE, cloud_model("segment"))
    if narration:
        lines.append({"item": "Narration check", "count": 1, "where": cloud_model("transcribe"),
                      "usd": pricing.TRANSCRIBE_PER_HOUR * (rt.seconds * (0.6 if rt.format == "trailer" else 0.8)) / 3600})
    return {"lines": lines, "total_usd": round(sum(item["usd"] for item in lines), 4)}


@app.get("/api/fabric/workers")
async def fabric_workers():
    return await app.state.fabric.get("/v1/workers")


@app.post("/api/fabric/workers/{worker_id}/{action}", dependencies=ADMIN)
async def fabric_worker_action(worker_id: str, action: str):
    return await app.state.fabric.post(f"/v1/workers/{worker_id}/{action}")


@app.get("/api/fabric/ledger", dependencies=ADMIN_IF_ACCOUNTS)
async def fabric_ledger():
    return await app.state.fabric.get("/v1/ledger")


@app.get("/api/fabric/decisions", dependencies=ADMIN_IF_ACCOUNTS)
async def fabric_decisions(limit: int = 100):
    return await app.state.fabric.get("/v1/decisions", limit=limit)


async def _admin(method: str, path: str, body: dict | None = None):
    """Forward an admin call to the fabric; the fabric token never reaches the browser."""
    status, data = await app.state.fabric.call(method, path, json=body)
    if status >= 400:
        raise HTTPException(status, data.get("detail", "fabric error"))
    return data


@app.post("/api/fabric/machines/inspect", dependencies=ADMIN)
async def inspect_machine(body: dict):
    return await _admin("POST", "/v1/machines/inspect", body)


@app.post("/api/fabric/machines", dependencies=ADMIN)
async def add_machine(body: dict):
    return await _admin("POST", "/v1/machines", body)


@app.delete("/api/fabric/machines/{machine_id}", dependencies=ADMIN)
async def remove_machine(machine_id: str):
    return await _admin("DELETE", f"/v1/machines/{machine_id}")


@app.get("/api/fabric/settings", dependencies=ADMIN)
async def fabric_settings():
    return await _admin("GET", "/v1/settings")


@app.put("/api/fabric/api-key", dependencies=ADMIN)
async def set_api_key(body: dict):
    return await _admin("PUT", "/v1/settings/api-key", body)


@app.put("/api/fabric/limit", dependencies=ADMIN)
async def set_limit(body: dict):
    return await _admin("PUT", "/v1/ledger/limit", body)


@app.get("/api/fabric/join", dependencies=ADMIN)
async def join_command():
    """What to run on another GPU machine so it adds itself."""
    s = app.state.settings
    token = machines.join_token(s)
    fabric = s.fabric_url.replace("127.0.0.1", "<this-pc-ip>").replace("localhost", "<this-pc-ip>")
    return {
        "fabric_url": fabric,
        "token_hint": f"…{token[-4:]}",
        "command": f"CF_JOIN_TOKEN={token} python -m cinemafabric.node --join {fabric}",
        "command_powershell": f"$env:CF_JOIN_TOKEN='{token}'; python -m cinemafabric.node --join {fabric}",
        "note": "Run this on any machine with a CinemaFabric checkout. It finds the machine's GPUs and Ollama models "
                "and adds them. The fabric must listen on your network (--host 0.0.0.0) for other machines to reach it.",
    }


@app.get("/api/characters")
async def characters(request: Request):
    return {"characters": app.state.library.all(_owner(request))}


@app.post("/api/jobs/{job_id}/cast/{index}/save")
async def save_character(job_id: str, index: int, request: Request):
    state = app.state.store.get(job_id)
    if not state or not (0 <= index < len(state.get("cast", []))):
        raise HTTPException(404, "no such character in this film")
    c = state["cast"][index]
    sheet = app.state.settings.data_dir / "jobs" / job_id / "cast" / c["image"].split("/cast/", 1)[1].split("?")[0]
    if not sheet.resolve().is_relative_to((app.state.settings.data_dir / "jobs" / job_id).resolve()) or not sheet.exists():
        raise HTTPException(404, "the character sheet is missing")
    return app.state.library.save(c["name"], c["look"], sheet, job_id, _owner(request))


@app.delete("/api/characters/{char_id}")
async def delete_character(char_id: str, request: Request):
    if not app.state.library.delete(char_id, _owner(request)):
        raise HTTPException(404, "no such character")
    return {"ok": True}


class ShareSetting(BaseModel):
    public: bool


@app.post("/api/jobs/{job_id}/share")
async def share_job(job_id: str, body: ShareSetting):
    state = _finished(job_id)
    held = _held(state)
    if body.public and held:
        raise HTTPException(409, f"this film can't get a public link while a check is unresolved: {held[0]['check']} "
                                 f"({held[0].get('review') or held[0]['verdict']}). Redo the shot or change the note first.")
    if body.public:
        state["share"] = state.get("share") or {"id": secrets.token_urlsafe(12), "created": time.time()}
    else:
        state.pop("share", None)
    app.state.store.save(state)
    return state


@app.post("/api/jobs/{job_id}/export/{preset}")
async def export_film(job_id: str, preset: str):
    if preset not in exporter.PRESETS:
        raise HTTPException(404, "unknown export preset")
    state = _finished(job_id)
    folder = app.state.settings.data_dir / "jobs" / job_id
    out = folder / f"film_{preset}.mp4"
    app.state.planning.add(job_id)
    try:
        await asyncio.to_thread(exporter.export, folder / "film.mp4", out, preset, (state.get("story") or {}).get("title", ""))
        state = app.state.store.get(job_id) or state
        production = Production(state, app.state.fabric, app.state.store, app.state.settings.data_dir)
        production.restore()
        await production.sign([out.name])
        state["assets"].setdefault("exports", {})[preset] = f"/media/{job_id}/{out.name}?v={int(out.stat().st_mtime)}"
        packed = await asyncio.to_thread(write_zip, folder, (state.get("story") or {}).get("title") or job_id)
        state["assets"]["zip"] = f"/media/{job_id}/{packed.name}?v={int(packed.stat().st_mtime)}"
        app.state.store.save(state)
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e
    finally:
        app.state.planning.discard(job_id)
    if (bucket := media_store(app.state.settings)) is not None:
        await asyncio.to_thread(bucket.upload_job, folder, job_id)
    return state


REPORT_REASONS = {
    "real_person": "Shows a real person",
    "copyright": "Copies someone else's work",
    "harmful": "Harmful or offensive",
    "other": "Something else",
}
REPORTS_PER_JOB = 50
_reports: dict[str, deque] = defaultdict(deque)


class Report(BaseModel):
    reason: str = Field(pattern="^(" + "|".join(REPORT_REASONS) + ")$")
    note: str = Field(default="", max_length=500)


@app.post("/api/report/{share_id}")
async def report_film(share_id: str, body: Report, request: Request):
    """Anyone watching a shared film can flag it; the owner and admins see reports in the Safety panel."""
    now, seen = time.monotonic(), _reports[request.client.host if request.client else ""]
    while seen and now - seen[0] > 3600:
        seen.popleft()
    if len(seen) >= 5:
        raise HTTPException(429, "thanks, we've got your reports; please try again later")
    seen.append(now)
    state = _shared(share_id)
    reports = state.setdefault("reports", [])
    if len(reports) >= REPORTS_PER_JOB:
        return {"ok": True, "recorded": False, "note": "This film has already been reported many times; the owner has been told."}
    reports.append({"reason": body.reason, "label": REPORT_REASONS[body.reason], "note": body.note.strip(),
                    "at": time.time()})
    app.state.store.save(state)
    return {"ok": True, "recorded": True}


@app.get("/api/reports", dependencies=ADMIN)
async def reports():
    """Every film that viewers have reported, newest report first."""
    found = []
    for summary in app.state.store.list(limit=500):
        state = app.state.store.get(summary["id"])
        if state and state.get("reports"):
            found.append({"id": state["id"], "title": (state.get("story") or {}).get("title"), "shared": bool(state.get("share")),
                          "reports": state["reports"]})
    return {"films": sorted(found, key=lambda f: -max(r["at"] for r in f["reports"]))}


def _held(state: dict) -> list[dict]:
    """Checks that keep a film from being shown publicly."""
    return [c for c in state.get("checks", []) if c.get("verdict") in ("still flagged", "blocked")]


def _shared(share_id: str) -> dict:
    state = app.state.store.find_shared(share_id)
    if not state or not state["assets"].get("video") or _held(state):
        raise HTTPException(404, "this film isn't shared")
    return state


@app.get("/watch/{share_id}", response_class=HTMLResponse, include_in_schema=False)
async def watch(share_id: str, request: Request):
    return HTMLResponse(share.watch_page(_shared(share_id), str(request.base_url)))


@app.get("/embed/{share_id}", response_class=HTMLResponse, include_in_schema=False)
async def embed(share_id: str):
    return HTMLResponse(share.embed_page(_shared(share_id)))


def static_file(root: Path, path: str) -> Path | None:
    """A file under the built UI, or None (including for anything that resolves outside it)."""
    candidate = (root / path).resolve()
    return candidate if path and candidate.is_relative_to(root) and candidate.is_file() else None


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets", check_dir=False), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        return FileResponse(static_file(FRONTEND_DIST, path) or FRONTEND_DIST / "index.html")
