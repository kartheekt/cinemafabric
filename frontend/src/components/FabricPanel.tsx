import { useState } from "react";
import { api, secs, usd, usePoll } from "../api";
import { MODEL_NAMES, type Machine, type Part, machines } from "../machines";
import type { Decision, Ledger, Worker } from "../types";
import AddMachine from "./AddMachine";
import ApiKeyEditor from "./ApiKeyEditor";

const STATUS_DOT: Record<string, string> = {
  up: "bg-good",
  down: "bg-bad",
  disabled: "bg-muted",
  unknown: "bg-gold",
};

const LOCATION_ICON: Record<string, string> = {
  "mac-mini": "◉",
  pc: "▣",
  lan: "▣",
  cloud: "☁",
  fabric: "◇",
};

const ROLE_LABEL: Record<Part["role"], string> = { gpu: "Graphics cards", model: "Models", service: "Services" };

function OfflineButton({ enabled, onClick, what }: { enabled: boolean; onClick: () => void; what: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={enabled ? `Stop sending work to ${what}` : `Start sending work to ${what} again`}
      className={`min-h-6 shrink-0 rounded-md border px-2 py-0.5 text-[11px] ${
        enabled ? "border-line text-muted hover:border-bad/60 hover:text-bad" : "border-good/50 text-good"
      }`}
    >
      {enabled ? "Take offline" : "Bring back"}
    </button>
  );
}

function PartRow({ p, onToggle, onRemove }: { p: Part; onToggle: () => void; onRemove: () => void }) {
  const { w } = p;
  const lat = Object.entries(w.latency_s)
    .map(([k, v]) => `${k.split(".")[1]} ${v}s`)
    .join(", ");
  return (
    <li className="rounded-lg bg-panel-2/60 px-2.5 py-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 shrink-0 rounded-full ${w.enabled ? STATUS_DOT[w.status] : STATUS_DOT.disabled} ${w.inflight ? "pulse" : ""}`} />
            <span className="truncate text-sm" title={w.label}>
              {p.name}
              {p.role === "gpu" && <span className="text-muted"> · {w.device.split(" · ").slice(1).join(" · ")}</span>}
            </span>
          </div>
          <div className="mt-0.5 truncate text-[11px] text-muted" title={w.offers.map((o) => o.model).join(", ")}>
            {p.does}
          </div>
        </div>
        {w.id !== "fabric-synth" && (
          <div className="flex shrink-0 gap-1">
            <OfflineButton enabled={w.enabled} onClick={onToggle} what={p.name} />
            {w.source === "added" && (
              <button
                type="button"
                onClick={onRemove}
                title={`Remove ${p.name}`}
                aria-label={`Remove ${w.label}`}
                className="min-h-6 min-w-6 rounded-md border border-line px-1.5 py-0.5 text-[11px] text-muted hover:border-bad/60 hover:text-bad"
              >
                ✕
              </button>
            )}
          </div>
        )}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-muted">
        <span className={w.status === "up" ? "" : "text-bad/80"}>{w.detail}</span>
        {w.inflight > 0 && <span className="text-gold">{w.inflight} running</span>}
        <span>{w.completed} done</span>
        {lat && <span>{lat}</span>}
      </div>
    </li>
  );
}

const STAGE_NAMES: Record<string, string> = {
  safety: "content and originality checks",
  story: "story",
  script: "script",
  shots: "shot prompts",
  cast: "character sheets",
  frames: "storyboard frames",
  review: "frame checks",
  poster: "poster",
  depth: "cutouts",
  qa: "narration check",
  director: "director notes",
  commentary: "commentary",
  voice: "voice-over translation",
  edit: "subtitle translation",
  suggest: "idea suggestions",
  docs: "README banner and diagrams",
};

function CloudModels({ w, ledger }: { w: Worker; ledger: Ledger | null }) {
  const stats = new Map((ledger?.by_model ?? []).map((m) => [m.model, m]));
  const models = [...new Map(w.offers.map((o) => [o.model, o])).values()];
  const used = models.reduce((n, o) => n + (stats.get(o.model)?.tasks ?? 0), 0);
  return (
    <div className="mt-2 rounded-lg bg-panel-2/60 p-2.5">
      <div className="mb-1.5 flex items-baseline justify-between text-[11px]">
        <span className="text-[10px] uppercase tracking-wider text-muted">Meta models used here</span>
        <span className="text-muted">{used} calls so far</span>
      </div>
      <ul className="space-y-1.5">
        {models.map((o) => {
          const s = stats.get(o.model);
          const stages = (s?.stages ?? []).filter((x) => x in STAGE_NAMES).map((x) => STAGE_NAMES[x]).join(", ");
          return (
            <li key={o.model} className="text-[11px]">
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-sm text-text" title={o.model}>
                  {MODEL_NAMES[o.model] ?? o.model}
                </span>
                <span className="shrink-0 font-mono text-gold">{s ? usd(s.cost_usd, 4) : "—"}</span>
              </div>
              <div className="text-muted">
                {s && s.tasks + (s.cached ?? 0) > 0 ? (
                  <>
                    {s.tasks} call{s.tasks === 1 ? "" : "s"}
                    {s.cached ? `, ${s.cached} from cache` : ""}
                    {s.tasks ? ` · ${secs(s.avg_ms ?? 0)} avg · ${usd(s.cost_usd / Math.max(1, s.tasks), 4)} per call` : ""}
                    {s.failed ? ` · ${s.failed} failed` : ""}
                    {stages && <span className="block">Used for {stages}</span>}
                  </>
                ) : (
                  "Not used yet"
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function MachineCard({
  m,
  onToggle,
  onToggleMachine,
  onRemove,
  children,
  ledger = null,
}: {
  m: Machine;
  onToggle: (w: Worker) => void;
  onToggleMachine: () => void;
  onRemove: (w: Worker) => void;
  children?: React.ReactNode;
  ledger?: Ledger | null;
}) {
  const roles = (["gpu", "model", "service"] as const).filter((r) => m.parts.some((p) => p.role === r));
  const location = m.parts[0].w.location;
  return (
    <li className="rounded-xl border border-line bg-ink/50 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 shrink-0 rounded-full ${m.up ? STATUS_DOT.up : m.enabled ? STATUS_DOT.down : STATUS_DOT.disabled} ${m.inflight ? "pulse" : ""}`} />
            <span className="truncate font-medium">
              <span className="mr-1 text-muted">{LOCATION_ICON[location]}</span>
              {m.host}
            </span>
          </div>
          <div className="mt-0.5 truncate text-xs text-muted">{m.summary}</div>
        </div>
        {m.parts.length > 1 && !m.cloud && <OfflineButton enabled={m.enabled} onClick={onToggleMachine} what={`all of ${m.host}`} />}
      </div>
      {roles.map((role) => (
        <div key={role} className="mt-2">
          {roles.length > 1 && <div className="mb-1 text-[10px] uppercase tracking-wider text-muted">{ROLE_LABEL[role]}</div>}
          <ul className="space-y-1">
            {m.parts
              .filter((p) => p.role === role)
              .map((p) => (
                <PartRow key={p.w.id} p={p} onToggle={() => onToggle(p.w)} onRemove={() => onRemove(p.w)} />
              ))}
          </ul>
        </div>
      ))}
      {m.cloud && m.parts.map((p) => <CloudModels key={p.w.id} w={p.w} ledger={ledger} />)}
      {children}
    </li>
  );
}

function LimitEditor({ current, onDone }: { current: number | null; onDone: () => void }) {
  const [value, setValue] = useState(current != null ? String(current) : "");
  const [error, setError] = useState<string | null>(null);
  const save = async (amount: number | null) => {
    try {
      await api.put("/api/fabric/limit", { budget_usd: amount });
      onDone();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
      <span className="text-muted">$</span>
      <input
        aria-label="Spending limit in dollars"
        type="number"
        min={0}
        step={1}
        value={value}
        placeholder="no limit"
        onChange={(e) => setValue(e.target.value)}
        className="w-24 rounded border border-line bg-ink px-2 py-1"
      />
      <button type="button" onClick={() => save(value === "" ? null : Number(value))} className="rounded bg-gold px-2 py-1 font-semibold text-ink">
        Save
      </button>
      {current != null && (
        <button type="button" onClick={() => save(null)} className="rounded border border-line px-2 py-1 text-muted hover:text-text">
          Remove limit
        </button>
      )}
      {error && <span className="w-full text-bad">{error}</span>}
    </div>
  );
}

function Budget({ ledger, refresh }: { ledger: Ledger; refresh: () => void }) {
  const [editing, setEditing] = useState(false);
  const limit = ledger.budget_usd;
  const stop = ledger.hard_stop_usd;
  const pct = (v: number) => `${limit ? Math.min(100, (v / limit) * 100) : 0}%`;
  return (
    <div className="rounded-xl border border-line bg-ink/50 p-3">
      <div className="flex items-baseline justify-between">
        <span className="label">Cloud spend {ledger.simulated && <span className="text-gold">· simulated</span>}</span>
        <span className="font-mono text-sm">
          {usd(ledger.spent_usd, 4)}
          {limit != null && <span className="text-muted"> / ${limit}</span>}
        </span>
      </div>
      <button type="button" onClick={() => setEditing((e) => !e)} className="mt-1 min-h-6 text-[11px] text-gold hover:underline">
        {editing ? "Cancel" : limit != null ? "Change limit" : "Set a spending limit"}
      </button>
      {editing && (
        <LimitEditor
          current={limit}
          onDone={() => {
            setEditing(false);
            refresh();
          }}
        />
      )}
      {limit != null ? (
        <>
          <div className="relative mt-2 h-2.5 overflow-hidden rounded-full bg-line">
            <div className="absolute inset-y-0 left-0 bg-gold" style={{ width: `max(${pct(ledger.spent_usd)}, 2px)` }} />
            <div className="absolute inset-y-0 bg-gold/40" style={{ left: pct(ledger.spent_usd), width: pct(ledger.reserved_usd) }} />
            {stop != null && stop < limit && (
              <div className="absolute inset-y-0 w-0.5 bg-bad" style={{ left: pct(stop) }} title="Cloud calls stop here" />
            )}
          </div>
          <div className="mt-1 flex justify-between text-[11px] text-muted">
            <span>in flight {usd(ledger.reserved_usd, 4)}</span>
            <span className="text-bad/80">limit ${stop ?? limit}</span>
          </div>
        </>
      ) : (
        <p className="mt-1 text-[11px] text-muted">
          No spending limit set{ledger.reserved_usd > 0 && <>, {usd(ledger.reserved_usd, 4)} in flight</>}.
        </p>
      )}
      <dl className="mt-3 grid grid-cols-3 gap-2 text-center">
        <div className="rounded-lg bg-panel-2 p-2">
          <dt className="text-[10px] uppercase tracking-wider text-muted">Local</dt>
          <dd className="font-mono text-lg">{ledger.local_tasks}</dd>
        </div>
        <div className="rounded-lg bg-panel-2 p-2">
          <dt className="text-[10px] uppercase tracking-wider text-muted">Cloud</dt>
          <dd className="font-mono text-lg">{ledger.cloud_tasks}</dd>
        </div>
        <div className="rounded-lg bg-panel-2 p-2">
          <dt className="text-[10px] uppercase tracking-wider text-muted">Cached</dt>
          <dd className="font-mono text-lg">{ledger.cache_hits}</dd>
        </div>
      </dl>
      <p className="mt-2 text-[11px] text-muted">
        Without the local machines and the cache, this would have cost <span className="text-good">{usd(ledger.avoided_usd, 4)}</span> more.
      </p>
    </div>
  );
}

type ReportedFilm = { id: string; title: string | null; shared: boolean; reports: { label: string; note: string; at: number }[] };

function Reports() {
  const [data] = usePoll<{ films: ReportedFilm[] }>("/api/reports", 30000);
  const films = data?.films ?? [];
  if (!films.length) return null;
  return (
    <div className="rounded-xl border border-bad/40 bg-bad/5 p-3 text-xs">
      <div className="label mb-1 text-bad">Reported by viewers</div>
      <ul className="space-y-1.5">
        {films.map((f) => (
          <li key={f.id}>
            <span className="text-text">{f.title ?? f.id}</span>
            <span className="text-muted">
              {" "}
              · {f.reports.length} report{f.reports.length === 1 ? "" : "s"} · {f.shared ? "still shared" : "no longer shared"}
            </span>
            <span className="block text-muted">{f.reports.map((r) => r.label).join(", ")}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function DecisionRow({ d, showJob = false }: { d: Decision; showJob?: boolean }) {
  const where = d.cached ? "cache" : d.worker_label ?? "—";
  return (
    <li className="border-t border-line/60 py-2 first:border-t-0">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="min-w-0 truncate">
          <span className={d.status === "ok" ? "" : "text-bad"}>{d.label ?? d.kind}</span>
          {showJob && d.stage && <span className="text-muted"> · {d.stage}</span>}
        </span>
        <span className="shrink-0 font-mono text-muted">
          {d.cached ? "cached" : secs(d.latency_ms)} · <span className={d.cost_usd > 0 ? "text-gold" : "text-good"}>{usd(d.cost_usd)}</span>
        </span>
      </div>
      <div className="mt-0.5 flex items-center gap-1.5 text-[11px]">
        <span
          className={`rounded px-1.5 py-px ${
            d.cached ? "bg-sky/15 text-sky" : d.is_cloud ? "bg-gold/15 text-gold" : "bg-good/15 text-good"
          }`}
        >
          {d.cached ? "CACHE" : d.is_cloud ? "CLOUD" : "LOCAL"}
        </span>
        <span className="truncate text-muted">
          {where}
          {d.model && !d.cached ? ` · ${d.model}` : ""}
        </span>
      </div>
      {(d.reason || d.error) && (
        <p className={`mt-1 text-[11px] leading-snug ${d.fallbacks.length || d.error ? "text-bad/90" : "text-muted"}`}>
          {d.error ?? d.reason}
        </p>
      )}
    </li>
  );
}

type Props = {
  workers: Worker[] | null;
  ledger: Ledger | null;
  decisions: Decision[] | null;
  mode: string;
  refresh: () => void;
  refreshLedger: () => void;
};

export default function FabricPanel({ workers, ledger, decisions, mode, refresh, refreshLedger }: Props) {
  const [adding, setAdding] = useState(false);
  const toggle = async (w: Worker) => {
    await api.post(`/api/fabric/workers/${w.id}/${w.enabled ? "disable" : "enable"}`).catch(() => undefined);
    refresh();
  };
  const toggleMachine = async (m: Machine) => {
    const action = m.enabled ? "disable" : "enable";
    const targets = m.parts.filter((p) => p.w.enabled === m.enabled && p.w.id !== "fabric-synth");
    await Promise.all(targets.map((p) => api.post(`/api/fabric/workers/${p.w.id}/${action}`).catch(() => undefined)));
    refresh();
  };
  const remove = async (w: Worker) => {
    if (!window.confirm(`Remove ${w.label}? Running tasks finish first; new ones go elsewhere.`)) return;
    await api.del(`/api/fabric/machines/${w.id}`).catch(() => undefined);
    refresh();
  };
  return (
    <section className="card flex flex-col gap-4 p-5" aria-label="Machines">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-xl">Machines</h2>
        {(mode === "mock" || mode === "live") && (
          <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${mode === "mock" ? "bg-gold/15 text-gold" : "bg-good/15 text-good"}`}>
            {mode === "mock" ? "MOCK MODE" : "LIVE"}
          </span>
        )}
      </div>
      {ledger && <Budget ledger={ledger} refresh={refreshLedger} />}
      <div>
        <div className="mb-2 flex items-center justify-between">
          <span className="label">Connected</span>
          <button type="button" onClick={() => setAdding((a) => !a)} className="min-h-6 text-xs text-gold hover:underline">
            {adding ? "Cancel" : "+ Add machine"}
          </button>
        </div>
        {adding && (
          <div className="mb-2">
            <AddMachine
              onDone={() => {
                setAdding(false);
                refresh();
              }}
            />
          </div>
        )}
        <ul className="space-y-2">
          {machines(workers ?? []).map((m) => (
            <MachineCard key={m.host} m={m} onToggle={toggle} onToggleMachine={() => toggleMachine(m)} onRemove={remove} ledger={ledger}>
              {m.parts.some((p) => p.w.id === "muse-cloud") && <ApiKeyEditor onChange={refresh} />}
            </MachineCard>
          ))}
          {!workers && <li className="text-sm text-muted">Connecting…</li>}
        </ul>
      </div>
      <Reports />
      <div className="min-h-0">
        <div className="label mb-1">Recent tasks</div>
        <ul className="max-h-[420px] overflow-y-auto pr-1">
          {(decisions ?? []).map((d) => (
            <DecisionRow key={d.task_id} d={d} showJob />
          ))}
          {decisions?.length === 0 && <li className="py-2 text-sm text-muted">No tasks yet.</li>}
        </ul>
      </div>
    </section>
  );
}
