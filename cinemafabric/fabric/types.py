"""Shared request/response types for the fabric API."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskKind(str, Enum):
    TEXT = "text.generate"
    IMAGE = "image.generate"
    SEGMENT = "image.segment"
    TTS = "audio.tts"
    MUSIC = "audio.music"
    TRANSCRIBE = "audio.transcribe"
    MODERATE = "text.moderate"
    WATERMARK = "audio.watermark"
    DEPTH = "image.depth"


class Tier(str, Enum):
    DRAFT = "draft"
    STANDARD = "standard"
    PREMIUM = "premium"

    @property
    def min_quality(self) -> int:
        return {"draft": 1, "standard": 2, "premium": 3}[self.value]


class Preference(str, Enum):
    LOCAL_FIRST = "local_first"
    BALANCED = "balanced"
    CLOUD_FIRST = "cloud_first"


class TaskRequest(BaseModel):
    kind: TaskKind
    payload: dict[str, Any]
    tier: Tier = Tier.STANDARD
    preference: Preference = Preference.LOCAL_FIRST
    job_id: str | None = None
    stage: str | None = None
    label: str | None = None
    cache: bool = True
    max_job_usd: float | None = None
    exclude_licences: list[str] = Field(default_factory=list, max_length=20)


class Candidate(BaseModel):
    worker_id: str
    model: str
    is_cloud: bool
    est_cost_usd: float
    expected_s: float
    rejected: str | None = None


class RouteDecision(BaseModel):
    worker_id: str
    worker_label: str
    model: str
    is_cloud: bool
    reason: str
    candidates: list[Candidate] = Field(default_factory=list)
    fallbacks: list[str] = Field(default_factory=list)
    licence: str | None = None


class TaskResult(BaseModel):
    task_id: str
    kind: TaskKind
    output: dict[str, Any]
    decision: RouteDecision
    cost_usd: float
    cloud_equivalent_usd: float
    latency_ms: int
    cached: bool = False
