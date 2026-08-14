import { mutate, readJSON } from "../../bridge";

/* The narrative seat's endpoints, in one file.
 *
 * The seat's mission is "own the lore graph, quests and dialogue, and run
 * canon_check on every write before it lands". All three have HTTP routes now:
 * bgate_ui/routes/world.py (lore + the gate), routes/quests.py (writes, and it
 * runs canon_check itself before anything lands), routes/dialogue.py (READ
 * ONLY, on purpose — dialogue_write is an MCP tool because a tree is a FILE in
 * a lane, and an HTTP write would be a second, quieter door to the same files
 * with neither the gate nor the lane around it).
 *
 * WHAT THE ENVELOPE COSTS US. readJSON() unwraps `{ok, data}` to `data` and
 * drops every sibling key, so `/api/lore`'s `graph` and its `page` never reach
 * this file. Two consequences, both handled rather than lived with:
 *
 *   · FACT COUNTS. The graph was the only place a per-entity fact count was
 *     reported, so the entity rows carried none. `/api/lore/facts` returns
 *     every fact carrying its entity's slug in ONE request — that is the count,
 *     measured rather than fabricated, and it is also the only way to answer
 *     "how much of this graph is locked" without 28 loopback reads.
 *   · TRUNCATION. `page.limit` defaults to 100 and the total is unreachable
 *     through the unwrap, so a 120-entity project would silently show 100 and
 *     the header would call it "120 entities" out of a list of 100. The list
 *     is asked for at MAX_LIMIT and the count below is the count of what
 *     actually arrived.
 */

/* ── lore ─────────────────────────────────────────────────────────────────── */

export type Entity = {
  id: number; kind: string; name: string; slug: string;
  summary: string; body: string; status: string;
  created_at?: string; updated_at?: string;
};

/** One atomic claim. `locked` is 0/1 out of SQLite, and locked is what turns a
 *  canon_check flag from "review" into a refusal — so it is drawn, not hidden. */
export type Fact = {
  id: number; entity_id: number; statement: string;
  source?: string; locked?: number; created_at?: string;
};

/** An edge touching this entity, in either direction, already resolved to a
 *  slug by lore.links_of. These ARE "what would break": the other entities
 *  whose meaning depends on this one. */
export type Link = {
  dir: "in" | "out"; rel: string; slug: string; name: string;
  kind: string; note?: string;
};

export type Brief = { entity: Entity | null; facts: Fact[]; links: Link[] };

export const EMPTY_BRIEF: Brief = { entity: null, facts: [], links: [] };

/** canon.check's flags. `level: "conflict"` is what refuses a write; "review"
 *  is the normal state of a first draft and must never read as an error. */
export type CanonFlag = {
  level: "conflict" | "review"; code: string; message?: string;
  entity?: string; name?: string; canon?: string; text?: string; fact_id?: number;
};

export type CanonVerdict = {
  verdict: "ok" | "review" | "conflict";
  flags: CanonFlag[];
  mentions: { slug: string; name: string; status: string }[];
  canon: { id: number; statement: string; locked: boolean }[];
};

/** `/api/lore` unwraps to a bare ARRAY, which does not satisfy the reader's
 *  object constraint — hence the cast, in one place rather than at the call
 *  site. A read failure comes back tagged rather than thrown, so the screen can
 *  say "could not read" instead of showing the empty state, which would be a
 *  lie about a project that has 28 entities.
 *
 *  `graph=false` because the graph is the expensive half of this route and this
 *  screen draws no node canvas — it was being built and thrown away on every
 *  20s poll. `limit` is the route's MAX_LIMIT; the default of 100 truncates
 *  without saying so, and `page` does not survive the unwrap. */
export async function loreList(): Promise<{ entities: Entity[]; error?: string }> {
  const body = await readJSON<Record<string, unknown>>(
    "/api/lore?graph=false&limit=500", {});
  const error = typeof body.__error === "string" ? body.__error : undefined;
  return { entities: Array.isArray(body) ? (body as Entity[]) : [], error };
}

/** One fact as `/api/lore/facts` reports it: the fact, plus the entity it
 *  belongs to. `slug` is the join key the rail counts on. */
export type FactRow = Fact & { slug: string; name: string; status: string };

/** Every fact in the project, one request. See the header: this is where the
 *  per-entity fact count comes from, and it is also the locked-fact census the
 *  head chip reports — locked is the set a write may not contradict at all. */
export async function loreAllFacts(): Promise<{ facts: FactRow[]; error?: string }> {
  const body = await readJSON<{ facts?: FactRow[]; __error?: string }>(
    "/api/lore/facts", {});
  return { facts: Array.isArray(body.facts) ? body.facts : [], error: body.__error };
}

/** Assert one atomic claim. THIS is the write canon_check exists to protect —
 *  world.py runs the gate over the statement and refuses a hard conflict with
 *  409 — so it is not quiet: a refusal here is the whole point of the route.
 *  `locked` is human-only server-side (require_human), which the dashboard
 *  caller is and an agent is not. */
export const loreAddFact = (ref: string, body: {
  statement: string; source?: string; locked?: boolean;
}) => mutate<Fact>(`/api/lore/${encodeURIComponent(ref)}/facts`,
                   { body, ok: "fact asserted" });

export const loreBrief = (ref: string) =>
  readJSON<Brief>(`/api/lore/${encodeURIComponent(ref)}`, EMPTY_BRIEF);

/** The gate, run on demand. Quiet: the verdict is rendered inline, and a
 *  "review" toast on every keystroke-sized save would train the seat to ignore
 *  the one that matters. */
export const canonCheck = (text: string, entities?: string[]) =>
  mutate<CanonVerdict>("/api/canon/check", {
    body: { text, ...(entities?.length ? { entities } : {}) }, quiet: true,
  });

/** PATCH runs the same gate server-side and answers 409 on a hard conflict, so
 *  a save that skipped the button above still cannot land a contradiction.
 *  `summary` travels with `body` because the route accepts both and the panel
 *  edits both — sending only the body was silently discarding the summary the
 *  writer had just typed. */
export const loreSave = (ref: string,
                         fields: { summary?: string; body?: string; status?: string }) =>
  mutate<Entity>(`/api/lore/${encodeURIComponent(ref)}`, {
    method: "PATCH", body: fields, quiet: true,
  });

/** Moving an entity's status is the highest-consequence act on this screen and
 *  the only one canon_check itself reads: `retired` turns every later mention
 *  into a hard conflict, `canon` is what "settled" means. NOT quiet — the
 *  server refuses this for an agent actor (require_human) and a silent refusal
 *  would look exactly like a status that moved. */
export const loreSetStatus = (ref: string, status: string) =>
  mutate<Entity>(`/api/lore/${encodeURIComponent(ref)}`, {
    method: "PATCH", body: { status }, ok: `now ${status}`,
  });

/* ── dialogue (read-only over HTTP, on purpose) ───────────────────────────── */

export type Choice = { text: string; goto: string; tag?: string; condition?: string };

export type DNode = {
  id: string; speaker?: string; text?: string; end?: boolean;
  choices?: Choice[]; tags?: string[]; note?: string;
};

/** One tree as dialogue.read() returns it. */
export type Tree = {
  name: string; title?: string; summary?: string;
  start?: string; ends?: string[]; nodes: DNode[];
  rel_path?: string; res_path?: string;
};

/** One row of dialogue.list_dialogues(): it already re-validates every file and
 *  reports `ok:false` with the reason, which is the cheapest place a broken
 *  tree is ever noticed. */
export type TreeRow = {
  name: string; title?: string; start?: string; nodes?: number;
  ok?: boolean; error?: string; rel_path?: string;
};

export const EMPTY_TREE: Tree = { name: "", nodes: [] };

export async function dialogueList(): Promise<{ trees: TreeRow[]; error?: string }> {
  const body = await readJSON<Record<string, unknown>>("/api/dialogue", {});
  const error = typeof body.__error === "string" ? body.__error : undefined;
  /* `readJSON` unwraps the envelope to `data`, which is an OBJECT with a
     `dialogues` list — not a bare array. Reading it as an array meant the list
     was permanently empty and the empty state permanently wrong: a project with
     twenty trees was told it had none. */
  const trees = Array.isArray((body as { dialogues?: unknown }).dialogues)
    ? ((body as { dialogues: TreeRow[] }).dialogues) : [];
  return { trees, error };
}

export const dialogueRead = (name: string) =>
  readJSON<Tree>(`/api/dialogue/${encodeURIComponent(name)}`, EMPTY_TREE);

/** THE WRITER'S OWN VERDICT, without writing. `dialogue.validate` is what
 *  refuses `dialogue_write`, and it refuses for more than the three shape
 *  checks graph.ts draws: a duplicate id, an over-long id, a choice with no
 *  text or no goto, a `start` naming no node, a tree with no ending at all.
 *  Computing three of those on the client and printing "the checks pass" is how
 *  a green screen turns into a refused write, so the strip asks the authority
 *  and says which answer it is showing.
 *
 *  `ok:false` here is DATA, not a failed request — routes/dialogue.py catches
 *  DialogueError precisely so the broken trees this exists to describe do not
 *  500 the endpoint. */
export type DValidate = {
  ok: boolean; problem: string; start?: string; ends?: string[];
  /** Set when the ROUTE failed (404, no such build) rather than when the tree
   *  failed. The two must not be collapsed: "unreachable" is not "clean". */
  __error?: string;
};

export const dialogueValidate = (name: string) =>
  readJSON<DValidate>(`/api/dialogue/${encodeURIComponent(name)}/validate`,
                      { ok: false, problem: "" });

/* ── quests ───────────────────────────────────────────────────────────────── */

/** One step. `done_when` is the observable that closes it and is never blank —
 *  bgate_core/quests.py refuses the write — so the panel can draw it as a
 *  second line rather than as an optional field. */
export type Step = {
  id: number; quest_id: number; ord: number; text: string;
  done_when: string; optional?: number;
};

/** The three shape failures, each naming its step. Same contract as dialogue's
 *  problems, deliberately: one vocabulary for "this does not hold together". */
export type QuestProblem = {
  kind: "no-steps" | "all-optional" | "broken-order";
  step: number | null; text: string;
};

export type Quest = {
  id: number; slug: string; title: string; premise: string; reward: string;
  state: string; giver_id?: number | null;
  giver?: { slug: string; name: string; kind: string; status: string } | null;
  giver_slug?: string | null; giver_name?: string | null;
  steps: Step[]; ok: boolean; problems: QuestProblem[];
  created_at?: string; updated_at?: string;
};

export type Giver = { slug: string; name: string; kind: string; status: string };

export type QuestBrief = { quests: Quest[]; givers: Giver[]; states: string[] };

export const EMPTY_QUESTS: QuestBrief = { quests: [], givers: [], states: [] };

export const questList = () =>
  readJSON<QuestBrief>("/api/quests", EMPTY_QUESTS);

/** NOT quiet: writing a quest is a deliberate act with a server-side
 *  canon_check behind it, and a refusal ("the wizard is retired") is the one
 *  thing the writer must not miss. */
export const questAdd = (body: {
  title: string; premise?: string; reward?: string; giver?: string;
  steps: { text: string; done_when: string; optional?: boolean }[];
}) => mutate<{ quest: Quest; canon: CanonVerdict }>("/api/quests", { body });

/* THE VERDICT COMES BACK AND MUST BE DRAWN. Both writers above return
   `{quest, canon}`; the conflict level never arrives here at all (it is a 400
   and lands in `error`), so the `canon` that DOES arrive is the review level —
   "the wizard is still draft", "Brennan has no entity in the lore graph". That
   is the whole information content of a successful narrative write, and
   dropping it made the panel's own comment ("a review flag lands and is
   reported") false. */

export const questAddStep = (ref: string, body: {
  text: string; done_when: string; optional?: boolean;
}) => mutate<{ quest: Quest; canon: CanonVerdict }>(
  `/api/quests/${encodeURIComponent(ref)}/steps`, { body });

export const questCutStep = (stepId: number) =>
  mutate(`/api/quests/steps/${stepId}`, { method: "DELETE", ok: "step cut" });

/** STATE ONLY, DELIBERATELY, UNTIL THE ROUTE GROWS A GATE.
 *
 *  PATCH /api/quests/{ref} accepts `premise`, `reward`, `giver` and `state`,
 *  and — unlike POST /api/quests and POST .../steps, which both run
 *  canon.check before they write — it runs NO canon check at all
 *  (bgate_ui/routes/quests.py:132). `premise` and `reward` are prose: a premise
 *  naming a retired character is refused on create and accepted on edit, so the
 *  gate is one PATCH away from being a formality on the exact field it was
 *  written for.
 *
 *  So this screen only ever sends `state`, which is a four-value enum with
 *  nothing for canon to read, and the prose fields stay create-only until the
 *  route gates them. Wiring an editor to the ungated door would have been the
 *  faster change and the wrong one. Not quiet: a refused state move that says
 *  nothing looks identical to one that landed. */
export const questSetState = (ref: string, state: string) =>
  mutate<Quest>(`/api/quests/${encodeURIComponent(ref)}`,
                { method: "PATCH", body: { state }, ok: `state → ${state}` });
