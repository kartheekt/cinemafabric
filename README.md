# CinemaFabric

![A film strip turning into threads of light that link several computers and the cloud](docs/banner.jpg)

CinemaFabric makes the machines you already own and the Meta Model API work as one AI studio. At its core is a small *distributed inference fabric*: every step of a production is a task, and the fabric sends each task to the place that runs it best, whether a local NVIDIA GPU, an Apple silicon Mac, a plain CPU or a Meta model in the cloud. It weighs quality, latency, queue depth, licence and cost, records the reason for every decision, moves work elsewhere when a machine drops out, and reuses any result it has already produced. Adding a machine adds capacity; removing one costs nothing but time.

The studio built on the fabric turns a one-sentence idea into a finished trailer or short film, complete with a script, storyboard, poster, voice-over, score, subtitles and release copy. It is a demanding test of the fabric: a 20-second trailer is around sixty tasks of nine kinds (writing, safety checks, pictures, cutouts, depth, speech, music, transcription and watermarking), and the router places each one.

Built for the Meta AI Global Developer Hackathon 2026. **Built with Llama.**

*The banner was painted by Muse Image and the [architecture diagrams](docs/architecture.md) were drawn by Muse Spark, both routed through CinemaFabric itself.*

## Why hybrid

A team with a few GPUs and a cloud API account usually ends up using one or the other: local hardware sits idle while everything goes to the cloud, or work stays local and waits for models those machines cannot run. CinemaFabric uses both at once, each for what it does best.

- **Local machines take the work they run well.** Trailer scripts, checks and agents on Muse Glimmer, content checks on Llama Guard, and depth, voice-over, music and watermarking on local GPUs. In a benchmark of the same ideas under each routing preference, local-first and balanced routing ran about 70% of every film's tasks locally, and the local machines were working for more than 80% of each production.
- **The cloud takes what local hardware cannot do as well.** Muse Wonder writes short films, Muse Image draws the pictures, SAM 3.1 cuts out the subjects, Muse Voice Transcribe checks the narration, and Muse Spark or Muse Wonder step in when a task needs a larger model or the local ones are busy. That is nearly all of the cloud use: about $0.13 for a 30-second trailer and about $0.50 for a 2-minute short film.
- **Finished work is reused.** Results are cached by content, so retries, redos and new versions reuse everything that has not changed. An alternate ending re-made three of nine shots for $0.035, against $0.136 for the original film.
- **Capacity follows the hardware.** A busy device passes work to the next option, a failing one is taken out of rotation, and a new machine joins with one command; its node agent finds the devices and decides which models each can hold.
- **Policy decides what may run where.** Licences, a local-first preference that keeps ideas and drafts on your own machines, and spending limits are enforced by the router for every task.

## Who it's for

- **Studios, agencies and teams that already own GPUs** and want that hardware working before they pay for cloud capacity, with drafts kept on their own machines.
- **Creators who work in their own language.** Voice-over in eighteen languages and subtitles in nineteen, so a story reaches its audience in the language they speak. That includes languages the large tools tend to overlook: eight Indian languages, Swahili, Indonesian, Vietnamese, Turkish and Arabic, alongside English, Spanish, French, German and Portuguese.
- **Independent filmmakers and writers** pitching an idea, with a trailer, poster and storyboard to show a producer.
- **Teachers, NGOs and community groups** that need short explainer or public-information films in local languages.

Every film arrives with what someone publishing it needs: a visible AI label, an audio watermark, Content Credentials, a record of every check, and a commercial-safe mode.

**Themes.** *Push the Frontier*: a fabric that turns a scattered set of GPUs, Macs and the Meta Model API into a single studio, with agents that review and correct their own work. *Build for Your World*: films in the languages people speak, made on hardware they already own.

## For judges

1. **Run it without a GPU or an API key.** Mock mode simulates every model, so nothing is spent:
   ```bash
   uv sync && npm --prefix frontend ci && npm --prefix frontend run build
   CF_MODE=mock uv run python scripts/smoke.py      # makes several films end to end and checks the results
   ./scripts/dev.sh mock                             # then open http://127.0.0.1:8000
   ```
2. **Take a machine away mid-run.** While a film is being made, the *Live fabric* panel shows each task travelling to the machine or Meta model that took it. Switch any machine off and watch the next tasks move elsewhere; the panel confirms nothing was lost or paid for twice. In a measured run, 23 writing tasks moved to the cloud after a machine was taken offline mid-film, and none were lost.
3. **Read the evidence:** [evaluation results](docs/evals.md), including where each film's tasks ran, [five rounds of code review with Muse Code](docs/reviews.md), the [responsible AI notes](docs/RESPONSIBLE_AI.md) and the [system card](docs/SYSTEM_CARD.md).

## What it makes

Choose a **trailer** (15 to 90 seconds) or a **short film** (1 to 5 minutes), or let the writer choose the length from how much story there is. Then choose a style (live-action, 3D animation, anime, film noir, claymation, watercolour), a genre, an aspect ratio (16:9, 2.39:1, 9:16, 1:1), a resolution (720p to 4K), a frame rate (24, 30 or 60 fps) and where the work should run.

| Output | How it is made |
|---|---|
| Story and bible | For a short film, Muse Wonder writes the complete story first. For a trailer, Muse Glimmer on a local machine, or Muse Spark or Muse Wonder through the API. Characters, places and key objects get one fixed description, used in every picture that shows them |
| Shot-by-shot script | The same model, returning validated structured output. A short film's narration tells the story as it goes, so someone new can follow it |
| Character reference sheets | Muse Image |
| Storyboard frames | Muse Image, anchored on the character sheets so characters stay consistent |
| Poster | Muse Image key art with typography set by the studio |
| Camera motion | DINOv2 estimates depth for each frame and SAM outlines the subject. Each camera move then bends with that depth, so near objects travel further than distant ones. The warp is continuous, so no part of the picture is cut away from the rest |
| Voice-over | MMS-TTS on a local GPU in eighteen languages, with a distinct voice for each speaker. Every language was tested by speaking a sentence and transcribing it back |
| Score | MusicGen on a local GPU, written in sections that follow the story's music cues and continue from one another, then ducked under the voice-over |
| Narration check | Muse Voice Transcribe listens back to the voice-over and unclear lines are recorded again |
| Subtitles | Up to four of nineteen languages. Each translation is checked for line count, script and fidelity, and retried in the cloud if the local model gets it wrong |
| Edit | Straight cuts, title cards, subtitles and sound, encoded with x264 at CRF 17 and tuned for animation or live action by style |
| Trailer for every film | A short film also gets a trailer of about 30 seconds cut from its own shots, voice and score, using only the first three quarters so the ending stays hidden |
| Platform versions | Vertical, widescreen and square, re-framed and loudness-normalised |
| Director's commentary | An optional second soundtrack in which the director talks about each shot and how it was made. Claims about machines are checked against the routing log |
| Release copy | YouTube and Instagram copy and a thumbnail, as a draft to review before posting |
| Download | One ZIP with every version of the film, the poster, script, subtitles, frames, credits, manifest and Production Passport |
| Sharing | An optional watch page with an unguessable link, an embed player and link previews |

A 1080p, 3½-minute film and its trailer render in about two minutes on a desktop CPU, and a 3-minute film at 4K in about five and a half. Resolutions above 1080p are upscaled from the picture model's output of about 2,000 pixels.

### Working with a finished film

- **Review the script first.** Optionally pause after the script to edit lines, timings and camera moves, or to reorder and remove shots.
- **Redo a shot.** Describe what should change and only that shot is regenerated; everything else comes from the cache.
- **Give the director a note.** For example, "end on her alone at dawn and make the last line hopeful". The director agent proposes a few checked edits with a cost estimate, and nothing changes until you apply them.
- **Try another ending.** The film is copied and only the final stretch is remade; everything before it is reused.
- **Make the next episode.** Same cast, reference sheets and settings; the writer is given how the previous episode ended.
- **Recast a character.** Save a character from one film and cast them in another with the same name, look and reference sheet.
- **Keep it commercial-safe.** One switch excludes models licensed for non-commercial use. The built-in score replaces MusicGen and the voice-over becomes on-screen captions.

## The agent crew

Alongside the pipeline, each film is worked on by a crew of agents. An agent looks at one part of the production and may change it only through a small set of operations, within fixed limits. Every turn is recorded on the film's **Agent crew** panel: what the agent saw, its score, what it changed, which model did the work and what it cost. The crew is on by default and can be switched off per film.

| Agent | Runs | Looks at | Can change | Limits |
|---|---|---|---|---|
| Script doctor | After the script | The full shot list | Lines, pictures, timing, camera moves | Six edits per round, two rounds, stops at 8/10 |
| Story editor | After the script doctor, on short films | The story and every line | Rewrites the voice-over as one piece, for flow and tension | Keeps the facts and speakers; 3 to 22 words a line |
| Originality checker | Before and after the script | The idea, title, names and lines | Offers an original rewrite; renames; rewrites lines | Replacements must match the original text exactly |
| Frame critic | Each frame | The frame and its shot description | Redraws the frame | Two redraws, then a scenery shot |
| Continuity supervisor | After the frames | A contact sheet of each character's reference and every shot they appear in | Redraws frames that drifted | Three redraws; a redraw that fails the picture check is discarded |
| Narration check | After the voice-over | The voice-over, transcribed by Muse Voice Transcribe | Records unclear lines again | Keeps a new take only if more of it is understood |
| Editor | Once the voice-over exists | The shot list with real line lengths | Shot lengths and camera moves | Every line must fit; total length stays within 10% |
| Translation reviewer | Each translated language | English and translated lines side by side | Retranslates lines that drift | A fix must still pass the translation checks |
| Test screening and director | After the first cut | A contact sheet of the cut and the script | Writes a note, which the director agent turns into edits | Only below 8/10, one round, $0.06 limit, all or nothing, then scored again |
| Release writer | At the end | Title, logline and the models that made each part | YouTube and Instagram copy, thumbnail | Describes how the film was made only from the routing log; never claims a person made it |
| Commentary writer | On request | The shots and the routing log | A commentary track | Every claim about a machine must match the log |

Agents that compare several pictures look at a single numbered contact sheet, so any vision model on the fabric can do the work, local or cloud.

## Safety and provenance

- **Content check.** Llama Guard 3 checks every piece of text that leads to generation: the idea, the script, picture prompts, edits, notes and commentary. Anything it flags goes to a larger model with a written fiction policy, which blocks real-world harm even in fiction and allows what fiction legitimately depicts.
- **Original work only.** Ideas built on an existing franchise, a real person, a brand, a named artist's style or song lyrics are stopped before anything is generated, and an original version of the same idea is offered. Borrowed titles, famous names and quoted lines in the finished script are replaced.
- **Picture checks.** Every frame, character sheet and poster is checked for real people, brands, existing characters and stray writing or watermarks. A picture that keeps failing is redrawn twice and then replaced. A film with an unresolved check cannot be shared, and viewers can report any shared film.
- **Labelled as AI.** "Made with AI" appears on the end card and poster. Files carry IPTC `trainedAlgorithmicMedia` metadata and signed C2PA Content Credentials, and the soundtrack carries an AudioSeal watermark that is verified before export.
- **A record of every film.** The *Safety and provenance* panel and the `manifest.json` in each download list every check, its verdict and reason, and every model used with its licence.
- **Spending limits** can be set overall and per film, and can never be exceeded.

Details, including what these measures do not cover, are in [docs/RESPONSIBLE_AI.md](docs/RESPONSIBLE_AI.md).

## The fabric

```
               ┌──────────────┐        ┌────────────────────── Fabric (:8100) ───────────────────────┐
 Browser ────▶ │ Studio :8000 │ ─────▶ │  Router ─ ranks offers by tier, preference, cost, latency    │
  (React)      │  pipeline    │  task  │  Ledger ─ reserves cost before cloud calls, optional limits  │
               │  agents      │ ◀───── │  Cache  ─ content-addressed; retries and replays are free    │
               └──────────────┘ result │  Registry ─ health probes, queue depth, circuit breaker      │
                                       └───────┬──────────────────┬──────────────────┬───────────────┘
                                               │                  │                  │
                                     Node agents (any number)   Profile machines    Meta Model API
                                     GPU / Apple / CPU workers  e.g. a remote       Muse Spark / Wonder
                                     + local Ollama models      Ollama server       Muse Image · SAM 3.1
                                                                                    Muse Voice Transcribe
```

A fuller diagram is in [docs/architecture.md](docs/architecture.md).

**How a task is routed**

1. The studio submits a task such as `text.generate` at the `standard` tier with a `local_first` preference.
2. The router gathers every offer for that kind of task and rules out those below the tier, those without vision when images are attached, those with a licence the profile or film excludes, unhealthy or disabled workers, and anything that would exceed a spending limit.
3. It ranks what remains. *Local-first* sorts by cloud use, then cost, then expected time. *Balanced* weighs cost against time. *Cloud-first* prefers the API. Expected time is measured latency plus the current queue, so a busy device passes work to the next option.
4. Cloud calls reserve their worst-case cost in the ledger before they run and settle the actual cost afterwards, so concurrent tasks cannot overshoot a limit.
5. If a worker fails, the next candidate takes the task and the reroute is recorded. Two consecutive failures take a worker out of rotation for 30 seconds, and a worker that was healthy is marked down only after two failed health checks in a row.
6. If nothing meets the requested tier, the router falls back to a simpler option (for example, the built-in score) rather than failing the film.

**Where the work goes.** With local-first routing, pictures, subject cutouts and the narration check go to the cloud; writing, safety checks, depth, voices, music and watermarking stay on local hardware while it is available. A 30-second trailer typically sends $0.13 to $0.15 of work to the cloud, nearly all of it Muse Image at $0.01 per picture; the agent crew adds a few cents. The studio shows an estimate before a film starts and a receipt afterwards, broken down by machine and by model, so you can see exactly what ran where.

## Meta models used

| Model | Runs on | Used for | Licence |
|---|---|---|---|
| Muse Spark 1.3 (standard and contributor tiers) | Meta Model API | Story, script, checks, translation, agents | Meta Model API terms |
| Muse Wonder 1.0 | Meta Model API | Short-film stories and scripts, the Story editor, premium trailer scripts | Meta Model API terms |
| Muse Image 1.0 | Meta Model API | Character sheets, frames, poster, banner | Meta Model API terms |
| SAM 3.1 | Meta Model API | Subject cutouts from a text prompt | Meta Model API terms |
| Muse Voice Transcribe 1.0 | Meta Model API | Narration check | Meta Model API terms |
| Muse Glimmer | Ollama, any machine | Story, script, checks, vision agents | Apache 2.0 |
| Llama 3.1 8B | Ollama, any machine | Shot prompts, suggestions | Llama 3.1 Community License |
| Llama Guard 3 1B | Ollama, any machine | Content check | Llama 3.2 Community License |
| DINOv2 (DPT depth head) | Local GPU | Depth for camera motion | Apache 2.0 |
| SAM 3 or SAM 2.1 | Local GPU | Local cutouts | SAM License or Apache 2.0 |
| MMS-TTS | Local GPU | Voice-over in eighteen languages | CC-BY-NC 4.0 |
| MusicGen (stereo medium) | Local GPU | Score | CC-BY-NC 4.0 |
| AudioSeal | Local GPU | Soundtrack watermark | MIT |

Each offer carries its licence and the router only uses licences the profile allows. To run without non-commercial weights, remove `cc-by-nc-4.0` from `[licences] allowed`, or use the per-film commercial-safe switch. Attributions are in [NOTICE](NOTICE).

**Meta tools used to build it.** Beyond the models the product runs, Meta's tools were part of how it was made:

- **Muse Code**, Meta's coding agent, reviewed the code in five rounds covering security, guardrails, spending, licensing, code quality, tests, the content safeguards and the agent crew. It ran read-only on a clean copy of the repository, and each finding was verified before anything changed. Every finding and its outcome is in [docs/reviews.md](docs/reviews.md).
- **Muse Spark** drew the [architecture diagrams](docs/architecture.md) from the live worker registry, routed through CinemaFabric's own fabric.
- **Muse Image** painted the banner at the top of this page.

## What it runs on

Everything the running product uses. Licences are in [NOTICE](NOTICE) and in each package.

| Part | Uses |
|---|---|
| Meta models and API | The models listed above, through the Meta Model API and as open weights |
| Local models | Ollama, PyTorch, Hugging Face Transformers |
| Services | Python, FastAPI, Uvicorn, Pydantic, httpx |
| Pictures and sound | Pillow, OpenCV, NumPy, SciPy, ffmpeg with x264, system fonts (DejaVu in Docker) |
| Interface | React, TypeScript, Vite, Tailwind CSS |
| Provenance | AudioSeal, c2pa-python, cryptography |
| Storage | SQLite by default; optionally Postgres (psycopg) and S3-compatible storage (boto3) |
| Monitoring (optional) | Prometheus, Grafana |
| Packaging and checks | uv, npm, Docker, pytest, Ruff |

## Getting started

### Mock mode (no GPU, no API key)

Mock mode simulates every worker, so the router, ledger, pipeline, renderer and interface all run on an ordinary laptop with nothing spent.

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 22 (or 20.19+) and ffmpeg on `PATH` (or its path in `CF_FFMPEG`).

```bash
uv sync
npm --prefix frontend install
npm --prefix frontend run build
./scripts/dev.sh mock          # macOS and Linux; on Windows: ./scripts/dev.ps1 -Mode mock
```

Open <http://127.0.0.1:8000>, choose an example idea and press **Start**.

### With real models

```bash
cp .env.example .env               # set MODEL_API_KEY and, optionally, CF_PROFILE
uv sync --extra gpu --extra c2pa   # PyTorch for local workers; C2PA signing for Content Credentials
npm --prefix frontend install && npm --prefix frontend run build
./scripts/dev.sh                   # or ./scripts/dev.ps1 on Windows
```

This starts the fabric, the studio and a node agent for the current machine.

### Adding machines

On any machine with a CinemaFabric checkout, run the node agent:

```bash
CF_JOIN_TOKEN=<join token> python -m cinemafabric.node --join http://<fabric-host>:8100 [--dry-run]
```

The agent finds the machine's NVIDIA GPUs, Apple GPU or CPU and its local Ollama models. It keeps memory free for Ollama, decides which models fit on which device, starts one worker per device and registers everything with the fabric. Workers are restarted if they exit. `--dry-run` prints the plan without starting anything.

- **Ollama on another machine:** run `scripts/serve-ollama.sh` there, then add it from **Machines → Add machine**, or list it under `[[machines]]` in your profile.
- **The join token** is `CF_JOIN_TOKEN`, or is generated once into `data/join_token`. The interface shows the full node command. Machines added by agents or through the interface persist across restarts.
- **SAM 3** is gated on Hugging Face. Request access, run `huggingface-cli login` on the worker machine and restart the node; until then, workers use SAM 2.1.

Keep the virtual environment outside cloud-synced folders, as sync clients can lock files inside it. `dev.ps1` uses `~/.venvs/cinemafabric`.

### Docker

```bash
docker compose up --build                          # fabric and studio
docker compose --profile monitoring up --build     # adds Prometheus and Grafana
```

Workers and Ollama stay native for direct GPU access, so run node agents on the GPU hosts. Set `CF_ADMIN_PASSWORD`, because requests through Docker's port mapping do not come from localhost. The containers run as an unprivileged user (UID 1000), so on Linux make sure `./data` is writable by that user. The monitoring profile adds Prometheus on `127.0.0.1:9090` and Grafana on `127.0.0.1:3000` with a provisioned CinemaFabric dashboard (its admin password comes from `GRAFANA_ADMIN_PASSWORD`); the fabric serves metrics at `/metrics` to holders of the fabric token.

## Configuration

**Environment** (see [.env.example](.env.example)):

| Variable | Purpose |
|---|---|
| `CF_MODE` | `live` or `mock` |
| `MODEL_API_KEY` | Meta Model API key |
| `CF_PROFILE` | Path to a profile that overrides the defaults |
| `CF_JOIN_TOKEN`, `CF_FABRIC_TOKEN` | Tokens for joining machines and for full fabric access; generated if empty |
| `CF_ADMIN_PASSWORD` | Allows settings changes from machines other than this one |
| `CF_C2PA_CERT`, `CF_C2PA_KEY` | Your own certificate for Content Credentials; a local certificate is used otherwise |
| `CF_ACCOUNTS`, `CF_SIGNUP` | Optional sign-in. The first account becomes the admin; `open` lets anyone sign up |
| `CF_USER_DAILY_USD`, `CF_USER_MAX_ACTIVE` | Each person's daily cloud allowance and films in progress |
| `CF_DATABASE_URL` | Postgres instead of SQLite, so several fabric and studio copies can share state |
| `CF_MEDIA_BUCKET`, `CF_S3_ENDPOINT` | Copy finished films to S3 or any S3-compatible store |
| `CF_PARALLEL_PRODUCTIONS` | Films a studio makes at once |

**Profile** (TOML, merged over [`cinemafabric/profiles/default.toml`](cinemafabric/profiles/default.toml)):

| Section | Controls |
|---|---|
| `[cloud]` | Which Meta Model API models fill each role |
| `[budget]` | The starting spending limit |
| `[licences]` | Licences the router may use |
| `[models]` | Model families a node agent may offer from Ollama |
| `[safety]` | `moderation` (`required`, `best_effort` or `off`) and the Llama Guard categories that stop a film |
| `[style]` | Writing guidance: overused names, title patterns and phrases to avoid |
| `[node]` | Memory kept free per device and the first worker port |
| `[[machines]]` | Fixed machines: label, kind, URL, model, quality, vision |

Facts about local models (memory, devices, licence) are in [`cinemafabric/catalog.toml`](cinemafabric/catalog.toml).

## Security

- **Fabric API.** Every `/v1` call requires the fabric token, which the studio holds and the browser never sees. The join token can only read the policy and register machines.
- **Studio settings.** Changes to machines, the API key and spending limits are accepted from localhost only, or from anywhere with `CF_ADMIN_PASSWORD`. Cross-site requests are refused.
- **Workers** generate a secret when they join and refuse calls without it.
- **Ollama** has no authentication of its own, so it stays on localhost behind a small gate that requires a secret and passes inference calls only.
- **Machine addresses** that point at link-local, multicast or reserved ranges are refused.
- **Accounts** (optional) use scrypt-hashed passwords and server-side sessions in an HttpOnly cookie, and each person sees only their own films and files.
- **Secrets at rest**, including token files, databases and the signing key, are readable by their owner only. The services speak plain HTTP; put them behind a TLS reverse proxy before exposing them beyond a trusted network.

The code was reviewed in five rounds with Muse Code. Every finding and its outcome is in [docs/reviews.md](docs/reviews.md).

## API

**Fabric** (`:8100`; every route requires the fabric token unless noted):

| Endpoint | Purpose |
|---|---|
| `POST /v1/tasks` | Run a task through the router |
| `GET /v1/workers`; `POST /v1/workers/{id}/disable`, `/enable` | Registry snapshot; take a worker out of or back into rotation |
| `GET /v1/ledger`, `GET /v1/ledger/jobs/{job_id}` | Spend overall and per film |
| `GET /v1/decisions` | Routing log |
| `GET /v1/policy`, `POST /v1/machines` | Policy for node agents and machine registration (join token accepted) |
| `POST /v1/machines/inspect`, `DELETE /v1/machines/{id}` | Inspect or remove a machine |
| `PUT /v1/ledger/limit` | Set or clear the overall spending limit |
| `GET /v1/settings`, `PUT /v1/settings/api-key` | Show settings or replace the API key, which is never returned |
| `GET /metrics` | Prometheus metrics |

**Studio** (`:8000`):

| Endpoint | Purpose |
|---|---|
| `GET /api/options`, `GET /api/estimate` | Form choices and cost estimate |
| `POST /api/jobs`, `GET /api/jobs/{id}` | Start a film and follow its progress |
| `POST /api/jobs/{id}/retry`, `/approve` | Resume a failed film from cache; continue after script review |
| `POST /api/jobs/{id}/shots/{n}/redo` | Regenerate one shot and re-cut |
| `POST /api/jobs/{id}/director`, `/director/apply`, `/director/discard` | Plan, apply or discard a director's note |
| `POST /api/jobs/{id}/ending`, `/next`, `/commentary` | Another ending, the next episode, a director's commentary |
| `POST /api/jobs/{id}/export/{vertical\|widescreen\|square}` | A platform version |
| `POST /api/jobs/{id}/share`; `GET /watch/{share}`, `/embed/{share}` | Public link, watch page and embed player |
| `POST /api/report/{share}`; `GET /api/reports` | Report a shared film; list reports (admin) |
| `GET /api/suggest/ideas`; `POST /api/suggest/polish`, `/settings` | Model-written ideas, idea polishing and suggested settings |
| `GET /api/characters`; `POST /api/jobs/{id}/cast/{n}/save`; `DELETE /api/characters/{id}` | The character library |
| `GET /api/auth/me`; `POST /api/auth/sign-up`, `/sign-in`, `/sign-out`; `GET`/`POST /api/people` | Accounts, when enabled |

Interactive API documentation is served at `/docs` on both services.

## Project layout

```
cinemafabric/
  config.py, profile.py   settings, profile and catalogue loading
  profiles/default.toml   default profile
  catalog.toml            facts about local models
  fabric/                 router, registry, ledger, cache, machines, metrics, API, client
    backends/             Meta Model API, Ollama, local worker, CPU fallback, mock
  node/                   node agent: discovery, planning, worker supervision, Ollama gate
  worker/                 local model worker: voice, music, depth, cutouts, watermark
  studio/                 pipeline, agents, director, originality, commentary, prompts, API
    media/                renderer, audio mix, typography, provenance, credentials, exports
  tools/                  architecture diagrams, evaluation
frontend/                 React, Vite and Tailwind interface
scripts/                  development launchers, Ollama gate, end-to-end smoke test
tests/
```

## Tests

```bash
uv run pytest                     # unit and integration tests
uv run python scripts/smoke.py    # end-to-end mock production
./scripts/check.sh                # everything, in clean Linux containers; needs only Docker
```

`check.sh` installs from the lock file, lints, runs the tests and a mock film on SQLite and on Postgres, builds the interface and the image, and starts the whole compose stack on spare ports. It leaves your own environment and running services alone.

The smoke test makes several films with every feature switched on, including the agent crew, director notes, another ending, the next episode and a commentary, and checks the results and the contents of each download. The content rules are covered by the unit tests and by the [evaluation](docs/evals.md).

## Known limitations

- Motion is a depth-aware camera move over a still frame, so people do not move within a shot.
- Each language has one MMS-TTS voice, pitched differently for each character, rather than separate voices.
- Picture checks rely on a vision model's judgement; there is no reference database of existing images to compare against. Music is not fingerprinted against existing recordings.
- Resolutions above 1080p are upscaled and carry no additional detail.
- Several studio copies can share Postgres and a media bucket, but each runs its own productions; there is no shared job queue yet.
- An API key saved through the interface is stored, unencrypted, in the fabric's owner-only database, as it would be in `.env`.

## Roadmap

- Llama Guard 4 vision on frames and Prompt Guard 2 on inputs, once access is granted
- A shared job queue so any studio copy can pick up any production
- Generated video in place of camera moves

## Licence

[MIT](LICENSE). Model weights and APIs are governed by their own licences; see [Meta models used](#meta-models-used) and [NOTICE](NOTICE).
