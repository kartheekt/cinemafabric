"""In-process CPU fallbacks: a procedural score and a heuristic subject matte."""

import asyncio

from ... import procedural
from ..types import TaskKind
from .base import Backend, ExecResult, Offer


class SynthBackend(Backend):
    id = "fabric-synth"
    label = "Fabric CPU · procedural fallback"
    location = "fabric"
    device = "CPU"
    max_concurrency = 2

    def __init__(self):
        self.offers = [
            Offer(TaskKind.MUSIC, "procedural-score", 1, typical_s=300.0),
            Offer(TaskKind.SEGMENT, "center-weighted-matte", 1, typical_s=300.0),
        ]

    async def probe(self) -> tuple[bool, str]:
        return True, "always available"

    async def execute(self, offer: Offer, payload: dict) -> ExecResult:
        if offer.kind == TaskKind.MUSIC:
            out = await asyncio.to_thread(procedural.music_payload, payload.get("prompt", ""), float(payload.get("duration_s", 30)))
        else:
            out = await asyncio.to_thread(procedural.matte_payload, payload["image"])
        return ExecResult(out)
