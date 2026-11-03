import { useState } from "react";
import { api, usd } from "../api";
import type { Job } from "../types";

export default function DirectorPanel({ job, onChange }: { job: Job; onChange: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"" | "plan" | "apply" | "discard">("");
  const [error, setError] = useState<string | null>(null);
  const pending = job.director?.pending ?? null;
  const turns = job.director?.turns ?? [];

  const act = async (kind: "plan" | "apply" | "discard") => {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "plan") {
        await api.post(`/api/jobs/${job.id}/director`, { note: note.trim() });
        setNote("");
      } else {
        await api.post(`/api/jobs/${job.id}/director/${kind}`);
      }
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="rounded-xl border border-line p-4" aria-labelledby="director-heading">
      <h3 id="director-heading" className="label">
        Notes on the cut
      </h3>
      <p className="mt-1 text-xs text-muted">
        Say what to change. A model plans the edits, the content check reviews them, and nothing changes until you apply
        the plan.
      </p>

      {turns.length > 0 && (
        <ol className="mt-3 space-y-2 text-xs">
          {turns.map((t, i) => (
            <li key={i} className="rounded-lg bg-ink/50 p-2">
              <p className="text-text">“{t.note}”</p>
              <p className="mt-1 text-muted">
                {t.reply}{" "}
                {t.applied
                  ? `${t.edits.length} edit${t.edits.length === 1 ? "" : "s"} applied, ${usd(t.cost_usd)}.`
                  : `Not applied (${usd(t.cost_usd)} spent before it failed).`}
              </p>
            </li>
          ))}
        </ol>
      )}

      {pending ? (
        <div className="mt-3 rounded-lg border border-gold/40 bg-gold/5 p-3 text-xs">
          <p className="text-text">“{pending.note}”</p>
          <p className="mt-2">{pending.reply}</p>
          {pending.edits.length > 0 ? (
            <ul className="mt-2 list-disc space-y-0.5 pl-4 text-muted">
              {pending.edits.map((e, i) => (
                <li key={i}>{e.summary}</li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-muted">No edits planned.</p>
          )}
          <p className="mt-2 text-muted">
            Planned by {pending.planned_by}. Estimated cloud cost up to {usd(pending.estimate_usd)} for new pictures;
            voice and music run on your machines.
          </p>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={() => act("apply")}
              disabled={busy !== "" || pending.edits.length === 0}
              className="rounded-lg bg-gold px-3 py-1.5 font-semibold text-ink disabled:opacity-40"
            >
              {busy === "apply" ? "Starting…" : "Apply and re-cut"}
            </button>
            <button
              type="button"
              onClick={() => act("discard")}
              disabled={busy !== ""}
              className="rounded-lg border border-line px-3 py-1.5 hover:border-gold"
            >
              Discard
            </button>
          </div>
        </div>
      ) : (
        <form
          className="mt-3 flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (note.trim().length >= 3) void act("plan");
          }}
        >
          <label htmlFor="director-note" className="sr-only">
            Note on the cut
          </label>
          <textarea
            id="director-note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={600}
            rows={2}
            placeholder="e.g. Make the last shot darker and cut the second line of voice-over."
            className="w-full rounded-lg border border-line bg-ink p-2 text-sm"
          />
          <button
            type="submit"
            disabled={busy !== "" || note.trim().length < 3}
            className="self-start rounded-lg border border-gold/60 px-3 py-1.5 text-xs font-semibold text-gold disabled:opacity-40"
          >
            {busy === "plan" ? "Planning…" : "Plan the changes"}
          </button>
        </form>
      )}
      {error && (
        <p role="alert" className="mt-2 rounded border border-bad/40 bg-bad/10 p-2 text-xs text-bad">
          {error}
        </p>
      )}
    </section>
  );
}
