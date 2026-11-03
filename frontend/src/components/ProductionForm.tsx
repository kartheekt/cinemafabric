import { useEffect, useState } from "react";
import { api, usd } from "../api";
import type { Catalog, Estimate, Job, JobOptions, SavedCharacter } from "../types";

type Ideas = { ideas: string[]; by: string };
type Suggested = { format: JobOptions["format"]; seconds: number; genre: string | null; style: string | null; aspect: string | null; reason: string; by: string };

const STYLE_SWATCH: Record<string, string> = {
  "live-action": "from-amber-700/60 to-slate-900",
  "animation-3d": "from-sky-500/60 to-fuchsia-700/50",
  anime: "from-rose-400/60 to-indigo-700/60",
  noir: "from-zinc-300/50 to-black",
  claymation: "from-orange-400/60 to-lime-800/50",
  watercolor: "from-teal-300/50 to-pink-300/40",
};

type Props = { catalog: Catalog; onCreated: (job: Job) => void; busy: boolean };

const shortLength = (s: number) => (s < 60 ? `${s}s` : s % 60 === 0 ? `${s / 60} min` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);

function lengthLabel(format: string, s: number) {
  if (format === "trailer") return `${s}-second trailer`;
  return s % 60 === 0 ? `${s / 60}-minute movie` : `${shortLength(s)} movie`;
}

function Segmented({ value, options, onChange }: { value: string; options: { id: string; label: string }[]; onChange: (v: string) => void }) {
  return (
    <div className="seg" role="group">
      {options.map((o) => (
        <button key={o.id} type="button" aria-pressed={value === o.id} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Toggle({ checked, onChange, label, hint }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint: string }) {
  return (
    <label className="flex cursor-pointer items-start gap-3 py-1.5">
      <input type="checkbox" className="peer sr-only" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="mt-0.5 h-5 w-9 shrink-0 rounded-full bg-line p-0.5 transition peer-checked:bg-gold/80 peer-focus-visible:ring-2 peer-focus-visible:ring-sky">
        <span className={`block h-4 w-4 rounded-full bg-text transition ${checked ? "translate-x-4" : ""}`} />
      </span>
      <span>
        <span className="block text-sm">{label}</span>
        <span className="block text-xs text-muted">{hint}</span>
      </span>
    </label>
  );
}

export default function ProductionForm({ catalog, onCreated, busy }: Props) {
  const [opts, setOpts] = useState<JobOptions>({
    prompt: "",
    format: "trailer",
    length: "fixed",
    seconds: 30,
    genre: "Sci-fi",
    style: "live-action",
    aspect: "16:9",
    preference: "local_first",
    premium_script: false,
    critic: true,
    narration: true,
    fresh: false,
    review_script: false,
    max_cost_usd: null,
    subtitle_languages: [],
    cast_ids: [],
    voice_language: "en",
    commercial_safe: false,
    agents: true,
    resolution: "1080p",
    fps: 24,
  });
  const [library, setLibrary] = useState<SavedCharacter[]>([]);
  useEffect(() => {
    api.get<{ characters: SavedCharacter[] }>("/api/characters").then((r) => setLibrary(r.characters), () => setLibrary([]));
  }, []);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [ideas, setIdeas] = useState<Ideas | null>(null);
  const [helper, setHelper] = useState<"" | "ideas" | "polish" | "settings">("");
  const [note, setNote] = useState<string | null>(null);
  const set = <K extends keyof JobOptions>(k: K, v: JobOptions[K]) => setOpts((o) => ({ ...o, [k]: v }));
  const forget = async (c: SavedCharacter) => {
    if (!window.confirm(`Remove ${c.name} from your saved characters? Films that used them are not affected.`)) return;
    try {
      await api.del(`/api/characters/${c.id}`);
      setLibrary((l) => (l ?? []).filter((x) => x.id !== c.id));
      setOpts((o) => ({ ...o, cast_ids: o.cast_ids.filter((x) => x !== c.id) }));
    } catch {
      window.alert("Couldn't remove that character. Try again.");
    }
  };
  const fmt = catalog.formats.find((f) => f.id === opts.format) ?? catalog.formats[0];
  const pickFormat = (id: string) => {
    const next = catalog.formats.find((f) => f.id === id) ?? fmt;
    setOpts((o) => ({ ...o, format: next.id, seconds: next.default }));
  };

  useEffect(() => {
    const q = new URLSearchParams({
      format: opts.format,
      length: opts.length,
      seconds: String(opts.seconds ?? fmt.default),
      preference: opts.preference,
      premium_script: String(opts.premium_script),
      critic: String(opts.critic),
      narration: String(opts.narration),
    });
    const t = window.setTimeout(
      () => api.get<Estimate>(`/api/estimate?${q}`).then(setEstimate).catch(() => setEstimate(null)),
      150,
    );
    return () => window.clearTimeout(t);
  }, [opts.format, opts.length, opts.seconds, opts.preference, opts.premium_script, opts.critic, opts.narration, fmt.default]);

  const loadIdeas = async (fresh: boolean) => {
    setHelper("ideas");
    try {
      setIdeas(await api.get<Ideas>(`/api/suggest/ideas${fresh ? "?fresh=true" : ""}`));
    } catch {
      setIdeas(null);
    } finally {
      setHelper("");
    }
  };

  useEffect(() => {
    void loadIdeas(false);
  }, []);

  const polish = async () => {
    setHelper("polish");
    setNote(null);
    try {
      const asked = opts.prompt;
      const res = await api.post<{ idea: string; by: string }>("/api/suggest/polish", { idea: asked });
      if (res.idea) {
        let applied = false;
        setOpts((o) => {
          applied = o.prompt === asked;
          return applied ? { ...o, prompt: res.idea } : o;
        });
        setNote(applied ? `Rewritten by ${res.by}. Edit it freely.` : `You kept typing, so here's the suggestion instead: ${res.idea}`);
      }
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setHelper("");
    }
  };

  const suggestSettings = async () => {
    setHelper("settings");
    setNote(null);
    try {
      const s = await api.post<Suggested>("/api/suggest/settings", { idea: opts.prompt });
      setOpts((o) => ({
        ...o,
        format: s.format,
        length: "fixed",
        seconds: s.seconds,
        genre: s.genre ?? o.genre,
        style: s.style ?? o.style,
        aspect: s.aspect ?? o.aspect,
      }));
      setNote(`${s.reason} Suggested by ${s.by}; change anything you like.`);
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setHelper("");
    }
  };

  const submit = async () => {
    setError(null);
    setSubmitting(true);
    try {
      onCreated(await api.post<Job>("/api/jobs", opts));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="card p-5" aria-label="New production">
      <h2 className="font-display text-xl">New production</h2>
      <p className="mt-1 text-sm text-muted">Describe the story in a line or two. You'll get a script, storyboard, poster and the final cut.</p>

      <label className="label mt-5 block" htmlFor="prompt">
        Idea
      </label>
      <textarea
        id="prompt"
        rows={4}
        value={opts.prompt}
        onChange={(e) => set("prompt", e.target.value)}
        placeholder="e.g. a ferry captain keeps picking up the same passenger every night"
        className="mt-2 w-full resize-none rounded-xl border border-line bg-ink p-3 text-sm outline-none placeholder:text-muted focus:border-gold/60"
      />
      <div className="mt-2 flex flex-wrap gap-1.5">
        <button
          type="button"
          onClick={polish}
          disabled={helper !== "" || opts.prompt.trim().length < 8}
          className="rounded-lg border border-line px-2.5 py-1 text-xs hover:border-gold/60 disabled:opacity-40"
        >
          {helper === "polish" ? "Polishing…" : "Polish my idea"}
        </button>
        <button
          type="button"
          onClick={suggestSettings}
          disabled={helper !== "" || opts.prompt.trim().length < 8}
          className="rounded-lg border border-line px-2.5 py-1 text-xs hover:border-gold/60 disabled:opacity-40"
        >
          {helper === "settings" ? "Thinking…" : "Suggest settings"}
        </button>
      </div>
      {note && (
        <p className="mt-2 text-xs text-muted" role="status">
          {note}
        </p>
      )}
      {ideas && ideas.ideas.length > 0 && (
        <div className="mt-3">
          <div className="flex flex-wrap gap-1.5">
            {ideas.ideas.map((ex) => (
              <button
                key={ex}
                type="button"
                onClick={() => set("prompt", ex)}
                title={ex}
                className="rounded-full border border-line px-2.5 py-1 text-left text-xs text-muted hover:border-gold/50 hover:text-text"
              >
                {ex.length > 52 ? ex.slice(0, 52) + "…" : ex}
              </button>
            ))}
          </div>
          <p className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-muted">
            Ideas by {ideas.by}.
            <button type="button" onClick={() => loadIdeas(true)} disabled={helper !== ""} className="min-h-6 underline hover:text-text">
              {helper === "ideas" ? "Thinking…" : "New ideas"}
            </button>
          </p>
        </div>
      )}

      <div className="mt-5 space-y-4">
        <div>
          <div className="label mb-2">Make a</div>
          <Segmented value={opts.format} options={catalog.formats} onChange={pickFormat} />
        </div>

        <div>
          <div className="label mb-2">Length</div>
          <Segmented
            value={opts.length}
            options={[
              { id: "fixed", label: "Set it myself" },
              { id: "auto", label: "Let the story decide" },
            ]}
            onChange={(v) => set("length", v as JobOptions["length"])}
          />
          {opts.length === "fixed" ? (
            <div className="mt-3 rounded-xl border border-line bg-ink/60 p-3">
              <div className="flex items-baseline justify-between">
                <span className="text-sm">{lengthLabel(opts.format, opts.seconds ?? fmt.default)}</span>
                <span className="text-[11px] text-muted">
                  {fmt.min}s to {fmt.max >= 120 ? `${fmt.max / 60} min` : `${fmt.max}s`}
                </span>
              </div>
              <input
                type="range"
                aria-label="Length in seconds"
                min={fmt.min}
                max={fmt.max}
                step={opts.format === "trailer" ? 5 : 15}
                value={opts.seconds ?? fmt.default}
                onChange={(e) => set("seconds", Number(e.target.value))}
                className="mt-2 w-full accent-[var(--color-gold)]"
              />
              <div className="mt-1 flex flex-wrap gap-1.5">
                {fmt.presets.map((p) => (
                  <button
                    key={p}
                    type="button"
                    aria-pressed={opts.seconds === p}
                    onClick={() => set("seconds", p)}
                    className={`min-h-6 rounded-full border px-2.5 py-0.5 text-xs ${
                      opts.seconds === p ? "border-gold text-text" : "border-line text-muted hover:text-text"
                    }`}
                  >
                    {shortLength(p)}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <p className="mt-1.5 text-xs text-muted">
              The writer picks a length between {shortLength(fmt.min)} and {shortLength(fmt.max)} based on how much story there is, and
              says why.
            </p>
          )}
        </div>

        <div>
          <div className="label mb-2">Style</div>
          <div className="grid grid-cols-3 gap-2">
            {catalog.styles.map((s) => (
              <button
                key={s.id}
                type="button"
                aria-pressed={opts.style === s.id}
                onClick={() => set("style", s.id)}
                className={`relative h-14 overflow-hidden rounded-lg border text-left text-xs transition ${
                  opts.style === s.id ? "border-gold" : "border-line hover:border-muted"
                }`}
              >
                <span className={`absolute inset-0 bg-gradient-to-br ${STYLE_SWATCH[s.id] ?? "from-slate-600 to-slate-900"}`} />
                <span className="absolute bottom-1.5 left-2 font-medium drop-shadow">{s.label}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="label mb-2 block" htmlFor="genre">
              Genre
            </label>
            <select
              id="genre"
              value={opts.genre}
              onChange={(e) => set("genre", e.target.value)}
              className="w-full rounded-lg border border-line bg-ink px-3 py-2 text-sm"
            >
              {catalog.genres.map((g) => (
                <option key={g}>{g}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="label mb-2 block" htmlFor="aspect">
              Aspect ratio
            </label>
            <select
              id="aspect"
              value={opts.aspect}
              onChange={(e) => set("aspect", e.target.value)}
              className="w-full rounded-lg border border-line bg-ink px-3 py-2 text-sm"
            >
              {catalog.aspects.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label mb-2 block" htmlFor="resolution">
              Resolution
            </label>
            <select
              id="resolution"
              value={opts.resolution}
              onChange={(e) => set("resolution", e.target.value)}
              className="w-full rounded-lg border border-line bg-ink px-3 py-2 text-sm"
            >
              {catalog.resolutions.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label mb-2 block" htmlFor="fps">
              Frame rate
            </label>
            <select
              id="fps"
              value={String(opts.fps)}
              onChange={(e) => set("fps", Number(e.target.value))}
              className="w-full rounded-lg border border-line bg-ink px-3 py-2 text-sm"
            >
              {catalog.frame_rates.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <div className="label mb-2">Where to run</div>
          <Segmented value={opts.preference} options={catalog.preferences} onChange={(v) => set("preference", v)} />
          <p className="mt-1.5 text-xs text-muted">
            {opts.preference === "local_first" && "Uses your own machines when they're free. The cloud only gets what they can't do, like images."}
            {opts.preference === "balanced" && "Decides per task. If a local machine is busy, it'll spend a fraction of a cent to go faster."}
            {opts.preference === "cloud_first" && "Sends everything it can to the Meta Model API. The local machines are the backup."}
          </p>
          {catalog.cloud_text_model?.includes("contributor") && (
            <p className="mt-1 text-xs text-muted">
              Cloud writing uses {catalog.cloud_text_model}, a contributor tier: Meta may use what's sent to it to improve its
              models. Keep confidential ideas local, or change the cloud text model in your profile.
            </p>
          )}
        </div>

        <div className="rounded-xl border border-line bg-ink/60 px-3 py-1">
          <Toggle checked={opts.review_script} onChange={(v) => set("review_script", v)} label="Let me check the script first" hint="Pauses before any images are made so you can edit, reorder or cut shots." />
          {opts.format === "trailer" && <Toggle checked={opts.premium_script} onChange={(v) => set("premium_script", v)} label="Better writing" hint="Uses Muse Wonder for the story and script. Costs a bit more." />}
          <Toggle checked={opts.critic} onChange={(v) => set("critic", v)} label="Frame check" hint="A vision model scores each frame and redraws weak ones. Checks for real people, brands and existing characters run either way." />
          <Toggle checked={opts.narration} onChange={(v) => set("narration", v)} label="Voice-over" hint="Adds narration and subtitles." />
          <Toggle checked={opts.fresh} onChange={(v) => set("fresh", v)} label="Don't reuse old results" hint="Skips the cache, so you pay for every step again." />
          <Toggle
            checked={opts.commercial_safe}
            onChange={(v) => set("commercial_safe", v)}
            label="Commercial-safe"
            hint="Leaves out models licensed for non-commercial use only. The voice-over becomes on-screen captions and the music becomes the built-in score."
          />
          <Toggle
            checked={opts.agents}
            onChange={(v) => set("agents", v)}
            label="Agent crew"
            hint="A script doctor, continuity supervisor, editor, translation reviewer, test screening with director fixes, and a release writer. Adds a few minutes and a few cents."
          />
        </div>

        {opts.narration && catalog.voice_languages?.length > 1 && (
          <div className="flex items-center justify-between gap-3 rounded-xl border border-line bg-ink/60 px-3 py-2">
            <label htmlFor="voice-language" className="text-sm">
              Voice-over language
              <span className="block text-xs text-muted">Spoken by a local voice model; English captions stay on the film.</span>
            </label>
            <select
              id="voice-language"
              value={opts.voice_language}
              onChange={(e) => set("voice_language", e.target.value)}
              className="rounded-lg border border-line bg-ink px-2 py-1 text-sm"
            >
              {catalog.voice_languages.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.label}
                </option>
              ))}
            </select>
          </div>
        )}

        {library.length > 0 && (
          <fieldset className="rounded-xl border border-line bg-ink/60 px-3 py-2">
            <legend className="sr-only">Returning cast</legend>
            <div className="text-sm">Returning cast</div>
            <div className="text-xs text-muted">Up to 3 characters you saved, with the same look as before.</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {library.map((c) => {
                const on = opts.cast_ids.includes(c.id);
                return (
                  <span key={c.id} className="flex items-center">
                    <button
                      type="button"
                      aria-pressed={on}
                      disabled={!on && opts.cast_ids.length >= 3}
                      onClick={() => set("cast_ids", on ? opts.cast_ids.filter((x) => x !== c.id) : [...opts.cast_ids, c.id])}
                      title={c.look}
                      className={`flex items-center gap-2 rounded-l-full border py-0.5 pl-0.5 pr-2 text-xs disabled:opacity-40 ${
                        on ? "border-gold bg-gold/15 text-gold" : "border-line text-muted hover:text-text"
                      }`}
                    >
                      <img src={c.image} alt="" className="h-6 w-6 rounded-full object-cover" />
                      {c.name}
                    </button>
                    <button
                      type="button"
                      onClick={() => forget(c)}
                      aria-label={`Remove ${c.name} from saved characters`}
                      title="Remove from saved characters"
                      className="min-h-7 rounded-r-full border border-l-0 border-line px-2 text-xs text-muted hover:border-bad/60 hover:text-bad"
                    >
                      ✕
                    </button>
                  </span>
                );
              })}
            </div>
          </fieldset>
        )}

        {opts.narration && catalog.languages?.length > 0 && (
          <fieldset className="rounded-xl border border-line bg-ink/60 px-3 py-2">
            <legend className="sr-only">Subtitles in other languages</legend>
            <div className="text-sm">Subtitles in other languages</div>
            <div className="text-xs text-muted">
              Up to {catalog.max_languages}. Translated by a model on the fabric; viewers pick them in the player.
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {catalog.languages.map((l) => {
                const on = opts.subtitle_languages.includes(l.id);
                const full = !on && opts.subtitle_languages.length >= catalog.max_languages;
                return (
                  <button
                    key={l.id}
                    type="button"
                    aria-pressed={on}
                    disabled={full}
                    onClick={() =>
                      set("subtitle_languages", on ? opts.subtitle_languages.filter((x) => x !== l.id) : [...opts.subtitle_languages, l.id])
                    }
                    className={`rounded-full border px-2.5 py-1 text-xs disabled:opacity-40 ${
                      on ? "border-gold bg-gold/15 text-gold" : "border-line text-muted hover:text-text"
                    }`}
                  >
                    {l.label}
                  </button>
                );
              })}
            </div>
          </fieldset>
        )}

        <div className="flex items-center justify-between gap-3 rounded-xl border border-line bg-ink/60 px-3 py-2">
          <label htmlFor="max-cost" className="text-sm">
            Cost limit for this run
            <span className="block text-xs text-muted">Optional. Cloud calls stop once it's reached.</span>
          </label>
          <div className="flex items-center gap-1">
            <span className="text-muted">$</span>
            <input
              id="max-cost"
              type="number"
              min={0}
              step={0.05}
              placeholder="none"
              value={opts.max_cost_usd ?? ""}
              onChange={(e) => set("max_cost_usd", e.target.value === "" || Number(e.target.value) <= 0 ? null : Number(e.target.value))}
              className="w-20 rounded-lg border border-line bg-ink px-2 py-1 text-right text-sm outline-none focus:border-gold/60"
            />
          </div>
        </div>
      </div>

      {estimate && (
        <details className="mt-4 rounded-xl border border-line bg-ink/60 p-3 text-sm">
          <summary className="flex cursor-pointer list-none items-center justify-between">
            <span className="text-muted">Rough cost</span>
            <span className="font-mono text-gold">
              {estimate.min_usd != null ? `${usd(estimate.min_usd, 3)} – ${usd(estimate.max_usd, 3)}` : usd(estimate.total_usd, 3)}
            </span>
          </summary>
          <table className="mt-2 w-full text-xs">
            <tbody>
              {estimate.lines.map((l) => (
                <tr key={l.item} className="border-t border-line/60">
                  <td className="py-1 pr-2">
                    {l.item} <span className="text-muted">×{l.count}</span>
                  </td>
                  <td className="py-1 pr-2 text-muted">{l.where}</td>
                  <td className="py-1 text-right font-mono">{usd(l.usd, 3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[11px] text-muted">{estimate.assumes}</p>
        </details>
      )}

      {error && <p className="mt-3 rounded-lg border border-bad/40 bg-bad/10 p-2 text-sm text-bad">{error}</p>}

      <button
        type="button"
        onClick={submit}
        disabled={submitting || opts.prompt.trim().length < 8}
        className="mt-4 w-full rounded-xl bg-gold py-3 font-semibold text-ink transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {submitting ? "Starting…" : busy ? "Add to queue" : "Start"}
      </button>
    </section>
  );
}
