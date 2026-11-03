import type { Decision, Job } from "../types";

const VERDICT_STYLE: Record<string, string> = {
  passed: "bg-good/15 text-good",
  "allowed with notes": "bg-gold/15 text-gold",
  "cleared on review": "bg-gold/15 text-gold",
  redrawn: "bg-gold/15 text-gold",
  renamed: "bg-gold/15 text-gold",
  resolved: "bg-good/15 text-good",
  "still flagged": "bg-bad/15 text-bad",
  blocked: "bg-bad/15 text-bad",
  "not checked": "bg-muted/15 text-muted",
};

const NON_COMMERCIAL = new Set(["cc-by-nc-4.0"]);

function Row({ ok, title, children }: { ok: boolean | null; title: string; children?: React.ReactNode }) {
  const mark = ok === null ? "–" : ok ? "✓" : "✕";
  const tone = ok === null ? "text-muted" : ok ? "text-good" : "text-bad";
  return (
    <li className="flex gap-2">
      <span className={`w-4 shrink-0 text-center ${tone}`} aria-hidden="true">
        {mark}
      </span>
      <span className="min-w-0">
        <span className="sr-only">{ok === null ? "Not applicable: " : ok ? "Yes: " : "No: "}</span>
        <span className="text-text">{title}</span>
        {children && <span className="block text-muted">{children}</span>}
      </span>
    </li>
  );
}

export default function SafetyPanel({ job, decisions }: { job: Job; decisions: Decision[] }) {
  const checks = job.checks ?? [];
  const reviews = job.shots.filter((s) => s.review);
  const redone = reviews.filter((s) => s.review?.regenerated).length;
  const avg = reviews.length ? reviews.reduce((n, s) => n + (s.review?.score ?? 0), 0) / reviews.length : 0;
  const wm = job.watermark;
  const models = new Map<string, string>();
  for (const d of decisions) if (d.status === "ok" && d.model) models.set(d.model, d.licence ?? "unknown");
  const nc = [...models.entries()].filter(([, lic]) => NON_COMMERCIAL.has(lic));

  return (
    <details className="rounded-xl border border-line p-4" open={job.status === "done"}>
      <summary className="label cursor-pointer">Safety and provenance</summary>

      <div className="mt-3 grid gap-4 text-xs sm:grid-cols-2">
        <div>
          <h3 className="mb-1.5 font-semibold text-text">Content checks</h3>
          {checks.length === 0 ? (
            <p className="text-muted">No checks recorded for this run.</p>
          ) : (
            <ul className="space-y-1.5">
              {checks.map((c, i) => (
                <li key={i} className="flex items-start justify-between gap-2">
                  <span className="min-w-0">
                    {c.check}
                    <span className="block text-muted">
                      {c.by}
                      {c.chunks > 1 && !c.check.startsWith("Picture") ? `, ${c.chunks} parts` : ""}
                      {c.categories.length > 0 ? ` · noted: ${c.categories.join(", ").toLowerCase()}` : ""}
                    </span>
                    {c.review && (
                      <span className="block text-muted">
                        {c.by.includes(" + ") ? "Second opinion: " : ""}
                        {c.review}
                      </span>
                    )}
                  </span>
                  <span className={`shrink-0 rounded-full px-2 py-0.5 font-semibold ${VERDICT_STYLE[c.verdict] ?? ""}`}>{c.verdict}</span>
                </li>
              ))}
            </ul>
          )}
          {reviews.length > 0 && (
            <p className="mt-3 text-muted">
              Frame check: {reviews.length} frames scored, average {avg.toFixed(1)}/10, {redone} redrawn.
            </p>
          )}
        </div>

        <div>
          <h3 className="mb-1.5 font-semibold text-text">Labels and watermark</h3>
          <ul className="space-y-1.5">
            <Row ok={job.status === "done"} title="“Made with AI” on the end card and poster" />
            <Row ok={job.status === "done"} title="IPTC metadata: trained algorithmic media">
              In the poster and frames, where platforms that read AI labels can find it; the film carries it in its tags.
            </Row>
            <Row ok={wm ? Boolean(wm.applied && wm.verified) : null} title="AudioSeal watermark on the soundtrack">
              {!wm
                ? "Not applied yet."
                : wm.applied
                  ? `Read back with ${Math.round((wm.detection ?? 0) * 100)}% confidence; payload ${wm.payload}.`
                  : `Not applied: ${wm.reason}`}
            </Row>
            <Row
              ok={job.credentials ? Object.keys(job.credentials.signed).length > 0 && job.credentials.verified !== false : null}
              title="Content Credentials (C2PA)"
            >
              {!job.credentials
                ? "Not signed yet."
                : Object.keys(job.credentials.signed).length === 0
                  ? `Not signed: ${job.credentials.reason ?? "unknown reason"}.`
                  : `Signed manifest in ${Object.keys(job.credentials.signed).join(", ")}, saying it was made with AI. ${
                      job.credentials.own_certificate
                        ? "Signed with your certificate."
                        : "Signed with this studio's local certificate, so viewers will say the signer isn't publicly trusted."
                    }`}
            </Row>
            <Row ok={Boolean(job.assets.zip)} title="Credits and manifest in the download">
              CREDITS.txt lists every model and its licence; manifest.json records these checks.
            </Row>
          </ul>
        </div>
      </div>

      {(job.reports?.length ?? 0) > 0 && (
        <div className="mt-4 text-xs">
          <h3 className="mb-1.5 font-semibold text-bad">Reports from viewers ({job.reports!.length})</h3>
          <ul className="space-y-1">
            {job.reports!.map((r, i) => (
              <li key={i}>
                <span className="text-text">{r.label}</span>
                <span className="text-muted">
                  {" "}
                  · {new Date(r.at * 1000).toLocaleString()}
                  {r.note && <> · “{r.note}”</>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {models.size > 0 && (
        <div className="mt-4 text-xs">
          <h3 className="mb-1.5 font-semibold text-text">Models and licences</h3>
          <ul className="flex flex-wrap gap-1.5">
            {[...models.entries()].map(([m, lic]) => (
              <li
                key={m}
                className={`rounded border px-2 py-0.5 font-mono ${NON_COMMERCIAL.has(lic) ? "border-gold/50 text-gold" : "border-line text-muted"}`}
              >
                {m} · {lic}
              </li>
            ))}
          </ul>
          {nc.length > 0 && (
            <p className="mt-2 text-gold">
              {nc.map(([m]) => m).join(", ")} {nc.length === 1 ? "is" : "are"} licensed for non-commercial use only. Remove
              “cc-by-nc-4.0” from the allowed licences in your profile to keep them out.
            </p>
          )}
        </div>
      )}
    </details>
  );
}
