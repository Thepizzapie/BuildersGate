import { useEffect, useMemo, useRef, useState } from "react";
import { type ConsoleState } from "./api";

/* THE HANDOVER. What a seat carries back when its work closes.
 *
 * THE NOTE IS THE ITEM'S RESULT AND NOTHING ELSE. Not a summary of it, not a
 * cheerful "done", not the title standing in for a result that was never
 * written: the string the agent left on the work item, verbatim, truncated by
 * the server at 600 characters and by the card in CSS. The floor's one claim is
 * that what it draws is what the board said, and a note is the only thing on
 * this pane with room to say something untrue at length.
 *
 * A CLOSE IS AN EVENT, AND THE POLL ONLY EVER SENDS STATE. /api/console/state
 * says an item IS done; it does not say it JUST became done, and there is no
 * "finished_at" to lean on. So this is the one place on the floor with a memory
 * of the previous poll: an item is handed over when THIS session watched it
 * cross from open into closed. Two consequences, both deliberate:
 *
 *   · the first poll of a session hands nothing over. Every item in it is
 *     already in whatever state it is in, and a page reload is not eight agents
 *     finishing at once.
 *   · an item that scrolls into the payload already closed hands nothing over
 *     either. The board's window moves as work is filed, and an old row
 *     appearing is not news about that seat.
 *
 * Server clocks are not consulted for this. `updated_at` is written by whatever
 * machine holds the db and compared against a browser clock it has no
 * relationship with, and a two minute skew would either fire every note at once
 * on load or never fire one at all.
 */

export type Handoff = {
  seat: string;
  itemId: number;
  title: string;
  /** How it closed: done, approved, rejected, cancelled. The word is the
   *  server's, because "finished" and "rejected" are not the same handover and
   *  flattening them would be the pane telling a kind lie. */
  status: string;
  /** The work item's own result text. Empty when the agent wrote none, and
   *  drawn as empty rather than filled in from somewhere else. */
  result: string;
  /** When this session saw it close, for the hold timer. Browser clock, used
   *  only against itself. */
  at: number;
};

/** Statuses that end a work item. `review` is NOT one: an item in review is
 *  being read by the qa agent and its seat has not finished with it. */
const CLOSED = new Set(["done", "approved", "rejected", "cancelled"]);

/** How long a note stays on the Director's desk. Long enough to read a line of
 *  it at a glance, short enough that a busy morning does not bury the office
 *  under an hour of paper. The board is where the full history lives. */
const HOLD_MS = 14000;

export type Handoffs = {
  /** Every note currently on the desk, newest last. */
  notes: Handoff[];
  /** The note a seat's character is carrying. One per seat: a person has two
   *  hands and one position, so when a seat closes two items inside one hold
   *  the newest is the one being walked over and both are on the desk. */
  bySeat: Map<string, Handoff>;
};

const NONE: Handoffs = { notes: [], bySeat: new Map() };

export function useHandoffs(state: ConsoleState, hold = HOLD_MS): Handoffs {
  /* null until the first poll has been recorded, which is what makes "we have
     never seen this item" distinguishable from "we saw it open". */
  const seen = useRef<Map<number, string> | null>(null);
  const [notes, setNotes] = useState<Handoff[]>([]);

  useEffect(() => {
    const items = state.items || [];
    const before = seen.current;
    seen.current = new Map(items.map((i) => [i.id, i.status]));
    if (!before) return;

    const fresh: Handoff[] = [];
    for (const it of items) {
      if (!CLOSED.has(it.status)) continue;
      const was = before.get(it.id);
      // Unseen, or already closed when we last looked: not a close we watched.
      if (was === undefined || CLOSED.has(was)) continue;
      if (!it.seat) continue;
      fresh.push({
        seat: it.seat, itemId: it.id, title: it.title || `item ${it.id}`,
        status: it.status, result: (it.result || "").trim(), at: Date.now(),
      });
    }
    if (!fresh.length) return;
    setNotes((old) => {
      const ids = new Set(fresh.map((f) => f.itemId));
      return [...old.filter((n) => !ids.has(n.itemId)), ...fresh];
    });
  }, [state]);

  /* One timer for the whole desk, armed on the OLDEST note. A timer per note
     would be a handful of timers churning on a busy board for no gain, and the
     sweep drops everything expired anyway. */
  useEffect(() => {
    if (!notes.length) return;
    const oldest = Math.min(...notes.map((n) => n.at));
    const t = window.setTimeout(
      () => setNotes((old) => old.filter((n) => Date.now() - n.at < hold)),
      Math.max(250, oldest + hold - Date.now()));
    return () => window.clearTimeout(t);
  }, [notes, hold]);

  return useMemo(() => {
    if (!notes.length) return NONE;
    const bySeat = new Map<string, Handoff>();
    // Later wins: `notes` is append-ordered, so this leaves the newest close.
    for (const n of notes) bySeat.set(n.seat, n);
    return { notes, bySeat };
  }, [notes]);
}
