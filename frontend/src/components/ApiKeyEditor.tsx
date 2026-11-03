import { useEffect, useState } from "react";
import { api } from "../api";

type KeyInfo = { api_key_set: boolean; api_key_hint: string | null; api_key_source: "env" | "ui" | null };

export default function ApiKeyEditor({ onChange }: { onChange: () => void }) {
  const [info, setInfo] = useState<KeyInfo | null>(null);
  const [editing, setEditing] = useState(false);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => api.get<KeyInfo>("/api/fabric/settings").then(setInfo).catch(() => setInfo(null));
  useEffect(() => {
    load();
  }, []);

  const save = async (value: string | null) => {
    setBusy(true);
    setError(null);
    try {
      setInfo(await api.put<KeyInfo>("/api/fabric/api-key", { key: value }));
      setEditing(false);
      setKey("");
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!info) return null;
  return (
    <div className="mt-2 border-t border-line/60 pt-2 text-[11px]">
      {!editing ? (
        <div className="flex items-center justify-between gap-2">
          <span className="text-muted">
            {info.api_key_set
              ? `API key ${info.api_key_hint} (${info.api_key_source === "ui" ? "saved here" : "from .env"})`
              : "No API key yet. Images and cloud writing need one."}
          </span>
          <button type="button" onClick={() => setEditing(true)} className="min-h-6 shrink-0 text-gold hover:underline">
            {info.api_key_set ? "Change" : "Add key"}
          </button>
        </div>
      ) : (
        <div className="space-y-1.5">
          <div className="flex gap-1.5">
            <input
              type="password"
              autoComplete="off"
              aria-label="Meta Model API key"
              placeholder="Paste your Meta Model API key"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              className="min-w-0 flex-1 rounded border border-line bg-ink px-2 py-1"
            />
            <button type="button" disabled={busy || !key.trim()} onClick={() => save(key)} className="rounded bg-gold px-2 font-semibold text-ink disabled:opacity-40">
              {busy ? "Checking…" : "Save"}
            </button>
            <button type="button" onClick={() => (setEditing(false), setError(null))} className="rounded border border-line px-2 text-muted">
              Cancel
            </button>
          </div>
          <p className="text-muted">
            The key is checked with Meta, then stored on this server's disk (like .env), never sent back to the browser.
          </p>
          {info.api_key_source === "ui" && (
            <button type="button" onClick={() => save(null)} className="text-muted underline hover:text-text">
              Forget the saved key
            </button>
          )}
          {error && <p className="text-bad">{error}</p>}
        </div>
      )}
    </div>
  );
}
