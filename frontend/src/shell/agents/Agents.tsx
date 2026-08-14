import { useCallback, useEffect, useRef, useState } from "react";
import {
  Badge, Button, Group, Paper, ScrollArea, SegmentedControl, Spoiler,
  Stack, Tabs, Text,
} from "@mantine/core";
import { Ti } from "../Ti";
import { useStickyBottom } from "../sticky";
import { SEAT_COLOR, SEAT_ICON } from "../nav";
import { useViewActive, usePoll } from "../../hooks";
import { setSelection } from "../selection";
import { notifyUpdate, mutate, readJSON, toast } from "../../bridge";
import { BrainstormFoot } from "./Brainstorm";
import { Composer, type Aim } from "./Composer";
import { Cat } from "./Cat";
import { Streamer } from "./Streamer";
import {
  EMPTY_CONSOLE, bsCreate, bsList, bsOpen, bsSay, clearConsole, consoleState, say,
  steerAll, steerItem,
  type BsSession, type ConsoleState, type Item, type Turn,
} from "./api";

/* The director's console — ONE console in two states.
 *
 * IDLE is the composer as the page: no panes, no board, no toolbar, because
 * there is nothing to look at yet. What surrounds it is only what helps you
 * start — three suggested prompts and the last few things that closed.
 *
 * LIVE is the same console with work in it: the panes split, the board fills
 * with what was just queued, and the composer SLIDES TO THE FOOT of the
 * transcript. It is deliberately the same element (see Composer) — that is what
 * makes this read as one screen changing rather than two screens swapping, and
 * it is why the prompt you typed is still on screen, as the first line of the
 * transcript, when the answer arrives.
 *
 * The suggestions and "last closed" fall away in live, because now there is
 * real work to look at. Closing everything reverses it.
 *
 * WHAT THIS REPLACES: agents_console.js — 2,245 lines that owned the transcript,
 * a pixel cat, brainstorm, five tabs and a delegation graph, rebuilt from
 * innerHTML on a 3s poll behind five hand-written signature caches. Its
 * brainstorm Reset button was appended conditionally and could fail to exist at
 * all, and its cockpit was a fixed-height grid that reserved 560px for a
 * three-line conversation.
 */

declare global {
  interface Window {
    /** Prefill the director's box from somewhere else in the app. */
    BGCompose?: (task: { seat?: string; title?: string; brief?: string }) => void;
    /** chatlive.js — viewer chat captured during a stream, as feedback. It
     *  mounts itself into a host id and is otherwise self-contained. */
    /* mount takes the HOST ELEMENT, not an id — chatlive.js binds its click and
       keydown handlers straight onto what you hand it. The declaration said
       `id: string`, so this file passed a string and the mount threw
       "host.addEventListener is not a function", killing the tab's subtree. */
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

const SUGGESTIONS = [
  { icon: "refresh", text: "redo the enemy set — they're off-model" },
  { icon: "alert-triangle", text: "the hub screen feels dead, give it parallax" },
  { icon: "settings", text: "audit what shipped this week" },
];

const CLOSED = new Set(["done", "failed", "cancelled", "approved", "rejected"]);

/** How long ago, in the two characters the row has room for. Timestamps are the
 *  server's own `YYYY-MM-DD HH:MM:SS` in UTC — parsed as UTC EXPLICITLY, since
 *  letting the browser read them as local time reports every row as hours old
 *  in one direction or in the future in the other. */
function ago(stamp?: string): string {
  if (!stamp) return "";
  const t = Date.parse(stamp.replace(" ", "T") + "Z");
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

export function Agents() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [state, setState] = useState<ConsoleState>(EMPTY_CONSOLE);
  const [mode, setMode] = useState<"dispatch" | "brainstorm">("dispatch");
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [session, setSession] = useState<BsSession | null>(null);
  /* WHO THE BOX IS TALKING TO. null is the director — the old and still the
     usual case. A number or "all" turns the same box into a steer, because
     correcting work that is already running is a different act from filing
     more of it, and the box used to make them look identical. */
  const [aim, setAim] = useState<Aim>(null);
  /* The project's own seat table — what the tag menu offers when nothing is
     running. Read once per activation, not per poll: it changes when somebody
     edits the seat config, which is not a thing that happens mid-session. */
  const [seats, setSeats] = useState<string[]>([]);
  useEffect(() => {
    if (!active) return;
    readJSON<{ seats: { role?: string }[] }>("/api/state", { seats: [] })
      .then((d) => setSeats(
        (d.seats || []).map((row) => row.role || "").filter(Boolean)));
  }, [active]);

  const refresh = useCallback(async () => {
    const next = await consoleState();
    setState(next);
    // The bell has no view of its own and was fed by the classic console's
    // poll. This is that poll now; drop the call and the badge goes stale.
    notifyUpdate(next);
  }, []);
  usePoll(refresh, POLL_MS, active);

  /* Brainstorm reuses the director's open session rather than opening one per
     visit — a room per page load is how you get fifty empty rooms and no way to
     tell which one had the idea in it. */
  const openBrainstorm = useCallback(async () => {
    const list = await bsList();
    const existing = (list.sessions || []).find((s) => s.status === "open");
    const id = existing?.id ?? (await bsCreate("director thread")).data?.id;
    if (id) setSession(await bsOpen(id));
  }, []);
  useEffect(() => { if (mode === "brainstorm" && !session) openBrainstorm(); },
            [mode, session, openBrainstorm]);

  async function send(said = text) {
    const body = said.trim();
    if (!body || sending) return;
    setSending(true);
    setText("");
    if (mode === "brainstorm" && session) {
      const r = await bsSay(session.id, body);
      if (r.ok) setSession(await bsOpen(session.id));
    } else if (aim === "all") {
      /* Reported per item on purpose: a runner that takes its prompt at launch
         has no live channel, and a broadcast that half landed must not toast
         like a whole one. */
      const r = await steerAll(body);
      const n = r.data?.count ?? 0;
      const no = r.data?.refused_count ?? 0;
      toast(!r.ok ? (r.error || "nothing was steered")
            : no ? `steered ${n}, ${no} had no live channel`
            : `steered ${n} agent${n === 1 ? "" : "s"}`,
            (!r.ok || (!n && !!no)) ? "warn" : "ok");
      await refresh();
    } else if (aim && typeof aim === "object") {
      /* Straight to the seat: the item is filed for it and dispatched to it,
         with the human's own wording as the brief. */
      const r = await say(body, aim.seat);
      if (r.ok) await refresh();
    } else if (typeof aim === "number") {
      const r = await steerItem(aim, body);
      /* The tag STAYS after a steer. A correction is usually two sentences,
         not one, and dropping the aim between them sends the second to the
         director as fresh work — which is the mistake this whole control
         exists to stop. */
      if (!r.ok) setAim(null);
      await refresh();
    } else {
      const r = await say(body);
      if (r.ok) await refresh();
    }
    setSending(false);
  }

  /* THE ONE VERB OTHER SCREENS NEED FROM THE CONSOLE.
     atlas.js and the asset library both offer "deploy a task about this", and
     both used to prefill the old Agents board's queue composer by id. Those ids
     went with that board; without a replacement the buttons navigated here and
     did nothing, silently. Registered while this screen is mounted and handed
     back on unmount, so a stale hook can never outlive the component. */
  useEffect(() => {
    const prev = window.BGCompose;
    window.BGCompose = ({ seat, title, brief }) => {
      setMode("dispatch");
      setText([seat ? `@${seat}` : "", title, brief].filter(Boolean).join(" — "));
    };
    return () => { window.BGCompose = prev; };
  }, []);

  const turns = state.turns || [];
  const items = state.items || [];
  const floor = state.floor || {};
  const open = items.filter((i) => !CLOSED.has(i.status));
  const messages = session?.messages || [];
  /* Read, never assumed — see api.ts. The mood uses it too: a board that
     dispatches itself is 'auto', not 'waiting on you'. */
  const autoDeploy = !!state.autopilot?.on;

  /* THE ONE DECISION THIS COMPONENT MAKES. Live the moment there is anything to
     look at: a turn in this session, an agent running, or work waiting. NOT
     "has the user started typing" — a page that rearranges itself under the
     cursor mid-sentence is worse than either state. */
  const live = turns.length > 0 || (floor.running || 0) > 0 || open.length > 0
    || (mode === "brainstorm" && messages.length > 0);

  /* THE TAGGABLE AGENTS: what is running right now, with the item it is on.
     `state.agents` is the live roster and `items` carries the titles, so the
     menu can say "#424 tech — Wire mimic interact trigger" rather than an id
     the human has to go and look up. */
  const targets = (state.agents || [])
    .map((a) => {
      const item = items.find((i) => i.id === a.item_id);
      return item
        ? { item_id: item.id, seat: item.seat || "", title: item.title || "" }
        : null;
    })
    .filter((t): t is { item_id: number; seat: string; title: string } => !!t);

  /* An aim at an item that has since finished is an aim at nobody. Dropped
     here rather than on send, so the chip disappears when the run does. */
  useEffect(() => {
    if (typeof aim === "number" && !targets.some((t) => t.item_id === aim)) setAim(null);
    if (aim === "all" && !targets.length) setAim(null);
    // A seat aim survives a finished run on purpose: it is addressed to the
    // CRAFT, not to a process, and it is the form that works when the board is
    // idle.
    if (aim && typeof aim === "object" && seats.length && !seats.includes(aim.seat))
      setAim(null);
  }, [aim, targets, seats]);

  const composer = (variant: "hero" | "foot") => (
    <Composer variant={variant} mode={mode} onMode={setMode}
              value={text} onValue={setText} onSend={() => send()}
              targets={targets} seats={seats} aim={aim} onAim={setAim}
              sending={sending} autoDeploy={autoDeploy}
              onClear={variant === "foot"
                ? () => clearConsole().then(refresh) : undefined} />
  );

  return (
    <div className={`bg4-console${live ? " live" : ""}`} ref={host}
         style={{ ["--rail" as string]: `${readRail()}px` }}>
      {live
        ? <Live state={state} turns={turns} open={open} sending={sending}
                mode={mode} messages={messages} session={session}
                composer={composer("foot")}
                onBrainstormReset={() => { setSession(null); openBrainstorm(); }}
                onRefresh={refresh} />
        : <Idle items={items} floor={floor} composer={composer("hero")}
                onSuggest={(t) => send(t)} />}
    </div>
  );
}

/* ── 5a · idle ───────────────────────────────────────────────────────────── */

function Idle({ items, floor, composer, onSuggest }: {
  items: Item[];
  floor: NonNullable<ConsoleState["floor"]>;
  composer: React.ReactNode;
  onSuggest: (text: string) => void;
}) {
  const closed = items.filter((i) => CLOSED.has(i.status))
    .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")))
    .slice(0, 4);
  const all = (floor.done || 0) + (floor.failed || 0);

  return (
    <div className="bg4-idle">
      <div className="bg4-idle-inner">
        <Text fw={500} fz={17} mb="md">What should the studio work on?</Text>

        {/* NO MASCOT. It was here to say how the floor was doing before you
            read a word; it cost more attention than it returned and the owner
            cut it. The chips in the header carry the same state in words. */}
        <div className="bg4-hero">
          {composer}
        </div>

        <Group gap="xs" mt="sm" mb="xl">
          {SUGGESTIONS.map((s) => (
            <Button key={s.text} variant="default" size="xs" className="bg4-suggest"
                    leftSection={<Ti name={s.icon} size={13} />}
                    onClick={() => onSuggest(s.text)}>
              {s.text}
            </Button>
          ))}
        </Group>

        <Group gap="sm" className="bg4-rule" mb={4}>
          <Text className="bg4-eyebrow" style={{ padding: 0 }}>Last closed</Text>
          <span className="line" />
          <Text size="xs" c="dimmed" ff="var(--mono)">all {all}</Text>
        </Group>
        <Stack gap={0}>
          {closed.map((i) => <ClosedRow key={i.id} item={i} />)}
          {!closed.length && (
            <Text size="xs" c="dimmed" py="md">nothing has closed yet</Text>
          )}
        </Stack>
      </div>

      <Group className="bg4-idle-foot" gap="sm" wrap="nowrap">
        <Ti name="chevron-up" size={13} />
        <Text size="xs" c="dimmed" ff="var(--mono)">
          flow graph — opens when something is running
        </Text>
        <span style={{ flex: 1 }} />
        <Text size="xs" c="dimmed" ff="var(--mono)">nothing queued</Text>
      </Group>
    </div>
  );
}

function ClosedRow({ item }: { item: Item }) {
  const c = SEAT_COLOR[item.seat] || "var(--text-3)";
  const bad = item.status === "failed" || item.status === "rejected";
  return (
    <button className="bg4-closed" onClick={() => setSelection({
      key: `i${item.id}`, kind: "item", itemId: item.id,
      title: item.title, seat: item.seat,
    })}>
      <span className="tile" style={{ background: `${c}22`, color: c }}>
        <Ti name={SEAT_ICON[item.seat] || "point"} size={15} />
      </span>
      <span className="ttl">{item.title}</span>
      <span className={bad ? "st bad" : "st"}>{item.status}</span>
      <span className="age">{ago(item.updated_at)}</span>
    </button>
  );
}

/* ── 5b · session live ───────────────────────────────────────────────────── */

function Live({
  state, turns, open, sending, mode, messages, session, composer,
  onBrainstormReset, onRefresh,
}: {
  state: ConsoleState;
  turns: Turn[];
  open: Item[];
  sending: boolean;
  mode: "dispatch" | "brainstorm";
  messages: { id: number; role: string; text: string }[];
  session: BsSession | null;
  composer: React.ReactNode;
  onBrainstormReset: () => void;
  onRefresh: () => void;
}) {
  const [pane, setPane] = useState<"board" | "graph">("board");
  /* THE TABS BELONG TO THE RAIL, NOT TO THE BOARD PANE.
     They used to live inside BoardPane, so switching to Graph took Asked you,
     Approve, Responses, Chat and Stream off the screen with it — the graph is a
     different VIEW OF THE QUEUE, not a different console, and losing the rest
     of the rail to look at it is why the layout read as two products. Held
     here, the strip is the same strip in both panes and the switch only decides
     how the queue itself is drawn. */
  const [tab, setTab] = useState<string | null>("queue");
  /* Counts, not arrays — the 3s poll rebuilds the objects and scrolling on
     that would drag the reader to the bottom every tick. */
  const feed = useStickyBottom(
    mode === "brainstorm" ? messages.length : turns.length);

  return (
    <>
      <div className="bg4-console-main">
        {/* Follows the newest turn unless you have scrolled up to read. */}
        <ScrollArea className="bg4-transcript" type="auto" viewportRef={feed}>
          {mode === "brainstorm"
            ? messages.map((m) => (
                <Msg key={m.id} who={m.role === "user" ? "you" : "partner"} text={m.text} />
              ))
            : turns.length
            ? turns.map((t) => <TurnRow key={t.id} turn={t} />)
            /* A LIVE BOARD WITH AN EMPTY TRANSCRIPT IS THE NORMAL CASE NOW, and
               it was drawing a thousand pixels of black nothing.
               `live` is true when anything is running or queued — but work
               filed from a seat's Generate tab, from an MCP queue_add, or by a
               chain never passes through this box, so there are items and no
               turns. The console then rendered a void above a composer, which
               reads as a broken screen rather than as an empty conversation.
               Say which it is, and point at where the work actually is. */
            : (
              <div className="bg4-console-quiet">
                <b>Nothing has been said in this console.</b>
                <span>
                  {open.length
                    ? `${open.length} item${open.length === 1 ? " is" : "s are"} on the board — `
                      + "filed from a seat, an MCP call or a chain rather than typed here. "
                      + "The rail on the right is the board; this pane is the conversation."
                    : "Type below and the director answers, then delegates."}
                </span>
              </div>
            )}
          {sending && <Msg who="director" live text="waking up…" />}
        </ScrollArea>
        {/* THE CAT SITS ON THE SILL, between the transcript it reacts to and the
            box you type in. Not in the rail: it is a readout of the FLOOR, and
            the rail is the board's own detail. */}
        <Cat state={state} talking={sending} />
        {composer}
        {mode === "brainstorm" && (
          <div className="bg4-composer" style={{ borderTop: 0, paddingTop: 0 }}>
            <BrainstormFoot session={session} onReset={onBrainstormReset} />
          </div>
        )}
      </div>

      <RailGrip />
      <div className="bg4-console-side">
        <Group gap="xs" p="xs" className="bg4-side-head" wrap="nowrap">
          <SegmentedControl size="xs" value={pane}
                            onChange={(v) => setPane(v as "board" | "graph")}
                            data={[
                              { value: "board",
                                label: <span><Ti name="layout-list" size={12} /> Board</span> },
                              { value: "graph",
                                label: <span><Ti name="sitemap" size={12} /> Graph</span> },
                            ]} />
          <span style={{ flex: 1 }} />
          <Badge size="sm" variant="default" leftSection={<Ti name="clock" size={11} />}>
            {open.length} queued
          </Badge>
        </Group>
        <BoardPane state={state} open={open} onRefresh={onRefresh}
                   tab={tab} setTab={setTab}
                   /* The graph draws the QUEUE tab and nothing else: it is a
                      picture of the board's items and their chains, so it has
                      nothing to say about Approve or Stream. Picking one of
                      those with Graph selected shows that list and leaves the
                      switch where it was, so going back is one click. */
                   graph={pane === "graph" && tab === "queue"
                     ? <GraphPane state={state} /> : null} />
      </div>
    </>
  );
}

function BoardPane({ state, open, onRefresh, tab, setTab, graph }: {
  state: ConsoleState; open: Item[]; onRefresh: () => void;
  tab: string | null; setTab: (v: string | null) => void;
  /** The graph, when the switch is on it and the queue tab is showing. It
   *  replaces the queue's card list and nothing else — same tabs above it,
   *  same auto-deploy note below. */
  graph: React.ReactNode;
}) {
  const autoDeploy = !!state.autopilot?.on;
  const [busy, setBusy] = useState(false);
  const queued = open.filter((i) => i.status === "queued");
  const responses = (state.items || []).filter((i) => i.result);

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
            <Tabs.Tab value="chat">Chat</Tabs.Tab>
            <Tabs.Tab value="stream">Stream</Tabs.Tab>
          </Tabs.List>
        </Tabs>
        {tab === "queue" && queued.length > 0 && (
          <Button size="compact-xs" onClick={deployAll} loading={busy}
                  leftSection={<Ti name="send" size={12} />}>
            deploy all
          </Button>
        )}
      </Group>

      {/* The graph is its own body: it fills the rail and scrolls itself (it
          pans), so it must not be wrapped in the card scroller below. */}
      {graph}

      {!graph && (
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

/** One line of the conversation. `who` picks the treatment: `you` is the flat
 *  tinted block, everyone else is a bordered card. `live` marks a turn still
 *  being thought about — the border goes accent and the text dims, so a reply
 *  in progress never reads as a delivered answer. */
function Msg({ who, text, live }: {
  who: "you" | "partner" | "director"; text: string; live?: boolean;
}) {
  const mine = who === "you";
  return (
    <div className={`bg4-msg ${mine ? "you" : "dir"}${live ? " live" : ""}`}>
      <div className="who">{who}</div>
      <div className={`txt${live ? " thinking" : ""}`}>{text}</div>
    </div>
  );
}

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

function TurnRow({ turn }: { turn: Turn }) {
  const r = turn.reply || {};
  return (
    <div className="bg4-turn">
      <Msg who="you" text={turn.said || turn.title || ""} />
      {r.text ? (
        <div className="bg4-msg dir">
          <div className="who">director</div>
          {/* A long answer folds instead of pushing the composer off screen, and
              Spoiler keeps its open state across the poll — which the classic
              <details> could not, once innerHTML replaced it every 3s. */}
          <Spoiler maxHeight={260} showLabel="show the rest" hideLabel="fold"
                   classNames={{ control: "bg4-spoiler" }}>
            <div className="txt">{r.text}</div>
          </Spoiler>
          <div className="foot">
            {r.cost ? `$${Number(r.cost).toFixed(3)}` : ""}
            {r.step_count ? ` · ${r.step_count} steps` : ""}
          </div>
        </div>
      ) : r.running ? (
        <Msg who="director" live text={r.thinking || "reading the board…"} />
      ) : turn.status === "failed" ? (
        <div className="bg4-msg dir bad">
          <div className="who">director</div>
          <div className="txt">that turn failed — open its log from the floor</div>
        </div>
      ) : (
        <Msg who="director" text="not dispatched — nothing has read this yet" />
      )}
    </div>
  );
}
