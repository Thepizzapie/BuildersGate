/* THE FLOOR DRAWN ON A CANVAS, NOT IN THE DOM.
 *
 * The DOM version placed 69 absolutely-positioned elements inside a
 * perspective-transformed div and asked the browser to composite them into
 * something that looks like a room. It never did: CSS is a document layout
 * engine, not a game renderer, and the result read as a web page wearing a
 * skin rather than a place.
 *
 * This file draws the same building the same data describes, on a 2D canvas:
 * tiled floors, composited sprites, y-sorted depth, pixel-art scaling. The
 * sprites are already drawn for a 70-degree camera, so rendering them flat
 * IS the correct projection. A game engine would do the same thing.
 *
 * NOTHING IN HERE DECIDES WHERE ANYTHING IS. floorplan.ts computes the grid,
 * occupancy.ts decides who stands where, route.ts paths through doorways.
 * This file converts cells to pixels and draws.
 */

import { type FloorPlan, type PlanRoom, type Prop, type Spot } from "./floorplan";
import { type Occupant } from "./occupancy";
import { type Nav } from "./route";
import { castFrames } from "./castFrames";

/* ── image cache ─────────────────────────────────────────────────────────── */

const imgCache = new Map<string, HTMLImageElement>();
const loading = new Set<string>();

function getImg(src: string): HTMLImageElement | null {
  const hit = imgCache.get(src);
  if (hit) return hit.complete && hit.naturalWidth > 0 ? hit : null;
  if (loading.has(src)) return null;
  loading.add(src);
  const img = new Image();
  img.src = src;
  img.onload = () => { imgCache.set(src, img); loading.delete(src); };
  img.onerror = () => { loading.delete(src); };
  imgCache.set(src, img);
  return null;
}

/* ── colours ─────────────────────────────────────────────────────────────── */

const SEAT_HEX: Record<string, string> = {
  director: "#f59e0b", narrative: "#ef4444", gameplay: "#ec4899",
  tech: "#06b6d4", art: "#f472b6", audio: "#10b981",
  qa: "#8b5cf6", cinematic: "#3b82f6",
};
/* WALLS HAVE HEIGHT NOW, so they need three tones rather than one: the top
   surface you look down on, the face that turns toward the camera, and the
   shadow the wall throws on the floor in front of it. A single flat colour is
   what made the building read as a diagram of a floor rather than a place with
   things standing up in it. */
const WALL_TOP = "#4b4a58";
const WALL_FACE = "#33323d";
const WALL_FACE_LIT = "#3e3d4a";
const WALL_OUTER = "#565565";

/* HOW TALL A WALL IS, IN CELLS. Not a real height - the camera is a fixed
   three-quarter and this is the number of cells of FACE drawn below a wall's
   line. Small on purpose: enough to occlude a character's feet and catch light,
   not so much that the building becomes a maze of parapets you cannot see over. */
const WALL_H = 0.85;

/* ONE LIGHT DIRECTION FOR THE WHOLE BUILDING, and everything that casts a
   shadow uses it. Shadows that disagree about where the light is are the
   fastest way to make a scene look assembled rather than lit. */
const LIGHT_DX = -0.18;
const LIGHT_DY = 0.42;

/* Where the floor's own UI sprites live. Separate from the env sprites because
   these are chrome the building is drawn WITH rather than things standing in
   it. */
const UI = "/static/img/floor/ui";

/* THE NINE-SLICE MARGIN OF bubble-box.png, IN THAT SPRITE'S OWN PIXELS.
   Measured off the generated art's alpha channel (65px corner notch at the
   1024 generation size, shipped at quarter scale) rather than chosen. If the
   bubble sprite is ever regenerated this number is regenerated with it. */
const BUBBLE_MARGIN = 16;
const PLATE_BG = "rgba(24,23,28,0.88)";
const PLATE_TEXT = "#bab6ae";

/* ── animation ───────────────────────────────────────────────────────────── */

const CAST_SEATS = new Set([
  "art", "audio", "narrative", "gameplay", "qa", "cinematic", "tech", "director",
]);
const POSE: Record<string, string> = {
  running: "working", delivering: "handoff", idle: "idle",
  dispatched: "idle", chained: "idle", waiting: "idle", failed: "idle",
};

function animFor(who: Occupant): string {
  return who.carrying ? "handoff" : (POSE[who.state] || "idle");
}

const ANIM_SPEED: Record<string, number> = {
  idle: 2800, sitting: 3200, working: 1200, walk: 720, handoff: 800,
};


/* ── deterministic detail ─────────────────────────────────────────────────────

   EVERY SCUFF, STAIN AND PLANK ON THIS FLOOR IS A PURE FUNCTION OF ITS OWN
   COORDINATE, and that is not a stylistic choice - it is the only way detail
   can exist at all. A floor speckled with Math.random() re-rolls every frame,
   so the scuffs crawl and the whole building fizzes like television static. The
   hash below returns the same number for the same cell forever, so a stain is
   in the same place this second as it was last second and the floor holds
   still.

   IT IS ALSO WHY THE FLOOR CAN AFFORD TO BE EXPENSIVE. Because the result never
   changes between frames, the entire static building is baked ONCE into an
   offscreen canvas (see bakeFloor) and blitted per frame. The per-frame cost of
   a floor with ten thousand grain marks on it is one drawImage. */
function hash2(x: number, y: number): number {
  let h = Math.imul(x | 0, 374761393) ^ Math.imul(y | 0, 668265263);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

/** Two independent draws from one coordinate, for anything needing a position
 *  AND a size without the two correlating into a visible diagonal. */
function hash2b(x: number, y: number, salt: number): number {
  return hash2(x * 31 + salt * 7919, y * 17 + salt * 104729);
}

/* WHAT EACH ROOM'S FLOOR IS MADE OF. A studio does not carpet its server room
   and tile its director's office, and one uniform surface under all nine rooms
   was most of why the building read as a diagram of a floor plan. The material
   is chosen from the SEAT, so it is stable across sessions and it means
   something. */
type Surface = "carpet" | "tile" | "wood" | "vinyl" | "concrete";

const ROOM_SURFACE: Record<string, Surface> = {
  director: "wood",      // the corner office got the parquet
  narrative: "carpet",   // writers' room, soft and warm
  art: "vinyl",          // wipe-clean, because paint
  audio: "carpet",       // dead room, deep pile
  tech: "concrete",      // raised floor over cable trays
  gameplay: "carpet",
  qa: "vinyl",
  cinematic: "carpet",   // edit bay, quiet underfoot
};

const SURFACE_BASE: Record<Surface, string> = {
  carpet: "#221f26",
  tile: "#1e1f24",
  wood: "#2a2019",
  vinyl: "#20222a",
  concrete: "#1e1e21",
};

/** Lighten (t > 0) or darken (t < 0) a #rrggbb by a fraction. Written out
 *  rather than pulled from a library because this file has no dependencies and
 *  three lines is cheaper than one. */
function shade(hex: string, t: number): string {
  const n = parseInt(hex.slice(1), 16);
  const f = (v: number) => Math.max(0, Math.min(255, Math.round(
    t > 0 ? v + (255 - v) * t : v * (1 + t))));
  return "rgb(" + f((n >> 16) & 255) + "," + f((n >> 8) & 255) + "," + f(n & 255) + ")";
}

/* ── the renderer ────────────────────────────────────────────────────────── */

export type DrawState = {
  plan: FloorPlan;
  people: Occupant[];
  spots: Map<string, Spot>;
  nav: Nav;
  picked: string;
  /* Whether the lounge radio is playing. It is the ONE piece of state on this
     floor that is not a fact about the board, and it is here because the radio
     is drawn differently when it is on - a control that looks identical in both
     states is a switch nobody can read. */
  musicOn: boolean;
  /* WHAT THE LOUNGE IS SAYING AND WHO IS SAYING IT, or null for silence.
     Chosen by the pane, not here - the renderer must not decide who talks any
     more than it decides who is running. */
  banter: { seat: string; line: string } | null;
};

type CharState = {
  x: number; y: number;
  /** Remaining corners of the route, in cells. Empty when standing. */
  path: Spot[];
  goal: Spot;
  animTime: number;
  facingRight: boolean;
};

export class FloorRenderer {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private cell = 24;
  private chars = new Map<string, CharState>();
  private lastTime = 0;
  private raf = 0;
  private data: DrawState | null = null;
  /* Milliseconds since the renderer started, for anything that animates on its
     own clock rather than a character's. */
  private clock = 0;
  /* Is the pointer over the lounge radio. Tracked so the one clickable prop on
     the floor can say so, and so the cursor can change over it - a canvas has
     no :hover, and without this the radio is a picture of a radio. */
  private hoverRadio = false;
  /* Which character the pointer is over, so the tooltip is only rewritten when
     it changes rather than on every mousemove. */
  private hoverPerson = "";
  /* Which line the bubble is currently showing, and when it appeared - the fade
     is a property of the LINE changing, and the line changes on a 15s hook tick
     that knows nothing about frames. */
  private bubbleFor = "";
  private bubbleAt = 0;

  onClick: ((seat: string) => void) | null = null;
  /** Clicked the lounge radio. Separate from onClick because it selects
   *  nothing - it is a switch, not a way into the inspector. */
  onRadio: (() => void) | null = null;
  /** Is there a soundtrack to switch at all. Drives the radio's hover state and
   *  its tooltip; the prop is still drawn either way, because a lounge with no
   *  radio in it because nobody ran the generator is a stranger floor than a
   *  lounge with a radio nobody has loaded a tape into. */
  radioLive = false;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("no 2d context");
    this.ctx = ctx;
    canvas.addEventListener("click", this.handleClick);
    canvas.addEventListener("mousemove", this.handleMove);
    canvas.addEventListener("mouseleave", this.handleLeave);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.canvas.removeEventListener("click", this.handleClick);
    this.canvas.removeEventListener("mousemove", this.handleMove);
    this.canvas.removeEventListener("mouseleave", this.handleLeave);
  }

  /** Canvas pixels for a pointer event, in CELLS. */
  private cellAt(e: MouseEvent): Spot {
    const rect = this.canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * (this.canvas.width / rect.width);
    const my = (e.clientY - rect.top) * (this.canvas.height / rect.height);
    const pad = this.padding();
    return { x: (mx - pad.x) / this.cell, y: (my - pad.y) / this.cell };
  }

  private radioAt(x: number, y: number): boolean {
    for (const room of this.data?.plan.rooms || []) {
      for (const prop of room.props) {
        if (prop.kind !== "radio") continue;
        if (x >= prop.x - 0.5 && x < prop.x + prop.w + 0.5
            && y >= prop.y - 0.5 && y < prop.y + prop.h + 0.5) return true;
      }
    }
    return false;
  }

  /** Which character is under this point, or "". Nearest-to-camera first, so a
   *  figure standing in front of another gets the click - the same rule the
   *  drawing uses. */
  private personAt(x: number, y: number): string {
    let best = "";
    let bestY = -Infinity;
    for (const [seat, ch] of this.chars) {
      /* The drawn figure: 2.16 cells wide, 2.7 tall, standing on its feet. A
         little wider than the art so a small character at a small cell size is
         still comfortably clickable. */
      /* THE DIRECTOR IS NOT A CONTROL, and that is a decision rather than an
         omission: the main console IS the Director's chat, so a panel here
         would be a worse copy of the screen this floor is standing on. */
      if (seat === "director") continue;
      if (x > ch.x - 1.15 && x < ch.x + 1.15
          && y > ch.y - 2.7 && y < ch.y + 0.35
          && ch.y > bestY) {
        best = seat;
        bestY = ch.y;
      }
    }
    return best;
  }

  private handleMove = (e: MouseEvent) => {
    if (!this.data) return;
    const at = this.cellAt(e);

    /* THE TOOLTIP NAMES WHOEVER IS UNDER THE POINTER. A canvas has no title
       attribute per shape, so the one the element carries is rewritten as the
       pointer moves. Without it the cast is a picture: there is nothing to tell
       a reader that the figure in the lounge is a control at all. */
    const who = this.personAt(at.x, at.y);
    if (who !== this.hoverPerson) {
      this.hoverPerson = who;
      if (who) {
        const p = this.data.people.find((q) => q.seat === who);
        this.canvas.title = p
          ? [p.seat, p.note, p.itemId ? "#" + p.itemId : null, p.title]
              .filter(Boolean).join(" - ")
          : who;
      } else {
        this.canvas.title = "";
      }
    }

    const hot = this.radioAt(at.x, at.y);
    if (hot === this.hoverRadio && !who) return;
    this.hoverRadio = hot;
    if (who) return;
    /* NO TOOLTIP WHEN THERE IS NOTHING TO PLAY. `radioLive` is false when no
       track set has been generated, and a radio that offers to turn on and then
       does nothing is worse than one that says nothing. */
    this.canvas.title = hot && this.radioLive
      ? (this.data.musicOn ? "turn the radio off" : "turn the radio on")
      : "";
  };

  private handleLeave = () => {
    this.hoverRadio = false;
    this.hoverPerson = "";
    this.canvas.title = "";
  };

  private handleClick = (e: MouseEvent) => {
    /* Guarded on the DATA only. The radio is a switch that needs no selection
       target, so a mount with no onClick - the stream overlay - can still
       answer a click on it. */
    if (!this.data) return;
    const at = this.cellAt(e);

    /* A CHARACTER IS THE FIRST THING TESTED, because it is the last thing
       drawn and because it is the thing the reader is actually pointing at.

       THIS DID NOT EXIST AND IT IS WHY THE LOUNGE WAS DEAD. Only ROOMS answered
       a click, and a room resolves to the seat that OWNS it - so an idle agent,
       which by definition is standing in the lounge rather than in its own
       room, could not be selected at all. Clicking it hit the lounge, the
       lounge belongs to no seat, and nothing happened. The one place you most
       want to click somebody - the room full of agents with nothing to do, to
       give one of them something to do - was the one place clicking did
       nothing.

       THE TARGET IS THE FIGURE, NOT THE FEET. A character is drawn from its
       feet UP, so its box runs from the standing coordinate up by the sprite's
       height. Testing the feet alone would mean aiming at the floor under
       somebody rather than at them. */
    const hit = this.personAt(at.x, at.y);
    if (hit && this.onClick) {
      this.onClick(hit);
      return;
    }

    /* THE RADIO NEXT, because it sits INSIDE the lounge and the lounge is
       itself a click target. Last-drawn-wins is what a reader expects from a
       thing drawn on top of another thing. */
    if (this.radioAt(at.x, at.y)) {
      this.onRadio?.();
      return;
    }

    for (const room of this.data.plan.rooms) {
      if (this.onClick && room.seat && room.seat !== "director"
          && at.x >= room.rect.x && at.x < room.rect.x + room.rect.w
          && at.y >= room.rect.y && at.y < room.rect.y + room.rect.h) {
        this.onClick(room.seat);
        return;
      }
    }
  };

  resize() {
    const parent = this.canvas.parentElement;
    if (!parent) return;
    const dpr = window.devicePixelRatio || 1;
    const w = parent.clientWidth;
    const h = parent.clientHeight;
    this.canvas.width = w * dpr;
    this.canvas.height = h * dpr;
    this.canvas.style.width = w + "px";
    this.canvas.style.height = h + "px";
    if (this.data) {
      const maxCellW = (w - 24) / this.data.plan.cols;
      const maxCellH = (h - 24) / this.data.plan.rows;
      this.cell = Math.max(8, Math.min(28, maxCellW, maxCellH)) * dpr;
    }
  }

  private padding(): { x: number; y: number } {
    if (!this.data) return { x: 0, y: 0 };
    const pw = this.data.plan.cols * this.cell;
    const ph = this.data.plan.rows * this.cell;
    return {
      x: (this.canvas.width - pw) / 2,
      y: (this.canvas.height - ph) / 2,
    };
  }

  update(data: DrawState) {
    this.data = data;
    const live = new Set<string>();
    for (const p of data.people) {
      live.add(p.seat);
      const spot = data.spots.get(p.seat);
      if (!spot) continue;
      let ch = this.chars.get(p.seat);
      if (!ch) {
        /* First sighting: appear where the board says, do not walk in from a
           coordinate the server never described. */
        ch = { x: spot.x, y: spot.y, path: [], goal: spot,
               animTime: 0, facingRight: false };
        this.chars.set(p.seat, ch);
        continue;
      }
      if (Math.abs(spot.x - ch.goal.x) > 0.1 || Math.abs(spot.y - ch.goal.y) > 0.1) {
        ch.goal = spot;
        /* THE LINE BETWEEN TWO PLACEMENTS IS ROUTED, NOT DRAWN. Both ends are
           the server's; everything between runs through partitions, so it comes
           from the same wall list the walls are drawn from. */
        const corners = data.nav.route({ x: ch.x, y: ch.y }, spot);
        ch.path = corners.slice(1);
      }
    }
    for (const seat of [...this.chars.keys()]) {
      if (!live.has(seat)) this.chars.delete(seat);
    }
  }

  start() {
    this.lastTime = performance.now();
    const loop = (now: number) => {
      const dt = Math.min(now - this.lastTime, 100);
      this.lastTime = now;
      this.tick(dt);
      this.draw();
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }

  /** Cells per second. The poll is 3s and the building is 30 cells across, so
   *  the longest crossing lands well inside one tick - a character still
   *  travelling when the next placement arrives re-routes from where it stands,
   *  which is the honest thing to draw. */
  private static SPEED = 6;

  private tick(dt: number) {
    this.clock += dt;
    const budget = FloorRenderer.SPEED * dt / 1000;
    for (const [, ch] of this.chars) {
      ch.animTime += dt;
      let left = budget;
      while (ch.path.length > 0 && left > 0) {
        const next = ch.path[0];
        const dx = next.x - ch.x;
        const dy = next.y - ch.y;
        const dist = Math.hypot(dx, dy);
        if (dist <= left) {
          ch.x = next.x;
          ch.y = next.y;
          left -= dist;
          ch.path.shift();
        } else {
          ch.x += (dx / dist) * left;
          ch.y += (dy / dist) * left;
          if (Math.abs(dx) > 0.05) ch.facingRight = dx > 0;
          left = 0;
        }
      }
    }
  }


  /* -- the ground, baked --------------------------------------------------

     THE FLOOR IS DRAWN ONCE AND BLITTED, NOT REDRAWN SIXTY TIMES A SECOND.
     Every mark below - plank seams, carpet grain, the scuffed traffic lane
     through a doorway, grout, the skirting shadow - is a pure function of a
     coordinate and never changes, so recomputing it per frame would be paying
     thousands of fill calls for an image identical to the last one. Baked into
     an offscreen canvas keyed on the plan and the cell size, the per-frame cost
     is ONE drawImage, and that is what makes it affordable for the ground to
     have any detail at all.

     WHAT IS NOT IN HERE IS ANYTHING THAT MOVES OR MEANS ANYTHING. The light
     pools brighten when a seat starts running, and the walls have to y-sort
     against characters, so both stay in the live pass. The rule for this
     function is: if it can change, it does not belong here. */
  private bakeKey = "";
  private baked: HTMLCanvasElement | null = null;

  private bakeFloor() {
    const plan = this.data!.plan;
    const cell = this.cell;
    const W = Math.ceil(plan.cols * cell);
    const H = Math.ceil(plan.rows * cell);
    const key = plan.cols + "x" + plan.rows + "@" + cell.toFixed(2) + ":"
      + plan.rooms.map((r) => r.id + (r.seat || "")).join(",");
    if (this.bakeKey === key && this.baked) return;
    if (W < 2 || H < 2) return;

    const off = document.createElement("canvas");
    off.width = W;
    off.height = H;
    const g = off.getContext("2d");
    if (!g) return;

    /* THE CORRIDOR: institutional tile, laid in ONE grid running through the
       whole building rather than per room, because a corridor is a continuous
       surface and a per-room grid would draw seams where there is no seam. */
    g.fillStyle = "#191a1f";
    g.fillRect(0, 0, W, H);
    const tileN = Math.max(6, cell * 1.6);
    g.strokeStyle = "rgba(255,255,255,0.028)";
    g.lineWidth = 1;
    for (let x = 0; x <= W; x += tileN) {
      g.beginPath();
      g.moveTo(Math.round(x) + 0.5, 0);
      g.lineTo(Math.round(x) + 0.5, H);
      g.stroke();
    }
    for (let y = 0; y <= H; y += tileN) {
      g.beginPath();
      g.moveTo(0, Math.round(y) + 0.5);
      g.lineTo(W, Math.round(y) + 0.5);
      g.stroke();
    }
    /* A few tiles a shade off, so the grid reads as a floor and not as graph
       paper. */
    for (let ty = 0; ty * tileN < H; ty++) {
      for (let tx = 0; tx * tileN < W; tx++) {
        const r = hash2(tx, ty);
        if (r > 0.86) {
          g.fillStyle = "rgba(255,255,255," + (0.012 + r * 0.014) + ")";
          g.fillRect(tx * tileN, ty * tileN, tileN, tileN);
        } else if (r < 0.08) {
          g.fillStyle = "rgba(0,0,0,0.10)";
          g.fillRect(tx * tileN, ty * tileN, tileN, tileN);
        }
      }
    }

    for (const room of plan.rooms) {
      const rx = Math.round(room.rect.x * cell);
      const ry = Math.round(room.rect.y * cell);
      const rw = Math.round(room.rect.w * cell);
      const rh = Math.round(room.rect.h * cell);
      const surface: Surface = room.kind === "lounge" ? "wood"
        : (room.seat && ROOM_SURFACE[room.seat]) || "carpet";

      g.save();
      g.beginPath();
      g.rect(rx, ry, rw, rh);
      g.clip();

      g.fillStyle = SURFACE_BASE[surface];
      g.fillRect(rx, ry, rw, rh);

      /* The seat's own colour, barely there. Enough that two rooms side by side
         are different rooms; not so much that the floor is tinted plastic. */
      const seatColor = room.seat ? SEAT_HEX[room.seat] : null;
      if (seatColor) {
        g.globalAlpha = 0.07;
        g.fillStyle = seatColor;
        g.fillRect(rx, ry, rw, rh);
        g.globalAlpha = 1;
      }

      this.bakeSurface(g, surface, rx, ry, rw, rh, cell);

      /* THE TRAFFIC LANE. Carpet wears where people walk, and the one line
         everybody in a room has walked is doorway to desk. A soft darker path
         along it is the cheapest way to say the room has been used. */
      if (surface === "carpet" || surface === "vinyl") {
        const d = room.desk;
        const midX = room.rect.x * cell + rw * 0.5;
        const lane = g.createLinearGradient(midX, ry + rh, d.x * cell, d.y * cell);
        lane.addColorStop(0, "rgba(0,0,0,0.18)");
        lane.addColorStop(1, "rgba(0,0,0,0)");
        g.strokeStyle = lane;
        g.lineWidth = cell * 1.5;
        g.lineCap = "round";
        g.beginPath();
        g.moveTo(midX, ry + rh);
        g.lineTo(d.x * cell, (d.y + 0.8) * cell);
        g.stroke();
      }

      /* SKIRTING. A board runs round the bottom of every wall in a real
         building, and its absence is one of those things nobody names but
         everybody registers as "this is a drawing of a room". */
      const sk = Math.max(2, cell * 0.16);
      g.fillStyle = "rgba(255,255,255,0.05)";
      g.fillRect(rx, ry, rw, sk);
      g.fillStyle = "rgba(0,0,0,0.22)";
      g.fillRect(rx, ry + rh - sk, rw, sk);
      g.fillRect(rx, ry, sk, rh);
      g.fillRect(rx + rw - sk, ry, sk, rh);

      /* AMBIENT OCCLUSION at the wall join. Light does not reach that corner,
         and drawing it dark is most of what tells the eye a wall is standing up
         rather than painted on. */
      const ao = Math.max(3, cell * 0.7);
      const sides: number[][] = [
        [rx, ry, rx, ry + ao],
        [rx, ry + rh, rx, ry + rh - ao],
        [rx, ry, rx + ao, ry],
        [rx + rw, ry, rx + rw - ao, ry],
      ];
      for (const side of sides) {
        const eg = g.createLinearGradient(side[0], side[1], side[2], side[3]);
        eg.addColorStop(0, "rgba(0,0,0,0.40)");
        eg.addColorStop(1, "rgba(0,0,0,0)");
        g.fillStyle = eg;
        g.fillRect(rx, ry, rw, rh);
      }
      g.restore();
    }

    this.baked = off;
    this.bakeKey = key;
  }

  /** The grain of one material. Deterministic, so it bakes identically every
   *  time and never crawls. */
  private bakeSurface(g: CanvasRenderingContext2D, surface: Surface,
                      rx: number, ry: number, rw: number, rh: number,
                      cell: number) {
    if (surface === "wood") {
      /* PLANKS, with staggered end joints. A parquet of equal squares reads as
         a chessboard; staggered planks read as a floor. */
      const pw = cell * 1.05;
      let i = 0;
      for (let y = ry; y < ry + rh; y += pw, i++) {
        g.fillStyle = "rgba(255,255,255," + (0.012 + hash2(i, 3) * 0.03) + ")";
        g.fillRect(rx, y, rw, pw - 1);
        g.fillStyle = "rgba(0,0,0,0.30)";
        g.fillRect(rx, y + pw - 1, rw, 1);
        const jitter = hash2(i, 11) * cell * 6;
        for (let x = rx + jitter; x < rx + rw; x += cell * 5.5) {
          g.fillStyle = "rgba(0,0,0,0.28)";
          g.fillRect(Math.round(x), y, 1, pw - 1);
        }
        for (let k = 0; k < 3; k++) {
          const gy = y + hash2b(i, k, 5) * (pw - 2);
          const gx = rx + hash2b(i, k, 9) * rw * 0.7;
          g.fillStyle = "rgba(0,0,0,0.10)";
          g.fillRect(gx, gy, rw * 0.18 * hash2b(i, k, 13), 1);
        }
      }
      return;
    }
    if (surface === "carpet") {
      /* PILE, as fine speckle. Dense enough to read as texture at a glance, and
         cheap because it is baked. */
      const step = Math.max(2, cell * 0.18);
      for (let y = ry; y < ry + rh; y += step) {
        for (let x = rx; x < rx + rw; x += step) {
          const r = hash2(x, y);
          if (r > 0.62) {
            g.fillStyle = "rgba(255,255,255," + ((r - 0.62) * 0.07) + ")";
            g.fillRect(x, y, step * 0.7, step * 0.7);
          } else if (r < 0.16) {
            g.fillStyle = "rgba(0,0,0," + ((0.16 - r) * 0.4) + ")";
            g.fillRect(x, y, step * 0.7, step * 0.7);
          }
        }
      }
      return;
    }
    if (surface === "tile" || surface === "vinyl") {
      const t = cell * (surface === "tile" ? 1.0 : 1.7);
      g.strokeStyle = "rgba(0,0,0,0.26)";
      g.lineWidth = 1;
      for (let x = rx; x <= rx + rw; x += t) {
        g.beginPath();
        g.moveTo(Math.round(x) + 0.5, ry);
        g.lineTo(Math.round(x) + 0.5, ry + rh);
        g.stroke();
      }
      for (let y = ry; y <= ry + rh; y += t) {
        g.beginPath();
        g.moveTo(rx, Math.round(y) + 0.5);
        g.lineTo(rx + rw, Math.round(y) + 0.5);
        g.stroke();
      }
      /* A highlight along the top of each tile: the sheen of a hard floor, and
         the thing that stops grout lines looking like a wireframe. */
      for (let ty = 0; ty * t < rh; ty++) {
        for (let tx = 0; tx * t < rw; tx++) {
          const r = hash2(tx, ty);
          g.fillStyle = "rgba(255,255,255," + (0.010 + r * 0.016) + ")";
          g.fillRect(rx + tx * t + 1, ry + ty * t + 1, t - 2, 1);
          if (r > 0.93) {
            g.fillStyle = "rgba(0,0,0,0.10)";
            g.fillRect(rx + tx * t, ry + ty * t, t, t);
          }
        }
      }
      return;
    }
    /* CONCRETE: blotchy, with the odd hairline crack. */
    const blot = Math.max(4, cell * 0.9);
    for (let y = ry; y < ry + rh; y += blot) {
      for (let x = rx; x < rx + rw; x += blot) {
        const r = hash2(x, y);
        g.fillStyle = r > 0.5
          ? "rgba(255,255,255," + ((r - 0.5) * 0.05) + ")"
          : "rgba(0,0,0," + ((0.5 - r) * 0.14) + ")";
        g.fillRect(x, y, blot, blot);
      }
    }
    g.strokeStyle = "rgba(0,0,0,0.22)";
    g.lineWidth = 1;
    for (let k = 0; k < 3; k++) {
      let x = rx + hash2b(k, 1, 21) * rw;
      let y = ry + hash2b(k, 2, 22) * rh;
      g.beginPath();
      g.moveTo(x, y);
      for (let seg = 0; seg < 5; seg++) {
        x += (hash2b(k, seg, 31) - 0.5) * cell * 3;
        y += (hash2b(k, seg, 37) - 0.5) * cell * 3;
        g.lineTo(x, y);
      }
      g.stroke();
    }
  }

  private draw() {
    if (!this.data) return;
    const { ctx, cell } = this;
    const { plan, people, picked } = this.data;
    const pad = this.padding();

    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.save();
    ctx.translate(pad.x, pad.y);

    /* THE GROUND, IN ONE BLIT. Floors, their materials, the traffic lanes,
       the skirting and the wall-join occlusion are all baked (see bakeFloor)
       because none of them can change while the pane is open. */
    this.bakeFloor();
    if (this.baked) ctx.drawImage(this.baked, 0, 0);

    /* One lookup for the whole pass rather than a find() per room per frame. */
    const bySeat = new Map(people.map((p) => [p.seat, p]));
    const occFor = (seat?: string) => (seat ? bySeat.get(seat) : undefined);

    /* THE LIGHT IN EACH ROOM, WHICH IS THE PART THAT MEANS SOMETHING and is
       therefore the part that is NOT baked. A soft pool centred a little above
       the middle - the fixture hangs over the desks, not over the doorway -
       falling off to nothing at the walls.

       IT IS BRIGHTER WHEN THE SEAT IS WORKING, which makes the lighting say
       something true rather than decorate: a floor glanced at from across the
       desk reads as "two rooms are lit, the rest have gone home" before any
       label is read. An empty room is dimmer, never dark - a room nobody can
       see into cannot be inspected. */
    for (const room of plan.rooms) {
      const rx = room.rect.x * cell;
      const ry = room.rect.y * cell;
      const rw = room.rect.w * cell;
      const rh = room.rect.h * cell;
      const lit = occFor(room.seat);
      const state = lit?.state;
      const warm = state === "running" ? 0.20
        : state === "delivering" || state === "dispatched" ? 0.13
        : state && state !== "idle" ? 0.10
        : 0.055;

      const g = ctx.createRadialGradient(
        rx + rw * 0.5, ry + rh * 0.38, cell * 0.4,
        rx + rw * 0.5, ry + rh * 0.38, Math.max(rw, rh) * 0.72);
      g.addColorStop(0, "rgba(255,236,205," + warm + ")");
      g.addColorStop(0.55, "rgba(255,226,190," + warm * 0.42 + ")");
      g.addColorStop(1, "rgba(255,220,180,0)");
      ctx.fillStyle = g;
      ctx.fillRect(rx, ry, rw, rh);

      /* Selected room: a soft wash in the seat's colour rather than a border,
         because a border is a rectangle drawn on a floor and a wash is a room
         somebody is looking at. */
      if (room.seat && room.seat === picked) {
        const c = SEAT_HEX[room.seat] || "#888";
        ctx.save();
        ctx.globalAlpha = 0.10;
        ctx.fillStyle = c;
        ctx.fillRect(rx, ry, rw, rh);
        ctx.restore();
        ctx.strokeStyle = c;
        ctx.globalAlpha = 0.45;
        ctx.lineWidth = Math.max(1, cell * 0.08);
        ctx.strokeRect(rx + 1, ry + 1, rw - 2, rh - 2);
        ctx.globalAlpha = 1;
      }

      /* A FAILED ROOM IS LIT RED FROM INSIDE rather than outlined. An outline
         is a badge on a rectangle; a room glowing the wrong colour is a room
         with a problem in it, which is the thing being reported. */
      if (state === "failed") {
        const fg = ctx.createRadialGradient(
          rx + rw * 0.5, ry + rh * 0.5, cell * 0.3,
          rx + rw * 0.5, ry + rh * 0.5, Math.max(rw, rh) * 0.7);
        const pulse = 0.10 + 0.05 * Math.sin(this.clock / 520);
        fg.addColorStop(0, "rgba(239,68,68," + pulse + ")");
        fg.addColorStop(1, "rgba(239,68,68,0)");
        ctx.fillStyle = fg;
        ctx.fillRect(rx, ry, rw, rh);
      }
    }

    /* VERTICAL WALLS FIRST, IN THE BASE LAYER. Their faces are edge-on to this
       camera, so they are a thin lit ridge rather than a surface, and nothing
       meaningful ever has to be drawn in front of one. They go down before the
       y-sorted pass so a character crossing a doorway is never cut in half by
       the jamb beside it. */
    for (const w of plan.walls) {
      if (w.dir !== "v") continue;
      const x = w.a * cell;
      const y0 = w.s * cell;
      const h = (w.e - w.s) * cell;
      const t = Math.max(2, cell * 0.16);
      ctx.fillStyle = "rgba(0,0,0,0.30)";
      ctx.fillRect(x - t / 2 + t, y0, t * 0.6, h);
      ctx.fillStyle = WALL_FACE;
      ctx.fillRect(x - t / 2, y0, t, h);
      ctx.fillStyle = WALL_TOP;
      ctx.fillRect(x - t / 2, y0, t * 0.45, h);
    }

    /* Y-sorted drawables: walls, props and characters in ONE list.
       
       THE WALLS BELONG IN THIS SORT AND THAT IS THE WHOLE POINT OF THE PASS. A
       horizontal wall is a solid object standing between the camera and
       whatever is north of it, so a character in the room above must be
       OCCLUDED by it - drawing every wall first, as this did, painted every
       character in front of every wall and the building went flat again the
       moment anybody stood near one. */
    type Drawable = { y: number; draw: () => void };
    const drawables: Drawable[] = [];

    /* HORIZONTAL WALLS, EXTRUDED. Three surfaces: the shadow it throws south
       onto the floor, the face turned toward the camera, and the cap you look
       down on. */
    for (const w of plan.walls) {
      if (w.dir !== "h") continue;
      drawables.push({
        y: w.a + WALL_H,
        draw: () => {
          const x = w.s * cell;
          const wide = (w.e - w.s) * cell;
          const yTop = w.a * cell;
          const face = WALL_H * cell;
          const capH = Math.max(2, cell * 0.2);

          /* The shadow on the floor, offset along the one light direction. */
          const sg = ctx.createLinearGradient(0, yTop + face, 0, yTop + face + cell * 0.7);
          sg.addColorStop(0, "rgba(0,0,0,0.38)");
          sg.addColorStop(1, "rgba(0,0,0,0)");
          ctx.fillStyle = sg;
          ctx.fillRect(x + LIGHT_DX * cell, yTop + face, wide, cell * 0.7);

          /* The face, shaded down its height so it is a surface rather than a
             band of flat colour. */
          const fg = ctx.createLinearGradient(0, yTop, 0, yTop + face);
          fg.addColorStop(0, WALL_FACE_LIT);
          fg.addColorStop(1, WALL_FACE);
          ctx.fillStyle = fg;
          ctx.fillRect(x, yTop, wide, face);

          /* The cap. */
          ctx.fillStyle = WALL_TOP;
          ctx.fillRect(x, yTop - capH, wide, capH);
          ctx.fillStyle = "rgba(255,255,255,0.07)";
          ctx.fillRect(x, yTop - capH, wide, Math.max(1, capH * 0.4));
        },
      });
    }

    /* Props. */
    const allProps = [
      ...plan.rooms.flatMap(r => r.props),
      ...plan.props,
    ];
    for (const prop of allProps) {
      const py = prop.y + prop.h;
      drawables.push({ y: py, draw: () => this.drawProp(prop) });
    }

    /* Characters. */
    for (const p of people) {
      const ch = this.chars.get(p.seat);
      if (!ch) continue;
      drawables.push({ y: ch.y, draw: () => this.drawCharacter(p, ch) });
    }

    drawables.sort((a, b) => a.y - b.y);
    for (const d of drawables) d.draw();

    /* THE OUTER WALL, after the building rather than before it, because it is
       the one wall the whole floor sits inside and nothing is ever in front of
       it. */
    ctx.strokeStyle = WALL_OUTER;
    ctx.lineWidth = Math.max(3, cell * 0.22);
    ctx.strokeRect(0, 0, plan.cols * cell, plan.rows * cell);

    /* THE SPEECH BUBBLE, above everything including the nameplates, because a
       line half behind a plate is a line nobody reads.

       THIS IS DRAWN AS SPEECH, WITH A TAIL, POINTING AT A PERSON - which the
       floor plan explicitly forbade, and the ban is lifted by the user rather
       than forgotten. Worth recording WHY it was safe to lift: the objection
       was never the shape of the box, it was that a canned line could be
       mistaken for an agent's real output. That is prevented by WHEN this can
       exist, not by how it looks. floorIsQuiet gates it on every seat idle,
       nothing running, queued, dispatched or in review, no gate, no question,
       the director not mid-reply and nothing on the handover desk - so at the
       moment a bubble is on screen there is provably no real output anywhere
       for it to be confused with, and the instant any of that changes the line
       is cleared on the same frame. */
    /* THE BUBBLE FADES RATHER THAN BLINKING. A line that appears instantly at
       full opacity reads as a UI element being switched on; one that lifts and
       fades in over a third of a second reads as somebody starting to talk. The
       fade runs on the renderer's own clock, not on a React transition, because
       the line arrives from a hook that fires every fifteen seconds and nothing
       should re-render for an animation. */
    if (this.data.banter && this.data.banter.line) {
      const ch = this.chars.get(this.data.banter.seat);
      if (ch) {
        if (this.bubbleFor !== this.data.banter.line) {
          this.bubbleFor = this.data.banter.line;
          this.bubbleAt = this.clock;
        }
        const age = this.clock - this.bubbleAt;
        const fade = Math.min(1, age / 320);
        this.drawBubble(ch, this.data.banter.line, fade);
      }
    } else {
      this.bubbleFor = "";
    }

    /* ── the air ──────────────────────────────────────────────────────────────

       DUST IN THE LIGHT, and it is the only thing on this floor that moves when
       absolutely nothing is happening. A building where every character is idle
       and every animation is a two-pixel breath reads as a screenshot; a few
       motes drifting through the corridor light read as a room with air in it.
       This is the cheapest possible "alive", and it costs nothing to be wrong
       about.

       THE MOTES ARE A LOOP, NOT A SIMULATION. Each one's position is a pure
       function of the clock and its own index, so there is no particle list to
       update, nothing to allocate per frame, and it never drifts out of sync
       with itself. They rise slowly and wrap, because dust in a lit room is
       mostly convection.

       DRAWN OVER THE BUILDING BUT UNDER THE TEXT. Dust in front of a nameplate
       would be dirt on the label. */
    const motes = 34;
    for (let i = 0; i < motes; i++) {
      const seedX = hash2(i, 1);
      const seedY = hash2(i, 2);
      const speed = 0.35 + hash2(i, 3) * 0.5;
      const drift = (hash2(i, 4) - 0.5) * 1.4;
      const t = (this.clock / 1000) * speed;
      const x = ((seedX + Math.sin(t * 0.5 + i) * 0.012 + drift * 0.004 * t)
                 % 1 + 1) % 1 * plan.cols * cell;
      const y = ((seedY - t * 0.03) % 1 + 1) % 1 * plan.rows * cell;
      /* Brighter where the light is, which is a fair approximation of dust
         only being visible in a beam. */
      const lit = 0.10 + 0.16 * Math.abs(Math.sin(t + i));
      ctx.fillStyle = "rgba(255,240,214," + lit.toFixed(3) + ")";
      const r = Math.max(0.7, cell * 0.035);
      ctx.fillRect(x, y, r, r);
    }

    /* Nameplates, on top of everything. */
    for (const room of plan.rooms) {
      this.drawPlate(room, people.find(p => p.seat === room.seat));
    }

    /* Queue label. */
    if (people.some(p => p.zone === "door") && plan.queue.length > 0) {
      const qx = (plan.queue[0].x - 5.2) * cell;
      const qy = plan.queue[0].y * cell;
      ctx.font = `bold ${Math.round(cell * 0.42)}px "Segoe UI", Arial, sans-serif`;
      ctx.fillStyle = "#fbbf24";
      ctx.globalAlpha = 0.9;
      ctx.fillText("WAITING ON YOU", qx, qy);
      ctx.globalAlpha = 1;
    }

    ctx.restore();
  }

  private drawProp(prop: Prop) {
    const { ctx, cell } = this;
    const px = prop.x * cell;
    const py = prop.y * cell;
    const pw = prop.w * cell;
    const ph = prop.h * cell;

    if (prop.sprite) {
      const img = getImg(prop.sprite);
      if (img) {
        /* THE SPRITE KEEPS ITS OWN PROPORTIONS AND STANDS ON THE BACK EDGE OF
           ITS RECT. The rect is the FOOTPRINT the planner reserved - how much
           floor the desk takes and where a character can stand - and it is not
           the shape of the drawing: a desk drawn taller than it is deep,
           stretched to fill a wide shallow footprint, is a slab. Fitted inside
           and pinned to the far edge, the drawing sits where the furniture
           does and the near floor in front of it stays walkable. */
        const scale = Math.min(pw / img.naturalWidth, ph / img.naturalHeight);
        const dw = img.naturalWidth * scale;
        const dh = img.naturalHeight * scale;

        /* A CONTACT SHADOW UNDER EVERY SPRITE. Without it a prop is a picture
           lying on the carpet rather than a thing standing on it, and the whole
           room goes flat no matter how good the sprite is. */
        ctx.fillStyle = "rgba(0,0,0,0.30)";
        ctx.beginPath();
        ctx.ellipse(px + pw * 0.5 + LIGHT_DX * cell,
                    py + ph - dh * 0.04 + LIGHT_DY * cell * 0.1,
                    dw * 0.44, Math.max(1.5, dh * 0.07), 0, 0, Math.PI * 2);
        ctx.fill();

        /* KNOCKED BACK, AND THIS IS WHAT MAKES THE SPRITES BELONG TO THE ROOM.
           They are drawn at full brightness by their generator, while the room
           around them is lit at a fraction of that - so a white couch on a dim
           carpet reads as a sticker on a photograph. Dropping the alpha lets
           the floor's own value show through and pulls every prop into the same
           light as the building.

           CHARACTERS ARE NOT KNOCKED BACK, deliberately: the cast is the thing
           the reader is meant to look at, so it is the one layer drawn at full
           strength, and the contrast against the receded furniture is what
           makes a person pop out of a room rather than sit in a collage. */
        ctx.globalAlpha = 0.88;
        ctx.imageSmoothingEnabled = false;
        ctx.drawImage(img, px + (pw - dw) / 2, py + ph - dh, dw, dh);
        ctx.imageSmoothingEnabled = true;
        ctx.globalAlpha = 1;
        return;
      }
    }

    if (prop.kind === "radio") {
      this.drawRadio(px, py, pw, ph);
      return;
    }

    /* ── the craft props ───────────────────────────────────────────────────────

       EACH OF THESE IS WHY ITS ROOM READS AS ITS DISCIPLINE. The generic shaded
       box below is fine for a filing cabinet against a wall; it is useless for
       the thing that is supposed to tell you at a glance that this room is the
       audio booth and that one is the server room. Nine rooms of identical
       boxes was the single biggest reason the floor looked flat.

       THE WALL-MOUNTED ONES CAST NO FLOOR SHADOW, and that is the tell that
       makes them read as hung rather than standing. Foam, swatches, cards,
       frames and screens are all flush to a wall; racks, easels, arcades and
       broken panels stand on the floor and get the shadow. */

    /* ACOUSTIC FOAM: a wedge panel, so it needs a texture rather than a fill.
       Diagonal hatching at a shallow angle reads as the wedge profile from
       across the pane, which is all it has to do. */
    if (prop.kind === "foam") {
      ctx.fillStyle = "#3e3c47";
      ctx.fillRect(px, py, pw, ph);
      ctx.save();
      ctx.beginPath();
      ctx.rect(px, py, pw, ph);
      ctx.clip();
      ctx.strokeStyle = "rgba(255,255,255,0.07)";
      ctx.lineWidth = 1;
      for (let x = px - ph; x < px + pw; x += Math.max(3, cell * 0.22)) {
        ctx.beginPath();
        ctx.moveTo(x, py + ph);
        ctx.lineTo(x + ph, py);
        ctx.stroke();
      }
      ctx.restore();
      ctx.fillStyle = "rgba(0,0,0,0.25)";
      ctx.fillRect(px, py + ph - 1, pw, 1);
      return;
    }

    /* MIC ON A BOOM: a vertical arm, a diagonal boom, a capsule with a rim lit
       from the building's one light direction. */
    if (prop.kind === "mic") {
      const cx0 = px + pw * 0.5;
      ctx.strokeStyle = "#2f2f36";
      ctx.lineWidth = Math.max(1.5, cell * 0.06);
      ctx.beginPath();
      ctx.moveTo(cx0, py + ph);
      ctx.lineTo(cx0, py + ph * 0.35);
      ctx.lineTo(px + pw * 0.05, py + ph * 0.1);
      ctx.stroke();
      ctx.fillStyle = "#3a3a44";
      ctx.beginPath();
      ctx.ellipse(px + pw * 0.05, py + ph * 0.12, Math.max(2, pw * 0.19),
                  Math.max(3, ph * 0.11), 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.22)";
      ctx.beginPath();
      ctx.ellipse(px + pw * 0.05 + LIGHT_DX * cell * 0.3, py + ph * 0.09,
                  Math.max(1, pw * 0.09), Math.max(1, ph * 0.05), 0, 0, Math.PI * 2);
      ctx.fill();
      return;
    }

    /* EASEL: an A-frame with a canvas on it, and a couple of strokes on the
       canvas so it reads as a work in progress rather than a blank board. */
    if (prop.kind === "easel") {
      ctx.fillStyle = "rgba(0,0,0,0.30)";
      ctx.beginPath();
      ctx.ellipse(px + pw * 0.5 + LIGHT_DX * cell, py + ph + LIGHT_DY * cell * 0.3,
                  pw * 0.45, Math.max(1.5, ph * 0.09), 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#4a3f33";
      ctx.lineWidth = Math.max(1.5, pw * 0.07);
      ctx.beginPath();
      ctx.moveTo(px + pw * 0.18, py + ph);
      ctx.lineTo(px + pw * 0.5, py + ph * 0.12);
      ctx.lineTo(px + pw * 0.82, py + ph);
      ctx.stroke();
      const cw = pw * 0.8, chh = ph * 0.47;
      const cx1 = px + pw * 0.1, cy1 = py + ph * 0.15;
      ctx.fillStyle = "#e8e2d4";
      ctx.fillRect(cx1, cy1, cw, chh);
      ctx.strokeStyle = "#3a332b";
      ctx.lineWidth = Math.max(1, pw * 0.05);
      ctx.strokeRect(cx1, cy1, cw, chh);
      /* Two strokes of paint, deterministic so they never flicker. */
      const hues = ["#c0563e", "#3f6ea8"];
      for (let i = 0; i < 2; i++) {
        ctx.fillStyle = hues[i];
        ctx.globalAlpha = 0.75;
        ctx.fillRect(cx1 + cw * (0.12 + i * 0.24), cy1 + chh * (0.55 + i * 0.14),
                     cw * 0.42, Math.max(1, chh * 0.1));
      }
      ctx.globalAlpha = 1;
      return;
    }

    /* SWATCHES: a card of colour chips pinned to a wall. Two rows, the lower
       one darker, which is how a real swatch card is printed. */
    if (prop.kind === "swatches") {
      ctx.fillStyle = "#2f2b26";
      ctx.fillRect(px, py, pw, ph);
      const chips = ["#c0563e", "#d08a3a", "#cbb03a", "#4f9a54", "#3d8f96", "#7a5aa8"];
      const cw = pw / chips.length;
      for (let i = 0; i < chips.length; i++) {
        ctx.fillStyle = chips[i];
        ctx.fillRect(px + i * cw + 1, py + ph * 0.12, cw - 2, ph * 0.36);
        ctx.globalAlpha = 0.55;
        ctx.fillRect(px + i * cw + 1, py + ph * 0.54, cw - 2, ph * 0.32);
        ctx.globalAlpha = 1;
      }
      ctx.fillStyle = "rgba(255,255,255,0.18)";
      ctx.fillRect(px, py, pw, 1);
      return;
    }

    /* INDEX CARDS ON STRING: the writers' room in one prop. The sag on the
       string is what stops it reading as a row of stickers. */
    if (prop.kind === "cards") {
      const n = Math.max(4, Math.round(pw / (cell * 0.42)));
      const step = pw / n;
      ctx.strokeStyle = "#8a4038";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px, py + ph * 0.18);
      ctx.quadraticCurveTo(px + pw * 0.5, py + ph * 0.34, px + pw, py + ph * 0.18);
      ctx.stroke();
      for (let i = 0; i < n; i++) {
        const cx1 = px + i * step + step * 0.5;
        const sag = Math.sin((i / n) * Math.PI) * ph * 0.13;
        const cy1 = py + ph * 0.22 + sag;
        const tilt = (hash2(i, 7) - 0.5) * 0.22;
        ctx.save();
        ctx.translate(cx1, cy1);
        ctx.rotate(tilt);
        const w = step * 0.72, h = ph * 0.5;
        ctx.fillStyle = "#ded7c4";
        ctx.fillRect(-w / 2, 0, w, h);
        ctx.fillStyle = "rgba(0,0,0,0.35)";
        for (let k = 0; k < 2; k++) {
          ctx.fillRect(-w * 0.32, h * (0.3 + k * 0.26), w * 0.64, 1);
        }
        ctx.restore();
      }
      return;
    }

    /* DEVICE SHELF: a rack of handhelds, each with a lit screen. QA's whole
       identity is a wall of other people's hardware. */
    if (prop.kind === "devices") {
      ctx.fillStyle = "rgba(0,0,0,0.28)";
      ctx.fillRect(px + pw * 0.05, py + ph * 0.92, pw * 0.9, ph * 0.12);
      ctx.fillStyle = "#33313a";
      ctx.fillRect(px, py, pw, ph);
      ctx.fillStyle = "rgba(255,255,255,0.10)";
      ctx.fillRect(px, py, pw, Math.max(1, ph * 0.12));
      const cols = Math.max(3, Math.round(pw / (cell * 0.55)));
      const rows = 2;
      const gw = pw / cols, gh = ph / rows;
      for (let r0 = 0; r0 < rows; r0++) {
        for (let c0 = 0; c0 < cols; c0++) {
          const h = hash2(c0, r0);
          ctx.fillStyle = "#15171c";
          ctx.fillRect(px + c0 * gw + gw * 0.18, py + r0 * gh + gh * 0.18,
                       gw * 0.64, gh * 0.6);
          /* A couple of the screens are on. Deterministic which ones. */
          if (h > 0.55) {
            ctx.fillStyle = h > 0.85 ? "#4ade80" : "#3d6a9a";
            ctx.globalAlpha = 0.5 + 0.2 * Math.sin(this.clock / 900 + c0);
            ctx.fillRect(px + c0 * gw + gw * 0.24, py + r0 * gh + gh * 0.24,
                         gw * 0.52, gh * 0.44);
            ctx.globalAlpha = 1;
          }
        }
      }
      return;
    }

    /* THE BROKEN THING. Tilted, cracked, and one edge still faintly lit, which
       is what makes it read as dead hardware rather than a dropped box. */
    if (prop.kind === "broken") {
      ctx.save();
      ctx.translate(px + pw * 0.5, py + ph * 0.5);
      ctx.rotate(0.21);
      ctx.translate(-pw * 0.5, -ph * 0.5);
      ctx.fillStyle = "rgba(0,0,0,0.32)";
      ctx.fillRect(pw * 0.08, ph * 0.92, pw * 0.9, ph * 0.16);
      ctx.fillStyle = "#2f3542";
      ctx.fillRect(0, 0, pw, ph);
      ctx.fillStyle = "#12141a";
      ctx.fillRect(pw * 0.1, ph * 0.12, pw * 0.8, ph * 0.66);
      ctx.strokeStyle = "rgba(255,255,255,0.55)";
      ctx.lineWidth = 1;
      const ox = pw * 0.4, oy = ph * 0.42;
      for (const [dx, dy] of [[-0.34, 0.26], [0.3, -0.2], [0.12, 0.34]]) {
        ctx.beginPath();
        ctx.moveTo(ox, oy);
        ctx.lineTo(ox + pw * dx, oy + ph * dy);
        ctx.stroke();
      }
      ctx.fillStyle = "rgba(255,106,61,0.5)";
      ctx.fillRect(0, ph * 0.78, pw, 1);
      ctx.restore();
      return;
    }

    /* SERVER RACK: slot lines and a column of LEDs, one of them blinking. The
       blink is the only reason the tech room ever looks awake. */
    if (prop.kind === "rack") {
      ctx.fillStyle = "rgba(0,0,0,0.34)";
      ctx.beginPath();
      ctx.ellipse(px + pw * 0.5 + LIGHT_DX * cell, py + ph + LIGHT_DY * cell * 0.3,
                  pw * 0.5, Math.max(1.5, ph * 0.07), 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#2c3038";
      ctx.fillRect(px, py, pw, ph);
      ctx.fillStyle = "rgba(255,255,255,0.10)";
      ctx.fillRect(px, py, pw, Math.max(1, ph * 0.06));
      const slots = 7;
      for (let i = 1; i < slots; i++) {
        ctx.fillStyle = "rgba(255,255,255,0.06)";
        ctx.fillRect(px + pw * 0.06, py + (ph / slots) * i, pw * 0.88, 1);
      }
      for (let i = 0; i < slots; i++) {
        const on = hash2(i, 3) > 0.35;
        if (!on) continue;
        const blink = hash2(i, 9) > 0.72
          ? (Math.sin(this.clock / (300 + i * 90)) > 0 ? 1 : 0.25) : 1;
        ctx.globalAlpha = blink;
        ctx.fillStyle = hash2(i, 5) > 0.5 ? "#4ade80" : "#f59e0b";
        ctx.fillRect(px + pw * 0.74, py + (ph / slots) * i + ph * 0.04,
                     Math.max(1.5, pw * 0.07), Math.max(1.5, ph * 0.035));
        ctx.globalAlpha = 1;
      }
      return;
    }

    /* FLOOR CABLING. Flat, no box, no shadow - it has to read as something you
       walk over, because characters do route across it. */
    if (prop.kind === "cables") {
      ctx.save();
      ctx.globalAlpha = 0.5;
      ctx.strokeStyle = "#22252b";
      ctx.lineWidth = Math.max(1.5, cell * 0.07);
      ctx.lineCap = "round";
      for (let i = 0; i < 4; i++) {
        const y0 = py + ph * (0.15 + i * 0.23);
        ctx.beginPath();
        ctx.moveTo(px, y0);
        ctx.bezierCurveTo(px + pw * 0.3, y0 + ph * (hash2(i, 2) - 0.5) * 0.6,
                          px + pw * 0.7, y0 + ph * (hash2(i, 4) - 0.5) * 0.6,
                          px + pw, y0 + ph * 0.05);
        ctx.stroke();
      }
      ctx.restore();
      return;
    }

    /* WALL SCREEN: a bezel, a face with a gradient, and one scan band. The band
       drifts, which is the cheapest possible "this display is on". */
    if (prop.kind === "screen") {
      ctx.fillStyle = "#1b1e24";
      ctx.fillRect(px, py, pw, ph);
      const fx = px + pw * 0.06, fy = py + ph * 0.12;
      const fw = pw * 0.88, fh = ph * 0.7;
      const g2 = ctx.createLinearGradient(0, fy, 0, fy + fh);
      g2.addColorStop(0, "#2b3a4d");
      g2.addColorStop(1, "#16202b");
      ctx.fillStyle = g2;
      ctx.fillRect(fx, fy, fw, fh);
      const band = ((this.clock / 2600) % 1) * fh;
      ctx.fillStyle = "rgba(255,255,255,0.07)";
      ctx.fillRect(fx, fy + band, fw, Math.max(1, fh * 0.1));
      ctx.fillStyle = "rgba(0,0,0,0.35)";
      ctx.fillRect(px, py + ph, pw, Math.max(1, cell * 0.12));
      return;
    }

    /* ARCADE CABINET: a lit screen over a control shelf. Upright, so it takes
       the standing shadow. */
    if (prop.kind === "arcade") {
      ctx.fillStyle = "rgba(0,0,0,0.34)";
      ctx.beginPath();
      ctx.ellipse(px + pw * 0.5 + LIGHT_DX * cell, py + ph + LIGHT_DY * cell * 0.3,
                  pw * 0.55, Math.max(1.5, ph * 0.06), 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#2a2620";
      ctx.fillRect(px, py, pw, ph);
      ctx.fillStyle = "rgba(255,255,255,0.10)";
      ctx.fillRect(px, py, pw, Math.max(1, ph * 0.05));
      ctx.fillStyle = "#1b2430";
      ctx.fillRect(px + pw * 0.12, py + ph * 0.08, pw * 0.76, ph * 0.4);
      ctx.fillStyle = "#ff6a3d";
      ctx.globalAlpha = 0.3 + 0.12 * Math.sin(this.clock / 700);
      ctx.fillRect(px + pw * 0.16, py + ph * 0.12, pw * 0.68, ph * 0.32);
      ctx.globalAlpha = 1;
      ctx.fillStyle = "#3a332b";
      ctx.fillRect(px + pw * 0.08, py + ph * 0.56, pw * 0.84, ph * 0.16);
      return;
    }

    /* A FRAMED THING ON A WALL. No floor shadow - it is hanging. */
    if (prop.kind === "frame") {
      ctx.fillStyle = "#3a332c";
      ctx.fillRect(px, py, pw, ph);
      ctx.fillStyle = "#cbb996";
      ctx.fillRect(px + pw * 0.12, py + ph * 0.12, pw * 0.76, ph * 0.76);
      ctx.fillStyle = "#4a5568";
      ctx.fillRect(px + pw * 0.28, py + ph * 0.28, pw * 0.44, ph * 0.44);
      ctx.fillStyle = "rgba(255,255,255,0.18)";
      ctx.fillRect(px, py, pw, 1);
      return;
    }
    /* ── the generic solid ─────────────────────────────────────────────────────

       NO BESPOKE DRAWING FOR THIS KIND, SO IT IS DRAWN AS A BOX WITH VOLUME.
       The first version filled one flat rectangle with a lit strip on top,
       which at a glance is a coloured slab lying ON the carpet rather than a
       cabinet standing on it - and nine rooms of coloured slabs was a large
       part of why the floor read as a diagram.

       THE CAMERA IS THE REASON THIS WORKS. It is a high three-quarter, so a box
       shows its TOP and its FRONT and nothing else: the top is a squashed
       rectangle offset upward by the object's height, the front is the face
       below it, and the two meeting along a lit edge is the entire illusion.
       Three fills and a shadow, and the prop stands up.

       HEIGHT IS PER KIND because a rug is flat, a counter is waist high and a
       cabinet is taller than a person. Getting that wrong is what makes a
       drawing of furniture look like a floor plan with colours on it. */
    const TALL: Record<string, number> = {
      cabinet: 0.62, fridge: 0.7, printer: 0.34, counter: 0.42, table: 0.3,
      desk: 0.3, sofa: 0.36, monitor: 0.5, chair: 0.4, partition: 0.8,
      coffee: 0.45, cooler: 0.55, sink: 0.4, board: 0.7,
    };
    const lift = (TALL[prop.kind] ?? 0.4) * cell;

    /* The contact shadow, along the building's one light direction. Tight to
       the base, because a shadow that spreads reads as the object floating. */
    ctx.fillStyle = "rgba(0,0,0,0.34)";
    ctx.beginPath();
    ctx.ellipse(px + pw * 0.5 + LIGHT_DX * cell, py + ph + LIGHT_DY * cell * 0.12,
                pw * 0.52, Math.max(1.5, ph * 0.18), 0, 0, Math.PI * 2);
    ctx.fill();

    const PROP_COLORS: Record<string, string> = {
      desk: "#6b5340", monitor: "#3d4657", chair: "#4a4453",
      partition: "#4a4958", table: "#6b5340", counter: "#4f4a44",
      fridge: "#4a5058", coffee: "#5a4a5e", cooler: "#42505e",
      printer: "#4c4a52", plant: "#3c6b46", board: "#5c5a62",
      cabinet: "#4a4038", rug: "#6b5a44", sofa: "#5a4a5e",
      sink: "#42505e",
    };
    const base = PROP_COLORS[prop.kind] || "#4a4850";

    if (prop.kind === "plant") {
      /* Foliage, not a box: an ellipse with a lighter crown, over a pot. */
      ctx.fillStyle = "#3a2f28";
      ctx.fillRect(px + pw * 0.3, py + ph - lift * 0.5, pw * 0.4, lift * 0.5);
      ctx.fillStyle = base;
      ctx.beginPath();
      ctx.ellipse(px + pw / 2, py + ph - lift * 0.75, pw * 0.48, ph * 0.42,
                  0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.12)";
      ctx.beginPath();
      ctx.ellipse(px + pw * 0.42, py + ph - lift * 0.95, pw * 0.24, ph * 0.18,
                  0, 0, Math.PI * 2);
      ctx.fill();
      return;
    }
    if (prop.kind === "rug") {
      /* Flat on the floor by definition, so no lift and no shadow of its own -
         it IS the floor for as long as it is drawn. A border, because a rug
         with no edge is a stain. */
      ctx.globalAlpha = 0.14;
      ctx.fillStyle = base;
      ctx.fillRect(px, py, pw, ph);
      ctx.globalAlpha = 0.22;
      ctx.strokeStyle = base;
      ctx.lineWidth = Math.max(1, cell * 0.08);
      ctx.strokeRect(px + 1, py + 1, pw - 2, ph - 2);
      ctx.globalAlpha = 1;
      return;
    }

    /* THE FRONT FACE, shaded down its height so it is a surface and not a band
       of colour. */
    const faceTop = py + ph - lift;
    const fg = ctx.createLinearGradient(0, faceTop, 0, py + ph);
    fg.addColorStop(0, base);
    fg.addColorStop(1, shade(base, -0.35));
    ctx.fillStyle = fg;
    ctx.fillRect(px, faceTop, pw, lift);

    /* THE TOP FACE. Squashed, because it is seen at a steep angle, and lighter
       because it is the surface facing the ceiling light. */
    ctx.fillStyle = shade(base, 0.26);
    ctx.fillRect(px, py + ph - lift - ph * 0.55, pw, ph * 0.55);

    /* The lit edge where top meets front - the single line doing most of the
       work in the whole illusion. */
    ctx.fillStyle = "rgba(255,255,255,0.16)";
    ctx.fillRect(px, faceTop - 1, pw, 1);

    /* A panel seam on anything tall enough to have one, so a big cabinet is not
       one undifferentiated colour. */
    if (lift > cell * 0.45) {
      ctx.fillStyle = "rgba(0,0,0,0.22)";
      ctx.fillRect(px + pw * 0.5, faceTop + 1, 1, lift - 2);
      ctx.fillStyle = "rgba(255,255,255,0.07)";
      ctx.fillRect(px + pw * 0.12, faceTop + lift * 0.42, pw * 0.24, 1);
      ctx.fillRect(px + pw * 0.62, faceTop + lift * 0.42, pw * 0.24, 1);
    }
  }

  private drawRadio(px: number, py: number, pw: number, ph: number) {
    const { ctx } = this;
    const on = !!this.data?.musicOn;
    const hot = this.hoverRadio && this.radioLive;
    const t = this.clock;

    ctx.fillStyle = "rgba(0,0,0,0.35)";
    ctx.fillRect(px + pw * 0.05, py + ph * 0.88, pw * 0.9, ph * 0.18);

    /* The body. */
    ctx.fillStyle = on ? "#5a4a3a" : "#403a34";
    ctx.fillRect(px, py, pw, ph);
    ctx.fillStyle = "rgba(255,255,255,0.16)";
    ctx.fillRect(px, py, pw, Math.max(1, ph * 0.2));

    /* Two speaker cones and a dial between them. */
    const r = Math.min(pw, ph) * 0.19;
    const cy = py + ph * 0.58;
    for (const cxp of [px + pw * 0.26, px + pw * 0.74]) {
      ctx.beginPath();
      ctx.arc(cxp, cy, r, 0, Math.PI * 2);
      ctx.fillStyle = on ? "#ff6a3d" : "#2a2620";
      ctx.globalAlpha = on ? 0.55 + 0.25 * Math.sin(t / 260) : 1;
      ctx.fill();
      ctx.globalAlpha = 1;
    }
    ctx.fillStyle = on ? "#ffd8a8" : "#6a625a";
    ctx.fillRect(px + pw * 0.44, cy - r * 0.5, pw * 0.12, r);

    /* The antenna, leaning the way an antenna leans. */
    ctx.strokeStyle = on ? "#ffd8a8" : "#6a625a";
    ctx.lineWidth = Math.max(1, pw * 0.035);
    ctx.beginPath();
    ctx.moveTo(px + pw * 0.86, py + ph * 0.1);
    ctx.lineTo(px + pw * 1.08, py - ph * 0.5);
    ctx.stroke();

    /* Notes, only while it is playing. Three of them on staggered phases so it
       reads as a stream rather than a pulse. */
    if (on) {
      ctx.fillStyle = "#ff6a3d";
      for (let i = 0; i < 3; i++) {
        const phase = ((t / 1400) + i / 3) % 1;
        ctx.globalAlpha = Math.max(0, 0.85 - phase);
        const nx = px + pw * (0.5 + 0.42 * Math.sin(phase * 5 + i * 2));
        const ny = py - ph * (0.25 + phase * 1.7);
        const nr = Math.max(1, pw * 0.075);
        ctx.beginPath();
        ctx.arc(nx, ny, nr, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillRect(nx + nr * 0.7, ny - nr * 2.6, Math.max(1, nr * 0.5), nr * 2.6);
      }
      ctx.globalAlpha = 1;
    }

    if (hot) {
      ctx.strokeStyle = "#ff6a3d";
      ctx.lineWidth = 1.5;
      ctx.globalAlpha = 0.8;
      ctx.strokeRect(px - 2, py - 2, pw + 4, ph + 4);
      ctx.globalAlpha = 1;
    }
  }

  private drawCharacter(p: Occupant, ch: CharState) {
    const { ctx, cell } = this;
    const px = ch.x * cell;
    const py = ch.y * cell;
    const artW = cell * 2.16;
    const artH = cell * 2.7;
    const color = SEAT_HEX[p.seat] || "#888";

    /* Shadow. */
    ctx.fillStyle = "rgba(0,0,0,0.25)";
    ctx.beginPath();
    ctx.ellipse(px, py, cell * 0.5, cell * 0.22, 0, 0, Math.PI * 2);
    ctx.fill();

    /* Sprite. */
    const cast = CAST_SEATS.has(p.seat) ? p.seat : "generic";
    const moving = ch.path.length > 0;
    const anim = moving ? "walk" : animFor(p);
    const frames = castFrames(cast, anim);
    const sheet = getImg(`/static/img/floor/${cast}/${anim}.png`);

    const speed = ANIM_SPEED[anim] || 2400;
    const frameIdx = Math.floor((ch.animTime % speed) / (speed / frames)) % frames;

    /* Idle dimming. */
    if (p.state === "idle") ctx.globalAlpha = 0.55;
    else if (p.state === "chained") ctx.globalAlpha = 0.72;

    if (sheet) {
      ctx.imageSmoothingEnabled = false;
      ctx.save();
      ctx.translate(px, py - artH);
      if (moving && ch.facingRight) {
        ctx.translate(artW / 2, 0);
        ctx.scale(-1, 1);
        ctx.translate(-artW / 2, 0);
      }
      /* Draw one frame from the strip. */
      const srcW = sheet.naturalWidth / frames;
      const srcH = sheet.naturalHeight;
      ctx.drawImage(sheet,
        frameIdx * srcW, 0, srcW, srcH,
        -artW / 2, 0, artW, artH);
      ctx.restore();
      ctx.imageSmoothingEnabled = true;
    } else {
      /* Blockout: head + body. */
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(px, py - artH * 0.72, cell * 0.16, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = color + "88";
      ctx.fillRect(px - cell * 0.26, py - artH * 0.55, cell * 0.52, cell * 0.48);
    }

    ctx.globalAlpha = 1;

    /* Carrying note. */
    if (p.carrying) {
      ctx.fillStyle = "#ddd";
      ctx.fillRect(px + cell * 0.15, py - artH * 0.3, cell * 0.3, cell * 0.35);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.strokeRect(px + cell * 0.15, py - artH * 0.3, cell * 0.3, cell * 0.35);
    }
  }

  /* ONE SPEECH BUBBLE, NINE-SLICED FROM A GENERATED SPRITE.
   *
   * THE ART IS GENERATED, THE GEOMETRY IS MEASURED. The balloon was drawn by
   * nano-banana-2 and its slice margins were read OFF ITS OWN ALPHA CHANNEL
   * rather than eyeballed - box 870x485 with 65px corner notches at the
   * generated size, scaled to a quarter and shipped at 218x121 with a 16px
   * margin. Eyeballing that number is how a nine-slice ends up with one corner
   * a pixel wider than the other three, which nothing catches until a wide line
   * stretches it.
   *
   * NINE-SLICE IS WHY THE CORNERS ARE A SEPARATE DRAW. A bubble holds one line
   * or three, so the box has to stretch - but stretching the whole sprite
   * smears the corner notches and the border thickness with it. The four
   * corners are blitted at their true size, the four edges are stretched along
   * one axis only, and the middle is stretched both ways. That is what lets one
   * 218px sprite be any size without ever looking scaled.
   *
   * THE TAIL IS A SEPARATE SPRITE, not part of the nine-slice, because it has
   * to sit under the box at the SPEAKER's x rather than at the box's centre -
   * when the clamp pushes a bubble sideways to keep it inside the building, the
   * tail still has to point at whoever is talking.
   *
   * IF THE SPRITE HAS NOT LOADED, NOTHING IS DRAWN. Not a fallback rectangle: a
   * different-looking bubble appearing for the first two seconds of a session
   * is worse than a line that starts a moment late, and the sprite is a local
   * file that will be there on the next frame.
   */
  private drawBubble(ch: CharState, line: string, fade: number) {
    const { ctx, cell } = this;
    const box = getImg(`${UI}/bubble-box.png`);
    const tail = getImg(`${UI}/bubble-tail.png`);
    if (!box || !tail) return;

    const px = (n: number) => Math.round(n);
    const font = Math.max(7, Math.round(cell * 0.34));
    ctx.font = `500 ${font}px "Segoe UI", Arial, sans-serif`;

    /* Wrapped here because a canvas has no line box: fillText runs a sentence
       off the side of the building rather than turning it over. */
    /* NARROW ON PURPOSE. A bubble is drawn over a room eight cells wide, so a
       box wider than about five cells covers the room it is standing in - which
       was the whole objection to putting banter on the floor. Narrow forces the
       wrap to two short lines, which is the shape the short pool was written
       for. */
    const maxW = Math.min(cell * 5, this.canvas.width * 0.22);
    const lines: string[] = [];
    let run = "";
    for (const word of line.split(" ")) {
      const test = run ? `${run} ${word}` : word;
      if (ctx.measureText(test).width > maxW && run) { lines.push(run); run = word; }
      else run = test;
    }
    if (run) lines.push(run);

    const M = BUBBLE_MARGIN;
    const padX = Math.max(M * 0.7, font * 0.55);
    const padY = Math.max(M * 0.5, font * 0.34);
    const lineH = px(font * 1.28);
    const textW = Math.max(...lines.map((l) => ctx.measureText(l).width));
    const boxW = Math.max(px(textW + padX * 2), M * 2 + 4);
    const boxH = Math.max(px(lines.length * lineH + padY * 2), M * 2 + 2);

    const feetY = ch.y * cell;
    const headY = feetY - cell * 2.7;
    /* SCALED FROM THE CELL, NOT FROM A CONSTANT. This read `cell / 26`, a
       reference size that meant nothing to anything else on the floor: at a
       28px cell the tail came out bigger than the character it pointed at. A
       tail is a detail on a speech bubble, so it is sized as a fraction of a
       cell like every other detail here. */
    const tailW = Math.max(3, cell * 0.42);
    const tailH = px(tailW * (tail.naturalHeight / tail.naturalWidth));
    /* Rises as it fades in. A bubble that appears at its final position reads
       as a panel being switched on; one that lifts reads as somebody speaking. */
    let bx = px(ch.x * cell - boxW / 2);
    let by = px(headY - boxH - tailH - cell * (0.15 + (1 - fade) * 0.45));

    const plan = this.data!.plan;
    const edge = px(cell * 0.25);
    bx = Math.max(edge, Math.min(bx, px(plan.cols * cell - boxW - edge)));
    by = Math.max(edge, by);

    ctx.globalAlpha = fade;
    ctx.imageSmoothingEnabled = false;

    /* The tail first, so the box's bottom edge covers where they meet. */
    const tw = px(tailW);
    const tx = Math.max(bx + M, Math.min(px(ch.x * cell - tw / 2), bx + boxW - M - tw));
    ctx.drawImage(tail, tx, by + boxH - 1, tw, tailH);

    /* THE NINE SLICES. Source margins are the measured M; destination corners
       are drawn at that same size so they never scale. */
    const sw = box.naturalWidth;
    const sh = box.naturalHeight;
    const midSW = sw - M * 2;
    const midSH = sh - M * 2;
    const midDW = boxW - M * 2;
    const midDH = boxH - M * 2;
    const put = (sx: number, sy: number, sW: number, sH: number,
                 dx: number, dy: number, dW: number, dH: number) => {
      if (sW <= 0 || sH <= 0 || dW <= 0 || dH <= 0) return;
      ctx.drawImage(box, sx, sy, sW, sH, dx, dy, dW, dH);
    };
    put(0, 0, M, M, bx, by, M, M);
    put(M, 0, midSW, M, bx + M, by, midDW, M);
    put(sw - M, 0, M, M, bx + boxW - M, by, M, M);
    put(0, M, M, midSH, bx, by + M, M, midDH);
    put(M, M, midSW, midSH, bx + M, by + M, midDW, midDH);
    put(sw - M, M, M, midSH, bx + boxW - M, by + M, M, midDH);
    put(0, sh - M, M, M, bx, by + boxH - M, M, M);
    put(M, sh - M, midSW, M, bx + M, by + boxH - M, midDW, M);
    put(sw - M, sh - M, M, M, bx + boxW - M, by + boxH - M, M, M);

    ctx.imageSmoothingEnabled = true;

    /* PALE TEXT ON THE DARK BALLOON, and NOT IN A SEAT COLOUR. Everything else
       on this floor belonging to a seat is tinted with it; this deliberately is
       not, because the one property it must keep is not looking like that seat
       reporting something real. */
    ctx.fillStyle = "#d6d2ca";
    ctx.textBaseline = "top";
    lines.forEach((l, i) => {
      ctx.fillText(l, bx + padX, by + padY + i * lineH + px(lineH * 0.08));
    });
    ctx.textBaseline = "alphabetic";
    ctx.globalAlpha = 1;
  }

  private drawPlate(room: PlanRoom, occ?: Occupant) {
    const { ctx, cell } = this;
    const px = room.rect.x * cell + cell * 0.4;
    const py = (room.rect.y + room.rect.h - 0.6) * cell;
    const color = room.seat ? SEAT_HEX[room.seat] || PLATE_TEXT : PLATE_TEXT;
    const label = room.label.toUpperCase();
    const fontSize = Math.max(8, Math.round(cell * 0.42));

    ctx.font = `600 ${fontSize}px "Segoe UI", Arial, sans-serif`;
    const tw = ctx.measureText(label).width;

    ctx.fillStyle = PLATE_BG;
    ctx.fillRect(px - 3, py - fontSize - 2, tw + 10, fontSize + 6);

    ctx.fillStyle = occ?.state === "failed" ? "#ef4444"
                  : occ?.state ? color : PLATE_TEXT;
    ctx.fillText(label, px, py);

    /* Vibe word. */
    const VIBE: Record<string, string> = {
      director: "calls", narrative: "story", gameplay: "play",
      tech: "code", art: "paint", audio: "sound",
      qa: "checks", cinematic: "cuts",
    };
    const vibe = room.seat ? VIBE[room.seat] || "desk" : "";
    if (vibe) {
      ctx.font = `400 ${Math.round(fontSize * 0.85)}px "Cascadia Code", "Fira Code", monospace`;
      ctx.fillStyle = "#5d5a55";
      ctx.fillText(vibe, px + tw + 6, py);
    }
  }
}
