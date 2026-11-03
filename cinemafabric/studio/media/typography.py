"""Text rendering for posters, title cards and subtitles."""

import textwrap
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SERIF = [
    "C:/Windows/Fonts/georgiab.ttf",
    "C:/Windows/Fonts/timesbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
]
SANS = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
SANS_BOLD = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


@lru_cache(maxsize=64)
def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for path in {"serif": SERIF, "sans": SANS, "sans_bold": SANS_BOLD}[kind]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def spaced_width(text: str, fnt: ImageFont.FreeTypeFont, tracking: float) -> float:
    return sum(fnt.getlength(ch) for ch in text) + tracking * max(len(text) - 1, 0)


def draw_spaced(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, fnt, fill, tracking: float) -> None:
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += fnt.getlength(ch) + tracking


def fit_spaced(text: str, kind: str, max_width: float, start_size: int, tracking_ratio: float = 0.12):
    size = start_size
    while size > 12:
        fnt = font(kind, size)
        if spaced_width(text, fnt, size * tracking_ratio) <= max_width:
            return fnt, size * tracking_ratio
        size -= 2
    return font(kind, 12), 1.0


def centered_spaced(draw, text, kind, y, width, max_width, start_size, fill, tracking_ratio=0.12):
    fnt, tracking = fit_spaced(text, kind, max_width, start_size, tracking_ratio)
    w = spaced_width(text, fnt, tracking)
    draw_spaced(draw, ((width - w) / 2, y), text, fnt, fill, tracking)
    return fnt.size


def poster(art: Image.Image, title: str, tagline: str, credits: str, label: str = "") -> Image.Image:
    """Lay out a one-sheet: tagline on top, title and billing block at the bottom."""
    img = art.convert("RGB")
    w, h = img.size
    shade = np.zeros((h, w), dtype=np.float32)
    ramp = np.linspace(0, 1, h)
    shade += np.clip((ramp - 0.55) / 0.45, 0, 1)[:, None] * 0.85
    shade += np.clip((0.18 - ramp) / 0.18, 0, 1)[:, None] * 0.6
    arr = np.asarray(img, dtype=np.float32) * (1 - shade[..., None])
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(img)
    centered_spaced(draw, tagline.upper(), "sans", h * 0.045, w, w * 0.86, int(h * 0.028), (235, 235, 235), 0.25)
    title_size = centered_spaced(draw, title.upper(), "serif", h * 0.72, w, w * 0.9, int(h * 0.11), (250, 246, 238), 0.08)
    small = font("sans", max(10, int(h * 0.0125)))
    y = h * 0.72 + title_size * 1.35
    for line in textwrap.wrap(credits.upper(), 95)[:3]:
        lw = small.getlength(line)
        draw.text(((w - lw) / 2, y), line, font=small, fill=(190, 190, 190))
        y += small.size * 1.4
    centered_spaced(draw, "COMING SOON", "sans_bold", h * 0.94, w, w * 0.5, int(h * 0.02), (220, 220, 220), 0.4)
    if label:
        centered_spaced(draw, label.upper(), "sans", h * 0.972, w, w * 0.4, int(h * 0.011), (150, 150, 150), 0.3)
    return img


def title_card(size: tuple[int, int], text: str, sub: str = "", note: str = "") -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, (0, 0, 0))
    draw = ImageDraw.Draw(img)
    big = min(w, h) * (0.11 if len(text) < 18 else 0.075)
    fsize = centered_spaced(draw, text.upper(), "serif", h * 0.44, w, w * 0.84, int(big), (245, 240, 230), 0.1)
    if sub:
        centered_spaced(draw, sub.upper(), "sans", h * 0.44 + fsize * 1.5, w, w * 0.7, int(big * 0.28), (170, 170, 170), 0.35)
    if note:
        centered_spaced(draw, note.upper(), "sans", h * 0.9, w, w * 0.6, int(min(w, h) * 0.022), (130, 130, 130), 0.3)
    glow = img.filter(ImageFilter.GaussianBlur(min(w, h) / 60))
    return Image.blend(img, glow, 0.35)


def subtitle_overlay(size: tuple[int, int], text: str) -> Image.Image:
    """Transparent RGBA layer with an outlined caption near the bottom."""
    w, h = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    fsize = max(14, int(min(w, h) * 0.045))
    fnt = font("sans_bold", fsize)
    lines = textwrap.wrap(text, max(16, int(w / (fsize * 0.55))))[:2]
    y = h - fsize * (1.6 * len(lines) + 1.4)
    for line in lines:
        lw = fnt.getlength(line)
        draw.text(((w - lw) / 2, y), line, font=fnt, fill=(255, 255, 255, 255),
                  stroke_width=max(2, fsize // 12), stroke_fill=(0, 0, 0, 220))
        y += fsize * 1.6
    return layer
