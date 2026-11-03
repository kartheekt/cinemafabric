# Responsible AI

CinemaFabric generates every part of a film: the script, the pictures, the voices and the music. It therefore has to be careful about what it will make, clear about what it has made, and honest about where its safeguards stop. This document maps the risks we know about to the measures in the code, following the areas of Meta's [Responsible Use Guide](https://ai.meta.com/static-resource/responsible-use-guide/).

## Harmful content

| Risk | Measure | Where |
|---|---|---|
| A harmful idea | Llama Guard 3 checks the idea before anything is generated. Categories on the profile's block list stop the film; others, such as violence in a thriller, are recorded and allowed for fiction | `check_content` in `studio/pipeline.py`; `[safety]` in `profiles/default.toml` |
| A borderline classification | Anything Llama Guard flags is reviewed by a larger model with a written fiction policy. It blocks real-world instructions for serious harm, sexualised minors, promotion of self-harm, hate and targeting of real people, even in a category normally allowed for fiction, and allows what fiction legitimately depicts. Its reason is recorded | `_second_opinion`; `safety_review` in `studio/prompts.py` |
| Harm introduced later in the run | The script, the picture prompts, script edits, redo notes, director notes and the edits planned from them, and the commentary are each checked in full before use. Long text is split into parts, never truncated | `check_content` call sites in `studio/pipeline.py` |
| No classifier available | With `moderation = "best_effort"` the film continues and the record says the check was skipped; with `"required"` the film stops | `[safety] moderation` |
| Harmful pictures | Muse Image applies Meta's content policy and refuses requests that breach it; the studio records the refusal and draws the shot again without the reference, then from the script's plain description, then as the setting with nobody in it. Every picture then goes through the picture checks described below | `_frame` in `studio/pipeline.py` |
| A problem noticed after release | Every public watch page has a *Report this film* form. Reports appear on the film's Safety panel and in an admin list, and are rate-limited per address | `report_film` in `studio/app.py` |

The block list is a policy choice. By default it stops sex-related crimes, child sexual exploitation, intellectual property, indiscriminate weapons, hate, suicide and self-harm, and sexual content. Violent and non-violent crime, defamation, specialised advice, privacy, elections and code-interpreter abuse are recorded but allowed, because fiction legitimately depicts them.

## Real people, brands and other people's work

CinemaFabric makes original work only. Four layers enforce this, from the idea to the final picture.

1. **The idea.** After Llama Guard, a language model checks the idea against a written policy. It rejects existing franchises and their characters, real public figures, real brands at the centre of a story, a named living artist's or studio's style, and song lyrics or quoted lines. Genres, tropes, history, myth, folklore and public-domain works are allowed. A borrowed idea stops before anything is generated, and the studio offers an original version of the same idea that can be started with one click. In a live test, an idea built on a well-known superhero franchise, naming its hero, his love interest and his nemesis, became "a streetwise acrobat with a grappling rig swings through monsoon Mumbai to rescue a journalist from a masked tech mogul". Llama Guard's intellectual-property category is also on the block list. (`studio/originality.py`; `idea_originality`)
2. **The story.** Once the script exists, its title, character names and voice-over lines are checked. A title that copies a well-known work, a famous character's or celebrity's name, or a quoted line is replaced everywhere it appears, before anything is drawn or spoken. (`story_originality`)
3. **The pictures.** Every storyboard frame, character sheet and poster is checked by a vision model for a recognisable real person, a real brand or logo, an existing character, and stray writing, signatures or watermarks, which often indicate copied material. A flagged picture is redrawn with an instruction to make the design entirely original, then redrawn again with a stricter instruction. A frame that still fails is replaced by a shot of the setting with nobody in it; with `moderation = "required"`, the film stops instead. These checks run whether or not the frame critic is enabled. (`_clean_picture`, `_replace_with_setting`)
4. **Sharing.** A film with an unresolved check cannot be given a public link.

Prompts reinforce these rules: the writer is told never to name real brands, companies or celebrities, picture prompts ask for no text or logos, and model-written idea suggestions exclude real people, sequels and existing characters.

**Limits.** No reference database of existing images is available, so likeness and character checks depend on a vision model's judgement. Music is not fingerprinted against existing recordings; MusicGen was trained on licensed music.

## Disclosure and provenance

- **Visible labels.** The end card and the poster say "Made with AI", and the label travels with shared films.
- **Metadata.** Posters and frames carry IPTC `DigitalSourceType = trainedAlgorithmicMedia` in XMP, the field platforms (including Meta's) read to label AI-generated images. The MP4 carries the same statement in its tags.
- **Content Credentials.** With the `c2pa` extra installed, the film, its trailer, the commentary version, platform versions, the poster and the thumbnail carry a signed C2PA manifest stating that they were created by CinemaFabric with the digital source type `trainedAlgorithmicMedia`. Set `CF_C2PA_CERT` and `CF_C2PA_KEY` to sign with your own certificate. Otherwise a local certificate is created once in the data directory, with its key readable by its owner only; files signed this way verify as intact, but viewers will report that the signer is not on a public trust list.
- **Audio watermark.** The soundtrack is marked with [AudioSeal](https://github.com/facebookresearch/audioseal) (MIT) using a 16-bit payload derived from the film's identifier. The mark is read back before export in ten-second windows whose bit probabilities are averaged, so full-length films verify as reliably as trailers. It survives the AAC encode in the final MP4.
- **Records.** Every download includes `CREDITS.txt`, listing each model used with its licence and required notices, and `manifest.json`, recording every check with its verdict and reason, the frame scores, the narration check and the watermark. The film's *Safety and provenance* panel shows the same information.

## Licences and commercial use

- Every model the router can use carries a licence, and the profile's `[licences] allowed` list decides which may be used. Removing `cc-by-nc-4.0` excludes the two non-commercial models, MusicGen and MMS-TTS; the router then uses the built-in score and the voice-over is shown as captions rather than spoken.
- The **Commercial-safe** switch applies the same exclusion to a single film without changing the profile. The director's commentary is unavailable for such films, because its voice uses MMS-TTS.
- The Safety panel and `CREDITS.txt` identify any non-commercial model a film used.
- Outputs produced with Llama carry "Built with Llama" in the interface, the README, `NOTICE` and every export that used Llama.
- The lowest-cost Muse Spark tier, *contributor*, allows Meta to use inputs to improve its models. It is the default text model in `profiles/default.toml`; for confidential work, set `[cloud] text` to `muse-spark-1.3`.

## Privacy

- With local machines available, ideas, scripts and pictures are processed on your own hardware, and the routing log shows exactly which tasks went to the cloud.
- There is no telemetry. Films, media and the routing log stay in the data directory.
- The API key is stored only in the fabric's owner-readable database and is never shown back in the interface.

## Spending

Every cloud call reserves its worst-case cost, including any reasoning budget, before it runs. Neither the overall limit nor a film's own limit can therefore be exceeded, even with many calls in flight. Estimates are shown before a film starts and before a director's plan is applied.

## Agents

The director agent turns a note into edits. It is constrained to:

- a fixed set of operations and at most eight edits, each validated against the film;
- a content check on both the note and the planned text;
- a cost estimate, with **nothing applied until the user approves**;
- the film's spending limit;
- a recorded history of every note, plan, approval and cost.

The rest of the agent crew (script doctor, story editor, continuity supervisor, editor, translation reviewer, test screening and release writer) follows the same pattern. Each agent has a fixed set of operations; every change is validated against shot numbers, camera names, length ranges or the translation checks; and every turn is recorded with its model and cost.

Two agents act without asking, within narrow limits:

- **Test screening.** Its note goes to the director agent and is applied automatically only when the cut scores below 8/10. This happens once, within a $0.06 budget, all or nothing, and the result is scored again.
- **Continuity supervisor.** It redraws at most three frames and discards any redraw that fails the picture check.

The release writer produces a draft for a person to review; nothing is posted anywhere. Its description of how the film was made is limited to the models in the routing log, and it may not claim that a person wrote, voiced or edited the film. The commentary writer is held to the same log: a sentence attributing a shot to a machine or service the log does not show is removed before anything is spoken. *Another ending* uses the director agent with its edits restricted to the final shots, so the earlier part of the film cannot change.

## Security

The fabric requires a token, studio settings require local access or a password, workers require a per-worker secret, machine addresses are validated and request sizes are capped. Details are in the README's Security section. The code was reviewed in five rounds with Muse Code, and every finding and its outcome is recorded in [reviews.md](reviews.md).

## Known limits

- Classifying fiction is a judgement call. Every Llama Guard flag gets a second opinion from a larger model with a written fiction policy, and both verdicts are recorded (see [evals.md](evals.md)); two models can still agree on a wrong answer.
- Pictures are safeguarded by Muse Image's content policy and the picture checks above, rather than by a separate local image classifier.
- The AudioSeal watermark marks the soundtrack only. Image metadata and Content Credentials can be lost to a screenshot or re-encode.
- The narration check measures how much of the script the voice-over conveys, not how natural it sounds.
- Agents' scores are a model's opinion, so the automatic polish is limited to a single, low-cost round.
