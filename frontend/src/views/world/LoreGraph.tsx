import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Modal } from "@mantine/core";
import { Icon } from "../../components/Icon";
import { toast, type MutateResult } from "../../bridge";
import { useEvents } from "../../hooks";
import {
  ask, canonRefusal, loreAdd, loreAddFact, loreBrief, loreLink, loreRead,
  loreSave, loreSetStatus,
  type Brief, type CanonRefusal, type Lore, type LoreNode,
} from "./api";
import { layoutLore } from "./layout";
import { measure, proseBlocks } from "./prose";

/* The lore graph — the canon, re-laid out here by how it is wired, rendered
 * with NodeCanvas (frontend/public/nodecanvas.js, a classic script the page
 * loads before this module).
 *
 * One refusal is surfaced rather than swallowed, because it is the feature: a
 * 409 from a lore write means the prose breaks canon; the flags are shown and
 * a human may override. An agent may not. */

/* NodeCanvas is a classic script on the page; the surface this view uses. */
type NodeCanvasInstance = {
  mount(): NodeCanvasInstance;
  fit(opts?: { min?: number }): void;
  destroy(): void;
  select(id: string | null): void;
};
type NodeCanvasCtor = new (host: HTMLElement, opts: {
  nodes: LoreNode[]; edges: unknown[];
  renderBody(node: LoreNode): string;
  onSelect(node: LoreNode | null): void;
  onNodeMove(node: LoreNode | null): void;
  onConnect(from: [string, string], to: [string, string]): void;
  accent?: string;
}) => NodeCanvasInstance;

declare global {
  interface Window { NodeCanvas?: NodeCanvasCtor }
}

const STATUS_COLOR: Record<string, string> = {
  canon: "var(--good)", draft: "var(--warn)", retired: "var(--bad)",
};
const LORE_GLYPH: Record<string, string> = {
  faction: "⚑", character: "☗", place: "⌂", event: "✦", item: "◈",
  concept: "◍", species: "❖",
};
const STATUSES = ["draft", "canon", "retired"];
const LONG_BODY = 700;   // past this, 340px is not a reading measure
/* The graph refreshes on the bus's fallback (no event kind describes a lore
   write) — and a refetch that changes nothing does not rebuild the canvas,
   so pan, zoom and the open entity survive it. */
const REFRESH_MS = 30000;

const E = (s: unknown) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string));

type Filter = { kind: string; status: string; q: string };

export function LoreGraph({ active }: { active: boolean }) {
  const canvasHost = useRef<HTMLDivElement>(null);
  const canvas = useRef<NodeCanvasInstance | null>(null);
  /* Nodes the reader has dragged this session. A refetch rebuilds the canvas
     after every write, so without this, saving a summary would throw away the
     arrangement they had just made. */
  const moved = useRef(new Map<string, { x: number; y: number }>());
  const [lore, setLore] = useState<Lore | null>(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<Filter>({ kind: "", status: "", q: "" });
  const [selected, setSelected] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<{ error: CanonRefusal; retry: (override: boolean) => void } | null>(null);
  const filterRef = useRef(filter);
  filterRef.current = filter;
  const signature = useRef("");

  const refresh = useCallback(async () => {
    // kind=/status= narrow the FIRST load only; every later fetch takes the
    // whole set, because filtering is client-side and the facet counts are
    // derived from what is in memory.
    const first = !signature.current;
    const f = filterRef.current;
    const res = await loreRead(first ? { kind: f.kind, status: f.status } : {});
    if (!res.lore) { setError(res.error || "could not read the canon"); return; }
    setError("");
    const sig = JSON.stringify(res.lore);
    if (sig === signature.current) return;
    signature.current = sig;
    setLore(res.lore);
  }, []);
  useEvents(refresh, { enabled: active, kinds: [], fallbackMs: REFRESH_MS });

  /* ---- the canvas ---------------------------------------------------------- */
  const linkEntities = useCallback(async (src: string, dst: string) => {
    const rel = await ask({
      title: "Link entities", body: `How does "${src}" relate to "${dst}"?`,
      label: "Relationship", placeholder: "rules, betrayed, lives-in",
      multiline: false, required: true, ok: "link",
    });
    if (!rel || !rel.trim()) { signature.current = ""; return refresh(); }   // undo the optimistic edge
    const res = await loreLink(src, dst, rel.trim());
    signature.current = "";
    if (!res.ok) return refresh();
    toast("linked", "ok");
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const hostEl = canvasHost.current;
    if (!hostEl || !lore) return;
    const g = lore.graph;
    if (!g.nodes.length || !window.NodeCanvas) return;
    // The payload arrives in NodeCanvas's own shape; the glyph and the
    // placement are ours to pick. Copied first: layout mutates x/y.
    const nodes = g.nodes.map((n) => ({ ...n, glyph: LORE_GLYPH[n.kind] || "◆" }));
    layoutLore(nodes, g.edges, moved.current);
    const nc = new window.NodeCanvas(hostEl, {
      nodes, edges: g.edges,
      renderBody: (node) => `
        <div class="wl-node-meta">
          <span class="wl-pill" style="--p:${STATUS_COLOR[node.status] || "var(--text-3)"}">${E(node.status)}</span>
          <span class="wl-node-kind">${E(node.kind)}</span>
          ${node.facts ? `<span class="wl-node-kind">${node.facts} fact${node.facts === 1 ? "" : "s"}</span>` : ""}
        </div>
        <div class="wl-node-sum">${E(node.summary || "no summary yet")}</div>`,
      onSelect: (node) => setSelected(node ? node.id : null),
      // deleting an edge reports a move of nobody, so the null is real
      onNodeMove: (node) => { if (node) moved.current.set(node.id, { x: node.x, y: node.y }); },
      onConnect: (from, to) => void linkEntities(from[0], to[0]),
      accent: "var(--accent)",
    }).mount();
    nc.fit();
    canvas.current = nc;
    return () => { nc.destroy(); canvas.current = null; };
  }, [lore, linkEntities]);

  /* ---- searching and faceting the canon ----------------------------------- *
   * All entities arrive on the first request WITH their bodies, so narrowing
   * them is a filter over memory. Filtering repaints, it does not refetch: one
   * class per node and the chip counts, so pan, zoom and the entity you were
   * reading all stay put. */
  const entities = lore?.entities || [];
  const needle = filter.q.trim().toLowerCase();
  const filtering = !!(filter.kind || filter.status || needle);
  const hit = (e: { name?: string; summary?: string; body?: string }) => !needle
    || `${e.name || ""}\n${e.summary || ""}\n${e.body || ""}`.toLowerCase().includes(needle);

  const visible = useMemo(() => {
    const out = new Set<string>();
    entities.forEach((e) => {
      if (filter.kind && e.kind !== filter.kind) return;
      if (filter.status && e.status !== filter.status) return;
      if (hit(e)) out.add(e.slug);
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entities, filter]);

  /* A chip counts what picking it would leave, so the search and the OTHER
     facet apply and its own does not. */
  const facetCounts = (field: "kind" | "status") => {
    const other = field === "kind" ? "status" : "kind";
    const n: Record<string, number> = {};
    entities.forEach((e) => {
      if (filter[other] && e[other] !== filter[other]) return;
      if (hit(e)) n[e[field]] = (n[e[field]] || 0) + 1;
    });
    return n;
  };

  useEffect(() => {
    const hostEl = canvasHost.current;
    if (!hostEl) return;
    hostEl.classList.toggle("wl-filtering", filtering);
    hostEl.querySelectorAll<HTMLElement>(".nc-node").forEach((el) =>
      el.classList.toggle("wl-dim", filtering && !visible.has(el.dataset.node || "")));
  }, [filtering, visible, lore]);

  const setFacet = (field: "kind" | "status", value: string) =>
    // clicking the live chip again clears that facet — "all kinds" has to
    // stay reachable
    setFilter((f) => ({ ...f, [field]: f[field] === value ? "" : value }));
  const clearFilter = () => setFilter({ kind: "", status: "", q: "" });

  const facetRow = (field: "kind" | "status", label: string) => {
    if (!lore) return null;
    // Only the values actually present get a chip: offering "faction" to a
    // project with no factions is noise.
    const order = (field === "kind" ? lore.graph.kinds : lore.graph.statuses) || [];
    const present = order.filter((v) => entities.some((e) => e[field] === v));
    if (present.length < 2) return null;
    const counts = facetCounts(field);
    return (
      <div className="wl-facet" data-facet={field}>
        <span className="wl-facet-l sec-sub"><Icon name={field === "kind" ? "world" : "verify"} size={12} />{label}</span>
        {present.map((v) => (
          <button key={v}
                  className={`afilter${filter[field] === v ? " active" : ""}${!counts[v] && filter[field] !== v ? " wl-none" : ""}`}
                  onClick={() => setFacet(field, v)}>
            {field === "kind" ? `${LORE_GLYPH[v] || "◆"} ` : ""}{v}
            <span className="n">{counts[v] || 0}</span>
          </button>
        ))}
      </div>
    );
  };

  /* ---- the canon gate ------------------------------------------------------ *
   * POST /api/lore and the fact/patch routes 409 with the conflict flags and do
   * NOT write. Showing the flags and offering the override to a human is the
   * whole contract — a UI that retried silently would put the old formality
   * straight back. Every canon-gated write funnels through here so the
   * override path is identical to the first attempt, minus the flag. */
  const gated = async (run: (override: boolean) => Promise<MutateResult>): Promise<MutateResult> => {
    const res = await run(false);
    if (res.ok) return res;
    const canon = canonRefusal(res);
    if (canon) {
      setRefusal({
        error: canon,
        retry: async (override) => {
          const again = await run(override);
          if (!again.ok) { toast(again.error || "the write was refused", "warn"); return; }
          toast("written over the canon flag", "ok");
          signature.current = "";
          void refresh();
        },
      });
      return res;
    }
    toast(res.error || "the write was refused", "warn");
    return res;
  };

  const addEntity = async () => {
    // One form, not three questions: the three fields make ONE entity, and the
    // kind is a closed set — typed free-hand it reached the server misspelled
    // and opened a category of one.
    const kinds = lore?.graph.kinds.length ? lore.graph.kinds : Object.keys(LORE_GLYPH);
    const out = await ask({
      title: "New entity", ok: "create",
      fields: [
        { name: "name", label: "Name", type: "text", required: true },
        {
          name: "kind", label: "Kind", type: "select", required: true,
          value: kinds.includes("concept") ? "concept" : kinds[0],
          options: kinds.map((k) => ({ value: k, label: `${LORE_GLYPH[k] || "◆"} ${k}` })),
        },
        { name: "summary", label: "One-line summary", type: "text" },
      ],
    });
    if (!out || !out.name.trim()) return;
    const res = await gated((override) => loreAdd({
      kind: out.kind, name: out.name.trim(), summary: (out.summary || "").trim(), override,
    }));
    if (res.ok) {
      const slug = (res.data as { slug?: string } | null)?.slug;
      if (slug) setSelected(slug);
      signature.current = "";
      void refresh();
    }
  };

  const count = lore ? (filtering
    ? `${visible.size} of ${entities.length} entities match`
    : `${entities.length} entities · ${lore.graph.edges.length} links - drag a port to link two of them`) : "";

  return (
    <div>
      <div className="wl-toolbar">
        <input className="asset-search" type="search" spellCheck={false}
               placeholder="search names, summaries and prose…" value={filter.q}
               aria-label="Search the canon"
               onChange={(e) => setFilter({ ...filter, q: e.currentTarget.value })}
               onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); clearFilter(); } }} />
        <span className="wl-note">{count}</span>
        {filtering && <button className="qbtn small ghost" onClick={clearFilter}>clear</button>}
        <button className="qbtn small ghost" onClick={() => void addEntity()}>＋ entity</button>
      </div>
      <div className="wl-facets">{facetRow("kind", "kind")}{facetRow("status", "canon")}</div>
      {!lore && !error && <div className="empty">reading canon…</div>}
      {error && <div className="empty err">{error}</div>}
      {/* A 3.8k-character body has no measure at 340px, so the box widens
          (.reading) when there is prose to take it for — the entity panel
          below sets that on its parent. */}
      {lore && (
        <div className="wl-graph">
          <div className="wl-canvas" ref={canvasHost}>
            {!lore.graph.nodes.length && (
              <div className="empty">no lore yet - the first entity is usually the place the game happens in</div>
            )}
          </div>
          <EntityPanel slug={selected} gated={gated}
                       onWrote={() => { signature.current = ""; void refresh(); }} />
        </div>
      )}
      <Modal opened={!!refusal} onClose={() => setRefusal(null)} title="This breaks canon" size="lg" centered>
        {refusal && <CanonFlags refusal={refusal.error}
                                onOverride={() => { const r = refusal.retry; setRefusal(null); r(true); }}
                                onCancel={() => setRefusal(null)} />}
      </Modal>
    </div>
  );
}

function CanonFlags({ refusal, onOverride, onCancel }: {
  refusal: CanonRefusal; onOverride: () => void; onCancel: () => void;
}) {
  const flags = refusal.detail.flags || [];
  const conflicts = flags.filter((f) => f.level === "conflict");
  const reviews = flags.filter((f) => f.level !== "conflict");
  const row = (f: typeof flags[number], i: number) => (
    <div className={`wl-flag ${f.level}`} key={i}>
      <div className="wl-flag-h"><b>{f.code}</b>{f.entity ? ` · ${f.entity}` : ""}</div>
      <div>{f.message}</div>
      {f.canon && <div className="wl-flag-q">canon: {f.canon}</div>}
      {f.text && <div className="wl-flag-q">yours: {f.text}</div>}
    </div>
  );
  return (
    <div>
      <p className="wl-note">{refusal.message} Nothing was written.</p>
      {conflicts.map(row)}
      {reviews.length > 0 && <><div className="wl-l">also worth a look</div>{reviews.map(row)}</>}
      <div className="wl-choice">
        <button className="qbtn small" onClick={onOverride}>override — I know, write it anyway</button>
        <button className="qbtn small ghost" onClick={onCancel}>cancel, I'll fix the text</button>
      </div>
    </div>
  );
}

/* ---- reading an entity -------------------------------------------------------
 * A lore body is a design document: ALL-CAPS-colon headings, hard-wrapped
 * paragraphs and "- " bullets. The panel reads first and renders the structure
 * the prose already has; editing is a mode you ask for. Mode and the cached
 * payload survive re-renders, so toggling read/edit is not a refetch. */
function EntityPanel({ slug, gated, onWrote }: {
  slug: string | null;
  gated: (run: (override: boolean) => Promise<MutateResult>) => Promise<MutateResult>;
  onWrote: () => void;
}) {
  const [data, setData] = useState<Brief | null>(null);
  const [error, setError] = useState("");
  const [mode, setMode] = useState<"read" | "edit">("read");
  const [summary, setSummary] = useState("");
  const [body, setBody] = useState("");
  const [fact, setFact] = useState("");
  const [lock, setLock] = useState(false);
  const panel = useRef<HTMLElement>(null);

  const load = useCallback(async () => {
    if (!slug) { setData(null); return; }
    const b = await loreBrief(slug);
    if (b.__error) { setError(b.__error); setData(null); return; }
    setError("");
    setData(("entity" in b && b.entity) ? (b as Brief) : null);
  }, [slug]);
  useEffect(() => { setMode("read"); void load(); }, [load]);

  /* The graph box widens for prose. Reached through the DOM because the box
     is the parent, and a class there is the whole of what changes. */
  useEffect(() => {
    const box = panel.current?.closest(".wl-graph");
    const long = !!data && String(data.entity.body || "").length > LONG_BODY;
    box?.classList.toggle("reading", long);
  }, [data]);

  const save = async () => {
    if (!slug) return;
    const res = await gated((override) => loreSave(slug, { summary, body, override }));
    if (res.ok) { toast("saved", "ok"); setMode("read"); void load(); onWrote(); }
  };
  const setStatus = async (status: string) => {
    if (!slug) return;
    const res = await loreSetStatus(slug, status);
    if (!res.ok) return load();
    void load(); onWrote();
  };
  const addFact = async () => {
    const statement = fact.trim();
    if (!slug || !statement) return;
    const res = await gated((override) => loreAddFact(slug, { statement, locked: lock, override }));
    if (res.ok) { setFact(""); void load(); onWrote(); }
  };

  if (!slug) return <aside className="spanel k-doc wl-entity"><div className="empty">pick a node</div></aside>;
  if (error) return <aside className="spanel k-doc wl-entity"><div className="empty err">{error}</div></aside>;
  if (!data) return <aside className="spanel k-doc wl-entity"><div className="empty">loading…</div></aside>;

  const { entity, facts, links } = data;
  const text = String(entity.body || "");
  const m = measure(text);
  const editing = mode === "edit";

  return (
    <aside className="spanel k-doc wl-entity" ref={panel} data-slug={entity.slug}>
      <div className="wl-ehead">
        <div>
          <div className="wl-ekind">{entity.kind}</div>
          <h3>{entity.name}</h3>
        </div>
        <select className="wl-estatus" value={entity.status} aria-label="Canon status"
                onChange={(e) => void setStatus(e.currentTarget.value)}>
          {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          {!STATUSES.includes(entity.status) && <option value={entity.status}>{entity.status}</option>}
        </select>
      </div>
      <div className="wl-ebar">
        <span className="wl-emeas" title={m.full}>{m.short}</span>
        <button className="qbtn small ghost" onClick={() => {
          if (!editing) { setSummary(entity.summary || ""); setBody(text); }
          setMode(editing ? "read" : "edit");
        }}>{editing ? "done editing" : "edit prose"}</button>
      </div>
      {editing ? (
        <>
          <label className="wl-l">Summary</label>
          <textarea rows={2} placeholder="one line" value={summary} aria-label="Summary"
                    onChange={(e) => setSummary(e.currentTarget.value)} />
          <label className="wl-l">Body</label>
          <textarea rows={18} placeholder="the prose a narrative agent reads" value={body} aria-label="Body"
                    onChange={(e) => setBody(e.currentTarget.value)} />
          <button className="qbtn small" onClick={() => void save()}>save prose</button>
        </>
      ) : (
        <>
          {entity.summary && <p className="wl-elead">{entity.summary}</p>}
          <div className="wl-read"><Prose body={text} /></div>
        </>
      )}

      <label className="wl-l">Canon facts <span className="wl-n">{facts.length}</span></label>
      <p className="wl-note">One checkable statement each. This is what refuses a contradicting write.</p>
      <div className="wl-list">
        {facts.length ? facts.map((f) => (
          <div className={`wl-fact${f.locked ? " locked" : ""}`} key={f.id}>
            <span>{f.statement}</span>
            {f.locked ? <span className="wl-badge">locked</span> : null}
          </div>
        )) : <div className="empty">no facts — nothing here can be contradicted yet</div>}
      </div>
      <div className="wl-factadd">
        <input type="text" placeholder="assert one fact…" maxLength={300} value={fact} aria-label="New fact"
               onChange={(e) => setFact(e.currentTarget.value)}
               onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void addFact(); } }} />
        <label className="wl-check"><input type="checkbox" checked={lock} onChange={(e) => setLock(e.currentTarget.checked)} /> lock</label>
        <button className="qbtn small" onClick={() => void addFact()}>assert</button>
      </div>

      <label className="wl-l">Links <span className="wl-n">{links.length}</span></label>
      <div className="wl-list">
        {links.length ? links.map((l, i) => (
          <div className="wl-fact" key={`${l.dir}${l.slug}${l.rel}${i}`}>
            <span>{l.dir === "out" ? "→" : "←"} <b>{l.rel}</b> · {l.name}</span>
          </div>
        )) : <div className="empty">no links - drag between node ports</div>}
      </div>
    </aside>
  );
}

function Prose({ body }: { body: string }) {
  const blocks = proseBlocks(body);
  if (!blocks.length) return <div className="empty">no prose yet - the narrative seats read this</div>;
  // Consecutive bullets share one list.
  const out: React.ReactNode[] = [];
  let items: string[] = [];
  const flush = () => {
    if (!items.length) return;
    out.push(<ul className="wl-rl" key={`ul${out.length}`}>{items.map((t, i) => <li key={i}>{t}</li>)}</ul>);
    items = [];
  };
  blocks.forEach((b, i) => {
    if (b.t === "li") { items.push(b.text); return; }
    flush();
    if (b.t === "h") out.push(<h4 className="wl-rh" key={i}>{b.text}</h4>);
    else if (b.t === "f") out.push(<div className="wl-rf" key={i}><span className="wl-rf-k">{b.key}</span><span className="wl-rf-v">{b.text}</span></div>);
    else out.push(<p className="wl-rp" key={i}>{b.text}</p>);
  });
  flush();
  return <>{out}</>;
}
