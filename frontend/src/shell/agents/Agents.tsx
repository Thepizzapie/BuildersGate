import { useCallback, useEffect, useRef, useState } from "react";
import {
  Badge, Button, Group, Paper, ScrollArea, SegmentedControl, Stack, Tabs, Text, Textarea,
} from "@mantine/core";
import { Ti } from "../Ti";
import { SEAT_COLOR, SEAT_ICON } from "../nav";
import { useViewActive, useEvents } from "../../hooks";
import { setSelection } from "../selection";
import { askText, notifyUpdate, mutate, readJSON, toast } from "../../bridge";
import { ago } from "../seats/api";
import { DirectorChat } from "./DirectorChat";
import { FloorPane } from "./FloorPane";
import { moduleOff } from "../../bridge";
import { Streamer } from "./Streamer";
import { ChatLive } from "./ChatLive";
import {
  EMPTY_CONSOLE, consoleState, type ConsoleState, type Item, type Question,
} from "./api";
import {
  DISMISSED_ATTENTION_STORAGE, gateAttentionKey, itemAttentionKey,
  questionAttentionKey, readDismissedAttention,
} from "./attention";
import { claimUtility, onUtility } from "../utility";

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
    /** The delegation graph (static/agents_graph.js). `mount` takes the canvas
     *  host and the detail rail; `apply` is fed the console state on each poll. */
    AgentsGraph?: {
      mount(host: HTMLElement, detail: HTMLElement | null): unknown;
      apply(state: unknown): void;
      activate?(): void;
      fit?(): void;
    };
  }
}

export type Pane = "board" | "graph" | "floor";

const CLOSED = new Set(["done", "failed", "cancelled", "approved", "rejected"]);

function attentionTotal(state: ConsoleState, open: Item[], dismissed: ReadonlySet<string>): number {
  return (state.questions || []).filter((q) => !dismissed.has(questionAttentionKey(q))).length
    + (state.gates || []).filter((g) => g.blocking !== false && !dismissed.has(gateAttentionKey(g))).length
    + (state.items || []).filter((i) => i.status === "failed" && !dismissed.has(itemAttentionKey(i))).length
    + open.filter((i) => ["blocked", "held", "exhausted"].includes(i.execution_state || "")
      && !dismissed.has(itemAttentionKey(i))).length;
}

export function Agents() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [state, setState] = useState<ConsoleState>(EMPTY_CONSOLE);
  const [pane, setPane] = useState<Pane>("board");
  const [mobileRail, setMobileRail] = useState(false);
  const [localDismissed, setLocalDismissed] = useState<string[]>(readDismissedAttention);

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
  useEvents(refresh, { enabled: active });

  const items = state.items || [];
  const open = items.filter((i) => !CLOSED.has(i.status));
  const dismissed = new Set([...(state.dismissed_attention || []), ...localDismissed]);
  const attention = attentionTotal(state, open, dismissed);

  const dismissAttention = useCallback((key: string) => {
    setLocalDismissed((current) => {
      if (current.includes(key)) return current;
      const next = [...current, key].slice(-500);
      try {
        window.localStorage.setItem(DISMISSED_ATTENTION_STORAGE, JSON.stringify(next));
      } catch {
        // Storage can be unavailable in locked-down browser contexts; the
        // in-memory dismissal still takes effect for this session.
      }
      return next;
    });
    void fetch("/api/console/attention/dismiss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    const show = () => { claimUtility("orchestration"); setMobileRail(true); };
    window.addEventListener("bgate:orchestration-tab", show);
    return () => window.removeEventListener("bgate:orchestration-tab", show);
  }, []);
  useEffect(() => onUtility((name) => { if (name !== "orchestration") setMobileRail(false); }), []);

  return (
    <div className={`bg4-console live${mobileRail ? " rail-open" : ""}`} ref={host}
         style={{ ["--rail" as string]: `${readRail()}px` }}>
      <button className="bg4-mobile-board" onClick={() => { claimUtility("orchestration"); setMobileRail(true); }}>
        <Ti name="layout-sidebar-right" size={15} /> Work & attention
        {attention > 0 && <span>{attention}</span>}
      </button>
      <button className="bg4-mobile-scrim" aria-label="Close work panel" onClick={() => setMobileRail(false)} />
      <DirectorChat active={active} onSent={refresh} />
      <RailGrip />
      <Rail state={state} open={open} pane={pane} setPane={setPane}
            dismissed={dismissed} onDismiss={dismissAttention}
            onRefresh={refresh} onClose={() => setMobileRail(false)} />
    </div>
  );
}

/* ── the board rail ─────────────────────────────────────────────────────── */

function Rail({ state, open, pane, setPane, dismissed, onDismiss, onRefresh, onClose }: {
  state: ConsoleState;
  open: Item[];
  /* THREE READINGS OF ONE QUEUE: a list, a dependency graph, and the floor,
     where a seat is a room and an agent's POSITION is its state. The pane is
     the parent's state so the rest of the screen can read it. */
  pane: Pane;
  setPane: (p: Pane) => void;
  dismissed: ReadonlySet<string>;
  onDismiss: (key: string) => void;
  onRefresh: () => void;
  onClose: () => void;
}) {
  /* THE TABS BELONG TO THE RAIL, NOT TO THE BOARD PANE. They used to live
     inside BoardPane, so switching to Graph took Asked you, Approve and
     Responses off the screen with it — the graph is a different VIEW OF THE
     QUEUE, not a different screen. */
  const [tab, setTab] = useState<string | null>("queue");
  useEffect(() => {
    const pick = (e: Event) => setTab((e as CustomEvent<{ tab?: string }>).detail?.tab || "attention");
    window.addEventListener("bgate:orchestration-tab", pick);
    return () => window.removeEventListener("bgate:orchestration-tab", pick);
  }, []);

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
        <button className="bg4-mobile-close" onClick={onClose} aria-label="Close work panel"><Ti name="x" size={15} /></button>
      </Group>
      <BoardPane state={state} open={open} dismissed={dismissed} onDismiss={onDismiss} onRefresh={onRefresh}
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

function BoardPane({ state, open, dismissed, onDismiss, onRefresh, tab, setTab, queueView }: {
  state: ConsoleState; open: Item[]; onRefresh: () => void;
  dismissed: ReadonlySet<string>; onDismiss: (key: string) => void;
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
  const [broadcastPane, setBroadcastPane] = useState("chat");
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
  useEvents(loadStreamer, { kinds: ["settings.*"] });

  /* Turning streamer mode off while sitting on one of its tabs would leave the
     strip with nothing selected and the body blank. Fall back to the queue. */
  useEffect(() => {
    if (!streamer && tab === "broadcast") setTab("queue");
  }, [streamer, tab, setTab]);
  /* WHAT HAS REPORTED, newest first. The window /api/console/state returns is
     already the recent board — this used to be scoped by the console's cut
     line, which went with the work-item turns. */
  const responses = (state.items || []).filter((i) => i.result)
    .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));

  /* THE FAILURES, WITH THE TWO VERBS A HUMAN HAS FOR THEM. Failed items used
     to exist only as a red number in the floor tally: the automatic
     escalation paths see recent failures only (the event batch, the sweep's
     12-hour window), so anything that failed while the server was down aged
     out with no surface anywhere to act on it. */
  const failed = (state.items || []).filter((i) => i.status === "failed"
      && !dismissed.has(itemAttentionKey(i)))
    .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
  const needsWork = open.filter((i) => ["blocked", "held", "exhausted"].includes(i.execution_state || "")
    && !dismissed.has(itemAttentionKey(i)));
  const blockingGates = state.gates.filter((g) => g.blocking !== false
    && !dismissed.has(gateAttentionKey(g)));
  const questions = state.questions.filter((q) => !dismissed.has(questionAttentionKey(q)));
  const attentionCount = questions.length + blockingGates.length + failed.length + needsWork.length;

  async function escalate(i: Item) {
    const r = await mutate("/api/console/escalate",
                           { body: { item_id: i.id }, quiet: true });
    toast(r.ok ? `#${i.id} escalated to the director` : r.error || "refused",
          r.ok ? "ok" : undefined);
    if (r.ok) onRefresh();
  }

  async function reopenFailed(i: Item) {
    const reason = await askText({
      title: `reopen #${i.id}`,
      body: "What should change before it runs again? The next agent reads this.",
      ok: "reopen",
    });
    if (!reason) return;
    const r = await mutate("/api/console/signoff",
                           { body: { item_id: i.id, verdict: "reopen", reason },
                             quiet: true });
    toast(r.ok ? `#${i.id} is back in the queue` : r.error || "refused",
          r.ok ? "ok" : undefined);
    if (r.ok) onRefresh();
  }

  /* Deploy what is READY, one at a time. A chain link whose predecessor has not
     landed refuses, and firing twenty at once turns one concurrency refusal
     into twenty toasts — so this is sequential, stops at the first real
     failure, and reports how far it got. */
  async function deployAll() {
    /* Held rows (escalations, chat) are one-at-a-time human acts — a bulk
       deploy firing one is exactly the auto-dispatch they are held from. */
    const ready = queued.filter((i) => i.ready !== false && !i.held);
    const held = queued.length - ready.length;
    if (!ready.length) {
      toast(held ? `${held} item(s) are waiting on earlier links or held for you`
                 : "nothing to deploy");
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
      <div className="bg4-rail-summary">
        <div><b>{attentionCount}</b><span>Needs attention</span></div>
        <div><b>{open.length}</b><span>Open work</span></div>
        <div><b>{responses.length}</b><span>Reports</span></div>
      </div>
      {/* nowrap + a scroller: five pills do not fit a 330px rail, and Mantine's
          Tabs.List wraps by default — which is the two ragged rows in the
          screenshot. Scrolling keeps them one row and keeps every tab
          reachable, which hiding one behind a "more" menu would not. */}
      <Group gap="xs" px="xs" pb="xs" wrap="nowrap" className="bg4-side-tabs">
        <Tabs value={tab} onChange={setTab} variant="pills" style={{ flex: 1, minWidth: 0 }}>
          <Tabs.List>
            <Tabs.Tab value="queue">Work {queued.length || ""}</Tabs.Tab>
            <Tabs.Tab value="attention">Needs attention {attentionCount || ""}</Tabs.Tab>
            <Tabs.Tab value="activity">Activity</Tabs.Tab>
            {streamer && <Tabs.Tab value="broadcast">Broadcast</Tabs.Tab>}
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

          {tab === "attention" && !attentionCount && <Empty>nothing needs your attention</Empty>}
          {tab === "attention" && questions.length > 0 && <div className="bg4-attention-group">
            <Text size="xs" fw={600}>Questions</Text>
            {questions.map((q) => <QuestionCard key={q.id} question={q} onAnswered={onRefresh}
              onDismiss={() => onDismiss(questionAttentionKey(q))} />)}
          </div>}

          {tab === "attention" && blockingGates.length > 0 && <div className="bg4-attention-group">
            <Text size="xs" fw={600}>Approvals</Text>
            {blockingGates.map((g) => (
              <Paper key={g.id} p="xs" withBorder className="bg4-sidecard">
                <Group gap={6} wrap="nowrap">
                  <div style={{ flex: 1, minWidth: 0 }}><Text size="xs">{g.title}</Text><Text size="xs" c="dimmed">{g.seat}</Text></div>
                  <Button size="compact-xs" variant="subtle" color="gray"
                          onClick={() => onDismiss(gateAttentionKey(g))}>dismiss</Button>
                </Group>
              </Paper>
            ))}
          </div>}

          {tab === "broadcast" && <>
            <SegmentedControl size="xs" fullWidth value={broadcastPane} onChange={setBroadcastPane}
                              data={[{ value: "chat", label: "Live chat" }, { value: "stream", label: "Stream tools" }]} />
            {broadcastPane === "chat" ? <div className="bg4-chathost"><ChatLive /></div> : <Streamer />}
          </>}

          {tab === "attention" && failed.length > 0 && <div className="bg4-attention-group">
            <Text size="xs" fw={600}>Failed work</Text>
            {failed.map((i) => (
              <Paper key={i.id} p="xs" withBorder className="bg4-sidecard"
                     onClick={() => setSelection({ key: `i${i.id}`, kind: "item", itemId: i.id, title: i.title, seat: i.seat })}>
                <SeatStamp item={i} verb="failed" /><Text size="xs" fw={500} lineClamp={1}>{i.title}</Text>
                <Group gap={6} mt={4} wrap="nowrap" className="bg4-failure-actions" onClick={(e) => e.stopPropagation()}><div style={{ flex: 1 }} />
                  {i.escalated ? <Badge size="xs" variant="light" className="bg4-escalated">escalated</Badge> : <Button size="compact-xs" variant="default" onClick={() => escalate(i)}>escalate</Button>}
                  <Button size="compact-xs" variant="default" onClick={() => reopenFailed(i)}>reopen</Button>
                  <Button size="compact-xs" variant="subtle" color="gray"
                          onClick={() => onDismiss(itemAttentionKey(i))}>dismiss</Button>
                </Group>
              </Paper>
            ))}
          </div>}

          {tab === "attention" && needsWork.length > 0 && <div className="bg4-attention-group">
            <Text size="xs" fw={600}>Blocked or held</Text>
            {needsWork.map((i) => <QueueCard key={i.id} item={i} items={state.items}
              onDismiss={() => onDismiss(itemAttentionKey(i))} />)}
          </div>}

          {tab === "activity" && (responses.length
            ? responses.slice(0, 30).map((i) => (
                <Paper key={i.id} p="xs" withBorder className="bg4-sidecard"
                       onClick={() => setSelection({ key: `i${i.id}`, kind: "item",
                                                     itemId: i.id, title: i.title, seat: i.seat })}>
                  <SeatStamp item={i} />
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

function QuestionCard({ question, onAnswered, onDismiss }: {
  question: Question; onAnswered(): void; onDismiss(): void;
}) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  async function send() {
    const text = answer.trim();
    if (!text) return;
    setBusy(true);
    const r = await mutate<{ delivery?: string }>("/api/console/answer",
      { body: { seq: question.id, answer: text }, quiet: true });
    setBusy(false);
    if (!r.ok) { toast(r.error || "the answer was refused"); return; }
    toast(String(r.data?.delivery || "answer recorded"), "ok");
    setAnswer(""); onAnswered();
  }
  return <Paper p="xs" withBorder className="bg4-sidecard bg4-question-card">
    <Group gap={6} wrap="nowrap"><Text size="xs" c="dimmed">{question.seat || "director"}</Text>
      <Text size="xs" c="dimmed" style={{ flex: 1 }}>{ago(question.asked_at)}</Text>
      <Button size="compact-xs" variant="subtle" color="gray" onClick={onDismiss}>dismiss</Button></Group>
    <Text size="xs" mt={5}>{question.text}</Text>
    <Textarea value={answer} onChange={(e) => setAnswer(e.currentTarget.value)} autosize minRows={2} maxRows={5}
              mt="xs" size="xs" placeholder="Answer this question"
              onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") void send(); }} />
    <Group justify="flex-end" mt="xs"><Button size="compact-xs" loading={busy} disabled={!answer.trim()} onClick={() => void send()}>Send answer</Button></Group>
  </Paper>;
}

function QueueCard({ item, items, onDismiss }: { item: Item; items: Item[]; onDismiss?: () => void }) {
  const c = SEAT_COLOR[item.seat] || "var(--text-3)";
  /* THE SERVER'S VERDICT WINS. _chain_state stamps ready/waiting_on across
     BOTH dependency stores; the local depends_on lookup survives only as the
     fallback for a payload from an older server, because it cannot see
     fan-in parents and its blocker may sit outside the board window. */
  const localBlocker = item.depends_on
    ? items.find((x) => x.id === item.depends_on) : undefined;
  const blocker = item.waiting_on
    ?? (localBlocker && !CLOSED.has(localBlocker.status)
        ? localBlocker : undefined);
  const waiting = item.ready === false || (item.ready == null && !!blocker);
  /* A dead predecessor is not "waiting" — nothing will ever free it. Name
     the two acts that do, or the card reads as patience when it is a stall. */
  const stuck = !!item.stuck
    || (!!blocker && (blocker.status === "failed" || blocker.status === "cancelled"));
  const more = (item.waiting_count || 0) > 1 ? ` +${item.waiting_count! - 1}` : "";
  /* EXHAUSTED IS ITS OWN STATE. The harness has stopped buying rounds for this
     item and a person now owns it; before the row carried it, this rendered as
     an ordinary queued card and the only tell was reading auto_retries off the
     database and comparing it to a setting by hand. Two readers did that
     arithmetic differently on the same board. */
  const exhausted = item.execution_state === "exhausted" || !!item.exhausted_at;
  /* THE SERVER WRITES THE SENTENCE, and it names the blocker's TITLE. The
     local fallback below says `blocked until #45 closes`, which still sends
     the reader off to look #45 up — and the defect this fixes is precisely
     that they were not looking it up: `#43 QUEUED` beside a running #45 and a
     done #42 read as a scheduler that skipped an item, when #45 had been
     inserted between them on purpose and the order was right. */
  const line = item.waiting_line
    ?? (item.held
      ? "held for you — no auto-dispatcher takes this"
      : stuck && blocker
        ? `stuck: #${blocker.id} is ${blocker.status} — reopen it or cut the dependency`
        : waiting && blocker
          ? `blocked until #${blocker.id}${more} closes`
          : item.seat);
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
          <Text size="xs" c="dimmed" ff="var(--mono)" lineClamp={2}>
            {item.chain_pos != null ? `lane ${item.chain_pos} · ` : ""}
            {line}
          </Text>
        </div>
        <Stack gap={2} align="flex-end">
          <Badge size="xs" variant="default"
                 color={exhausted ? "red" : stuck ? "red" : item.held ? "grape"
                        : waiting ? "yellow" : undefined}>
            {exhausted ? "exhausted" : stuck ? "stuck" : item.held ? "held"
             : waiting ? "waiting" : item.status}
          </Badge>
          {onDismiss && <Button size="compact-xs" variant="subtle" color="gray"
            onClick={(e) => { e.stopPropagation(); onDismiss(); }}>dismiss</Button>}
        </Stack>
      </Group>
    </Paper>
  );
}

/* THE DELEGATION GRAPH — the real one, mounted.
 *
 * agents_graph.js is a NodeCanvas graph with drag-to-place, saved positions per
 * project, an active-work scope, spotlighting of what is live, and a detail
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
      <div className="bg4-graphbar">
        <span className="on" title="running, queued, gated, and recent failures plus their dependency chain">active</span>
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

/** WHICH AGENT, WHICH TASK, WHEN — a card's identity line. A title alone
 *  ("prop_whiteboard ships its _ne and _se views swapped") reads as one
 *  more unlabeled result in a pile of thirty; the seat that ran it and the
 *  item number that ties it to the board (the graph, the inspector, a
 *  reopen) were pushed into a dim trailing line nobody scanned. This puts
 *  both first: a coloured, iconed seat pill (the same colour the floor and
 *  the graph use for that seat, so a reader learns one mapping and reuses
 *  it everywhere), the item id, and a relative time whose title attribute
 *  carries the full UTC stamp for whoever needs it exactly. */
function SeatStamp({ item, verb = "" }: { item: Item; verb?: string }) {
  const color = SEAT_COLOR[item.seat] || "var(--line-strong)";
  return (
    <Group gap={6} mb={2} wrap="nowrap">
      <Group gap={4} wrap="nowrap" className="bg4-seatpill"
             style={{ "--seat": color } as React.CSSProperties}>
        <Ti name={SEAT_ICON[item.seat] || "point"} size={11} />
        <span>{item.seat || "—"}</span>
      </Group>
      <Text size="xs" c="dimmed">#{item.id}</Text>
      {!!item.updated_at && (
        <Text size="xs" c="dimmed" title={item.updated_at} style={{ flex: 1 }}>
          {verb ? `${verb} ` : ""}{ago(item.updated_at)} ago
        </Text>
      )}
    </Group>
  );
}
