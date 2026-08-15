import { useEffect, useMemo, useState } from "react";
import { Ti } from "../Ti";
import { SEAT_COLOR, SEAT_ICON } from "../nav";
import { readJSON } from "../../bridge";
import { type ConsoleState } from "./api";
import "./floor.css";

/* THE STUDIO FLOOR - the third reading of the queue, beside Board and Graph.
 *
 * The board says what is queued and the graph says what depends on what.
 * Neither says WHO IS DOING WHAT RIGHT NOW in a way you can take in without
 * reading. The floor does: the studio seen from a high three-quarter angle,
 * each seat is a room, and an agent's POSITION IN THE BUILDING is its state.
 * Walking to the lounge is idle, sitting at the desk is running, standing at
 * the Director's door is waiting on a human. Step 1 is the building only; the
 * occupants and the walking arrive in the steps after this one.
 *
 * THE LAYOUT IS GENERATED FROM THE SEAT TABLE, NEVER FROM A LIST IN HERE.
 * Seats are per project - a project can disable qa or cinematic, and
 * seat_config is what says so - so a hardcoded seven-room floor would draw
 * rooms for staff that do not exist and silently omit the ones that do. Every
 * room below is computed from whatever /api/state returns, and the arrangement
 * rule is what stays fixed: rooms to the LEFT and RIGHT of a central lounge so
 * travel across the floor is horizontal, and the Director's office alone at the
 * bottom, because the Director is who you walk to, not who you sit beside.
 *
 * NO ART EXISTS YET. Every room here is CSS blockout - a rectangle with a wall
 * band along its top edge to suggest the tilt. Generated art replaces the
 * insides of `.bg4-room` and nothing else; the grid, the seat ordering and the
 * (coming) state machine do not know what is painted in a room.
 */

/** What a room is CALLED. The seat table's own title is the default; this
 *  overrides only where the craft's name and the room's name differ. A studio
 *  has a video room, not a "cinematic". */
const ROOM_LABEL: Record<string, string> = {
  cinematic: "Video",
  qa: "QA",
};

/** The one word under the label. It is what the room is FOR, in the language a
 *  studio uses about itself - the difference between reading this as a floor of
 *  workshops and reading it as an org chart with rounded corners. Unknown seats
 *  fall back to "desk", which is true of any seat a project invents. */
const ROOM_VIBE: Record<string, string> = {
  director: "calls",
  narrative: "story",
  gameplay: "play",
  tech: "code",
  art: "paint",
  audio: "sound",
  qa: "checks",
  cinematic: "cuts",
};

type Seat = { role: string; title?: string };

/** Where every room sits. `side` rooms are laid out in pairs down the grid,
 *  left then right, so the two columns fill evenly and the lounge always has
 *  something on both sides of it. */
type Floor = {
  left: Seat[];
  right: Seat[];
  /** The Director, when the project has one. A project may disable the seat,
   *  and the floor must not draw an office for staff who were let go. */
  director: Seat | null;
};

export function planFloor(seats: Seat[]): Floor {
  const director = seats.find((s) => s.role === "director") || null;
  const rest = seats.filter((s) => s.role !== "director");
  const left: Seat[] = [];
  const right: Seat[] = [];
  /* ALTERNATING, NOT SPLIT DOWN THE MIDDLE. Halving the list puts the first
     half of the seat table in one column and the second half in the other, so
     the floor's shape changes completely when a seat is disabled near the
     middle. Alternating means disabling one seat moves one room. */
  rest.forEach((s, i) => (i % 2 === 0 ? left : right).push(s));
  return { left, right, director };
}

function Room({ seat, kind }: { seat: Seat; kind: "side" | "office" }) {
  const color = SEAT_COLOR[seat.role];
  const label = ROOM_LABEL[seat.role] || seat.title
    || seat.role.charAt(0).toUpperCase() + seat.role.slice(1);
  return (
    /* `--seat` rides on the element rather than being written into a rule, so
       the room stylesheet has ONE set of rules for every seat and the colour
       comes from nav.ts - the same table the rail, the queue cards and the
       graph read, which is what keeps art pink in all four places. Seats the
       table has no colour for fall back to the theme's own line colour rather
       than to a literal. */
    <div className={`bg4-room bg4-room-${kind}`} data-seat={seat.role}
         style={{ "--seat": color || "var(--line-strong)" } as React.CSSProperties}>
      {/* THE WALL IS THE WHOLE TILT. There is no 3D transform anywhere in this
          pane: rotating the container would make a twelve-seat floor unreadable
          and unscrollable, and it would fight whatever perspective the
          generated art is drawn with. A band along the top edge reads as a back
          wall at a high angle, and costs nothing. */}
      <div className="bg4-room-wall">
        <span className="bg4-room-plate">
          <Ti name={SEAT_ICON[seat.role] || "point"} size={12} />
          {label}
        </span>
      </div>
      <div className="bg4-room-body">
        <span className="bg4-room-vibe">{ROOM_VIBE[seat.role] || "desk"}</span>
        {/* WHERE THE PEOPLE GO. Empty in step 1 and deliberately still here:
            it is the positioning context the characters are placed in, so the
            step that adds them adds children, not a layout. */}
        <div className="bg4-room-stage" />
      </div>
    </div>
  );
}

export function FloorPane({ state }: { state: ConsoleState }) {
  /* The project's own seat table. Read once, not per poll: it changes when
     somebody edits seat config, which is not a thing that happens mid-session -
     the same call and the same reasoning as the composer's seat menu. */
  const [seats, setSeats] = useState<Seat[] | null>(null);
  useEffect(() => {
    readJSON<{ seats: { role?: string; title?: string }[] }>(
      "/api/state", { seats: [] })
      .then((d) => setSeats((d.seats || [])
        .filter((r): r is Seat => !!r.role)
        .map((r) => ({ role: r.role, title: r.title }))));
  }, []);

  /* FALLBACK TO THE SEATS THE WORK IS ADDRESSED TO. /api/state answers with an
     empty seat list when no project is selected, and an empty floor beside a
     board full of running items reads as a broken pane rather than as "no
     project". Whatever the items are addressed to is a true, if partial,
     roster. */
  const floor = useMemo(() => {
    const table = seats && seats.length
      ? seats
      : [...new Set(state.items.map((i) => i.seat).filter(Boolean))]
        .map((role) => ({ role }));
    return planFloor(table);
  }, [seats, state.items]);

  const sides = floor.left.length + floor.right.length;
  if (!sides && !floor.director) {
    return <div className="bg4-floorwrap"><div className="bg4-floor-none">
      no seats configured
    </div></div>;
  }

  return (
    /* `data-sides` IS WHAT LETS THE FLOOR SURVIVE A SMALL SEAT TABLE. A project
       running Director plus one seat has nothing to put in the third column,
       and a three-column grid with two empty tracks is a room floating in the
       middle of a lot of nothing. The stylesheet collapses the arrangement at
       0 and 1, and the rule stays in CSS rather than becoming a second JSX
       branch to keep one floor, not three. */
    <div className="bg4-floorwrap">
      <div className="bg4-floor" data-sides={Math.min(sides, 2)}>
        <div className="bg4-floor-col bg4-floor-left">
          {floor.left.map((s) => <Room key={s.role} seat={s} kind="side" />)}
        </div>

        {/* THE LOUNGE IS NOT A SEAT and has no room chrome - no wall band, no
            nameplate. It is the middle of the floor, which is where an agent
            stands when it holds no work, and it has to read as open ground or
            "idle" looks like an eighth department. */}
        <div className="bg4-lounge">
          <span className="bg4-lounge-label">lounge</span>
          <div className="bg4-lounge-stage" />
        </div>

        <div className="bg4-floor-col bg4-floor-right">
          {floor.right.map((s) => <Room key={s.role} seat={s} kind="side" />)}
        </div>

        {floor.director && (
          <div className="bg4-floor-office">
            <Room seat={floor.director} kind="office" />
          </div>
        )}
      </div>
    </div>
  );
}
