import { mutate, readJSON } from "../../bridge";

/* The console's endpoints, in one file.
 *
 * agents_console.js reached for eleven URLs from nine call sites, and the two
 * that mattered most — brainstorm reset and deploy — were built into innerHTML
 * strings, which is how a Reset button can go missing from a rebuild without
 * anything failing loudly. Naming them here means a screen that forgets one
 * does not compile.
 */

export type Reply = {
  text?: string; running?: boolean; thinking?: string;
  cost?: number; step_count?: number;
};
export type Turn = {
  id: number; said?: string; title?: string; status: string; reply?: Reply;
};
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
  /** Work-item id the current console session starts after. */
  cleared_before?: number;
  /* `state` MATTERS AND WAS NOT DECLARED. This roster is every agent the
     dispatcher knows about -- running, finished and failed -- not just the
     live ones; the server filters by this field for its own bookkeeping and
     sends the list unfiltered. Leaving it off the type is why the tag menu
     read the whole roster as "running" and offered five dead agents to steer.
     Callers that mean "live" must say so. */
  turns: Turn[]; items: Item[]; agents: { item_id: number; state?: string }[];
  questions: Question[]; gates: Gate[];
  /** Which item produced which — the Graph pane's whole input. */
  lineage?: { parents?: Record<string, number>; children?: Record<string, number[]> };
  /* WHETHER THE BOARD DISPATCHES ITSELF. The console has always shipped this;
     the screen used to hardcode `false` and print "auto-deploy is off" as a
     fact beside it — right until somebody turned it on or set BGATE_AUTODEPLOY,
     after which the console asserted the opposite of what the board was doing. */
  autopilot?: { on?: boolean; source?: string; env_override?: string };
  floor?: {
    running?: number; queued?: number; review?: number;
    done?: number; failed?: number;
  };
};

export const EMPTY_CONSOLE: ConsoleState = {
  turns: [], items: [], agents: [], questions: [], gates: [],
};

export const consoleState = () =>
  readJSON<ConsoleState>("/api/console/state", EMPTY_CONSOLE);

/** `seat` addresses the work to that craft instead of the director, which
 *  answers and delegates. Omitted, this is exactly what it always was. */
export const say = (text: string, seat?: string) =>
  mutate("/api/console/say", { body: seat ? { text, seat } : { text } });

export const clearConsole = () =>
  mutate("/api/console/clear", { ok: "console cleared" });

/* ── steering ────────────────────────────────────────────────────────────────
 *
 * `say` hands work OUT — it reaches the director, which answers and delegates.
 * These two go the other way: they interrupt work that is ALREADY RUNNING. That
 * is the distinction the composer's tag makes visible, because typing
 * "@narrative — do it this way instead" into the director got a new item filed
 * against work that was already in flight, which is the expensive kind of
 * misunderstanding. */
export const steerItem = (itemId: number, text: string) =>
  mutate<{ steers?: number }>(`/api/queue/${itemId}/steer`,
                              { body: { text }, ok: `steered #${itemId}` });

/** One sentence to every agent running right now. Returns per-item results —
 *  a runner with no live channel refuses, and a broadcast that half landed must
 *  not report as a whole one. */
export const steerAll = (text: string) =>
  mutate<{ count?: number; refused_count?: number; refused?: { item_id: number }[] }>(
    "/api/queue/steer-all", { body: { text }, quiet: true });

/* ── brainstorm ──────────────────────────────────────────────────────────── */

export type BsMessage = { id: number; role: string; text: string };
export type BsSession = {
  id: number; title?: string; status?: string;
  messages?: BsMessage[]; thinker?: { live?: boolean; cost_usd?: number };
};

export const bsList = () =>
  readJSON<{ sessions: BsSession[] }>("/api/brainstorm?seat=director", { sessions: [] });

export const bsOpen = (id: number) =>
  readJSON<BsSession>(`/api/brainstorm/${id}`, { id });

export const bsCreate = (title: string) =>
  mutate<BsSession>("/api/brainstorm", { body: { seat: "director", title } });

export const bsSay = (id: number, text: string) =>
  mutate<{ reply?: string }>(`/api/brainstorm/${id}/message`, { body: { text } });

/** THE BUTTON THAT WENT MISSING. Stops the partner and clears the thread; the
 *  notes and the drawing are kept, which is why it is not a delete. */
export const bsReset = (id: number) =>
  mutate(`/api/brainstorm/${id}/reset`, { ok: "thread reset" });

export const bsSynthesize = (id: number) =>
  mutate<{ plan?: unknown }>(`/api/brainstorm/${id}/synthesize`, {});

export const bsDeploy = (id: number, plan: unknown) =>
  mutate(`/api/brainstorm/${id}/deploy`, { body: { plan }, ok: "filed" });
