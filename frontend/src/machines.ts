import type { Worker } from "./types";

export type Part = { w: Worker; name: string; role: "gpu" | "model" | "service"; does: string };
export type Machine = { host: string; parts: Part[]; summary: string; up: boolean; enabled: boolean; inflight: number; cloud: boolean };

export const MODEL_NAMES: Record<string, string> = {
  "muse-spark-1.3-contributor": "Muse Spark 1.3 (contributor)",
  "muse-spark-1.3": "Muse Spark 1.3",
  "muse-wonder-1.0": "Muse Wonder 1.0",
  "muse-image-1.0": "Muse Image 1.0",
  "sam-3.1": "SAM 3.1",
  "muse-voice-transcribe-1.0": "Muse Voice Transcribe 1.0",
};

export const JOB: Record<string, string> = {
  "text.generate": "writing",
  "text.moderate": "content check",
  "image.generate": "pictures",
  "image.segment": "cutouts",
  "image.depth": "depth",
  "audio.tts": "voices",
  "audio.music": "music",
  "audio.watermark": "watermark",
  "audio.transcribe": "listening",
};

const GPU = /nvidia|geforce|radeon|rtx|gtx|cuda|\bgpu\b/i;
const ORDER: Part["role"][] = ["gpu", "model", "service"];

function gigabytes(device: string) {
  return Number(device.match(/(\d+(?:\.\d+)?)\s*GB/)?.[1] ?? 0);
}

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** Workers grouped by the computer they run on: its graphics cards, the models it serves, or a cloud service. */
export function machines(workers: Worker[]): Machine[] {
  const groups = new Map<string, Worker[]>();
  for (const w of workers) {
    const host = w.is_cloud ? "Meta Model API" : w.label.split(" · ")[0];
    groups.set(host, [...(groups.get(host) ?? []), w]);
  }
  return [...groups].map(([host, ws]) => {
    const parts = ws.map((w): Part => {
      const role = w.is_cloud || w.source === "builtin" ? "service" : GPU.test(w.device) ? "gpu" : "model";
      const raw = w.is_cloud ? "Muse" : w.label.split(" · ").slice(1).join(" · ") || w.device;
      const name = role === "gpu" ? raw.replace(/^(NVIDIA|AMD|Intel)\s+(GeForce\s+|Radeon\s+)?/i, "") : raw;
      const does = [...new Set(w.offers.map((o) => JOB[o.kind] ?? o.kind.split(".")[1]))].join(", ");
      return { w, name, role, does };
    }).sort((a, b) => ORDER.indexOf(a.role) - ORDER.indexOf(b.role));
    const gpus = parts.filter((p) => p.role === "gpu");
    const models = parts.filter((p) => p.role === "model");
    const box = models.find((p) => !/\(.*\)$/.test(p.w.device) && p.w.device !== host)?.w.device;
    const memory = gpus.reduce((n, p) => n + gigabytes(p.w.device), 0);
    const summary = ws.every((w) => w.is_cloud)
      ? plural(ws.flatMap((w) => w.offers).length, "model")
      : ws.every((w) => w.source === "builtin")
        ? "built-in fallback"
        : [
            box,
            gpus.length ? `${plural(gpus.length, "graphics card")}${memory ? ` (${memory} GB)` : ""}` : "",
            models.length ? plural(models.length, "model") : "",
          ]
            .filter(Boolean)
            .join(" · ");
    return {
      host,
      parts,
      summary,
      up: ws.some((w) => w.status === "up" && w.enabled),
      enabled: ws.some((w) => w.enabled),
      inflight: ws.reduce((n, w) => n + w.inflight, 0),
      cloud: ws.every((w) => w.is_cloud),
    };
  });
}
