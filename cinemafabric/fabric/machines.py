"""Machines added at runtime: from the UI, or by a GPU worker registering itself with a join token."""

import asyncio
import contextlib
import ipaddress
import json
import os
import re
import secrets
import socket
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from ..config import Settings
from ..db import open_db
from .backends.base import Backend
from .backends.gpuworker import CAP_KINDS, GpuWorkerBackend
from .backends.ollama import OllamaBackend

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    spec TEXT NOT NULL
);
"""


class MachineSpec(BaseModel):
    id: str | None = None
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["ollama", "gpu-worker"]
    base_url: str = Field(max_length=300)
    model: str | None = Field(default=None, max_length=200)
    caps: list[str] = Field(default_factory=list, max_length=8)
    device: str = Field(default="", max_length=200)
    quality: int | None = Field(default=None, ge=1, le=3)
    vision: bool | None = None
    secret: str | None = Field(default=None, max_length=128)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "machine"


def guess_quality(parameter_size: str, name: str = "") -> int:
    m = re.match(r"([\d.]+)\s*([BM])", parameter_size or "", re.I) or re.search(r"(\d+(?:\.\d+)?)([bm])\b", name, re.I)
    if not m:
        return 1
    billions = float(m.group(1)) / (1000 if m.group(2).upper() == "M" else 1)
    return 2 if billions >= 20 else 1


def is_guard(model: str) -> bool:
    return "guard" in model.lower()


def is_meta(model: str, family: str = "") -> bool:
    """Meta's open-weight families as Ollama names them (Llama, Llama Guard, Muse)."""
    return any(k in f"{model} {family}".lower() for k in ("llama", "muse", "glimmer"))


def validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL must look like http://host:port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URL must be just scheme, host, port and an optional path")
    return url.strip().rstrip("/")


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = getattr(ip, "ipv4_mapped", None) or ip
    if ip.is_loopback:
        return False
    if ip.version == 4 and ip.is_link_local:
        return True
    return ip.is_multicast or ip.is_unspecified or ip.is_reserved


async def check_target(url: str) -> str:
    """Machines live on loopback or the LAN; refuse IPv4 link-local (cloud metadata), multicast and reserved ranges.

    IPv6 link-local stays allowed: that's where .local (mDNS) names often resolve on a home network.
    """
    url = validate_url(url)
    host = urlparse(url).hostname or ""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"can't resolve {host}") from e
    for info in infos:
        if _blocked(ipaddress.ip_address(str(info[4][0]).split("%")[0])):
            raise ValueError(f"{host} resolves to an address machines can't use")
    return url


async def inspect(kind: str, base_url: str, secret: str | None = None) -> dict:
    """Ask a server what it can do, so the UI can offer sensible defaults."""
    url = await check_target(base_url)
    headers = {"X-Worker-Secret": secret} if secret else None
    async with httpx.AsyncClient(base_url=url, timeout=8.0, headers=headers) as client:
        if kind == "gpu-worker":
            info = (await client.get("/health")).raise_for_status().json()
            return {"kind": kind, "device": info.get("device", ""), "caps": info.get("caps", [])}
        tags = (await client.get("/api/tags")).raise_for_status().json().get("models", [])
        models = []
        for m in tags:
            name = m.get("name", "")
            details = m.get("details") or {}
            try:
                caps = (await client.post("/api/show", json={"model": name})).json().get("capabilities") or []
            except (httpx.HTTPError, ValueError):
                caps = []
            if "embedding" in caps and "completion" not in caps:
                continue
            models.append({
                "name": name,
                "parameter_size": details.get("parameter_size", ""),
                "family": details.get("family", ""),
                "vision": "vision" in caps,
                "guard": is_guard(name),
                "meta": is_meta(name, details.get("family", "")),
                "quality": guess_quality(details.get("parameter_size", ""), name),
                "size_gb": round(m.get("size", 0) / 2**30, 2),
            })
        return {"kind": kind, "models": models}


def build(spec: MachineSpec) -> Backend:
    assert spec.id
    if spec.kind == "gpu-worker":
        caps = [c for c in spec.caps if c in CAP_KINDS]
        if not caps:
            raise ValueError("a worker needs at least one of: " + ", ".join(CAP_KINDS))
        backend: Backend = GpuWorkerBackend(spec.id, spec.label, spec.device or "GPU", spec.base_url, caps, spec.secret)
    else:
        if not spec.model:
            raise ValueError("pick a model on the Ollama server")
        quality = spec.quality or guess_quality("", spec.model)
        backend = OllamaBackend(
            id=spec.id, label=spec.label, location="lan", device=spec.device or urlparse(spec.base_url).hostname or "",
            base_url=spec.base_url, model=spec.model, quality=quality, vision=bool(spec.vision),
            typical_s=60.0 if quality >= 2 else 15.0, role="guard" if is_guard(spec.model) else "text",
            secret=spec.secret,
        )
    backend.source = "added"
    return backend


class MachineStore:
    def __init__(self, path: Path):
        self.db = open_db(path, SCHEMA)

    def all(self) -> list[MachineSpec]:
        return [MachineSpec.model_validate_json(r["spec"]) for r in self.db.query("SELECT spec FROM machines ORDER BY created")]

    def save(self, spec: MachineSpec) -> None:
        self.db.upsert("machines", {"id": spec.id, "created": time.time(), "spec": spec.model_dump_json()}, "id")

    def delete(self, machine_id: str) -> bool:
        exists = bool(self.db.query("SELECT 1 FROM machines WHERE id = ?", (machine_id,)))
        self.db.execute("DELETE FROM machines WHERE id = ?", (machine_id,))
        return exists


def _secret(settings: Settings, name: str, configured: str) -> str:
    if configured:
        return configured
    path = settings.path(name)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(32))
    except FileExistsError:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    return path.read_text(encoding="utf-8").strip()


def join_token(settings: Settings) -> str:
    """Lets a node register machines. CF_JOIN_TOKEN, or a random one kept in the data directory."""
    return _secret(settings, "join_token", settings.join_token)


def fabric_token(settings: Settings) -> str:
    """Full access to the fabric API. CF_FABRIC_TOKEN, or a random one kept in the data directory."""
    return _secret(settings, "fabric_token", settings.fabric_token)


def describe(spec: MachineSpec) -> dict:
    return json.loads(spec.model_dump_json(exclude={"secret"}))
