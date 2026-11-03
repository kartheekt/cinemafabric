"""Mock mode: mirror the real topology but fabricate outputs locally with short simulated latency."""

import asyncio
import random
import re

from ... import procedural
from ...config import Settings
from ...util import b64e, wav_bytes
from ..types import TaskKind
from .base import Backend, ExecResult, Offer
from .gpuworker import GpuWorkerBackend
from .muse import MuseCloudBackend
from .ollama import OllamaBackend
from .synth import SynthBackend

WORDS = {
    "title": ["Low Tide", "Night Ferry", "Room 14", "Salt and Static"],
    "tagline": ["Keep the light on.", "Some calls you don't return.", "Nobody else heard it."],
    "visual": [
        "A lone figure on a windswept cliff at dusk, storm clouds gathering",
        "Close-up of trembling hands holding a glowing map",
        "A neon-lit city street reflected in rain puddles",
        "Wide shot of an ancient forest with shafts of golden light",
        "Silhouette against a burning sunset, coat flapping in the wind",
    ],
    "narration": [
        "Dad never let me near the lamp room.",
        "It started answering back on a Tuesday.",
        "I told the coast guard. They laughed.",
        "Same time every night. Same three flashes.",
        "",
        "",
    ],
    "logline": [
        "When the lighthouse on Kovalam point starts getting replies from under the water, its keeper's daughter "
        "has one week to work out who is signalling before the coast guard shuts it down.",
    ],
    "tone": ["quiet, uneasy, a little funny"],
    "setting": ["a lighthouse off the Kerala coast, monsoon season, 1987"],
    "music_mood": ["low cello drones, a slow tabla pulse, sparse piano, building"],
    "look": [
        "late 20s, wiry, cropped hair, oversized grey wool sweater, rubber boots, a torch on a lanyard",
        "60s, broad shoulders, white stubble, faded navy uniform shirt, reading glasses on a cord",
    ],
    "heading": ["EXT. LIGHTHOUSE - NIGHT", "INT. LAMP ROOM - NIGHT", "EXT. JETTY - DAWN"],
    "summary": ["Mara catches the reply for the first time and tries to record it."],
    "beat": ["The light blinks back.", "Nobody believes her.", "She rows out alone.", "Something surfaces."],
    "issue": [""],
    "reason": ["One location, two people and a single reveal, so it doesn't need long."],
}


SCRIPT_LETTER = {"Hindi": 0x0915, "Marathi": 0x0915, "Bengali": 0x0995, "Gujarati": 0x0A95, "Tamil": 0x0B95,
                 "Telugu": 0x0C15, "Kannada": 0x0C95, "Malayalam": 0x0D15, "Arabic": 0x0628, "Japanese": 0x3042,
                 "Korean": 0xAC00}


def _fake_value(name: str, schema: dict, prompt: str, rng: random.Random):
    t = schema.get("type")
    if "enum" in schema:
        return rng.choice(schema["enum"])
    if t == "object":
        return {k: _fake_value(k, v, prompt, rng) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        count = 3
        m = re.search(rf"(\d+)\s+{name}", prompt)
        if m:
            count = int(m.group(1))
        elif name == "scenes":
            count = 1
        elif name == "problems":
            count = 0
        return [_fake_value(name.rstrip("s"), schema.get("items", {}), prompt, rng) for _ in range(count)]
    if t in ("number", "integer"):
        if name == "runtime_seconds":
            fixed = re.search(r"runtime_seconds: return (\d+)", prompt)
            span = re.search(r"between (\d+) and (\d+) seconds", prompt)
            if fixed:
                return int(fixed.group(1))
            return rng.randint(int(span.group(1)), int(span.group(2))) if span else 30
        if name == "duration":
            m = re.search(r"(\d+)\s+shots totaling\s+(\d+)", prompt)
            return round(int(m.group(2)) / int(m.group(1)), 2) if m else 3.0
        if name == "score":
            return rng.choice([6, 7, 8, 9])
        if name == "shot" and (m := re.search(r"only shots (\d+) to (\d+)", prompt)):
            return rng.randint(int(m.group(1)), int(m.group(2)))
        if name == "shot" and (offered := re.findall(r"shot (\d+) \(up to", prompt)):
            return int(rng.choice(offered))
        return 1
    if t == "boolean":
        return name not in ("real_person", "brand_logo", "known_character", "stray_text", "harmful")
    if name == "title_card":
        return rng.choice(["THIS WINTER", "", "", "", "", "", "", "", "", ""])
    for key, bank in WORDS.items():
        if key in name:
            return rng.choice(bank)
    if name in ("character", "characters", "name"):
        return rng.choice(["Mara", "Theo", "Ish"])
    return f"Mock {name.replace('_', ' ')} for: {prompt[:60]}"


class MockBackend(Backend):
    def __init__(self, template: Backend):
        self.id = template.id
        self.label = template.label + " (mock)"
        self.location = template.location
        self.device = template.device
        self.is_cloud = template.is_cloud
        self.max_concurrency = template.max_concurrency
        self.offers = template.offers
        self._template = template

    async def probe(self) -> tuple[bool, str]:
        return True, "simulated"

    async def execute(self, offer: Offer, payload: dict) -> ExecResult:
        await asyncio.sleep(min(offer.typical_s * 0.04, 1.2) + random.uniform(0.05, 0.3))
        prompt = payload.get("prompt", "")
        rng = random.Random(hash((offer.model, prompt)) & 0xFFFF)
        if offer.kind == TaskKind.TEXT:
            if payload.get("json_schema", {}).get("name") == "translation":
                count = int(re.search(r"Return exactly (\d+) lines", prompt).group(1))
                language = re.search(r"spoken (\w+)", prompt).group(1)
                letter = SCRIPT_LETTER.get(language)
                word = chr(letter) * 3 + chr(letter + 1) * 2 if letter else language.lower()
                out = {"text": "", "json": {"lines": [f"{word} {word} {i + 1}" for i in range(count)]}}
            elif payload.get("json_schema"):
                data = _fake_value("root", payload["json_schema"]["schema"], prompt, rng)
                out = {"text": "", "json": data}
            else:
                out = {"text": f"Mock reply from {offer.model}."}
        elif offer.kind == TaskKind.IMAGE:
            size = payload.get("size", "1792x1024")
            out = {"images": [b64e(procedural.placeholder_image(prompt, size)) for _ in range(int(payload.get("n", 1)))]}
        elif offer.kind == TaskKind.SEGMENT:
            out = procedural.matte_payload(payload["image"])
        elif offer.kind == TaskKind.TTS:
            audio = procedural.babble_speech(payload["text"])
            out = {"audio": b64e(wav_bytes(audio, 16000)), "sample_rate": 16000, "duration_s": len(audio) / 16000}
        elif offer.kind == TaskKind.MUSIC:
            out = procedural.music_payload(prompt, float(payload.get("duration_s", 30)))
        elif offer.kind == TaskKind.DEPTH:
            out = {"depth": procedural.depth_payload(payload["image"]), "model": f"{offer.model} (simulated)"}
        elif offer.kind == TaskKind.WATERMARK:
            out = {"audio": payload["audio"], "detected": 1.0, "bits_match": True, "model": f"{offer.model} (simulated)"}
        elif offer.kind == TaskKind.MODERATE:
            flagged = "unsafe-test" in payload.get("text", "").lower()
            out = {"safe": not flagged, "categories": ["S10"] if flagged else [], "raw": "unsafe\nS10" if flagged else "safe"}
        else:
            out = {"text": "mock transcript"}
        return ExecResult(out, offer.estimate(payload))

    async def aclose(self) -> None:
        await self._template.aclose()


def build_mock_backends(settings: Settings) -> list[Backend]:
    templates: list[Backend] = [
        OllamaBackend("node-a-glimmer", "Node A · muse-glimmer", "lan", "sample node A", "http://mock",
                      "muse-glimmer:30b", 2, True, 60.0),
        OllamaBackend("node-b-llama", "Node B · llama3.1", "lan", "sample node B", "http://mock",
                      "llama3.1:8b", 1, False, 15.0),
        OllamaBackend("node-b-guard", "Node B · llama-guard3", "lan", "sample node B", "http://mock",
                      "llama-guard3:1b", 2, False, 1.0, role="guard"),
        GpuWorkerBackend("node-b-gpu0", "Node B · GPU 0", "sample GPU 0", "http://mock", ["music", "segment", "watermark", "depth"]),
        GpuWorkerBackend("node-b-gpu1", "Node B · GPU 1", "sample GPU 1", "http://mock", ["segment", "tts"]),
        MuseCloudBackend(settings),
    ]
    backends: list[Backend] = [MockBackend(t) for t in templates]
    backends.append(SynthBackend())
    backends[-1].source = backends[-2].source = "builtin"
    return backends
