import { useEffect, useState } from "react";
import { api } from "../api";

export type User = { id: string; email: string; name: string; is_admin: boolean; daily_usd: number | null };
export type AuthState = { accounts: boolean; user: User | null; can_sign_up: boolean };

export function SignIn({ auth, onDone }: { auth: AuthState; onDone: () => void }) {
  const [mode, setMode] = useState<"in" | "up">(auth.can_sign_up ? "up" : "in");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post(mode === "up" ? "/api/auth/sign-up" : "/api/auth/sign-in", { email, name, password });
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-4">
      <h1 className="font-display text-3xl">CinemaFabric</h1>
      <p className="mt-1 text-sm text-muted">
        {mode === "up" ? "Create your account. The first account on a studio becomes its admin." : "Sign in to your studio."}
      </p>
      <form onSubmit={submit} className="card mt-5 space-y-3 p-5">
        <label className="block text-sm">
          Email
          <input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded-lg border border-line bg-ink px-3 py-2 text-sm" />
        </label>
        {mode === "up" && (
          <label className="block text-sm">
            Name <span className="text-muted">(optional)</span>
            <input autoComplete="name" value={name} onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full rounded-lg border border-line bg-ink px-3 py-2 text-sm" />
          </label>
        )}
        <label className="block text-sm">
          Password
          <input type="password" required minLength={mode === "up" ? 10 : 1}
            autoComplete={mode === "up" ? "new-password" : "current-password"} value={password}
            onChange={(e) => setPassword(e.target.value)} className="mt-1 w-full rounded-lg border border-line bg-ink px-3 py-2 text-sm" />
        </label>
        {error && <p role="alert" className="text-sm text-bad">{error}</p>}
        <button type="submit" disabled={busy} className="w-full rounded-lg bg-gold py-2 text-sm font-semibold text-ink disabled:opacity-50">
          {busy ? "One moment…" : mode === "up" ? "Create account" : "Sign in"}
        </button>
      </form>
      {auth.can_sign_up && (
        <button type="button" onClick={() => setMode(mode === "up" ? "in" : "up")} className="mt-3 min-h-6 text-sm text-gold hover:underline">
          {mode === "up" ? "I already have an account" : "Create an account"}
        </button>
      )}
    </main>
  );
}

export function PeoplePanel() {
  const [people, setPeople] = useState<User[]>([]);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const load = () => api.get<{ people: User[] }>("/api/people").then((r) => setPeople(r.people), () => setPeople([]));
  useEffect(() => {
    void load();
  }, []);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setNote(null);
    try {
      await api.post("/api/people", { email, password });
      setNote(`Added ${email}. Give them the password; they can sign in now.`);
      setEmail("");
      setPassword("");
      void load();
    } catch (err) {
      setNote((err as Error).message);
    }
  };

  return (
    <section className="card p-4 text-xs" aria-labelledby="people-heading">
      <h2 id="people-heading" className="label mb-2">People</h2>
      <ul className="space-y-1">
        {people.map((p) => (
          <li key={p.id} className="flex justify-between gap-2">
            <span className="truncate">{p.name} <span className="text-muted">{p.email}</span></span>
            <span className="shrink-0 text-muted">{p.is_admin ? "admin" : p.daily_usd != null ? `$${p.daily_usd}/day` : "default allowance"}</span>
          </li>
        ))}
      </ul>
      <form onSubmit={add} className="mt-3 flex flex-col gap-1.5">
        <input type="email" required placeholder="Email" aria-label="New person's email" value={email} onChange={(e) => setEmail(e.target.value)}
          className="rounded border border-line bg-ink px-2 py-1" />
        <input type="password" required minLength={10} placeholder="Starting password (10+ characters)" aria-label="Starting password"
          autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} className="rounded border border-line bg-ink px-2 py-1" />
        <button type="submit" className="min-h-6 self-start rounded border border-gold/60 px-2 py-1 text-gold">Add person</button>
      </form>
      {note && <p role="status" className="mt-2 text-muted">{note}</p>}
    </section>
  );
}
