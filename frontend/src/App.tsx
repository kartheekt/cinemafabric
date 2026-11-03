import { useEffect, useRef, useState } from "react";
import { api, usd, usePoll } from "./api";
import { type AuthState, PeoplePanel, SignIn, type User } from "./components/Auth";
import FabricPanel from "./components/FabricPanel";
import JobView from "./components/JobView";
import ProductionForm from "./components/ProductionForm";
import type { Catalog, Decision, Job, JobSummary, Ledger, Worker } from "./types";

function History({ jobs, active, onSelect }: { jobs: JobSummary[]; active: string | null; onSelect: (id: string) => void }) {
  if (!jobs.length) return null;
  return (
    <section className="card p-4" aria-label="Past productions">
      <div className="label mb-2">Past runs</div>
      <ul className="space-y-1">
        {jobs.map((j) => (
          <li key={j.id}>
            <button
              type="button"
              onClick={() => onSelect(j.id)}
              className={`flex w-full items-center gap-3 rounded-lg p-1.5 text-left hover:bg-panel-2 ${active === j.id ? "bg-panel-2" : ""}`}
            >
              <span className="h-12 w-8 shrink-0 overflow-hidden rounded border border-line bg-ink">
                {j.poster && <img src={j.poster} alt="" className="h-full w-full object-cover" />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm">{j.title ?? j.prompt}</span>
                <span className="block text-[11px] text-muted">
                  {j.runtime_label} · {j.status}
                  {j.cost_usd != null && ` · ${usd(j.cost_usd)}`}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const load = () =>
    api.get<AuthState>("/api/auth/me").then(setAuth, () => setAuth({ accounts: false, user: null, can_sign_up: false }));
  useEffect(() => {
    void load();
  }, []);
  if (!auth) return <div className="p-10 text-sm text-muted">Connecting to studio…</div>;
  if (auth.accounts && !auth.user) return <SignIn auth={auth} onDone={load} />;
  return <Studio user={auth.user} onSignOut={() => api.post("/api/auth/sign-out").then(load, load)} />;
}

function Studio({ user, onSignOut }: { user: User | null; onSignOut: () => void }) {
  const [catalog] = usePoll<Catalog>("/api/options", 60000);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [jobs, refreshJobs] = usePoll<{ jobs: JobSummary[] }>("/api/jobs", 5000);
  const [job, refreshJob] = usePoll<Job>(activeId ? `/api/jobs/${activeId}` : null, 1500);
  const [workers, refreshWorkers] = usePoll<{ mode: string; workers: Worker[] }>("/api/fabric/workers", 2000);
  const [ledger, refreshLedger] = usePoll<Ledger>("/api/fabric/ledger", 2500);
  const [decisions] = usePoll<{ decisions: Decision[] }>("/api/fabric/decisions?limit=60", 2500);

  // A fresh page starts on the idea box. Only a film still being made opens by itself, so that a reload doesn't lose it.
  const opened = useRef(false);
  useEffect(() => {
    if (opened.current || !jobs) return;
    opened.current = true;
    const making = jobs.jobs.find((j) => j.status === "running" || j.status === "queued");
    if (making) setActiveId(making.id);
  }, [jobs]);

  const busy = jobs?.jobs.some((j) => j.status === "running" || j.status === "queued") ?? false;
  const mode = workers?.mode ?? catalog?.mode ?? "…";

  return (
    <div className="mx-auto max-w-[1680px] px-4 pb-10 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3 py-5">
        <div className="flex items-center gap-3">
          <img src="/favicon.svg" alt="" className="h-9 w-9" />
          <div>
            <h1 className="font-display text-2xl tracking-wide">CinemaFabric</h1>
            <p className="text-xs text-muted">Trailers and short films from a one-line idea, made on your own machines and the Meta Model API</p>
          </div>
        </div>
        {ledger && (
          <div
            className="flex items-center gap-3 rounded-full border border-line bg-panel px-4 py-1.5 text-xs"
            title="Worked out from Meta's list prices for calls made through CinemaFabric. Your Meta account is the actual bill, and also includes anything else using the same account."
          >
            <span className="text-muted">Spent (est.)</span>
            <span className="font-mono text-gold">{usd(ledger.spent_usd, 4)}</span>
            {ledger.budget_usd != null && <span className="text-muted">of ${ledger.budget_usd}</span>}
            {ledger.simulated && <span className="rounded bg-gold/15 px-1.5 text-gold">mock</span>}
          </div>
        )}
        {user && (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-muted">{user.name}</span>
            <button type="button" onClick={onSignOut} className="min-h-6 rounded border border-line px-2 hover:border-gold">
              Sign out
            </button>
          </div>
        )}
      </header>

      <main className="grid grid-cols-1 gap-5 xl:grid-cols-[360px_minmax(0,1fr)_380px] lg:grid-cols-[360px_minmax(0,1fr)]">
        <div className="space-y-5">
          {catalog ? (
            <ProductionForm
              catalog={catalog}
              busy={busy}
              onCreated={(j) => {
                setActiveId(j.id);
                refreshJobs();
              }}
            />
          ) : (
            <div className="card p-5 text-sm text-muted">Connecting to studio…</div>
          )}
          <History jobs={jobs?.jobs ?? []} active={activeId} onSelect={setActiveId} />
        </div>

        <div className="min-w-0">
          {job ? (
            <JobView
              key={job.id}
              job={job}
              onRetry={refreshJob}
              onOpen={(id) => {
                setActiveId(id);
                refreshJobs();
              }}
              exportPresets={catalog?.exports ?? []}
            />
          ) : (
            <section className="card flex min-h-[420px] flex-col items-center justify-center p-10 text-center">
              <p className="font-display text-3xl">What's your story?</p>
              <p className="mt-2 max-w-md text-sm text-muted">
                Describe an idea and start it. Your film takes shape here step by step, and the Machines panel shows which machine
                picked up each task. Earlier films are under Past runs.
              </p>
            </section>
          )}
        </div>

        <div className="space-y-5 lg:col-span-2 xl:col-span-1">
          {user?.is_admin && <PeoplePanel />}
          <FabricPanel workers={workers?.workers ?? null} ledger={ledger} decisions={decisions?.decisions ?? null} mode={mode} refresh={refreshWorkers} refreshLedger={refreshLedger} />
        </div>
      </main>
      <footer className="mt-8 text-center text-[11px] text-muted">
        Built with Llama · Uses Meta models under their own licences · <a className="underline hover:text-text" href="https://www.llama.com/llama3_1/use-policy/" target="_blank" rel="noreferrer">Llama Acceptable Use Policy</a>
      </footer>
    </div>
  );
}
