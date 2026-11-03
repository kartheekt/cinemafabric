"""Director notes: turn a note about a finished film into a short, checked list of edits."""

from typing import Literal

from pydantic import BaseModel

from ..fabric import pricing
from .prompts import fenced, house_style
from .schemas import STR, Shot, Treatment, _obj

MAX_EDITS = 8
OPS = ("narration", "visual", "redraw", "duration", "title", "tagline", "music")

PLAN_SCHEMA = {
    "name": "director_plan",
    "schema": _obj({
        "reply": STR,
        "edits": {"type": "array", "items": _obj({
            "op": {"type": "string", "enum": list(OPS)},
            "shot": {"type": "integer"},
            "text": STR,
            "seconds": {"type": "number"},
        })},
    }),
}


class Edit(BaseModel):
    op: Literal["narration", "visual", "redraw", "duration", "title", "tagline", "music"]
    shot: int = 0
    text: str = ""
    seconds: float = 0.0

    def describe(self) -> str:
        where = f"Shot {self.shot}: " if self.shot else ""
        return {
            "narration": f'{where}voice-over → "{self.text}"' if self.text else f"{where}remove the voice-over",
            "visual": f"{where}new picture: {self.text}",
            "redraw": f"{where}redraw ({self.text})",
            "duration": f"{where}{self.seconds:.1f} s",
            "title": f'Title → "{self.text}"',
            "tagline": f'Tagline → "{self.text}"',
            "music": f"Music → {self.text}",
        }[self.op]


class Plan(BaseModel):
    reply: str = ""
    edits: list[Edit] = []


def prompt(t: Treatment, shots: list[Shot], note: str, history: list[dict]) -> str:
    rows = []
    for n, s in enumerate(shots, 1):
        if s.title_card:
            rows.append(f"{n}. [title card] {s.title_card} ({s.duration:.1f}s)")
        else:
            vo = f' | voice-over: "{s.narration}"' if s.narration else ""
            rows.append(f"{n}. {s.visual} ({s.duration:.1f}s, {s.camera.replace('_', ' ')}){vo}")
    earlier = "\n".join(f"- note: {h['note']} -> {h['reply']}" for h in history[-4:]) or "none"
    return f"""You are the film's director making changes to a finished cut. Plan the smallest set of edits that does
what the note asks, using only these operations:

- narration: replace the voice-over on a shot (text; "" removes it). Keep lines under 20 words. The text is only
  the words spoken: no speaker names, labels or quotation marks.
- visual: new description of what a shot shows; its picture is redrawn (text).
- redraw: redraw a shot's picture with the same description and a short fix note (text).
- duration: set a shot's length in seconds (1.5 to 6).
- title / tagline: change the film's title or tagline (text; shot 0).
- music: describe a new score mood, instruments and tempo (text; shot 0).

Shots are numbered from 1. Title cards can only change duration. Use at most {MAX_EDITS} edits. If the note asks for
something these operations can't do, say so in reply and make no edits. reply is one or two plain sentences telling
the user what you'll change.

{house_style()}

FILM: "{t.title}" — {t.logline}
TAGLINE: {t.tagline}
MUSIC: {t.music_mood}
SHOTS:
{chr(10).join(rows)}

EARLIER NOTES: {earlier}

NOTE FROM THE USER: {fenced(note)}"""


def ending_range(shots: list[Shot]) -> tuple[int, int]:
    """The shots an alternate ending may change: roughly the last third, at least two."""
    n = len(shots)
    return max(1, n - max(2, round(n * 0.3)) + 1), n


def ending_note(idea: str, first: int, last: int) -> str:
    want = idea.strip() or "a different ending that still follows from everything before it"
    return (f"Give this film a different ending: {want}. Change only shots {first} to {last}; every shot before them "
            "stays exactly as it is. Rewrite the pictures and voice-over of those shots so the new ending lands, and "
            "change the tagline if it no longer fits.")


def validate(plan: Plan, shots: list[Shot]) -> list[Edit]:
    """Keep only edits that make sense for this film, clamped to safe ranges."""
    kept: list[Edit] = []
    seen: set[tuple[str, int]] = set()
    for e in plan.edits:
        film_wide = e.op in ("title", "tagline", "music")
        if film_wide:
            e.shot = 0
        elif not 1 <= e.shot <= len(shots):
            continue
        elif shots[e.shot - 1].title_card and e.op != "duration":
            continue
        if (e.op, e.shot) in seen:
            continue
        e.text = " ".join(e.text.split())[: 60 if e.op in ("title", "tagline") else 400]
        if e.op in ("visual", "title", "music") and not e.text:
            continue
        if e.op == "duration":
            e.seconds, e.text = round(min(max(e.seconds, 1.5), 6.0), 1), ""
        if e.shot and e.op in ("narration", "visual"):
            current = getattr(shots[e.shot - 1], e.op)
            if e.text.strip(" \"'“”").lower() == " ".join(current.split()).strip(" \"'“”").lower():
                continue
        seen.add((e.op, e.shot))
        kept.append(e)
    return kept[:MAX_EDITS]


def estimate(edits: list[Edit]) -> float:
    """Cloud cost if every redrawn picture goes to the image API; narration and music run locally when they can."""
    return round(sum(pricing.IMAGE_PRICE for e in edits if e.op in ("visual", "redraw")), 4)


def checked_text(edits: list[Edit]) -> str:
    return "\n".join(e.text for e in edits if e.text)
