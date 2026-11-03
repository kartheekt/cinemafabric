"""Soundtrack mixing: narration over a ducked, looped score, plus trailer impact hits."""

import numpy as np
from scipy.signal import resample_poly

OUT_SR = 48000


def to_rate(samples: np.ndarray, sr: int, target: int = OUT_SR) -> np.ndarray:
    if sr == target:
        return samples.astype(np.float32)
    g = np.gcd(sr, target)
    return resample_poly(samples, target // g, sr // g).astype(np.float32)


def loop_to(samples: np.ndarray, n: int, sr: int, xfade_s: float = 1.5) -> np.ndarray:
    """Extend a clip to n samples by looping it with a crossfade."""
    if len(samples) >= n:
        return samples[:n]
    xf = min(int(xfade_s * sr), len(samples) // 3)
    out = samples.copy()
    ramp = np.linspace(0, 1, xf, dtype=np.float32)
    while len(out) < n:
        head = samples[:xf] * ramp
        out[-xf:] = out[-xf:] * (1 - ramp) + head
        out = np.concatenate([out, samples[xf:]])
    return out[:n]


def impact(sr: int = OUT_SR) -> np.ndarray:
    t = np.arange(int(1.6 * sr)) / sr
    boom = np.sin(2 * np.pi * (48 - 20 * t) * t) * np.exp(-t * 2.2)
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(len(t)) * np.exp(-t * 14) * 0.35
    return ((boom + noise) * 0.9).astype(np.float32)


def envelope(mask: np.ndarray, sr: int, attack_s: float = 0.15, release_s: float = 0.45) -> np.ndarray:
    """Smooth a 0/1 activity mask into a ducking envelope."""
    out = np.empty_like(mask, dtype=np.float32)
    a, r = 1 / max(1, int(attack_s * sr)), 1 / max(1, int(release_s * sr))
    level = 0.0
    step = 64
    for i in range(0, len(mask), step):
        target = mask[i : i + step].max()
        rate = a if target > level else r
        level += np.clip(target - level, -rate * step, rate * step)
        out[i : i + step] = level
    return out


def over(base: np.ndarray, sr: int, lines: list[tuple[float, np.ndarray, int]], under: float = 0.3) -> np.ndarray:
    """Lay a second voice over a finished mix, dipping the mix to `under` while it speaks."""
    voice = np.zeros(len(base), dtype=np.float32)
    active = np.zeros(len(base), dtype=np.float32)
    for start, samples, rate in lines:
        clip = to_rate(samples, rate, sr)
        clip = clip / (np.abs(clip).max() or 1) * 0.9
        a = int(start * sr)
        b = min(len(base), a + len(clip))
        if b > a:
            voice[a:b] = clip[: b - a]
            active[a:b] = 1.0
    out = base.astype(np.float32) * (1.0 - (1.0 - under) * envelope(active, sr)) + voice
    return (out / (np.abs(out).max() or 1.0) * 0.95).astype(np.float32)


def join_score(sections: list[np.ndarray], fade_s: float = 0.25, sr: int = 32000) -> np.ndarray:
    """Sections of one score as a single piece: matched in loudness, with a short crossfade at each join."""
    levels = [float(np.sqrt(np.mean(s**2))) for s in sections if len(s)]
    target = float(np.median(levels)) if levels else 0.0
    n = int(fade_s * sr)
    out = np.zeros(0, dtype=np.float32)
    for s in sections:
        level = float(np.sqrt(np.mean(s**2))) if len(s) else 0.0
        part = (s * min(target / level, 4.0) if level > 0 else s).astype(np.float32)
        if len(out) >= n and len(part) >= n:
            fade = np.linspace(0, 1, n, dtype=np.float32)
            out[-n:] = out[-n:] * (1 - fade) + part[:n] * fade
            part = part[n:]
        out = np.concatenate([out, part])
    return out


def is_silent(samples: np.ndarray, floor: float = 0.005) -> bool:
    return not len(samples) or float(np.sqrt(np.mean(samples**2))) < floor


def mix(
    duration_s: float,
    narration: list[tuple[float, np.ndarray, int]],
    score: tuple[np.ndarray, int] | None,
    impacts: list[float],
) -> np.ndarray:
    """Return a mono 48 kHz mix. narration is a list of (start_s, samples, sample_rate)."""
    n = int(duration_s * OUT_SR)
    voice = np.zeros(n, dtype=np.float32)
    active = np.zeros(n, dtype=np.float32)
    for start, samples, sr in narration:
        clip = to_rate(samples, sr)
        clip = clip / (np.abs(clip).max() or 1) * 0.92
        a = int(start * OUT_SR)
        b = min(n, a + len(clip))
        if b > a:
            voice[a:b] += clip[: b - a]
            active[a:b] = 1.0
    out = voice
    if score is not None:
        music = to_rate(*score)
        music = loop_to(music, n, OUT_SR)
        music = music / (np.abs(music).max() or 1) * 0.55
        duck = 1.0 - 0.6 * envelope(active, OUT_SR)
        fade = min(int(2.0 * OUT_SR), n // 4)
        music[:fade] *= np.linspace(0, 1, fade)
        music[-fade:] *= np.linspace(1, 0, fade)
        out = out + music * duck
    hit = impact()
    for t in impacts:
        a = int(t * OUT_SR)
        b = min(n, a + len(hit))
        if b > a:
            out[a:b] += hit[: b - a] * 0.7
    peak = np.abs(out).max() or 1.0
    return (out / peak * 0.95).astype(np.float32)
