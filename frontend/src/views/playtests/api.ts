/* views/playtests/api.ts — the wire shapes of the Playtests deck, and the pure
 * reading-out-loud helpers the review and the notepad share.
 *
 * Nothing here touches the DOM. The three translations that used to live in
 * ptreview.js (heard, clock, quiet) are exported from one place so the triage
 * row, the transcript chip and the notepad cannot drift apart on what a whisper
 * log-probability or a session clock means. */

/* ── /api/playtest/status ─────────────────────────────────────────────── */
export type Recording = {
  id: number; name: string; telemetry_events: number; native: boolean;
  /** The session clock's zero — what mm:ss a note is about to land on. */
  started_epoch: number; level?: number | null;
};
export type Processing = { id: number; stage: string; error: string; worker: string };
export type Status = { recording: Recording | null; processing: Processing[] };

/* ── /api/state → sessions ───────────────────────────────────────────── */
export type SessionRow = {
  id: number; name: string; status: string; duration_s?: number;
  processing_stage?: string; processing_worker?: string; audio_path?: string;
};

/* ── /api/playtest/preflight and /api/doctor ─────────────────────────── */
export type Check = {
  ok?: boolean; available?: boolean; required?: boolean; reason?: string;
  costs?: string; size_mb?: number; installable?: boolean;
};
export type Preflight = { ready?: boolean; checks?: Record<string, Check> };

/* ── /api/play/status ────────────────────────────────────────────────── */
export type PlayStatus = {
  built?: boolean; stale?: boolean; reason?: string; newest_source?: string;
  blocked?: string;
};

/* ── /api/playtest/<id>/notes ────────────────────────────────────────── */
export type Note = {
  id?: number; text: string; kind?: string; seat?: string; clock?: string;
  frame_rel?: string; mine?: boolean; author?: string; frame_error?: string;
};

/* ── /api/playtest/<id> — playtest.brief() ───────────────────────────── */
export type TeleEvent = { kind: string; data?: Record<string, unknown> };
export type Classification = {
  kind?: string; seat?: string; confidence?: number; scores?: Record<string, number>;
};
export type Item = {
  id: number; t: number; kind: string; text: string; seat?: string; status: string;
  merged_into_id?: number | null; source?: string; author?: string;
  classification?: Classification; transcript_confidence?: number | null;
  assets?: { logical_name: string }[]; events?: TeleEvent[]; frame_rel?: string;
  director_recommendation?: string;
  work?: { id: number; seat: string; status: string; result?: string } | null;
};
export type Segment = {
  t_start: number; t_end: number; text: string; source?: string; confidence?: number | null;
};
export type Setting = {
  t: number; group?: string; prop?: string; key?: string; from: unknown; to: unknown;
  count?: number;
};
export type Moment = { t: number; kind: string };
export type Review = {
  session: {
    id: number; name: string; status: string; duration_s?: number; build_ref?: string;
    video_path?: string; video_offset_s?: number;
  };
  video_offset_s?: number;
  counts: { items: number; events: number };
  telemetry_backed?: boolean;
  telemetry?: {
    by_kind: Record<string, number>; settings: Setting[]; moments: Moment[];
    fps?: { min: number; max: number; avg: number } | null; total: number;
  };
  iteration?: {
    id: number; source_commit?: string; dirty_fingerprint?: string;
    source_fingerprint?: string; export_hash?: string; active_artifact_ids: unknown[];
    tests?: { status?: string }; telemetry_schema_version?: number;
  } | null;
  has_video?: boolean;
  timeline_markers?: { t: number; kind: string; text: string }[];
  items: Item[];
  transcript?: Segment[];
  coverage_warnings?: { message: string }[];
  asset_options?: { logical_name: string; artifact_id: number }[];
};

/* ── vocabularies ─────────────────────────────────────────────────────── */
export const SEATS = ["unassigned", "director", "narrative", "gameplay", "tech",
                      "art", "audio", "cinematic", "qa"];
export const KINDS = ["like", "fix", "add", "change", "question", "note"];

/* ── reading the numbers out loud ─────────────────────────────────────── */

/* whisper avg_logprob bands. <= 0 always; the numbers are the adapter's, the
   words are ours. */
const HEARD_FAIR = -0.5;
const HEARD_POOR = -1.0;

export type Heard = { word: string; tone: "" | "warn"; tip: string };

/** Whisper's avg_logprob, in words. Returns null when the line was heard
 *  cleanly — a warning that fires on every row is wallpaper. "confidence
 *  -1.17" was a log probability wearing the word confidence: the stored value
 *  is untouched and this translates at the render boundary. */
export function heard(value: unknown): Heard | null {
  if (value == null) return null;
  const v = Number(value);
  if (!Number.isFinite(v) || v > HEARD_FAIR) return null;
  const tip = `whisper scored this ${v.toFixed(2)} - it is a log probability, `
            + `always 0 or below. Above -0.5 is a clean hearing, -1.0 and `
            + `below is poor. The words may not be the words that were said.`;
  return v <= HEARD_POOR
    ? { word: "probably misheard", tone: "warn", tip }
    : { word: "may be misheard", tone: "", tip };
}

/** Seconds from session start as m:ss. */
export const clock = (t: unknown): string => {
  const s = Math.max(0, Number(t) || 0);
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
};

/** The notepad's mm:ss.ff label — mirrors playtest._clock so the pad shows the
 *  same label the backend will store. Empty without a session: there is no
 *  origin and nothing honest to show. */
export function sessionClock(ts: number, startedEpoch?: number | null): string {
  if (!startedEpoch) return "";
  const t = Math.max(0, ts - startedEpoch);
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:`
       + `${(t % 60).toFixed(2).padStart(5, "0")}`;
}

/* An unclassified, unrouted, telemetry-free line shorter than this is a mic
   check or a grunt. Six words is roughly the shortest actionable sentence
   anyone says while playing ("the jump feels bad here"). */
const MIN_WORDS = 6;

export const words = (s: unknown) => String(s || "").trim().split(/\s+/).filter(Boolean).length;
export const realEvents = (item: Item) => (item.events || []).filter((e) => e.kind !== "fps");
export const scored = (item: Item) =>
  Object.keys((item.classification || {}).scores || {}).length > 0;

/** Did anything in the pipeline find something here? A conjunction of misses,
 *  not a score, so it cannot quietly demote a line that any one stage
 *  recognised. */
export function quiet(item: Item): boolean {
  if (item.source === "typed" || item.source === "chat") return false;
  if (scored(item)) return false;
  if (item.seat && item.seat !== "unassigned") return false;
  if (realEvents(item).length) return false;
  const h = Number(item.transcript_confidence);
  if (Number.isFinite(h) && h <= HEARD_POOR) return true;
  return words(item.text) < MIN_WORDS;
}

export const praise = (item: Item) =>
  item.kind === "like" || item.director_recommendation === "keep";

export type Groups = {
  open: Item[]; quiet: Item[]; praise: Item[]; filed: Item[]; binned: Item[];
};

/** Partition by DECISION, not by kind — the classifier's kind is empty for
 *  half of any spoken session, and sorting on it is the flat list again with
 *  extra furniture. What the reader needs split is open versus settled. */
export function partition(items: Item[]): Groups {
  const g: Groups = { open: [], quiet: [], praise: [], filed: [], binned: [] };
  for (const item of items) {
    if (item.merged_into_id) { g.binned.push(item); continue; }
    if (item.status === "dismissed") { g.binned.push(item); continue; }
    if (item.status === "promoted") { g.filed.push(item); continue; }
    if (praise(item)) { g.praise.push(item); continue; }
    if (quiet(item)) { g.quiet.push(item); continue; }
    g.open.push(item);
  }
  return g;
}
