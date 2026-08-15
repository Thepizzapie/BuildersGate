/* THE STUDIO FLOOR, matching the user's sketch layout:
 *
 *      +--------+---------+--------+
 *      | audio  |narrative| video  |   <- top row
 *      +--------+---------+--------+
 *      |gameplay| LOUNGE  |  qa    |   <- middle row
 *      +--------+---------+--------+
 *      |  art   |Director | (tech) |   <- bottom row
 *      +--------+---------+--------+
 *
 * NO MEETING ROOM. NO KITCHEN. NO CUBICLE BLOCKS. Craft rooms ring the
 * outside. The lounge is the middle. The Director is at the bottom.
 * Corridors run between everything.
 *
 * THE UNIT IS A CELL, NOT A PIXEL, and that is deliberate: the pane is inside
 * a rail the user drags, so the whole building has to shrink without any of it
 * being re-planned. Cells are turned into lengths as `calc(var(--cell) * n)`,
 * so ONE custom property in the stylesheet resizes the entire floor and every
 * wall, door and character stays exactly where the plan put it.
 *
 * THE ARRANGEMENT RULE IS FIXED, THE ASSIGNMENT IS COMPUTED. A project can
 * disable a seat (or invent one), so no room below is hardcoded to a craft.
 * What is fixed is the shape of the building the user settled on: craft rooms
 * ring the outside, lounge center, Director bottom center.
 *
 * WALLS ARE SHARED, NOT DOUBLED. Every room hands in its four edges, identical
 * edges collapse to one segment, and the segment is drawn ONCE on the line
 * between the two rooms. A DOORWAY IS A GAP IN A WALL.
 */

/** Cells. Everything below is in these; the renderer multiplies by --cell. */
export type Rect = { x: number; y: number; w: number; h: number };
export type Spot = { x: number; y: number };
export type Side = "n" | "s" | "e" | "w";

/** A hole in one edge. `at` is measured from the edge's start (left for n/s,
 *  top for e/w). */
export type Door = { side: Side; at: number; len: number };

export type PropKind =
  | "desk" | "monitor" | "chair" | "partition" | "table" | "counter"
  | "fridge" | "coffee" | "cooler" | "printer" | "plant" | "board"
  | "cabinet" | "rug" | "sofa" | "sink"
  /* THE CRAFT PROPS. Each of these exists because a room needed to read as its
     discipline before its label was read - an office with a desk in it is any
     office, and nine of them side by side was the flattest thing about this
     floor. Every one is drawn by hand in floorRender.drawProp: no sprite has
     been generated for them yet, and a shaded box would put us back where we
     started. */
  | "foam"      // acoustic panel, flush to a wall
  | "mic"       // condenser on a boom arm
  | "easel"     // A-frame with a canvas on it
  | "swatches"  // colour chips pinned to a wall
  | "cards"     // index cards on string
  | "devices"   // a shelf of handhelds under test
  | "broken"    // something visibly dead, on purpose
  | "rack"      // server rack, blinking
  | "cables"    // floor cabling, flat and walkable
  | "screen"    // wall display
  | "arcade"    // upright cabinet
  | "frame"
  /* THE ONE PROP THAT IS A CONTROL. Everything else on this floor is scenery
     the reader looks at; the radio in the lounge is a thing they click, and it
     turns the studio's soundtrack on and off. It carries no sprite: the
     renderer draws it, because it has to draw a DIFFERENT state depending on
     whether it is playing, and a still image cannot say that. */
  | "radio";
export type Prop = {
  kind: PropKind;
  x: number; y: number; w: number; h: number;
  /** When set, the renderer draws a sprite image instead of a CSS rectangle. */
  sprite?: string;
};

export type RoomKind = "seat" | "lounge" | "office";

export type PlanRoom = {
  id: string;
  kind: RoomKind;
  seat?: string;
  /** How this seat looks, carried from the seat table so the renderer never
   *  has to key anything off the seat's name. */
  persona?: Persona;
  label: string;
  rect: Rect;
  doors: Door[];
  props: Prop[];
  desk: Spot;
  stand: Spot;
};

/** A drawn piece of wall: a run along one line, after the doors are cut out. */
export type Wall = { dir: "h" | "v"; a: number; s: number; e: number };

export type FloorPlan = {
  cols: number;
  rows: number;
  rooms: PlanRoom[];
  walls: Wall[];
  props: Prop[];
  lounge: Spot[];
  queue: Spot[];
  office: Spot[];
  bySeat: Map<string, PlanRoom>;
};

/* HOW A SEAT LOOKS, READ FROM THE SEAT TABLE RATHER THAN DECIDED HERE.
 *
 * Every visual fact about a room used to be keyed to the seat's NAME in this
 * file and in the renderer: which sprite walks around it, what its floor is
 * made of, the word under its nameplate. That is fine right up until a project
 * renames a seat or invents one, at which point the floor has opinions about
 * "art" and nothing to say about whatever this project actually calls it.
 *
 * bgate_core.seats carries a persona per seat and /api/state ships it, so the
 * defaults are unchanged and a project can now move any of it without touching
 * this code. Every field is optional because a seat invented by a project that
 * has never set one still has to draw. */
export type Persona = {
  /** What this seat goes by. Drawn on the nameplate instead of the title, so a
   *  project's narrative seat can be called Dave. */
  name?: string;
  /** THIS SEAT'S OWN BANTER, which is the part that is actually a personality
   *  rather than a decor choice. When this character is the one talking in the
   *  lounge it says these instead of the shared pool, so two projects' art
   *  seats are different people rather than differently-carpeted rooms. */
  lines?: string[];
  /** Which character sprite set walks around the room. */
  cast?: string;
  /** carpet | tile | wood | vinyl | concrete. */
  surface?: string;
  /** The one word under the nameplate. */
  vibe?: string;
};

export type Seat = { role: string; title?: string; persona?: Persona };

/* ── grid constants ─────────────────────────────────────────────────────── */

const ROOM_W = 8;
const ROOM_H = 7;
const CORR = 3;
const DOOR_LEN = 2.5;

const C0 = 0;
const C1 = ROOM_W + CORR;                  // 11
const C2 = 2 * (ROOM_W + CORR);            // 22
const R0 = 0;
const R1 = ROOM_H + CORR;                  // 10
const R2 = 2 * (ROOM_H + CORR);            // 20

const TOTAL_W = 3 * ROOM_W + 2 * CORR;     // 30
const TOTAL_H = 3 * ROOM_H + 2 * CORR;     // 27

/* ── slot system ────────────────────────────────────────────────────────── */

/* Where each seat goes when the standard crafts are present. */
const PREFERRED_SLOT: Record<string, number> = {
  audio: 0,      // top-left
  narrative: 1,  // top-center
  cinematic: 2,  // top-right
  gameplay: 3,   // mid-left
  qa: 4,         // mid-right
  art: 5,        // bot-left
  tech: 6,       // bot-right
};

type SlotDef = { x: number; y: number; door: Side };

const SLOTS: SlotDef[] = [
  { x: C0, y: R0, door: "s" },   // 0: top-left
  { x: C1, y: R0, door: "s" },   // 1: top-center
  { x: C2, y: R0, door: "s" },   // 2: top-right
  { x: C0, y: R1, door: "e" },   // 3: mid-left
  { x: C2, y: R1, door: "w" },   // 4: mid-right
  { x: C0, y: R2, door: "n" },   // 5: bot-left
  { x: C2, y: R2, door: "n" },   // 6: bot-right
];

const ROOM_LABEL: Record<string, string> = {
  cinematic: "Video",
  qa: "QA",
};

function labelFor(seat: Seat): string {
  /* THE PROJECT'S NAME FOR THIS SEAT WINS. Falling through to the title is what
     every project that has not renamed anything gets, which is all of them
     until somebody types in the Look panel. */
  const named = seat.persona?.name?.trim();
  if (named) return named;
  return ROOM_LABEL[seat.role] || seat.title
    || seat.role.charAt(0).toUpperCase() + seat.role.slice(1);
}

/* ── per-room dressing ──────────────────────────────────────────────────── */

const SIG_SPRITE: Record<string, string> = {
  art: "/static/img/floor/env/sig-art.png",
  audio: "/static/img/floor/env/sig-audio.png",
  narrative: "/static/img/floor/env/sig-narrative.png",
  gameplay: "/static/img/floor/env/sig-gameplay.png",
  qa: "/static/img/floor/env/sig-qa.png",
  cinematic: "/static/img/floor/env/sig-cinematic.png",
  tech: "/static/img/floor/env/sig-tech.png",
  director: "/static/img/floor/env/sig-director.png",
};

const S = "/static/img/floor/env";

/* ── per-discipline room dressing ──────────────────────────────────────
   Each room should be readable as its discipline BEFORE the label. A desk
   and a monitor say "office"; an easel says "art room". The shared kit
   (desk, chair, monitor) goes in every room because a studio has those,
   and the discipline-specific props sit on top.

   WHERE SPRITES EXIST the prop carries a `sprite` path and renders as an
   <img>. Where they do not, the prop falls back to a CSS-styled rectangle
   keyed on `kind`. The plan says to start with blockouts and swap art in
   later; a desk drawn as a coloured slab is better than a desk drawn as
   nothing. */

function dressRoom(role: string, r: Rect): Prop[] {
  const mid = r.x + r.w / 2;
  const props: Prop[] = [];

  /* THE SHARED KIT: desk, monitor, chair. Every craft room has these. */
  props.push(
    { kind: "desk", x: mid - 1.5, y: r.y + 1, w: 3, h: 1.3,
      sprite: `${S}/prop-desk.png` },
    { kind: "monitor", x: mid - 0.65, y: r.y + 1.05, w: 1.3, h: 0.7,
      sprite: `${S}/prop-monitor.png` },
    { kind: "chair", x: mid - 0.4, y: r.y + 2.6, w: 0.8, h: 1,
      sprite: `${S}/prop-chair.png` },
  );

  /* THE DISCIPLINE-SPECIFIC DRESSING. */
  switch (role) {
    /* ART: easels, a reference wall, swatches, and paint on the floor. */
    case "art":
      props.push(
        // reference wall: pinned art hung flat on the west wall
        { kind: "board", x: r.x + 0.35, y: r.y + 0.3, w: 1.7, h: 2.2,
          sprite: `${S}/prop-whiteboard.png` },
        // colour swatch strip on the east wall, up near the ceiling line
        { kind: "swatches", x: r.x + 6.05, y: r.y + 0.35, w: 1.5, h: 0.6 },
        // a second swatch card taped beside the reference wall
        { kind: "swatches", x: r.x + 0.4, y: r.y + 2.6, w: 1.2, h: 0.45 },
        // drawing tablet propped on the right end of the shared desk
        { kind: "monitor", x: r.x + 4.7, y: r.y + 1.3, w: 0.8, h: 0.5,
          sprite: `${S}/prop-monitor.png` },
        // working easel right of the desk, canvas turned toward the artist
        { kind: "easel", x: r.x + 5.9, y: r.y + 1.3, w: 1.6, h: 2.1 },
        // flat-file plan chest against the west wall
        { kind: "cabinet", x: r.x + 0.35, y: r.y + 3.15, w: 1.6, h: 1.1 },
        // paint bench under the east swatches
        { kind: "counter", x: r.x + 6.0, y: r.y + 3.4, w: 1.7, h: 1.2 },
        // paint-spattered floor cloth, drawn flat
        { kind: "rug", x: r.x + 2.2, y: r.y + 3.7, w: 3.3, h: 1.7 },
        // second easel in the foreground, across the near-left corner
        { kind: "easel", x: r.x + 0.5, y: r.y + 4.3, w: 1.6, h: 2.1 },
        // stool at that easel - nearest object, sells the depth
        { kind: "chair", x: r.x + 2.1, y: r.y + 5.2, w: 0.8, h: 1.0,
          sprite: `${S}/prop-chair.png` },
        // studio plant keeping the foreground from going flat
        { kind: "plant", x: r.x + 3.5, y: r.y + 5.5, w: 1.1, h: 1.1,
          sprite: `${S}/prop-plant.png` },
        { kind: "cabinet", x: r.x + r.w - 3, y: r.y + r.h - 2.6, w: 2.6, h: 2,
          sprite: SIG_SPRITE.art },
      );
      break;

    /* AUDIO: a treated booth. Foam on every wall, monitors on stands, a boom. */
    case "audio":
      props.push(
        // acoustic foam on the back wall, either side of the desk
        { kind: "foam", x: r.x + 0.5, y: r.y + 0.3, w: 2.2, h: 0.5 },
        { kind: "foam", x: r.x + 5.3, y: r.y + 0.3, w: 2.2, h: 0.5 },
        // foam columns treating the side walls
        { kind: "foam", x: r.x + 0.3, y: r.y + 1.2, w: 0.4, h: 2.8 },
        { kind: "foam", x: r.x + 7.3, y: r.y + 1.2, w: 0.4, h: 2.8 },
        // near-field monitors on stands, either side of the desk
        { kind: "monitor", x: r.x + 1.5, y: r.y + 1.0, w: 0.8, h: 0.9,
          sprite: `${S}/prop-monitor.png` },
        { kind: "monitor", x: r.x + 5.7, y: r.y + 1.0, w: 0.8, h: 0.9,
          sprite: `${S}/prop-monitor.png` },
        // condenser mic swung in over the desk on its boom
        { kind: "mic", x: r.x + 2.1, y: r.y + 1.9, w: 0.5, h: 1.5 },
        // outboard rack against the west wall
        { kind: "cabinet", x: r.x + 0.5, y: r.y + 4.2, w: 1.4, h: 1.8 },
        // patchbay shelf within reach of the chair
        { kind: "counter", x: r.x + 6.2, y: r.y + 2.5, w: 1.0, h: 1.2 },
        // dampening rug under the listening position
        { kind: "rug", x: r.x + 2.2, y: r.y + 3.3, w: 3.6, h: 2.2 },
        { kind: "cabinet", x: r.x + 5.2, y: r.y + 4.4, w: 2.6, h: 2.0,
          sprite: SIG_SPRITE.audio },
        { kind: "plant", x: r.x + 0.5, y: r.y + 5.4, w: 1.1, h: 1.1,
          sprite: `${S}/prop-plant.png` },
      );
      break;

    /* NARRATIVE: a writers' room. Corkboard, cards on string, too much coffee. */
    case "narrative":
      props.push(
        // the story spine, pinned to the back-left wall
        { kind: "board", x: r.x + 0.4, y: r.y + 0.3, w: 2.0, h: 1.9,
          sprite: `${S}/prop-whiteboard.png` },
        // index cards on string across the back-right wall: the act structure
        { kind: "cards", x: r.x + 5.2, y: r.y + 0.35, w: 2.4, h: 1.1 },
        // the long table the room writes at
        { kind: "table", x: r.x + 0.5, y: r.y + 3.7, w: 2.2, h: 2.0,
          sprite: `${S}/prop-meeting-table.png` },
        // chair tucked in at the far side, drawn behind the table
        { kind: "chair", x: r.x + 1.3, y: r.y + 2.8, w: 0.8, h: 1.0,
          sprite: `${S}/prop-chair.png` },
        // chair pulled out on the near side - somebody just got up
        { kind: "chair", x: r.x + 0.7, y: r.y + 5.8, w: 0.8, h: 1.0,
          sprite: `${S}/prop-chair.png` },
        // the coffee point, the room's real centre of gravity
        { kind: "coffee", x: r.x + 5.8, y: r.y + 2.4, w: 1.4, h: 1.2,
          sprite: `${S}/prop-coffee-point.png` },
        // two abandoned mugs, because too much coffee is the point
        { kind: "coffee", x: r.x + 1.3, y: r.y + 4.1, w: 0.5, h: 0.4 },
        { kind: "coffee", x: r.x + 2.3, y: r.y + 2.0, w: 0.5, h: 0.4 },
        { kind: "plant", x: r.x + 0.3, y: r.y + 2.2, w: 0.9, h: 0.9,
          sprite: `${S}/prop-plant.png` },
        { kind: "cabinet", x: r.x + 5.2, y: r.y + 4.6, w: 2.4, h: 1.9,
          sprite: SIG_SPRITE.narrative },
      );
      break;

    /* GAMEPLAY: test rigs, pads, a whiteboard of feel tunables, a play couch. */
    case "gameplay":
      props.push(
        // the feel-tunables whiteboard, facing the desk
        { kind: "board", x: r.x + 0.4, y: r.y + 0.4, w: 1.9, h: 2.3,
          sprite: `${S}/prop-whiteboard.png` },
        // test-rig bench along the west wall
        { kind: "counter", x: r.x + 0.4, y: r.y + 2.8, w: 2.3, h: 1.3 },
        // two capture monitors, so it reads as a station and not a desk
        { kind: "monitor", x: r.x + 0.55, y: r.y + 3.4, w: 1.0, h: 0.75,
          sprite: `${S}/prop-monitor.png` },
        { kind: "monitor", x: r.x + 1.65, y: r.y + 3.4, w: 1.0, h: 0.75,
          sprite: `${S}/prop-monitor.png` },
        // shelf of pads and controllers, clear of the doorway
        { kind: "devices", x: r.x + 6.0, y: r.y + 0.5, w: 1.5, h: 1.8 },
        // the big playtest display, angled at the couch
        { kind: "screen", x: r.x + 4.3, y: r.y + 0.35, w: 2.2, h: 0.9 },
        { kind: "rug", x: r.x + 2.8, y: r.y + 4.6, w: 4.0, h: 2.0 },
        // the couch people actually play on
        { kind: "sofa", x: r.x + 3.4, y: r.y + 5.0, w: 2.6, h: 1.2,
          sprite: `${S}/prop-couch.png` },
        { kind: "printer", x: r.x + 6.2, y: r.y + 5.4, w: 1.3, h: 0.9 },
        // signature moved FRONT-LEFT so it never blocks the east door
        { kind: "cabinet", x: r.x + 0.4, y: r.y + 4.6, w: 2.6, h: 2.0,
          sprite: SIG_SPRITE.gameplay },
      );
      break;

    /* QA: a device shelf, a bug wall, and one thing broken on purpose. */
    case "qa":
      props.push(
        // the phones and handhelds under test
        { kind: "devices", x: r.x + 0.4, y: r.y + 0.35, w: 2.6, h: 1.3 },
        { kind: "devices", x: r.x + 5.2, y: r.y + 0.35, w: 2.4, h: 1.3 },
        // the bug wall: open defects, papered up
        { kind: "board", x: r.x + 6.2, y: r.y + 1.9, w: 1.5, h: 2.0,
          sprite: `${S}/prop-whiteboard.png` },
        // charging bench below the doorway
        { kind: "counter", x: r.x + 0.4, y: r.y + 5.0, w: 2.0, h: 1.1 },
        { kind: "monitor", x: r.x + 1.5, y: r.y + 4.9, w: 0.9, h: 0.55,
          sprite: `${S}/prop-monitor.png` },
        // printer mid-jam, drawn in front of the bench
        { kind: "printer", x: r.x + 0.7, y: r.y + 5.5, w: 1.1, h: 0.7 },
        // the thing that is visibly broken: a dead cracked panel on the floor
        { kind: "broken", x: r.x + 2.2, y: r.y + 5.9, w: 1.4, h: 1.0 },
        { kind: "plant", x: r.x + 3.9, y: r.y + 5.9, w: 1.0, h: 1.0,
          sprite: `${S}/prop-plant.png` },
        { kind: "cabinet", x: r.x + 5.2, y: r.y + 4.6, w: 2.4, h: 1.9,
          sprite: SIG_SPRITE.qa },
      );
      break;

    /* CINEMATIC: an edit bay. Timelines, a review screen, a shot board. */
    case "cinematic":
      props.push(
        // two timeline monitors, so the desk reads as a bay not an office
        { kind: "monitor", x: r.x + 2.6, y: r.y + 1.05, w: 0.9, h: 0.6,
          sprite: `${S}/prop-monitor.png` },
        { kind: "monitor", x: r.x + 4.7, y: r.y + 1.05, w: 0.9, h: 0.6,
          sprite: `${S}/prop-monitor.png` },
        // the review screen everyone watches cuts on
        { kind: "screen", x: r.x + 0.5, y: r.y + 0.35, w: 2.2, h: 0.9 },
        { kind: "plant", x: r.x + 0.5, y: r.y + 1.7, w: 1.1, h: 1.1,
          sprite: `${S}/prop-plant.png` },
        // the shot board, cards pinned in sequence
        { kind: "board", x: r.x + 0.4, y: r.y + 3.2, w: 1.7, h: 2.2,
          sprite: `${S}/prop-whiteboard.png` },
        // drive archive: where the footage lives
        { kind: "cabinet", x: r.x + 6.1, y: r.y + 0.4, w: 1.5, h: 2.0 },
        // continuity strip facing the shot board
        { kind: "board", x: r.x + 6.1, y: r.y + 2.6, w: 1.5, h: 1.8,
          sprite: `${S}/prop-whiteboard.png` },
        { kind: "rug", x: r.x + 0.4, y: r.y + 4.6, w: 2.6, h: 2.2 },
        // review couch, out of the door lane
        { kind: "sofa", x: r.x + 0.4, y: r.y + 5.5, w: 2.2, h: 1.1,
          sprite: `${S}/prop-couch.png` },
        // signature pushed right of the door gap
        { kind: "cabinet", x: r.x + 5.4, y: r.y + 4.8, w: 2.3, h: 1.8,
          sprite: SIG_SPRITE.cinematic },
      );
      break;

    /* TECH: racks, blinking lights, and cable management nobody believes in. */
    case "tech":
      props.push(
        // the west wall reads as a ROW of racks, not one box
        { kind: "rack", x: r.x + 0.4, y: r.y + 0.4, w: 1.5, h: 2.0 },
        { kind: "rack", x: r.x + 0.4, y: r.y + 2.7, w: 1.5, h: 2.0 },
        // a third framing the doorway from inside
        { kind: "rack", x: r.x + 6.1, y: r.y + 0.4, w: 1.5, h: 2.0 },
        { kind: "monitor", x: r.x + 2.6, y: r.y + 1.05, w: 0.9, h: 0.6,
          sprite: `${S}/prop-monitor.png` },
        // cable spilling out of the racks - flat, so it stays walkable
        { kind: "cables", x: r.x + 1.6, y: r.y + 2.4, w: 2.0, h: 1.2 },
        { kind: "cables", x: r.x + 5.6, y: r.y + 2.8, w: 1.8, h: 1.0 },
        { kind: "plant", x: r.x + 6.4, y: r.y + 2.9, w: 1.1, h: 1.1,
          sprite: `${S}/prop-plant.png` },
        { kind: "counter", x: r.x + 0.4, y: r.y + 5.0, w: 2.4, h: 1.0 },
        { kind: "coffee", x: r.x + 0.7, y: r.y + 5.9, w: 0.6, h: 0.45 },
        // label printer, out where it gets tripped over
        { kind: "printer", x: r.x + 2.9, y: r.y + 5.5, w: 1.2, h: 0.8 },
        { kind: "cabinet", x: r.x + 5.0, y: r.y + 4.4, w: 2.6, h: 2.0,
          sprite: SIG_SPRITE.tech },
      );
      break;

    default:
      props.push(
        { kind: "cabinet", x: r.x + r.w - 3, y: r.y + r.h - 2.6, w: 2.6, h: 2 },
        { kind: "plant", x: r.x + 0.4, y: r.y + r.h - 1.6, w: 1.1, h: 1.1,
          sprite: `${S}/prop-plant.png` },
      );
      break;
  }

  return props;
}

/* JUST INSIDE A ROOM'S DOOR, WITH ROOM FOR A HEAD.
 *
 * A spot is where a character's FEET go and the art stands about 2.7 cells up
 * from there, which is the whole reason the north case is not symmetric with
 * the south one. Standing 1.4 cells inside a NORTH door put the figure's head
 * and shoulders through the wall it had just walked past - most visibly on the
 * Director, who is in its office in every state and so is the one character
 * always on screen doing it. Coming in a south door has the opposite problem
 * and none of the same cost: the wall is behind the camera, so a figure near it
 * simply stands at the front of the room.
 *
 * HEAD is the sprite's height in cells and it is the same number the lounge
 * grid reserves. If the cast is ever redrawn taller, both move together. */
const HEAD = 2.7;

function insideDoor(r: Rect, d: Door): Spot {
  switch (d.side) {
    case "e": return { x: r.x + r.w - 1.4, y: r.y + d.at + d.len / 2 };
    case "w": return { x: r.x + 1.4, y: r.y + d.at + d.len / 2 };
    /* Far enough down that the whole figure is inside the room, and clamped so
       a room shorter than the cast cannot push it out of the far side. */
    case "n": return { x: r.x + d.at + d.len / 2,
                       y: r.y + Math.min(HEAD + 0.3, r.h - 1.2) };
    default:  return { x: r.x + d.at + d.len / 2, y: r.y + r.h - 1.1 };
  }
}

function craftRoom(seat: Seat, slot: SlotDef): PlanRoom {
  const rect: Rect = { x: slot.x, y: slot.y, w: ROOM_W, h: ROOM_H };
  const doorAt = slot.door === "n" || slot.door === "s"
    ? rect.w / 2 - DOOR_LEN / 2
    : rect.h / 2 - DOOR_LEN / 2;
  const door: Door = { side: slot.door, at: doorAt, len: DOOR_LEN };
  return {
    id: seat.role, kind: "seat", seat: seat.role, label: labelFor(seat),
    persona: seat.persona,
    rect, doors: [door], props: dressRoom(seat.role, rect),
    /* AT THE CHAIR, NOT ON THE DESK. The shared kit puts the desk across
       y+1 to y+2.3 and the chair at y+2.6 to y+3.6, so a character standing at
       y+2.5 was drawn straddling the desk itself - and, because a figure is
       2.7 cells tall measured up from its feet, its head went through the wall
       behind. y+3.1 seats it on its own chair with the whole figure inside the
       room, which is both what the furniture says and what the wall allows. */
    desk: { x: rect.x + rect.w / 2, y: rect.y + 3.1 },
    stand: insideDoor(rect, door),
  };
}

/* ── the whole building ─────────────────────────────────────────────────── */

export function planFloor(seats: Seat[]): FloorPlan {
  const director = seats.find((s) => s.role === "director") || null;
  const crafts = seats.filter((s) => s.role !== "director");

  /* Assign crafts to slots. Preferred positions first, then fill remaining. */
  const assigned = new Map<number, Seat>();
  const unassigned: Seat[] = [];
  for (const s of crafts) {
    const pref = PREFERRED_SLOT[s.role];
    if (pref !== undefined && !assigned.has(pref)) {
      assigned.set(pref, s);
    } else {
      unassigned.push(s);
    }
  }
  for (const s of unassigned) {
    for (let i = 0; i < SLOTS.length; i++) {
      if (!assigned.has(i)) { assigned.set(i, s); break; }
    }
  }

  const rooms: PlanRoom[] = [];

  /* Craft rooms in their assigned slots. */
  for (const [idx, seat] of assigned) {
    if (idx < SLOTS.length) {
      rooms.push(craftRoom(seat, SLOTS[idx]));
    }
  }

  /* THE LOUNGE: center of the grid, wide openings on all four sides. */
  const loungeRect: Rect = { x: C1, y: R1, w: ROOM_W, h: ROOM_H };
  /* THE LOUNGE IS DESIGNED AROUND ITS STANDING SPOTS, NOT AROUND ITS WALLS.
     Six of the sixteen lounge spots land inside the room, in two columns at
     x+1.6 and x+3.8, and all four sides have a doorway - so the whole centre
     and the two through-routes are reserved floor. Everything solid is pushed
     to the walls, which is why there are nine props here and not fourteen: a
     coffee table in the middle would be a table drawn through whoever is
     standing at it. */
  const loungeProps: Prop[] = [
    // one flat rug under the standing crowd; drawn at low alpha so it never
    // occludes a character and never blocks a route
    { kind: "rug", x: C1 + 0.9, y: R1 + 1.2, w: 5.6, h: 4.8 },
    // the lounge's sign, hung on the north wall east of the doorway
    { kind: "cabinet", x: C1 + 5.6, y: R1 + 0.2, w: 2.3, h: 1.2,
      sprite: `${S}/sig-lounge.png` },
    // the good couch, under that sign, facing west into the open middle
    { kind: "sofa", x: C1 + 5.7, y: R1 + 1.5, w: 2.2, h: 1.3,
      sprite: `${S}/prop-couch.png` },
    // the second couch, south-east, facing back north across the rug
    { kind: "sofa", x: C1 + 5.6, y: R1 + 5.2, w: 2.2, h: 1.4,
      sprite: `${S}/prop-couch.png` },
    // the coffee machine, flat against the west wall north of the doorway, so
    // the queue for it forms in the corridor rather than in the room
    { kind: "coffee", x: C1 + 0.2, y: R1 + 0.5, w: 1.0, h: 1.4,
      sprite: `${S}/prop-coffee-point.png` },
    // the games cabinet: tall and narrow, so it reads as a machine somebody
    // stands AT rather than a shelf
    { kind: "arcade", x: C1 + 0.2, y: R1 + 5.0, w: 0.95, h: 1.7 },
    // the plant nobody waters; its footprint stops a tenth of a cell above the
    // first standing row, so an idle character is drawn in front of it
    { kind: "plant", x: C1 + 1.25, y: R1 + 0.2, w: 0.9, h: 0.9,
      sprite: `${S}/prop-plant.png` },
    /* THE RADIO. Click target, hand-drawn, two states. Do not move it: the hit
       test in floorRender finds it by kind, but the reader finds it by memory. */
    { kind: "radio", x: C1 + ROOM_W / 2 - 0.75, y: R1 + 2.9, w: 1.5, h: 1.1 },
  ];
  rooms.push({
    id: "lounge", kind: "lounge", label: "Lounge", rect: loungeRect,
    doors: [
      { side: "n", at: ROOM_W / 2 - 1.5, len: 3 },
      { side: "s", at: ROOM_W / 2 - 1.5, len: 3 },
      { side: "w", at: ROOM_H / 2 - 1.5, len: 3 },
      { side: "e", at: ROOM_H / 2 - 1.5, len: 3 },
    ],
    props: loungeProps,
    desk: { x: C1 + ROOM_W / 2, y: R1 + 3 },
    stand: { x: C1 + ROOM_W / 2, y: R1 + ROOM_H - 1.5 },
  });

  /* DIRECTOR'S OFFICE: bottom center. */
  const officeRect: Rect = { x: C1, y: R2, w: ROOM_W, h: ROOM_H };
  const officeDoor: Door = { side: "n", at: ROOM_W / 2 - DOOR_LEN / 2, len: DOOR_LEN };
  if (director) {
    rooms.push({
      id: director.role, kind: "office", seat: director.role,
      persona: director.persona,
      label: labelFor(director), rect: officeRect, doors: [officeDoor],
      /* THE CORNER OFFICE, DESIGNED AROUND ITS SIX STANDING SPOTS. The whole
         southern half of this room is where agents queue to see the Director,
         so nothing solid goes below y+3.2 in the middle: a filing cabinet there
         would be drawn through whoever is waiting in it. That constraint is
         also the joke - the room is mostly empty floor for people to stand on
         while the desk gets the north wall to itself. */
      props: [
        /* THE BIG DESK SITS OFF THE BACK WALL, NOT AGAINST IT, and the gap is
           load-bearing rather than taste. The Director stands at its own desk
           spot, and a figure is about 2.7 cells tall measured UP from its feet
           - so with the desk hard against the north wall the cat's head was
           drawn through the wall behind it. Everything moved down together:
           the desk, the monitor on it, the chair behind it, and the desk SPOT
           below. Moving furniture without moving the coordinate a character
           stands at is how a desk ends up drawn through somebody's chest. */
        { kind: "desk", x: C1 + 2.2, y: R2 + 2.2, w: 3.6, h: 1.5,
          sprite: `${S}/prop-desk.png` },
        { kind: "monitor", x: C1 + 3.35, y: R2 + 2.25, w: 1.3, h: 0.65,
          sprite: `${S}/prop-monitor.png` },
        // The throne, tucked BEHIND the desk so the desk draws over its base
        // and the seat reads as the far side of the table.
        { kind: "chair", x: C1 + 3.6, y: R2 + 1.25, w: 0.9, h: 0.9,
          sprite: `${S}/prop-chair.png` },
        { kind: "cabinet", x: C1 + 5.3, y: R2 + 0.35, w: 2.4, h: 1.9,
          sprite: SIG_SPRITE.director },
        /* THE EGO WALL: three framed things in a row, evenly spaced and
           identical, which is the entire joke. */
        { kind: "frame", x: C1 + 0.35, y: R2 + 0.2, w: 0.65, h: 0.5 },
        { kind: "frame", x: C1 + 1.2, y: R2 + 0.2, w: 0.65, h: 0.5 },
        { kind: "frame", x: C1 + 2.05, y: R2 + 0.2, w: 0.65, h: 0.5 },
        // The drinks cabinet, stopping above the first row of standing spots.
        { kind: "cabinet", x: C1 + 0.25, y: R2 + 2.4, w: 0.85, h: 1.2 },
        { kind: "plant", x: C1 + 7.0, y: R2 + 2.6, w: 0.9, h: 0.9,
          sprite: `${S}/prop-plant.png` },
        // The visitors' couch nobody is invited to sit on.
        { kind: "sofa", x: C1 + 6.4, y: R2 + 3.6, w: 1.4, h: 1.9,
          sprite: `${S}/prop-couch.png` },
        // The rug that says this floor is nicer than yours.
        { kind: "rug", x: C1 + 1.2, y: R2 + 3.2, w: 5.4, h: 3.2 },
        // The cat's cushion, where a visitor's chair should be. Flat, so the
        // Director is drawn standing ON it rather than behind it.
        { kind: "rug", x: C1 + 3.3, y: R2 + 3.0, w: 1.4, h: 0.9 },
      ],
      desk: { x: C1 + ROOM_W / 2, y: R2 + 3.9 },
      stand: insideDoor(officeRect, officeDoor),
    });
  }

  /* Corridor props: what tells you the space between rooms is FLOOR. */
  const props: Prop[] = [
    { kind: "plant", x: C1 - 2, y: R1 - 2, w: 1.1, h: 1.1,
      sprite: `${S}/prop-plant.png` },
    { kind: "plant", x: C2 + 0.6, y: R1 - 2, w: 1.1, h: 1.1,
      sprite: `${S}/prop-plant.png` },
    { kind: "board", x: C2 + 0.6, y: R2 + 1.5, w: 1.2, h: 1.6,
      sprite: `${S}/prop-whiteboard.png` },
    { kind: "plant", x: C1 - 2, y: R2 + 2, w: 1.1, h: 1.1,
      sprite: `${S}/prop-plant.png` },
  ];

  /* ── walls ──────────────────────────────────────────────────────────────
     Every room's four edges, identical edges collapsed to one, doors cut
     out, boundary edges dropped (the outer wall is one continuous element). */
  type Line = { dir: "h" | "v"; a: number; s: number; e: number; gaps: [number, number][] };
  const lines = new Map<string, Line>();
  const edgeOf = (r: Rect, side: Side): Line =>
    side === "n" ? { dir: "h", a: r.y, s: r.x, e: r.x + r.w, gaps: [] }
    : side === "s" ? { dir: "h", a: r.y + r.h, s: r.x, e: r.x + r.w, gaps: [] }
    : side === "w" ? { dir: "v", a: r.x, s: r.y, e: r.y + r.h, gaps: [] }
    : { dir: "v", a: r.x + r.w, s: r.y, e: r.y + r.h, gaps: [] };

  for (const room of rooms) {
    for (const side of ["n", "s", "e", "w"] as Side[]) {
      const line = edgeOf(room.rect, side);
      if (line.dir === "h" && (line.a === 0 || line.a === TOTAL_H)) continue;
      if (line.dir === "v" && (line.a === 0 || line.a === TOTAL_W)) continue;
      const key = `${line.dir}:${line.a}:${line.s}:${line.e}`;
      const at = lines.get(key) || (lines.set(key, line), line);
      for (const d of room.doors) {
        if (d.side !== side) continue;
        const start = (side === "n" || side === "s" ? room.rect.x : room.rect.y) + d.at;
        at.gaps.push([start, start + d.len]);
      }
    }
  }

  const walls: Wall[] = [];
  for (const line of lines.values()) {
    const gaps = line.gaps.slice().sort((a, b) => a[0] - b[0]);
    let cursor = line.s;
    for (const [gs, ge] of gaps) {
      if (gs > cursor) walls.push({ dir: line.dir, a: line.a, s: cursor, e: gs });
      cursor = Math.max(cursor, ge);
    }
    if (cursor < line.e) walls.push({ dir: line.dir, a: line.a, s: cursor, e: line.e });
  }

  /* ── standing spots ─────────────────────────────────────────────────── */

  /* WHERE PEOPLE STAND IN THE LOUNGE, AND ALL OF IT IS INSIDE THE LOUNGE.

     This built sixteen spots on a two-wide grid marching NORTH from the back
     wall, which needed eight rows in a room that fits three. Rows four to eight
     were at negative offsets from the room's own top edge, so the moment more
     than six agents were idle the overflow stood in the CORRIDOR - outside the
     wall, in the gap between the lounge and the room above it, with a doorway
     drawn through them. On a pane whose entire claim is that position is state,
     "idle" was being drawn as "standing in a corridor".

     SO THE GRID IS BUILT FROM THE ROOM AND THEN FILTERED BY IT. Candidates are
     laid out across the interior, anything that would land on a solid prop is
     dropped, and what is left is ordered from the back of the room forward so a
     small crowd fills the far side first and the near floor stays clear. Both
     the count and the spacing fall out of the room's size rather than being
     numbers that have to agree with it.

     A ROOM CAN RUN OUT OF SPOTS, and that is handled where it is read rather
     than here: spotFor clamps to the last one, so a tenth idle agent stands
     with the ninth instead of stepping outside. Two characters overlapping is a
     crowd; one standing in a wall is a bug. */
  const lounge: Spot[] = [];
  {
    /* Solid furniture only. A rug is walked on and the radio is waist high, so
       neither takes floor away from somebody standing. */
    const solid = loungeProps.filter(
      (q) => q.kind !== "rug" && q.kind !== "cables");
    /* THE MARGIN IS HALF A CHARACTER, NOT A WHOLE ONE. A figure is about a cell
       wide at the feet, so 0.4 keeps somebody from standing inside the couch
       while still allowing them to stand IN FRONT of it - which is what a
       lounge is for. Too generous a margin and a furnished room has nowhere to
       stand at all, which is how this first came out with five spots for seven
       idle crafts. */
    const clear = (x: number, y: number) => !solid.some(
      (q) => x > q.x - 0.4 && x < q.x + q.w + 0.4
          && y > q.y - 0.4 && y < q.y + q.h + 0.4);

    /* HEAD ROOM, WHICH IS THE PART A FOOT COORDINATE DOES NOT SAY. A spot is
       where somebody's FEET go, and the art stands about 2.7 cells up from
       there - so a spot one cell inside the north wall puts a character's head
       and shoulders out in the corridor above, which is exactly the "standing
       outside the room" this was fixed to stop. The back row therefore starts
       far enough down that the whole figure is inside the room it is standing
       in. */
    const stepX = 1.25;
    const stepY = 1.45;
    for (let y = R1 + HEAD + 0.2; y <= R1 + ROOM_H - 1.0; y += stepY) {
      for (let x = C1 + 1.2; x <= C1 + ROOM_W - 1.0; x += stepX) {
        if (clear(x, y)) lounge.push({ x, y });
      }
    }
    /* Back of the room first. */
    lounge.sort((a, b) => a.y - b.y || a.x - b.x);
  }
  /* A lounge so furnished that nobody can stand in it would place every idle
     agent at (0,0). Never observed, but the fallback costs one line. */
  if (!lounge.length) {
    lounge.push({ x: C1 + ROOM_W / 2, y: R1 + ROOM_H - 1.6 });
  }

  /* Queue outside the Director's door, running west through the corridor. */
  const doorX = C1 + officeDoor.at + officeDoor.len / 2;
  const queue: Spot[] = [];
  for (let i = 0; i < 8; i++) {
    const x = doorX - 1.6 - i * 1.8;
    if (x < 1.2) break;
    queue.push({ x, y: R2 - 1.2 });
  }
  if (!queue.length) queue.push({ x: doorX, y: R2 - 1.2 });

  const office: Spot[] = [];
  for (let i = 0; i < 6; i++) {
    office.push({
      x: C1 + 1.6 + (i % 3) * 2,
      y: R2 + ROOM_H - 1.2 - Math.floor(i / 3) * 1.8,
    });
  }

  const bySeat = new Map<string, PlanRoom>();
  for (const r of rooms) if (r.seat) bySeat.set(r.seat, r);

  return { cols: TOTAL_W, rows: TOTAL_H, rooms, walls, props, lounge, queue, office, bySeat };
}

/** WHERE ONE CHARACTER STANDS, from the zone occupancy.ts put it in. */
export function spotFor(plan: FloorPlan, seat: string, zone: string, nth: number): Spot {
  const room = plan.bySeat.get(seat);
  const pick = (list: Spot[]) => list[Math.min(nth, list.length - 1)];
  switch (zone) {
    case "desk": return room ? room.desk : pick(plan.lounge);
    case "room": return room ? room.stand : pick(plan.lounge);
    case "door": return pick(plan.queue);
    case "office": return pick(plan.office);
    default: return pick(plan.lounge);
  }
}

/** Cells to a CSS length. */
export function cx(n: number): string {
  return `calc(var(--cell) * ${Number(n.toFixed(3))})`;
}
