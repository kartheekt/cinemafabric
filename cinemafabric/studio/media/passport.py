"""The Production Passport: one card that says what made a film, what it cost and what checked it."""

import re
from pathlib import Path

from PIL import Image, ImageDraw

from . import provenance
from .render import cover
from .typography import font

SIZE = (1200, 630)
INK, TEXT, MUTED, GOLD, GOOD = (10, 11, 15), (233, 230, 223), (150, 156, 172), (245, 184, 74), (74, 222, 128)
OFFLINE = re.compile(r"(\S+) skipped \((disabled|circuit open|removed|[^)]*unreachable[^)]*)\)")


def facts(state: dict, receipt: dict, decisions: list[dict]) -> dict:
    """The numbers on the card, from the job state, its ledger receipt and its routing log."""
    ok = [d for d in decisions if d.get("status") == "ok"]
    hosts: list[str] = []
    for d in ok:
        label = d.get("worker_label") or ""
        host = "Meta Model API" if d.get("is_cloud") else label.split(" · ")[0]
        if host and host not in hosts:
            hosts.append(host)
    moved = sum(1 for d in ok if d.get("fallbacks") or OFFLINE.search(d.get("reason") or ""))
    checks = state.get("checks", [])
    wm = state.get("watermark") or {}
    voice = (state.get("options") or {}).get("voice_language", "en")
    tracks = [t["label"] for t in (state.get("assets") or {}).get("subtitle_tracks", [])]
    return {
        "title": (state.get("story") or {}).get("title") or "Untitled",
        "format": (state.get("runtime") or {}).get("label", ""),
        "cost": float(receipt.get("cost_usd") or 0.0),
        "all_cloud": float(receipt.get("cost_usd") or 0.0) + float(receipt.get("avoided_usd") or 0.0),
        "local": int(receipt.get("local_tasks") or 0),
        "tasks": int(receipt.get("local_tasks") or 0) + int(receipt.get("cloud_tasks") or 0),
        "cached": int(receipt.get("cache_hits") or 0),
        "machines": hosts,
        "moved": moved,
        "failed": sum(1 for d in decisions if d.get("status") != "ok"),
        "checks_ok": sum(1 for c in checks if c.get("verdict") in ("passed", "allowed with notes", "cleared on review", "redrawn")),
        "checks": len(checks),
        "watermark": bool(wm.get("verified")),
        "voice": voice,
        "captions": tracks,
        "llama": any("llama" in (d.get("model") or "").lower() for d in ok),
    }


def render(poster: Path | None, f: dict, voice_name: str) -> Image.Image:
    h = SIZE[1]
    img = Image.new("RGB", SIZE, INK)
    draw = ImageDraw.Draw(img)
    left = 0
    if poster and poster.exists():
        art = cover(Image.open(poster).convert("RGB"), (420, h)).resize((420, h), Image.Resampling.LANCZOS)
        img.paste(art, (0, 0))
        left = 420
    x, y = left + 48, 44
    draw.text((x, y), "PRODUCTION PASSPORT", font=font("sans_bold", 18), fill=GOLD)
    y += 34
    title = f["title"] if len(f["title"]) <= 28 else f["title"][:27] + "…"
    draw.text((x, y), title, font=font("serif", 48), fill=TEXT)
    y += 66
    draw.text((x, y), f["format"], font=font("sans", 20), fill=MUTED)
    y += 46

    def row(label: str, value: str, colour=TEXT):
        nonlocal y
        draw.text((x, y), label.upper(), font=font("sans_bold", 13), fill=MUTED)
        draw.text((x + 170, y - 4), value, font=font("sans", 22), fill=colour)
        y += 44

    saved = f["all_cloud"] - f["cost"]
    row("Cloud cost", f"${f['cost']:.3f}" + (f"   (all-cloud ≈ ${f['all_cloud']:.3f})" if saved > 0.0005 else ""), GOLD)
    row("Your machines", f"{f['local']} of {f['tasks']} tasks" + (f", {f['cached']} reused free" if f["cached"] else ""))
    row("Made on", ", ".join(f["machines"][:4]) or "—")
    row("Reroutes", f"{f['moved']} moved, {'none lost' if f['failed'] == 0 else str(f['failed']) + ' failed'}"
        if f["moved"] else "none needed")
    row("Checks", f"{f['checks_ok']} of {f['checks']} passed" + ("  ·  watermark verified" if f["watermark"] else ""),
        GOOD if f["checks_ok"] == f["checks"] else TEXT)
    row("Language", f"voice {voice_name}" + (f"  ·  captions {', '.join(f['captions'])}" if f["captions"] else ""))
    footer = f"{provenance.LABEL} · CinemaFabric" + (" · Built with Llama" if f["llama"] else "")
    draw.text((x, h - 48), footer, font=font("sans", 16), fill=MUTED)
    return img
