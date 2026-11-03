import { useState } from "react";
import { api } from "../api";
import type { Job, Option } from "../types";

function Copyable({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div>
      <div className="text-muted">{label}</div>
      <div className="mt-1 flex gap-1">
        <input readOnly value={value} aria-label={label} className="min-w-0 flex-1 rounded border border-line bg-ink px-2 py-1 font-mono text-[11px]" />
        <button
          type="button"
          onClick={() => navigator.clipboard?.writeText(value).then(() => setCopied(true), () => undefined)}
          className="rounded border border-line px-2 hover:border-gold"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

export default function SharePanel({ job, presets, onChange }: { job: Job; presets: Option[]; onChange: () => void }) {
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const link = job.share ? `${window.location.origin}/watch/${job.share.id}` : "";
  const embed = job.share
    ? `<iframe src="${window.location.origin}/embed/${job.share.id}" width="640" height="360" allowfullscreen title="${job.story?.title ?? "Film"}"></iframe>`
    : "";

  const run = async (key: string, call: () => Promise<unknown>) => {
    setBusy(key);
    setError(null);
    try {
      await call();
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="space-y-3 rounded-lg border border-line p-3 text-xs">
      <div className="label">Share and export</div>
      <label className="flex cursor-pointer items-center gap-2">
        <input
          type="checkbox"
          checked={Boolean(job.share)}
          disabled={busy !== ""}
          onChange={(e) => run("share", () => api.post(`/api/jobs/${job.id}/share`, { public: e.target.checked }))}
        />
        Anyone with the link can watch
      </label>
      {job.share && (
        <>
          <Copyable label="Watch page" value={link} />
          <Copyable label="Embed" value={embed} />
        </>
      )}
      <div>
        <div className="text-muted">Versions for other platforms</div>
        <div className="mt-1 space-y-1">
          {presets.map((p) => {
            const done = job.assets.exports?.[p.id];
            return (
              <div key={p.id} className="flex items-center justify-between gap-2">
                <span>{p.label}</span>
                {done ? (
                  <a href={done} download className="inline-flex min-h-6 items-center text-gold underline">
                    Download
                  </a>
                ) : (
                  <button
                    type="button"
                    disabled={busy !== ""}
                    onClick={() => run(p.id, () => api.post(`/api/jobs/${job.id}/export/${p.id}`))}
                    className="min-h-6 rounded border border-line px-2 py-0.5 hover:border-gold disabled:opacity-40"
                  >
                    {busy === p.id ? "Making…" : "Make"}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </div>
      {error && (
        <p role="alert" className="text-bad">
          {error}
        </p>
      )}
    </div>
  );
}
