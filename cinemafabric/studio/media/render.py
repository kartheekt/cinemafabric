"""Turns still storyboard frames into moving shots and encodes the film with ffmpeg."""

import contextlib
import math
import os
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import typography

PARALLAX_MOVES = {"push_in", "pull_out", "static", "parallax"}
OVERSAMPLE = 1.15


@dataclass
class Clip:
    duration: float
    camera: str = "static"
    image: Path | None = None
    mask: Path | None = None
    card_text: str = ""
    card_sub: str = ""
    card_note: str = ""
    depth: Path | None = None


def ffmpeg_bin() -> str:
    path = os.environ.get("CF_FFMPEG") or shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not found on PATH (install it or set CF_FFMPEG)")
    return path


def cover(img: Image.Image, size: tuple[int, int], oversample: float = 1.0) -> Image.Image:
    """Centre-crop to the target aspect and scale so the result covers size * oversample."""
    tw, th = size
    target = tw / th
    w, h = img.size
    if w / h > target:
        nw = int(h * target)
        img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / target)
        img = img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    min_w = int(tw * oversample)
    if img.width < min_w:
        img = img.resize((min_w, int(min_w / target)), Image.Resampling.LANCZOS)
    return img


def ease(p: float) -> float:
    smooth = 0.5 - 0.5 * math.cos(math.pi * p)
    return 0.6 * p + 0.4 * smooth


def camera_path(camera: str, p: float) -> tuple[float, float, float]:
    """(zoom, centre_x, centre_y) for normalised progress p."""
    e = ease(p)
    if camera == "push_in":
        return 1.0 + 0.14 * e, 0.5, 0.5
    if camera == "pull_out":
        return 1.14 - 0.14 * e, 0.5, 0.5
    if camera == "pan_left":
        return 1.14, 0.56 - 0.12 * e, 0.5
    if camera == "pan_right":
        return 1.14, 0.44 + 0.12 * e, 0.5
    if camera == "tilt_up":
        return 1.14, 0.5, 0.56 - 0.12 * e
    if camera == "tilt_down":
        return 1.14, 0.5, 0.44 + 0.12 * e
    if camera == "parallax":
        return 1.04 + 0.06 * e, 0.5, 0.5
    return 1.02 + 0.04 * e, 0.5, 0.5


def view(img: Image.Image, size: tuple[int, int], zoom: float, cx: float, cy: float) -> Image.Image:
    w, h = img.size
    vw, vh = w / zoom, h / zoom
    cx = min(max(cx * w, vw / 2), w - vw / 2)
    cy = min(max(cy * h, vh / 2), h - vh / 2)
    box = (cx - vw / 2, cy - vh / 2, cx + vw / 2, cy + vh / 2)
    return img.transform(size, Image.Transform.EXTENT, box, Image.Resampling.BILINEAR)


# How far depth bends each camera move: (extra zoom for near things, sideways, up-down), as a share of the frame.
DEPTH_MOTION = {
    "push_in": (0.10, 0.0, 0.0),
    "pull_out": (-0.08, 0.0, 0.0),
    "pan_left": (0.0, -0.035, 0.0),
    "pan_right": (0.0, 0.035, 0.0),
    "tilt_up": (0.0, 0.0, -0.03),
    "tilt_down": (0.0, 0.0, 0.03),
    "parallax": (0.04, 0.035, -0.01),
    "static": (0.03, 0.012, 0.0),
}


def _depth_field(path: Path | None, mask: Path | None, src_size: tuple[int, int], size: tuple[int, int],
                 base_size: tuple[int, int]) -> np.ndarray | None:
    """Nearness from 0 (far) to 1 (near) over the base image, smoothed so the warp bends rather than tears. Near
    regions are grown a little so their outlines stay rigid and the background stretches instead."""
    def load(p: Path) -> np.ndarray:
        img = Image.open(p).convert("L").resize(src_size, Image.Resampling.BILINEAR)
        img = cover(img, size, oversample=OVERSAMPLE).resize(base_size, Image.Resampling.BILINEAR)
        return np.asarray(img, dtype=np.float32) / 255

    if path is not None and path.exists():
        d = load(path)
        if mask is not None and mask.exists():
            d = np.maximum(d, load(mask) * 0.9)
    elif mask is not None and mask.exists():
        d = 0.35 + 0.55 * load(mask)
    else:
        return None
    k = max(3, int(base_size[0] * 0.012) | 1)
    d = cv2.dilate(d, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    return cv2.GaussianBlur(d, (0, 0), base_size[0] * 0.008)


class ShotRenderer:
    """One shot: a camera move over the picture, with depth bending the move so near things travel further."""

    def __init__(self, clip: Clip, size: tuple[int, int]):
        self.clip = clip
        self.size = size
        self.card: Image.Image | None = None
        if clip.image is None:
            self.card = typography.title_card(size, clip.card_text, clip.card_sub, clip.card_note)
            return
        src = Image.open(clip.image).convert("RGB")
        base = cover(src, size, oversample=OVERSAMPLE)
        self.base = np.asarray(base, dtype=np.uint8)
        h, w = self.base.shape[:2]
        if w / size[0] > 1.05:
            self.base = cv2.GaussianBlur(self.base, (0, 0), 0.4)
        self.depth = _depth_field(clip.depth, clip.mask, src.size, size, (w, h))
        ow, oh = size
        self.ox = ((np.arange(ow, dtype=np.float32) + 0.5) / ow - 0.5)[None, :].repeat(oh, 0)
        self.oy = ((np.arange(oh, dtype=np.float32) + 0.5) / oh - 0.5)[:, None].repeat(ow, 1)

    def frame(self, p: float) -> Image.Image:
        if self.card is not None:
            fade_in = min(1.0, p * self.clip.duration / 0.35)
            fade_out = min(1.0, (1 - p) * self.clip.duration / 0.35)
            alpha = max(0.0, min(fade_in, fade_out))
            frame = view(self.card, self.size, 1.0 + 0.04 * p, 0.5, 0.5)
            return Image.blend(Image.new("RGB", self.size), frame, alpha)
        h, w = self.base.shape[:2]
        zoom, cx, cy = camera_path(self.clip.camera, float(p))
        vw, vh = w / zoom, h / zoom
        cx = float(min(max(cx * w, vw / 2), w - vw / 2))
        cy = float(min(max(cy * h, vh / 2), h - vh / 2))
        mx = cv2.addWeighted(self.ox, vw, self.ox, 0.0, cx)
        my = cv2.addWeighted(self.oy, vh, self.oy, 0.0, cy)
        if self.depth is not None:
            kz, kx, ky = DEPTH_MOTION.get(self.clip.camera, DEPTH_MOTION["static"])
            e = float(ease(float(p)))
            near = cv2.remap(self.depth, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            near -= 0.5
            grow = cv2.addWeighted(near, kz * e, near, 0.0, 1.0)
            cv2.divide(1.0, grow, dst=grow)
            mx = cv2.addWeighted(cv2.multiply(self.ox, grow, scale=vw), 1.0, near, kx * e * w, cx)
            my = cv2.addWeighted(cv2.multiply(self.oy, grow, scale=vh), 1.0, near, ky * e * h, cy)
        out = cv2.remap(self.base, mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        return Image.fromarray(out)


def visibility(clips: list[Clip], i: int, local: float, fade_s: float) -> float:
    """How much of a picture shows: pictures cut straight to each other (blending two different frames ghosts the
    characters), and fade through black next to a title card."""
    clip = clips[i]
    if clip.image is None or fade_s <= 0:
        return 1.0
    level = 1.0
    if i + 1 < len(clips) and clips[i + 1].image is None:
        level = min(level, (clip.duration - local) / fade_s)
    if i > 0 and clips[i - 1].image is None:
        level = min(level, local / fade_s)
    return min(1.0, level)


def render_film(
    clips: list[Clip],
    size: tuple[int, int],
    audio_wav: Path,
    out_path: Path,
    subtitles: list[tuple[float, float, str]] | None = None,
    fps: int = 24,
    fade_s: float = 0.3,
    progress: Callable[[float], None] | None = None,
    metadata: list[str] | None = None,
    crf: int = 17,
    tune: str = "film",
) -> Path:
    w, h = size
    starts, t = [], 0.0
    for c in clips:
        starts.append(t)
        t += c.duration
    total = t
    nframes = int(round(total * fps))
    log_path = out_path.with_suffix(".ffmpeg.log")
    cmd = [
        ffmpeg_bin(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
        "-i", str(audio_wav),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-tune", tune, "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart+use_metadata_tags",
        *(metadata or []),
        str(out_path),
    ]
    renderers: dict[int, ShotRenderer] = {}
    overlays = {text: typography.subtitle_overlay(size, text) for _, _, text in subtitles or []}
    black = Image.new("RGB", size)
    shot_of = []
    shot = 0
    for k in range(nframes):
        while shot + 1 < len(clips) and k / fps >= starts[shot + 1]:
            shot += 1
        shot_of.append(shot)

    def compose(k: int) -> bytes:
        t, shot = k / fps, shot_of[k]
        clip = clips[shot]
        local = t - starts[shot]
        frame = renderers[shot].frame(min(1.0, local / clip.duration))
        level = visibility(clips, shot, local, fade_s)
        if t < 0.4 and clip.image is not None:
            level = min(level, t / 0.4)
        if level < 1.0:
            frame = Image.blend(black, frame, max(0.0, level))
        for start, end, text in subtitles or []:
            if start <= t < end:
                frame.paste(overlays[text], (0, 0), overlays[text])
                break
        return frame.tobytes()

    batch = max(4, min(16, os.cpu_count() or 4))
    written, closed = 0, False
    with open(log_path, "wb") as log, ThreadPoolExecutor(batch) as pool:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)
        assert proc.stdin is not None
        try:
            for first in range(0, nframes, batch):
                ks = range(first, min(nframes, first + batch))
                needed = {shot_of[k] for k in ks}
                for i in needed - renderers.keys():
                    renderers[i] = ShotRenderer(clips[i], size)
                for i in [i for i in renderers if i < min(needed)]:
                    del renderers[i]
                for data in pool.map(compose, ks):
                    try:
                        proc.stdin.write(data)
                    except OSError:  # Windows reports a closed pipe as EINVAL; ffmpeg's exit code and log say why
                        closed = True
                        break
                    written += 1
                if closed:
                    break
                if progress:
                    progress(first / nframes)
        finally:
            with contextlib.suppress(OSError):
                proc.stdin.close()
            code = proc.wait()
    if code != 0 or closed:
        raise RuntimeError(f"ffmpeg stopped after {written} of {nframes} frames (exit code {code}): "
                           f"{log_path.read_text(errors='ignore')[-500:] or 'it wrote no error'}")
    log_path.unlink(missing_ok=True)
    return out_path


def replace_audio(film: Path, audio_wav: Path, out_path: Path, metadata: list[str] | None = None) -> Path:
    """The same picture with a different soundtrack; the video stream is copied, not re-encoded."""
    subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(film), "-i", str(audio_wav), "-map", "0:v",
                    "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                    "-movflags", "+faststart+use_metadata_tags", *(metadata or []), str(out_path)],
                   check=True, capture_output=True)
    return out_path


def _timestamp(s: float, sep: str) -> str:
    ms = int(round(s * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02}{sep}{ms % 1000:03}"


def write_srt(subtitles: list[tuple[float, float, str]], path: Path) -> None:
    lines = []
    for i, (a, b, text) in enumerate(subtitles, 1):
        lines += [str(i), f"{_timestamp(a, ',')} --> {_timestamp(b, ',')}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_vtt(subtitles: list[tuple[float, float, str]], path: Path) -> None:
    """WebVTT, which browsers show as a selectable caption track."""
    lines = ["WEBVTT", ""]
    for a, b, text in subtitles:
        lines += [f"{_timestamp(a, '.')} --> {_timestamp(b, '.')}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
