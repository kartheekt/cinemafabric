"""Fabric service."""

import hmac
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ValidationError

from ..config import get_settings
from ..profile import allowed_licences, family_allowed, profile
from . import machines, metrics
from .backends.base import Backend
from .backends.muse import MuseCloudBackend
from .cache import ResultCache
from .ledger import Ledger
from .registry import Registry
from .router import NoRouteError, Router
from .topology import build_backends, materialise
from .types import TaskRequest, TaskResult

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cinemafabric.fabric")

MAX_BODY = 64 * 2**20
JOIN_ROUTES = {("GET", "/v1/policy"), ("POST", "/v1/machines")}


class BodyLimit:
    """Caps request bodies as they stream in, so chunked uploads can't skip the Content-Length check."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > MAX_BODY:
                raise HTTPException(413, "request too large")
            return message

        await self.app(scope, limited, send)


def _cloud(backend: Backend) -> MuseCloudBackend:
    inner = getattr(backend, "_template", backend)
    assert isinstance(inner, MuseCloudBackend)
    return inner


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    backends = build_backends(settings)
    registry = Registry(backends, settings.health_interval_s)
    mode = "mock" if settings.mock else "live"
    budget = profile()["budget"]
    ledger = Ledger(settings.path("fabric.db"), budget["limit_usd"] or None, budget["stop_usd"] or None,
                    simulated=settings.mock)
    cache = ResultCache(settings.data_dir / "cache" / mode)
    store = machines.MachineStore(settings.path("fabric.db"))
    for spec in store.all():
        try:
            await registry.add(materialise(spec, settings.mock))
        except ValueError as e:
            log.warning("skipping saved machine %s: %s", spec.id, e)
    cloud = next((b for b in backends if isinstance(getattr(b, "_template", b), MuseCloudBackend)), None)
    saved_key = ledger.get_pref("meta_api_key")
    if cloud is not None and saved_key:
        _cloud(cloud).set_key(saved_key)
    await registry.start()
    app.state.cloud = cloud
    app.state.settings = settings
    app.state.registry = registry
    app.state.ledger = ledger
    app.state.machines = store
    app.state.join_token = machines.join_token(settings)
    app.state.fabric_token = machines.fabric_token(settings)
    app.state.router = Router(registry, ledger, cache, allowed_licences())
    yield
    await registry.stop()
    ledger.db.close()


app = FastAPI(title="CinemaFabric · Fabric", version="0.1.0", lifespan=lifespan)
app.add_middleware(BodyLimit)


def _matches(given: str | None, expected: str) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), expected.encode())


@app.middleware("http")
async def guard(request: Request, call_next):
    """The fabric token opens the whole API; the join token only lets a node read policy and register machines."""
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return JSONResponse({"detail": "request too large"}, status_code=413)
    if request.url.path.startswith("/v1/") or request.url.path == "/metrics":
        bearer = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        ok = _matches(request.headers.get("x-fabric-token") or bearer, app.state.fabric_token) or (
            (request.method, request.url.path) in JOIN_ROUTES
            and _matches(request.headers.get("x-join-token"), app.state.join_token)
        )
        if not ok:
            return JSONResponse({"detail": "missing or wrong token"}, status_code=401)
    return await call_next(request)


class InspectRequest(BaseModel):
    kind: str
    base_url: str
    secret: str | None = None


@app.post("/v1/machines/inspect")
async def inspect_machine(req: InspectRequest):
    try:
        return await machines.inspect(req.kind, req.base_url, req.secret)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"couldn't reach {req.base_url} ({e.__class__.__name__})") from e


@app.get("/v1/policy")
async def policy():
    p = profile()
    return {"families": p["models"]["families"], "ignore": p["models"].get("ignore", []),
            "licences": sorted(allowed_licences()), "node": p.get("node", {})}


@app.get("/v1/machines")
async def list_machines():
    return {"machines": [machines.describe(s) for s in app.state.machines.all()]}


@app.post("/v1/machines")
async def add_machine(spec: machines.MachineSpec):
    registry = app.state.registry
    try:
        spec.base_url = await machines.check_target(spec.base_url)
        spec.id = machines.slug(spec.id or spec.label)
        if spec.kind == "ollama" and spec.model and not family_allowed(spec.model):
            raise ValueError(f"{spec.model} isn't in an allowed model family")
        existing = registry.by_id.get(spec.id)
        if existing is not None and existing.source != "added":
            raise ValueError(f"'{spec.id}' is already defined in the profile")
        if spec.kind == "ollama" and spec.model and (spec.vision is None or spec.quality is None):
            info = await machines.inspect("ollama", spec.base_url, spec.secret)
            found = next((m for m in info["models"] if m["name"] == spec.model), None)
            if found is None:
                raise ValueError(f"{spec.model} isn't pulled on that server")
            spec.vision = found["vision"] if spec.vision is None else spec.vision
            spec.quality = found["quality"] if spec.quality is None else spec.quality
        backend = materialise(spec, app.state.settings.mock)
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, str(e)) from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"couldn't reach {spec.base_url} ({e.__class__.__name__})") from e
    app.state.machines.save(spec)
    await registry.add(backend)
    return {"ok": True, "machine": machines.describe(spec)}


@app.delete("/v1/machines/{machine_id}")
async def remove_machine(machine_id: str):
    if not app.state.machines.delete(machine_id):
        raise HTTPException(404, "only machines added from the UI or by a worker can be removed here")
    await app.state.registry.remove(machine_id)
    return {"ok": True}


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus():
    return PlainTextResponse(metrics.render(app.state.registry, app.state.ledger),
                             media_type="text/plain; version=0.0.4")


@app.get("/health")
async def health():
    return {"ok": True, "mode": app.state.settings.mode}


@app.get("/v1/workers")
async def workers():
    return {"mode": app.state.settings.mode, "workers": app.state.registry.snapshot()}


@app.post("/v1/workers/{worker_id}/{action}")
async def worker_action(worker_id: str, action: str):
    registry = app.state.registry
    if worker_id not in registry.by_id:
        raise HTTPException(404, f"unknown worker {worker_id}")
    if action not in ("enable", "disable"):
        raise HTTPException(400, "action must be enable or disable")
    registry.set_enabled(worker_id, action == "enable")
    if action == "enable":
        await registry.probe_all()
    return {"ok": True, "worker": worker_id, "enabled": action == "enable"}


@app.post("/v1/tasks", response_model=TaskResult)
async def run_task(req: TaskRequest):
    try:
        return await app.state.router.run(req)
    except NoRouteError as e:
        raise HTTPException(503, str(e)) from e


@app.get("/v1/ledger")
async def ledger_summary():
    return app.state.ledger.summary()


class ApiKeyRequest(BaseModel):
    key: str | None = None


def _key_info() -> dict:
    cloud = _cloud(app.state.cloud)
    key = cloud.api_key
    return {"api_key_set": bool(key), "api_key_hint": f"…{key[-4:]}" if key else None, "api_key_source": cloud.key_source}


@app.get("/v1/settings")
async def get_settings_info():
    return _key_info()


@app.put("/v1/settings/api-key")
async def set_api_key(req: ApiKeyRequest):
    cloud = _cloud(app.state.cloud)
    key = (req.key or "").strip()
    if key:
        if not app.state.settings.mock:
            ok, detail = await cloud.check_key(key)
            if not ok:
                raise HTTPException(422, detail)
        app.state.ledger.set_pref("meta_api_key", key)
        cloud.set_key(key)
    else:
        app.state.ledger.set_pref("meta_api_key", None)
        cloud.set_key(None)
    await app.state.registry.probe_all()
    return _key_info()


class LimitRequest(BaseModel):
    budget_usd: float | None = None


@app.put("/v1/ledger/limit")
async def set_limit(req: LimitRequest):
    if req.budget_usd is not None and req.budget_usd < 0:
        raise HTTPException(422, "limit can't be negative")
    app.state.ledger.set_limit(req.budget_usd or None)
    return app.state.ledger.summary()


@app.get("/v1/ledger/jobs/{job_id}")
async def ledger_job(job_id: str):
    return app.state.ledger.job_summary(job_id)


@app.get("/v1/decisions")
async def decisions(job_id: str | None = None, limit: int = 200):
    return {"decisions": app.state.ledger.decisions(job_id, max(1, min(limit, 10000 if job_id else 1000)))}
