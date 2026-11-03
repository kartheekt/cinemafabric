"""Director's commentary: a second voice over the finished film about the choices behind it and where each shot was made."""

import re

from pydantic import BaseModel

from .prompts import house_style
from .schemas import STR, Shot, Treatment, _obj

WORDS_PER_SECOND = 2.4
GAP_S = 7.0
MAX_LINES = 16
MAX_WORDS = 30
PLACES = {r"\bmac\b": "mac", r"\bcloud\b|\bapi\b|\bmeta model": "meta model api",
          r"\bgpu\b|\brtx\b|\bnvidia\b|\bgeforce\b": "nvidia", r"\blocal\b|\bin-house\b|\bour own\b": "local"}

SCHEMA = {
    "name": "commentary",
    "schema": _obj({"lines": {"type": "array", "items": _obj({"shot": {"type": "integer"}, "text": STR})}}),
}


class Line(BaseModel):
    shot: int
    text: str


class Commentary(BaseModel):
    lines: list[Line] = []


def shot_facts(shots: list[Shot], decisions: list[dict]) -> list[str]:
    """What the routing log says about each shot's picture: model, machine, time, cost."""
    frames: dict[int, dict] = {}
    for d in decisions:
        m = re.match(r"Frame (\d+)", d.get("label") or "")
        if m and d.get("status") == "ok":
            n = int(m.group(1))
            if n not in frames or (d.get("ts") or 0) > (frames[n].get("ts") or 0):
                frames[n] = d
    facts = []
    for n, s in enumerate(shots, 1):
        if s.title_card:
            facts.append("title card, drawn in the studio")
            continue
        d = frames.get(n)
        if not d:
            facts.append("no routing record")
            continue
        host, _, device = (d.get("worker_label") or "a local machine").partition(" · ")
        where = "the Meta Model API" if d.get("is_cloud") else f"local machine {host}" + (f" ({device})" if device else "")
        how = "reused from an earlier take" if d.get("cached") else f"{(d.get('latency_ms') or 0) / 1000:.0f} s"
        cost = f"${d['cost_usd']:.3f}" if d.get("cost_usd") else "free"
        facts.append(f"{d.get('model') or 'unknown model'} on {where}, {how}, {cost}")
    return facts


def anchors(shots: list[Shot]) -> dict[int, int]:
    """The shots to talk over, about GAP_S apart, each with a word limit that ends before the next one starts."""
    starts, t = [], 0.0
    for s in shots:
        starts.append(t)
        t += s.duration
    picked: list[int] = []
    for n, (s, start) in enumerate(zip(shots, starts, strict=False), 1):
        if not s.title_card and (not picked or start >= starts[picked[-1] - 1] + GAP_S):
            picked.append(n)
    picked = picked[:MAX_LINES]
    ends = [starts[n - 1] for n in picked[1:]] + [t]
    return {n: min(MAX_WORDS, max(6, int((end - starts[n - 1] - 0.6) * WORDS_PER_SECOND))) for n, end in zip(picked, ends, strict=False)}


def prompt(t: Treatment, shots: list[Shot], starts: list[float], facts: list[str], receipt: dict) -> str:
    rows = []
    for n, (s, start, fact) in enumerate(zip(shots, starts, facts, strict=False), 1):
        what = f"[title card] {s.title_card}" if s.title_card else s.visual
        vo = f' | voice-over: "{s.narration}"' if s.narration else ""
        rows.append(f"{n}. at {start:.0f}s, {s.duration:.1f}s long: {what}{vo} | made with: {fact}")
    local, cloud = receipt.get("local_tasks") or 0, receipt.get("cloud_tasks") or 0
    slots = ", ".join(f"shot {n} (up to {cap} words)" for n, cap in anchors(shots).items())
    return f"""You directed this film and are recording the commentary track that plays over it. Talk like a director
on a DVD commentary: first person, relaxed, specific. Say why a shot is framed the way it is, what you wanted the
audience to feel, what you'd change. Once or twice, mention how a shot was actually made, using only the facts in
"made with" for that same shot, in your own words rather than read out like a log. Never invent facts about the
production, and never say a shot was made somewhere its "made with" doesn't say.

Rules:
- Say exactly one line for each of these shots, within its word limit, since each line plays over that shot and the
  ones after it: {slots}. A line can talk about any of the shots it plays over. Short sentences.
- Don't read the voice-over or describe what's plainly on screen.
- The whole film was made by {local} tasks on local machines and {cloud} on the cloud, for ${receipt.get("cost_usd") or 0:.2f}.

{house_style()}

FILM: "{t.title}" ({t.logline})
SHOTS:
{chr(10).join(rows)}

Return lines as {{"shot": number from 1, "text": what you say}}."""


def grounded(text: str, fact: str) -> str:
    """Drop sentences that say a shot was made somewhere its routing record doesn't."""
    fact = fact.lower()
    keep = [s for s in re.split(r"(?<=[.!?])\s+", text)
            if all(where in fact for pattern, where in PLACES.items() if re.search(pattern, s.lower()))]
    return " ".join(keep)


def made_note(fact: str) -> str:
    """A spoken sentence built straight from a shot's routing fact, so it can't be wrong."""
    m = re.match(r"(.+?) on (the Meta Model API|local machine ([^,(]+?)(?: \((.+)\))?), (\d+) s, (free|\$[\d.]+)$", fact)
    if not m:
        return ""
    seconds = int(m.group(5))
    took = f"in about {seconds} seconds" if seconds else "in no time"
    if m.group(2) == "the Meta Model API":
        return f"That picture came back from Meta's cloud {took}."
    on = f"the {m.group(4)} in {m.group(3)}" if m.group(4) else m.group(3)
    return f"{on[0].upper()}{on[1:]} drew that one {took}, at no cost."


def validate(c: Commentary, shots: list[Shot], facts: list[str] | None = None) -> list[Line]:
    """One line per anchor shot, trimmed to fit before the next; claims about machines must match the routing log."""
    caps, kept = anchors(shots), {}
    for line in c.lines:
        text = " ".join(line.text.split())
        if line.shot not in caps or not text or line.shot in kept:
            continue
        if facts:
            nxt = min([n for n in caps if n > line.shot], default=len(shots) + 1)
            text = grounded(text, " ".join(facts[line.shot - 1:nxt - 1]))
        if text:
            kept[line.shot] = Line(shot=line.shot, text=trim(text, caps[line.shot]))
    if facts and kept and not any(re.search(p, line.text.lower()) for line in kept.values() for p in PLACES):
        add_note(kept, caps, facts, len(shots))
    return [kept[k] for k in sorted(kept)]


def add_note(kept: dict[int, Line], caps: dict[int, int], facts: list[str], count: int) -> None:
    """Say how one shot was made: in a slot with no line if there is one, else ahead of the last line."""
    for n in sorted([n for n in caps if n not in kept], reverse=True) + [max(kept)]:
        nxt = min([k for k in caps if k > n], default=count + 1)
        note = next((made_note(f) for f in facts[n - 1:nxt - 1] if made_note(f)), "")
        if note:
            kept[n] = Line(shot=n, text=trim(f"{note} {kept[n].text}" if n in kept else note, caps[n]))
            return


def trim(text: str, cap: int = MAX_WORDS) -> str:
    """Whole sentences up to the word cap. A first sentence a little over is kept; a long one ends at a comma."""
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if out and len(" ".join(out + [sentence]).split()) > cap:
            break
        out.append(sentence)
    words = " ".join(out).split()
    if len(words) <= cap * 1.3:
        return " ".join(words)
    head = " ".join(words[:cap])
    if "," in head:
        return head.rsplit(",", 1)[0] + "."
    return head.rstrip(",;:") + "…"
