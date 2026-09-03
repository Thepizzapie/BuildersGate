import { useEffect, useRef, useState } from "react";

/* Is the deck-view containing this element the one on screen?
 *
 * The shell shows one `.deck-view.active` at a time and every classic view
 * checks that class before doing any work — `refreshOverview` opened with
 * exactly that guard. A React island that polls regardless would put every
 * converted view's traffic on the wire at once, which is a regression the
 * moment there are two of them. MutationObserver rather than a poll, because
 * the class flips from a click, not from time. */
export function useViewActive(ref: React.RefObject<HTMLElement | null>): boolean {
  const [active, setActive] = useState(true);
  useEffect(() => {
    const view = ref.current?.closest(".deck-view") as HTMLElement | null;
    if (!view) return; // mounted outside the deck — always live
    const read = () => setActive(view.classList.contains("active"));
    read();
    const obs = new MutationObserver(read);
    obs.observe(view, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, [ref]);
  return active;
}

/* ── the event bus ──────────────────────────────────────────────────────────
 *
 * ONE CONNECTION, NOT FORTY TIMERS. Every panel used to ask the server every
 * 1-5 seconds whether anything had changed. The server records every
 * consequential transition in its event table and streams it at
 * /api/events/stream (SSE), so the page holds one EventSource and each panel
 * refetches when a kind it cares about arrives. The classic decks share the
 * same channel through window.BGEvents (frontend/public/events.js); the two
 * do not share a socket, but two per tab is the ceiling rather than one per
 * panel.
 *
 * RESUME IS THE BROWSER'S. The server stamps `id:` with the row id, so a drop
 * and reconnect carries Last-Event-ID and replays what was missed. What this
 * module adds is the listener registry — EventSource dispatches a named event
 * only to a listener registered for that name, so the server's `hello` frame
 * lists the vocabulary and any kind first seen mid-stream is announced with a
 * `vocabulary` frame before it is sent. */

export type BusEvent = {
  id: number; kind: string; ref: string; actor: string;
  payload: Record<string, unknown>; created_at: string;
};

type Listener = (e: BusEvent) => void;
type StateListener = (up: boolean) => void;

const listeners = new Set<Listener>();
const stateListeners = new Set<StateListener>();
const registered = new Set<string>();
let source: EventSource | null = null;
let up = false;
let retryMs = 1000;
let retryTimer = 0;

const STREAM_URL = "/api/events/stream";

function dispatch(raw: MessageEvent) {
  let ev: BusEvent | null = null;
  try { ev = JSON.parse(String(raw.data)); } catch { ev = null; }
  if (!ev || !ev.kind) return;
  listeners.forEach((fn) => { try { fn(ev!); } catch { /* one bad panel */ } });
}

function listenFor(es: EventSource, kinds: string[]) {
  for (const k of kinds) {
    if (!k || registered.has(k) || k === "hello" || k === "vocabulary") continue;
    registered.add(k);
    es.addEventListener(k, dispatch as EventListener);
  }
}

function setUp(next: boolean) {
  if (up === next) return;
  up = next;
  stateListeners.forEach((fn) => { try { fn(next); } catch { /* ignore */ } });
}

function connect() {
  if (source || typeof EventSource === "undefined") return;
  const es = new EventSource(STREAM_URL);
  source = es;
  registered.clear();
  es.addEventListener("hello", (e) => {
    try {
      const d = JSON.parse(String((e as MessageEvent).data));
      listenFor(es, d.kinds || []);
    } catch { /* a hello we cannot read still means we are connected */ }
    retryMs = 1000;
    setUp(true);
  });
  es.addEventListener("vocabulary", (e) => {
    try { listenFor(es, JSON.parse(String((e as MessageEvent).data)).kinds || []); }
    catch { /* ignore */ }
  });
  /* EventSource reconnects on its own for a dropped socket, but a server that
     answers with an error (restart mid-deploy, a 503 while the project is
     being switched) closes it for good. Rebuild with backoff either way — the
     browser's own retry keeps Last-Event-ID, ours starts from now, and the
     `up` flip below is what tells every panel to refetch what it missed. */
  es.onerror = () => {
    setUp(false);
    if (es.readyState === EventSource.CLOSED) {
      source = null;
      window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 30000);
    }
  };
}

/** Does `kind` fall under `pattern`? Exact, or a `prefix.*` family, or `*`. */
export function kindMatches(pattern: string, kind: string): boolean {
  if (pattern === "*" || pattern === kind) return true;
  return pattern.endsWith(".*") && kind.startsWith(pattern.slice(0, -1));
}

/** Subscribe to the bus outside React — the same registry the hook uses. */
export function onEvents(kinds: string[], fn: Listener): () => void {
  connect();
  const wrapped: Listener = (e) => {
    if (kinds.some((k) => kindMatches(k, e.kind))) fn(e);
  };
  listeners.add(wrapped);
  return () => { listeners.delete(wrapped); };
}

export function busUp(): boolean { return up; }

export type EventsOptions = {
  /** Kinds that mean "your data may have changed". `*` for any. Default `*`. */
  kinds?: string[];
  /** Run at all. Off means no fetch, no timer, no subscription. */
  enabled?: boolean;
  /** A parameter of the read; a change refetches NOW (see usePoll's history). */
  key?: unknown;
  /** The safety net: a timer for data no event kind describes, or that a
   *  missed event would leave stale. 30s is the house default; 0 disables. */
  fallbackMs?: number;
};

export const FALLBACK_MS = 30000;

/* When the bus is down — no EventSource in this host, the server restarting,
   a first connect that has not landed — a panel with no timer would sit on
   stale data with nothing admitting it. So a disconnected bus polls at this
   cadence regardless of `fallbackMs`, and a reconnect refetches once. */
const DEGRADED_MS = 5000;

/** Run `fn` now, whenever a relevant event arrives, and on a slow fallback.
 *
 *  The successor to usePoll. `fn` lives in a ref, so a component that closes
 *  over fresh state does not re-subscribe on every render; `key` re-arms the
 *  immediate fetch the way it re-armed the poll — pick a different lore
 *  entity and the read happens now, not on the next tick.
 *
 *  Arrivals are COALESCED: a chain landing writes six rows in one transaction
 *  and the panel wants one refetch, not six. A short trailing debounce is the
 *  difference. */
export function useEvents(fn: () => void, opts: EventsOptions = {}): void {
  const { kinds = ["*"], enabled = true, key, fallbackMs = FALLBACK_MS } = opts;
  const saved = useRef(fn);
  saved.current = fn;
  const [connected, setConnected] = useState(up);
  const kindsKey = kinds.join("|");

  useEffect(() => {
    if (!enabled) return;
    connect();
    const onState = (next: boolean) => {
      setConnected(next);
      if (next) saved.current();   // catch up on what a dead socket missed
    };
    stateListeners.add(onState);
    return () => { stateListeners.delete(onState); };
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    saved.current();
    let debounce = 0;
    const off = onEvents(kindsKey.split("|"), () => {
      window.clearTimeout(debounce);
      debounce = window.setTimeout(() => saved.current(), 150);
    });
    const every = connected ? fallbackMs : Math.min(fallbackMs || DEGRADED_MS, DEGRADED_MS);
    const timer = every > 0 ? window.setInterval(() => saved.current(), every) : 0;
    return () => {
      off();
      window.clearTimeout(debounce);
      if (timer) window.clearInterval(timer);
    };
  }, [enabled, key, kindsKey, fallbackMs, connected]);
}

/* The kinds most panels mean by "the board moved". */
export const WORK_KINDS = ["item.*", "chain.*", "agent.*", "dispatch.*", "budget.*"];
