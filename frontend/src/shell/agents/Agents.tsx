import { useCallback, useEffect, useRef, useState } from "react";
import {
  Badge, Button, Group, Paper, ScrollArea, SegmentedControl, Stack, Tabs, Text,
} from "@mantine/core";
import { Ti } from "../Ti";
import { SEAT_COLOR, SEAT_ICON } from "../nav";
import { useViewActive, usePoll } from "../../hooks";
import { setSelection } from "../selection";
import { notifyUpdate, mutate, readJSON, toast } from "../../bridge";
import { DirectorChat } from "./DirectorChat";
import { FloorPane } from "./FloorPane";
import { moduleOff } from "../../bridge";
import { Streamer } from "./Streamer";
import {
  EMPTY_CONSOLE, consoleState, type ConsoleState, type Item,
} from "./api";

/* The director's screen: a session on the left, the board on the right.
 *
 * ONE READING, not two. This used to be a console in two states — an idle hero
 * composer that became a live transcript — over a composer with four ways to
 * address a sentence (the director, one running agent, all of them, or a seat)
 * and two modes (dispatch, brainstorm). All of that was ways to file work
 * without the director, around a director that could not be talked to properly.
 *
 * So the left is DirectorChat, which is a Claude Code session and nothing else,
 * and the right is the board that session files work onto.
 */

declare global {
  interface Window {
    /** Prefill the director's box from somewhere else in the app. Registered
     *  by DirectorChat, which owns the box. */
    BGCompose?: (task: { seat?: string; title?: string; brief?: string }) => void;
    /** chatlive.js — viewer chat captured during a stream, as feedback. It
     *  mounts itself into a host element and is otherwise self-contained. */
    ChatLive?: { mount(host: HTMLElement): void; unmount?(): void; refresh?(): void };
    /** The delegation graph (static/agents_graph.js). `mount` takes the canvas
     *  host and the detail rail; `apply` is fed the console state on each poll. */
    AgentsGraph?: {
      mount(host: HTMLElement, detail: HTMLElement | null): unknown;
      apply(state: unknown): void;
      activate?(): void;
      fit?(): void;
      setFilter?(mode: string): string;
    };
  }
}

const POLL_MS = 3000;

export type Pane = "board" | "graph" | "floor";

const CLOSED = new Set(["done", "failed", "cancelled", "approved", "rejected"]);

export function Agents() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [state, setState] = useState<ConsoleState>(EMPTY_CONSOLE);
  const [pane, setPane] = useState<Pane>("board");

  /* A FAILED POLL IS NOT AN EMPTY BOARD, and it used to be drawn as one.
     readJSON never throws: a fetch error or a 500 comes back as the fallback we
     handed it — EMPTY_CONSOLE — tagged with __error. Written straight into
     state, that turned one bad response into "no items, no agents", which the
     floor then drew as every seat idle. So the last payload we actually
     received is KEPT and the error is stamped on it. */
  const refresh = useCallback(async () => {
    const next = await consoleState();
    if (next.__error) {
      setState((was) => ({ ...was, __error: next.__error }));
      return;
    }
    setState(next);
    // The bell has no view of its own and is fed by this poll.
    notifyUpdate(next);
  }, []);
  usePoll(refresh, POLL_MS, active);

  const items = state.items || [];
  const open = items.filter((i) => !CLOSED.has(i.status));

  return (
    <div className="bg4-console live" ref={host}
         style={{ ["--rail" as string]: `${readRail()}px` }}>
      <DirectorChat active={active} onSent={refresh} />
      <RailGrip />
      <Rail state={state} open={open} pane={pane} setPane={setPane}
            onRefresh={refresh} />
    </div>
  );
}

/* ── the board rail ─────────────────────────────────────────────────────── */

function Rail({ state, open, pane, setPane, onRefresh }: {
  state: ConsoleState;
  open: Item[];
  /* THREE READINGS OF ONE QUEUE: a list, a dependency graph, and the floor,
     where a seat is a room and an agent's POSITION is its state. The pane is
     the parent's state so the rest of the screen can read it. */
  pane: Pane;
  setPane: (p: Pane) => void;
  onRefresh: () => void;
}) {
  /* THE TABS BELONG TO THE RAIL, NOT TO THE BOARD PANE. They used to live
     inside BoardPane, so switching to Graph took Asked you, Approve and
     Responses off the screen with it — the graph is a different VIEW OF THE
     QUEUE, not a different screen. */
  const [tab, setTab] = useState<string | null>("queue");

  return (
    <div className="bg4-console-side">
      <Group gap="xs" p="xs" className="bg4-side-head" wrap="nowrap">
        {/* bg4-modes is what colours the ACTIVE label. Without it the selected
            segment inherits Mantine's default, which on this ground is dark
            text on a dark indicator — the option you are ON is the one you
            cannot read. */}
        <SegmentedControl size="xs" value={pane} className="bg4-modes"
                          onChange={(v) => setPane(v as Pane)}
                          data={[
                            { value: "board",
                              label: <span><Ti name="layout-list" size={12} /> Board</span> },
                            { value: "graph",
                              label: <span><Ti name="sitemap" size={12} /> Graph</span> },
                            /* The floor is a MODULE — a project that switched
                               it off gets no third segment. */
                            ...(moduleOff("floor") ? [] : [{ value: "floor",
                              label: <span><Ti name="building" size={12} /> Floor</span> }]),
                          ]} />
        <span style={{ flex: 1 }} />
        <Badge size="sm" variant="default" leftSection={<Ti name="clock" size={11} />}>
          {open.length} queued
        </Badge>
      </Group>
      <BoardPane state={state} open={open} onRefresh={onRefresh}
                 tab={tab} setTab={setTab}
                 /* The graph and the floor draw the QUEUE tab and nothing else:
                    both are pictures of the board's items, so neither has
                    anything to say about Approve or Stream. */
                 queueView={tab !== "queue" ? null
                   : pane === "graph" ? <GraphPane state={state} />
                   : pane === "floor" && !moduleOff("floor") ? <FloorPane state={state} />
                   : null} />
    </div>
  );
}

function BoardPane({ state, open, onRefresh, tab, setTab, queueView }: {
  state: ConsoleState; open: Item[]; onRefresh: () => void;
  tab: string | null; setTab: (v: string | null) => void;
  /** Whatever is drawing the queue instead of the card list - the graph, or
   *  the floor - when the switch is on it and the queue tab is showing. It
   *  replaces the card list and nothing else — same tabs above it, same
   *  auto-deploy note below. Named for the SLOT rather than for the graph,
   *  which is what it was called while the graph was the only thing that could
   *  fill it. */
  queueView: React.ReactNode;
}) {
  const autoDeploy = !!state.autopilot?.on;
  const [busy, setBusy] = useState(false);
  const queued = open.filter((i) => i.status === "queued");

  /* CHAT AND STREAM ARE STREAMER-MODE TABS, and they only appear when it is
     on. Both are about broadcasting -- the live chat relay and the OBS overlay
     pages -- so to anyone not streaming they were two permanent pills that did
     nothing, taking width from the four tabs that are the actual board. That
     width matters here: this strip scrolls rather than wraps, so the tabs
     nobody is using were pushing the ones they are off the visible end.

     Polled rather than read once, because the switch that flips this sits in
     the same rail: the strip has to answer a toggle without a reload. */
  const [streamer, setStreamer] = useState(false);
  const loadStreamer = useCallback(async () => {
    const s = await readJSON<{ on?: boolean }>("/api/streamer", {});
    setStreamer(!!s.on);
  }, []);
  usePoll(loadStreamer, 10000, true);

  /* Turning streamer mode off while sitting on one of its tabs would leave the
     strip with nothing selected and the body blank. Fall back to the queue. */
  useEffect(() => {
    if (!streamer && (tab === "chat" || tab === "stream")) setTab("queue");
  }, [streamer, tab, setTab]);
  /* WHAT HAS REPORTED, newest first. The window /api/console/state returns is
     already the recent board — this used to be scoped by the console's cut
     line, which went with the work-item turns. */
  const responses = (state.items || []).filter((i) => i.result)
    .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));

  /* Deploy what is READY, one at a time. A chain link whose predecessor has not
     landed refuses, and firing twenty at once turns one concurrency refusal
     into twenty toasts — so this is sequential, stops at the first real
     failure, and reports how far it got. */
  async function deployAll() {
    const ready = queued.filter((i) => i.ready !== false);
    const held = queued.length - ready.length;
    if (!ready.length) {
      toast(held ? `${held} item(s) are waiting on earlier links` : "nothing to deploy");
      return;
    }
    setBusy(true);
    let sent = 0;
    for (const item of ready) {
      const r = await mutate(`/api/queue/${item.id}/dispatch`, { quiet: true });
      if (r.ok) { sent += 1; continue; }
      toast(sent ? `deployed ${sent} — then stopped: ${r.error}` : r.error || "refused");
      break;
    }
    setBusy(false);
    if (sent) toast(`deployed ${sent} item(s)`
      + (held ? ` — ${held} still waiting on earlier links` : ""), "ok");
    onRefresh();
  }

  return (
    <>
      {/* nowrap + a scroller: five pills do not fit a 330px rail, and Mantine's
          Tabs.List wraps by default — which is the two ragged rows in the
          screenshot. Scrolling keeps them one row and keeps every tab
          reachable, which hiding one behind a "more" menu would not. */}
      <Group gap="xs" px="xs" pb="xs" wrap="nowrap" className="bg4-side-tabs">
        <Tabs value={tab} onChange={setTab} variant="pills" style={{ flex: 1, minWidth: 0 }}>
          <Tabs.List>
            <Tabs.Tab value="queue">Queue {queued.length || ""}</Tabs.Tab>
            <Tabs.Tab value="asked">Asked you</Tabs.Tab>
            <Tabs.Tab value="approve">Approve {state.gates.length || ""}</Tabs.Tab>
            <Tabs.Tab value="responses">Responses</Tabs.Tab>
            {streamer && <Tabs.Tab value="chat">Chat</Tabs.Tab>}
            {streamer && <Tabs.Tab value="stream">Stream</Tabs.Tab>}
          </Tabs.List>
        </Tabs>
        {tab === "queue" && queued.length > 0 && (
          <Button size="compact-xs" onClick={deployAll} loading={busy}
                  leftSection={<Ti name="send" size={12} />}>
            deploy all
          </Button>
        )}
      </Group>

      {/* The graph and the floor are their own bodies: each fills the rail and
          scrolls itself (the graph pans, the floor grows downward with the seat
          count), so neither may be wrapped in the card scroller below. */}
      {queueView}

      {!queueView && (
      <ScrollArea className="bg4-side-body" type="auto">
        <Stack gap={8} p="xs">
          {tab === "queue" && (open.length
            ? open.map((i) => <QueueCard key={i.id} item={i} items={state.items} />)
            : <Empty>nothing queued</Empty>)}

          {tab === "asked" && (state.questions.length
            ? state.questions.map((q) => (
                <Paper key={q.id} p="xs" withBorder className="bg4-sidecard">
                  <Text size="xs">{q.text}</Text>
                  <Text size="xs" c="dimmed">{q.seat}</Text>
                </Paper>
              ))
            : <Empty>nothing waiting — ask for something above</Empty>)}

          {tab === "approve" && (state.gates.length
            ? state.gates.map((g) => (
                <Paper key={g.id} p="xs" withBorder className="bg4-sidecard">
                  <Text size="xs">{g.title}</Text>
                  <Text size="xs" c="dimmed">{g.seat}</Text>
                </Paper>
              ))
            : <Empty>nothing to approve</Empty>)}

          {/* chatlive.js owns every pixel inside its host and mounts itself by
              id; React renders the host and never its children. */}
          {tab === "chat" && <ChatHost />}
          {tab === "stream" && <Streamer />}

          {tab === "responses" && (responses.length
            ? responses.slice(0, 30).map((i) => (
                <Paper key={i.id} p="xs" withBorder className="bg4-sidecard"
                       onClick={() => setSelection({ key: `i${i.id}`, kind: "item",
                                                     itemId: i.id, title: i.title, seat: i.seat })}>
                  <Text size="xs" fw={500} lineClamp={1}>{i.title}</Text>
                  <Text size="xs" c="dimmed" lineClamp={3}>{i.result}</Text>
                </Paper>
              ))
            : <Empty>no agent has reported yet</Empty>)}
        </Stack>
      </ScrollArea>
      )}

      {/* SAID ONCE, WHERE THE WAITING HAPPENS. Queued work standing still with
          nothing on screen explaining why is this product's most confusing
          state, and the answer is always this switch. */}
      {queued.length > 0 && (
        <Group gap="xs" className="bg4-side-note" wrap="nowrap">
          <Ti name="info-circle" size={13} />
          <Text size="xs" c="dimmed">
            {autoDeploy
              ? "auto-deploy is on — queued work dispatches itself as slots free up"
              : "queued work waits for you to press deploy — auto-deploy is off"}
          </Text>
        </Group>
      )}
    </>
  );
}

function QueueCard({ item, items }: { item: Item; items: Item[] }) {
  const c = SEAT_COLOR[item.seat] || "var(--text-3)";
  const blocker = item.depends_on
    ? items.find((x) => x.id === item.depends_on) : undefined;
  const waiting = !!blocker && !CLOSED.has(blocker.status);
  return (
    <Paper p="xs" withBorder className="bg4-sidecard"
           onClick={() => setSelection({ key: `i${item.id}`, kind: "item",
                                         itemId: item.id, title: item.title, seat: item.seat })}>
      <Group gap={8} wrap="nowrap" align="flex-start">
        <span className="tile" style={{ background: `${c}22`, color: c }}>
          <Ti name={SEAT_ICON[item.seat] || "point"} size={14} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Text size="xs" fw={500} lineClamp={2}>{item.title}</Text>
          <Text size="xs" c="dimmed" ff="var(--mono)" lineClamp={1}>
            {item.chain_pos != null ? `lane ${item.chain_pos} · ` : ""}
            {waiting ? `blocked until #${blocker!.id} closes` : item.seat}
          </Text>
        </div>
        <Badge size="xs" variant="default" color={waiting ? "yellow" : undefined}>
          {waiting ? "waiting" : item.status}
        </Badge>
      </Group>
    </Paper>
  );
}

/* THE DELEGATION GRAPH — the real one, mounted.
 *
 * agents_graph.js is a NodeCanvas graph with drag-to-place, saved positions per
 * project, an active/all filter, spotlighting of what is live, and a detail
 * rail with steer/stop on the selected agent. It was unloaded from index.html
 * on the argument that "lanes-by-seat is the Board screen now" — and the Board
 * screen was deleted afterwards, so the console has had no graph at all since.
 * A React reimplementation was the wrong answer twice over: it threw away a
 * working thousand lines, and it could not do the half of them that matter
 * (positions, steering) without rewriting those too.
 *
 * So this is a HOST. React owns the element and never a child of it — the same
 * contract the brainstorm room and the workflow canvas hold. `apply(state)` is
 * called on every console poll, which is how the graph learns what is running;
 * it diffs internally against `_sig` and rebuilds only when the shape moved.
 */
function GraphPane({ state }: { state: ConsoleState }) {
  const host = useRef<HTMLDivElement>(null);
  const rail = useRef<HTMLDivElement>(null);
  const ready = useRef(false);
  /* Seeded from the module's own stored preference so the switch agrees with
     the canvas on the first paint rather than after the first click. */
  const [mode, setMode] = useState<"active" | "all">(() => {
    try {
      return localStorage.getItem("bgate-graph-filter") === "all" ? "all" : "active";
    } catch { return "active"; }
  });

  /* The freshest state, readable from an effect that must not re-run on every
     poll. The mount effect needs it exactly once, at mount. */
  const latest = useRef(state);
  latest.current = state;

  useEffect(() => {
    const el = host.current, det = rail.current;
    if (!el || !det || !window.AgentsGraph) return;
    window.AgentsGraph.mount(el, det);
    ready.current = true;
    /* PAINT NOW, not on the next tick. apply() only runs from the poll effect
       below, which does not fire on a remount with unchanged state — so coming
       back to the graph left it empty for up to three seconds, which looks
       exactly like the black pane this switch used to leave behind. */
    window.AgentsGraph.apply(latest.current);
    /* fit() needs a laid-out host; on the first mount the pane has just been
       switched to and has no size yet. activate() defers it one frame. */
    window.AgentsGraph.activate?.();
    return () => { ready.current = false; };
  }, []);

  /* The data, every poll. Not in the mount effect: remounting the canvas on
     each 3s tick would throw away pan, zoom and any node the user just moved. */
  useEffect(() => {
    if (ready.current) window.AgentsGraph?.apply(state);
  }, [state]);

  if (!window.AgentsGraph) {
    return (
      <ScrollArea className="bg4-side-body" type="auto">
        <Stack gap={10} p="xs">
          <Empty>agents_graph.js is not loaded on this build</Empty>
        </Stack>
      </ScrollArea>
    );
  }
  return (
    <div className="bg4-graphwrap">
      {/* THE FILTER HAD NO CONTROL. agents_graph.js has always had one — it
          reads its mode from localStorage on mount and exposes setFilter — but
          the React pane never drew a switch, so whatever the classic console
          last wrote was permanent. A graph stuck on "all" is the whole board on
          a canvas; stuck on "active" with no way out, you cannot see what
          finished. */}
      <div className="bg4-graphbar">
        {(["active", "all"] as const).map((m) => (
          <button key={m} className={mode === m ? "on" : ""}
                  title={m === "active"
                    ? "running, queued, gated, and a failure you have not seen yet — plus their ancestors, so a chain keeps its trunk"
                    : "every item the board window returned, capped at 60"}
                  onClick={() => {
                    window.AgentsGraph?.setFilter?.(m);
                    setMode(m);
                  }}>{m}</button>
        ))}
        <span className="sp" />
        <button onClick={() => window.AgentsGraph?.fit?.()} title="fit to view">fit</button>
      </div>
      <div className="cg-canvas" ref={host} />
      <div className="cg-detail" ref={rail} />
    </div>
  );
}

/* ── the adjustable rail ───────────────────────────────────────────────────
 *
 * The board rail was a hardcoded 340px. That is one number for a column that
 * holds a queue, a graph's detail pane and an approval list — and the
 * graph in particular is unreadable in 340px while the queue wastes half of it.
 *
 * Dragged as a CSS variable on the grid, not as React state per frame: the
 * pointermove handler writes --rail straight to the element, so a drag is one
 * style write per event instead of a re-render of the whole console (which
 * holds the transcript and the canvas). The final value is persisted, and the
 * bounds are real — below ~260px the tabs cannot show, above 70% the
 * conversation stops being the main thing on the screen.
 */
const RAIL_KEY = "bg4-console-rail";
const RAIL_MIN = 260, RAIL_MAX_FRAC = 0.7, RAIL_DEFAULT = 340;

function readRail(): number {
  const n = Number(localStorage.getItem(RAIL_KEY));
  return Number.isFinite(n) && n >= RAIL_MIN ? n : RAIL_DEFAULT;
}

/* Finds its own grid rather than taking a ref: the grip is rendered by `Live`,
   which is a child of the element being resized, so passing the ref down would
   thread it through a component that has no other use for it. */
function RailGrip() {
  const me = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; w: number } | null>(null);
  const grid = () => me.current?.closest(".bg4-console") as HTMLElement | null;

  const onDown = (e: React.PointerEvent) => {
    const el = grid();
    if (!el) return;
    e.preventDefault();
    /* Capture so the drag survives the pointer leaving a 6px column — but it
       throws for a pointer the element does not currently have (a released one,
       a synthetic one), and an exception here aborts the handler before the
       drag is armed, which is a grip that silently does nothing. The drag works
       without capture; capture only makes it tolerant of a fast hand. */
    try { (e.target as HTMLElement).setPointerCapture(e.pointerId); }
    catch { /* not capturable — the move handler still fires */ }
    drag.current = { x: e.clientX, w: readRail() };
    el.classList.add("dragging");
  };
  const onMove = (e: React.PointerEvent) => {
    const el = grid(), d = drag.current;
    if (!el || !d) return;
    // Rightwards shrinks the rail: the grip is on its LEFT edge.
    const max = Math.max(RAIL_MIN, el.clientWidth * RAIL_MAX_FRAC);
    const w = Math.min(max, Math.max(RAIL_MIN, d.w - (e.clientX - d.x)));
    el.style.setProperty("--rail", `${Math.round(w)}px`);
  };
  const onUp = () => {
    const el = grid();
    if (!el || !drag.current) return;
    drag.current = null;
    el.classList.remove("dragging");
    const w = parseInt(el.style.getPropertyValue("--rail"), 10);
    if (Number.isFinite(w)) {
      try { localStorage.setItem(RAIL_KEY, String(w)); } catch { /* private mode */ }
    }
    /* The canvas sizes itself from its host, and the host just changed. */
    window.AgentsGraph?.fit?.();
  };

  return (
    <div className="bg4-railgrip" ref={me} role="separator" aria-orientation="vertical"
         title="drag to resize · double-click to reset"
         onPointerDown={onDown} onPointerMove={onMove}
         onPointerUp={onUp} onPointerCancel={onUp}
         onDoubleClick={() => {
           const el = grid();
           if (!el) return;
           el.style.setProperty("--rail", `${RAIL_DEFAULT}px`);
           try { localStorage.setItem(RAIL_KEY, String(RAIL_DEFAULT)); } catch { /**/ }
           window.AgentsGraph?.fit?.();
         }} />
  );
}

/* ── the two helpers the console is built out of ───────────────────────────
 *
 * RESTORED. Both lived between GraphPane and TurnRow and were deleted with the
 * old list-shaped GraphPane when this pane was rewritten. Vite does not
 * typecheck, so the build succeeded and the console threw "Msg is not defined"
 * at render — the whole screen, not one panel, because a throw in a React tree
 * takes the tree. That is the failure mode of building without reading tsc's
 * answer, and it is why the typecheck runs before the build here.
 */

/** A panel with nothing in it, saying so in the rail's own voice. Deliberately
 *  a sentence and not an icon: every one of these names what would fill it. */
function Empty({ children }: { children: React.ReactNode }) {
  return <div className="bg4-empty">{children}</div>;
}

/** The viewer-chat island. chatlive.js mounts itself into a host id and owns
 *  everything inside it — chat captured during a stream, promotable to
 *  feedback. React renders the host and never a child of it, the same contract
 *  the brainstorm room and the workflow canvas hold. */
function ChatHost() {
  /* THE ELEMENT, not its id. See the ChatLive declaration above: the tab was
     rendering an empty box because mount() threw on the string it was given,
     every time, since the day this pane was written. A ref also means the host
     is guaranteed to exist when the effect runs, which an id lookup only
     happens to be. */
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (host.current) window.ChatLive?.mount(host.current);
    return () => window.ChatLive?.unmount?.();
  }, []);
  if (!window.ChatLive) {
    return <Empty>chatlive.js is not loaded on this build</Empty>;
  }
  return <div ref={host} id="bg4-chat-host" className="bg4-chathost" />;
}
