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
   *  than aborting on the refusal they would return. */
  ready?: boolean;
  /** The link this one is blocked behind, when it is blocked. _chain_state
   *  sends the predecessor's own row so a card can name WHO it is waiting on
   *  rather than printing a bare id at somebody. */
  waiting_on?: { id: number; seat?: string; title?: string; status?: string } | null;
};
export type Question = { id: number; seat?: string; text?: string; asked_at?: string };
/** `blocking` IS THE FIELD THAT DECIDES WHETHER A HUMAN IS NEEDED, and it was
 *  missing from this type while the route has always sent it: routes/console.py
 *  stamps `blocking:false` on a plain qa-gate, because that is an agent
 *  reviewing an agent — queued work, not something waiting on a person. Every
 *  reader that filters on it (the director's rail, the cat's mood) had to reach
 *  past the type to see it. */
export type Gate = {
  id: number; seat?: string; title?: string; item_id?: number;
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
    done?: number; failed?: number;
  };
  /** Whether the director session is mid-reply. Chat turns stopped being
   *  work items, so floor.running no longer covers a streaming answer — and
   *  floorIsQuiet's whole contract ("one source of words on screen") needs
   *  this shared signal rather than the chat pane's private running flag. */
  director?: { running?: boolean };
};

export const EMPTY_CONSOLE: ConsoleState = {
  items: [], agents: [], questions: [], gates: [],
};

export const consoleState = () =>
  readJSON<ConsoleState>("/api/console/state", EMPTY_CONSOLE);

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

export type ChatState = {
  __error?: string;
  messages: ChatMsg[];
  /** A turn is in flight right now. */
  running?: boolean;
  live?: boolean;
  session_id?: string;
  spent_usd?: number;
  model?: string;
  ceiling_usd?: number;
};

export const directorChat = (after = 0) =>
  readJSON<ChatState>(`/api/director/chat?after=${after}`, { messages: [] });

export const directorSay = (text: string) =>
  mutate<{ n?: number }>("/api/director/say", { body: { text }, quiet: true });

/** Fresh conversation. The old transcript is archived on disk, not deleted. */
export const directorNew = () =>
  mutate("/api/director/new", { quiet: true });
