export type Option = { id: string; label: string; seconds?: number };

export type Format = { id: "trailer" | "movie"; label: string; min: number; max: number; default: number; presets: number[] };

export type Catalog = {
  formats: Format[];
  styles: Option[];
  genres: string[];
  aspects: Option[];
  resolutions: Option[];
  frame_rates: Option[];
  preferences: Option[];
  mode: string;
  cloud_text_model?: string;
  languages: Option[];
  voice_languages: Option[];
  max_languages: number;
  exports: Option[];
};

export type JobOptions = {
  prompt: string;
  format: "trailer" | "movie";
  length: "auto" | "fixed";
  seconds: number | null;
  genre: string;
  style: string;
  aspect: string;
  preference: string;
  premium_script: boolean;
  critic: boolean;
  narration: boolean;
  fresh: boolean;
  review_script: boolean;
  max_cost_usd: number | null;
  subtitle_languages: string[];
  cast_ids: string[];
  voice_language: string;
  commercial_safe: boolean;
  agents: boolean;
  resolution: string;
  fps: number;
};

export type Stage = {
  id: string;
  label: string;
  status: "pending" | "running" | "done" | "skipped" | "failed";
  detail: string;
  started: number | null;
  finished: number | null;
};

export type ShotState = {
  index: number;
  visual: string;
  camera: string;
  duration: number;
  narration: string;
  characters: string[];
  title_card: string;
  image: string | null;
  mask: string | null;
  prompt: string | null;
  review: { score: number; matches: boolean; issue: string; regenerated?: boolean } | null;
};

export type Receipt = {
  tasks: number;
  local_tasks: number;
  cloud_tasks: number;
  cache_hits: number;
  failed: number;
  rerouted: number;
  cost_usd: number;
  avoided_usd: number;
  by_worker: { worker_id: string; worker_label: string; tasks: number; cost_usd: number }[];
  by_model: {
    model: string;
    tasks: number;
    cost_usd: number;
    is_cloud?: number;
    cached?: number;
    failed?: number;
    avg_ms?: number;
    kinds?: string[];
    stages?: string[];
  }[];
};

export type AgentTurn = {
  agent: string;
  summary: string;
  changes: string[];
  score: number | null;
  after: number | null;
  by: string;
  cost_usd: number;
  at: number;
};

export type Job = {
  id: string;
  created: number;
  updated: number;
  status: "queued" | "running" | "review" | "done" | "failed";
  error: string | null;
  options: JobOptions & { duration?: string };
  runtime: { seconds: number; label: string; shots: number; decided_by: "story" | "you" | "pending"; reason: string } | null;
  stages: Stage[];
  story: {
    title: string;
    logline: string;
    tagline: string;
    tone: string;
    setting: string;
    characters: { name: string; look: string }[];
  } | null;
  shots: ShotState[];
  cast: { name: string; look: string; image: string; returning?: boolean }[];
  assets: Partial<Record<"poster" | "video" | "script" | "subtitles" | "zip" | "trailer" | "release" | "thumbnail", string>> & {
    video_seconds?: number;
    subtitle_tracks?: { lang: string; label: string; vtt: string; srt: string }[];
    exports?: Record<string, string>;
    subtitles_skipped?: string[];
    passport?: string;
    commentary?: string;
    commentary_vtt?: string;
  };
  share?: { id: string; created: number };
  qa: {
    transcript: string;
    similarity: number;
    first_similarity?: number;
    redone_lines?: { shot: number; before: number; after: number }[];
  } | null;
  previously?: { job: string; title: string; episode: number } | null;
  variant_of?: { job: string; title: string; idea: string } | null;
  originality?: { problems: string[]; rewrite: string };
  agents?: AgentTurn[];
  release?: { youtube_title: string; youtube_description: string; hashtags: string[]; instagram_caption: string; thumbnail_shot: number };
  credentials?: { signed: Record<string, number>; own_certificate: boolean; verified?: boolean; reason?: string };
  reports?: { reason: string; label: string; note: string; at: number }[];
  commentary?: { shot: number; start: number; text: string }[];
  voice_text?: Record<string, string>;
  voice_note?: string;
  checks?: { check: string; by: string; verdict: string; categories: string[]; review?: string; chunks: number; at: number }[];
  watermark?: {
    method: string;
    applied: boolean;
    model?: string;
    payload?: string;
    detection?: number;
    verified?: boolean;
    reason?: string;
  } | null;
  director?: {
    turns: (DirectorPlan & { applied: boolean; cost_usd: number })[];
    pending: DirectorPlan | null;
  };
  receipt: Receipt | null;
};

export type SavedCharacter = { id: string; name: string; look: string; image: string; source_job: string | null };

export type DirectorPlan = {
  note: string;
  reply: string;
  at: number;
  edits: { op: string; shot: number; text: string; seconds: number; summary: string }[];
  estimate_usd: number;
  planned_by: string;
};

export type JobSummary = {
  id: string;
  created: number;
  status: string;
  title: string | null;
  prompt: string;
  runtime_label: string;
  poster: string | null;
  cost_usd: number | null;
};

export type Worker = {
  id: string;
  label: string;
  location: string;
  device: string;
  is_cloud: boolean;
  source: "env" | "added" | "builtin";
  status: "up" | "down" | "disabled" | "unknown";
  detail: string;
  enabled: boolean;
  inflight: number;
  completed: number;
  failures: number;
  last_error: string | null;
  latency_s: Record<string, number>;
  offers: { kind: string; model: string; quality: number; vision: boolean }[];
};

export type Ledger = Receipt & {
  budget_usd: number | null;
  hard_stop_usd: number | null;
  spent_usd: number;
  reserved_usd: number;
  remaining_usd: number | null;
  simulated: boolean;
};

export type Decision = {
  task_id: string;
  ts: number;
  job_id: string | null;
  stage: string | null;
  label: string | null;
  kind: string;
  worker_id: string | null;
  worker_label: string | null;
  model: string | null;
  is_cloud: boolean;
  status: string;
  cost_usd: number;
  cloud_equiv_usd: number;
  latency_ms: number;
  cached: boolean;
  reason: string | null;
  fallbacks: string[];
  error: string | null;
  licence?: string | null;
};

export type Estimate = {
  lines: { item: string; count: number; where: string; usd: number }[];
  total_usd: number;
  min_usd?: number;
  max_usd?: number;
  label: string;
  assumes: string;
};
