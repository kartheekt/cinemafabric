"""End-to-end smoke test in mock mode: start the fabric and studio, make a trailer, check the outputs.

    uv run python scripts/smoke.py

Used by scripts/check.sh. Needs ffmpeg on PATH. Uses a throwaway data directory and two free local ports.
"""

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx

from cinemafabric.studio.media import provenance


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FABRIC_PORT, STUDIO_PORT = free_port(), free_port()
FABRIC, STUDIO = f"http://127.0.0.1:{FABRIC_PORT}", f"http://127.0.0.1:{STUDIO_PORT}"


def wait_up(url: str, seconds: float = 60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"{url} did not come up")


def main() -> int:
    data = tempfile.mkdtemp(prefix="cinemafabric-smoke-")
    env = {**os.environ, "CF_MODE": "mock", "CF_DATA_DIR": data, "CF_FABRIC_URL": FABRIC}
    procs = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "cinemafabric.fabric.app:app", "--port", str(FABRIC_PORT)], env=env),
        subprocess.Popen([sys.executable, "-m", "uvicorn", "cinemafabric.studio.app:app", "--port", str(STUDIO_PORT)], env=env),
    ]
    try:
        wait_up(f"{FABRIC}/health")
        wait_up(f"{STUDIO}/api/options")
        job = httpx.post(f"{STUDIO}/api/jobs", json={
            "prompt": "A night-shift nurse finds a patient who isn't in any record",
            "format": "trailer", "length": "fixed", "seconds": 20, "aspect": "9:16", "subtitle_languages": ["hi", "te"],
            "voice_language": "hi",
        }, timeout=30).json()
        deadline = time.time() + 300
        while time.time() < deadline:
            job = httpx.get(f"{STUDIO}/api/jobs/{job['id']}", timeout=10).json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(1)
        print("status:", job["status"], job.get("error") or "")
        for stage in job["stages"]:
            print(f"  {stage['status']:8} {stage['label']}: {stage['detail']}")
        if job["status"] != "done":
            return 1
        film = Path(data) / "jobs" / job["id"] / "film.mp4"
        assert film.exists() and film.stat().st_size > 10_000, "film.mp4 missing or empty"
        assert job["assets"].get("zip") and job["assets"].get("poster"), "downloads missing"
        assert job["receipt"]["tasks"] > 10, "routing log looks empty"
        out = Path(data) / "jobs" / job["id"]
        with zipfile.ZipFile(next(out.glob("*.zip"))) as z:
            manifest = json.loads(z.read("manifest.json"))
            assert "passport.png" in z.namelist(), "no production passport in the download"
        assert manifest["digital_source_type"] == provenance.SOURCE_TYPE and manifest["models"], "manifest incomplete"
        assert provenance.is_labelled((out / "poster.png").read_bytes()), "poster has no AI label metadata"
        assert provenance.LABEL.encode() in film.read_bytes()[-4096:] + film.read_bytes()[:65536], "film has no AI label"
        assert job["checks"], "content checks not recorded"
        assert job.get("voice_text"), "the Hindi voice-over wasn't translated"
        langs = {t["lang"] for t in job["assets"].get("subtitle_tracks", [])}
        assert {"en", "hi", "te"} <= langs, f"subtitle tracks missing: {langs}"
        planned = httpx.post(f"{STUDIO}/api/jobs/{job['id']}/director", json={"note": "Make the ending quieter."},
                             timeout=120).json()
        assert planned["director"]["pending"]["edits"], "director planned nothing"
        httpx.post(f"{STUDIO}/api/jobs/{job['id']}/director/apply", timeout=30).raise_for_status()
        deadline = time.time() + 300
        while time.time() < deadline:
            job = httpx.get(f"{STUDIO}/api/jobs/{job['id']}", timeout=10).json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(1)
        assert job["status"] == "done" and job["director"]["turns"], f"director note not applied: {job.get('error')}"
        crew = {t["agent"].split(" (")[0] for t in job.get("agents", [])}
        assert {"Script doctor", "Editor", "Test screening", "Release writer"} <= crew, f"agent crew missing: {crew}"
        packed = httpx.get(f"{STUDIO}{job['assets']['zip']}", timeout=60, follow_redirects=True).content
        assert {"release.md", "thumbnail.jpg"} <= set(zipfile.ZipFile(io.BytesIO(packed)).namelist()), "no release copy"
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(packed)).read("manifest.json"))
        assert "Release writer" in {t["agent"] for t in manifest["agents"]}, "manifest misses the agent crew"
        if job["cast"]:
            saved = httpx.post(f"{STUDIO}/api/jobs/{job['id']}/cast/0/save", timeout=30).json()
            second = httpx.post(f"{STUDIO}/api/jobs", json={
                "prompt": "The same character returns for one more night on the water", "format": "trailer",
                "length": "fixed", "seconds": 20, "cast_ids": [saved["id"]], "critic": False, "commercial_safe": True,
            }, timeout=30).json()
            deadline = time.time() + 300
            while time.time() < deadline:
                second = httpx.get(f"{STUDIO}/api/jobs/{second['id']}", timeout=10).json()
                if second["status"] in ("done", "failed"):
                    break
                time.sleep(1)
            assert second["status"] == "done", f"second film failed: {second.get('error')}"
            names = [c["name"] for c in second["story"]["characters"]]
            assert saved["name"] in names, f"returning character missing: {names}"
            assert any(c.get("returning") for c in second["cast"]) or not second["cast"], "sheet was redrawn"
            routes = httpx.get(f"{STUDIO}/api/jobs/{second['id']}/decisions", timeout=10).json()["decisions"]
            non_commercial = [d for d in routes if d["status"] == "ok" and d.get("licence") == "cc-by-nc-4.0"]
            assert not non_commercial, "commercial-safe film used a non-commercial model"
            sequel = httpx.post(f"{STUDIO}/api/jobs/{job['id']}/next", json={"idea": ""}, timeout=30).json()
            deadline = time.time() + 300
            while time.time() < deadline:
                sequel = httpx.get(f"{STUDIO}/api/jobs/{sequel['id']}", timeout=10).json()
                if sequel["status"] in ("done", "failed"):
                    break
                time.sleep(1)
            assert sequel["status"] == "done" and sequel["previously"]["episode"] == 2, f"next episode failed: {sequel.get('error')}"
            assert {c["name"] for c in job["cast"]} <= {c["name"] for c in sequel["cast"] if c.get("returning")}, "cast was recast"
            ending = httpx.post(f"{STUDIO}/api/jobs/{job['id']}/ending", json={"idea": "she stays ashore"}, timeout=120)
            assert ending.status_code == 200, ending.text
            ending = ending.json()
            deadline = time.time() + 300
            while time.time() < deadline:
                ending = httpx.get(f"{STUDIO}/api/jobs/{ending['id']}", timeout=10).json()
                if ending["status"] in ("done", "failed") and not ending["director"].get("pending"):
                    break
                time.sleep(1)
            assert ending["status"] == "done" and ending["variant_of"]["job"] == job["id"], f"ending failed: {ending.get('error')}"
            assert ending["receipt"]["tasks"] < job["receipt"]["tasks"], "the alternate ending re-made the whole film"
            inherited = [t for t in ending.get("agents", []) if t["at"] < ending["created"]]
            assert not inherited and not ending.get("release"), "the alternate ending kept the original's records"
            variant = httpx.get(f"{STUDIO}{ending['assets']['zip']}", timeout=60, follow_redirects=True).content
            assert "release.md" not in zipfile.ZipFile(io.BytesIO(variant)).namelist(), "the alternate ending shipped stale release copy"
            httpx.post(f"{STUDIO}/api/jobs/{job['id']}/commentary", timeout=30).raise_for_status()
            deadline = time.time() + 300
            while time.time() < deadline:
                job = httpx.get(f"{STUDIO}/api/jobs/{job['id']}", timeout=10).json()
                if job["status"] == "done" and (job["assets"].get("commentary") or job.get("error")):
                    break
                time.sleep(1)
            assert job["assets"].get("commentary") and job["commentary"], f"no commentary: {job.get('error')}"
            packed = httpx.get(f"{STUDIO}{job['assets']['zip']}", timeout=60, follow_redirects=True).content
            assert "commentary.mp4" in zipfile.ZipFile(io.BytesIO(packed)).namelist(), "the ZIP has no commentary version"
        print(f"ok: {film.stat().st_size // 1024} KB film, {job['receipt']['tasks']} routed tasks, labelled, "
              f"{len(job['director']['turns'][0]['edits'])} director edits applied")
        return 0
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
