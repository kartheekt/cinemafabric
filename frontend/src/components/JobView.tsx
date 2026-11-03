import { useState } from "react";
import { api, usd, usePoll } from "../api";
import type { AgentTurn, Decision, Job, Option, ShotState, Stage } from "../types";
import { DecisionRow } from "./FabricPanel";
import DirectorPanel from "./DirectorPanel";
import FabricLive from "./FabricLive";
import MachineTimeline from "./MachineTimeline";
import SafetyPanel from "./SafetyPanel";
import SharePanel from "./SharePanel";
import ScriptReview from "./ScriptReview";

const STAGE_ICON: Record<Stage["status"], string> = {
  pending: "○",
  running: "◐",
  done: "●",
  skipped: "◌",
  failed: "✕",
};
const STAGE_COLOR: Record<Stage["status"], string> = {
  pending: "text-muted",
  running: "text-gold pulse",
  done: "text-good",
  skipped: "text-muted",
  failed: "text-bad",
};

function elapsed(s: Stage) {
  if (!s.started) return "";
  const end = s.finished ?? Date.now() / 1000;
  const t = end - s.started;
  return t < 60 ? `${t.toFixed(0)}s` : `${Math.floor(t / 60)}m${Math.round(t % 60)}s`;
}

function Pipeline({ stages }: { stages: Stage[] }) {
  const done = stages.filter((s) => s.status === "done" || s.status === "skipped").length;
  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <span className="label">Steps</span>
        <span className="text-xs text-muted">
          {done}/{stages.length}
        </span>
      </div>
      <div className="mb-3 h-1 overflow-hidden rounded-full bg-line">
        <div className="h-full bg-gold transition-all" style={{ width: `${(done / stages.length) * 100}%` }} />
      </div>
      <ol className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
        {stages.map((s) => (
          <li key={s.id} className="flex min-w-0 items-baseline gap-2 text-sm">
            <span className={`w-3 shrink-0 text-center ${STAGE_COLOR[s.status]}`}>{STAGE_ICON[s.status]}</span>
            <span className={s.status === "pending" ? "text-muted" : ""}>{s.label}</span>
            <span className="min-w-0 truncate text-xs text-muted" title={s.detail}>
              {s.detail}
            </span>
            <span className="ml-auto shrink-0 font-mono text-[11px] text-muted">{elapsed(s)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function RedoForm({ jobId, shot, onDone }: { jobId: string; shot: ShotState; onDone: () => void }) {
  const [visual, setVisual] = useState(shot.visual);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/jobs/${jobId}/shots/${shot.index}/redo`, { visual, note });
      onDone();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <div className="space-y-1.5 border-t border-line p-2">
      <textarea
        aria-label="What the camera sees"
        rows={2}
        value={visual}
        onChange={(e) => setVisual(e.target.value)}
        className="w-full resize-y rounded border border-line bg-ink px-1.5 py-1 text-[11px]"
      />
      <input
        aria-label="What should change"
        value={note}
        placeholder="What should change? (optional)"
        onChange={(e) => setNote(e.target.value)}
        className="w-full rounded border border-line bg-ink px-1.5 py-1 text-[11px]"
      />
      {error && <p className="text-[11px] text-bad">{error}</p>}
      <button type="button" onClick={submit} disabled={busy} className="w-full rounded bg-gold py-1 text-[11px] font-semibold text-ink disabled:opacity-40">
        {busy ? "Starting…" : "Redo this shot and re-cut"}
      </button>
    </div>
  );
}

function ShotCard({ shot, aspect, jobId, canRedo, onRedo }: { shot: ShotState; aspect: string; jobId: string; canRedo: boolean; onRedo: () => void }) {
  const [redoing, setRedoing] = useState(false);
  const ratio = aspect === "9:16" ? "aspect-[9/16]" : aspect === "1:1" ? "aspect-square" : aspect === "2.39:1" ? "aspect-[2.39/1]" : "aspect-video";
  return (
    <figure className="group relative overflow-hidden rounded-lg border border-line bg-ink">
      {canRedo && !shot.title_card && shot.image && (
        <button
          type="button"
          onClick={() => setRedoing((r) => !r)}
          aria-label={`Redo shot ${shot.index + 1}`}
          className={`absolute right-1.5 top-7 z-10 min-h-6 rounded bg-black/80 px-2 py-0.5 text-[11px] transition-opacity focus:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100 ${
            redoing ? "opacity-100" : "opacity-0"
          }`}
        >
          {redoing ? "close" : "redo"}
        </button>
      )}
      <div className={`relative ${ratio} film-grain`}>
        {shot.title_card ? (
          <div className="flex h-full items-center justify-center bg-black p-2 text-center font-display text-sm tracking-[0.2em]">
            {shot.title_card}
          </div>
        ) : shot.image ? (
          <img src={shot.image} alt={shot.visual} loading="lazy" className="h-full w-full object-cover" />
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-muted">
            <span className="pulse">rendering…</span>
          </div>
        )}
        <span className="absolute left-1.5 top-1.5 rounded bg-black/70 px-1.5 py-px font-mono text-[10px]">
          {String(shot.index + 1).padStart(2, "0")} · {shot.duration.toFixed(1)}s
        </span>
        <div className="absolute right-1.5 top-1.5 flex gap-1">
          {shot.mask && <span className="rounded bg-sky/80 px-1.5 py-px text-[10px] text-ink" title="Subject outlined with SAM to shape the camera move">SAM</span>}
          {shot.review && (
            <span
              className={`rounded px-1.5 py-px text-[10px] ${shot.review.regenerated ? "bg-gold text-ink" : "bg-black/70"}`}
              title={shot.review.issue || "Passed the frame check"}
            >
              {shot.review.regenerated ? "↻ " : "★ "}
              {shot.review.score}/10
            </span>
          )}
        </div>
      </div>
      <figcaption className="space-y-1 p-2 text-[11px] leading-snug">
        {!shot.title_card && (
          <div className="flex items-center gap-1.5 text-muted">
            <span className="rounded border border-line px-1">{shot.camera.replace("_", " ")}</span>
            <span className="line-clamp-2">{shot.visual}</span>
          </div>
        )}
        {shot.narration && <p className="italic text-text/80">“{shot.narration}”</p>}
      </figcaption>
      {redoing && (
        <RedoForm
          jobId={jobId}
          shot={shot}
          onDone={() => {
            setRedoing(false);
            onRedo();
          }}
        />
      )}
    </figure>
  );
}

function CastMember({ jobId, index, c }: { jobId: string; index: number; c: Job["cast"][number] }) {
  const [saved, setSaved] = useState(Boolean(c.returning));
  return (
    <figure className="w-32 shrink-0">
      <img src={c.image} alt={c.look} className="aspect-square w-full rounded-lg border border-line object-cover" />
      <figcaption className="mt-1 flex items-center justify-between gap-1 text-xs">
        <span className="truncate">{c.name}</span>
        {saved ? (
          <span className="text-muted">{c.returning ? "returning" : "saved"}</span>
        ) : (
          <button
            type="button"
            onClick={() => api.post(`/api/jobs/${jobId}/cast/${index}/save`).then(() => setSaved(true), () => undefined)}
            aria-label={`Save ${c.name} to your character library`}
            className="min-h-6 text-gold hover:underline"
          >
            save
          </button>
        )}
      </figcaption>
    </figure>
  );
}

function runtimeLabel(job: Job) {
  if (job.runtime?.decided_by === "pending") return `${job.options.format === "movie" ? "Movie" : "Trailer"}, length to be decided`;
  if (job.runtime) return job.runtime.label;
  return job.options.duration === "movie120" ? "2-minute movie" : "30-second trailer";
}

function Receipt({ job }: { job: Job }) {
  const r = job.receipt;
  if (!r) return null;
  const total = r.local_tasks + r.cloud_tasks;
  return (
    <div className="rounded-xl border border-gold/30 bg-gold/5 p-4">
      <div className="label text-gold/90">Cost</div>
      <p className="mt-2 font-display text-lg leading-snug">
        <span className="text-gold">{usd(r.cost_usd, 4)}</span> total.
        {total > 0 && (
          <>
            {" "}
            {r.local_tasks} of {total} tasks ran locally
          </>
        )}
        {r.cache_hits > 0 && <>, {r.cache_hits} reused from cache</>}
        {r.rerouted > 0 && <>, {r.rerouted} retried after a failure</>}.
      </p>
      <ul className="mt-3 grid gap-1 text-xs sm:grid-cols-2">
        {r.by_worker.map((w) => (
          <li key={w.worker_id} className="flex justify-between gap-2 rounded bg-ink/50 px-2 py-1">
            <span className="truncate">{w.worker_label}</span>
            <span className="shrink-0 font-mono text-muted">
              {w.tasks} · {usd(w.cost_usd)}
            </span>
          </li>
        ))}
      </ul>
      {r.avoided_usd > 0 && (
        <p className="mt-2 text-xs text-muted">
          All-cloud, this would have cost about <span className="text-good">{usd(r.avoided_usd)}</span> more.
        </p>
      )}
    </div>
  );
}

const AGENT_ROLE: Record<string, string> = {
  "Story editor": "Rewrites the short film's whole voice-over as one piece, so that it flows and grips",
  "Script doctor": "Scores the script, then rewrites lines, pictures, timing and camera moves",
  "Continuity supervisor": "Compares every frame with the character's reference sheet; redraws the ones that drifted",
  Editor: "Sets shot lengths and camera moves once the voice-over is recorded",
  "Test screening": "Watches the cut like an audience; sends a note to the director if it falls short",
  Director: "Turns a note into a few checked edits and re-cuts only what changed",
  "Release writer": "YouTube and Instagram copy, and the thumbnail",
};

function AgentCrew({ turns, release }: { turns: AgentTurn[]; release: Job }) {
  const spent = turns.reduce((n, t) => n + t.cost_usd, 0);
  return (
    <details className="rounded-xl border border-line p-4" open>
      <summary className="label cursor-pointer">
        Agent crew · {turns.length} turns · {usd(spent, 4)}
      </summary>
      <ol className="mt-3 space-y-3 text-xs">
        {turns.map((t, i) => (
          <li key={i} className="border-l-2 border-gold/50 pl-3">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-sm font-semibold text-text">{t.agent}</span>
              <span className="font-mono text-muted">
                {t.score != null && (
                  <span className={t.score >= 8 ? "text-good" : "text-gold"}>
                    {t.score}/10{t.after != null ? ` → ${t.after}/10` : ""}
                  </span>
                )}
                {t.cost_usd > 0 && ` · ${usd(t.cost_usd, 4)}`}
              </span>
            </div>
            {AGENT_ROLE[t.agent.split(" (")[0]] && <p className="text-muted">{AGENT_ROLE[t.agent.split(" (")[0]]}</p>}
            <p className="mt-1 text-text">{t.summary}</p>
            {t.changes.length > 0 && (
              <ul className="mt-1 list-disc pl-4 text-muted">
                {t.changes.map((c, k) => (
                  <li key={k}>{c}</li>
                ))}
              </ul>
            )}
            {t.by && <p className="mt-0.5 text-muted/80">{t.by}</p>}
          </li>
        ))}
      </ol>
      {release.release && (
        <div className="mt-3 rounded-lg bg-panel-2/60 p-3 text-xs">
          <div className="label mb-1">Release copy</div>
          <p className="text-sm text-text">{release.release.youtube_title}</p>
          <p className="mt-1 whitespace-pre-line text-muted">{release.release.youtube_description}</p>
          <p className="mt-1 text-gold">{release.release.hashtags.map((h) => `#${h.replace(/[^\p{L}\p{N}]/gu, "")}`).join(" ")}</p>
        </div>
      )}
    </details>
  );
}

const PREFERENCE: Record<string, string> = { local_first: "Local first", balanced: "Balanced", cloud_first: "Cloud first" };

function Settings({ job }: { job: Job }) {
  const o = job.options;
  const chips = [
    o.resolution && `${o.resolution}${o.fps ? ` · ${o.fps} fps` : ""}`,
    PREFERENCE[o.preference] ?? o.preference,
    !o.narration
      ? "No voice-over"
      : o.commercial_safe
        ? "Voice-over as captions"
        : o.voice_language && o.voice_language !== "en"
          ? `Voice-over: ${o.voice_language.toUpperCase()}`
          : "Voice-over: English",
    o.subtitle_languages?.length ? `Subtitles: ${o.subtitle_languages.map((l) => l.toUpperCase()).join(", ")}` : "",
    (o.premium_script || o.format === "movie") && "Premium script",
    o.critic && "Frame critic",
    o.agents && "Agent crew",
    o.commercial_safe && "Commercial-safe",
    o.review_script && "Script review",
    o.max_cost_usd != null && `Limit $${o.max_cost_usd}`,
    o.cast_ids?.length ? `${o.cast_ids.length} returning cast` : "",
  ].filter(Boolean) as string[];
  return (
    <ul className="mt-1.5 flex flex-wrap gap-1" aria-label="Settings for this film">
      {chips.map((c) => (
        <li key={c} className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted">
          {c}
        </li>
      ))}
    </ul>
  );
}

function OriginalVersion({ job, onOpen }: { job: Job; onOpen: (id: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const o = job.originality!;
  const start = async () => {
    setBusy(true);
    setError("");
    try {
      const made = await api.post<Job>("/api/jobs", { ...job.options, prompt: o.rewrite });
      onOpen(made.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-3 rounded-lg border border-gold/40 bg-gold/5 p-3 text-sm">
      <p className="font-semibold text-gold">This idea builds on someone else's work, so nothing was made.</p>
      <ul className="mt-1 list-disc pl-5 text-muted">
        {o.problems.map((p) => (
          <li key={p}>{p}</li>
        ))}
      </ul>
      {o.rewrite && (
        <>
          <p className="mt-2 text-text">An original version of the same idea:</p>
          <p className="mt-1 italic">“{o.rewrite}”</p>
          <button
            type="button"
            onClick={start}
            disabled={busy}
            className="mt-2 rounded-lg bg-gold px-3 py-1.5 text-xs font-semibold text-ink disabled:opacity-60"
          >
            {busy ? "Starting…" : "Make this version instead"}
          </button>
        </>
      )}
      {error && <p className="mt-2 text-xs text-bad">{error}</p>}
    </div>
  );
}

function Spinoff({
  job,
  path,
  title,
  about,
  placeholder,
  button,
  pending,
  onOpen,
}: {
  job: Job;
  path: "next" | "ending";
  title: string;
  about: string;
  placeholder: string;
  button: string;
  pending: string;
  onOpen: (id: string) => void;
}) {
  const [idea, setIdea] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const start = async () => {
    setBusy(true);
    setError("");
    try {
      const made = await api.post<Job>(`/api/jobs/${job.id}/${path}`, { idea });
      onOpen(made.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="rounded-xl border border-line p-4">
      <div className="label mb-1">{title}</div>
      <p className="mb-3 text-xs text-muted">{about}</p>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          maxLength={600}
          placeholder={placeholder}
          aria-label={title}
          className="min-w-0 flex-1 rounded-lg border border-line bg-ink px-3 py-2 text-sm"
        />
        <button type="button" onClick={start} disabled={busy} className="rounded-lg bg-gold px-4 py-2 text-sm font-semibold text-ink disabled:opacity-60">
          {busy ? pending : button}
        </button>
      </div>
      {error && <p className="mt-2 text-xs text-bad">{error}</p>}
    </div>
  );
}

export default function JobView({
  job,
  onRetry,
  onOpen,
  exportPresets,
}: {
  job: Job;
  onRetry: () => void;
  onOpen: (id: string) => void;
  exportPresets: Option[];
}) {
  const running = job.status === "running" || job.status === "queued";
  const [decisions] = usePoll<{ decisions: Decision[] }>(`/api/jobs/${job.id}/decisions`, 2500, running);
  const [retrying, setRetrying] = useState(false);
  const [version, setVersion] = useState<"film" | "trailer" | "commentary">("film");
  const [recording, setRecording] = useState(false);
  const story = job.story;
  const versions = [
    { id: "film" as const, label: job.options.format === "movie" ? "Movie" : "Trailer", src: job.assets.video },
    { id: "trailer" as const, label: "Trailer", src: job.assets.trailer },
    { id: "commentary" as const, label: "Director's commentary", src: job.assets.commentary },
  ].filter((v) => v.src);
  const video = versions.find((v) => v.id === version)?.src ?? job.assets.video;

  const recordCommentary = async () => {
    setRecording(true);
    await api.post(`/api/jobs/${job.id}/commentary`).catch(() => undefined);
    setRecording(false);
    setVersion("commentary");
    onRetry();
  };
  const posterRatio = "aspect-[2/3]";

  const stop = async () => {
    if (!window.confirm("Stop this film? It can't be resumed, and what it has spent so far isn't refunded.")) return;
    await api.post(`/api/jobs/${job.id}/stop`).catch(() => undefined);
    onRetry();
  };

  const retry = async () => {
    if (
      job.options.fresh &&
      !window.confirm("This film was made with \"Don't reuse old results\", so Retry makes it again from the start and pays for every step again. Continue?")
    )
      return;
    setRetrying(true);
    await api.post(`/api/jobs/${job.id}/retry`).catch(() => undefined);
    setRetrying(false);
    onRetry();
  };

  return (
    <section className="card overflow-hidden" aria-label="Production">
      <header className="border-b border-line p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="label">
              {runtimeLabel(job)} · {job.options.genre} · {job.options.style} · {job.options.aspect}
            </div>
            <Settings job={job} />
            <h2 className="mt-1 font-display text-3xl tracking-wide">{story?.title ?? "Writing…"}</h2>
            {job.previously && (
              <p className="mt-1 text-xs text-muted">
                Episode {job.previously.episode}, following{" "}
                <button type="button" onClick={() => onOpen(job.previously!.job)} className="text-gold underline-offset-2 hover:underline">
                  {job.previously.title}
                </button>
              </p>
            )}
            {job.variant_of && (
              <p className="mt-1 text-xs text-muted">
                Another ending for{" "}
                <button type="button" onClick={() => onOpen(job.variant_of!.job)} className="text-gold underline-offset-2 hover:underline">
                  {job.variant_of.title}
                </button>
                {job.variant_of.idea && <>: “{job.variant_of.idea}”</>}
              </p>
            )}
            {story && <p className="mt-1 max-w-2xl text-sm text-muted">{story.logline}</p>}
            {!story && <p className="mt-1 max-w-2xl text-sm text-muted">“{job.options.prompt}”</p>}
            {job.runtime?.decided_by === "story" && (
              <p className="mt-2 text-xs text-muted">
                <span className="text-gold">{job.runtime.seconds}s</span>, picked by the writer
                {job.runtime.reason ? `: ${job.runtime.reason.replace(/\.$/, "")}.` : "."}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            <span
              className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
                job.status === "done"
                  ? "bg-good/15 text-good"
                  : job.status === "failed"
                    ? "bg-bad/15 text-bad"
                    : "bg-gold/15 text-gold"
              }`}
            >
              {job.status.toUpperCase()}
            </span>
            {running && !job.assets.video && (
              <button type="button" onClick={stop} className="rounded-lg border border-line px-3 py-1 text-xs hover:border-bad hover:text-bad">
                Stop
              </button>
            )}
            {job.status === "failed" && (
              <button
                type="button"
                onClick={retry}
                disabled={retrying}
                title={
                  job.options.fresh
                    ? "Makes the film again from the start and pays for every step again"
                    : "Starts again. Exact matches are reused for free, but if the story comes out differently, the rest is made and paid for again"
                }
                className="rounded-lg border border-line px-3 py-1 text-xs hover:border-gold"
              >
                {retrying ? "Retrying…" : "Retry"}
              </button>
            )}
          </div>
        </div>
        {job.error && !job.originality && (
          <p className="mt-3 rounded-lg border border-bad/40 bg-bad/10 p-2 text-sm text-bad">{job.error}</p>
        )}
        {job.originality && <OriginalVersion job={job} onOpen={onOpen} />}
      </header>

      {running && (
        <div className="px-5 pt-5">
          <FabricLive key={job.id} jobId={job.id} running={running} />
        </div>
      )}
      <div className="grid grid-cols-1 gap-5 p-5 lg:grid-cols-[minmax(0,1fr)_260px]">
        <div className="min-w-0 space-y-5">
          {job.status === "review" ? (
            <ScriptReview key={job.id} job={job} onDone={onRetry} />
          ) : video ? (
            <video key={video} src={video} controls playsInline crossOrigin="anonymous" className="w-full rounded-xl border border-line bg-black">
              {video === job.assets.commentary && job.assets.commentary_vtt ? (
                <track kind="subtitles" src={job.assets.commentary_vtt} srcLang="en" label="Commentary" default />
              ) : (
                (job.assets.subtitle_tracks ?? [])
                  .filter((t) => t.lang !== "en")
                  .map((t) => <track key={t.lang} kind="subtitles" src={t.vtt} srcLang={t.lang} label={t.label} />)
              )}
            </video>
          ) : (
            <Pipeline stages={job.stages} />
          )}
          {job.assets.video && job.status === "done" && (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              {versions.length > 1 && (
                <div role="group" aria-label="Version" className="flex overflow-hidden rounded-lg border border-line">
                  {versions.map((v) => (
                    <button
                      key={v.id}
                      type="button"
                      aria-pressed={video === v.src}
                      onClick={() => setVersion(v.id)}
                      className={`px-3 py-1.5 ${video === v.src ? "bg-gold text-ink" : "hover:bg-panel-2"}`}
                    >
                      {v.label}
                    </button>
                  ))}
                </div>
              )}
              {!job.assets.commentary && (
                <button
                  type="button"
                  onClick={recordCommentary}
                  disabled={recording || job.options.commercial_safe}
                  title={
                    job.options.commercial_safe
                      ? "The commentary voice uses a model licensed for non-commercial use only"
                      : "The director talks over the film about the choices behind each shot and which machine made it"
                  }
                  className="rounded-lg border border-line px-3 py-1.5 hover:border-gold disabled:opacity-50"
                >
                  {recording ? "Starting…" : "Record a director's commentary"}
                </button>
              )}
            </div>
          )}
          {(job.agents?.length ?? 0) > 0 && <AgentCrew turns={job.agents!} release={job} />}
          {job.assets.video && <Receipt job={job} />}
          {job.assets.video && !running && <MachineTimeline decisions={decisions?.decisions ?? []} />}
          {job.assets.passport && (
            <img src={job.assets.passport} alt={`${story?.title ?? "Film"}: production passport`} className="w-full rounded-xl border border-line" />
          )}
          {job.assets.video && (job.status === "done" || job.status === "failed") && <DirectorPanel job={job} onChange={onRetry} />}
          {job.status === "done" && job.story && (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <Spinoff
                job={job}
                path="ending"
                title="Another ending"
                about="Keeps every shot up to the last stretch and only redraws and re-voices the ending, so it costs a fraction of a new film."
                placeholder="How should it end instead? (optional)"
                button="Try it"
                pending="Planning…"
                onOpen={onOpen}
              />
              <Spinoff
                job={job}
                path="next"
                title="Next episode"
                about="Same cast and look, same settings. The writer reads how this one ended and picks up from there."
                placeholder="What happens next? (optional)"
                button={`Make episode ${(job.previously?.episode ?? 1) + 1}`}
                pending="Starting…"
                onOpen={onOpen}
              />
            </div>
          )}
          {(job.checks?.length ?? 0) > 0 && <SafetyPanel job={job} decisions={decisions?.decisions ?? []} />}
          {job.cast.length > 0 && (
            <div>
              <div className="label mb-2">Cast</div>
              <div className="flex gap-3 overflow-x-auto">
                {job.cast.map((c, i) => (
                  <CastMember key={c.name} jobId={job.id} index={i} c={c} />
                ))}
              </div>
            </div>
          )}
          {job.shots.length > 0 && job.status !== "review" && (
            <div>
              <div className="label mb-2">
                Storyboard · {job.shots.length} shots
                {job.status === "done" && <span className="ml-2 normal-case tracking-normal text-muted">use "redo" on a frame to change it</span>}
              </div>
              <div className={`grid gap-2 ${job.options.aspect === "9:16" ? "grid-cols-3 sm:grid-cols-5" : "grid-cols-2 sm:grid-cols-3 xl:grid-cols-4"}`}>
                {job.shots.map((s) => (
                  <ShotCard
                    key={s.index}
                    shot={s}
                    aspect={job.options.aspect}
                    jobId={job.id}
                    canRedo={job.status === "done" || job.status === "failed"}
                    onRedo={onRetry}
                  />
                ))}
              </div>
            </div>
          )}
          {job.assets.video && <Pipeline stages={job.stages} />}
        </div>

        <aside className="space-y-4">
          <div className={`${posterRatio} overflow-hidden rounded-xl border border-line bg-ink`}>
            {job.assets.poster ? (
              <img src={job.assets.poster} alt={`${story?.title ?? "Film"} poster`} className="h-full w-full object-cover" />
            ) : (
              <div className="flex h-full items-center justify-center text-xs text-muted">No poster yet</div>
            )}
          </div>
          {story?.tagline && <p className="text-center font-display text-sm italic text-muted">“{story.tagline}”</p>}
          <div className="space-y-1.5">
            <div className="label">Downloads</div>
            {job.assets.zip && (
              <a href={job.assets.zip} download className="block rounded-lg bg-gold px-3 py-2 text-center text-sm font-semibold text-ink hover:brightness-110">
                Download all (.zip)
              </a>
            )}
            {(
              [
                ["video", "Film (.mp4)"],
                ["trailer", "Trailer (.mp4)"],
                ["poster", "Poster (.png)"],
                ["thumbnail", "Thumbnail (.jpg)"],
                ["script", "Script (.md)"],
                ["subtitles", "Subtitles (.srt)"],
                ["release", "Release copy (.md)"],
              ] as const
            ).map(([k, label]) =>
              job.assets[k] ? (
                <a key={k} href={job.assets[k]} download className="block rounded-lg border border-line px-3 py-1.5 text-sm hover:border-gold/60">
                  {label}
                </a>
              ) : null,
            )}
            {(job.assets.subtitle_tracks ?? [])
              .filter((t) => t.lang !== "en")
              .map((t) => (
                <a key={t.lang} href={t.srt} download className="block rounded-lg border border-line px-3 py-1.5 text-sm hover:border-gold/60">
                  Subtitles, {t.label} (.srt)
                </a>
              ))}
            {(job.assets.subtitles_skipped ?? []).length > 0 && (
              <p className="text-xs text-muted">
                No {job.assets.subtitles_skipped?.join(" or ")} subtitles: neither model produced a translation that passed the check.
              </p>
            )}
          </div>
          {job.assets.video && job.status === "done" && <SharePanel job={job} presets={exportPresets} onChange={onRetry} />}
          {job.voice_note && <p className="rounded-lg border border-line p-3 text-xs text-muted">{job.voice_note}</p>}
          {job.qa && (
            <div className="rounded-lg border border-line p-3 text-xs">
              <div className="label mb-1">Narration check</div>
              <p>
                When the voice-over was transcribed back, <span className="text-gold">{Math.round(job.qa.similarity * 100)}%</span> of the
                words matched the script.
              </p>
              {job.qa.redone_lines && (
                <p className="mt-1 text-muted">
                  The first pass heard {Math.round((job.qa.first_similarity ?? 0) * 100)}%, so {job.qa.redone_lines.length} unclear
                  line{job.qa.redone_lines.length === 1 ? " was" : "s were"} spoken again and the clearer take kept.
                </p>
              )}
            </div>
          )}
          <div>
            <div className="label mb-1">Tasks in this run</div>
            <ul className="max-h-[360px] overflow-y-auto pr-1">
              {(decisions?.decisions ?? []).map((d) => (
                <DecisionRow key={d.task_id} d={d} />
              ))}
            </ul>
          </div>
        </aside>
      </div>
    </section>
  );
}
