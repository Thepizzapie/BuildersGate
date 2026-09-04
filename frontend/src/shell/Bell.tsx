import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { mutate, readJSON, toast, watchAgent } from "../bridge";
import { useEvents } from "../hooks";
import { SEAT_COLOR } from "./nav";
import { normalizeQuestion, useQuestions, type Question } from "../notify-store";
import { openAttention } from "./screen";
import { claimUtility, onUtility } from "./utility";

/* Bell.tsx — the header bell, and the drawer behind it.
 *
 * The complaint this answers: agents finish work and nothing tells you. The
 * event log (bgate_core.events, GET /api/events) records every consequential
 * transition; this is the surface that reads it. One badge for "how much has
 * happened that you have not seen", one panel that says WHAT happened and which
 * item it happened to, and one card for a director question that is waiting on
 * a sentence from you.
 *
 * BE HONEST ABOUT WHAT THIS CHANNEL IS. The bell only tells you things while
 * the dashboard is open in front of you — a closed tab is not notified. The
 * drawer's footer says so rather than letting a badge imply a delivery
 * guarantee it does not have; the channels that survive a closed tab are the
 * desktop window title and notify.webhook.
 *
 * IT OWNS NO TIMER OF ITS OWN. The event bus (hooks.ts) is what wakes it: any
 * event means the log moved, so /api/events is re-read then, plus the slow
 * fallback useEvents keeps for a dead socket. The old notify.js had a watchdog
 * for exactly this case; the bus is that watchdog now.
 *
 * THE BADGE IS NOT COUNTED FROM THE POLL. /api/events?since=<seq> walks forward
 * every read; the unread count comes from the server's stored `ui` cursor,
 * which moves only when a human dismisses it. Deriving the badge from the batch
 * that just arrived is how a bell reads zero because the poller ate the events.
 *
 * THE DRAWER IS A CHILD OF <body>, NOT OF THE HEADER. The header carries a
 * backdrop-filter in the orbit ground, which makes it a backdrop root: a
 * descendant's own frost could only sample the 42px of header behind it and
 * rendered as a clear pane, and the header's stacking context let the page's
 * panels paint straight over it. A portal to body frosts the actual page and
 * needs no z-index contest with the stage. */

const CACHE_MAX = 300;     // rows kept in memory; the log itself is the record
const SHOW_MAX = 60;       // rows painted, newest first
const TAIL_LIMIT = 100;    // a cold drawer wants the newest, not the oldest
const FALLBACK_MS = 45000; // nobody driving — one slow catch-up, not a poll

/* The event vocabulary, as a label and a tone. Tones map onto the theme's
   semantic colours (--good/--warn/--bad/--info) and nothing else. An unknown
   kind renders with its raw name and the muted tone rather than vanishing:
   this UI must not be the reason a newly-emitted kind is invisible. */
const KINDS: Record<string, { label: string; tone: string }> = {
  "item.done": { label: "done", tone: "ok" },
  "item.review": { label: "review", tone: "warn" },
  "item.failed": { label: "failed", tone: "bad" },
  "item.approved": { label: "approved", tone: "ok" },
  "item.rejected": { label: "rejected", tone: "warn" },
  "item.aging": { label: "aging", tone: "warn" },
  "chain.filed": { label: "chain", tone: "info" },
  "chain.advanced": { label: "handoff", tone: "info" },
  "chain.stalled": { label: "stalled", tone: "bad" },
  "gate.mode": { label: "gate", tone: "info" },
  "director.question": { label: "question", tone: "warn" },
  "agent.spawned": { label: "spawned", tone: "mute" },
  "agent.exited": { label: "exited", tone: "mute" },
};
const meta = (kind: string) => KINDS[kind] || { label: String(kind || "event"), tone: "mute" };

type Payload = Record<string, unknown>;
type Ev = { id: number; kind: string; ref?: string; created_at?: string; payload?: Payload };

type EventsRead = {
  events: Ev[]; seq?: number; head?: number; read_seq?: number;
  unread?: number; unread_total?: number; unread_by_kind?: Record<string, number>;
  notify_kinds?: string[]; vocabulary?: string[]; in_app?: boolean;
  gap?: boolean; older?: boolean; tail?: boolean; more?: boolean;
};

const num = (v: unknown) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
const trunc = (s: unknown, n: number) => {
  const t = String(s || ""); return t.length > n ? t.slice(0, n - 1) + "…" : t;
};
// A seat name lands inside a colour: whitelisted, never interpolated raw —
// the value is written by an agent.
const seatColor = (s: string) => SEAT_COLOR[s] || SEAT_COLOR.tech;

/* SQLite writes `datetime('now')`, which is UTC with no offset in it. A bare
   Date.parse of "2026-07-29 20:15:00" is read as LOCAL time by some engines,
   which is how a two-minute-old event renders as "in 5h". */
const stampMs = (when: unknown) => {
  const text = String(when || "");
  if (!text) return 0;
  const ms = Date.parse(text.replace(" ", "T")
    + (/[zZ]|[+-]\d\d:?\d\d$/.test(text) ? "" : "Z"));
  return Number.isFinite(ms) ? ms : 0;
};
const ago = (when: unknown) => {
  const ms = stampMs(when);
  if (!ms) return "";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 45) return "just now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};

/* ---- reading a payload ----------------------------------------------------
 * Every branch tolerates a payload that is not the shape it expects. These
 * dicts are written by five different producers, one of them replaces an
 * oversized payload with a truncation marker, and a drawer that throws on a
 * surprising key is a drawer that goes blank exactly when something unusual
 * happened. */
function itemOf(e: Ev): number {
  const p = e.payload || {};
  const head = (p.head || {}) as Payload;
  const id = num(p.item) || num(head.item) || num(p.from) || num(p.item_id);
  if (id) return id;
  // `ref` is an item id for the item.* kinds and a chain id for the chain
  // ones; only a numeric ref is a link worth offering.
  return num(e.ref) || 0;
}
function seatOf(e: Ev): string {
  const p = e.payload || {};
  const head = (p.head || {}) as Payload;
  return String(p.seat || head.seat || p.from_seat || "");
}
function lineOf(e: Ev): ReactNode {
  const p = e.payload || {};
  const kind = String(e.kind || "");
  if (p._truncated) return <i>a payload too large to store - {num(p.chars)} chars</i>;
  if (kind === "chain.advanced") {
    return <>{trunc(p.from_title || "a link", 44)} landed - <b>#{num(p.to)} {String(p.to_seat || "next")}</b> is ready
      {num(p.waiting) > 1 ? ` · ${num(p.waiting) - 1} more behind it` : ""}</>;
  }
  if (kind === "chain.stalled") {
    // TWO producers write this kind. heartbeat.py sends {chain_id, head:{…}};
    // steerbox's stale-question reminder sends {question_seq, question} and no
    // head at all, and reading it with the chain branch renders "chain has
    // not moved for 0m — #0 is stuck", which is a reminder about nothing.
    if (p.question_seq) return <>still waiting on your answer - {trunc(p.question || "a director question", 100)}</>;
    const head = (p.head || {}) as Payload;
    return <>chain <b>{trunc(p.chain_id || "", 24)}</b> has not moved for {num(p.idle_min)}m
      {" "}- #{num(head.item)} {String(head.seat || "")} is {String(head.status || "stuck")}
      {p.reason ? ` · ${trunc(p.reason, 90)}` : ""}</>;
  }
  if (kind === "item.aging") {
    return <><b>{trunc(p.title || "an item", 44)}</b> has been waiting {num(p.idle_min)}m for your approval</>;
  }
  if (kind === "gate.mode") {
    return <>sign-off is now <b>{String(p.mode || "?")}</b>
      {p.previous ? ` (was ${String(p.previous)})` : ""}
      {p.env_override ? ` · ${trunc(p.env_override, 70)}` : ""}</>;
  }
  if (kind === "director.question") return trunc(p.question || "the director asked you something", 120);
  if (p.title) {
    return <><b>{trunc(p.title, 48)}</b>{p.chain_id ? ` · link ${num(p.chain_pos)}` : ""}
      {p.result ? ` - ${trunc(p.result, 90)}` : ""}</>;
  }
  if (p.question) return trunc(p.question, 120);
  if (p.reason) return trunc(p.reason, 120);
  const ref = String(e.ref || "");
  return ref ? `ref ${trunc(ref, 60)}` : "—";
}

// 16px bell, stroke-only so it inherits the button's colour in both grounds.
const BellGlyph = () => (
  <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor"
       strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M4 6.5a4 4 0 0 1 8 0c0 2.4.6 3.6 1.2 4.2.3.3.1.8-.3.8H3.1c-.4 0-.6-.5-.3-.8C3.4 10.1 4 8.9 4 6.5Z" />
    <path d="M6.4 13.4a1.8 1.8 0 0 0 3.2 0" />
  </svg>
);

type Log = {
  events: Ev[]; seq: number; head: number; readSeq: number;
  unread: number; unreadTotal: number; unreadByKind: Record<string, number>;
  notifyKinds: string[]; vocabulary: string[]; inApp: boolean;
  gap: boolean; older: boolean; cold: boolean; err: string;
};
const EMPTY_LOG: Log = {
  events: [], seq: 0, head: 0, readSeq: 0, unread: 0, unreadTotal: 0, unreadByKind: {},
  notifyKinds: [], vocabulary: [], inApp: true, gap: false, older: false, cold: true, err: "",
};

/** Fold one /api/events payload into the log. Every key here is one the
 *  endpoint documents; nothing is inferred from the batch. */
function absorb(log: Log, d: EventsRead): Log {
  const batch = Array.isArray(d.events) ? d.events : [];
  const newest = batch.slice().reverse();       // server sends oldest first
  let events = log.events;
  let older = log.older;
  let cold = log.cold;
  if (d.tail) {
    events = newest; older = !!d.older; cold = false;
  } else if (newest.length) {
    const seen = new Set(newest.map((e) => num(e.id)));
    events = newest.concat(log.events.filter((e) => !seen.has(num(e.id))));
  }
  if (events.length > CACHE_MAX) events = events.slice(0, CACHE_MAX);
  // seq is the poll position and moves forward only. head/read_seq/unread
  // are the server's, and the badge is the server's number — never a count
  // of what happens to be in `events`.
  return {
    events, older, cold, err: "",
    seq: Math.max(log.seq, num(d.seq)),
    head: num(d.head), readSeq: num(d.read_seq),
    unread: num(d.unread), unreadTotal: num(d.unread_total),
    unreadByKind: (d.unread_by_kind && typeof d.unread_by_kind === "object") ? d.unread_by_kind : {},
    notifyKinds: Array.isArray(d.notify_kinds) ? d.notify_kinds : [],
    vocabulary: Array.isArray(d.vocabulary) ? d.vocabulary : [],
    inApp: d.in_app !== false,
    gap: !!d.gap,
  };
}

export function Bell() {
  const host = useRef<HTMLDivElement>(null);
  const drawer = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [log, setLog] = useState<Log>(EMPTY_LOG);
  const logRef = useRef(log);
  logRef.current = log;
  // A draft answer lives here, not in the DOM: the drawer repaints whenever
  // the log moves and a textarea's value used to go with it.
  const fetching = useRef(false);
  const coldRef = useRef(true);
  const driven = useQuestions();

  const refresh = useCallback(async (force = false) => {
    if (fetching.current) return;
    // A hidden tab must not keep reading; the next event or the fallback
    // picks it up when the page comes back.
    if (!force && document.visibilityState === "hidden") return;
    fetching.current = true;
    try {
      for (let drain = 0; drain < 4; drain++) {
        // COLD OPEN ASKS FOR THE TAIL, NOT since=0. `since` omitted means "the
        // newest limit events"; since=0 is the literal cursor read, which on a
        // fortnight of history hands back the two hundred OLDEST rows.
        const url = coldRef.current
          ? `/api/events?limit=${TAIL_LIMIT}`
          : `/api/events?since=${logRef.current.seq}&limit=200`;
        const d = await readJSON<EventsRead>(url, { events: [] });
        if (d.__error) {
          setLog((l) => ({ ...l, err: String(d.__error) }));
          return;
        }
        const next = absorb(logRef.current, d);
        coldRef.current = next.cold;
        logRef.current = next;
        setLog(next);
        /* `more` means the limit truncated a FORWARD read, so there is a newer
           page waiting. Drained here with a hard cap: a bug that leaves `more`
           permanently true must cost three requests, not a spin. Tail reads
           never set it, so this cannot loop on a cold open. */
        if (!(d.more && !d.tail)) break;
      }
    } finally {
      fetching.current = false;
    }
  }, []);
  useEvents(() => { void refresh(false); }, { kinds: ["*"], fallbackMs: FALLBACK_MS });

  const close = useCallback(() => setOpen(false), []);
  const openDrawer = useCallback(() => { claimUtility("notifications"); setOpen(true); void refresh(true); }, [refresh]);
  useEffect(() => onUtility((name) => { if (name !== "notifications") setOpen(false); }), []);

  /* Click outside shuts it — testing BOTH the bell's host and the drawer,
     because the drawer is a portal sibling of the host, not a child. */
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (host.current?.contains(t) || drawer.current?.contains(t)) return;
      close();
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("click", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);

  /* Questions from the event cache are the FALLBACK, and only until somebody
     drives us. The console payload is a live query; the cached event is a
     snapshot, so a question answered in another tab would be resurrected from
     it as an open card with a reply box that 409s. */
  const questions: Question[] = driven.driven
    ? driven.questions
    : log.events
        .filter((e) => e.kind === "director.question")
        .map((e) => normalizeQuestion({ event_seq: e.id, asked_at: e.created_at, ...(e.payload || {}) }))
        .slice(0, 6);
  const openQs = questions.filter((q) => q.seq && !q.answer);

  /* Mark everything read. `seq` is omitted on purpose: the server resolves it
     against the head it can see, so a page that has been open across a dozen
     emits cannot clear a range it never received — and cannot fail to clear
     the ones that landed while the click was in flight either. */
  const markRead = async () => {
    const r = await mutate<Partial<EventsRead>>("/api/events/read", { body: {}, quiet: true });
    if (!r.ok) { toast(`the bell could not be cleared - ${r.error}`); return; }
    const d = r.data || {};
    setLog((l) => ({
      ...l,
      readSeq: num(d.read_seq), unread: num(d.unread), unreadTotal: num(d.unread_total),
      unreadByKind: (d.unread_by_kind && typeof d.unread_by_kind === "object") ? d.unread_by_kind : {},
      head: num(d.head) || l.head,
    }));
  };

  /* An event row is a way back to the work it is about. Both handoffs are
     guarded: the graph may not be on the page, and a dead click is better
     than a thrown one. */
  const openItem = (id: number) => {
    if (!id) return;
    try {
      const g = window.AgentsGraph as { select?(id: string): unknown } | undefined;
      if (g?.select && g.select("task_" + id)) { close(); return; }
    } catch { /* fall through to the log */ }
    watchAgent(id);
    close();
  };

  // notify.in_app off means the bell does not RING. The drawer still opens
  // and still lists everything — the setting turns off the interruption, not
  // the record, and hiding the history as well would look like a data loss.
  const ring = log.inApp ? log.unread : 0;
  const title = log.err ? `the event log is unreachable - ${log.err}`
    : !log.inApp ? "notifications are off (notify.in_app) - the drawer still lists everything"
      : ring ? `${ring} unread${log.unreadTotal > ring ? ` · ${log.unreadTotal} events in all` : ""}`
        : "nothing new";

  return (
    /* `nt-host` stays as the id: streamer.js anchors its chip beside it. */
    <div id="nt-host" className="nt-wrap" ref={host}>
      <button className={`nt-bell${open ? " on" : ""}${log.inApp ? "" : " muted"}`}
              id="nt-bell" type="button" aria-expanded={open}
              aria-label={ring ? `Notifications - ${ring} unread` : "Notifications"}
              title={title} onClick={() => (open ? close() : openDrawer())}>
        <BellGlyph />
        <span className={`nt-badge${ring > 0 ? "" : " hidden"}`} id="nt-badge" aria-live="polite">
          {ring > 99 ? "99+" : String(ring)}
        </span>
      </button>
      {createPortal(
        <div className="nt-drawer" id="nt-drawer" role="dialog" aria-label="Notifications"
             hidden={!open} ref={drawer}>
          {open && (
            <>
              <div className="nt-head">
                <span className="nt-title">Notifications</span>
                {ring > 0 && <span className="nt-count">{ring} unread</span>}
                <span className="nt-acts">
                  <button className="nt-act" type="button" title="Re-read the log"
                          onClick={() => { coldRef.current = true; void refresh(true); }}>refresh</button>
                  <button className="nt-act primary" type="button" disabled={!log.unreadTotal}
                          onClick={markRead}>mark all read</button>
                </span>
              </div>
              <div className="nt-body">
                {/* A question is a card, not a row: it is the one event kind
                    waiting on the human for a sentence rather than for
                    attention. Answered ones are dropped — the answer is on the
                    event and in the handoff thread. */}
                {openQs.length > 0 && <button className="nt-inbox" onClick={() => { close(); openAttention(); }}>
                  <span><b>{openQs.length} question{openQs.length === 1 ? "" : "s"} need an answer</b>
                    <small>Open the Orchestration inbox to review and respond.</small></span>
                  <span>Open inbox</span>
                </button>}
                {log.err && <div className="nt-err">the event log is unreachable - {log.err}</div>}
                {/* A pruned range is reported, never silently skipped: "you
                    missed 40 events" and "nothing happened" must not look the same. */}
                {log.gap && <div className="nt-gap">some older events were pruned before this page read them — the log keeps 14 days</div>}
                {log.older && <div className="nt-note">showing the most recent {TAIL_LIMIT} — there is more history behind this window</div>}
                {!log.events.length
                  ? <div className="nt-empty">nothing has happened yet — this fills up when an agent finishes, a chain hands off, or something needs you</div>
                  : log.events.slice(0, SHOW_MAX).map((e) => {
                    const m = meta(e.kind);
                    const item = itemOf(e);
                    const fresh = num(e.id) > log.readSeq;
                    // A kind outside notify.kinds is still recorded and still
                    // listed; it just did not ring. Marked so the drawer
                    // explains its own badge.
                    const rings = log.notifyKinds.indexOf(e.kind) >= 0;
                    const seat = seatOf(e);
                    return (
                      <button key={e.id} type="button"
                              className={`nt-row${fresh ? " nt-new" : ""}${rings ? "" : " quiet"}`}
                              data-nt={item ? "open" : undefined}
                              title={item ? `Open #${item}` : undefined}
                              onClick={item ? () => openItem(item) : undefined}>
                        <span className="nt-top">
                          <span className={`nt-dot ${m.tone}`} />
                          <span className={`nt-k ${m.tone}`}>{m.label}</span>
                          {item > 0 && (
                            <span className="nt-item" style={{ color: seatColor(seat) }}>
                              #{item}{seat ? ` ${seat}` : ""}
                            </span>
                          )}
                          <span className="nt-when">{ago(e.created_at)}</span>
                        </span>
                        <span className="nt-line">{lineOf(e)}</span>
                      </button>
                    );
                  })}
              </div>
              <div className="nt-foot">
                <span>
                  {log.notifyKinds.length
                    ? `ringing for ${log.notifyKinds.length} of ${log.vocabulary.length || 14} kinds`
                    : "ringing for nothing - notify.kinds is empty"}
                  {log.inApp ? "" : " · muted (notify.in_app)"}
                </span>
                <span className="nt-honest">the bell only reaches you while this page is open — a webhook is the channel that survives a closed tab</span>
              </div>
            </>
          )}
        </div>,
        document.body)}
    </div>
  );
}
