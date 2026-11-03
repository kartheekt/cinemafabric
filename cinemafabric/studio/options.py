"""User-facing production options and the job request model."""

import math
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..fabric.types import Preference

FINAL_CARD_S = 3.5

FORMATS = {
    "trailer": {"label": "Trailer", "min": 15, "max": 90, "default": 30, "presets": [30, 60, 90],
                "shot_s": 3.0, "line_every_s": 6},
    "movie": {"label": "Short movie", "min": 60, "max": 300, "default": 120, "presets": [60, 120, 180, 300],
              "shot_s": 6.0, "line_every_s": 6},
}


@dataclass(frozen=True)
class Runtime:
    """Everything that scales with a film's length."""

    format: str
    seconds: int
    shots: int
    scenes: int
    lines: tuple[int, int]
    max_cards: int
    beats: tuple[int, int] = (4, 6)

    @property
    def label(self) -> str:
        if self.format == "trailer":
            return f"{self.seconds}-second trailer"
        minutes, rest = divmod(self.seconds, 60)
        return f"{minutes}-minute movie" if rest == 0 else f"{minutes}:{rest:02d} movie"


def runtime(fmt: str, seconds: int) -> Runtime:
    f = FORMATS[fmt]
    s = int(min(max(seconds, f["min"]), f["max"]))
    shots = max(4, round((s - FINAL_CARD_S) / f["shot_s"]))
    scenes = 1 if fmt == "trailer" else max(2, round(s / 30))
    n = s / f["line_every_s"]
    lines = (max(2, math.floor(n * 0.8)), max(3, math.ceil(n * 1.2)))
    max_cards = (2 if s <= 60 else 3) if fmt == "trailer" else max(2, s // 60 + 1)
    beats = (4, 6) if fmt == "trailer" else (max(5, s // 20), max(6, math.ceil(s / 15)))
    return Runtime(fmt, s, shots, scenes, lines, max_cards, beats)

STYLES = {
    "live-action": {
        "label": "Live-action",
        "prompt": ("cinematic live-action film still, anamorphic lens, natural film grain, motivated dramatic lighting, "
                   "shallow depth of field"),
    },
    "animation-3d": {
        "label": "3D Animation",
        "prompt": "stylized 3D animated feature film still, soft global illumination, expressive characters, rich saturated color",
    },
    "anime": {
        "label": "Anime",
        "prompt": "hand-drawn anime film still, clean cel shading, painterly detailed backgrounds, cinematic composition",
    },
    "noir": {
        "label": "Film Noir",
        "prompt": "black and white film noir still, high contrast, hard venetian-blind shadows, 1940s cinematography",
    },
    "claymation": {
        "label": "Claymation",
        "prompt": "stop-motion claymation still, handmade clay textures, miniature practical set, warm tactile lighting",
    },
    "watercolor": {
        "label": "Watercolor",
        "prompt": "watercolor storybook illustration, soft pigment washes, visible paper texture, gentle light",
    },
}

GENRES = ["Sci-fi", "Fantasy", "Thriller", "Drama", "Adventure", "Comedy", "Horror", "Romance", "Mystery"]

ASPECTS = {
    "16:9": {"label": "16:9 Widescreen", "video": (1280, 720), "image": "1792x1024"},
    "2.39:1": {"label": "2.39:1 Scope", "video": (1280, 536), "image": "1792x1024"},
    "9:16": {"label": "9:16 Reels", "video": (720, 1280), "image": "1024x1792"},
    "1:1": {"label": "1:1 Square", "video": (720, 720), "image": "1024x1024"},
}

# Sizes above are 720p; each resolution scales them. Pictures come back about 2000 px wide, so 1440p and 4K are upscaled.
RESOLUTIONS = {
    "720p": ("720p (fastest)", 1.0),
    "1080p": ("1080p Full HD", 1.5),
    "1440p": ("1440p (upscaled)", 2.0),
    "2160p": ("4K (upscaled, slow)", 3.0),
}
FRAME_RATES = {24: "24 fps (cinema)", 30: "30 fps", 60: "60 fps (smoothest, slower)"}
ANIMATED_STYLES = {"anime", "animation-3d", "claymation", "watercolor"}


def video_size(aspect: str, resolution: str) -> tuple[int, int]:
    w, h = ASPECTS[aspect]["video"]
    k = RESOLUTIONS.get(resolution, RESOLUTIONS["1080p"])[1]
    return int(round(w * k / 2) * 2), int(round(h * k / 2) * 2)

PREFERENCES = {
    Preference.LOCAL_FIRST.value: "Local-first",
    Preference.BALANCED.value: "Balanced",
    Preference.CLOUD_FIRST.value: "Cloud-first",
}

LANGUAGES = {
    "hi": "Hindi", "te": "Telugu", "ta": "Tamil", "bn": "Bengali", "mr": "Marathi", "kn": "Kannada", "ml": "Malayalam",
    "gu": "Gujarati", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese", "ja": "Japanese",
    "ko": "Korean", "ar": "Arabic", "id": "Indonesian", "tr": "Turkish", "vi": "Vietnamese", "sw": "Swahili",
}
MAX_LANGUAGES = 4
# Voice-over languages, each tested by speaking a sentence with MMS-TTS and transcribing it back with Muse Voice
# Transcribe.
VOICE_LANGUAGES = {
    "en": ("English", "eng"), "ar": ("Arabic", "ara"), "bn": ("Bengali", "ben"), "fr": ("French", "fra"),
    "de": ("German", "deu"), "gu": ("Gujarati", "guj"), "hi": ("Hindi", "hin"), "id": ("Indonesian", "ind"),
    "kn": ("Kannada", "kan"), "ml": ("Malayalam", "mal"), "mr": ("Marathi", "mar"), "pt": ("Portuguese", "por"),
    "es": ("Spanish", "spa"), "sw": ("Swahili", "swh"), "ta": ("Tamil", "tam"), "te": ("Telugu", "tel"),
    "tr": ("Turkish", "tur"), "vi": ("Vietnamese", "vie"),
}
OWN_SCRIPT = {"hi", "te", "ta", "bn", "mr", "kn", "ml", "gu", "ja", "ko", "ar"}

CAMERAS = ["push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down", "static", "parallax"]


class JobOptions(BaseModel):
    prompt: str = Field(min_length=8, max_length=1200)
    format: Literal["trailer", "movie"] = "trailer"
    length: Literal["auto", "fixed"] = "fixed"
    seconds: int | None = None
    genre: str = "Sci-fi"
    style: str = "live-action"
    aspect: str = "16:9"
    preference: Preference = Preference.LOCAL_FIRST
    premium_script: bool = False
    critic: bool = True
    narration: bool = True
    fresh: bool = False
    review_script: bool = False
    max_cost_usd: float | None = Field(default=None, gt=0)
    subtitle_languages: list[str] = Field(default_factory=list, max_length=MAX_LANGUAGES)
    cast_ids: list[str] = Field(default_factory=list, max_length=3)
    voice_language: str = "en"
    commercial_safe: bool = False
    agents: bool = True
    resolution: Literal["720p", "1080p", "1440p", "2160p"] = "1080p"
    fps: Literal[24, 30, 60] = 24

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_duration(cls, data: Any) -> Any:
        """Jobs saved before custom lengths used duration="trailer30" / "movie120"."""
        if isinstance(data, dict) and "duration" in data and "format" not in data:
            data = dict(data)
            legacy = data.pop("duration")
            data["format"], data["seconds"] = ("movie", 120) if legacy == "movie120" else ("trailer", 30)
            data["length"] = "fixed"
        return data

    @property
    def premium_writing(self) -> bool:
        """A short film stands or falls on its story, so it always gets the strongest writer."""
        return self.premium_script or self.format == "movie"

    @property
    def target_seconds(self) -> int:
        """The length to plan for before the story is written (auto uses the format default)."""
        return self.seconds if self.length == "fixed" and self.seconds else FORMATS[self.format]["default"]

    def validate_choices(self) -> None:
        f = FORMATS[self.format]
        if self.length == "fixed" and self.seconds is not None and not f["min"] <= self.seconds <= f["max"]:
            raise ValueError(f"a {self.format} must be between {f['min']} and {f['max']} seconds")
        if self.voice_language not in VOICE_LANGUAGES:
            raise ValueError(f"voice-over language must be one of {list(VOICE_LANGUAGES)}")
        unknown = [code for code in self.subtitle_languages if code not in LANGUAGES]
        if unknown:
            raise ValueError(f"unknown subtitle language: {', '.join(unknown)}")
        if self.genre not in GENRES:
            raise ValueError(f"genre must be one of {GENRES}")
        if self.style not in STYLES:
            raise ValueError(f"style must be one of {list(STYLES)}")
        if self.aspect not in ASPECTS:
            raise ValueError(f"aspect must be one of {list(ASPECTS)}")


def catalog() -> dict:
    return {
        "formats": [{"id": k, "label": v["label"], "min": v["min"], "max": v["max"], "default": v["default"],
                     "presets": v["presets"]} for k, v in FORMATS.items()],
        "styles": [{"id": k, "label": v["label"]} for k, v in STYLES.items()],
        "genres": GENRES,
        "aspects": [{"id": k, "label": v["label"]} for k, v in ASPECTS.items()],
        "resolutions": [{"id": k, "label": v[0]} for k, v in RESOLUTIONS.items()],
        "frame_rates": [{"id": str(k), "label": v} for k, v in FRAME_RATES.items()],
        "preferences": [{"id": k, "label": v} for k, v in PREFERENCES.items()],
        "languages": [{"id": k, "label": v} for k, v in LANGUAGES.items()],
        "voice_languages": [{"id": k, "label": v[0]} for k, v in VOICE_LANGUAGES.items()],
        "max_languages": MAX_LANGUAGES,
    }
