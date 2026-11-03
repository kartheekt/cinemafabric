"""Procedural media: a synth score, a heuristic matte, placeholder frames and speech."""

import hashlib
import io
import textwrap

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .util import b64d, b64e, wav_bytes

MUSIC_SR = 32000
NOTE = {"A": 220.0, "B": 246.94, "C": 261.63, "D": 293.66, "E": 329.63, "F": 349.23, "G": 392.0}
MINOR_PROGRESSION = [("A", [0, 3, 7]), ("F", [0, 4, 7]), ("C", [0, 4, 7]), ("G", [0, 4, 7])]
MAJOR_PROGRESSION = [("C", [0, 4, 7]), ("G", [0, 4, 7]), ("A", [0, 3, 7]), ("F", [0, 4, 7])]
BRIGHT_WORDS = ("hope", "joy", "whims", "light", "uplift", "comedy", "romance", "warm", "playful", "bright")


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def synth_score(prompt: str, duration_s: float, sr: int = MUSIC_SR) -> np.ndarray:
    """A slow chord pad, sub bass and a building heartbeat pulse."""
    bright = any(w in prompt.lower() for w in BRIGHT_WORDS)
    progression = MAJOR_PROGRESSION if bright else MINOR_PROGRESSION
    n = int(duration_s * sr)
    t = np.arange(n) / sr
    out = np.zeros(n, dtype=np.float32)
    bar = 4.0
    rng = np.random.default_rng(_seed(prompt))
    for i, start in enumerate(np.arange(0, duration_s, bar)):
        root, intervals = progression[i % len(progression)]
        a, b = int(start * sr), min(n, int((start + bar + 0.5) * sr))
        seg_t = t[a:b] - start
        env = np.minimum(1.0, seg_t / 1.2) * np.clip((bar + 0.5 - seg_t) / 1.2, 0, 1)
        f0 = NOTE[root] / 2
        chord = np.zeros(b - a, dtype=np.float32)
        for semis in intervals:
            f = f0 * 2 ** (semis / 12)
            for detune in (-0.6, 0.0, 0.6):
                chord += np.sin(2 * np.pi * (f + detune) * seg_t + rng.uniform(0, 6.28)).astype(np.float32)
        chord += 0.8 * np.sin(2 * np.pi * (f0 / 2) * seg_t).astype(np.float32)
        out[a:b] += 0.06 * chord * env
    beat = 60 / 72
    for k, start in enumerate(np.arange(0.0, duration_s, beat)):
        intensity = 0.15 + 0.85 * (start / max(duration_s, 1)) ** 1.5
        a = int(start * sr)
        length = min(int(0.45 * sr), n - a)
        if length <= 0:
            break
        bt = np.arange(length) / sr
        thump = np.sin(2 * np.pi * (55 - 25 * bt) * bt) * np.exp(-bt * 9)
        out[a : a + length] += (0.35 * intensity * thump * (1.0 if k % 2 == 0 else 0.6)).astype(np.float32)
    fade = min(int(1.5 * sr), n // 4)
    out[:fade] *= np.linspace(0, 1, fade)
    out[-fade:] *= np.linspace(1, 0, fade)
    peak = np.abs(out).max() or 1.0
    return (out / peak * 0.8).astype(np.float32)


def depth_payload(image_b64: str) -> str:
    """A plausible stand-in depth map: the lower middle of the frame is nearest."""
    from PIL import Image

    w, h = Image.open(io.BytesIO(b64d(image_b64))).size
    yy, xx = np.mgrid[0:h, 0:w]
    near = 0.6 * (yy / h) + 0.4 * np.exp(-(((xx - w / 2) / (w / 4)) ** 2 + ((yy - h * 0.6) / (h / 3)) ** 2))
    buf = io.BytesIO()
    Image.fromarray((np.clip(near, 0, 1) * 255).astype(np.uint8)).save(buf, "PNG")
    return b64e(buf.getvalue())


def music_payload(prompt: str, duration_s: float) -> dict:
    audio = synth_score(prompt, duration_s)
    return {"audio": b64e(wav_bytes(audio, MUSIC_SR)), "sample_rate": MUSIC_SR, "duration_s": duration_s}


def matte_payload(image_b64: str) -> dict:
    img = Image.open(io.BytesIO(b64d(image_b64)))
    w, h = img.size
    box = [int(w * 0.28), int(h * 0.18), int(w * 0.72), int(h * 0.98)]
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(w, h) / 40))
    buf = io.BytesIO()
    mask.save(buf, "PNG")
    return {"mask": b64e(buf.getvalue()), "box": box, "method": "center-weighted heuristic"}


def placeholder_image(prompt: str, size: str = "1792x1024") -> bytes:
    """A deterministic, clearly-fake frame for mock mode."""
    w, h = (int(v) for v in size.split("x"))
    rng = np.random.default_rng(_seed(prompt))
    c1, c2 = rng.integers(10, 120, 3), rng.integers(80, 230, 3)
    grad = np.linspace(0, 1, h)[:, None, None]
    arr = (c1 * (1 - grad) + c2 * grad).astype(np.uint8)
    img = Image.fromarray(np.broadcast_to(arr, (h, w, 3)).copy())
    draw = ImageDraw.Draw(img)
    for _ in range(6):
        x, y = rng.integers(0, w), rng.integers(0, h)
        r = int(rng.integers(h // 12, h // 3))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=tuple(int(v) for v in rng.integers(30, 255, 3)))
    img = img.filter(ImageFilter.GaussianBlur(h / 30))
    draw = ImageDraw.Draw(img)
    draw.ellipse([w * 0.4, h * 0.3, w * 0.6, h * 0.95], fill=(15, 15, 20))
    lines = textwrap.wrap(prompt, 60)[:4]
    draw.rectangle([0, h - 30 - 22 * len(lines), w, h], fill=(0, 0, 0))
    for i, line in enumerate(lines):
        draw.text((20, h - 20 - 22 * (len(lines) - i)), line, fill=(230, 230, 230))
    draw.text((20, 16), "MOCK FRAME", fill=(255, 220, 120))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def babble_speech(text: str, sr: int = 16000) -> np.ndarray:
    """Speech-shaped placeholder audio (syllable bursts) for mock mode."""
    syllables = max(2, int(len(text.split()) * 1.4))
    duration = syllables * 0.21 + 0.3
    t = np.arange(int(duration * sr)) / sr
    rng = np.random.default_rng(_seed(text))
    pitch = 110 + 15 * np.sin(2 * np.pi * 0.7 * t)
    voice = sum(np.sin(2 * np.pi * k * pitch * t) / k for k in range(1, 6))
    gate = np.zeros_like(t)
    for s in range(syllables):
        centre = 0.15 + s * 0.21 + rng.uniform(-0.03, 0.03)
        gate += np.exp(-((t - centre) ** 2) / (2 * 0.05**2))
    audio = voice * np.clip(gate, 0, 1)
    return (audio / (np.abs(audio).max() or 1) * 0.6).astype(np.float32)
