import { useEffect, useState } from "react";
import { api } from "../api";

type OllamaModel = { name: string; parameter_size: string; vision: boolean; guard: boolean; meta: boolean; quality: number };
type Inspection = { kind: string; models?: OllamaModel[]; device?: string; caps?: string[] };
type Join = { command: string; command_powershell: string; note: string; token_hint: string };

const TIER = ["", "draft", "standard", "premium"];

export default function AddMachine({ onDone }: { onDone: () => void }) {
  const [kind, setKind] = useState<"ollama" | "gpu-worker">("ollama");
  const [url, setUrl] = useState("http://");
  const [found, setFound] = useState<Inspection | null>(null);
  const [model, setModel] = useState("");
  const [label, setLabel] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [join, setJoin] = useState<Join | null>(null);

  useEffect(() => {
    api.get<Join>("/api/fabric/join").then(setJoin).catch(() => setJoin(null));
  }, []);

  const host = () => {
    try {
      return new URL(url).hostname;
    } catch {
      return "";
    }
  };

  const check = async () => {
    setBusy(true);
    setError(null);
    setFound(null);
    try {
      const res = await api.post<Inspection>("/api/fabric/machines/inspect", { kind, base_url: url, secret: secret || null });
      setFound(res);
      const models = [...(res.models ?? [])].sort((a, b) => Number(b.meta) - Number(a.meta) || b.quality - a.quality);
      const first = models.find((m) => !m.guard) ?? models[0];
      if (first) setModel(first.name);
      setLabel(kind === "ollama" ? `${host()} · ${first?.name ?? ""}` : `${res.device ?? host()} · ${(res.caps ?? []).join(", ")}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = {
        ...(kind === "ollama" ? { kind, base_url: url, label, model } : { kind, base_url: url, label, caps: found?.caps ?? [], device: found?.device ?? "" }),
        secret: secret || null,
      };
      await api.post("/api/fabric/machines", body);
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const models = [...(found?.models ?? [])].sort((a, b) => Number(b.meta) - Number(a.meta) || b.quality - a.quality);

  return (
    <div className="space-y-3 rounded-xl border border-gold/30 bg-ink/60 p-3 text-sm">
      <div className="seg" role="group">
        {(["ollama", "gpu-worker"] as const).map((k) => (
          <button key={k} type="button" aria-pressed={kind === k} onClick={() => (setKind(k), setFound(null))}>
            {k === "ollama" ? "Ollama server" : "GPU worker"}
          </button>
        ))}
      </div>
      <div className="flex gap-2">
        <input
          aria-label="Address"
          value={url}
          onChange={(e) => (setUrl(e.target.value), setFound(null))}
          placeholder={kind === "ollama" ? "http://192.168.0.20:11434" : "http://192.168.0.20:8201"}
          className="min-w-0 flex-1 rounded-lg border border-line bg-ink px-2 py-1.5 text-xs outline-none focus:border-gold/60"
        />
        <button type="button" onClick={check} disabled={busy || url.length < 10} className="rounded-lg border border-line px-3 text-xs hover:border-gold disabled:opacity-40">
          {busy && !found ? "Checking…" : "Check"}
        </button>
      </div>
      <input
        aria-label="Secret"
        type="password"
        autoComplete="off"
        value={secret}
        onChange={(e) => (setSecret(e.target.value), setFound(null))}
        placeholder="Secret, if the machine runs the gate (scripts/serve-ollama.sh prints it)"
        className="w-full rounded-lg border border-line bg-ink px-2 py-1.5 text-xs outline-none focus:border-gold/60"
      />

      {found && kind === "ollama" && (
        <div>
          <div className="label mb-1">Model</div>
          {models.length === 0 && <p className="text-xs text-muted">No models pulled on that server yet.</p>}
          <ul className="max-h-48 space-y-1 overflow-y-auto">
            {models.map((m) => (
              <li key={m.name}>
                <label className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs hover:bg-panel-2">
                  <input type="radio" name="model" checked={model === m.name} onChange={() => (setModel(m.name), setLabel(`${host()} · ${m.name}`))} />
                  <span className="truncate">{m.name}</span>
                  <span className="ml-auto flex shrink-0 gap-1 text-[10px] text-muted">
                    {m.parameter_size && <span>{m.parameter_size}</span>}
                    {m.guard ? <span className="text-sky">content check</span> : <span>{TIER[m.quality]}</span>}
                    {m.vision && <span className="text-good">vision</span>}
                    {!m.meta && <span className="text-bad/80">not Meta</span>}
                  </span>
                </label>
              </li>
            ))}
          </ul>
        </div>
      )}
      {found && kind === "gpu-worker" && (
        <p className="text-xs text-muted">
          {found.device} · serves {(found.caps ?? []).join(", ") || "nothing"}
        </p>
      )}

      {found && (
        <div className="flex gap-2">
          <input
            aria-label="Name"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-line bg-ink px-2 py-1.5 text-xs outline-none focus:border-gold/60"
          />
          <button type="button" onClick={add} disabled={busy || !label || (kind === "ollama" && !model)} className="rounded-lg bg-gold px-3 text-xs font-semibold text-ink disabled:opacity-40">
            Add
          </button>
        </div>
      )}
      {error && <p className="rounded border border-bad/40 bg-bad/10 p-2 text-xs text-bad">{error}</p>}

      {join && (
        <details className="text-xs text-muted">
          <summary className="cursor-pointer">Or have a machine add itself</summary>
          <p className="mt-1">{join.note}</p>
          <p className="mt-2">macOS / Linux</p>
          <code className="mt-1 block break-all rounded bg-panel-2 p-2 font-mono text-[11px] text-text">{join.command}</code>
          <p className="mt-2">Windows PowerShell</p>
          <code className="mt-1 block break-all rounded bg-panel-2 p-2 font-mono text-[11px] text-text">
            {join.command_powershell}
          </code>
        </details>
      )}
    </div>
  );
}
