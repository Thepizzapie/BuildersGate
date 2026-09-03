import { mutate, readJSON, type MutateResult } from "../../bridge";

/* The World view's endpoints — the design bible (routes/world.py), the lore
 * graph and its canon gate (same file), and the reference anchors hung off
 * bible sections (routes/refs.py).
 *
 * Two envelope shapes meet here. The world routes answer `{ok, data}` and
 * readJSON() unwraps them; the refs routes answer a bare payload and readJSON()
 * hands that back untouched. `/api/lore?graph=true` is the one read that
 * needs a sibling of `data` (`graph` rides beside it, not inside it), so it
 * is fetched raw below rather than through the unwrapping reader. */

/* ── the bible ────────────────────────────────────────────────────────────── */

export type Section = {
  id: number; kind: string; title: string; body: string; rank: number;
  /** Content version — sent back on PATCH so a stale editor cannot erase
   *  somebody else's edit (bible.StaleWrite → 409). */
  version?: string;
};

export type Bible = {
  pillars: Section[]; loop: Section[]; constraints: Section[]; references: Section[];
  sections: Section[]; kinds: string[];
};

export const EMPTY_BIBLE: Bible = {
  pillars: [], loop: [], constraints: [], references: [], sections: [], kinds: [],
};

export const bibleRead = () => readJSON<Bible>("/api/bible", EMPTY_BIBLE);

export const bibleAdd = (kind: string, title: string, rank: number) =>
  mutate<Section>("/api/bible", { body: { kind, title, rank } });

export const bibleUpdate = (id: number, fields: {
  title?: string; body?: string; version?: string;
}) => mutate<Section>(`/api/bible/${id}`, { method: "PATCH", body: fields });

export const bibleRemove = (id: number) =>
  mutate(`/api/bible/${id}`, { method: "DELETE" });

export const bibleReorder = (kind: string, order: number[]) =>
  mutate("/api/bible/reorder", { body: { kind, order } });

/* ── lore ─────────────────────────────────────────────────────────────────── */

export type Entity = {
  id: number; kind: string; name: string; slug: string;
  summary: string; body: string; status: string;
};

export type LoreNode = {
  id: string; kind: string; status: string; title: string; summary?: string;
  facts?: number; w?: number; x: number; y: number; glyph?: string;
  ports?: { in: { id: string; label: string }[]; out: { id: string; label: string }[] };
};

export type LoreEdge = { id?: number; from: [string, string]; to: [string, string]; rel?: string };

export type LoreGraph = {
  nodes: LoreNode[]; edges: LoreEdge[]; kinds: string[]; statuses: string[];
};

export type Lore = { entities: Entity[]; graph: LoreGraph };

export type Fact = { id: number; statement: string; locked?: number | boolean };
export type Link = { dir: "in" | "out"; rel: string; slug: string; name: string };
export type Brief = { entity: Entity; facts: Fact[]; links: Link[] };

/** The one read that needs the envelope's siblings. Mirrors readJSON's failure
 *  contract — a value, never a throw. */
export async function loreRead(params: { kind?: string; status?: string }):
  Promise<{ lore: Lore | null; error?: string }> {
  const q = ["graph=true", "limit=500"];
  if (params.kind) q.push(`kind=${encodeURIComponent(params.kind)}`);
  if (params.status) q.push(`status=${encodeURIComponent(params.status)}`);
  let body: Record<string, unknown> | null = null;
  try {
    const r = await fetch(`/api/lore?${q.join("&")}`);
    body = await r.json();
    if (!r.ok || !body || body.ok === false) {
      const e = body && (body.error as { message?: string } | undefined);
      return { lore: null, error: e?.message || `request failed · ${r.status}` };
    }
  } catch {
    return { lore: null, error: "backend unreachable" };
  }
  const graph = (body.graph as Partial<LoreGraph>) || {};
  return {
    lore: {
      entities: Array.isArray(body.data) ? (body.data as Entity[]) : [],
      graph: {
        nodes: graph.nodes || [], edges: graph.edges || [],
        kinds: graph.kinds || [], statuses: graph.statuses || [],
      },
    },
  };
}

export const loreBrief = (slug: string) =>
  readJSON<Brief | Record<string, never>>(`/api/lore/${encodeURIComponent(slug)}`, {});

/* THE CANON GATE. Every narrative write below is `quiet`: a 409 carrying flags
   is not an error to toast, it is the feature — the caller opens the flags and
   offers the override to a human. The flags live on `body.error.detail`,
   which mutate() keeps on a refusal precisely so a panel can read them. */

export type CanonFlag = {
  level: "conflict" | "review"; code: string; message?: string;
  entity?: string; canon?: string; text?: string;
};

export type CanonRefusal = {
  message: string;
  detail: { flags?: CanonFlag[]; verdict?: string };
};

/** The canon refusal inside a failed mutate, or null when the failure was
 *  anything else (offline, 400, a stale write). */
export function canonRefusal(res: MutateResult): CanonRefusal | null {
  if (res.status !== 409) return null;
  const err = (res.body as { error?: { message?: string; detail?: CanonRefusal["detail"] } } | null)?.error;
  const detail = err?.detail || {};
  if (!detail.flags && !detail.verdict) return null;
  return { message: err?.message || res.error || "this breaks canon", detail };
}

export const loreAdd = (body: { kind: string; name: string; summary: string; override: boolean }) =>
  mutate<Entity>("/api/lore", { body, quiet: true });

export const loreSave = (slug: string, body: { summary: string; body: string; override: boolean }) =>
  mutate<Entity>(`/api/lore/${encodeURIComponent(slug)}`, { method: "PATCH", body, quiet: true });

export const loreSetStatus = (slug: string, status: string) =>
  mutate<Entity>(`/api/lore/${encodeURIComponent(slug)}`, { method: "PATCH", body: { status } });

export const loreAddFact = (slug: string, body: { statement: string; locked: boolean; override: boolean }) =>
  mutate<Fact>(`/api/lore/${encodeURIComponent(slug)}/facts`, { body, quiet: true });

export const loreLink = (src: string, dst: string, rel: string) =>
  mutate("/api/lore/link", { body: { src, dst, rel } });

/* ── reference anchors ────────────────────────────────────────────────────── */

export type Pin = { name: string; kind: string; path?: string };

export type Anchor = {
  id?: number; section_id?: number; ref: string; kind?: string; note?: string;
  resolved_path?: string | null; exists?: boolean;
};

export type Suggestion = {
  section_id: number; title: string; propose?: string[]; unresolved?: string[];
};

export type RefsState = {
  pins: Pin[]; bySection: Record<string, Anchor[]>; suggestions: Suggestion[];
};

/** Three reads, one answer. The suggest read failing is not the panel failing
 *  — it is the rescue strip being empty. */
export async function refsRead(): Promise<{ state: RefsState; error?: string }> {
  const [pins, anchors, suggested] = await Promise.all([
    readJSON<{ refs?: Pin[] }>("/api/refs", {}),
    readJSON<{ by_section?: Record<string, Anchor[]> }>("/api/bible/refs", {}),
    readJSON<{ suggestions?: Suggestion[] }>("/api/bible/refs/suggest", {}),
  ]);
  let error = pins.__error || anchors.__error;
  /* The page is served from disk and the API from a process that started
     before this feature existed. Say that, rather than showing a raw 404 that
     reads as "your anchors are gone". */
  if (anchors.__error && /not found/i.test(anchors.__error)) {
    error = "the running dashboard predates the anchors API - restart bgate serve to pick it up";
  }
  return {
    state: {
      pins: pins.refs || [],
      bySection: anchors.by_section || {},
      suggestions: suggested.suggestions || [],
    },
    error,
  };
}

/* Quiet: the attach bar reports its own outcome inline, the way the classic
   panel did, so a refusal is read next to the control that caused it. */
export const refAttach = (sectionId: number | string, ref: string, kind: string) =>
  mutate(`/api/bible/${encodeURIComponent(String(sectionId))}/refs`,
         { body: { ref, kind }, quiet: true });

export const refDetach = (sectionId: number | string, ref: string) =>
  mutate(`/api/bible/${encodeURIComponent(String(sectionId))}/refs?ref=${encodeURIComponent(ref)}`,
         { method: "DELETE", quiet: true });

/** Upload goes as base64 in a JSON body: the dashboard takes no new
 *  dependencies and FastAPI needs python-multipart for a real form post. */
export const refUpload = (sectionId: number | string, body: { name: string; data: string; kind: string }) =>
  mutate(`/api/bible/${encodeURIComponent(String(sectionId))}/refs/upload`,
         { body, quiet: true });

/** A pin's stored path is absolute and /api/preview only serves root-relative
 *  paths. Cutting everything ahead of .bgate is the rule the seat workspaces
 *  already use. */
export const relRef = (p: string | null | undefined) => !p ? ""
  : String(p).replace(/^.*[\\/](?=\.bgate[\\/])/, "").replace(/\\/g, "/");

/* ── the page's dialog, with the multi-field form the classic view used ───── */

export type AskField = {
  name: string; label?: string; type?: "text" | "select" | "textarea";
  required?: boolean; value?: string; placeholder?: string;
  options?: { value: string; label: string }[];
};

type AskOpts = {
  title?: string; body?: string; label?: string; placeholder?: string;
  ok?: string; multiline?: boolean; required?: boolean; fields?: AskField[];
};

/** ask.js's askText takes `fields` for a one-card form and answers a record;
 *  bridge.ts declares only the single-string shape, so the wider call goes
 *  through a cast here rather than a second global declaration. Null is a
 *  cancel, and no dialog on the page is a cancel too — never a submit. */
export async function ask(opts: AskOpts & { fields: AskField[] }): Promise<Record<string, string> | null>;
export async function ask(opts: AskOpts): Promise<string | null>;
export async function ask(opts: AskOpts): Promise<string | Record<string, string> | null> {
  const fn = window.askText as unknown as
    ((o: AskOpts) => Promise<string | Record<string, string> | null>) | undefined;
  if (!fn) return null;
  return fn(opts);
}
