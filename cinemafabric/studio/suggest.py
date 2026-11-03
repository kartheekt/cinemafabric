"""Suggestions in the form, written by a model through the fabric (draft tier, local first)."""

import datetime as dt
import re
import secrets

from pydantic import BaseModel

from ..fabric.client import FabricClient
from ..fabric.types import Preference, TaskKind, Tier
from .options import ASPECTS, FORMATS, GENRES, STYLES
from .prompts import WRITER_SYSTEM, fenced, house_style
from .schemas import STR, _obj

IDEAS_SCHEMA = {"name": "ideas", "schema": _obj({"ideas": {"type": "array", "items": STR}})}
POLISH_SCHEMA = {"name": "polish", "schema": _obj({"idea": STR})}
SETTINGS_SCHEMA = {
    "name": "settings",
    "schema": _obj({
        "format": {"type": "string", "enum": list(FORMATS)},
        "seconds": {"type": "integer"},
        "genre": {"type": "string", "enum": GENRES},
        "style": {"type": "string", "enum": list(STYLES)},
        "aspect": {"type": "string", "enum": list(ASPECTS)},
        "reason": STR,
    }),
}


class Settings(BaseModel):
    format: str
    seconds: int
    genre: str
    style: str
    aspect: str
    reason: str = ""


async def _ask(fabric: FabricClient, schema: dict, prompt: str, label: str, temperature: float, cache: bool = True):
    res = await fabric.run(
        TaskKind.TEXT,
        {"system": WRITER_SYSTEM, "prompt": prompt, "json_schema": schema, "max_tokens": 600, "temperature": temperature},
        tier=Tier.DRAFT, preference=Preference.LOCAL_FIRST, stage="suggest", label=label, cache=cache,
    )
    return res.output.get("json") or {}, f"{res.decision.model} on {res.decision.worker_label}"


async def ideas(fabric: FabricClient, fresh: bool = False) -> dict:
    seed = secrets.token_hex(4) if fresh else dt.date.today().isoformat()
    prompt = f"""Pitch 4 original ideas for a short film or trailer, one sentence each (under 20 words). Mix genres and
places; at least one set in India. Each needs a specific person, place and a twist. No sequels, no real people,
no existing films or characters. Write only the idea itself: no title, no "Trailer:" or "Short film:" label.
Variation key: {seed}.

{house_style()}"""
    data, by = await _ask(fabric, IDEAS_SCHEMA, prompt, "Idea suggestions", 1.0, cache=not fresh)
    found = [_bare(str(i))[:160] for i in data.get("ideas", []) if _bare(str(i))]
    return {"ideas": found[:4], "by": by}


def _bare(idea: str) -> str:
    """Drop the "Trailer: 'Title' -" scaffolding small models like to add."""
    idea = " ".join(idea.split())
    idea = re.sub(r"^(short film|trailer|film|movie|idea)\s*\d*\s*[:.-]\s*", "", idea, flags=re.I)
    return re.sub(r"^[\"'‘“][^\"'’”]{1,60}[\"'’”]\s*[-–—:]\s*", "", idea).strip()


async def polish(fabric: FabricClient, idea: str) -> dict:
    prompt = f"""Rewrite this film idea as one or two vivid sentences (under 45 words). Keep the writer's characters, place
and premise; make it specific and add a hook. Return only the idea.

IDEA: {fenced(idea)}

{house_style()}"""
    data, by = await _ask(fabric, POLISH_SCHEMA, prompt, "Polish the idea", 0.7)
    return {"idea": " ".join(str(data.get("idea", "")).split())[:1200], "by": by}


async def settings(fabric: FabricClient, idea: str) -> dict:
    limits = "; ".join(f"{k}: {v['min']}-{v['max']} seconds" for k, v in FORMATS.items())
    styles = ", ".join(f"{k} ({v['label']})" for k, v in STYLES.items())
    prompt = f"""Choose production settings that suit this film idea.
Lengths: {limits}. Styles: {styles}. Aspect: 16:9 for cinema, 2.39:1 for epic scope, 9:16 for phone-first stories,
1:1 for social. reason: one short sentence explaining the choice.

IDEA: {fenced(idea)}"""
    data, by = await _ask(fabric, SETTINGS_SCHEMA, prompt, "Suggest settings", 0.3)
    s = Settings.model_validate(data)
    f = FORMATS.get(s.format) or FORMATS["trailer"]
    return {
        "format": s.format if s.format in FORMATS else "trailer",
        "seconds": min(max(s.seconds, f["min"]), f["max"]),
        "genre": s.genre if s.genre in GENRES else None,
        "style": s.style if s.style in STYLES else None,
        "aspect": s.aspect if s.aspect in ASPECTS else None,
        "reason": " ".join(s.reason.split())[:200],
        "by": by,
    }
