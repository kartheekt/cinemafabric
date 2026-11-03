"""Measure CinemaFabric on a running studio and print a Markdown report.

    python -m cinemafabric.tools.evaluate films    [--refusals FILE]   # films end to end, and requests to refuse
    python -m cinemafabric.tools.evaluate compare  [--ideas 3]         # the same ideas under each routing preference
    python -m cinemafabric.tools.evaluate failover [--worker ID]       # take a machine offline mid-film

Common options: --studio http://127.0.0.1:8000 and --out FILE (default: print). Every film runs with the cache off.

FILE holds the requests the content check must refuse, one per line as "label | request". The report shows only the
labels, so the wording of harmful requests stays out of the repository.
"""

import argparse
import re
import statistics
import time
from pathlib import Path

import httpx

IDEAS = [
    ("Drama", "live-action", "A retired ferry pilot on the Brahmaputra teaches his granddaughter to read the river at night"),
    ("Comedy", "animation-3d", "Two rival chai stalls at a railway junction must share one kettle during a cyclone"),
    ("Sci-fi", "noir", "A night-shift radio operator keeps receiving weather reports for a storm that hasn't happened yet"),
    ("Fantasy", "watercolor", "A girl in a hill town finds that the old postbox delivers letters to the people in her dreams"),
]
PREFERENCES = [("local_first", "Local-first"), ("balanced", "Balanced"), ("cloud_first", "Cloud-first")]

# Tasks starting this long after everything before them finished belong to a later session (a redo or a note).
GAP_S = 120
SKIPPED = re.compile(r"(\S+) skipped \((disabled|circuit open|removed|[^)]*unreachable[^)]*)\)")


def refusals_from(path: str | None) -> list[tuple[str, str]]:
    """(label, request) pairs the content check must refuse; none if no file is given."""
    if not path:
        return []
    pairs = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        label, _, request = line.partition("|")
        if request.strip():
            pairs.append((label.strip(), request.strip()))
    return pairs


def film(idea: tuple[str, str, str], preference: str = "local_first") -> dict:
    genre, style, prompt = idea
    return {"prompt": prompt, "format": "trailer", "length": "fixed", "seconds": 20, "genre": genre, "style": style,
            "aspect": "16:9", "preference": preference, "max_cost_usd": 0.6, "fresh": True}


def start(client: httpx.Client, body: dict) -> tuple[dict | None, str | None]:
    r = client.post("/api/jobs", json=body)
    if r.status_code != 200:
        return None, r.json().get("detail", r.text)
    return r.json(), None


def wait(client: httpx.Client, job: dict, began: float, limit_s: float = 900) -> tuple[dict, str | None, float]:
    while time.time() - began < limit_s:
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(3)
    return job, job.get("error"), time.time() - began


def run(client: httpx.Client, body: dict, limit_s: float = 900) -> tuple[dict | None, str | None, float]:
    began = time.time()
    job, error = start(client, body)
    if not job:
        return None, error, time.time() - began
    return wait(client, job, began, limit_s)


def decisions(client: httpx.Client, job_id: str) -> list[dict]:
    return client.get(f"/api/jobs/{job_id}/decisions").json()["decisions"]


def _union(spans: list[tuple[float, float]]) -> float:
    total, end = 0.0, float("-inf")
    for a, b in sorted(spans):
        if b > end:
            total += b - max(a, end)
            end = b
    return total


def usage(rows: list[dict]) -> dict:
    """How the first production session used the machines: the same measures as the film page's timeline."""
    bars = sorted((d["ts"] - d["latency_ms"] / 1000, d["ts"], d["is_cloud"], d["worker_label"] if not d["is_cloud"] else d["model"])
                  for d in rows if d["status"] == "ok" and not d["cached"] and d["worker_id"] and d["latency_ms"] > 0)
    first, reach = [], float("-inf")
    for bar in bars:
        if first and bar[0] > reach + GAP_S:
            break
        first.append(bar)
        reach = max(reach, bar[1])
    if not first:
        return {"span": 0.0, "local_busy": 0.0, "peak": 0, "local_rows": 0, "cloud_rows": 0}
    span = max(b[1] for b in first) - first[0][0]
    now = peak = 0
    for _, step in sorted([(b[0], 1) for b in first] + [(b[1], -1) for b in first], key=lambda e: (e[0], e[1])):
        now += step
        peak = max(peak, now)
    local = [b for b in first if not b[2]]
    return {"span": span, "local_busy": _union([(b[0], b[1]) for b in local]) / span if span else 0.0, "peak": peak,
            "local_rows": len({b[3] for b in local}), "cloud_rows": len({b[3] for b in first if b[2]})}


def measures(client: httpx.Client, job: dict, seconds: float) -> dict:
    r = job.get("receipt") or {}
    scores = [s["review"]["score"] for s in job.get("shots", []) if s.get("review")]
    qa = (job.get("qa") or {}).get("similarity")
    total = r.get("local_tasks", 0) + r.get("cloud_tasks", 0)
    return {"title": (job.get("story") or {}).get("title", "—"), "seconds": seconds, "cost": r.get("cost_usd", 0.0),
            "local": r.get("local_tasks", 0), "tasks": total, "frame": statistics.mean(scores) if scores else None,
            "narration": qa, "watermark": bool((job.get("watermark") or {}).get("verified")),
            **usage(decisions(client, job["id"]))}


def pct(x: float | None) -> str:
    return f"{x:.0%}" if x is not None else "—"


def score(x: float | None) -> str:
    return f"{x:.1f}" if x is not None else "—"


def films(client: httpx.Client, args: argparse.Namespace) -> list[str]:
    lines = ["## Films made end to end", "",
             "20-second trailers, local-first routing, a $0.60 limit per film and no cache.", "",
             "| Idea | Genre and style | Result | Time | Cloud cost | Tasks run locally | Frame score | Narration match | Watermark |",
             "|---|---|---|---|---|---|---|---|---|"]
    for idea in IDEAS[: args.ideas]:
        job, error, seconds = run(client, film(idea))
        print(f"{idea[2][:60]}… -> {job and job['status']} in {seconds:.0f}s {error or ''}")
        if not job or job["status"] != "done":
            lines.append(f"| {idea[2]} | {idea[0]}, {idea[1]} | failed: {(error or '')[:80]} | {seconds:.0f} s | | | | | |")
            continue
        m = measures(client, job, seconds)
        lines.append(f"| {idea[2]} | {idea[0]}, {idea[1]} | *{m['title']}* | {seconds:.0f} s | ${m['cost']:.3f} "
                     f"| {m['local']} of {m['tasks']} | {score(m['frame'])} | {pct(m['narration'])} "
                     f"| {'Verified' if m['watermark'] else 'No'} |")
    refusals = refusals_from(args.refusals)
    if refusals:
        lines += ["", "## Requests that must be refused", "",
                  "Described by category; the wording is kept out of the repository.", "",
                  "| Request | Refused | Cost | Category given |", "|---|---|---|---|"]
    for label, idea in refusals:
        body = {"prompt": idea, "format": "trailer", "length": "fixed", "seconds": 20, "preference": "local_first"}
        job, error, _ = run(client, body, 120)
        safety = next((st for st in (job or {}).get("stages", []) if st["id"] == "safety"), {})
        stopped = bool(job) and job["status"] == "failed" and safety.get("status") == "failed"
        cost = ((job or {}).get("receipt") or {}).get("cost_usd", 0.0)
        category = (error or "").split("(", 1)[-1].split(")", 1)[0] if "(" in (error or "") else ""
        print(f"refuse: {label} -> {'refused' if stopped else 'NOT refused'}")
        lines.append(f"| {label} | {'Yes' if stopped else '**No**'} | ${cost:.3f} | {category} |")
    return lines


def compare(client: httpx.Client, args: argparse.Namespace) -> list[str]:
    """Each idea under each routing preference, interleaved so that no preference gets a quieter machine."""
    chosen = [(p, n) for p, n in PREFERENCES if not args.preferences or p in args.preferences.split(",")]
    results: dict[str, list[dict]] = {p: [] for p, _ in chosen}
    lines = ["## Routing preferences compared", "",
             f"The same {args.ideas} ideas, each made three times as a 20-second trailer with the cache off, once under each "
             "routing preference. *Local busy* is the share of the production during which at least one local machine was "
             "working; *peak* is the most tasks running at once.", "",
             "| Idea | Preference | Result | Time | Cloud cost | Tasks run locally | Local busy | Peak | Frame score | Narration match |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for idea in IDEAS[args.skip: args.skip + args.ideas]:
        for pref, name in chosen:
            job, error, seconds = run(client, film(idea, pref))
            print(f"{name}: {idea[2][:50]}… -> {job and job['status']} in {seconds:.0f}s {error or ''}")
            if not job or job["status"] != "done":
                lines.append(f"| {idea[2][:60]}… | {name} | failed: {(error or '')[:60]} | {seconds:.0f} s | | | | | | |")
                continue
            m = measures(client, job, seconds)
            results[pref].append(m)
            lines.append(f"| {idea[2][:60]}… | {name} | *{m['title']}* | {seconds:.0f} s | ${m['cost']:.3f} "
                         f"| {m['local']} of {m['tasks']} | {pct(m['local_busy'])} | {m['peak']} | {score(m['frame'])} "
                         f"| {pct(m['narration'])} |")
    lines += ["", "**Averages**", "",
              "| Preference | Films | Time | Cloud cost | Tasks run locally | Local busy | Frame score | Narration match |",
              "|---|---|---|---|---|---|---|---|"]
    for pref, name in chosen:
        ms = results[pref]
        if not ms:
            lines.append(f"| {name} | 0 | | | | | | |")
            continue

        def avg(key: str, ms: list[dict] = ms) -> float | None:
            values = [m[key] for m in ms if m[key] is not None]
            return statistics.mean(values) if values else None

        lines.append(f"| {name} | {len(ms)} | {avg('seconds'):.0f} s | ${avg('cost'):.3f} "
                     f"| {pct(sum(m['local'] for m in ms) / max(1, sum(m['tasks'] for m in ms)))} | {pct(avg('local_busy'))} "
                     f"| {score(avg('frame'))} | {pct(avg('narration'))} |")
    return lines


def failover(client: httpx.Client, args: argparse.Namespace) -> list[str]:
    """Make a film, take a local machine offline once it has done some of the work, and check what happened."""
    workers = {w["id"]: w for w in client.get("/api/fabric/workers").json()["workers"]}

    def has_alternative(w: dict) -> bool:
        """Every kind of task this worker offers can also go to another real worker (not the procedural fallback)."""
        others = [o for o in workers.values() if o["id"] != w["id"] and o["enabled"] and o["status"] == "up"
                  and not (o.get("source") == "builtin" and not o.get("is_cloud"))]
        return all(any(k == offer["kind"] for o in others for k in [x["kind"] for x in o["offers"]]) for offer in w["offers"])

    candidates = {w["id"] for w in workers.values() if not w.get("is_cloud") and w["enabled"] and w["status"] == "up"
                  and w.get("source") != "builtin" and has_alternative(w)}
    if args.worker:
        candidates &= {args.worker}
    if not candidates:
        raise SystemExit("no local worker can be taken offline without leaving some task with nowhere to go")
    began = time.time()
    job, error = start(client, film(IDEAS[0]))
    if not job:
        raise SystemExit(f"could not start the film: {error}")
    target, offline_at = None, None
    try:
        while time.time() - began < 900:
            job = client.get(f"/api/jobs/{job['id']}").json()
            if job["status"] in ("done", "failed"):
                break
            if target is None:
                done = [d for d in decisions(client, job["id"]) if d["status"] == "ok" and not d["cached"]]
                used = {d["worker_id"]: sum(1 for x in done if x["worker_id"] == d["worker_id"]) for d in done}
                ready = [w for w in candidates if used.get(w)]
                if len(done) >= 3 and ready:
                    target = max(ready, key=lambda w: used[w])
                    client.post(f"/api/fabric/workers/{target}/disable").raise_for_status()
                    offline_at = time.time()
                    print(f"took {workers[target]['label']} offline after {len(done)} tasks")
            time.sleep(2)
    finally:
        if target:
            client.post(f"/api/fabric/workers/{target}/enable")
    seconds = time.time() - began
    rows = decisions(client, job["id"])
    after = [d for d in rows if offline_at and d["ts"] >= offline_at]
    moved = [d for d in after if d["status"] == "ok" and (d["fallbacks"] or any(
        m.group(1) == target for m in SKIPPED.finditer(d["reason"] or "")))]
    went = sorted({d["worker_label"] for d in moved})
    failed = [d for d in rows if d["status"] != "ok" and "content_policy" not in (d["error"] or "")]
    receipt = (job.get("receipt") or {}).get("cost_usd", 0.0)
    charged = sum(d["cost_usd"] for d in rows if d["status"] == "ok")
    m = measures(client, job, seconds)
    label = workers[target]["label"] if target else "nothing"
    return ["## Taking a machine offline mid-film", "",
            "A 20-second trailer is started with local-first routing. Once a local machine has done some of its work, the "
            "tool takes it offline through the studio, as the *Live fabric* panel's switch does, and brings it back when the "
            "film is finished.", "",
            "| Measure | Result |", "|---|---|",
            f"| Machine taken offline | {label}, after {round(offline_at - began) if offline_at else '—'} s |",
            f"| Tasks routed after that | {len(after)} |",
            f"| Tasks that could have used it and went elsewhere | {len(moved)}, to {', '.join(went) or '—'} |",
            f"| Tasks lost | {len(failed)} |",
            f"| Film | {'finished' if job['status'] == 'done' else 'failed'}: *{m['title']}* in {seconds:.0f} s, "
            f"watermark {'verified' if m['watermark'] else 'not verified'} |",
            f"| Cloud cost on the receipt vs. sum of completed tasks | ${receipt:.4f} vs. ${charged:.4f} |"]


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["films", "compare", "failover"])
    parser.add_argument("--studio", default="http://127.0.0.1:8000")
    parser.add_argument("--ideas", type=int, default=None, help="how many ideas to use (films: 4, compare: 3)")
    parser.add_argument("--refusals", help="films: file of 'label | request' lines the content check must refuse")
    parser.add_argument("--worker", help="failover: the worker id to take offline (default: chosen automatically)")
    parser.add_argument("--preferences", help="compare: comma-separated preferences to run (default: all three)")
    parser.add_argument("--skip", type=int, default=0, help="compare: start from this idea (default: the first)")
    parser.add_argument("--out", help="write the report here instead of printing it")
    args = parser.parse_args()
    args.ideas = args.ideas or (3 if args.mode == "compare" else len(IDEAS))
    client = httpx.Client(base_url=args.studio, timeout=60)
    report = "\n".join({"films": films, "compare": compare, "failover": failover}[args.mode](client, args)) + "\n"
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8", newline="\n")
        print(f"wrote {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    cli()
