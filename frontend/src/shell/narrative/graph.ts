import type { DNode, Tree } from "./api";

/* The dialogue graph: a layout, and the three proofs dialogue.py refuses on.
 *
 * WHY THE GRAPH IS THE EDITOR. bgate_core/dialogue.py will not write a tree
 * that fails any of three checks — a choice pointing nowhere, a node nothing
 * reaches, a node with no ending beyond it — and says so by naming the node.
 * All three are invisible in the JSON and obvious the moment the tree is drawn:
 * a stub going off into space, an island, a pocket you cannot get out of.
 *
 * THE CHECKS ARE COMPUTED HERE, ON THE CLIENT, BUT THEY DO NOT DECIDE. They
 * exist because a problem has to be positioned ON the picture — which node, at
 * which coordinates — and no endpoint can answer that. The verdict is the
 * server's: `GET /api/dialogue/{name}/validate` runs the real
 * dialogue.validate() and returns its refusal as data, Dialogue.tsx asks it on
 * every open, and when the two disagree the strip says so and names the server
 * as the authority. That route DOES exist (bgate_ui/routes/dialogue.py); the
 * comment that used to sit here said it did not, which is how this
 * reimplementation was allowed to be the last word for as long as it was.
 *
 * The checks below are a faithful reading of validate() and are still only
 * three of its rules — duplicate ids, over-long ids, a choice with no text or
 * no goto, a `start` naming no node, and a tree with no ending at all are
 * refusals this file does not compute. So a clean result here is not a verdict
 * and must never be rendered as one.
 *
 * LAYOUT IS LAYERED, NOT SIMULATED. Rank is distance from the entry node, order
 * within a rank is the order the nodes appear in the file. A conversation is
 * already a left-to-right thing; a force layout would spend a frame budget to
 * arrive somewhere less readable, and would move the node you were looking at.
 */

export const NODE_W = 190;
export const NODE_H = 62;
const COL = 250;          // rank pitch — NODE_W plus room for the edge to bend
const ROW = 84;
const PAD = 20;

/** The three named failures, plus the one adjacent refusal that is also a
 *  property of a single node ("marked end but still offers choices"). Kept
 *  distinct from the three so the strip can say which are THE three. */
export type ProblemKind = "dangling" | "orphan" | "trapped" | "end-with-choices";

export type Problem = {
  node: string;
  kind: ProblemKind;
  /** Phrased the way dialogue.py phrases its refusal — the node, then what
   *  about it. A message that does not name a node is not actionable. */
  text: string;
};

export type Placed = { id: string; node: DNode; x: number; y: number; rank: number };

export type Edge = {
  key: string; d: string; from: string; to: string;
  /** A choice that points at a node earlier in the conversation. Legal, common,
   *  and drawn dashed so it does not read as forward progress. */
  back: boolean;
  /** A choice whose target is not a node at all: drawn as a stub into nothing,
   *  because that is exactly what it is. */
  missing: boolean;
  /** Where the stub stops, for the marker and the label the renderer puts
   *  there. Carried rather than re-parsed out of `d` — a renderer picking
   *  numbers back out of a path string breaks the first time the path gains a
   *  curve. */
  tip?: { x: number; y: number };
  label: string;
};

export type Layout = {
  nodes: Placed[];
  edges: Edge[];
  width: number;
  height: number;
  start: string;
  problems: Problem[];
  /** Problems by node id, for the badge drawn ON the node. */
  byNode: Map<string, Problem[]>;
};

export const EMPTY_LAYOUT: Layout = {
  nodes: [], edges: [], width: 0, height: 0, start: "",
  problems: [], byNode: new Map(),
};

/** The entry node: the file's `start`, or the first node — the same fallback
 *  dialogue.validate() applies, so the reachability answer here matches the
 *  one the writer will get. */
function entryOf(tree: Tree): string {
  const ids = tree.nodes.map((n) => n.id);
  const declared = (tree.start || "").trim();
  return declared && ids.includes(declared) ? declared : (ids[0] || "");
}

export function layout(tree: Tree): Layout {
  const nodes = tree.nodes || [];
  if (!nodes.length) return EMPTY_LAYOUT;

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const start = entryOf(tree);
  const choices = (n: DNode) => n.choices || [];

  /* 1 · reachability from the entry, breadth-first. Also the rank. */
  const rank = new Map<string, number>();
  if (start) {
    rank.set(start, 0);
    const queue = [start];
    while (queue.length) {
      const id = queue.shift() as string;
      const here = rank.get(id) as number;
      for (const c of choices(byId.get(id) as DNode)) {
        if (byId.has(c.goto) && !rank.has(c.goto)) {
          rank.set(c.goto, here + 1);
          queue.push(c.goto);
        }
      }
    }
  }

  /* 2 · can an ending be reached from here? Fixed point, walked backwards from
     the nodes marked end — the same loop validate() runs. A node that is not in
     `escapes` is one the player enters and cannot leave, which in the game
     reads as a hang rather than as a bug. */
  const escapes = new Set(nodes.filter((n) => n.end).map((n) => n.id));
  for (let changed = true; changed;) {
    changed = false;
    for (const n of nodes) {
      if (escapes.has(n.id)) continue;
      if (choices(n).some((c) => escapes.has(c.goto))) { escapes.add(n.id); changed = true; }
    }
  }

  /* 3 · the problems, each naming its node. */
  const problems: Problem[] = [];
  for (const n of nodes) {
    choices(n).forEach((c, i) => {
      if (!byId.has(c.goto)) {
        problems.push({
          node: n.id, kind: "dangling",
          text: `choice ${i + 1} (${c.text || "untitled"}) points at "${c.goto}", `
              + "which is not a node in this dialogue",
        });
      }
    });
    if (n.end && choices(n).length) {
      problems.push({
        node: n.id, kind: "end-with-choices",
        text: `marked end but still offers ${choices(n).length} choice(s) — `
            + "an ending has nowhere to go",
      });
    }
  }
  for (const n of nodes) {
    if (!rank.has(n.id)) {
      problems.push({
        node: n.id, kind: "orphan",
        text: `nothing reaches this node from "${start}" — written, paid for, `
            + "and not in the game",
      });
    } else if (!escapes.has(n.id)) {
      problems.push({
        node: n.id, kind: "trapped",
        text: "no ending is reachable from here — the player enters and cannot leave",
      });
    }
  }

  /* 4 · placement. Unreachable nodes cannot have a distance from the entry, so
     they get a column of their own past the last one — off to the side, which
     is what being unreachable looks like. */
  const maxRank = Math.max(0, ...Array.from(rank.values()));
  const orphanCol = maxRank + 1;
  const rowOf = new Map<number, number>();
  const placed: Placed[] = nodes.map((n) => {
    const r = rank.has(n.id) ? (rank.get(n.id) as number) : orphanCol;
    const row = rowOf.get(r) || 0;
    rowOf.set(r, row + 1);
    return { id: n.id, node: n, rank: r, x: PAD + r * COL, y: PAD + row * ROW };
  });
  const pos = new Map(placed.map((p) => [p.id, p]));

  /* 5 · edges. One path per choice, so a node offering the same target twice
     draws twice — the file says it twice. */
  const edges: Edge[] = [];
  for (const p of placed) {
    choices(p.node).forEach((c, i) => {
      const from = { x: p.x + NODE_W, y: p.y + NODE_H / 2 };
      const target = pos.get(c.goto);
      const key = `${p.id}:${i}`;
      if (!target) {
        // A stub, ending in air. Drawn short so it cannot be mistaken for an
        // edge whose other end merely scrolled off.
        edges.push({
          key, from: p.id, to: c.goto, back: false, missing: true,
          label: c.text || "", tip: { x: from.x + 44, y: from.y },
          d: `M ${from.x} ${from.y} L ${from.x + 44} ${from.y}`,
        });
        return;
      }
      const to = { x: target.x, y: target.y + NODE_H / 2 };
      const back = target.rank <= p.rank;
      const dx = Math.max(30, Math.abs(to.x - from.x) / 2);
      edges.push({
        key, from: p.id, to: c.goto, back, missing: false, label: c.text || "",
        d: back
          // Bow below the row: a straight line backwards would run through
          // every node between the two ends.
          ? `M ${from.x} ${from.y} C ${from.x + 40} ${from.y + 46}, `
            + `${to.x - 40} ${to.y + 46}, ${to.x} ${to.y}`
          : `M ${from.x} ${from.y} C ${from.x + dx} ${from.y}, `
            + `${to.x - dx} ${to.y}, ${to.x} ${to.y}`,
      });
    });
  }

  const byNode = new Map<string, Problem[]>();
  for (const p of problems) byNode.set(p.node, [...(byNode.get(p.node) || []), p]);

  return {
    nodes: placed, edges, start, problems, byNode,
    width: PAD * 2 + (Math.max(orphanCol, maxRank) + 1) * COL,
    height: PAD * 2 + Math.max(1, ...Array.from(rowOf.values())) * ROW,
  };
}

/** Fit a label into a box, in the crudest way that is honest: SVG has no
 *  text-overflow, and a measured truncation would need a layout pass per node
 *  per render. The ellipsis says the text continues. */
export function clip(text: string, chars: number): string {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  return flat.length > chars ? `${flat.slice(0, chars - 1)}…` : flat;
}
