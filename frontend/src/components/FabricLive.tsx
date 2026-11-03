import { useEffect, useRef, useState } from "react";
import { api, usd, usePoll } from "../api";
import { JOB, MODEL_NAMES, type Part, machines } from "../machines";
import type { Decision, Worker } from "../types";

const ROLE_MARK: Record<Part["role"], string> = { gpu: "▣", model: "◆", service: "☁" };

export const KIND_COLOR: Record<string, string> = {
  text: "var(--color-gold)",
  image: "var(--color-sky)",
  audio: "var(--color-good)",
  moderate: "var(--color-bad)",
};
export const KIND_NAME: Record<string, string> = { text: "writing", image: "pictures", audio: "sound", moderate: "safety" };

export function family(kind: string) {
  if (kind.startsWith("text.moderate")) return "moderate";
  return kind.split(".")[0];
}

type Pulse = { id: string; worker: string; kind: string };

const OFFLINE = /(\S+) skipped \((disabled|circuit open|removed|[^)]*unreachable[^)]*)\)/;
const POLICY = /content_policy|content management policy/i;

/** Tasks that really changed machine. A retry on the same service isn't a move, and only local-first would have used
 * the skipped machine: under the other preferences the task may have gone to the cloud anyway. */
export function movedTasks(decisions: Decision[]) {
  return decisions
    .map((d) => {
      const reason = d.reason ?? "";
      const failedOn = d.fallbacks.map((f) => f.split(" ")[0]).find((f) => f !== d.worker_id);
      const from = failedOn ?? (reason.includes("Local-first:") ? reason.match(OFFLINE)?.[1] : undefined);
      return from && d.status === "ok" ? { d, from, failed: failedOn !== undefined } : null;
    })
    .filter((x): x is { d: Decision; from: string; failed: boolean } => x !== null);
}

const W = 600;
const ROW = 28;
const FABRIC = { x: 165, y: 0 };
const BOX = 240;

export default function FabricLive({ jobId, running }: { jobId: string; running: boolean }) {
  const [workers] = usePoll<{ workers: Worker[] }>("/api/fabric/workers", 1000, running);
  const [decisions] = usePoll<{ decisions: Decision[] }>(`/api/jobs/${jobId}/decisions`, 1500, running);
  const [pulses, setPulses] = useState<Pulse[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<string | null>(null);
  const seen = useRef<Set<string> | null>(null);

  useEffect(() => {
    const list = decisions?.decisions ?? [];
    if (seen.current === null) {
      seen.current = new Set(list.map((d) => d.task_id));
      return;
    }
    const fresh = list.filter((d) => !seen.current!.has(d.task_id) && d.worker_id);
    if (!fresh.length) return;
    fresh.forEach((d) => seen.current!.add(d.task_id));
    const added = fresh.map((d) => ({ id: d.task_id, worker: d.is_cloud ? `${d.worker_id}:${d.model}` : d.worker_id!, kind: family(d.kind) }));
    setPulses((p) => [...p, ...added].slice(-24));
    const t = window.setTimeout(() => setPulses((p) => p.filter((x) => !added.includes(x))), 1600);
    return () => window.clearTimeout(t);
  }, [decisions]);

  const groups = machines((workers?.workers ?? []).filter((w) => w.source !== "builtin" || w.is_cloud));
  const done = (decisions?.decisions ?? []).filter((d) => d.status === "ok");
  const doneBy = new Map<string, number>();
  for (const d of done) if (d.worker_id && !d.cached) doneBy.set(d.worker_id, (doneBy.get(d.worker_id) ?? 0) + 1);
  const byModel = new Map<string, { n: number; cost: number }>();
  for (const d of done) {
    if (!d.is_cloud || !d.model) continue;
    const s = byModel.get(d.model) ?? { n: 0, cost: 0 };
    byModel.set(d.model, { n: s.n + 1, cost: s.cost + d.cost_usd });
  }
  const rows: { key: string; w: Worker; y: number; name: string; label: string; model?: string }[] = [];
  const boxes: { host: string; summary: string; y: number; h: number }[] = [];
  let y = 16;
  for (const m of groups) {
    const lines = m.cloud
      ? m.parts.flatMap((p) => [...new Map(p.w.offers.map((o) => [o.model, o])).values()].map((o) => ({ p, model: o.model, kind: o.kind })))
      : m.parts.map((p) => ({ p, model: undefined as string | undefined, kind: "" }));
    boxes.push({ host: m.host, summary: m.summary, y, h: lines.length * ROW + 22 });
    lines.forEach(({ p, model, kind }, i) => {
      const name = model ? MODEL_NAMES[model] ?? model : p.name;
      rows.push({
        key: model ? `${p.w.id}:${model}` : p.w.id,
        w: p.w,
        y: y + 26 + i * ROW,
        name,
        model,
        label: `${ROLE_MARK[p.role]} ${name} · ${model ? JOB[kind] ?? kind.split(".")[1] : p.does}`,
      });
    });
    y += lines.length * ROW + 34;
  }
  const height = Math.max(y, 120);
  const fabricY = height / 2;
  const at = new Map(rows.map((r) => [r.key, r.y]));
  const spent = done.reduce((n, d) => n + d.cost_usd, 0);
  const local = done.filter((d) => !d.is_cloud && !d.cached).length;
  const moved = movedTasks(decisions?.decisions ?? []);
  const failed = (decisions?.decisions ?? []).filter((d) => d.status !== "ok" && !POLICY.test(d.error ?? "")).length;
  const names = new Map(rows.filter((r) => !r.model).map((r) => [r.w.id, `${r.name} (${r.w.label.split(" · ")[0]})`]));
  const toggle = async (w: Worker) => {
    setBusy(w.id);
    setError(null);
    try {
      await api.post(`/api/fabric/workers/${w.id}/${w.enabled ? "disable" : "enable"}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="rounded-xl border border-line bg-ink/60 p-3" aria-label="Live fabric">
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2 text-xs">
        <span className="label">Live fabric</span>
        <span className="text-muted">
          {done.length} tasks · {local} on your machines · <span className="text-gold">{usd(spent, 4)}</span> cloud
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${height}`} className="w-full" role="img" aria-label="Which machine is doing each task right now">
        <rect x={8} y={fabricY - 24} width={96} height={48} rx={10} fill="var(--color-panel-2)" stroke="var(--color-line)" />
        <text x={56} y={fabricY - 2} textAnchor="middle" fontSize={15} fill="var(--color-text)">Studio</text>
        <text x={56} y={fabricY + 15} textAnchor="middle" fontSize={11} fill="var(--color-muted)">your film</text>
        <line x1={104} y1={fabricY} x2={FABRIC.x - 30} y2={fabricY} stroke="var(--color-line)" strokeWidth={2} />
        <circle cx={FABRIC.x} cy={fabricY} r={32} fill="var(--color-panel-2)" stroke="var(--color-gold)" strokeWidth={1.5} />
        <text x={FABRIC.x} y={fabricY + 4} textAnchor="middle" fontSize={14} fill="var(--color-gold)">Fabric</text>
        {boxes.map((b) => (
          <g key={b.host}>
            <rect x={BOX} y={b.y} width={W - BOX - 6} height={b.h} rx={10} fill="none" stroke="var(--color-line)" />
            <text x={BOX + 12} y={b.y + 15} fontSize={12} fill="var(--color-muted)">
              <tspan fill="var(--color-text)">{b.host}</tspan>
              {b.summary && ` · ${b.summary}`}
            </text>
          </g>
        ))}
        {rows.map(({ key, w, y: ry, label, model }) => {
          const up = w.status === "up" && w.enabled;
          const busy = !model && w.inflight > 0;
          const used = model ? byModel.get(model) : undefined;
          return (
            <g key={key} opacity={up ? 1 : 0.4}>
              <line x1={FABRIC.x + 30} y1={fabricY} x2={BOX + 14} y2={ry} stroke={busy ? "var(--color-gold)" : "var(--color-line)"}
                strokeWidth={busy ? 2 : 1} strokeDasharray={up ? undefined : "4 4"} />
              <circle cx={BOX + 22} cy={ry} r={6} fill={up ? (busy ? "var(--color-gold)" : "var(--color-good)") : "var(--color-bad)"}
                className={busy ? "pulse" : undefined} />
              <text x={BOX + 34} y={ry + 5} fontSize={13} fill="var(--color-text)">{label.length > 33 ? label.slice(0, 33) + "…" : label}</text>
              <text x={W - 14} y={ry + 5} textAnchor="end" fontSize={11} fill={used?.cost ? "var(--color-gold)" : "var(--color-muted)"}>
                {!up
                  ? w.enabled ? "down" : "offline"
                  : model
                    ? used ? `${used.n} done · ${usd(used.cost, 3)}` : "ready"
                    : `${busy ? `${w.inflight} working · ` : ""}${doneBy.get(w.id) ?? 0} done`}
              </text>
            </g>
          );
        })}
        {pulses.map((p) => {
          const ty = at.get(p.worker);
          if (ty === undefined) return null;
          return (
            <circle key={p.id} cx={FABRIC.x + 30} cy={fabricY} r={6} fill={KIND_COLOR[p.kind] ?? "var(--color-text)"} className="fly"
              style={{ "--dx": `${BOX + 14 - FABRIC.x - 30}px`, "--dy": `${ty - fabricY}px` } as React.CSSProperties} />
          );
        })}
      </svg>
      {running && (
        <div className="mt-2 space-y-1 text-[11px]">
          <div className="text-muted">Try it: take a graphics card or model offline mid-run</div>
          {groups
            .filter((m) => !m.cloud)
            .map((m) => (
              <div key={m.host} className="flex flex-wrap items-center gap-1.5">
                <span className="min-w-16 text-muted">{m.host}</span>
                {m.parts.map(({ w, name }) => (
                  <button
                    key={w.id}
                    type="button"
                    disabled={busy !== ""}
                    onClick={() => toggle(w)}
                    className={`min-h-6 rounded border px-2 ${w.enabled ? "border-line hover:border-bad hover:text-bad" : "border-good/60 text-good"}`}
                  >
                    {w.enabled ? `${name} off` : `${name} back on`}
                  </button>
                ))}
              </div>
            ))}
        </div>
      )}
      {error && <p role="alert" className="mt-1 text-[11px] text-bad">{error}</p>}
      {moved.length > 0 && (
        <div className="mt-2 rounded-lg border border-gold/30 bg-gold/5 p-2 text-[11px]">
          <div className="text-gold">
            {moved.length} task{moved.length === 1 ? "" : "s"} moved to another machine · {failed === 0 ? "none lost" : `${failed} failed`} ·
            nothing paid for twice
          </div>
          <ul className="mt-1 space-y-0.5 text-muted">
            {moved.slice(0, 4).map(({ d, from, failed: retried }) => (
              <li key={d.task_id}>
                {d.label}: {names.get(from) ?? from} was {retried ? "failing" : "offline"}, so {d.worker_label} took it
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="mt-1 flex flex-wrap gap-3 text-[11px] text-muted">
        {Object.entries(KIND_NAME).map(([k, n]) => (
          <span key={k} className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: KIND_COLOR[k] }} /> {n}
          </span>
        ))}
      </div>
    </section>
  );
}
