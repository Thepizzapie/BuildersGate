import { useCallback, useMemo, useRef, useState } from "react";
import { readJSON } from "../../bridge";
import { useEvents, FALLBACK_MS } from "../../hooks";

/* seats/api.ts — the reads every seat workspace shares.
 *
 * ONE HOOK PER ENDPOINT, NOT ONE STORE. The eight workspaces have almost
 * nothing in common on the wire: narrative reads the lore graph, art reads the
 * asset workspace and the lock table, qa reads work history and the probe
 * contract. A single god-fetch would put all of it on the wire for whichever
 * seat happened to be open, which is exactly what the classic seat files did
 * (nine modules, each polling everything it might need at 3s).
 *
 * Everything here is polled ONLY while the seats deck is on screen and only
 * while the seat that needs it is selected — `enabled` threads down from
 * useViewActive plus the picker. A hidden panel costs nothing.
 */

/** A seat as /api/state reports it. The mission text and the write globs are
 *  the seat's OWN brief out of bgate_core/seats.py, not a copy in the UI —
 *  a project may customise them and the header must say what this project's
 *  seat actually is. */
export type Seat = {
  role: string;
  title: string;
  mission: string;
  write_globs?: string[];
  enabled?: boolean;
  locks?: { path?: string; work_item_id?: number }[];
  last_activity?: { summary?: string; kind?: string; at?: string } | null;
  promoted_feedback?: number;
};

export type QueueItem = {
  id: number; seat: string; title: string; status: string;
  priority?: number; result?: string; total_cost_usd?: number;
  created_at?: string; updated_at?: string;
};

/** GET + poll one endpoint. `path` may be null while a selection is missing —
 *  a null path polls nothing rather than fetching "/api/lore/undefined". */
export function useJSON<T extends Record<string, unknown>>(
  path: string | null,
  fallback: T,
  ms: number,
  enabled: boolean,
): T & { __error?: string } {
  const [data, setData] = useState<T & { __error?: string }>(fallback);
  /* THE STALE-RESPONSE GUARD. When `path` changes, the old path's in-flight
     response can land AFTER the new path's immediate fetch and overwrite it —
     entity A's data under entity B's header until the next tick, which on the
     slow panels is up to a minute. Same bug class the poll-key fix below
     closed once already; this closes the other half. A sequence number
     beats an AbortController here because the loser must also not WRITE. */
  const seq = useRef(0);
  const refresh = useCallback(async () => {
    if (!path) return;
    const mine = ++seq.current;
    const got = await readJSON<T>(path, fallback);
    if (mine === seq.current) setData(got);
    // fallback is a fresh literal on every render and would restart the timer
    // on every tick if it were a dependency; it is only ever read, never kept.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);
  /* `path` as the poll key: a changed URL fetches NOW rather than on the next
     tick. See usePoll — this was the bug that made every picker on every seat
     show the previously-selected thing for up to a minute. */
  /* `ms` is now the FLOOR of the fallback timer: the read happens when an
     event arrives, and the timer only covers what no event describes. */
  useEvents(refresh, { enabled: enabled && !!path, key: path,
                       fallbackMs: Math.max(ms, FALLBACK_MS) });
  /* `__refresh` RIDES ON THE RESULT, the same way `__error` does.
   *
   * Every panel that writes needs the read to happen NOW rather than on the
   * next tick — install a cut, bind a hook, settle a decision, and the card sat
   * stale for up to 15 seconds looking like the click missed. Four seats had
   * each invented a workaround: a local nonce, an optimistic copy keyed on
   * `_version`, a hand-rolled loader. Returning it as a field rather than
   * changing the signature keeps every existing caller compiling — the hook is
   * used in dozens of places and a tuple return would have been a rename
   * across all of them for one new capability. */
  return useMemo(
    () => Object.assign(data, { __refresh: refresh }),
    [data, refresh],
  ) as T & { __error?: string; __refresh?: () => void };
}

/* THE PAGE'S readJSON UNWRAPS THE ENVELOPE. `{ok:true, data:…}` comes back as
 * the data itself, so an endpoint whose data is a LIST hands this hook an
 * array where the generic signature promises an object. Spreading that into a
 * typed record silently produces `{0:…, 1:…}`, which renders as nothing and
 * looks exactly like an empty project. This coerces both shapes instead — the
 * enveloped list and the `{rows: […]}` body — in one place, so no panel has to
 * remember which of its endpoints is wrapped. */
export function useList<T>(path: string | null, ms: number, enabled: boolean):
  { rows: T[]; error?: string } {
  const raw = useJSON<Record<string, unknown>>(path, {}, ms, enabled);
  const rows = Array.isArray(raw)
    ? (raw as T[])
    : ((raw.data as T[] | undefined) || []);
  return { rows, error: raw.__error };
}

/** The seat table. /api/state is the shell's own poll and the most expensive
 *  body on the page, so this asks for it slowly — the mission and the globs
 *  change when somebody edits seat config, not between frames. */
export function useSeats(enabled: boolean): { seats: Seat[]; error?: string } {
  const s = useJSON<{ seats: Seat[] }>("/api/state", { seats: [] }, 15000, enabled);
  return { seats: s.seats || [], error: s.__error };
}

/** The lock table, shared by art and audio because they share the discipline:
 *  a .png and a .wav both fail to merge, and both seats hold leases on files
 *  the other one can then not touch. */
export type Locks = {
  held: { path?: string; seat?: string; work_item_id?: number; since?: string }[];
  waiters: { path?: string; seat?: string }[];
  path_leases: { path?: string; seat?: string; work_item_id?: number }[];
};

export function useLocks(enabled: boolean): Locks {
  /* /api/locks IS enveloped and the page unwraps it, so the fields arrive at
     the top level — not under `data`. */
  const d = useJSON<Partial<Locks>>("/api/locks",
    { held: [], waiters: [], path_leases: [] }, 6000, enabled);
  return { held: d.held || [], waiters: d.waiters || [], path_leases: d.path_leases || [] };
}

/** SQLite stamps are `datetime('now')` — UTC with no zone marker. Parsing one
 *  as local time is how "3 minutes ago" became "5 hours ago" in the classic
 *  seat core; the Z is not optional. */
export function ago(when?: string | null): string {
  if (!when) return "";
  const ms = Date.parse(String(when).replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(String(when)) ? "" : "Z"));
  if (!Number.isFinite(ms)) return "";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

export const usd = (n?: number | null) =>
  typeof n === "number" && n > 0 ? `$${n.toFixed(2)}` : "";
