"""Device discovery and capability planning."""

import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..fabric.backends.gpuworker import allowed_models
from ..profile import catalog


@dataclass
class Device:
    id: str
    kind: str
    name: str
    total_gb: float
    free_gb: float
    caps: list[str] = field(default_factory=list)


OLLAMA_SHARE = 0.15


def discover_devices() -> list[Device]:
    try:
        import torch
    except ImportError:
        return [Device("cpu", "cpu", "CPU", 0.0, 0.0)]
    devices: list[Device] = []
    for i in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(i)
        try:
            free, total = torch.cuda.mem_get_info(i)
        except RuntimeError:
            free, total = props.total_memory, props.total_memory
        devices.append(Device(f"cuda:{i}", "cuda", props.name, total / 2**30, free / 2**30))
    if not devices and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        try:
            import psutil

            free = psutil.virtual_memory().available / 2**30
            total = psutil.virtual_memory().total / 2**30
        except ImportError:
            free = total = 8.0
        devices.append(Device("mps", "mps", "Apple GPU (Metal)", total, free * 0.5))
    return devices or [Device("cpu", "cpu", "CPU", 0.0, 0.0)]


def plan(devices: list[Device], licences: set[str], reserve_gb: float, only: list[str] | None = None,
         extra_reserve: dict[str, float] | None = None) -> list[Device]:
    local = catalog()["local"]
    budget = {d.id: d.free_gb - reserve_gb - (extra_reserve or {}).get(d.id, 0.0) for d in devices}

    def eligible(cap: str, spec: dict, d: Device) -> bool:
        if (only and cap not in only) or d.kind not in spec["devices"] or cap in d.caps:
            return False
        if not allowed_models(cap, licences):
            return False
        return d.kind == "cpu" or spec["memory_gb"] <= budget[d.id]

    def place(cap: str, spec: dict, d: Device) -> None:
        d.caps.append(cap)
        if d.kind != "cpu":
            budget[d.id] -= spec["memory_gb"]

    # Heavy work goes to the strongest GPU that can hold it; the rest goes where there's most room.
    for cap, spec in sorted(local.items(), key=lambda kv: (not kv[1].get("heavy"), -kv[1]["memory_gb"])):
        fits = [d for d in devices if eligible(cap, spec, d)]
        if fits:
            place(cap, spec, max(fits, key=(lambda d: d.total_gb) if spec.get("heavy") else (lambda d: budget[d.id])))
    for cap, spec in sorted(local.items(), key=lambda kv: kv[1]["memory_gb"]):
        if not spec.get("duplicate", True):
            continue
        for d in devices:
            if eligible(cap, spec, d):
                place(cap, spec, d)
    # Every other GPU that could hold heavy work or a backup model offers it too. Models load on first use, so this costs
    # nothing until the router sends it work because the first GPU is busy or offline.
    for cap, spec in local.items():
        for d in devices:
            if ((spec.get("heavy") or spec.get("on_call")) and d.kind != "cpu" and d.kind in spec["devices"] and cap not in d.caps
                    and (not only or cap in only) and allowed_models(cap, licences)
                    and spec["memory_gb"] <= d.free_gb - reserve_gb):
                d.caps.append(cap)
    return [d for d in devices if d.caps]


def ollama_reserve(devices: list[Device], sizes_gb: list[float]) -> dict[str, float]:
    gpus = [d for d in devices if d.kind in ("cuda", "mps")]
    if not gpus or not sizes_gb:
        return {}
    target = max(gpus, key=lambda d: d.total_gb)
    # Ollama loads a model only while it's in use, so it doesn't get to keep most of the strongest GPU idle.
    return {target.id: round(min(max(sizes_gb) * 1.2, target.total_gb * OLLAMA_SHARE), 2)}


def advertise_host(fabric_url: str) -> str:
    host = urlparse(fabric_url).hostname or "127.0.0.1"
    if host in ("127.0.0.1", "localhost", "::1"):
        return "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect((host, 80))
            return s.getsockname()[0]
        except OSError:
            return socket.gethostname()
