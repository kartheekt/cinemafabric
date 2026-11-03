# Code reviews

CinemaFabric is reviewed with **Muse Code**, Meta's coding agent, running read-only on a clean copy of the
repository (no `.env`, data or local files). Each finding is checked against the code before anything changes.
The table records what was found and what happened to it.

```bash
muse exec --prompt-file review.md --output-schema findings.json --disable-write --disable-web-tools \
  --approval-mode never --json
```

| Round | Scope | Findings | Outcome |
|---|---|---|---|
| 1 | Security, guardrails, spending and licensing | 20 | 17 fixed, 3 partly fixed with the remainder by design |
| 2 | Verification of the round 1 fixes, and regressions | 7 follow-ups, 10 new | Follow-ups resolved or accepted by design; 9 new findings fixed, 1 needed no change |
| 3 | Code quality and tests | 16 | All real, all fixed |
| 4 | Accounts, sharing, exports, the director agent and media features | 17 | All fixed |
| 5 | Content safeguards and the agent crew | 24 | 22 fixed, 2 partly fixed with the remainder by design |

## Round 1: security, guardrails, spend and licensing

20 findings: 17 fixed and 3 partly fixed, with the rest by design or out of scope, as noted below.

| # | Severity | Finding | Outcome |
|---|---|---|---|
| 1 | Critical | The studio forwarded admin calls (machines, API key, limit) for anyone and showed the full join token | **Fixed.** Admin routes work from localhost only, or anywhere with `CF_ADMIN_PASSWORD`; cross-site requests are refused. The studio uses its own fabric token, which never reaches the browser |
| 2 | Critical | Fabric tasks, workers, ledger and decisions needed no token | **Fixed.** Every `/v1` route needs `X-Fabric-Token` |
| 3 | Critical | Add-machine URLs allowed SSRF and rogue workers | **Partly fixed.** Link-local (cloud metadata), multicast and reserved addresses, URL credentials and query strings are refused, and inspect/add need a token. LAN and loopback stay allowed because that's where machines live. A node holding the join token is trusted to run tasks |
| 4 | High | SPA fallback path traversal | **Fixed, confirmed real:** `/%2e%2e/%2e%2e/.env` resolved outside the UI build. Paths are now resolved and must stay inside it |
| 5 | High | Worker endpoints unauthenticated, payloads unbounded | **Fixed.** A joining worker makes a random secret, registers it, and refuses calls without it. Field limits on the worker; 64 MB body cap on the fabric |
| 6 | High | Script edits and shot redos skipped moderation | **Fixed.** The edited script is re-checked after review; redo text is checked before drawing |
| 7 | High | Moderation fails open, truncates at 6000 chars, ignores `safe`, skips images | **Partly fixed.** Text is checked in full in chunks; an unsafe verdict without a category now blocks. Fail-open is the documented `best_effort` default (the run shows the check as skipped; `required` makes it fail closed). Pictures rely on Muse Image's own content policy; local image moderation is on the roadmap |
| 8 | High | Cache replayed results across licence changes | **Fixed.** Entries record the licence that produced them and are skipped if it's no longer allowed |
| 9 | High | Prompt injection through stages; `genre` not validated | **Fixed** for genre (allowlist) and the idea (fenced as data, and the writer is told it isn't instructions). Later stages only see schema-checked JSON, and edits are re-moderated |
| 10 | High | Reservations under-counted reasoning tokens; transcription trusted the caller's duration | **Fixed.** Reservations use the real output cap (answer plus reasoning budget), so a limit can't be passed; transcription is priced from the audio itself |
| 11 | High | Removing a worker raced in-flight tasks | **Fixed.** Recording a result for a removed worker raised `KeyError` and failed a finished task; it no longer does. No reservation could leak, since only cloud calls reserve and cloud workers can't be removed |
| 12 | Medium | Unbounded job queue, approval list and fields | **Fixed.** 20 waiting runs, 200 shots, per-field caps |
| 13 | Medium | Plaintext API key, default file permissions, no TLS | **Partly fixed.** Token files and databases are owner-only. The key stays in the owner-only database, like other local credential stores. TLS is left to a reverse proxy (documented) |
| 14 | Medium | One join token for everything, passed in argv | **Fixed.** Split into a fabric token (full access) and a join token (policy and registration only), passed through the environment |
| 15 | Medium | A cancelled task counted as a worker failure | **Fixed.** Cancellation now releases the cloud reservation. A cancelled task never counted as a worker failure, since `CancelledError` isn't an `Exception` |
| 16 | Medium | Sibling tasks kept running (and spending) after one failed | **Fixed.** Parallel stages use a TaskGroup helper that cancels the rest |
| 17 | Medium | SAM 3 could load under an Apache-only policy | **Fixed.** The node agent passes workers only the segmentation models the policy allows |
| 18 | Medium | Ollama licence matched by substring; families not enforced on add | **Fixed.** Names match by segment (`llama3.1` yes, `notllama` no) and the family policy is enforced when a machine is added |
| 19 | Low | Cache temp-file collisions | **Fixed.** Unique temp names |
| 20 | Low | Poster credits hard-coded; export had no licence notice | **Fixed.** Credits come from the routing log; every export includes `CREDITS.txt` with the models used and the notices their licences require |

New tests for these cover token scopes, traversal, the address check, the admin gate, chunked moderation,
redo moderation, cache licences, cancellation of siblings, worker removal, estimates and credits.

## Round 2: checking the round 1 fixes

Muse Code re-read the code for each round 1 finding, then looked for bugs the fixes introduced. It ran in two
focused passes (findings 1–10, then 11–20 plus new issues), each told to cite only files it had opened.

**Verification:** 13 fixed. 7 partly fixed; what remained:

| # | What was left | Outcome |
|---|---|---|
| 7 | Moderation still fails open under `best_effort`; pictures aren't checked locally | By design and configurable (`required`); local image moderation is on the roadmap |
| 12 | Retry and redo skipped the queue cap; some machine fields unbounded | **Fixed** |
| 13 | Existing token files kept old permissions; SQLite WAL files not restricted | **Fixed** |
| 14 | `--token` on the command line still accepted | Kept for compatibility, now with a warning; the documented path and the node use the environment |
| 18 | A model's name, not the server-reported family, decides its family and licence | By design: whoever adds a machine controls its Ollama server, and adding needs admin access |
| 20 | Credits used only the newest 1000 decisions and looked licences up from live workers | **Fixed.** Each routing-log row now records the licence; credits read up to 10,000 rows per job |
| 11 | A few registry lookups still index by id directly | Not changed: those call sites run synchronously over the current worker list |

**New findings:** 10; 9 fixed, and 1 needed no change.

| Severity | Finding | Outcome |
|---|---|---|
| High | Optional stages swallow cancellation | **No change needed.** `CancelledError` isn't an `Exception`, so `running()` never catches it |
| Medium | A blocked chunk was forgotten if a later chunk's check failed; a redo with no checker available said nothing | **Fixed.** Verdicts so far are applied first; the skipped check is noted on the safety stage |
| Medium | A redo cut out a "person" from frames where the original run made no cutout | **Fixed.** Uses the same subject rule as the original run |
| Medium | A failed cache write turned a paid, successful call into an error | **Fixed.** Cache failures are logged, and the result is returned |
| Medium | Transcription estimate floored the byte rate, under-reserving low-sample-rate audio | **Fixed.** Uses the header's rate, channels and bit depth |
| Medium | `limit=-1` on the decisions log removed the row cap | **Fixed** |
| Medium | Chunked uploads bypassed the body-size check | **Fixed.** The body is counted as it streams |
| Medium | Retry and redo bypassed the queue cap | **Fixed** |
| Medium | Credits depend on live workers and the newest 1000 decisions | **Fixed** (see 20 above) |
| Low | A failed redo left the Frames stage showing "running" | **Fixed** |

## Round 3: code quality and tests

Two focused runs: the routing layer (fabric, node, worker), and the studio and web UI. Each was asked for correctness
bugs, misleading or dead code, missing tests and unhelpful errors. All 16 findings were checked against the code.
All were real and all are fixed, with a test for each where one makes sense.

| Severity | Finding | Outcome |
|---|---|---|
| High | A request that crashed a backend (e.g. a missing field) counted as a worker outage; two in a row took a healthy worker offline for 30 s | **Fixed.** Only transport and server errors trip the circuit; unexpected exceptions are logged without it |
| High | A malformed cache entry made every identical request fail | **Fixed.** Unreadable entries are treated as a miss |
| High | A worker kept advertising a capability whose model had failed to load | **Fixed.** Load failures show up in the worker's health, and the router skips that capability with the reason |
| High | Per-job spend and receipts mixed mock and live runs | **Fixed.** Both filter by mode |
| High | A failed or interrupted run left a stage showing "running" forever | **Fixed.** Interrupted stages are marked failed on restart |
| High | One malformed job record broke the whole job list | **Fixed.** Bad records are skipped |
| High | A frame check that returned nothing counted as a pass; a low score with no stated issue was never redone | **Fixed.** No score means not reviewed; any low score is redone |
| Medium | A stop limit without a budget wasn't enforced; "remaining" ignored the stop | **Fixed.** The lower of the two set limits is enforced and reported |
| Medium | Recorded latency included time spent queued, which was then added again when estimating | **Fixed.** Latency is measured from when the worker starts the task |
| Medium | If every candidate went down between ranking and running, the error said nothing | **Fixed.** The reason is recorded and shown |
| Medium | Transcripts repeated words when the response had both a full transcript and per-segment text | **Fixed.** The shallowest transcript in the response is used |
| Medium | The "saved vs. cloud" figure priced models the licence policy or vision needs would rule out | **Fixed** |
| Medium | Approving a script twice applied the second, possibly stale, edit | **Fixed.** The first approval moves the run on; a second is refused |
| Medium | Each redo wrapped the frame prompt again, so prompts grew with every redo | **Fixed.** Redos start from the shot's own prompt |
| Medium | The downloadable script listed shots that were dropped from the film | **Fixed.** It lists exactly the shots in the film |
| Medium | A blocked script left the content check showing "done" | **Fixed.** The stage shows which check blocked the run |

## Asking Muse Code about Meta's products

Muse Code also answered questions about the Meta Model API that shaped the design, with sources. The answers that
changed the code were checked against Meta's docs before use.

| Question | Answer | What we did |
|---|---|---|
| Prices | Muse Image is $0.01 per image at any size, and failed or filtered images are free; cached input tokens cost 88–98% less ([pricing](https://dev.meta.ai/docs/pricing-rate-limits)) | Cached tokens are now priced at the cached rate; the other prices already matched |
| Labelling generated media | The terms make labelling the app's job | We label every film, poster and frame ourselves: visible text, IPTC metadata, an AudioSeal watermark and signed C2PA Content Credentials |
| Rate limits | Muse Image and contributor models: 150 requests a minute per team | Within what the fabric sends |

## Round 4: accounts, sharing, the director agent and media features

Two focused runs over the new code: accounts, the Ollama gate, Postgres and S3, sharing, exports and metrics; then the
director agent, moderation, subtitles, voices, watermarking and the new UI. 17 findings, all checked against the code and
fixed.

| Severity | Finding | Outcome |
|---|---|---|
| Critical | The second opinion only read the first 8,000 characters of flagged text, so a harmful passage later on could be cleared along with the rest | **Fixed.** Each flagged part gets its own review; any harmful part blocks, and a missing review keeps Llama Guard's verdict |
| High | The title, tagline, logline and the expanded picture prompts were never content-checked | **Fixed.** The story is checked with the script, and the picture prompts before any picture is made |
| High | A director note that failed halfway left edited text, half-redrawn frames and a "failed" film, and hid what it had spent | **Fixed.** Notes apply all-or-nothing: files and text are restored, the film stays as it was, and the attempt's cost is recorded |
| High | Shot prompts numbered from 0, but a model answering from 1 shifted every prompt onto the wrong shot | **Fixed.** Both numberings are recognised; anything else falls back to order |
| High | Media of a shared film was reachable by job id, including the ZIP and script | **Fixed.** Job ids are random and only revealed by the share link, but anonymous access is now limited to what the watch page plays: video, pictures and caption tracks |
| High | Character sheets in the library could be fetched by another signed-in person who knew the file name | **Fixed.** Sheets are served only to their owner |
| Medium | The director's cost estimate left out planning | **Fixed.** Planning cost is counted in each note; the estimate now says it covers new pictures, since voice and music run locally |
| Medium | Subtitle checks accepted English with a dash in it as Hindi, and rejected short lines that are the same in both languages (names) | **Fixed.** The script check counts letters in the language's own Unicode block, and only lines of three or more words count as untranslated |
| Medium | A connection error on the first translation attempt skipped the cloud retry, and a language could vanish silently | **Fixed.** Any failure goes to the cloud retry; languages that still fail are listed under the downloads |
| Medium | A third returning character never got their saved sheet | **Fixed.** Every returning character keeps their sheet; new characters are still capped at two sheets |
| Medium | A director picture edit dropped the shot's characters unless their names appeared in the new description | **Fixed.** Names match as whole words, and if the description names nobody, the cast stays |
| Medium | "a 7-year-old boy" was voiced as elderly (the age check matched stray digits) | **Fixed.** Ages are read properly; children under 13 get a child's voice |
| Medium | A malformed watermark reply could fail the whole edit | **Fixed.** The whole watermark step is best-effort |
| Medium | Redrawn frames were never checked again | **Fixed.** A redrawn frame gets one more check, and its new score is stored |
| Medium | Switching films briefly showed the previous film, whose buttons still acted on it | **Fixed.** Polled data is cleared when the address changes |
| Low | "Polish my idea" overwrote text typed while it was working | **Fixed.** The rewrite only applies if the idea is unchanged; otherwise it's offered as a note |
| Low | The film has no XMP metadata despite the UI saying so; duplicate `-movflags` dropped fast start | **Fixed.** The UI and README now say the film carries the label in its tags (pictures carry XMP), and the duplicate flag is removed. Fast start was unaffected, since ffmpeg's `+flag` adds to the flags already set |

## Round 5: content safeguards and the agent crew

Two focused runs, each on a clean snapshot of the repository. The first covered the originality and
picture checks, sharing and reports, and Content Credentials; the second covered the agent crew and the wiring between
the interface, the API and the pipeline. Muse Code used Muse Wonder for these runs. 24 findings, all checked against
the code before anything changed; every fix has a test.

**Content safeguards**

| Severity | Finding | Outcome |
|---|---|---|
| Critical | Shots redrawn after the first run (a redo, a director note, another ending) skipped the picture check | **Fixed.** Every redraw goes through the same check, two stricter redraws, and the old frame is restored if it still fails; a director note then rolls back in full |
| Critical | The share gate was applied once; a shared film could change afterwards and stay public | **Fixed.** The gate is applied whenever the watch page, embed or report form is served. Reports do not take a film down automatically, since anyone could abuse that; the owner or an admin decides |
| Critical | A required stop in the optional character-sheet stage was swallowed | **Fixed.** A content stop always propagates, and the picture record is written before it does |
| Critical | Required mode let unchecked pictures and stories through when no checker answered or the answer was empty | **Fixed.** In required mode an unanswered or incomplete check stops the film; frames whose check fails are recorded as not checked |
| High | A contradictory idea verdict ("not original" with no problems, or an empty answer) passed | **Fixed.** Only a consistent "original" verdict passes; an empty answer is recorded as not checked, and blocks in required mode |
| High | The story check never saw the tagline, setting, logline or beats, and fixed a repeated quoted line only once | **Fixed.** Every story field is sent and rewritten, every copy of a line is replaced, and issues that match nothing are reported rather than passed |
| High | Replacement names and lines were not checked again | **Fixed.** The check takes a second look after making changes |
| Medium | A flagged shot that was later redrawn cleanly kept the film from being shared | **Fixed.** A clean redraw clears its shot's flag, and the check is marked resolved |
| High | A failed re-sign left the old "signed" record, and the watch page claimed the film was signed | **Fixed.** A failed signature removes the record, and the watch page relies on a verified signature of the current file |
| High | A crash while creating the local certificate could leave a mismatched pair that broke all later signing | **Fixed.** The pair is written atomically, checked on load and regenerated if damaged |
| High | Report limits were counted after the lookup, and the per-film cap dropped reports silently | **Partly fixed.** Attempts are counted before the lookup and the reply says when a report was not recorded. Visitors behind one shared address still share a limit, which we accept |
| Medium | Platform versions were signed with the job id as the title, and two exports could race | **Fixed.** The film is restored before signing, the job stays locked until signing and packaging finish, and signing uses a unique temporary file |

**Agent crew and wiring**

| Severity | Finding | Outcome |
|---|---|---|
| High | A longer line from a director note pushed later lines onto the wrong shots | **Fixed.** The shot is lengthened to fit its new line |
| High | A failed music change was reported as applied | **Fixed.** A failed rescore rolls the whole note back |
| High | Tidying the script after the script doctor undid some of its edits while the log still listed them | **Fixed.** The log is reconciled with the final script |
| Medium | The editor could not fit lines longer than 5.5 seconds | **Fixed.** A voiced shot may run to 8 seconds |
| Medium | The download kept a stale screenplay, release copy and thumbnail after a re-cut | **Fixed.** The screenplay is rewritten and the release copy regenerated when the title changes |
| Medium | A retried film's download could include frames and character sheets from the failed attempt | **Fixed.** Leftover files are removed before frames and sheets are drawn |
| Medium | A failed director note rolled back the audio but not the translated voice-over text | **Fixed.** The rollback includes it |
| Medium | Another ending ignored the person's remaining daily allowance | **Fixed.** It is limited like any other film |
| Medium | The continuity check looked at 11 appearances per character but reported that every one matched | **Fixed.** It checks every appearance in batches and reports how many it checked |
| Low | A film's trailer spoke more lines than intended | **Fixed.** Only the chosen lines are spoken |
| Low | The continuity agent's cost counted only its last call | **Fixed.** All calls are counted |
| Low | Switching films kept the previously selected version in the player | **Fixed.** Each film opens on its main cut |
