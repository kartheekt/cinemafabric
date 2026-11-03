"""Platform versions of a finished film: re-framed over a blurred fill, loudness-normalised for streaming."""

import json
import re
import subprocess
from pathlib import Path

from . import provenance
from .render import ffmpeg_bin

PRESETS = {
    "vertical": {"label": "Vertical 9:16 (Reels, Shorts)", "size": (1080, 1920)},
    "widescreen": {"label": "Widescreen 16:9 (YouTube)", "size": (1920, 1080)},
    "square": {"label": "Square 1:1 (feeds)", "size": (1080, 1080)},
}
TARGET = "I=-14:TP=-1.5:LRA=11"


def loudness_filter(film: Path) -> str:
    """Two-pass loudnorm: measure first, then correct linearly, which lands on target even for short films."""
    probe = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(film), "-af", f"loudnorm={TARGET}:print_format=json",
                            "-f", "null", "-"], capture_output=True, text=True)
    found = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", probe.stderr)
    if not found:
        return f"loudnorm={TARGET}"
    m = json.loads(found.group(0))
    return (f"loudnorm={TARGET}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
            f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")


def export(film: Path, out: Path, preset: str, title: str) -> Path:
    w, h = PRESETS[preset]["size"]
    frame = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:2,eq=brightness=-0.08[bgb];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(film), "-filter_complex", frame, "-map", "[v]",
           "-map", "0:a", "-af", loudness_filter(film), "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", "30",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart+use_metadata_tags",
           *provenance.ffmpeg_tags(title), str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"export failed: {result.stderr[-400:]}")
    return out
