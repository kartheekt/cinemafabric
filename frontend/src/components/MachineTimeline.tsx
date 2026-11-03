import { MODEL_NAMES } from "../machines";
import type { Decision } from "../types";
import { KIND_COLOR, KIND_NAME, family } from "./FabricLive";

/** Tasks starting more than this long after everything before them finished belong to a later session (a redo or a note). */
const GAP_S = 120;

type Bar = { row: string; cloud: boolean; start: number; end: number; kind: string; label: string };

function union(spans: [number, number][]) {
  let total = 0;
  let end = -Infinity;
  for (const [a, b] of [...spans].sort((x, y) => x[0] - y[0])) {
    if (b <= end) continue;
    total += b - Math.max(a, end);
    end = b;
  }
  return total;
}

/** The first production session of a film: one bar per task that actually ran, plus how many tasks came later. */
export function timeline(decisions: Decision[]) {
  const bars: Bar[] = decisions
    .filter((d) => d.status === "ok" && !d.cached && d.worker_id && d.latency_ms > 0)
    .map((d) => ({
      row: d.is_cloud ? `☁ ${MODEL_NAMES[d.model ?? ""] ?? d.model}` : (d.worker_label ?? d.worker_id!).replace(/NVIDIA GeForce /i, ""),
      cloud: d.is_cloud,
      start: d.ts - d.latency_ms / 1000,
      end: d.ts,
      kind: family(d.kind),
      label: `${d.label ?? d.kind} · ${(d.latency_ms / 1000).toFixed(1)} s`,
    }))
    .sort((a, b) => a.start - b.start);
  const first: Bar[] = [];
  let reach = -Infinity;
  for (const b of bars) {
    if (first.length && b.start > reach + GAP_S) break;
    first.push(b);
    reach = Math.max(reach, b.end);
  }
  if (!first.length) return null;
  const t0 = first[0].start;
  const span = Math.max(...first.map((b) => b.end)) - t0;
  const edges = first.flatMap((b) => [[b.start, 1] as const, [b.end, -1] as const]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  let now = 0;
  let peak = 0;
  for (const [, step] of edges) peak = Math.max(peak, (now += step));
  const local = first.filter((b) => !b.cloud);
  const rows = [...new Set(first.map((b) => b.row))].sort((a, b) => Number(a.startsWith("☁")) - Number(b.startsWith("☁")) || a.localeCompare(b));
  return {
    bars: first,
    rows,
    t0,
    span,
    peak,
    localBusy: span > 0 ? union(local.map((b) => [b.start, b.end])) / span : 0,
    localRows: new Set(local.map((b) => b.row)).size,
    computers: new Set(local.map((b) => b.row.split(" · ")[0])).size,
    cloudRows: new Set(first.filter((b) => b.cloud).map((b) => b.row)).size,
    later: bars.length - first.length,
  };
}

function clock(s: number) {
  return s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

const W = 460;
const LABEL = 150;
const ROW = 24;

export default function MachineTimeline({ decisions }: { decisions: Decision[] }) {
  const t = timeline(decisions);
  if (!t || t.span <= 0) return null;
  const x = (s: number) => LABEL + ((s - t.t0) / t.span) * (W - LABEL - 8);
  const height = t.rows.length * ROW + 22;
  const step = [10, 30, 60, 120, 300, 600].find((s) => t.span / s <= 6) ?? 1200;
  const ticks = Array.from({ length: Math.floor(t.span / step) + 1 }, (_, i) => i * step);
  return (
    <section className="rounded-xl border border-line p-3" aria-label="Where the work ran">
      <div className="label">Where the work ran</div>
      <p className="mt-1 text-xs text-muted">
        {t.computers} of your computer{t.computers === 1 ? "" : "s"} ({t.localRows} worker{t.localRows === 1 ? "" : "s"}) and{" "}
        {t.cloudRows} Meta model{t.cloudRows === 1 ? "" : "s"} in the cloud worked side by side. Your machines were busy for{" "}
        <span className="text-gold">{Math.round(t.localBusy * 100)}%</span> of the {clock(t.span)} run, with up to {t.peak} tasks
        running at once.
        {t.later > 0 && ` ${t.later} later task${t.later === 1 ? "" : "s"} (redos and notes) are not shown.`}
      </p>
      <svg viewBox={`0 0 ${W} ${height}`} className="mt-2 w-full" role="img" aria-label="Timeline of every task on each machine and cloud model">
        {t.rows.map((r, i) => (
          <g key={r}>
            <rect x={LABEL} y={i * ROW + 2} width={W - LABEL - 8} height={ROW - 4} rx={3} fill="var(--color-panel-2)" opacity={0.5} />
            <text x={LABEL - 6} y={i * ROW + 16} textAnchor="end" fontSize={12} fill="var(--color-text)">
              {r.length > 22 ? r.slice(0, 21) + "…" : r}
            </text>
          </g>
        ))}
        {t.bars.map((b, i) => (
          <rect key={i} x={x(b.start)} y={t.rows.indexOf(b.row) * ROW + 4} width={Math.max(1.5, x(b.end) - x(b.start))} height={ROW - 8}
            rx={2} fill={KIND_COLOR[b.kind] ?? "var(--color-text)"} opacity={0.8}>
            <title>{b.label}</title>
          </rect>
        ))}
        {ticks.map((s) => (
          <text key={s} x={x(t.t0 + s)} y={height - 4} textAnchor={s === 0 ? "start" : "middle"} fontSize={11} fill="var(--color-muted)">
            {s === 0 ? "0" : clock(s)}
          </text>
        ))}
      </svg>
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
