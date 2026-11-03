"""Backend interface."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..types import TaskKind


class BackendError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True, cost_usd: float = 0.0):
        super().__init__(message)
        self.retryable = retryable
        self.cost_usd = cost_usd


@dataclass
class Offer:
    kind: TaskKind
    model: str
    quality: int
    vision: bool = False
    estimate: Callable[[dict], float] = lambda payload: 0.0
    typical_s: float = 10.0
    licence: str = "builtin"


@dataclass
class ExecResult:
    output: dict[str, Any]
    cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


class Backend(ABC):
    id: str
    label: str
    location: str
    device: str
    is_cloud: bool = False
    max_concurrency: int = 1
    offers: list[Offer]
    source: str = "env"

    @abstractmethod
    async def probe(self) -> tuple[bool, str]: ...

    @abstractmethod
    async def execute(self, offer: Offer, payload: dict) -> ExecResult: ...

    async def aclose(self) -> None:
        return None

    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "location": self.location,
            "device": self.device,
            "is_cloud": self.is_cloud,
            "source": self.source,
            "max_concurrency": self.max_concurrency,
            "offers": [
                {"kind": o.kind.value, "model": o.model, "quality": o.quality, "vision": o.vision, "licence": o.licence}
                for o in self.offers
            ],
        }
