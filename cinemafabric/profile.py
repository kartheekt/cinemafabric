"""Deployment profile and model catalog."""

import os
import re
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).resolve().parent


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _load(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


@lru_cache
def profile() -> dict[str, Any]:
    from .config import get_settings

    data = _load(PACKAGE / "profiles" / "default.toml")
    custom = os.environ.get("CF_PROFILE") or get_settings().profile
    if custom and Path(custom).exists():
        data = _merge(data, _load(Path(custom)))
    return data


def cloud_model(role: str) -> str:
    return profile()["cloud"][role]


@lru_cache
def catalog() -> dict[str, Any]:
    return _load(PACKAGE / "catalog.toml")


def allowed_licences() -> set[str]:
    return set(profile()["licences"]["allowed"])


def _names(model: str, family: str, key: str) -> bool:
    """True when `key` starts a name segment, so "llama3.1" matches "llama" but "notllama" doesn't."""
    return re.search(rf"(?:^|[^a-z0-9]){re.escape(key.lower())}", f"{model} {family}".lower()) is not None


def ollama_licence(model: str, family: str = "") -> str:
    for key, licence in catalog()["ollama"]["families"].items():
        if _names(model, family, key):
            return licence
    return "unknown"


def family_allowed(model: str, family: str = "", families: list[str] | None = None) -> bool:
    return any(_names(model, family, f) for f in (profile()["models"]["families"] if families is None else families))
