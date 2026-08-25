import { useCallback, useState } from "react";
import { readJSON } from "../bridge";
import { usePoll } from "../hooks";

/* The floor's data: what is running, what is queued, what just happened.
 *
 * Three reads, not one — the shell's own poll rather than pollState()'s, because
 * /api/state does not carry the agent table or the activity log and adding them
 * to it would grow the one request every other view already waits on.
 *
 * NOTHING HERE IS SYNTHESISED. The prototype drew a progress bar on every live
 * row; a dispatched agent reports elapsed seconds and cost, and does NOT report
 * a percentage of anything, so the bar is drawn only where a real fraction
 * exists (a queue item's own phase count) and is simply absent otherwise. A
 * made-up 62% is worse than no bar: it is a number someone will plan around.
 */

export type QueueItem = {
  id: number; seat: string; title: string; status: string;
  phase?: number | null; phases?: number | null; note?: string | null;
  /* WHY THIS ROW IS NOT MOVING, from the server rather than derived here.
     `status` alone made a correct dependency insertion look like a skipped
     item: #43 QUEUED, next to a running #45 and a done #42, when the real
     order was #42 -> #45 -> #43 and always had been. `execution_state` is the
     one word a card colours by; `waiting_line` is the sentence, and it names
     the blocker's TITLE because an id alone sends the reader off to look it
     up and the whole defect is that people were not looking things up. */
  execution_state?: string;
  waiting_line?: string;
  waiting_on?: { id: number; seat: string; title: string; status: string };
  depends_on_all?: number[];
  unresolved?: number[];
  exhausted_at?: string | null;
  exhausted_why?: string | null;
};
export type Agent = {
  item_id: number; state: string; seconds?: number; cost_usd?: number;
  steers_pending?: number; runner?: string; last_output_s?: number | null;
};
export type Event = {
  id: number; seat: string; kind: string; summary: string;
  created_at: string; actor?: string; ref?: string;
};

export type Row = {
  key: string;
  kind: "agent" | "event";
  itemId?: number;
  eventId?: number;
  seat: string;
  icon: string;
  time: string;
  text: string;
  meta: string;
  live: boolean;
  warn: boolean;
  pct?: number;
};

export type Floor = {
  rows: Row[];
  items: QueueItem[];
  agents: Agent[];
  events: Event[];
  counts: { live: number; queued: number; history: number };
  error?: string;
};

const EMPTY: Floor = {
  rows: [], items: [], agents: [], events: [],
  counts: { live: 0, queued: 0, history: 0 },
};

/* An event's icon comes from what the event IS. The kinds are the backend's own
   vocabulary (queue, review, console, followup, artifact, playtest…). */
const KIND_ICON: Record<string, string> = {
  queue: "clipboard-list", review: "checks", console: "message-2",
  followup: "arrow-bounce", artifact: "photo-check", playtest: "device-gamepad-2",
  asset: "stack-2", lore: "book-2", spend: "coin", error: "alert-triangle",
  seat: "users", brainstorm: "bulb",
};

/* "Needs you" is a fact about the sentence, because the backend does not stamp
   one. These are the phrases the pipeline actually emits when it stops and
   waits — kept together here so the rule is one list rather than a regex
   scattered over three components. */
const WANTS_YOU = /needs? you|awaiting|blocked|violation|failed|error|rejected|cancelled/i;

const hhmm = (s: string) => (s || "").slice(11, 16) || "--:--";

export const clock = (sec?: number) => {
  if (!sec || sec < 0) return "";
  const m = Math.floor(sec / 60), r = sec % 60;
  return m ? `${m}m${String(r).padStart(2, "0")}s` : `${r}s`;
};

export function useFloor(ms = 4000, enabled = true): Floor {
  const [floor, setFloor] = useState<Floor>(EMPTY);

  const refresh = useCallback(async () => {
    const [q, ag, act] = await Promise.all([
      readJSON<{ items?: QueueItem[] }>("/api/queue", { items: [] }),
      readJSON<{ agents?: Agent[] }>("/api/agents", { agents: [] }),
      readJSON<{ events?: Event[] }>("/api/activity?after_id=0", { events: [] }),
    ]);
    const error = [q, ag, act].map((x) => x.__error).find(Boolean);
    const items = q.items || [];
    const agents = (ag.agents || []).filter((a) => a.state === "running");
    /* ALREADY NEWEST FIRST — /api/activity pages `id DESC`. This used to call
       .reverse() with a comment claiming it produced newest-first, which is
       exactly backwards: it took a descending list and made it ascending, so
       Work history opened on the oldest thing that ever happened and the run
       that just finished was at the far end of a 68-row scroll. */
    const events = act.events || [];
    const byId = new Map(items.map((i) => [i.id, i]));

    const liveRows: Row[] = agents.map((a) => {
      const it = byId.get(a.item_id);
      const pct = it?.phase && it?.phases
        ? Math.round((it.phase / it.phases) * 100) : undefined;
      return {
        key: `a${a.item_id}`, kind: "agent", itemId: a.item_id,
        seat: it?.seat || "", icon: "player-play", time: "now",
        text: it?.title || `item ${a.item_id}`,
        meta: [clock(a.seconds), a.cost_usd ? `$${a.cost_usd.toFixed(3)}` : ""]
          .filter(Boolean).join(" · "),
        live: true, warn: false, pct,
      };
    });

    const eventRows: Row[] = events.map((e) => ({
      key: `e${e.id}`, kind: "event", eventId: e.id,
      itemId: Number(e.ref) || undefined,
      seat: e.seat || "", icon: KIND_ICON[e.kind] || "point",
      time: hhmm(e.created_at), text: e.summary || e.kind,
      meta: e.kind, live: false, warn: WANTS_YOU.test(e.summary || ""),
    }));

    const rows = [...liveRows, ...eventRows];
    setFloor({
      rows, items, agents, events, error,
      counts: {
        live: agents.length,
        queued: items.filter((i) => i.status === "queued").length,
        history: events.length,
      },
    });
  }, []);

  usePoll(refresh, ms, enabled);
  return floor;
}
