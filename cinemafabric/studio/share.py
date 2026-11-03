"""Public watch and embed pages for films their owner chose to share."""

from html import escape

from .media import provenance

REPORT_LABELS = {"real_person": "Shows a real person", "copyright": "Copies someone else's work",
                 "harmful": "Harmful or offensive", "other": "Something else"}

STYLE = """
.report { margin-top: 28px; font-size: 14px; }
.report summary { cursor: pointer; color: #a7a39a; width: fit-content; }
.report form { display: grid; gap: 10px; max-width: 420px; margin-top: 10px; }
.report label { display: grid; gap: 4px; }
.report select, .report textarea { background: #151821; color: inherit; border: 1px solid #2a2f3a; border-radius: 8px; padding: 8px; font: inherit; }
.report button { width: fit-content; background: #e2b456; color: #0b0d12; border: 0; border-radius: 8px; padding: 8px 14px; font-weight: 600; cursor: pointer; min-height: 36px; }
:root { color-scheme: dark; }
body { margin: 0; background: #0b0d12; color: #e9e6df; font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font: 600 2rem/1.2 Georgia, serif; letter-spacing: .02em; margin: 16px 0 4px; }
p { color: #b8bcc8; margin: 4px 0; }
video { width: 100%; border-radius: 12px; background: #000; }
.badge { display: inline-block; margin-top: 12px; padding: 2px 10px; border: 1px solid #3a3f4d; border-radius: 999px;
         font-size: 12px; color: #b8bcc8; }
.row { display: flex; gap: 20px; align-items: flex-start; margin-top: 16px; flex-wrap: wrap; }
.row img { width: 180px; border-radius: 10px; border: 1px solid #262a35; }
a { color: #f5b84a; }
"""


def _tracks(state: dict) -> str:
    return "".join(
        f'<track kind="subtitles" src="{escape(t["vtt"])}" srclang="{escape(t["lang"])}" label="{escape(t["label"])}">'
        for t in state["assets"].get("subtitle_tracks", []) if t["lang"] != "en"
    )


def _still(state: dict) -> str:
    return escape(next((s["image"] for s in state.get("shots", []) if s.get("image")), ""))


def _ratio(state: dict) -> str:
    return (state.get("options") or {}).get("aspect", "16:9").replace(":", " / ")


def watch_page(state: dict, base_url: str) -> str:
    story = state.get("story") or {}
    title = escape(story.get("title") or "A CinemaFabric film")
    logline = escape(story.get("logline") or "")
    tagline = escape(story.get("tagline") or "")
    video = escape(state["assets"]["video"])
    poster = escape(state["assets"].get("poster") or "")
    absolute = base_url.rstrip("/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="robots" content="noindex">
<meta name="description" content="{logline}">
<meta property="og:type" content="video.other"><meta property="og:title" content="{title}">
<meta property="og:description" content="{logline}">
{f'<meta property="og:image" content="{absolute}{poster}">' if poster else ""}
<meta property="og:video" content="{absolute}{video}"><meta name="twitter:card" content="summary_large_image">
<style>{STYLE}</style></head>
<body><main>
<video src="{video}" poster="{_still(state)}" style="aspect-ratio: {_ratio(state)}; max-height: 80vh" controls playsinline preload="metadata">{_tracks(state)}</video>
<h1>{title}</h1>
{f"<p><em>{tagline}</em></p>" if tagline else ""}
{f'<img src="{escape(state["assets"]["passport"])}" alt="{title}: production passport" style="width:100%;margin-top:16px;border-radius:12px">' if state["assets"].get("passport") else ""}
<div class="row">
{f'<img src="{poster}" alt="{title} poster">' if poster else ""}
<div><p>{logline}</p>
<span class="badge">{provenance.LABEL} · {state["runtime"].get("label", "")}</span>
<p style="margin-top:12px;font-size:13px">Written, drawn, voiced and scored by AI models on
CinemaFabric. The soundtrack carries an AudioSeal watermark{", and the file carries signed Content Credentials (C2PA)" if (state.get("credentials") or {}).get("verified") else ""}.</p></div>
</div>
<details class="report"><summary>Report this film</summary>
<form id="report" data-id="{escape(state["share"]["id"])}">
<label>What's wrong? <select name="reason">{"".join(f'<option value="{k}">{escape(v)}</option>' for k, v in REPORT_LABELS.items())}</select></label>
<label>Anything else (optional) <textarea name="note" maxlength="500" rows="3"></textarea></label>
<button type="submit">Send report</button> <span role="status"></span>
</form></details>
</main>
<script>
document.getElementById("report").addEventListener("submit", async (e) => {{
  e.preventDefault();
  const f = e.target, status = f.querySelector("[role=status]");
  const r = await fetch("/api/report/" + f.dataset.id, {{ method: "POST", headers: {{ "content-type": "application/json" }},
    body: JSON.stringify({{ reason: f.reason.value, note: f.note.value }}) }}).catch(() => null);
  status.textContent = r && r.ok ? "Thanks. The film's owner will see your report." : "Couldn't send that; try again later.";
  if (r && r.ok) f.querySelector("button").disabled = true;
}});
</script></body></html>"""


def embed_page(state: dict) -> str:
    title = escape((state.get("story") or {}).get("title") or "Film")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><meta name="robots" content="noindex">
<style>html,body{{margin:0;height:100%;background:#000}}video{{width:100%;height:100%;object-fit:contain}}
.label{{position:absolute;right:8px;bottom:44px;font:11px system-ui;color:#ddd;background:#0009;padding:1px 6px;border-radius:4px}}</style>
</head><body><video src="{escape(state["assets"]["video"])}" poster="{_still(state)}" controls playsinline preload="metadata">{_tracks(state)}</video><span class="label">{provenance.LABEL}</span></body></html>"""
