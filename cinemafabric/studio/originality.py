"""Originality checks: ideas that build on someone else's work, and stories that borrow names, titles or lines."""

import re
from typing import Literal

from pydantic import BaseModel

from .prompts import fenced
from .schemas import STR, Shot, Treatment, _obj

IDEA_SCHEMA = {
    "name": "idea_originality",
    "schema": _obj({"original": {"type": "boolean"}, "problems": {"type": "array", "items": STR}, "rewrite": STR}),
}

STORY_SCHEMA = {
    "name": "story_originality",
    "schema": _obj({"issues": {"type": "array", "items": _obj({
        "where": {"type": "string", "enum": ["title", "character", "line", "phrase"]},
        "original": STR,
        "problem": STR,
        "replacement": STR,
    })}}),
}

RULES = """Not allowed, because it belongs to someone else or to a real person:
- an existing franchise, film, series, book, game or comic, or any of its characters, places or catchphrases;
- a real, identifiable person (a celebrity, politician, athlete, influencer or other public figure);
- a real brand, company or product at the centre of the story;
- a named living artist's or studio's style ("in the style of ...");
- song lyrics, or lines quoted from films, books or speeches.

Fine, because nobody owns them: genres, tropes and archetypes (a boy wizard, a masked vigilante, a heist crew), real
places and history told as fiction, myths, folklore and public-domain works (the Mahabharata, Shakespeare, Grimm's
tales), and generic jobs or institutions."""


class IdeaCheck(BaseModel):
    original: bool | None = None
    problems: list[str] = []
    rewrite: str = ""


class StoryIssue(BaseModel):
    where: Literal["title", "character", "line", "phrase"]
    original: str
    problem: str = ""
    replacement: str = ""


class StoryCheck(BaseModel):
    issues: list[StoryIssue] = []


def idea_prompt(idea: str) -> str:
    return f"""You check film ideas for a studio that only makes original work.

{RULES}

Decide whether this idea is original. If it isn't, list each problem in one short, plain sentence, and write an
original version of the same idea (one or two sentences) that keeps its genre, mood and premise but replaces anything
borrowed with something new. If it is original, return original true, no problems and an empty rewrite.

IDEA: {fenced(idea)}"""


def story_prompt(t: Treatment, shots: list[Shot]) -> str:
    lines = "\n".join(f"- {s.narration.strip()}" for s in shots if s.narration.strip()) or "- (none)"
    names = ", ".join(c.name for c in t.characters) or "(none)"
    beats = " ".join([t.story, *t.beats, *(f"{f.name}: {f.look}" for f in [*t.places, *t.props])])
    return f"""You check a new film's story for anything borrowed.

{RULES}

Also flag: a title that is the same as a well-known film, series or book; a character name that is a famous fictional
character's or a celebrity's name (an ordinary name shared by many people is fine); and any other borrowed name,
place, catchphrase or quotation anywhere below.

For each problem return where (title, character, line, or phrase for anything else), original (the exact text as
written below), problem (one short sentence) and replacement (an original title, a new name that fits the same
culture and setting, a new line with the same meaning and length, or an original phrase). Return no issues if
everything is original.

TITLE: {fenced(t.title)}
TAGLINE: {fenced(t.tagline)}
LOGLINE: {fenced(t.logline)}
SETTING: {fenced(t.setting)}
STORY BEATS: {fenced(beats)}
CHARACTERS: {fenced(names)}
VOICE-OVER LINES:
{lines}"""


def apply(t: Treatment, shots: list[Shot], issues: list[StoryIssue], unmatched: list[str] | None = None) -> list[str]:
    """Make the replacements that match the story exactly; returns what changed, in plain words. Issues that match
    nothing are added to `unmatched`."""
    changed: list[str] = []
    for issue in issues:
        before = len(changed)
        _apply_one(t, shots, issue, changed)
        if len(changed) == before and unmatched is not None and issue.original.strip():
            unmatched.append(issue.original.strip())
    return changed


def _apply_one(t: Treatment, shots: list[Shot], issue: StoryIssue, changed: list[str]) -> None:
    old, new = issue.original.strip(), " ".join(issue.replacement.split())
    if not old or not new or old.lower() == new.lower():
        return
    if issue.where == "title" and t.title.strip().lower() == old.lower():
        t.title = new[:60]
        changed.append(f'title "{old}" is now "{t.title}"')
    elif issue.where == "character":
        match = next((c for c in t.characters if c.name.strip().lower() == old.lower()), None)
        if match is None:
            return
        pattern = re.compile(rf"\b{re.escape(match.name)}\b", re.IGNORECASE)
        for c in [*t.characters, *t.places, *t.props]:
            c.look = pattern.sub(new, c.look)
        match.name = new
        t.logline, t.tagline, t.setting, t.story = (pattern.sub(new, x) for x in (t.logline, t.tagline, t.setting, t.story))
        t.beats = [pattern.sub(new, b) for b in t.beats]
        for s in shots:
            s.visual = pattern.sub(new, s.visual)
            s.narration = pattern.sub(new, s.narration)
            s.characters = [new if pattern.fullmatch(n) else n for n in s.characters]
            if pattern.fullmatch(s.speaker or ""):
                s.speaker = new
        changed.append(f'character "{old}" is now "{new}"')
    elif issue.where == "line":
        hits = [s for s in shots if s.narration.strip().lower() == old.lower()]
        for s in hits:
            s.narration = new
        if hits:
            changed.append(f'line "{old}" was rewritten' + (f" ({len(hits)} times)" if len(hits) > 1 else ""))
    elif issue.where == "phrase":
        pattern = re.compile(re.escape(old), re.IGNORECASE)
        fixed = [*t.places, *t.props]
        fields = [t.title, t.tagline, t.logline, t.setting, t.story, *t.beats, *(s.visual for s in shots),
                  *(s.narration for s in shots), *(c.look for c in t.characters), *(f"{f.name} {f.look}" for f in fixed)]
        if not any(pattern.search(x) for x in fields):
            return
        t.title, t.tagline, t.logline, t.setting, t.story = (
            pattern.sub(new, x) for x in (t.title, t.tagline, t.logline, t.setting, t.story))
        t.beats = [pattern.sub(new, b) for b in t.beats]
        for s in shots:
            s.visual, s.narration = pattern.sub(new, s.visual), pattern.sub(new, s.narration)
        for c in t.characters:
            c.look = pattern.sub(new, c.look)
        for f in fixed:
            f.name, f.look = pattern.sub(new, f.name), pattern.sub(new, f.look)
        changed.append(f'"{old}" is now "{new}"')
