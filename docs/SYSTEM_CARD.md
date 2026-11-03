# System card

## Overview

CinemaFabric makes short films and trailers from a one-sentence idea. A studio service runs the production (story, script, storyboard, poster, voice-over, music and edit) and sends every model call to a fabric service. The fabric chooses the most suitable healthy worker for each task: a GPU or Apple machine on the local network, an Ollama server, or the Meta Model API. Each choice is recorded with a plain-language reason, and a crew of agents reviews and refines the work at defined points in the run.

## Intended use

- Putting existing local GPUs and Macs to work alongside the Meta Model API, with each task routed to where it runs best.
- Pitch trailers, mood films, storyboards and posters for original stories.
- Short films for creators worldwide, with voice-over in eighteen languages and subtitles in nineteen.
- Teaching and demonstrating combined local and cloud model routing.

## Out of scope

- Films about real, identifiable people, or using brands, franchises or other people's characters. The originality and picture checks are designed to stop these.
- News, political or persuasive content presented as real.
- Anything prohibited by Llama's [Acceptable Use Policy](https://www.llama.com/llama3_1/use-policy/) or the Meta Model API terms.

## Models

What runs where depends on the available machines and the profile. These are the defaults.

| Role | Local (first choice) | Cloud (Meta Model API) | Licence |
|---|---|---|---|
| Story, script, shot prompts, director plans, agents, commentary | Muse Glimmer 30B, Llama 3.1 8B (Ollama) | Muse Spark 1.3 (standard or contributor tier); Muse Wonder 1.0 for premium scripts | Apache 2.0; Llama 3.1; Meta Model API terms |
| Frame critic, picture and continuity checks (vision) | Muse Glimmer 30B | Muse Spark 1.3 | As above |
| Originality checks, second opinion on content | Muse Glimmer 30B | Muse Spark 1.3 | As above |
| Content check | Llama Guard 3 1B | — | Llama 3.2 Community License |
| Suggestions in the form | Llama 3.1 8B (draft tier) | Muse Spark 1.3 | As above |
| Character sheets, frames, poster | — | Muse Image 1.0 | Meta Model API terms |
| Depth for camera motion | DINOv2 with a DPT depth head (small) | — | Apache 2.0 |
| Subject cutouts | SAM 3 (gated) or SAM 2.1 | SAM 3.1 | SAM License; Apache 2.0 |
| Voice-over and commentary voice | MMS-TTS in eighteen languages, pitched per character | — | CC-BY-NC 4.0 |
| Subtitle and voice-over translation | Muse Glimmer, Llama 3.1 | Muse Spark 1.3 | As above |
| Score | MusicGen stereo medium, with a built-in score as fallback | — | CC-BY-NC 4.0; built in |
| Narration check | — | Muse Voice Transcribe 1.0 | Meta Model API terms |
| Soundtrack watermark | AudioSeal 16-bit | — | MIT |

The two CC-BY-NC models are optional. Removing that licence from the profile, or switching a film to commercial-safe, excludes them.

## How it decides

- Each task specifies a kind, a quality tier and a preference: local-first, balanced or cloud-first.
- Workers that cannot perform the task, are unhealthy, use an excluded licence or would exceed a spending limit are ruled out, each with a recorded reason.
- The remaining candidates are ranked by cost and expected time, measured from real runs. If one fails, the next takes over; two consecutive failures take a worker out of rotation for 30 seconds.
- Results are cached by content, so a retry pays only for work that did not finish.

## Agents

Eleven agents work on each film, each with a single responsibility, a fixed set of operations and recorded limits: script doctor, story editor, originality checker, frame critic, continuity supervisor, narration check, editor, translation reviewer, test screening (which hands its note to the director agent), release writer and commentary writer. Each turn is recorded with the model that ran it and its cost. The README lists every agent's inputs, permitted changes and limits.

## Safety measures

Summarised from [RESPONSIBLE_AI.md](RESPONSIBLE_AI.md):

- Llama Guard on every text that leads to generation, with a second opinion from a larger model on anything it flags.
- Originality checks on the idea and the script, and picture checks on every frame, character sheet and poster.
- No public link while any check is unresolved, and a report form on every shared film.
- "Made with AI" on the film and poster, IPTC metadata, signed C2PA Content Credentials and an AudioSeal watermark.
- A credits file and a manifest of every check in each download.
- Licence-aware routing and a per-film commercial-safe switch.
- Director edits applied only after approval, except one automatic, low-cost polish round after the test screening.
- Spending limits that cannot be exceeded.

## Evaluation

[evals.md](evals.md) records the latest runs: films made end to end with cost, time, local share, frame scores, narration match and watermark verification, ideas the content and originality checks must refuse, and false-alarm tests.

## Limitations

- Motion is a depth-aware camera move over a still frame, not generated video.
- Each language has a single MMS-TTS voice, pitched differently for each character.
- Smaller local models write plainer scripts than Muse Spark or Muse Wonder, so short films are written by Muse Wonder; trailers can use it through the premium script option.
- The frame critic and the agents' scores are a model's opinion, and can pass work a person would redo.
- Safety gaps are listed under "Known limits" in [RESPONSIBLE_AI.md](RESPONSIBLE_AI.md).
