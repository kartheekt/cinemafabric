"""Local model worker: /tts, /music and /segment on one device."""

import asyncio
import contextlib
import hmac
import io
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager

import httpx
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException
from PIL import Image
from pydantic import BaseModel, Field

from ..profile import catalog
from ..util import b64d, b64e, read_wav, wav_bytes

CAPS = [c.strip() for c in os.environ.get("CF_WORKER_CAPS", "tts,music,segment,watermark,depth").split(",") if c.strip()]
_LOCAL = catalog()["local"]
TTS_MODEL = os.environ.get("CF_TTS_MODEL", _LOCAL["tts"]["models"][0])
MUSIC_MODEL = os.environ.get("CF_MUSIC_MODEL", _LOCAL["music"]["models"][0])
SEGMENT_MODELS = [m for m in os.environ.get("CF_SEGMENT_MODELS", ",".join(_LOCAL["segment"]["models"])).split(",") if m]
SECRET = os.environ.get("CF_WORKER_SECRET") or (secrets.token_urlsafe(32) if os.environ.get("CF_WORKER_JOIN") else "")
WATERMARK_MODEL = os.environ.get("CF_WATERMARK_MODEL", _LOCAL["watermark"]["models"][0])
DEPTH_MODEL = os.environ.get("CF_DEPTH_MODEL", _LOCAL["depth"]["models"][0])
MAX_IMAGE_B64 = 24 * 2**20
MAX_AUDIO_B64 = 48 * 2**20
WATERMARK_SR = 16000
log = logging.getLogger("cinemafabric.worker")


def _device() -> str:
    wanted = os.environ.get("CF_WORKER_DEVICE", "auto")
    torch = _torch()
    if wanted in ("cuda", "auto") and torch.cuda.is_available():
        return "cuda"
    if wanted in ("mps", "auto") and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _device_label(with_usage: bool = False) -> str:
    try:
        torch = _torch()
    except ImportError:
        return "cpu (torch not installed)"
    dev = _device()
    if dev == "cuda":
        name = torch.cuda.get_device_name(0)
        free, total = torch.cuda.mem_get_info()
        usage = f"{(total - free) / 2**30:.1f}/{total / 2**30:.0f} GB" if with_usage else f"{total / 2**30:.0f} GB"
        return f"{name} · {usage}"
    if dev == "mps":
        return "Apple GPU (Metal)"
    return "CPU"


def _memory() -> dict | None:
    try:
        torch = _torch()
        if _device() != "cuda":
            return None
        free, total = torch.cuda.mem_get_info()
    except Exception:
        return None
    return {"used_gb": round((total - free) / 2**30, 3), "total_gb": round(total / 2**30, 3)}


async def _register() -> None:
    fabric, token = os.environ["CF_WORKER_JOIN"].rstrip("/"), os.environ.get("CF_WORKER_TOKEN", "")
    device = await asyncio.to_thread(_device_label)
    spec = {
        "id": os.environ["CF_WORKER_ID"],
        "label": os.environ.get("CF_WORKER_NAME") or f"{device.split(' ·')[0]} · {', '.join(CAPS)}",
        "kind": "gpu-worker",
        "base_url": os.environ["CF_WORKER_ADVERTISE"],
        "caps": CAPS,
        "device": device,
        "secret": SECRET or None,
    }
    delay = 2.0
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            try:
                r = await client.post(f"{fabric}/v1/machines", json=spec, headers={"X-Join-Token": token})
                if r.status_code == 200:
                    log.info("joined fabric %s as %s", fabric, spec["id"])
                    return
                log.error("fabric refused registration: HTTP %s %s", r.status_code, r.text[:200])
                if r.status_code in (401, 422):
                    return
            except httpx.HTTPError as e:
                log.warning("fabric not reachable yet (%s); retrying", e.__class__.__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_register()) if os.environ.get("CF_WORKER_JOIN") else None
    if "segment" in CAPS:
        asyncio.get_running_loop().run_in_executor(None, _warm, "segment")
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="CinemaFabric · worker", lifespan=lifespan)


def _authorised(x_worker_secret: str | None = Header(None)) -> None:
    if SECRET and not hmac.compare_digest(x_worker_secret or "", SECRET):
        raise HTTPException(401, "missing or wrong worker secret")

_models: dict[str, tuple] = {}
_load_errors: dict[str, str] = {}
_load_lock = threading.Lock()
_gpu_lock = asyncio.Lock()


def _torch():
    import torch

    return torch


def _load(name: str):
    with _load_lock:
        if name in _models:
            return _models[name]
        torch = _torch()
        dev = _device()
        if name.startswith("tts"):
            from transformers import AutoTokenizer, VitsModel

            lang = name.partition(":")[2]
            model_name = _LOCAL["tts"]["by_language"].format(lang=lang) if lang and lang != "eng" else TTS_MODEL
            bundle = (VitsModel.from_pretrained(model_name).to(dev).eval(), AutoTokenizer.from_pretrained(model_name))
        elif name == "music":
            from transformers import AutoProcessor, MusicgenForConditionalGeneration

            dtype = torch.float16 if dev == "cuda" else torch.float32
            model = MusicgenForConditionalGeneration.from_pretrained(MUSIC_MODEL, torch_dtype=dtype).to(dev).eval()
            bundle = (model, AutoProcessor.from_pretrained(MUSIC_MODEL))
        elif name == "segment":
            bundle = _load_sam(dev)
        elif name == "depth":
            from transformers import AutoImageProcessor, DPTForDepthEstimation

            bundle = (DPTForDepthEstimation.from_pretrained(DEPTH_MODEL).to(dev).eval(),
                      AutoImageProcessor.from_pretrained(DEPTH_MODEL))
        elif name == "watermark":
            from audioseal import AudioSeal

            detector = WATERMARK_MODEL.replace("_wm_", "_detector_")
            bundle = (AudioSeal.load_generator(WATERMARK_MODEL).to(dev).eval(),
                      AudioSeal.load_detector(detector).to(dev).eval())
        else:
            raise ValueError(name)
        _models[name] = bundle
        return bundle


def _warm(name: str) -> None:
    """Load a model ahead of the first request; a failure is reported in /health so nothing is routed here."""
    try:
        _load(name)
    except Exception as e:
        log.error("%s model failed to load: %s", name, e)
        _load_errors[name] = str(e)[:200]


def _load_sam(dev: str) -> tuple:
    import transformers

    for name in SEGMENT_MODELS:
        try:
            if "sam3" in name:
                model = transformers.Sam3Model.from_pretrained(name).to(dev).eval()
                return ("text", name, model, transformers.Sam3Processor.from_pretrained(name))
            model = transformers.Sam2Model.from_pretrained(name).to(dev).eval()
            return ("point", name, model, transformers.Sam2Processor.from_pretrained(name))
        except Exception as e:
            log.warning("segmenter %s unavailable (%s)", name, e.__class__.__name__)
    raise RuntimeError("no segmentation model could be loaded")


class NoSubject(Exception):
    pass


class TTSRequest(BaseModel):
    text: str = Field(max_length=2000)
    language: str = Field(default="eng", pattern=r"^[a-z]{3}$")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=1.0, ge=0.7, le=1.4)


class MusicRequest(BaseModel):
    prompt: str = Field(max_length=1000)
    duration_s: float = 30.0
    continue_from: str | None = Field(default=None, max_length=4_000_000)


class DepthRequest(BaseModel):
    image: str = Field(max_length=MAX_IMAGE_B64)


class WatermarkRequest(BaseModel):
    audio: str = Field(max_length=MAX_AUDIO_B64)
    bits: list[int] = Field(min_length=16, max_length=16)


class SegmentRequest(BaseModel):
    image: str = Field(max_length=MAX_IMAGE_B64)
    concept: str = Field(default="person", max_length=80)


@app.get("/health")
def health():
    info = {"caps": CAPS, "loaded": sorted(_models), "device": _device_label(with_usage=True), "unavailable": _load_errors,
            "memory": _memory()}
    if "segment" in _models:
        info["segmenter"], info["segmenter_model"] = _models["segment"][0], _models["segment"][1]
    return info


def _require(cap: str) -> None:
    if cap not in CAPS:
        raise HTTPException(404, f"this worker does not serve {cap}")


def _tts(req: TTSRequest) -> dict:
    torch = _torch()
    model, tok = _load("tts" if req.language == "eng" else f"tts:{req.language}")
    model.speaking_rate = req.speed / req.pitch
    inputs = tok(req.text, return_tensors="pt").to(_device())
    with torch.no_grad():
        wave = model(**inputs).waveform[0].float().cpu().numpy()
    sr = model.config.sampling_rate
    if abs(req.pitch - 1.0) > 0.01:
        from scipy.signal import resample_poly

        wave = resample_poly(wave, 100, int(round(100 * req.pitch))).astype(np.float32)
    return {"audio": b64e(wav_bytes(wave / (np.abs(wave).max() or 1) * 0.9, sr)), "sample_rate": sr,
            "duration_s": len(wave) / sr}


def _music(req: MusicRequest) -> dict:
    torch = _torch()
    model, proc = _load("music")
    seconds = float(min(max(req.duration_s, 4.0), 30.0))
    sr = model.config.audio_encoder.sampling_rate
    if req.continue_from:
        tail, rate = read_wav(b64d(req.continue_from))
        if rate != sr:
            raise HTTPException(422, "continue_from must be at the model's sample rate")
        channels = model.config.decoder.audio_channels
        given = np.stack([tail] * channels) if channels > 1 else tail
        inputs = proc(audio=given, sampling_rate=sr, text=[req.prompt], padding=True, return_tensors="pt")
        inputs = {k: v.to(_device(), dtype=model.dtype) if v.is_floating_point() else v.to(_device()) for k, v in inputs.items()}
    else:
        tail = np.zeros(0)
        inputs = proc(text=[req.prompt], padding=True, return_tensors="pt").to(_device())
    with torch.no_grad():
        audio = model.generate(**inputs, max_new_tokens=int(seconds * 50), do_sample=True, guidance_scale=3.0)
    wave = audio[0].float().mean(dim=0).cpu().numpy()[len(tail):]  # the model returns the given audio first
    return {"audio": b64e(wav_bytes(wave / (np.abs(wave).max() or 1) * 0.9, sr)), "sample_rate": sr,
            "duration_s": len(wave) / sr}


def _segment(req: SegmentRequest) -> dict:
    torch = _torch()
    kind, name, model, proc = _load("segment")
    img = Image.open(io.BytesIO(b64d(req.image))).convert("RGB")
    w, h = img.size
    if kind == "text":
        inputs = proc(images=img, text=req.concept, return_tensors="pt").to(_device())
        with torch.no_grad():
            out = model(**inputs)
        found = proc.post_process_instance_segmentation(
            out, threshold=0.4, mask_threshold=0.5, target_sizes=inputs.get("original_sizes").tolist()
        )[0]
        if len(found["masks"]) == 0:
            raise NoSubject(f"no '{req.concept}' found")
        best = found["masks"].any(dim=0).cpu().numpy().astype(np.uint8)
        method = f"{name} text prompt '{req.concept}'"
    else:
        points = [[[[w * 0.5, h * 0.55], [w * 0.5, h * 0.4]]]]
        inputs = proc(images=img, input_points=points, input_labels=[[[1, 1]]], return_tensors="pt").to(_device())
        with torch.no_grad():
            out = model(**inputs, multimask_output=True)
        masks = proc.post_process_masks(out.pred_masks.cpu(), inputs["original_sizes"])[0][0]
        scores = out.iou_scores[0, 0].float().cpu().numpy()
        best, best_score = None, -1.0
        for i in range(masks.shape[0]):
            m = masks[i].numpy().astype(np.uint8)
            area = m.mean()
            score = float(scores[i]) - (0.5 if area < 0.04 or area > 0.7 else 0.0)
            if score > best_score:
                best, best_score = m, score
        if best is None or best.mean() < 0.01:
            raise NoSubject("no clear subject at the centre of the frame")
        method = f"{name} point prompt"
    ys, xs = np.nonzero(best)
    box = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
    buf = io.BytesIO()
    Image.fromarray(best * 255).save(buf, "PNG")
    return {"mask": b64e(buf.getvalue()), "box": box, "method": method}


async def _run(cap: str, fn, req):
    _require(cap)
    async with _gpu_lock:
        try:
            return await asyncio.to_thread(fn, req)
        except NoSubject as e:
            raise HTTPException(422, str(e)) from e
        except Exception as e:
            raise HTTPException(500, f"{cap} failed: {e}") from e


@app.post("/tts", dependencies=[Depends(_authorised)])
async def tts(req: TTSRequest):
    return await _run("tts", _tts, req)


@app.post("/music", dependencies=[Depends(_authorised)])
async def music(req: MusicRequest):
    return await _run("music", _music, req)


@app.post("/segment", dependencies=[Depends(_authorised)])
async def segment(req: SegmentRequest):
    return await _run("segment", _segment, req)


def depth_input(img: Image.Image, long_side: int = 1344) -> Image.Image:
    """The frame at a size the model handles well, in multiples of its 14-pixel patches. The model doesn't resize for
    itself, and a full 2016-pixel frame needs 5.8 GB where this needs 2.7 GB and matches it closely."""
    k = long_side / max(img.size)
    if k >= 1:
        return img
    return img.resize((max(14, round(img.width * k / 14) * 14), max(14, round(img.height * k / 14) * 14)), Image.LANCZOS)


def _depth(req: DepthRequest) -> dict:
    """A depth map as an 8-bit PNG at the image's size: white is near, black is far."""
    torch = _torch()
    model, proc = _load("depth")
    img = Image.open(io.BytesIO(b64d(req.image))).convert("RGB")
    with torch.no_grad():
        out = model(**proc(images=depth_input(img), return_tensors="pt").to(_device())).predicted_depth
    depth = torch.nn.functional.interpolate(out.unsqueeze(1), size=img.size[::-1], mode="bicubic").squeeze().float().cpu().numpy()
    near = 1 - (depth - depth.min()) / (depth.max() - depth.min() + 1e-6)
    buf = io.BytesIO()
    Image.fromarray((near * 255).astype(np.uint8)).save(buf, "PNG")
    return {"depth": b64e(buf.getvalue()), "model": DEPTH_MODEL}


@app.post("/depth", dependencies=[Depends(_authorised)])
async def depth(req: DepthRequest):
    return await _run("depth", _depth, req)


def read_mark(detect, audio: np.ndarray, rate: int, window_s: float = 10.0) -> tuple[float, list[int]]:
    """AudioSeal's detection and message, read in windows. Each window gives a probability per bit, and these are
    averaged, weighted by how sure the window is that a mark is there: one read over a long track loses the message,
    and a plain majority of per-window bits is often wrong even when the average is right."""
    n = int(window_s * rate)
    probs, soft = [], []
    for start in range(0, max(1, len(audio) - n // 2), n):
        chunk = audio[start:start + n]
        if len(chunk) < rate and probs:
            break
        prob, bits = detect(chunk)
        probs.append(prob)
        soft.append(bits)
    weights = np.array(probs) if sum(probs) > 0 else None
    message = (np.average(np.array(soft, dtype=np.float32), axis=0, weights=weights) > 0.5).astype(int).tolist()
    return float(np.median(probs)), message


def _watermark(req: WatermarkRequest) -> dict:
    """Embed an AudioSeal mark at 16 kHz, lift it back to the track's rate, then check it can be read back."""
    from audioseal.libs.moshi.utils.compile import no_compile
    from scipy.signal import resample_poly

    torch = _torch()
    gen, det = _load("watermark")
    samples, sr = read_wav(b64d(req.audio))
    low = resample_poly(samples, WATERMARK_SR, sr) if sr != WATERMARK_SR else samples
    bits = torch.tensor([[1 if b else 0 for b in req.bits]], device=_device())
    with torch.no_grad(), no_compile():
        mark = gen.get_watermark(torch.from_numpy(low.astype(np.float32))[None, None].to(_device()), WATERMARK_SR,
                                 message=bits)[0, 0].float().cpu().numpy()
        full = resample_poly(mark, sr, WATERMARK_SR)[: len(samples)] if sr != WATERMARK_SR else mark
        marked = np.clip(samples + np.pad(full, (0, len(samples) - len(full))), -1.0, 1.0).astype(np.float32)
        check = resample_poly(marked, WATERMARK_SR, sr) if sr != WATERMARK_SR else marked

        def detect(chunk: np.ndarray) -> tuple[float, list[float]]:
            frames, message = det(torch.from_numpy(chunk.astype(np.float32))[None, None].to(_device()), WATERMARK_SR)
            return float((frames[:, 1, :] > 0.5).float().mean()), message[0].float().cpu().tolist()

        prob, found = read_mark(detect, check, WATERMARK_SR)
    return {"audio": b64e(wav_bytes(marked, sr)), "sample_rate": sr, "detected": prob,
            "bits_match": found == [1 if b else 0 for b in req.bits], "model": WATERMARK_MODEL}


@app.post("/watermark", dependencies=[Depends(_authorised)])
async def watermark(req: WatermarkRequest):
    return await _run("watermark", _watermark, req)


@app.post("/warmup", dependencies=[Depends(_authorised)])
async def warmup():
    for cap in CAPS:
        await asyncio.to_thread(_load, cap)
    return {"loaded": sorted(_models)}
