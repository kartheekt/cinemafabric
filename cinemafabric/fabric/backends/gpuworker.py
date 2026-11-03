"""Client for a local model worker (cinemafabric.worker)."""

import httpx

from ...profile import catalog
from ..types import TaskKind
from .base import Backend, BackendError, ExecResult, Offer

CAP_KINDS = {"tts": TaskKind.TTS, "music": TaskKind.MUSIC, "segment": TaskKind.SEGMENT, "watermark": TaskKind.WATERMARK,
             "depth": TaskKind.DEPTH}
CAP_ROUTES = {TaskKind.TTS: "/tts", TaskKind.MUSIC: "/music", TaskKind.SEGMENT: "/segment",
              TaskKind.WATERMARK: "/watermark", TaskKind.DEPTH: "/depth"}
MODEL_LICENCES = {"facebook/sam2.1-hiera-base-plus": "apache-2.0", "facebook/sam2.1-hiera-large": "apache-2.0"}


def allowed_models(cap: str, licences: set[str]) -> list[str]:
    spec = catalog()["local"][cap]
    return [m for m in spec["models"] if MODEL_LICENCES.get(m, spec["licence"]) in licences]


def cap_offer(cap: str) -> Offer:
    spec = catalog()["local"][cap]
    model = spec["models"][-1] if cap == "segment" else spec["models"][0]
    return Offer(CAP_KINDS[cap], model, int(spec["quality"]), typical_s=float(spec["typical_s"]),
                 licence=MODEL_LICENCES.get(model, spec["licence"]))


class GpuWorkerBackend(Backend):
    location = "lan"

    def __init__(self, id: str, label: str, device: str, base_url: str, caps: list[str], secret: str | None = None):
        self.id = id
        self.label = label
        self.device = device
        self.max_concurrency = 1
        self.offers = [cap_offer(c) for c in caps if c in CAP_KINDS]
        self.unavailable: dict[TaskKind, str] = {}
        self.memory: dict | None = None
        headers = {"X-Worker-Secret": secret} if secret else None
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(600.0, connect=5.0),
                                         headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def probe(self) -> tuple[bool, str]:
        try:
            r = await self._client.get("/health", timeout=4.0)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return False, f"worker unreachable ({e.__class__.__name__})"
        info = r.json()
        self.memory = info.get("memory")
        self.unavailable = {CAP_KINDS[c]: f"model failed to load: {why}" for c, why in (info.get("unavailable") or {}).items()
                            if c in CAP_KINDS}
        if info.get("segmenter"):
            for offer in self.offers:
                if offer.kind == TaskKind.SEGMENT:
                    offer.model = info.get("segmenter_model", offer.model)
                    offer.quality = 2 if info["segmenter"] == "text" else 1
                    offer.licence = MODEL_LICENCES.get(offer.model, catalog()["local"]["segment"]["licence"])
        loaded = ", ".join(info.get("loaded", [])) or "no models loaded yet"
        return True, f"{info.get('device', 'device')} · {loaded}"

    async def execute(self, offer: Offer, payload: dict) -> ExecResult:
        try:
            r = await self._client.post(CAP_ROUTES[offer.kind], json=payload)
        except httpx.HTTPError as e:
            raise BackendError(f"worker transport error: {e.__class__.__name__}") from e
        if r.status_code != 200:
            raise BackendError(f"worker HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500)
        return ExecResult(r.json())
