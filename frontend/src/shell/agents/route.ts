import { type FloorPlan, type Spot } from "./floorplan";

/* GETTING THERE WITHOUT GOING THROUGH A WALL.
 *
 * WHY THIS FILE EXISTS. Step 1 gave every character an x,y in floor space and
 * step 2 moves it, and the moment a character MOVES between two rooms the
 * straight line between them is wrong: the office is at the bottom of the
 * building and the art room is up the left side, so the shortest path between
 * their two coordinates goes diagonally through three partitions and the
 * lounge. A figure sliding through a wall is not a smaller version of walking,
 * it is the card layout's teleport with a tween on it - it says the walls are
 * decoration, and the whole claim of this pane is that the building is real.
 *
 * SO THE PATH IS COMPUTED AGAINST THE PLAN'S OWN WALL LIST, and against
 * nothing else. Not against a hand-written table of "art room to office goes
 * via here", which would be a second description of the building that drifts
 * from the first the day somebody disables a seat. The walls are already
 * computed in floorplan.ts with the doorways cut out of them, so a doorway is
 * passable here for exactly the reason it is drawn as a gap there: THERE IS NO
 * WALL IN IT. One source, one truth, and a plan change re-routes everybody
 * without a line in here changing.
 *
 * THE GRID IS HALF A CELL, and that number is the doorway's. Doors are 2.5
 * cells wide and land on fractional coordinates, so a whole-cell grid leaves a
 * 2.5 cell opening covering three cell edges of which the two outer ones are
 * partly walled - and a partly walled edge has to be treated as closed, which
 * squeezes every door on the floor down to a single cell and makes two
 * characters passing in a doorway impossible. At half a cell the same door is
 * four clear crossings wide. Finer than that buys nothing: the building is
 * planned in whole cells.
 *
 * A CROSSING IS BLOCKED, NOT A CELL. Walls in this plan are lines ON cell
 * edges, not filled cells - that is what lets two rooms share one wall - so
 * "can I be here" is always yes inside the footprint and the only question is
 * "can I get from here to the cell next door". Marking cells impassable
 * instead would eat a half cell of floor on both sides of every wall and shut
 * the corridor, which is three cells wide, down to two.
 *
 * THE RESULT IS STRING-PULLED. Raw A* output is a staircase of half-cell steps
 * and a character walking it looks like it is being dragged over corrugated
 * iron. Any two points on the path with nothing between them get joined, which
 * leaves the few corners that are actually load-bearing: out of the door, along
 * the corridor, in through the next one. That is the shape the walk is supposed
 * to read as, and it falls out of the geometry rather than being posed.
 */

/** Half a cell. See above: this is the doorway's number, not a tuning knob. */
const RES = 0.5;
const EPS = 1e-6;

export type Nav = {
  /** The plan's width, so the renderer can work out how many pixels a cell is
   *  from the plane it has in front of it. The walk is planned in cells and
   *  animated in pixels, and this is the only bridge between the two. */
  cols: number;
  /** Corners from `from` to `to`, inclusive of both. Never crosses a wall. */
  route(from: Spot, to: Spot): Spot[];
};

/* ── line of sight ─────────────────────────────────────────────────────────
   Walls are axis aligned, so "does this segment cross that wall" is one
   division and one comparison. The endpoint tolerance on t matters: a character
   standing IN a doorway is on the wall's own line, and a crossing that happens
   exactly at t=0 or t=1 is the segment touching the wall rather than passing
   through it.

   THE WALL'S OWN SPAN IS CLOSED, and that is not a rounding detail. A segment
   END is a corner: it is either a door jamb or the point where this wall meets
   another. Treating the span as open let a crossing land exactly on that point
   and be called clear, and at a junction - v@10 running down to y=33 meeting
   h@33 running east from x=10 - both walls waved the same crossing through, so
   pull() re-joined two points straight across the corner of a room that has no
   door there while the A* step rule had correctly refused it. Closed at both
   ends, a junction is solid. The cost is that grazing a jamb exactly on the
   line now counts as blocked, which loses nothing: the grid closes any crossing
   a wall touches at all, so no route ever asks to pass there. */
type Seg = { dir: "h" | "v"; a: number; s: number; e: number };

function clear(p: Spot, q: Spot, walls: Seg[]): boolean {
  for (const w of walls) {
    if (w.dir === "v") {
      const dx = q.x - p.x;
      if (Math.abs(dx) < EPS) continue;
      const t = (w.a - p.x) / dx;
      if (t <= EPS || t >= 1 - EPS) continue;
      const y = p.y + t * (q.y - p.y);
      if (y > w.s - EPS && y < w.e + EPS) return false;
    } else {
      const dy = q.y - p.y;
      if (Math.abs(dy) < EPS) continue;
      const t = (w.a - p.y) / dy;
      if (t <= EPS || t >= 1 - EPS) continue;
      const x = p.x + t * (q.x - p.x);
      if (x > w.s - EPS && x < w.e + EPS) return false;
    }
  }
  return true;
}

/** Drop every corner that was only there because the grid is square. Walks
 *  forward from the last kept point and takes the FURTHEST point it can still
 *  see, so a straight run down a corridor collapses to its two ends. */
function pull(pts: Spot[], walls: Seg[]): Spot[] {
  if (pts.length < 3) return pts;
  const out: Spot[] = [pts[0]];
  let i = 0;
  while (i < pts.length - 1) {
    let j = pts.length - 1;
    for (; j > i + 1; j--) if (clear(pts[i], pts[j], walls)) break;
    out.push(pts[j]);
    i = j;
  }
  return out;
}

/* ── the heap ──────────────────────────────────────────────────────────────
   A* over a few thousand nodes with a linear scan for the minimum is quadratic
   and this runs inside a layout effect, before paint, on every poll that moves
   somebody. Twenty lines of binary heap is cheaper than the stall. */
class Heap {
  private v: number[] = [];
  private k: Float64Array;
  constructor(keys: Float64Array) { this.k = keys; }
  get size() { return this.v.length; }
  push(n: number) {
    const v = this.v;
    v.push(n);
    let i = v.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (this.k[v[p]] <= this.k[v[i]]) break;
      [v[p], v[i]] = [v[i], v[p]];
      i = p;
    }
  }
  pop(): number {
    const v = this.v;
    const top = v[0];
    const last = v.pop() as number;
    if (v.length) {
      v[0] = last;
      let i = 0;
      for (;;) {
        const l = i * 2 + 1, r = l + 1;
        let m = i;
        if (l < v.length && this.k[v[l]] < this.k[v[m]]) m = l;
        if (r < v.length && this.k[v[r]] < this.k[v[m]]) m = r;
        if (m === i) break;
        [v[m], v[i]] = [v[i], v[m]];
        i = m;
      }
    }
    return top;
  }
}

const SQ2 = Math.SQRT2;

/** Build the router for one plan. Called once per plan, not per walk: the
 *  passability grid is a fact about the BUILDING, and rebuilding it per
 *  character per poll would be re-deriving the walls eight times a tick. */
export function buildNav(plan: FloorPlan): Nav {
  const gw = Math.round(plan.cols / RES);
  const gh = Math.round(plan.rows / RES);
  const n = gw * gh;

  /* Open crossings, one flag per cell per direction. East and south only:
     the west crossing out of a cell is the east crossing out of its neighbour,
     and storing both is two places for one fact to be wrong in. */
  const openE = new Uint8Array(n).fill(1);
  const openS = new Uint8Array(n).fill(1);
  /* The footprint's own edge. The outer wall is drawn as one element rather
     than emitted as segments (see floorplan.ts), so it is not in the wall list
     and nothing else would stop a character walking off the building. */
  for (let gy = 0; gy < gh; gy++) openE[gy * gw + (gw - 1)] = 0;
  for (let gx = 0; gx < gw; gx++) openS[(gh - 1) * gw + gx] = 0;

  for (const w of plan.walls) {
    if (w.dir === "v") {
      /* The wall sits on the line between cell gx and gx+1, so it is the east
         crossing of the cell to its left that it closes. */
      const gx = Math.round(w.a / RES) - 1;
      if (gx < 0 || gx >= gw) continue;
      const lo = Math.max(0, Math.floor(w.s / RES));
      const hi = Math.min(gh, Math.ceil(w.e / RES));
      for (let gy = lo; gy < hi; gy++) {
        /* ANY overlap closes it. A crossing that is half walled is not half
           passable, and treating it as open is how a character ends up
           clipping the jamb of every door it walks through. */
        const a = gy * RES, b = a + RES;
        if (Math.min(b, w.e) - Math.max(a, w.s) > EPS) openE[gy * gw + gx] = 0;
      }
    } else {
      const gy = Math.round(w.a / RES) - 1;
      if (gy < 0 || gy >= gh) continue;
      const lo = Math.max(0, Math.floor(w.s / RES));
      const hi = Math.min(gw, Math.ceil(w.e / RES));
      for (let gx = lo; gx < hi; gx++) {
        const a = gx * RES, b = a + RES;
        if (Math.min(b, w.e) - Math.max(a, w.s) > EPS) openS[gy * gw + gx] = 0;
      }
    }
  }

  /* Scratch, allocated once. A* is run several times per poll and these are
     the only allocations big enough to be worth not making eight times. */
  const g = new Float64Array(n);
  const f = new Float64Array(n);
  const came = new Int32Array(n);
  const seen = new Int32Array(n);
  const shut = new Int32Array(n);
  /* A generation counter instead of clearing three arrays per search: the
     grid is a few thousand cells and the paths touch a fraction of them, so
     stamping the era a cell was last touched costs one compare and zeroing
     the arrays costs the whole building every time. Starts at 1 so a
     never-touched cell's 0 can never match. */
  let era = 0;

  const idx = (p: Spot) => {
    const gx = Math.min(gw - 1, Math.max(0, Math.floor(p.x / RES)));
    const gy = Math.min(gh - 1, Math.max(0, Math.floor(p.y / RES)));
    return gy * gw + gx;
  };
  const centre = (i: number): Spot => ({
    x: (i % gw) * RES + RES / 2,
    y: Math.floor(i / gw) * RES + RES / 2,
  });

  /** Can you step from cell `i` to the cell `dx,dy` away. Diagonals need BOTH
   *  orthogonal crossings, or a character cuts the corner of a doorway and
   *  passes through the point where two walls meet. */
  const step = (gx: number, gy: number, dx: number, dy: number): boolean => {
    const i = gy * gw + gx;
    if (dx > 0 && !openE[i]) return false;
    if (dx < 0 && !openE[i - 1]) return false;
    if (dy > 0 && !openS[i]) return false;
    if (dy < 0 && !openS[i - gw]) return false;
    if (dx && dy) {
      /* Both ways round the corner have to be walkable, not just one. */
      const h = gy * gw + (gx + dx);
      const v = (gy + dy) * gw + gx;
      if (dy > 0 ? !openS[h] : !openS[h - gw]) return false;
      if (dx > 0 ? !openE[v] : !openE[v - 1]) return false;
    }
    return true;
  };

  function search(from: number, to: number): number[] | null {
    era++;
    const tx = to % gw, ty = Math.floor(to / gw);
    const heap = new Heap(f);
    g[from] = 0;
    f[from] = 0;
    came[from] = -1;
    seen[from] = era;
    heap.push(from);

    while (heap.size) {
      const cur = heap.pop();
      if (cur === to) {
        const path: number[] = [];
        for (let i = cur; i >= 0; i = came[i]) path.push(i);
        return path.reverse();
      }
      /* The heap holds stale entries rather than supporting decrease-key, so
         a cell can come off it twice; the second time it is already settled
         and its neighbours were relaxed with the better score. */
      if (shut[cur] === era) continue;
      shut[cur] = era;
      const gx = cur % gw, gy = (cur - gx) / gw;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          if (!dx && !dy) continue;
          const nx = gx + dx, ny = gy + dy;
          if (nx < 0 || ny < 0 || nx >= gw || ny >= gh) continue;
          if (!step(gx, gy, dx, dy)) continue;
          const ni = ny * gw + nx;
          if (shut[ni] === era) continue;
          const cost = g[cur] + (dx && dy ? SQ2 : 1);
          if (seen[ni] === era && cost >= g[ni]) continue;
          seen[ni] = era;
          g[ni] = cost;
          came[ni] = cur;
          /* Octile, which is the exact distance on an 8-way grid with no walls
             and therefore admissible: it never overestimates, so the first
             time the goal comes off the heap the path is the shortest one. */
          const adx = Math.abs(nx - tx), ady = Math.abs(ny - ty);
          f[ni] = cost + (adx + ady) + (SQ2 - 2) * Math.min(adx, ady);
          heap.push(ni);
        }
      }
    }
    return null;
  }

  /* Routes repeat: the same seat walks desk to lounge and back all session,
     and every character in a chain leaves from the same room. The plan does
     not change under a cache entry, because a new plan gets a new Nav. */
  const cache = new Map<string, Spot[]>();

  return {
    cols: plan.cols,
    route(from: Spot, to: Spot): Spot[] {
      const key = `${from.x.toFixed(2)},${from.y.toFixed(2)}>`
        + `${to.x.toFixed(2)},${to.y.toFixed(2)}`;
      const hit = cache.get(key);
      if (hit) return hit;

      let out: Spot[];
      /* IN SIGHT OF EACH OTHER IS THE COMMON CASE and it must not pay for the
         grid: a character standing up from its desk, or shuffling one place
         along the queue, is a straight line and the search would return the
         same answer after several hundred pops. */
      if (clear(from, to, plan.walls)) {
        out = [from, to];
      } else {
        const path = search(idx(from), idx(to));
        /* NO ROUTE IS STILL A MOVE. A seat whose room was disabled mid-walk,
           or a coordinate the plan put in a sealed pocket, still has to end up
           where the poll says it is - dropping the character or freezing it
           somewhere it is not would be the pane lying about a placement, which
           is the one thing it exists not to do. It goes in a straight line and
           the geometry is wrong for one journey. */
        out = path
          ? pull([from, ...path.map(centre), to], plan.walls)
          : [from, to];
      }

      /* Bounded, because the key space is every pair of placements and this
         map would otherwise be a session-length leak on a pane that is open
         all day. Oldest out first; a re-walked route just recomputes. */
      if (cache.size > 400) cache.delete(cache.keys().next().value as string);
      cache.set(key, out);
      return out;
    },
  };
}
