import { useState } from "react";
import { api } from "../api";
import type { Job, ShotState } from "../types";

const CAMERAS = ["push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down", "static", "parallax"];

type Draft = Pick<ShotState, "visual" | "camera" | "duration" | "narration" | "characters" | "title_card"> & { key: number };

export default function ScriptReview({ job, onDone }: { job: Job; onDone: () => void }) {
  const [shots, setShots] = useState<Draft[]>(() =>
    job.shots.map((s, i) => ({ key: i, visual: s.visual, camera: s.camera, duration: s.duration, narration: s.narration, characters: s.characters, title_card: s.title_card })),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const total = shots.reduce((t, s) => t + (Number(s.duration) || 0), 0);

  const update = (i: number, patch: Partial<Draft>) => setShots((all) => all.map((s, k) => (k === i ? { ...s, ...patch } : s)));
  const move = (i: number, d: number) =>
    setShots((all) => {
      const j = i + d;
      if (j < 0 || j >= all.length) return all;
      const next = [...all];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });

  const approve = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/jobs/${job.id}/approve`, { shots: shots.map(({ key: _key, ...s }) => s) });
      onDone();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <div className="label text-gold/90">Your turn</div>
          <p className="mt-1 text-sm">
            Check the script before any pictures are made. Edit what you like, then continue. {shots.length} shots, about{" "}
            {Math.round(total + 3.5)}s with the end title.
          </p>
        </div>
        <button type="button" onClick={approve} disabled={busy} className="rounded-lg bg-gold px-4 py-2 text-sm font-semibold text-ink disabled:opacity-40">
          {busy ? "Continuing…" : "Looks good, continue"}
        </button>
      </div>
      {error && <p className="rounded border border-bad/40 bg-bad/10 p-2 text-xs text-bad">{error}</p>}
      <ol className="space-y-2">
        {shots.map((s, i) => (
          <li key={s.key} className="rounded-xl border border-line bg-ink/50 p-3">
            <div className="mb-2 flex items-center gap-2 text-xs text-muted">
              <span className="font-mono">{String(i + 1).padStart(2, "0")}</span>
              {s.title_card ? (
                <span className="rounded bg-panel-2 px-1.5">title card</span>
              ) : (
                <select
                  aria-label="Camera move"
                  value={s.camera}
                  onChange={(e) => update(i, { camera: e.target.value })}
                  className="rounded border border-line bg-ink px-1 py-0.5"
                >
                  {CAMERAS.map((c) => (
                    <option key={c} value={c}>
                      {c.replace("_", " ")}
                    </option>
                  ))}
                </select>
              )}
              <label className="flex items-center gap-1">
                <input
                  aria-label="Seconds"
                  type="number"
                  min={1}
                  max={10}
                  step={0.5}
                  value={s.duration}
                  onChange={(e) => update(i, { duration: Number(e.target.value) })}
                  className="w-14 rounded border border-line bg-ink px-1 py-0.5 text-right"
                />
                s
              </label>
              <span className="ml-auto flex gap-1">
                <button type="button" aria-label="Move up" onClick={() => move(i, -1)} className="rounded border border-line px-1.5 hover:text-text">
                  ↑
                </button>
                <button type="button" aria-label="Move down" onClick={() => move(i, 1)} className="rounded border border-line px-1.5 hover:text-text">
                  ↓
                </button>
                <button
                  type="button"
                  aria-label="Cut this shot"
                  onClick={() => setShots((all) => all.filter((_, k) => k !== i))}
                  className="rounded border border-line px-1.5 hover:border-bad/60 hover:text-bad"
                >
                  cut
                </button>
              </span>
            </div>
            {s.title_card ? (
              <input
                aria-label="Title card text"
                value={s.title_card}
                onChange={(e) => update(i, { title_card: e.target.value })}
                className="w-full rounded-lg border border-line bg-ink px-2 py-1.5 text-sm uppercase tracking-widest"
              />
            ) : (
              <textarea
                aria-label="What the camera sees"
                rows={2}
                value={s.visual}
                onChange={(e) => update(i, { visual: e.target.value })}
                className="w-full resize-y rounded-lg border border-line bg-ink px-2 py-1.5 text-sm"
              />
            )}
            <input
              aria-label="Voice-over"
              value={s.narration}
              placeholder="No voice-over on this shot"
              onChange={(e) => update(i, { narration: e.target.value })}
              className="mt-1.5 w-full rounded-lg border border-line bg-ink px-2 py-1.5 text-sm italic placeholder:not-italic placeholder:text-muted"
            />
          </li>
        ))}
      </ol>
    </div>
  );
}
