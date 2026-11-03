import { useEffect, useRef, useState } from "react";

const ADMIN_KEY = "cf-admin-password";

function adminPassword(): string {
  try {
    return sessionStorage.getItem(ADMIN_KEY) ?? "";
  } catch {
    return "";
  }
}

async function request<T>(path: string, init?: RequestInit, retried = false): Promise<T> {
  const password = adminPassword();
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(password ? { "X-Admin-Password": password } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (res.status === 401 && !retried && path.startsWith("/api/fabric/")) {
    const entered = window.prompt(password ? "Wrong password. Admin password:" : "Admin password:");
    if (entered) {
      try {
        sessionStorage.setItem(ADMIN_KEY, entered);
      } catch {
        /* storage blocked */
      }
      return request<T>(path, init, true);
    }
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not json */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T,>(path: string, body: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  del: <T,>(path: string) => request<T>(path, { method: "DELETE" }),
};

/** Poll an endpoint every `ms` while `enabled`; returns the latest value. */
export function usePoll<T>(path: string | null, ms: number, enabled = true): [T | null, () => void] {
  const [data, setData] = useState<T | null>(null);
  const [tick, setTick] = useState(0);
  const pathRef = useRef(path);
  const shownFor = useRef(path);
  pathRef.current = path;
  if (shownFor.current !== path) {
    shownFor.current = path;
    if (data !== null) setData(null);
  }

  useEffect(() => {
    if (!path) {
      setData(null);
      return;
    }
    let alive = true;
    const load = () =>
      api
        .get<T>(path)
        .then((d) => alive && pathRef.current === path && setData(d))
        .catch(() => undefined);
    load();
    if (!enabled) return () => void (alive = false);
    const id = window.setInterval(load, ms);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [path, ms, enabled, tick]);

  return [data, () => setTick((t) => t + 1)];
}

export const usd = (v: number | null | undefined, digits = 4) =>
  v == null ? "—" : v === 0 ? "$0" : v < 0.0001 ? "<$0.0001" : `$${v.toFixed(digits)}`;

export const secs = (ms: number) => (ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`);
