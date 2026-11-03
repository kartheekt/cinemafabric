"""JSON schemas sent to the models (structured output) and the validation models behind them."""

from pydantic import BaseModel, Field, field_validator

from .options import CAMERAS


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


STR = {"type": "string"}

TREATMENT_SCHEMA = {
    "name": "treatment",
    "schema": _obj(
        {
            "title": STR,
            "logline": STR,
            "tagline": STR,
            "tone": STR,
            "setting": STR,
            "music_mood": STR,
            "music_cues": {"type": "array", "items": STR},
            "runtime_seconds": {"type": "integer"},
            "runtime_reason": STR,
            "characters": {"type": "array", "items": _obj({"name": STR, "look": STR})},
            "places": {"type": "array", "items": _obj({"name": STR, "look": STR})},
            "props": {"type": "array", "items": _obj({"name": STR, "look": STR})},
            "story": STR,
            "beats": {"type": "array", "items": STR},
        }
    ),
}

SHOT_SCHEMA = _obj(
    {
        "visual": STR,
        "camera": {"type": "string", "enum": CAMERAS},
        "duration": {"type": "number"},
        "narration": STR,
        "speaker": STR,
        "characters": {"type": "array", "items": STR},
        "title_card": STR,
    }
)

SCRIPT_SCHEMA = {
    "name": "script",
    "schema": _obj(
        {"scenes": {"type": "array", "items": _obj({"heading": STR, "summary": STR, "shots": {"type": "array", "items": SHOT_SCHEMA}})}}
    ),
}

SHOT_PROMPTS_SCHEMA = {
    "name": "shot_prompts",
    "schema": _obj({"prompts": {"type": "array", "items": _obj({"index": {"type": "integer"}, "prompt": STR})}}),
}

SAFETY_REVIEW_SCHEMA = {
    "name": "safety_review",
    "schema": _obj({"harmful": {"type": "boolean"}, "category": STR, "reason": STR}),
}

TRANSLATION_SCHEMA = {"name": "translation", "schema": _obj({"lines": {"type": "array", "items": STR}})}

PICTURE_FIELDS = {"real_person": {"type": "boolean"}, "brand_logo": {"type": "boolean"},
                  "known_character": {"type": "boolean"}, "stray_text": {"type": "boolean"}}

REVIEW_SCHEMA = {
    "name": "frame_review",
    "schema": _obj({"score": {"type": "integer"}, "matches": {"type": "boolean"}, "issue": STR, **PICTURE_FIELDS}),
}

PICTURE_SCHEMA = {"name": "picture_check", "schema": _obj(PICTURE_FIELDS)}


class Character(BaseModel):
    name: str
    look: str


class Treatment(BaseModel):
    title: str
    logline: str
    tagline: str = ""
    tone: str = ""
    setting: str = ""
    music_mood: str = "cinematic orchestral"
    music_cues: list[str] = Field(default_factory=list)
    runtime_seconds: int = 0
    runtime_reason: str = ""
    characters: list[Character] = Field(default_factory=list)
    places: list[Character] = Field(default_factory=list)
    props: list[Character] = Field(default_factory=list)
    story: str = ""
    beats: list[str] = Field(default_factory=list)


class Shot(BaseModel):
    visual: str = ""
    camera: str = "static"
    duration: float = 3.0
    narration: str = ""
    speaker: str = ""
    characters: list[str] = Field(default_factory=list)
    title_card: str = ""

    @field_validator("camera")
    @classmethod
    def known_camera(cls, v: str) -> str:
        return v if v in CAMERAS else "static"


class Scene(BaseModel):
    heading: str = ""
    summary: str = ""
    shots: list[Shot]


class Script(BaseModel):
    scenes: list[Scene]

    def flat_shots(self) -> list[tuple[int, Shot]]:
        return [(si, shot) for si, scene in enumerate(self.scenes) for shot in scene.shots]


CONCERNS = {"real_person": "a real, recognisable person", "brand_logo": "a real brand or logo",
            "known_character": "an existing character", "stray_text": "writing, a signature or a watermark"}


class PictureCheck(BaseModel):
    real_person: bool = False
    brand_logo: bool = False
    known_character: bool = False
    stray_text: bool = False

    @property
    def concerns(self) -> list[str]:
        return [label for field, label in CONCERNS.items() if getattr(self, field)]


class Review(PictureCheck):
    score: int = 7
    matches: bool = True
    issue: str = ""
