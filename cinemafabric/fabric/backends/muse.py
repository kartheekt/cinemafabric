"""Meta Model API backend."""

import asyncio
import io
import json

import httpx
import meta_sam_parser
import numpy as np
from PIL import Image

from ...config import Settings
from ...profile import catalog, cloud_model
from ...util import b64d, b64e, data_url, extract_json, read_wav
from .. import pricing
from ..types import TaskKind
from .base import Backend, BackendError, ExecResult, Offer


def sam_text_to_mask(text: str, size: tuple[int, int], min_confidence: float = 0.4):
    """Merge box-local SAM masks into one full-frame mask: (mask, union box, count)."""
    parser = meta_sam_parser.image_segmentation_format().create_parser()
    parser.push(text)
    result = parser.finish(meta_sam_parser.CompletedOutcome()).result
    full = np.zeros((size[1], size[0]), dtype=np.uint8)
    count, boxes = 0, []
    for rec in getattr(result, "records", ()):
        if not isinstance(rec, meta_sam_parser.SegmentationMaskRecord):
            continue
        if rec.confidence is not None and rec.confidence < min_confidence:
            continue
        raster = np.frombuffer(meta_sam_parser.decode_mask_to_raster(rec.mask), dtype=np.uint8)
        raster = raster.reshape(rec.mask.height, rec.mask.width)
        b = rec.bounds
        left, top = max(0, int(round(b.left))), max(0, int(round(b.top)))
        right, bottom = min(size[0], int(round(b.right))), min(size[1], int(round(b.bottom)))
        if right <= left or bottom <= top:
            continue
        patch = Image.fromarray(raster * 255).resize((right - left, bottom - top), Image.Resampling.NEAREST)
        full[top:bottom, left:right] = np.maximum(full[top:bottom, left:right], np.asarray(patch))
        boxes.append((left, top, right, bottom))
        count += 1
    if not count:
        return None, None, 0
    box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    return Image.fromarray(full), box, count


class MuseCloudBackend(Backend):
    location = "cloud"
    device = "Meta Model API"
    is_cloud = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.id = "muse-cloud"
        self.label = "Meta Model API · Muse"
        self.max_concurrency = settings.cloud_concurrency
        self.api_key = settings.meta_api_key
        self.key_source = "env" if self.api_key else None
        self._client = httpx.AsyncClient(
            base_url=settings.meta_api_base.rstrip("/"),
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(300.0, connect=15.0),
        )

        licence = catalog()["cloud"]["licence"]

        def text_offer(model: str, quality: int, typical_s: float) -> Offer:
            return Offer(TaskKind.TEXT, model, quality, vision=True,
                         estimate=lambda p, m=model: pricing.estimate_text_cost(m, p), typical_s=typical_s, licence=licence)

        self.offers = [
            text_offer(cloud_model("text"), 2, 20.0),
            text_offer(cloud_model("text_standard"), 2, 20.0),
            text_offer(cloud_model("text_premium"), 3, 40.0),
            Offer(TaskKind.IMAGE, cloud_model("image"), 2, estimate=lambda p: pricing.IMAGE_PRICE * int(p.get("n", 1)),
                  typical_s=25.0, licence=licence),
            Offer(TaskKind.SEGMENT, cloud_model("segment"), 2, estimate=lambda p: pricing.SAM_IMAGE_PRICE,
                  typical_s=6.0, licence=licence),
            Offer(TaskKind.TRANSCRIBE, cloud_model("transcribe"), 2,
                  estimate=lambda p: pricing.TRANSCRIBE_PER_HOUR * pricing.wav_seconds(p.get("audio", "")) / 3600,
                  typical_s=8.0, licence=licence),
        ]

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_key(self, key: str | None, source: str = "ui") -> None:
        self.api_key = key or self.settings.meta_api_key
        self.key_source = (source if key else "env") if self.api_key else None
        self._client.headers["Authorization"] = f"Bearer {self.api_key}"

    async def check_key(self, key: str) -> tuple[bool, str]:
        try:
            r = await self._client.get("/models", headers={"Authorization": f"Bearer {key}"}, timeout=10.0)
        except httpx.HTTPError as e:
            return False, f"couldn't reach the Meta Model API ({e.__class__.__name__})"
        if r.status_code in (401, 403):
            return False, "the Meta Model API rejected this key"
        return r.status_code == 200, f"HTTP {r.status_code}"

    async def probe(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no API key yet"
        try:
            r = await self._client.get("/models", timeout=10.0)
        except httpx.HTTPError as e:
            return False, f"unreachable: {e.__class__.__name__}"
        if r.status_code == 200:
            return True, "API reachable"
        return False, f"HTTP {r.status_code}"

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        delay = 2.0
        for attempt in range(3):
            try:
                r = await self._client.request(method, path, **kwargs)
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise BackendError(f"Meta API transport error: {e.__class__.__name__}") from e
            else:
                if r.status_code < 400:
                    return r
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt == 2:
                        raise BackendError(f"Meta API HTTP {r.status_code}: {r.text[:200]}")
                else:
                    raise BackendError(f"Meta API HTTP {r.status_code}: {r.text[:300]}", retryable=False)
            await asyncio.sleep(delay)
            delay *= 2
        raise BackendError("Meta API retries exhausted")

    async def execute(self, offer: Offer, payload: dict) -> ExecResult:
        if offer.kind == TaskKind.TEXT:
            return await self._text(offer.model, payload)
        if offer.kind == TaskKind.IMAGE:
            return await self._image(offer.model, payload)
        if offer.kind == TaskKind.SEGMENT:
            return await self._segment(offer.model, payload)
        if offer.kind == TaskKind.TRANSCRIBE:
            return await self._transcribe(offer.model, payload)
        raise BackendError(f"unsupported task {offer.kind}", retryable=False)

    async def _text(self, model: str, payload: dict) -> ExecResult:
        messages = []
        if payload.get("system"):
            messages.append({"role": "system", "content": payload["system"]})
        images = payload.get("images") or []
        if images:
            content = [{"type": "text", "text": payload["prompt"]}] + [
                {"type": "image_url", "image_url": {"url": data_url(img)}} for img in images
            ]
        else:
            content = payload["prompt"]
        messages.append({"role": "user", "content": content})
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": pricing.output_cap(payload),
            "temperature": float(payload.get("temperature", 0.8)),
            "reasoning_effort": payload.get("reasoning_effort", "low"),
        }
        if payload.get("json_schema"):
            body["response_format"] = {"type": "json_schema", "json_schema": payload["json_schema"]}
        r = await self._request("POST", "/chat/completions", json=body)
        data = r.json()
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        cost = pricing.text_cost(model, in_tok, out_tok, cached)
        if not text and choice.get("finish_reason") == "length":
            raise BackendError(f"{model} used its whole token budget reasoning and returned no answer", retryable=False,
                               cost_usd=cost)
        output = {"text": text}
        if payload.get("json_schema"):
            try:
                output["json"] = extract_json(text, expect_object=payload["json_schema"]["schema"].get("type") == "object")
            except ValueError as e:
                raise BackendError(str(e), retryable=False, cost_usd=cost) from e
        return ExecResult(output, cost, {"input_tokens": in_tok, "output_tokens": out_tok})

    async def _image(self, model: str, payload: dict) -> ExecResult:
        n = int(payload.get("n", 1))
        body = {"model": model, "prompt": payload["prompt"], "n": n, "output_format": "png"}
        if payload.get("size"):
            body["size"] = payload["size"]
        refs = payload.get("references") or []
        if refs:
            body["images"] = [{"image_url": data_url(ref)} for ref in refs[:10]]
            r = await self._request("POST", "/images/edits", json=body)
        else:
            r = await self._request("POST", "/images/generations", json=body)
        items = r.json().get("data") or []
        images = [item["b64_json"] for item in items if item.get("b64_json")]
        if not images:
            raise BackendError("Muse Image returned no images")
        return ExecResult({"images": images}, pricing.IMAGE_PRICE * len(images), {"images": len(images)})

    async def _segment(self, model: str, payload: dict) -> ExecResult:
        """SAM 3.1 concept segmentation: every instance of the concept, merged into one pixel mask."""
        img = Image.open(io.BytesIO(b64d(payload["image"]))).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        body = {
            "model": model,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": payload.get("concept", "person")},
                        {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64e(buf.getvalue())},
                    ],
                }
            ],
            "metadata": {"include_confidence": "true", "mask_encoding": "one_bit"},
        }
        r = await self._request("POST", "/responses", json=body)
        text = "".join(
            part.get("text", "")
            for item in r.json().get("output", [])
            for part in (item.get("content") or [])
            if part.get("type") == "output_text"
        )
        mask, box, count = sam_text_to_mask(text, img.size, min_confidence=float(payload.get("min_confidence", 0.4)))
        if mask is None:
            raise BackendError(f"SAM 3.1 found no '{payload.get('concept', 'person')}' in the frame", retryable=False)
        out = io.BytesIO()
        mask.save(out, "PNG")
        return ExecResult({"mask": b64e(out.getvalue()), "box": box, "objects": count, "method": "sam-3.1 pixel mask"},
                          pricing.SAM_IMAGE_PRICE)

    async def _transcribe(self, model: str, payload: dict) -> ExecResult:
        audio = b64d(payload["audio"])
        samples, sr = read_wav(audio)
        duration = len(samples) / sr
        files = {
            "request": (None, json.dumps({"model": model, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK",
                                          **({"languageBias": [payload["language"], "English"]} if payload.get("language")
                                             else {})}), "application/json"),
            "audio": ("audio.wav", audio, "audio/wav"),
        }
        r = await self._request("POST", "/asr/transcribe", files=files, headers={"Accept": "application/json"})
        texts: list[str] = []
        level = [r.json()]
        while level and not texts:
            nested = []
            for node in level:
                items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
                for key, value in items:
                    if key in ("transcript", "text") and isinstance(value, str):
                        texts.append(value)
                    elif isinstance(value, dict | list):
                        nested.append(value)
            level = nested
        cost = pricing.TRANSCRIBE_PER_HOUR * duration / 3600
        return ExecResult({"text": " ".join(t.strip() for t in texts if t.strip())}, cost, {"audio_s": duration})
