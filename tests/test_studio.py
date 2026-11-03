import shutil

import numpy as np
import pytest
from PIL import Image

from cinemafabric import procedural
from cinemafabric.studio.media import audio, render
from cinemafabric.studio.options import FINAL_CARD_S, JobOptions, runtime
from cinemafabric.studio.pipeline import Production, new_state
from cinemafabric.studio.schemas import Scene, Script, Shot
from cinemafabric.util import extract_json


def production(tmp_path, **kw):
    opts = JobOptions(prompt="A lighthouse keeper hears the ocean answer", **kw)
    return Production(new_state("t1", opts), fabric=None, store=None, data_dir=tmp_path)  # type: ignore[arg-type]


def test_runtime_scales_with_length():
    short, long_ = runtime("trailer", 30), runtime("trailer", 90)
    assert short.label == "30-second trailer" and short.scenes == 1 and short.max_cards == 2
    assert long_.shots > short.shots and long_.max_cards == 3
    movie = runtime("movie", 150)
    assert movie.label == "2:30 movie" and movie.scenes == 5 and movie.lines[0] < movie.lines[1]
    assert runtime("movie", 999).seconds == 300 and runtime("trailer", 1).seconds == 15


def test_legacy_duration_options_still_load():
    opts = JobOptions.model_validate({"prompt": "an old saved job prompt", "duration": "movie120"})
    assert (opts.format, opts.length, opts.target_seconds) == ("movie", "fixed", 120)


def test_fixed_length_out_of_range_is_rejected():
    with pytest.raises(ValueError):
        JobOptions(prompt="too long for a trailer", format="trailer", seconds=200).validate_choices()


def test_auto_length_plans_with_default_until_story_decides(tmp_path):
    p = production(tmp_path, format="movie", length="auto")
    assert p.runtime.seconds == 120
    assert p.state["runtime"]["decided_by"] == "pending"


@pytest.mark.parametrize(("fmt", "seconds"), [("trailer", 30), ("trailer", 75), ("movie", 180)])
def test_normalise_hits_custom_lengths(tmp_path, fmt, seconds):
    p = production(tmp_path, format=fmt, seconds=seconds)
    shots = [Shot(visual=f"shot {i}", duration=4) for i in range(p.runtime.shots)]
    out = p._normalise(Script(scenes=[Scene(shots=shots)]))
    assert sum(s.duration for s in out) == pytest.approx(seconds - FINAL_CARD_S, rel=0.03)


def test_normalise_hits_target_length_and_limits_cards(tmp_path):
    p = production(tmp_path)
    shots = [Shot(visual=f"shot {i}", duration=9) for i in range(9)]
    shots += [Shot(title_card=f"card {i}", duration=5) for i in range(4)]
    shots.append(Shot(visual="", title_card="", duration=3))
    out = p._normalise(Script(scenes=[Scene(shots=shots)]))
    assert sum(1 for s in out if s.title_card) == 2
    assert all(s.title_card.isupper() for s in out if s.title_card)
    assert all(1.5 <= s.duration <= 6.0 for s in out if not s.title_card)
    assert sum(s.duration for s in out) == pytest.approx(30 - FINAL_CARD_S, abs=0.5)


def test_normalise_trims_extra_voice_over_but_keeps_the_last_line(tmp_path):
    p = production(tmp_path, seconds=30)
    shots = [Shot(visual=f"shot {i}", duration=3, narration=f"line {i}") for i in range(9)]
    out = p._normalise(Script(scenes=[Scene(shots=shots)]))
    lines = [s.narration for s in out if s.narration]
    assert len(lines) == p.runtime.lines[1] == 6
    assert lines[:5] == ["line 0", "line 1", "line 2", "line 3", "line 4"] and lines[-1] == "line 8"


def test_script_check_rejects_all_title_cards(tmp_path):
    p = production(tmp_path)
    script = Script(scenes=[Scene(shots=[Shot(title_card="NO", visual="x") for _ in range(10)])])
    with pytest.raises(ValueError):
        p._check_script(script)


def test_extract_json_handles_fences_and_prose():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"b": [1, 2]} thanks') == {"b": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_extract_json_expect_object_rejects_inner_list_from_truncated_reply():
    truncated = '{"title": "Low Tide", "characters": [{"name": "Mara"}, {"name": "Eli"}], "beats": ["one", "tw'
    with pytest.raises(ValueError):
        extract_json(truncated, expect_object=True)
    assert extract_json('{"a": [1]}', expect_object=True) == {"a": [1]}


def test_mix_ducks_music_under_voice():
    sr = 16000
    voice = np.sin(2 * np.pi * 220 * np.arange(sr) / sr).astype(np.float32)
    score = (np.ones(5 * 32000, dtype=np.float32) * 0.5, 32000)
    out = audio.mix(5.0, [(2.0, voice, sr)], score, impacts=[])
    assert len(out) == 5 * audio.OUT_SR
    quiet = np.abs(out[int(1.0 * audio.OUT_SR) : int(1.5 * audio.OUT_SR)]).mean()
    assert quiet > 0
    assert np.abs(out).max() <= 1.0


def test_loop_extends_short_score():
    clip = np.random.default_rng(0).standard_normal(1000).astype(np.float32)
    assert len(audio.loop_to(clip, 3500, 100)) == 3500


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_render_short_film_with_parallax(tmp_path):
    frame = tmp_path / "f.png"
    frame.write_bytes(procedural.placeholder_image("test frame", "640x360"))
    mask = tmp_path / "m.png"
    Image.new("L", (640, 360), 0).save(mask)
    m = Image.open(mask)
    m.paste(255, (240, 80, 400, 360))
    m.save(mask)
    wav = tmp_path / "a.wav"
    from cinemafabric.util import wav_bytes

    wav.write_bytes(wav_bytes(np.zeros(48000 * 3, dtype=np.float32), 48000))
    clips = [
        render.Clip(1.0, "push_in", frame, mask),
        render.Clip(1.0, "pan_left", frame),
        render.Clip(1.0, card_text="THE END"),
    ]
    out = render.render_film(clips, (320, 180), wav, tmp_path / "film.mp4", [(0.2, 0.9, "Hello")], fps=12)
    assert out.exists() and out.stat().st_size > 1000


def test_subject_for_picks_a_nameable_foreground():
    from cinemafabric.studio.pipeline import subject_for

    assert subject_for(Shot(visual="Marty at the wheel", characters=["Marty"])) == "person"
    assert subject_for(Shot(visual="A lone silhouette on the jetty")) == "person"
    assert subject_for(Shot(visual="Wide shot of the lighthouse against storm clouds")) == "lighthouse"
    assert subject_for(Shot(visual="Two boats rocking in the harbour")) == "boat"
    assert subject_for(Shot(visual="Fog rolling over an empty sea at dawn")) is None


def test_depth_bends_the_move_smoothly_so_near_things_grow_more(tmp_path):
    w, h = 320, 180
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[:, :, 2] = 120
    rgb[60:120, 130:190] = (230, 180, 60)
    frame = tmp_path / "f.png"
    Image.fromarray(rgb).save(frame)
    depth = np.full((h, w), 40, dtype=np.uint8)
    depth[60:120, 130:190] = 230
    dpath = tmp_path / "d.png"
    Image.fromarray(depth).save(dpath)

    def near_area(img: Image.Image) -> int:
        a = np.asarray(img, dtype=np.int16)
        return int(((a[..., 0] > 180) & (a[..., 2] < 110)).sum())

    deep = render.ShotRenderer(render.Clip(2.0, "push_in", frame, None, depth=dpath), (w, h))
    flat = render.ShotRenderer(render.Clip(2.0, "push_in", frame, None, depth=None), (w, h))
    assert deep.depth is not None and flat.depth is None
    assert near_area(deep.frame(1.0)) > near_area(flat.frame(1.0)) * 1.05
    steps = [np.asarray(deep.frame(p), dtype=np.float32) for p in np.linspace(0.4, 0.6, 6)]
    assert max(np.abs(b - a).max(axis=2).mean() for a, b in zip(steps, steps[1:], strict=False)) < 4.0


def test_next_episode_prompt_carries_the_last_story():
    from cinemafabric.studio import prompts
    from cinemafabric.studio.options import JobOptions

    opts = JobOptions(prompt="What happens next in Night Ferry")
    prev = {"job": "abc", "title": "Night Ferry", "logline": "Asha wants one last crossing.", "beats": ["She <ignores> the storm."],
            "episode": 2}
    text = prompts.treatment(opts, [{"name": "Asha", "look": "tall, red scarf"}], prev)
    assert "PREVIOUSLY (episode 1)" in text and "This is episode 2" in text and "Asha: tall, red scarf" in text
    assert "<ignores>" not in text and "PREVIOUSLY" not in prompts.treatment(opts)


def test_alternate_ending_only_touches_the_last_stretch():
    from cinemafabric.studio import director
    from cinemafabric.studio.schemas import Shot

    shots = [Shot(visual=f"shot {i}", camera="static", duration=3, narration="", characters=[]) for i in range(10)]
    first, last = director.ending_range(shots)
    assert (first, last) == (8, 10) and director.ending_range(shots[:2]) == (1, 2)
    assert "shots 8 to 10" in director.ending_note("", first, last)


def test_commentary_uses_real_routing_facts_and_fits_each_shot():
    from cinemafabric.studio import commentary
    from cinemafabric.studio.schemas import Shot

    shots = [Shot(visual=f"shot {i}", camera="static", duration=3, narration="", characters=[]) for i in range(6)]
    shots.append(Shot(visual="", camera="static", duration=2, narration="", characters=[], title_card="THIS WINTER"))
    decisions = [{"label": "Frame 1", "status": "ok", "ts": 1, "model": "old", "worker_label": "Mac · glimmer"},
                 {"label": "Frame 1 (redone)", "status": "ok", "ts": 2, "model": "flux", "worker_label": "Mac mini · ollama",
                  "latency_ms": 4200, "cost_usd": 0}]
    facts = commentary.shot_facts(shots, decisions)
    assert facts[0] == "flux on local machine Mac mini (ollama), 4 s, free" and facts[-1].startswith("title card")
    assert commentary.anchors(shots) == {1: 20, 4: 24}
    lines = commentary.validate(commentary.Commentary(lines=[
        commentary.Line(shot=1, text="I kept it close. " + "word " * 40), commentary.Line(shot=1, text="again"),
        commentary.Line(shot=2, text="not an anchor"), commentary.Line(shot=4, text="The Mac drew this. I held it.")]),
        shots, facts)
    assert [line.text for line in lines] == ["I kept it close.", "I held it."]
    assert commentary.trim("word " * 40).endswith("…") and len(commentary.trim("word " * 40).split()) == 30
    assert commentary.trim("one two three four five, six seven eight nine ten eleven twelve", 8) == "one two three four five."
    assert commentary.trim("one two three four five six seven eight nine", 8).endswith("nine")
    cloud = "muse-image-1.0 on the Meta Model API, 18 s, $0.010"
    assert commentary.grounded("The Mac drew this one fast. I let the dock drift.", cloud) == "I let the dock drift."
    assert commentary.grounded("This came back from the cloud in seconds.", cloud) == "This came back from the cloud in seconds."
    assert commentary.grounded("We made this on a local machine.", cloud) == ""
    assert commentary.grounded("Our own Mac drew it.", facts[0]) == "Our own Mac drew it."
    assert commentary.made_note(facts[0]) == "The ollama in Mac mini drew that one in about 4 seconds, at no cost."
    assert commentary.made_note(cloud) == "That picture came back from Meta's cloud in about 18 seconds."
    assert lines[-1].text == "I held it."
    quiet = commentary.validate(commentary.Commentary(lines=[commentary.Line(shot=4, text="I held it.")]),
                                shots, ["no routing record"] * 3 + [cloud] * 4)
    assert quiet[0].text == "That picture came back from Meta's cloud in about 18 seconds. I held it."
    gap = commentary.validate(commentary.Commentary(lines=[commentary.Line(shot=4, text="I held it.")]), shots, [cloud] * 7)
    assert [(line.shot, line.text) for line in gap] == [(1, "That picture came back from Meta's cloud in about 18 seconds."),
                                                        (4, "I held it.")]


def test_commentary_ducks_the_film_under_the_voice():
    import numpy as np

    from cinemafabric.studio.media import audio

    sr = 8000
    base = np.full(sr * 4, 0.5, dtype=np.float32)
    out = audio.over(base, sr, [(2.0, np.zeros(sr, dtype=np.float32), sr)])
    assert len(out) == len(base)
    assert out[int(2.7 * sr)] < out[int(0.5 * sr)] * 0.4


def test_zip_carries_every_version_of_the_film(tmp_path):
    import zipfile

    from cinemafabric.studio.pipeline import stale_versions, write_zip

    for name in ("film.mp4", "commentary.mp4", "film_vertical.mp4", "poster.png", "old.zip"):
        (tmp_path / name).write_bytes(b"x")
    path = write_zip(tmp_path, "River at Night")
    names = set(zipfile.ZipFile(path).namelist())
    assert {"film.mp4", "commentary.mp4", "film_vertical.mp4", "poster.png"} <= names
    assert not (tmp_path / "old.zip").exists() and len(list(tmp_path.glob("*.zip"))) == 1
    assert {p.name for p in stale_versions(tmp_path)} == {"commentary.mp4", "film_vertical.mp4"}


def test_pictures_cut_straight_and_fade_only_next_to_title_cards(tmp_path):
    pic = tmp_path / "a.png"
    clips = [render.Clip(3, image=pic), render.Clip(3, image=pic), render.Clip(2, card_text="LATER"), render.Clip(3, image=pic)]
    assert render.visibility(clips, 0, 2.95, 0.3) == 1.0
    assert render.visibility(clips, 1, 0.0, 0.3) == 1.0
    assert render.visibility(clips, 1, 2.85, 0.3) == pytest.approx(0.5)
    assert render.visibility(clips, 3, 0.15, 0.3) == pytest.approx(0.5)
    assert render.visibility(clips, 2, 0.0, 0.3) == 1.0


def test_trailer_cut_from_a_movie_skips_the_ending_and_keeps_a_few_lines():
    from cinemafabric.studio.pipeline import trailer_picks

    shots = [Shot(visual=f"shot {i}", camera="static", duration=4.0) for i in range(40)]
    shots[0] = Shot(title_card="ONE NIGHT", duration=2.0)
    voiced = {i: 3.0 for i in range(1, 40, 3)}
    picks = trailer_picks(shots, voiced)
    assert picks and all(not shots[i].title_card and i < 30 for i, _, _ in picks)
    assert sum(s for _, s, _ in picks) <= 30.0
    assert sum(1 for _, _, speaks in picks if speaks) <= 3
    assert trailer_picks([Shot(title_card="X")], {}) == []


def test_watermark_is_read_in_windows_and_voted():
    from cinemafabric.worker.app import read_mark

    rate, sent = 100, [1, 0, 1, 1]
    calls = []

    def detect(chunk):
        calls.append(len(chunk))
        if len(calls) == 1:
            return 1.0, [0.8, 0.3, 0.75, 0.7]
        return 0.9, [0.45, 0.55, 0.4, 0.55]

    prob, message = read_mark(detect, np.zeros(rate * 35), rate)
    assert message == sent and prob == 0.9 and calls == [1000, 1000, 1000]


def test_resolution_scales_every_aspect_to_even_sizes():
    from cinemafabric.studio.options import ASPECTS, video_size

    assert video_size("16:9", "1080p") == (1920, 1080) and video_size("16:9", "2160p") == (3840, 2160)
    assert video_size("9:16", "720p") == (720, 1280) and video_size("1:1", "1080p") == (1080, 1080)
    assert all(v % 2 == 0 for a in ASPECTS for r in ("720p", "1080p", "1440p", "2160p") for v in video_size(a, r))
    assert JobOptions(prompt="a ferry at night in the rain").resolution == "1080p"


def test_script_doctor_only_changes_what_it_may():
    from cinemafabric.studio import agents

    shots = [Shot(visual="a dock at night", duration=3, narration="Old line."), Shot(title_card="LATER", duration=1.5),
             Shot(visual="rain on a window", duration=3, camera="static")]
    edits = [agents.DoctorEdit(op="narration", shot=1, text="He never learned to swim."),
             agents.DoctorEdit(op="narration", shot=2, text="cards can't talk"),
             agents.DoctorEdit(op="duration", shot=2, seconds=9),
             agents.DoctorEdit(op="camera", shot=3, text="dolly_zoom"),
             agents.DoctorEdit(op="camera", shot=3, text="push_in"),
             agents.DoctorEdit(op="visual", shot=7, text="nothing")]
    changed = agents.apply_doctor(shots, edits)
    assert shots[0].narration == "He never learned to swim." and shots[1].title_card == "LATER" and shots[1].narration == ""
    assert shots[1].duration == 2.2 and shots[2].camera == "push_in" and len(changed) == 3
    agents.apply_doctor(shots, [agents.DoctorEdit(op="narration", shot=1, text='Mina: "Maa, I can\'t see you."')], ["Mina"])
    assert shots[0].narration == "Maa, I can't see you." and shots[0].speaker == "Mina"
    assert agents.split_speaker("Note: this stays", ["Mina"]) == ("", "Note: this stays")


def test_editor_keeps_every_line_audible_and_the_length_close():
    from cinemafabric.studio import agents

    shots = [Shot(visual=f"shot {i}", duration=3, camera="static") for i in range(6)]
    voice = {0: 4.2, 3: 2.0}
    edits = [agents.EditorEdit(shot=1, seconds=1.5, camera="push_in"), agents.EditorEdit(shot=2, camera="pan_left"),
             agents.EditorEdit(shot=9, seconds=3)]
    changed = agents.apply_editor(shots, edits, voice, target=18.0)
    assert shots[0].duration >= 4.7 and shots[3].duration >= 2.5
    assert shots[0].camera == "push_in" and shots[1].camera == "pan_left" and changed
    assert abs(sum(s.duration for s in shots) - 18.0) <= 1.8


def test_contact_sheet_numbers_every_tile(tmp_path):
    from cinemafabric.studio import agents

    paths = []
    for i in range(5):
        p = tmp_path / f"{i}.png"
        Image.new("RGB", (640, 360), (40 * i, 80, 120)).save(p)
        paths.append((str(i + 1), p))
    sheet = agents.contact_sheet([("REF", paths[0][1])] + paths, width=200, columns=3)
    assert sheet.width == 3 * 208 + 8 and sheet.height > 2 * 112


def test_director_drops_edits_that_change_nothing():
    from cinemafabric.studio import director

    shots = [Shot(visual="a dock", narration="I have kept this kettle warm."), Shot(visual="rain")]
    plan = director.Plan(edits=[director.Edit(op="narration", shot=1, text='"I have kept this kettle warm."'),
                                director.Edit(op="visual", shot=2, text="rain"),
                                director.Edit(op="narration", shot=2, text="Don't cry here.")])
    kept = director.validate(plan, shots)
    assert [(e.op, e.shot) for e in kept] == [("narration", 2)]


def test_every_voice_language_is_also_a_subtitle_language_with_a_model_code():
    from cinemafabric.studio.options import LANGUAGES, VOICE_LANGUAGES

    assert len(VOICE_LANGUAGES) == 18 and "ko" not in VOICE_LANGUAGES
    for code, (name, iso3) in VOICE_LANGUAGES.items():
        assert len(iso3) == 3 and iso3.islower()
        assert code == "en" or LANGUAGES[code] == name


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_a_failed_render_reports_what_ffmpeg_said(tmp_path):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), "gray").save(frame)
    with pytest.raises(RuntimeError, match=r"(?s)exit code [^0].*missing\.wav"):
        render.render_film([render.Clip(duration=20, image=frame)], (1280, 720), tmp_path / "missing.wav", tmp_path / "film.mp4", fps=24)


def test_longer_films_are_asked_for_more_story():
    from cinemafabric.studio import prompts
    from cinemafabric.studio.schemas import Treatment

    assert runtime("trailer", 90).beats == (4, 6) and runtime("movie", 60).beats == (5, 6)
    assert runtime("movie", 180).beats == (9, 12) and runtime("movie", 300).beats == (15, 20)
    assert runtime("movie", 180).lines[0] >= 24 and runtime("movie", 180).shots < 32
    film = JobOptions(prompt="An idea long enough", format="movie", length="fixed", seconds=180)
    told = prompts.treatment(film)
    assert "9 to 12 story beats" in told and "turning point" in told
    assert "one for every 15 to 20 seconds" in prompts.treatment(film.model_copy(update={"length": "auto"}))
    assert "4 to 6 short story beats" in prompts.treatment(JobOptions(prompt="An idea long enough", format="trailer"))
    t = Treatment(title="T", logline="l", beats=["a"])
    written = prompts.script(film, t, runtime("movie", 180))
    assert "Tell the whole story" in written and "friend who wasn't there" in written
    teaser = prompts.script(JobOptions(prompt="An idea long enough", format="trailer"), t, runtime("trailer", 30))
    assert "Tell the whole story" not in teaser and "friend who wasn't there" not in teaser


def test_a_rewritten_line_stays_with_the_right_speaker():
    from cinemafabric.studio import agents

    assert not agents.speaker_fits("Tam Brewster", "Tam just stared at my second mug.")
    assert agents.speaker_fits("Tam Brewster", "Stop chasing static, Annie.")
    assert agents.speaker_fits("Tam Brewster", "Tamarind grows on the cliff.")
    assert agents.speaker_fits("narrator", "Narrator says nothing.") and agents.speaker_fits("", "Anything.")
    shot = Shot(visual="a pier", narration="Old line.", speaker="Tam")
    assert "voice-over (Tam):" in agents.shot_list([shot])
    agents.apply_doctor([shot], [agents.DoctorEdit(op="narration", shot=1, text="narrator: Tam stared at me.")], ["Tam"])
    assert shot.speaker == "narrator" and shot.narration == "Tam stared at me."


def test_a_short_film_is_written_to_be_followed_by_someone_new():
    from cinemafabric.studio import agents, prompts
    from cinemafabric.studio.schemas import Treatment

    film = JobOptions(prompt="An idea long enough", format="movie", length="fixed", seconds=120)
    t = Treatment(title="T", logline="l", beats=["She finds the recorder."])
    written = prompts.script(film, t, runtime("movie", 120))
    assert "friend who wasn't there" in written and "The first two lines must name" in written
    assert "under 14 words" not in written and "3 to 8" in written and "one continuous telling" in written
    doctor = agents.doctor_prompt(t, [Shot(visual="x")], "short film")
    assert "She finds the recorder." in doctor and "score it 6 at most" in doctor and "one continuous telling" in doctor
    assert "score it 6 at most" not in agents.doctor_prompt(t, [Shot(visual="x")], "trailer")
    many = [Shot(visual=str(i)) for i in range(30)]
    edits = [agents.DoctorEdit(op="narration", shot=i + 1, text=f"New line {i}.") for i in range(30)]
    assert len(agents.apply_doctor(many, edits)) == 15


def test_places_and_objects_stay_the_same_in_every_picture():
    from cinemafabric.studio import prompts
    from cinemafabric.studio.schemas import Character, Treatment

    t = Treatment(title="T", logline="l", places=[Character(name="Lamp Room", look="round iron gallery with green glass")],
                  props=[Character(name="Blue Skiff", look="small wooden boat, sky blue, a white 17 on the bow")])
    film = JobOptions(prompt="An idea long enough", format="movie", length="fixed", seconds=120)
    assert "Blue Skiff: small wooden boat" in prompts.script(film, t, runtime("movie", 120))
    shown = prompts.frame_prompt(film, t, Shot(visual="Annie drags the blue skiff down the shingle"), "base", False)
    assert "a white 17 on the bow" in shown and "green glass" not in shown
    assert "a white 17" not in prompts.frame_prompt(film, t, Shot(visual="fog over the sea"), "base", False)
    assert "a white 17" in prompts.shot_prompts(film, t, [Shot(visual="a boat")])


def test_titles_are_asked_to_catch_the_eye():
    from cinemafabric.studio import prompts

    assert "eye-catching and memorable" in prompts.treatment(JobOptions(prompt="An idea long enough"))


def test_a_short_film_gets_a_written_story_and_the_strongest_writer():
    from cinemafabric.studio import prompts
    from cinemafabric.studio.schemas import Treatment

    film = JobOptions(prompt="An idea long enough", format="movie", length="fixed", seconds=120)
    teaser = JobOptions(prompt="An idea long enough", format="trailer")
    assert film.premium_writing and not teaser.premium_writing
    assert teaser.model_copy(update={"premium_script": True}).premium_writing
    assert "about 240 words" in prompts.treatment(film) and "about 240 words" not in prompts.treatment(teaser)
    t = Treatment(title="T", logline="l", story="Once, a boat sank.", beats=["a"])
    assert "STORY: Once, a boat sank." in prompts.script(film, t, runtime("movie", 120))


def test_story_editor_changes_only_the_words_of_valid_lines():
    from cinemafabric.studio import agents
    from cinemafabric.studio.schemas import Treatment

    shots = [Shot(visual="a", narration="Old one.", speaker="narrator"), Shot(visual="", title_card="WINTER"),
             Shot(visual="c", narration="Old three.", speaker="Tam"), Shot(visual="d", narration="Old four.")]
    changed = agents.apply_story_edit(shots, [
        agents.StoryLine(shot=1, text='"Nobody rows out at nine, and Mara knew why."'),
        agents.StoryLine(shot=2, text="A card must not gain a line here."),
        agents.StoryLine(shot=3, text="Tam watched her go from the pier."),
        agents.StoryLine(shot=4, text=" ".join(["word"] * 30)),
        agents.StoryLine(shot=9, text="No such shot in the film at all."),
    ])
    assert len(changed) == 1 and shots[0].narration == "Nobody rows out at nine, and Mara knew why."
    assert shots[0].speaker == "narrator" and not shots[1].narration
    assert shots[2].narration == "Old three." and shots[3].narration == "Old four."
    prompt = agents.story_editor_prompt(Treatment(title="T", logline="l", story="The tale."), shots)
    assert "THE STORY: The tale." in prompt and "hook" in prompt and "voice-over (narrator):" in prompt


def test_score_sections_join_at_an_even_loudness():
    sr = 1000
    quiet, loud = np.full(2000, 0.1, dtype=np.float32), np.full(2000, 0.5, dtype=np.float32)
    whole = audio.join_score([quiet, loud, quiet], fade_s=0.1, sr=sr)
    assert len(whole) == 6000 - 200
    assert abs(whole[500] - 0.1) < 0.01 and abs(np.median(whole[2500:3000]) - 0.1) < 0.01
    assert audio.is_silent(np.zeros(100)) and audio.is_silent(np.zeros(0)) and not audio.is_silent(loud)


async def test_the_score_follows_the_story_in_sections(tmp_path):
    from types import SimpleNamespace

    from cinemafabric.studio.schemas import Treatment
    from cinemafabric.util import b64d, b64e, read_wav, wav_bytes

    p = Production(new_state("t1", JobOptions(prompt="An idea long enough", format="movie")), fabric=None,
                   store=SimpleNamespace(save=lambda s: None), data_dir=tmp_path)
    p.treatment = Treatment(title="T", logline="l", music_mood="solo cello", music_cues=["uneasy", "rising", "hopeful"])
    calls = []

    async def worker(kind, payload, stage, label, **kw):
        calls.append((payload["prompt"], "continue_from" in payload, payload["duration_s"], label))
        n = int(min(payload["duration_s"], 25.0) * 1000)
        silent = len(calls) == 2
        tone = np.zeros(n) if silent else 0.4 * np.sin(np.arange(n) * 0.05)
        return SimpleNamespace(output={"audio": b64e(wav_bytes(tone, 1000))})

    p.call = worker
    samples, sr = read_wav(await p._score_in_sections(70.0))
    prompts_used = [c[0] for c in calls]
    assert sr == 1000 and "uneasy" in prompts_used[0] and "hopeful" in prompts_used[-1] and "solo cello" in prompts_used[-1]
    assert not calls[0][1] and all(c[1] for c in calls[1:])
    assert calls[2][3].endswith("(again)") and len(samples) / sr >= 69

    async def whole_film(kind, payload, stage, label, **kw):
        calls.append(payload)
        n = int(payload["duration_s"] * 1000)
        return SimpleNamespace(output={"audio": b64e(wav_bytes(0.4 * np.sin(np.arange(n) * 0.05), 1000))})

    calls.clear()
    p.call = whole_film
    await p._score_in_sections(70.0)
    assert len(calls) == 1, "a worker that returns the whole length needs only one call"
    assert b64d(b64e(b"x")) == b"x"


def test_the_written_story_is_checked_and_kept():
    from cinemafabric.studio import originality, pipeline, prompts
    from cinemafabric.studio.schemas import Treatment

    t = Treatment(title="T", logline="l", story="Borrowed Hero sinks the boat.", beats=["a"])
    assert "Borrowed Hero sinks" in pipeline._story_text(t)
    assert "Borrowed Hero sinks" in originality.story_prompt(t, [])
    originality.apply(t, [], [originality.StoryIssue(where="phrase", original="Borrowed Hero", replacement="Tenzin")])
    assert t.story == "Tenzin sinks the boat."
    film = JobOptions(prompt="An idea long enough", format="movie", length="fixed", seconds=120)
    from cinemafabric.studio.schemas import Scene, Script
    written = prompts.screenplay_markdown(t, Script(scenes=[Scene(heading="H", shots=[])]), film, runtime("movie", 120))
    assert "Tenzin sinks the boat." in written
