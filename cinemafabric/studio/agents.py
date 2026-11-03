"""The studio's agent crew. Each agent looks at one thing, may change it only through a few fixed operations within
limits, and leaves a record of what it saw, decided and changed (see `Production.agent_log`)."""

import re
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel

from .options import CAMERAS
from .prompts import house_style
from .schemas import STR, Shot, Treatment, _obj

MAX_DOCTOR_EDITS = 6
MAX_CONTINUITY_REDRAWS = 3
CONTINUITY_SHEET = 11
VOICED_MAX_S = 8.0
SCREENING_PASS = 8
POLISH_BUDGET_USD = 0.06

INT = {"type": "integer"}
NUM = {"type": "number"}

DOCTOR_SCHEMA = {
    "name": "script_doctor",
    "schema": _obj({"score": INT, "notes": {"type": "array", "items": STR}, "edits": {"type": "array", "items": _obj({
        "op": {"type": "string", "enum": ["narration", "visual", "duration", "camera"]},
        "shot": INT, "text": STR, "seconds": NUM,
    })}}),
}
STORY_EDITOR_SCHEMA = {
    "name": "story_editor",
    "schema": _obj({"notes": STR, "lines": {"type": "array", "items": _obj({"shot": INT, "text": STR})}}),
}
EDITOR_SCHEMA = {
    "name": "editor",
    "schema": _obj({"notes": STR, "edits": {"type": "array", "items": _obj({"shot": INT, "seconds": NUM, "camera": STR})}}),
}
CONTINUITY_SCHEMA = {
    "name": "continuity",
    "schema": _obj({"mismatched": {"type": "array", "items": _obj({"shot": INT, "problem": STR})}}),
}
TRANSLATION_REVIEW_SCHEMA = {
    "name": "translation_review",
    "schema": _obj({"lines": {"type": "array", "items": _obj({"n": INT, "faithful": {"type": "boolean"}, "fix": STR})}}),
}
SCREENING_SCHEMA = {
    "name": "test_screening",
    "schema": _obj({"score": INT, "works": STR, "problem": STR, "note": STR}),
}
RELEASE_SCHEMA = {
    "name": "release",
    "schema": _obj({"youtube_title": STR, "youtube_description": STR, "hashtags": {"type": "array", "items": STR},
                    "instagram_caption": STR, "thumbnail_shot": INT}),
}


class DoctorEdit(BaseModel):
    op: Literal["narration", "visual", "duration", "camera"]
    shot: int
    text: str = ""
    seconds: float = 0.0


class DoctorReport(BaseModel):
    score: int = 7
    notes: list[str] = []
    edits: list[DoctorEdit] = []


class StoryLine(BaseModel):
    shot: int
    text: str = ""


class StoryEdit(BaseModel):
    notes: str = ""
    lines: list[StoryLine] = []


class EditorEdit(BaseModel):
    shot: int
    seconds: float = 0.0
    camera: str = ""


class EditorPlan(BaseModel):
    notes: str = ""
    edits: list[EditorEdit] = []


class Mismatch(BaseModel):
    shot: int
    problem: str = ""


class ContinuityReport(BaseModel):
    mismatched: list[Mismatch] = []


class LineReview(BaseModel):
    n: int
    faithful: bool = True
    fix: str = ""


class TranslationReview(BaseModel):
    lines: list[LineReview] = []


class Screening(BaseModel):
    score: int = 7
    works: str = ""
    problem: str = ""
    note: str = ""


class Release(BaseModel):
    youtube_title: str = ""
    youtube_description: str = ""
    hashtags: list[str] = []
    instagram_caption: str = ""
    thumbnail_shot: int = 1


QUOTES = "\"'“”‘’«»"
SPOKEN_ONLY = "Voice-over text is only the words spoken: no speaker names, labels or quotation marks."


def split_speaker(text: str, names: list[str]) -> tuple[str, str]:
    """('Mina', 'I can't see you.') from 'Mina: "I can't see you."', so a narrator never reads a name aloud."""
    text = " ".join(text.split())
    speaker = ""
    m = re.match(r"^([^:]{1,40}):\s*(.+)$", text)
    if m and m.group(1).strip().lower() in {n.lower() for n in names} | {"narrator", "voice-over", "vo"}:
        speaker, text = m.group(1).strip(), m.group(2)
    return speaker, text.strip().strip(QUOTES).strip()


def speaker_fits(speaker: str, line: str) -> bool:
    """A character doesn't talk about themselves by name; a line like that belongs to the narrator."""
    first = speaker.split()[0] if speaker.strip() else ""
    return not first or first.lower() == "narrator" or not re.search(rf"\b{re.escape(first)}\b", line, re.IGNORECASE)


def shot_list(shots: list[Shot], voice: dict[int, float] | None = None) -> str:
    rows = []
    for n, s in enumerate(shots, 1):
        if s.title_card:
            rows.append(f"{n}. [title card] {s.title_card} ({s.duration:.1f}s)")
            continue
        said = f" ({s.speaker})" if s.speaker else ""
        vo = f' | voice-over{said}: "{s.narration}"' if s.narration else ""
        spoken = f" (spoken in {voice[n - 1]:.1f}s)" if voice and n - 1 in voice else ""
        rows.append(f"{n}. {s.visual} | {s.camera.replace('_', ' ')}, {s.duration:.1f}s{vo}{spoken}")
    return "\n".join(rows)


def doctor_limit(shots: list[Shot]) -> int:
    return max(MAX_DOCTOR_EDITS, len(shots) // 2)


def first_viewer(t: Treatment, kind: str) -> str:
    """A short film has to make sense to someone who knows only what's on screen."""
    if kind == "trailer":
        return ""
    return f"""
Judge it as someone who has read nothing else and sees only the lines and pictures. Could they say who this is about,
where they are, what they want, what happens in each of these beats, and how it ends? {t.beats}
Then read the voice-over lines in order, with nothing else. They must flow as one continuous telling, each leading to
the next, with no unexplained jump in time, place or thought, and no sudden change of who's speaking without a setup.
If any of that fails, or a line is a riddle, a fragment or an unexplained reference, score it 6 at most and rewrite
those lines as plain statements that say what's happening and why, and connect them to the lines around them."""


def doctor_prompt(t: Treatment, shots: list[Shot], kind: str) -> str:
    return f"""You are a script doctor on a {kind}. Score this script from 1 to 10 for: a hook in the first shots, a
clear story a viewer can follow, voice-over that's specific and sounds like a person, shots that are concrete and
different from each other, and an ending that lands.{first_viewer(t, kind)}
Then propose at most {doctor_limit(shots)} edits that would raise
the score most, using only these operations on existing shots (numbered from 1):
- narration: replace a shot's voice-over line (under 20 words; "" removes it). {SPOKEN_ONLY} The shot list shows who
  says each line, and they keep saying it, so a character's line must stay their own words. To give a line to the
  narrator instead, start the text with "narrator:".
- visual: replace what a shot shows with one concrete sentence (same characters unless the story needs otherwise)
- duration: set a shot's length in seconds (1.5 to 6)
- camera: one of {", ".join(CAMERAS)}
Title cards can only change duration. If the script already works, return few or no edits. notes: up to three short
sentences on what you found.

{house_style()}

FILM: "{t.title}" ({t.logline})
SHOTS:
{shot_list(shots)}"""


def story_editor_prompt(t: Treatment, shots: list[Shot]) -> str:
    return f"""You are the finest story editor in film, doing the final pass on a short film's voice-over. Rewrite the lines
below as one piece, so that hearing them in order is gripping from the first line to the last.

Keep every fact, name and event, and the order they happen in, and keep each line true to its own picture. Someone new
must be able to follow all of it: who, where, what they want and why it matters.

Make it told, not summarised:
- The first line is a hook that makes the viewer need the next one.
- Every line leaves a question or a pull that the next line answers or sharpens. Connect them; never just list events.
- Vary the rhythm and the openings. No two lines in a row start with the same word, and don't keep starting with "She",
  "He" or a name. Mix long lines that build with short ones that land.
- Feeling comes through specific things people do, see and lose, not through naming the emotion.
- Don't describe what people look like; the picture shows that.
- Build the tension to the turn, and make the last line the one the viewer takes home.
- A character's own lines stay in their own words; the shot list shows who says each one.

Each line is one sentence of 6 to 20 words, spoken aloud in about the length of its shot. Return one entry for every
numbered line you change, with its shot number and the new words only. notes: one sentence on what you changed.

{house_style()}

FILM: "{t.title}" ({t.logline})
THE STORY: {t.story}
SHOTS:
{shot_list(shots)}"""


def apply_story_edit(shots: list[Shot], lines: list[StoryLine]) -> list[str]:
    changed: list[str] = []
    for line in lines:
        if not 1 <= line.shot <= len(shots):
            continue
        s = shots[line.shot - 1]
        _, text = split_speaker(line.text, [])
        if s.title_card or not text or text == s.narration or not 3 <= len(text.split()) <= 22:
            continue
        if not speaker_fits(s.speaker, text):
            continue
        s.narration = text
        changed.append(f'shot {line.shot}: "{text}"')
    return changed


def apply_doctor(shots: list[Shot], edits: list[DoctorEdit], names: list[str] | None = None) -> list[str]:
    changed: list[str] = []
    for e in edits[:doctor_limit(shots)]:
        if not 1 <= e.shot <= len(shots):
            continue
        s = shots[e.shot - 1]
        text = " ".join(e.text.split())
        if e.op == "duration":
            seconds = round(min(max(e.seconds, 1.2 if s.title_card else 1.5), 2.2 if s.title_card else 6.0), 1)
            if abs(seconds - s.duration) >= 0.2:
                changed.append(f"shot {e.shot}: {s.duration:.1f}s → {seconds:.1f}s")
                s.duration = seconds
        elif s.title_card:
            continue
        elif e.op == "narration":
            speaker, text = split_speaker(text, names or [])
            if text == s.narration or len(text.split()) > 22:
                continue
            changed.append(f'shot {e.shot}: line → "{text}"' if text else f"shot {e.shot}: line removed")
            s.narration = text
            if speaker:
                s.speaker = "narrator" if speaker.lower() in ("narrator", "voice-over", "vo") else speaker
        elif e.op == "visual" and text and text != s.visual:
            changed.append(f"shot {e.shot}: now shows {text}")
            s.visual = text[:400]
        elif e.op == "camera" and text in CAMERAS and text != s.camera:
            changed.append(f"shot {e.shot}: camera {s.camera.replace('_', ' ')} → {text.replace('_', ' ')}")
            s.camera = text
    return changed


def editor_prompt(shots: list[Shot], voice: dict[int, float], target: float) -> str:
    return f"""You are the film's editor. The voice-over is recorded, so you know how long each line really takes.
Set each shot's length and camera move for rhythm: every voiced shot must last at least as long as its line plus
half a second; vary the camera moves so the same move rarely runs twice in a row; let the pace quicken toward the
climax and breathe at the end. The whole film should stay near {target:.0f} seconds.
Camera moves: {", ".join(CAMERAS)}. Return edits only for shots you change (shot numbered from 1, seconds 1.5 to 6,
camera or ""), and notes: one sentence on your plan.

SHOTS:
{shot_list(shots, voice)}"""


def apply_editor(shots: list[Shot], edits: list[EditorEdit], voice: dict[int, float], target: float) -> list[str]:
    """The editor's changes, then a guarantee that every line fits and the film stays near its length."""
    changed: list[str] = []
    for e in edits:
        if not 1 <= e.shot <= len(shots) or shots[e.shot - 1].title_card:
            continue
        s = shots[e.shot - 1]
        if e.camera in CAMERAS and e.camera != s.camera:
            changed.append(f"shot {e.shot}: camera {s.camera.replace('_', ' ')} → {e.camera.replace('_', ' ')}")
            s.camera = e.camera
        if e.seconds:
            seconds = round(min(max(e.seconds, 1.5), 6.0), 1)
            if abs(seconds - s.duration) >= 0.2:
                changed.append(f"shot {e.shot}: {s.duration:.1f}s → {seconds:.1f}s")
                s.duration = seconds
    floor = {i: min(VOICED_MAX_S, voice[i] + 0.5) for i in voice}
    for i, s in enumerate(shots):
        if i in floor and s.duration < floor[i]:
            s.duration = round(floor[i], 1)
    pictures = [i for i, s in enumerate(shots) if not s.title_card]
    total = sum(s.duration for s in shots)
    if pictures and abs(total - target) > target * 0.1:
        cards = sum(s.duration for s in shots if s.title_card)
        factor = (target - cards) / max(0.1, sum(shots[i].duration for i in pictures))
        for i in pictures:
            shots[i].duration = round(min(VOICED_MAX_S if i in floor else 6.0, max(floor.get(i, 1.5), shots[i].duration * factor)), 1)
    return changed


def contact_sheet(tiles: list[tuple[str, Path]], width: int = 320, columns: int = 4) -> Image.Image:
    """Numbered thumbnails in one image, so a vision model can compare them in a single look."""
    from .media import typography

    thumbs = []
    for label, path in tiles:
        img = Image.open(path).convert("RGB")
        img.thumbnail((width, width))
        thumbs.append((label, img))
    cell_h = max(img.height for _, img in thumbs) + 34
    rows = (len(thumbs) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * (width + 8) + 8, rows * (cell_h + 8) + 8), (18, 18, 22))
    draw = ImageDraw.Draw(sheet)
    font = typography.font("sans_bold", 22)
    for k, (label, img) in enumerate(thumbs):
        x, y = 8 + (k % columns) * (width + 8), 8 + (k // columns) * (cell_h + 8)
        sheet.paste(img, (x, y + 30))
        draw.text((x + 2, y + 2), label, fill=(255, 220, 120), font=font)
    return sheet


def continuity_prompt(name: str, look: str, shots: list[int]) -> str:
    return f"""You are the continuity supervisor. The tile marked REF is the reference sheet for {name}: {look}.
The other tiles are shots {", ".join(map(str, shots))}, each marked with its shot number. For each shot where the
person who is clearly meant to be {name} looks like a different person (face, hair, age, build or main clothes don't
match the reference), return the shot number and the problem in a few words. Ignore lighting, pose, angle, distance,
small accessories, and shots where {name} isn't clearly visible. Return an empty list if every shot matches."""


def translation_review_prompt(language: str, source: list[str], translated: list[str]) -> str:
    pairs = "\n".join(f"{n}. EN: {a}\n   {language.upper()}: {b}" for n, (a, b) in enumerate(zip(source, translated, strict=False), 1))
    return f"""You review film subtitles translated from English into {language}. For each numbered line, say whether
the {language} keeps the meaning, tone and names of the English (faithful). For a line that doesn't, give a corrected
{language} line in fix, written in {language}'s own script; otherwise leave fix empty. Small wording choices are fine.

{pairs}"""


def screening_prompt(t: Treatment, shots: list[Shot], heard: float | None, seconds: float) -> str:
    clarity = (f"A speech check heard {heard:.0%} of the voice-over's words back correctly, so judge the lines as written "
               "in the script." if heard is not None else "Judge the voice-over lines as written in the script.")
    return f"""You are a test-screening audience for a {seconds:.0f}-second film. The picture shows its frames in order,
numbered by shot. Below is the script with its voice-over lines. {clarity}

Score the film from 1 to 10 as a viewer would: does it hook you, can you follow it, do the pictures and words belong
together, does the ending land? Then: works (one sentence on what works best), problem (one sentence on the biggest
problem, "" if none), and note: one concrete instruction for the director to fix that problem with small changes to
specific shots (a line, a picture, timing, the tagline or the music), or "" if the film is good as it is.
{SPOKEN_ONLY}

FILM: "{t.title}" ({t.logline})
SHOTS:
{shot_list(shots)}"""


def release_prompt(t: Treatment, runtime_label: str, genre: str, language: str, shots: int, made_with: str) -> str:
    return f"""You write the release copy for a short AI-made film. Plain, specific, no hype words like "epic" or
"masterpiece", and no claims you can't back up. Mention that it was made with AI. Describe how it was made using only
MADE WITH below; never say a person wrote, voiced, filmed or edited it, and don't mention models that aren't listed.
Return: youtube_title (under 70 characters), youtube_description (3 short paragraphs: the story without spoilers,
how it was made with CinemaFabric on local machines and Meta's models, and a credit line), hashtags (5 to 8, no #
sign), instagram_caption (under 200 characters), and thumbnail_shot: the shot number (1 to {shots}) that would make
the most striking thumbnail without giving the ending away.

FILM: "{t.title}" · {runtime_label} · {genre} · voice-over in {language}
LOGLINE: {t.logline}
TAGLINE: {t.tagline}
MADE WITH: {made_with}"""


def release_markdown(r: Release, title: str) -> str:
    tags = " ".join("#" + "".join(ch for ch in h if ch.isalnum()) for h in r.hashtags if h.strip())
    return "\n".join([
        f"# Release copy: {title}", "", "## YouTube", "", f"**Title:** {r.youtube_title}", "", r.youtube_description, "",
        f"**Tags:** {tags}", "", "## Instagram", "", r.instagram_caption, "", tags, "",
        "*Written by the release agent. Check it before you post.*", "",
    ])


