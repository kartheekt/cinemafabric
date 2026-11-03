"""Join this machine to a fabric.

    CF_JOIN_TOKEN=<token> python -m cinemafabric.node --join http://<fabric>:8100
    CF_JOIN_TOKEN=<token> python -m cinemafabric.node --join http://<fabric>:8100 --dry-run
"""

import argparse
import asyncio
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..fabric import machines
from ..fabric.backends.gpuworker import allowed_models
from ..profile import family_allowed
from .discovery import advertise_host, discover_devices, ollama_reserve, plan


def fetch_policy(fabric: str, token: str) -> dict:
    r = httpx.get(f"{fabric}/v1/policy", headers={"X-Join-Token": token}, timeout=10)
    if r.status_code == 401:
        raise SystemExit("the fabric rejected the join token")
    r.raise_for_status()
    return r.json()


def register(fabric: str, token: str, spec: dict) -> str:
    r = httpx.post(f"{fabric}/v1/machines", json=spec, headers={"X-Join-Token": token}, timeout=30)
    return "ok" if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:160]}"


def ollama_specs(url: str, families: list[str], host: str, local_fabric: bool, ignore: tuple[str, ...] = ()) -> list[dict]:
    try:
        found = asyncio.run(machines.inspect("ollama", url))
    except Exception as e:
        print(f"  ollama at {url}: not reachable ({e.__class__.__name__})")
        return []
    base = url
    parsed = urlparse(url)
    if parsed.hostname in ("127.0.0.1", "localhost") and not local_fabric:
        base = f"{parsed.scheme}://{host}:{parsed.port or 11434}"
    node = socket.gethostname()
    specs = []
    for m in found["models"]:
        if m["name"] in ignore or not family_allowed(m["name"], m.get("family", ""), families):
            continue
        specs.append({
            "id": machines.slug(f"{node}-{m['name']}"),
            "label": f"{node} · {m['name']}",
            "kind": "ollama",
            "base_url": base,
            "model": m["name"],
            "quality": m["quality"],
            "vision": m["vision"],
            "device": f"{node} (Ollama)",
            "size_gb": m.get("size_gb", 0.0),
            "local": parsed.hostname in ("127.0.0.1", "localhost"),
            "target": url,
        })
    return specs


def main() -> None:
    parser = argparse.ArgumentParser(description="Join this machine to a CinemaFabric fabric")
    parser.add_argument("--join", required=True, help="fabric URL")
    parser.add_argument("--token", default=os.environ.get("CF_JOIN_TOKEN", ""), help="prefer the CF_JOIN_TOKEN variable")
    parser.add_argument("--ollama", action="append", default=None, help="Ollama URL (repeatable)")
    parser.add_argument("--caps", default="", help="limit worker capabilities, e.g. tts,segment")
    parser.add_argument("--reserve-gb", type=float, default=None, help="memory to leave free on each device")
    parser.add_argument("--port", type=int, default=None, help="first worker port")
    parser.add_argument("--no-workers", action="store_true")
    parser.add_argument("--no-ollama", action="store_true")
    parser.add_argument("--ollama-port", type=int, default=None, help="port for the Ollama gate (other machines use it)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if any(a == "--token" or a.startswith("--token=") for a in sys.argv[1:]):
        print("note: --token is visible to other users in the process list; prefer CF_JOIN_TOKEN")

    fabric = args.join.rstrip("/")
    policy = fetch_policy(fabric, args.token)
    node_cfg = policy.get("node", {})
    reserve = args.reserve_gb if args.reserve_gb is not None else float(node_cfg.get("reserve_gb", 1.5))
    port = args.port or int(node_cfg.get("worker_port", 8201))
    host = advertise_host(fabric)
    local_fabric = host == "127.0.0.1"
    bind = "127.0.0.1" if local_fabric else "0.0.0.0"

    specs = [] if args.no_ollama else [
        s for url in (args.ollama or ["http://127.0.0.1:11434"])
        for s in ollama_specs(url, policy["families"], host, local_fabric, tuple(policy.get("ignore", [])))
    ]
    gates: list[tuple[list[str], dict]] = []
    if specs and not local_fabric:
        gate_port = args.ollama_port or int(node_cfg.get("ollama_port", 11500))
        for i, target in enumerate(sorted({s["target"] for s in specs if s["local"]})):
            secret = secrets.token_urlsafe(24)
            gate = [sys.executable, str(Path(__file__).with_name("ollama_proxy.py")), "--port", str(gate_port + i),
                    "--target", target]
            gates.append((gate, {**os.environ, "CF_PROXY_SECRET": secret}))
            for s in specs:
                if s["local"] and s["target"] == target:
                    s["base_url"], s["secret"] = f"http://{host}:{gate_port + i}", secret
    found = [] if args.no_workers else discover_devices()
    held = ollama_reserve(found, [s["size_gb"] for s in specs if s["local"]])
    devices = plan(found, set(policy["licences"]), reserve, [c for c in args.caps.split(",") if c] or None, held)
    print(f"node {socket.gethostname()} -> {fabric} (advertising {host})")
    for i, d in enumerate(devices):
        note = f", {held[d.id]:.1f} GB kept for Ollama" if d.id in held else ""
        print(f"  worker {d.id:7} {d.name} · {d.free_gb:.1f}/{d.total_gb:.1f} GB free{note} -> {', '.join(d.caps)} on :{port + i}")
    for s in specs:
        print(f"  model  {s['model']} ({s['base_url']}{', behind a gate' if s.get('secret') else ''})")
    if args.dry_run:
        return

    procs: list[tuple[subprocess.Popen, list[str], dict]] = []
    for gate, env_ in gates:
        procs.append((subprocess.Popen(gate, env=env_), gate, env_))
    if gates:
        time.sleep(1)
    for s in specs:
        payload = {k: v for k, v in s.items() if k not in ("size_gb", "local", "target")}
        print(f"  joined {s['model']}: {register(fabric, args.token, payload)}")

    env = {**os.environ, "CF_JOIN_TOKEN": args.token,
           "CF_SEGMENT_MODELS": ",".join(allowed_models("segment", set(policy["licences"])))}
    for i, d in enumerate(devices):
        cmd = [sys.executable, "-m", "cinemafabric.worker", "--device", d.id, "--caps", ",".join(d.caps),
               "--host", bind, "--port", str(port + i), "--join", fabric,
               "--advertise", f"http://{host}:{port + i}", "--name", f"{socket.gethostname()} · {d.name}"]
        procs.append((subprocess.Popen(cmd, env=env), cmd, env))
    if not procs:
        return

    stop = False

    def shutdown(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    restarts = [0] * len(procs)
    while not stop:
        time.sleep(2)
        for i, (proc, cmd, env_) in enumerate(procs):
            if proc.poll() is not None and restarts[i] < 3:
                restarts[i] += 1
                print(f"  worker {i} exited ({proc.returncode}); restarting")
                procs[i] = (subprocess.Popen(cmd, env=env_), cmd, env_)
    for proc, _, _ in procs:
        proc.terminate()
    for proc, _, _ in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
