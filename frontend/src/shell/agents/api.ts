import { mutate, readJSON } from "../../bridge";

/* The console's endpoints, in one file.
 *
 * Two things live behind this screen and they are not the same thing: the
 * DIRECTOR CHAT (/api/director/*) is a Claude Code session's transcript, and
 * the BOARD (/api/console/state) is what the seats are doing. The chat used to
 * be modelled as work items on that board, which is why `say` returned a turn
 * id and the transcript had costs and step counts in it.
 */

export type Item = {
  id: number; seat: string; title: string; status: string;
  priority?: number; result?: string; total_cost_usd?: number;
  created_at?: string; updated_at?: string;
  /** Chain membership — `lane 2` on a card is chain_pos, and depends_on is what
   *  it is blocked behind. */
  chain_id?: number | null; chain_pos?: number | null; depends_on?: number | null;
  /** False when an earlier link has not landed. deploy-all skips these rather
   *  than aborting on the refusal they would return. Covers BOTH the
   *  depends_on column and the fan-in parents in work_item_dep — deriving
   *  this client-side from depends_on alone is how a card with an unmet
   *  extra parent got a deploy button whose one outcome was a refusal. */
  ready?: boolean;
  /** The link this one is blocked behind, when it is blocked. _chain_state
   *  sends the predecessor's own row so a card can name WHO it is waiting on
   *  rather than printing a bare id at somebody. */
  waiting_on?: { id: number; seat?: string; title?: string; status?: string } | null;
  /** How many parents are unmet when it is more than the one in waiting_on. */
  waiting_count?: number;
  /** A blocking predecessor is failed/cancelled: it will NEVER reach done on
   *  its own, so this is not "waiting", it is parked until a human reopens
   *  the predecessor or cuts the dependency. The card copy must say so —
   *  "waiting" on a dead link is how chains sat still for days. */
  stuck?: boolean;
  /** Queued, but no auto-dispatcher will ever take it (escalations, chat):
   *  a human acts on it. Without the flag it looked like any queued item. */
  held?: boolean;
  /** ONE WORD FOR WHAT IS ACTUALLY HAPPENING, from the server rather than
   *  recombined here from four booleans:
   *    ready | running | waiting | blocked | held | exhausted | <status>
   *  `waiting` is the board working and nobody should touch it; `blocked`,
   *  `held` and `exhausted` each need a person, and all three used to render
   *  as an ordinary queued row. */
  execution_state?: string;
  /** The sentence — "WAITING ON #45 Swap in furniture". It names the
   *  blocker's TITLE on purpose: `blocked until #45 closes` still makes the
   *  reader go and look #45 up, and the whole defect is that they were not
   *  looking things up. #43 queued next to a running #45 read as a skipped
   *  item when the order #42 -> #45 -> #43 was correct all along. */
  waiting_line?: string;
  /** Every predecessor, both dependency stores merged. Which table holds a
   *  link is a fact about the database, not a question a reader should have. */
  depends_on_all?: number[];
  unresolved?: number[];
  /** The harness has stopped buying rounds for this item; only a reopen
   *  starts it again. It used to be two counters somebody had to add up. */
  exhausted_at?: string | null;
  exhausted_why?: string | null;
  /** Failed items only: a director escalation already exists for this failure
   *  (the once-per-item cap), so the rail shows a badge instead of a button. */
  escalated?: boolean;
};
export type Question = {
  id: number; event_seq?: number; item_id?: number; seat?: string;
  text?: string; question?: string; asked_at?: string; refs?: string[];
};
/** `blocking` IS THE FIELD THAT DECIDES WHETHER A HUMAN IS NEEDED, and it was
 *  missing from this type while the route has always sent it: routes/console.py
 *  stamps `blocking:false` on a plain qa-gate, because that is an agent
 *  reviewing an agent — queued work, not something waiting on a person. Every
 *  reader that filters on it (the director's rail, the cat's mood) had to reach
 *  past the type to see it. */
export type Gate = {
  id: number | string; seat?: string; title?: string; item_id?: number;
  kind?: string; blocking?: boolean; parked?: boolean;
  result?: string; status?: string; created_at?: string;
};
export type ConsoleState = {
  /** SET WHEN THE READ FAILED, and the whole payload beside it is then the
   *  caller's fallback rather than the board. readJSON never throws: it returns
   *  the fallback tagged with this, so a panel that does not look at it renders
   *  an empty board as fact. Declared here because the console's readers all
   *  have to be able to tell "nothing is running" from "nobody answered". */
  __error?: string;
  /* `state` MATTERS AND WAS NOT DECLARED. This roster is every agent the
     dispatcher knows about -- running, finished and failed -- not just the
     live ones; the server filters by this field for its own bookkeeping and
     sends the list unfiltered. Leaving it off the type is why the tag menu
     read the whole roster as "running" and offered five dead agents to steer.
     Callers that mean "live" must say so. */
  items: Item[]; agents: { item_id: number; state?: string }[];
  questions: Question[]; gates: Gate[];
  /** Which item produced which — the Graph pane's whole input. */
  lineage?: { parents?: Record<string, number>; children?: Record<string, number[]> };
  /* WHETHER THE BOARD DISPATCHES ITSELF. The console has always shipped this;
     the screen used to hardcode `false` and print "auto-deploy is off" as a
     fact beside it — right until somebody turned it on or set BGATE_AUTODEPLOY,
     after which the console asserted the opposite of what the board was doing. */
  autopilot?: { on?: boolean; source?: string; env_override?: string };
  floor?: {
    /* `dispatched` was missing while the route has always sent it, so anything
       asking "is the board empty" from these counts alone read a board with
       handed-out work on it as idle. */
    running?: number; queued?: number; dispatched?: number; review?: number;
    integrating?: number; done?: number; failed?: number;
  };
  /** Whether the director session is mid-reply. Chat turns stopped being
   *  work items, so floor.running no longer covers a streaming answer — and
   *  floorIsQuiet's whole contract ("one source of words on screen") needs
   *  this shared signal rather than the chat pane's private running flag. */
  director?: { running?: boolean };
  /** Per-project UI acknowledgements. The underlying item remains on the
   *  board; only its current attention snapshot is hidden. */
  dismissed_attention?: string[];
};

export const EMPTY_CONSOLE: ConsoleState = {
  items: [], agents: [], questions: [], gates: [],
};

export const consoleState = async () => {
  const state = await readJSON<ConsoleState>("/api/console/state", EMPTY_CONSOLE);
  state.questions = (state.questions || []).map((q) => ({
    ...q,
    id: Number(q.id || q.event_seq || 0),
    text: q.text || q.question || "",
  }));
  return state;
};

/* ── the director chat ───────────────────────────────────────────────────────
 *
 * A Claude Code session in the project, and its transcript. `after` is the last
 * message number you have seen, so a poll carries only what is new. */

export type ChatMsg = {
  n: number; ts?: number;
  /** user and assistant are the conversation; tool is one call the session
   *  made on the way; error is a turn that did not land. */
  role: "user" | "assistant" | "tool" | "error";
  text: string; tool?: string;
};

export type DirectorApproval = {
  id: string; kind: "command" | "file_change" | "permissions" | "mcp";
  reason?: string; command?: string; cwd?: string; server?: string;
  permissions?: unknown; available_decisions: string[];
};

export type ChatState = {
  __error?: string;
  messages: ChatMsg[];
  /** A turn is in flight right now. */
  running?: boolean;
  /** Why a running turn is silent, when it is not the model thinking: an
   *  overloaded API being retried, or a refused usage window. */
  waiting?: string;
  live?: boolean;
  session_id?: string;
  approvals?: DirectorApproval[];
  dispatch_mode?: "structured" | "chaos";
  runner?: "claude" | "codex";
  model?: string;
  runners?: { value: string; label: string; installed: boolean }[];
  models?: Record<string, {
    value: string; label: string; description?: string; default?: boolean;
  }[]>;
  usage?: {
    context?: { used?: number; limit?: number };
    five_hour?: { used_percent?: number; resets_at?: number; status?: string };
    weekly?: { used_percent?: number; resets_at?: number; status?: string };
  };
  usage_bridge?: {
    enabled?: boolean; has_snapshot?: boolean; updated_at?: number;
    needs_restart?: boolean;
  };
};

export const directorChat = (after = 0) =>
  readJSON<ChatState>(`/api/director/chat?after=${after}`, { messages: [] });

export const directorSay = (text: string) =>
  mutate<{ n?: number }>("/api/director/say", { body: { text }, quiet: true });

/** Fresh conversation. The old transcript is archived on disk, not deleted. */
export const directorNew = () =>
  mutate("/api/director/new", { quiet: true });

export const directorConfigure = (runner: string, model: string) =>
  mutate<ChatState>("/api/director/config", {
    method: "PUT", body: { runner, model }, quiet: true,
  });

export const directorDispatchMode = (mode: "structured" | "chaos") =>
  mutate<ChatState>("/api/director/dispatch-mode", {
    method: "PUT", body: { mode }, quiet: true,
  });

export const directorUsageConnect = () =>
  mutate<NonNullable<ChatState["usage_bridge"]>>("/api/director/usage-bridge", {
    method: "POST", quiet: true,
  });

export const directorUsageDisconnect = () =>
  mutate<NonNullable<ChatState["usage_bridge"]>>("/api/director/usage-bridge", {
    method: "DELETE", quiet: true,
  });

export const directorApprove = (id: string, decision: string) =>
  mutate(`/api/director/approvals/${encodeURIComponent(id)}`, {
    method: "POST", body: { decision }, quiet: true,
  });
