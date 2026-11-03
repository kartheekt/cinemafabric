"""Generate docs/architecture.md with a Meta model, routed through CinemaFabric's own fabric."""

import argparse
import asyncio
import io
import re
from collections import Counter
from pathlib import Path

from PIL import Image

from ..config import get_settings
from ..fabric.client import FabricClient
from ..fabric.machines import fabric_token
from ..fabric.topology import build_backends
from ..fabric.types import Preference, TaskKind, Tier
from ..studio.pipeline import STAGES
from ..util import b64d

DOCS = Path(__file__).resolve().parents[2] / "docs"

SCHEMA = {
    "name": "architecture",
    "schema": {
        "type": "object",
        "properties": {
            "system_diagram": {"type": "string"},
            "pipeline_diagram": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["system_diagram", "pipeline_diagram", "summary"],
        "additionalProperties": False,
    },
}

ROUTING_RULES = [
    "Each task names a kind (text, image, segment, tts, music, transcribe), a quality tier and a compute preference.",
    "Candidates are workers whose offers match the kind, meet the tier, support vision if needed, are healthy, "
    "and (for cloud) fit the remaining budget.",
    "Local-first sorts by (is_cloud, cost, expected seconds); balanced scores cost*100 + seconds*0.02 + cloud nudge; "
    "cloud-first prefers the API.",
    "Expected seconds = measured latency EMA + queue depth; Ollama and GPU workers run one task at a time.",
    "Cloud calls reserve estimated cost in the ledger before running and settle actual cost after; "
    "an optional spending limit refuses reservations that would pass it.",
    "On failure the router falls back to the next candidate; two consecutive failures open a 30 s circuit breaker.",
    "Results are cached by content hash, so retries and replays cost $0.",
    "If nothing meets the tier the router degrades to draft quality (e.g. procedural score) rather than failing.",
]

PROMPT = """You are a software architect drawing diagrams for a public GitHub README.
Draw CinemaFabric from the facts below. Use ONLY these facts; do not invent components.

Return:
- system_diagram: Mermaid `flowchart LR` showing: Browser UI -> Studio service (FastAPI :8000) -> Fabric service
  (FastAPI :8100) with its Router, Budget ledger (SQLite) and Result cache; then every worker below grouped in
  subgraphs by the device or host given in the facts, with cloud workers in their own subgraph. Every worker node label must list the models it serves exactly as written in the facts (for
  example muse-image-1.0<br/>sam-3.1), one per line. Show the studio writing media to disk and ffmpeg.
- pipeline_diagram: Mermaid `flowchart TD` of the production stages in order, showing that the visual track
  (cast -> frames + critic -> poster, depth) and the audio track (voice -> score, QA) run in parallel before
  edit -> package. Label each stage node with its name once, then where it runs, exactly as given.
- summary: 2-3 sentences describing the architecture.

Mermaid rules: node ids are short alphanumerics; put labels in double quotes; use <br/> for line breaks; no
parentheses or special characters outside quotes; no styling directives; no markdown fences.

FACTS
Workers (name, cloud or local, offers kind:model:quality):
{workers}

Pipeline stages in order:
{stages}

Routing rules:
{rules}
"""


def generic_names(workers: list[dict]) -> dict[str, str]:
    hosts: dict[str, str] = {}
    names: dict[str, str] = {}
    for w in workers:
        if w.get("is_cloud"):
            names[w["label"]] = "Meta Model API"
        elif w.get("source") == "builtin":
            names[w["label"]] = "Fabric CPU fallback"
        else:
            host = w["label"].split(" · ")[0]
            node = hosts.setdefault(host, f"Node {chr(ord('A') + len(hosts))}")
            kind = "Ollama" if any(o["kind"] in ("text.generate", "text.moderate") for o in w["offers"]) else "worker"
            names[w["label"]] = f"{node} {kind}"
    return names


def topology(workers: list[dict], names: dict[str, str]) -> str:
    lines = []
    for w in workers:
        offers = ", ".join(f"{o['kind']}:{o['model']}:q{o['quality']}" for o in w["offers"])
        lines.append(f"- {names.get(w['label'], w['label'])} | {'cloud' if w['is_cloud'] else 'local'} | {offers}")
    return "\n".join(lines)


def clean_mermaid(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:mermaid)?\s*|\s*```$", "", text)
    text = text.replace("\\n", "\n").strip()
    return re.sub(r'\b([A-Za-z][\w]*)\s+"([^"\n]*)"', r'\1["\2"]', text)


def validate(diagram: str, kind: str, must_mention: list[str]) -> list[str]:
    problems = []
    bare = re.sub(r'\[\s*"[^"\n]*"\s*\]|\(\s*"[^"\n]*"\s*\)|\{\s*"[^"\n]*"\s*\}|\|\s*"[^"\n]*"\s*\|', "", diagram)
    if '"' in bare:
        problems.append('has a quoted label outside brackets; write nodes as id["label"]')
    if not diagram.startswith(f"flowchart {kind}") and not diagram.startswith(f"graph {kind}"):
        problems.append(f"does not start with 'flowchart {kind}'")
    if diagram.count("-->") + diagram.count("---") + diagram.count("-.->") < 4:
        problems.append("has too few edges")
    normal = diagram.lower().replace("-", " ").replace("_", " ")
    missing = [m for m in must_mention if m.lower().replace("-", " ") not in normal]
    if missing:
        problems.append(f"does not mention: {', '.join(missing)}")
    return problems


async def stage_homes(fabric: FabricClient, names: dict[str, str]) -> dict[str, str]:
    try:
        decisions = (await fabric.get("/v1/decisions", limit=1000))["decisions"]
    except Exception:
        return {}
    counts: dict[str, Counter] = {}
    for d in decisions:
        if d.get("status") == "ok" and not d.get("cached") and d.get("stage") and d.get("worker_label"):
            where = names.get(d["worker_label"], "a local node")
            counts.setdefault(d["stage"], Counter())[f"{d['model']} on {where}"] += 1
    return {stage: c.most_common(1)[0][0] for stage, c in counts.items()}


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    fabric = FabricClient(args.fabric or settings.fabric_url, fabric_token(settings))
    try:
        workers = (await fabric.get("/v1/workers"))["workers"]
    except Exception:
        workers = [b.describe() for b in build_backends(settings)]
    names = generic_names(workers)
    homes = await stage_homes(fabric, names)
    stages = "\n".join(f"{i + 1}. {label} (runs on: {homes.get(sid, 'studio')})" for i, (sid, label) in enumerate(STAGES))
    prompt = PROMPT.format(workers=topology(workers, names), stages=stages, rules="\n".join(f"- {r}" for r in ROUTING_RULES))
    cloud = [o["model"] for w in workers if w.get("is_cloud") for o in w["offers"]]
    must = ["Router", "Ledger"] + cloud[:3]

    feedback, result, res = "", None, None
    for attempt in range(3):
        res = await fabric.run(
            TaskKind.TEXT,
            {"prompt": prompt + feedback, "json_schema": SCHEMA, "max_tokens": 4000, "temperature": 0.3},
            tier=Tier.STANDARD,
            preference=Preference(args.preference),
            stage="docs",
            label=f"Architecture diagram{' (retry)' if attempt else ''}",
            cache=False,
        )
        data = res.output.get("json") or {}
        system = clean_mermaid(data.get("system_diagram", ""))
        pipeline = clean_mermaid(data.get("pipeline_diagram", ""))
        problems = validate(system, "LR", must) + validate(pipeline, "TD", ["frames", "edit"])
        if not problems:
            result = (system, pipeline, data.get("summary", "").strip())
            break
        feedback = "\n\nYour previous diagrams were rejected: " + "; ".join(problems) + ". Fix them."
        print("rejected:", "; ".join(problems))
        print(system[:1200])
    if result is None or res is None:
        raise SystemExit("could not get valid diagrams after 3 attempts")

    system, pipeline, summary = result
    d = res.decision
    DOCS.mkdir(exist_ok=True)
    md = f"""# Architecture

{summary}

## System

```mermaid
{system}
```

## Production pipeline

```mermaid
{pipeline}
```

## Routing rules

{chr(10).join(f"- {r}" for r in ROUTING_RULES)}

---

*These diagrams were generated by `{d.model}` on **{d.worker_label}**, routed through CinemaFabric's own
fabric (cost ${res.cost_usd:.4f}). Regenerate with `python -m cinemafabric.tools.architecture`.*
"""
    (DOCS / "architecture.md").write_text(md, encoding="utf-8", newline="\n")
    print(f"wrote {DOCS / 'architecture.md'} via {d.worker_label} / {d.model} (${res.cost_usd:.4f})")
    print(f"routing: {d.reason}")

    if args.banner:
        art = await fabric.run(
            TaskKind.IMAGE,
            {
                "prompt": "Wide cinematic banner: a glowing film strip unspooling into a web of light threads that connect "
                "a few computers of different sizes and a soft cloud, dark navy background, warm "
                "amber and cyan highlights, elegant, minimal. No text, no letters, no logos.",
                "size": "1792x1024",
                "n": 1,
            },
            preference=Preference.CLOUD_FIRST,
            stage="docs",
            label="README banner",
        )
        img = Image.open(io.BytesIO(b64d(art.output["images"][0]))).convert("RGB")
        w, h = img.size
        img = img.crop((0, int(h * 0.2), w, int(h * 0.2) + int(w / 3.2)))
        img.save(DOCS / "banner.png", optimize=True)
        print(f"wrote {DOCS / 'banner.png'} (${art.cost_usd:.4f})")
    await fabric.aclose()


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fabric", help="fabric URL (default from settings)")
    parser.add_argument("--preference", default="cloud_first", choices=[p.value for p in Preference])
    parser.add_argument("--banner", action="store_true", help="also paint docs/banner.png with Muse Image")
    asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    cli()
