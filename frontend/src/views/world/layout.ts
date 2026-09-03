import type { LoreEdge, LoreNode } from "./api";

/* Placing the lore graph.
 *
 * The payload arrives laid out server-side: one column per KIND at 300px
 * pitch, indexed into the seven-kind tuple. A project holding only characters,
 * concepts and species therefore drew columns at x=340/1540/1840 with a 960px
 * hole where faction/place/event/item would have been, stacked 19 characters
 * into a single 2,900px column, and left fit() clamped on its 0.55 floor
 * showing the top third of it. Kind is not structure — how the entities are
 * wired is. So the client re-places the nodes it was handed: every hub takes
 * its satellites in a fan beside it, on the side its links point away from,
 * and the resulting blocks are shelf-packed into a viewport-shaped rectangle.
 * lore.graph() is left alone; the MCP tool surface reads it too.
 *
 * Deterministic throughout — every tie breaks on the slug, so the same canon
 * draws the same picture on every load. */

const LO = { w: 240, colGap: 32, rowH: 156, gap: 56, margin: 40, aspect: 2.4, hub: 3 };
const CW = () => LO.w + LO.colGap;

type Adj = {
  nb: Map<string, Set<string>>; out: Map<string, number>; inn: Map<string, number>;
  deg: (id: string) => number;
};

type Slot = { id: string; x: number; y: number };
type Block = { at: Slot[]; w: number; h: number; key: string; ox?: number; oy?: number };

/** Undirected neighbours plus the in/out counts, which decide which side of
 *  its fan a hub stands on. */
function loreAdj(nodes: LoreNode[], edges: LoreEdge[]): Adj {
  const nb = new Map<string, Set<string>>(), out = new Map<string, number>(), inn = new Map<string, number>();
  nodes.forEach((n) => { nb.set(n.id, new Set()); out.set(n.id, 0); inn.set(n.id, 0); });
  (edges || []).forEach((e) => {
    const a = e.from && e.from[0], b = e.to && e.to[0];
    if (a === b || !nb.has(a) || !nb.has(b)) return;
    out.set(a, (out.get(a) || 0) + 1); inn.set(b, (inn.get(b) || 0) + 1);
    nb.get(a)!.add(b); nb.get(b)!.add(a);
  });
  return { nb, out, inn, deg: (id) => nb.get(id)?.size || 0 };
}

/** Cols x rows for n cards at roughly the page's aspect, penalising the slots
 *  a ragged last row would leave empty. */
function gridShape(n: number): { cols: number; rows: number } {
  if (n <= 1) return { cols: 1, rows: 1 };
  let best: { cols: number; rows: number; score: number } | null = null;
  for (let cols = 1; cols <= n; cols++) {
    const rows = Math.ceil(n / cols);
    const score = Math.abs(Math.log((cols * CW()) / (rows * LO.rowH) / LO.aspect))
      + (cols * rows - n) * 0.04;
    if (!best || score < best.score) best = { cols, rows, score };
  }
  return best!;
}

function gridBlock(ids: string[]): Block {
  const g = gridShape(ids.length);
  return {
    at: ids.map((id, i) => ({ id, x: (i % g.cols) * CW(), y: Math.floor(i / g.cols) * LO.rowH })),
    w: g.cols * CW(), h: g.rows * LO.rowH, key: ids[0],
  };
}

function hubBlock(hub: string, sats: string[], adj: Adj): Block {
  if (!sats.length) return gridBlock([hub]);
  // The tone links point INTO the tone guide, and an edge leaves a card on
  // its right and enters the next on its left — so a hub that is mostly a
  // destination has to stand to the RIGHT of its fan or every link doubles back.
  const right = (adj.inn.get(hub) || 0) >= (adj.out.get(hub) || 0);
  const g = gridShape(sats.length);
  const gx = right ? 0 : CW();
  const at: Slot[] = sats.map((id, i) => ({
    id, x: gx + (i % g.cols) * CW(), y: Math.floor(i / g.cols) * LO.rowH,
  }));
  const h = g.rows * LO.rowH;
  at.push({ id: hub, x: right ? g.cols * CW() : 0, y: Math.max(0, (h - LO.rowH) / 2) });
  return { at, w: (g.cols + 1) * CW(), h, key: hub };
}

/** Mutates `nodes` in place with x/y. `moved` wins over the layout: it holds
 *  the positions the reader dragged this session, which a rebuild after a
 *  write must not throw away. */
export function layoutLore(nodes: LoreNode[], edges: LoreEdge[],
                           moved: Map<string, { x: number; y: number }>): void {
  if (!nodes || !nodes.length) return;
  const adj = loreAdj(nodes, edges);
  const ids = nodes.map((n) => n.id).slice().sort();
  const byDeg = (a: string, b: string) => adj.deg(b) - adj.deg(a) || (a < b ? -1 : 1);

  const hubs = ids.filter((id) => adj.deg(id) >= LO.hub).sort(byDeg);
  const isHub = new Set(hubs);
  // A satellite goes to its biggest neighbouring hub, so the one dominant hub
  // keeps its crowd instead of it being split across whoever asked first.
  const owner = new Map<string, string>();
  ids.forEach((id) => {
    if (isHub.has(id)) return;
    const pick = [...(adj.nb.get(id) || [])].filter((h) => isHub.has(h)).sort(byDeg)[0];
    if (pick) owner.set(id, pick);
  });

  const blocks: Block[] = hubs.map((h) =>
    hubBlock(h, ids.filter((i) => owner.get(i) === h).sort(byDeg), adj));

  // What is left over: small linked runs kept whole, then the unlinked.
  const loose = ids.filter((i) => !isHub.has(i) && !owner.has(i));
  const free = new Set(loose);
  const seen = new Set<string>();
  loose.forEach((start) => {
    if (seen.has(start) || !adj.deg(start)) return;
    const comp: string[] = [], stack = [start];
    seen.add(start);
    while (stack.length) {
      const id = stack.pop() as string;
      comp.push(id);
      [...(adj.nb.get(id) || [])].sort().forEach((next) => {
        if (free.has(next) && !seen.has(next)) { seen.add(next); stack.push(next); }
      });
    }
    blocks.push(gridBlock(comp.sort(byDeg)));
  });
  const alone = loose.filter((i) => !adj.deg(i));
  if (alone.length) blocks.push(gridBlock(alone));
  if (!blocks.length) return;

  // Shelf-pack tallest first. A pile shaped like the viewport is the whole
  // difference between fit() clamping at its floor and showing the graph.
  blocks.sort((a, b) => b.h - a.h || b.w - a.w || (a.key < b.key ? -1 : 1));
  const area = blocks.reduce((sum, b) => sum + b.w * b.h, 0);
  const width = Math.max(Math.max(...blocks.map((b) => b.w)), Math.sqrt(area * LO.aspect));
  let x = 0, y = 0, shelf = 0;
  blocks.forEach((b) => {
    if (x > 0 && x + b.w > width) { x = 0; y += shelf + LO.gap; shelf = 0; }
    b.ox = x; b.oy = y;
    x += b.w + LO.gap;
    shelf = Math.max(shelf, b.h);
  });

  const pos = new Map<string, { x: number; y: number }>();
  blocks.forEach((b) => b.at.forEach((s) =>
    pos.set(s.id, { x: LO.margin + (b.ox || 0) + s.x, y: LO.margin + (b.oy || 0) + s.y })));
  nodes.forEach((n) => {
    const p = moved.get(n.id) || pos.get(n.id);
    if (p) { n.x = p.x; n.y = p.y; }
  });
}
