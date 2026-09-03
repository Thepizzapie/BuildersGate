import { useSyncExternalStore } from "react";

/* notify-store.ts — the open ask_human questions, pushed in by whoever polls
 * /api/console/state.
 *
 * The bell (shell/Bell.tsx) belongs to no view, and the console payload is
 * the authoritative list of open questions: a question stays open until it
 * is answered, so an event cursor that has walked past the `director.question`
 * row would show it once and never again. The console already reads that
 * payload on every event; it hands the questions here rather than the bell
 * reading the whole board a second time. Same shape as store.ts.
 *
 * `driven` records that a caller has handed us the list at least once. Until
 * then the bell falls back to the questions it can see in the event cache —
 * and once a driver has spoken, an empty list means there are none. */

export type Question = {
  seq: number; item_id: number; seat: string; question: string;
  asked_at: string; asked_by: string; refs: string[];
  answer: string; answered_at: string;
};

const num = (v: unknown) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };

/** One question shape whatever it came from, with every key present. */
export function normalizeQuestion(q: unknown): Question {
  const src = (q && typeof q === "object" ? q : {}) as Record<string, unknown>;
  return {
    seq: num(src.event_seq || src.seq),
    item_id: num(src.item_id),
    seat: String(src.seat || ""),
    question: String(src.question || src.text || ""),
    asked_at: String(src.asked_at || ""),
    asked_by: String(src.asked_by || ""),
    refs: Array.isArray(src.refs) ? src.refs.map(String) : [],
    answer: String(src.answer || ""),
    answered_at: String(src.answered_at || ""),
  };
}

type Snapshot = { driven: boolean; questions: Question[] };

let snap: Snapshot = { driven: false, questions: [] };
const subs = new Set<() => void>();

/** Feed the bell the console state just read. Ignored when the read failed
 *  or carried no question list, so a bad poll never empties the cards. */
export function push(state: unknown): void {
  const s = state as { __error?: unknown; questions?: unknown } | null;
  if (!s || s.__error || !Array.isArray(s.questions)) return;
  snap = { driven: true, questions: s.questions.map(normalizeQuestion) };
  subs.forEach((fn) => fn());
}

/** Mark one question answered locally, ahead of the next poll. */
export function answered(seq: number, answer: string): void {
  if (!snap.driven) return;
  snap = { ...snap, questions: snap.questions.map((q) => q.seq === seq ? { ...q, answer } : q) };
  subs.forEach((fn) => fn());
}

const subscribe = (fn: () => void) => { subs.add(fn); return () => { subs.delete(fn); }; };

export function useQuestions(): Snapshot {
  return useSyncExternalStore(subscribe, () => snap, () => snap);
}
