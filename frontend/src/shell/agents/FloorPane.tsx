import { useEffect, useMemo, useRef, useState } from "react";
import { Ti } from "../Ti";
import { SEAT_COLOR, SEAT_ICON } from "../nav";
import { useSelection, type Selection } from "../selection";
import { readJSON } from "../../bridge";
import { useViewActive } from "../../hooks";
import { type ConsoleState } from "./api";
import { floorSignature, placeFloor, type Occupant } from "./occupancy";
import { useHandoffs, type Handoff } from "./handoff";
import { useWalk } from "./useWalk";
import { banterOnStored, floorIsQuiet, storeBanterOn, useBanter } from "./banter";
import "./floor.css";

/* THE STUDIO FLOOR - the third reading of the queue, beside Board and Graph.
 *
 * The board says what is queued and the graph says what depends on what.
 * Neither says WHO IS DOING WHAT RIGHT NOW in a way you can take in without
 * reading. The floor does: the studio seen from a high three-quarter angle,
 * each seat is a room, and an agent's POSITION IN THE BUILDING is its state.
 * Walking to the lounge is idle, sitting at the desk is running, standing at
 * the Director's door is waiting on a human.
 *
 * THE OCCUPANTS ARE PLACED BY occupancy.ts AND BY NOTHING IN HERE. Every
 * character on this floor stands where one row of /api/console/state put it,
 * and a seat nothing was reported about is drawn idle rather than guessed at -
 * a figure sat at a desk that is not running would be worse than an empty
 * pane, because it stops you going to look. The placement is a pure function
 * so that claim can be checked without a browser.
 *
 * THE MOVEMENT IS INTERPOLATED BETWEEN POLLS AND THE POLL DID NOT GET FASTER.
 * consoleState() ticks at 3s and that is the only clock this pane has; useWalk
 * animates from the placement it measured last commit to the one it is looking
 * at now. Every character is therefore drawn where the LAST poll put it or
 * where THIS one did, never at a coordinate the server never described.
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
 *
 * CLICKING DRIVES THE SHELL'S SELECTION AND THE FLOOR OWNS NO INSPECTOR. A room
 * and the character in it are two controls that resolve to the SAME selection
 * key as the board's card for that item - `i<id>` - so clicking an agent here
 * and clicking its card over on the board are one act, not two panels
 * disagreeing about what is selected. A floor-local detail panel would have
 * been a second inspector to keep in step with the first, and the first one
 * already fetches the log, steers and stops.
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

/** What selecting this character selects.
 *
 *  THE KEY IS THE ITEM'S KEY WHEREVER THERE IS AN ITEM. `i<id>` is what the
 *  board, the queue rail and the responses list all write, so the inspector
 *  stays on one thing when you click the same work in two places, and the
 *  highlight on the floor agrees with the highlight on the board.
 *
 *  A seat with nothing on it has no item to key on, so it keys on the seat.
 *  That selection carries no item id ON PURPOSE - there is no log to fetch and
 *  the inspector must not pretend otherwise; `seatState` is what tells it to
 *  offer the seat work instead. */
function selectionFor(who: Occupant): Selection {
  return {
    key: who.itemId ? `i${who.itemId}` : `seat:${who.seat}`,
    /* The same split the board's cards make: a live run is an agent, anything
       else is the row it came from. */
    kind: who.state === "running" ? "agent" : "item",
    itemId: who.itemId ?? undefined,
    /* NEVER INVENTED, like everything else on this pane. A seat with no item
       has no title, so the panel is headed with the placement's own note - the
       words occupancy.ts justified the position with. */
    title: who.title || who.note,
    seat: who.seat,
    seatState: who.state,
  };
}

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

/* WHICH DRAWING, for a seat in a state.
 *
 * The pose is chosen from the SAME state the position is chosen from, so the
 * picture and the place can never disagree: a character at a desk is drawn
 * working, one crossing the floor with a result is drawn carrying it. Walking
 * is not selected here at all - useWalk drives the two-frame cycle while an
 * actor is in transit, and a pose picked on a 3s poll would sit still while the
 * character slid across the room.
 *
 * CAST_SEATS is the list that has art. Anything else falls back to `generic`
 * rather than rendering nothing, because a project can invent a seat and a
 * missing drawing must not remove a live agent from the floor.
 */
const CAST_SEATS = new Set([
  "art", "audio", "narrative", "gameplay", "qa", "cinematic", "tech", "director",
]);

const POSE: Record<string, string> = {
  running: "working",
  delivering: "handoff",
  idle: "sitting",
  dispatched: "idle",
  chained: "idle",
  waiting: "idle",
  failed: "idle",
};

export function spriteFor(who: { seat: string; state: string; carrying?: unknown }): string {
  const cast = CAST_SEATS.has(who.seat) ? who.seat : "generic";
  const pose = who.carrying ? "handoff" : (POSE[who.state] || "idle");
  return `/static/img/floor/${cast}/${pose}.png`;
}

/* A CHARACTER. Head, body, and the seat's own colour - blockout, like the
   rooms, and for the same reason: generated art replaces what is inside these
   two divs and finds the position, the state attribute and the hover text
   already decided for it.

   The title is the whole tooltip and it says WHAT THE SERVER SAID, item number
   included. Somebody who doubts a placement has to be able to check it against
   the board in one hover, or the floor is decoration.

   `data-walk` IS THE SEAT AND NOT THE ELEMENT. A character changes parent when
   it changes zone - out of the lounge, into a room - so React throws the node
   away and builds another, and useWalk matches the old position to the new one
   by this key. Name it anything less stable and the only moves worth animating
   are exactly the ones that stop animating.

   IT IS A BUTTON, and that is the whole of the keyboard story on this pane. A
   div with an onClick is unreachable by tab, unannounced by a screen reader and
   silent on Enter, and the three of those would have had to be re-implemented
   here by hand. The tooltip is the hover text and the aria-label is the same
   sentence, because what the mouse gets to read the keyboard has to as well.


   NO `onPick` MEANS THE CHARACTER IS NOT A CONTROL - the Director, whose panel
   is the console itself. Disabled rather than a second element type, so the
   walk, the states and the note it may be carrying all keep one implementation
   and a tab through the floor skips it the way it should. */
function Person({ who, delay = 0, onPick, picked }: {
  who: Occupant; delay?: number;
  onPick?: (who: Occupant) => void; picked?: boolean;
}) {
  const color = SEAT_COLOR[who.seat] || "var(--line-strong)";
  const hover = [
    who.seat,
    who.note,
    who.itemId ? `#${who.itemId}` : null,
    who.title,
    who.chainId ? `chain ${who.chainId}${who.chainPos ? ` lane ${who.chainPos}` : ""}` : null,
  ].filter(Boolean).join(" · ");
  return (
    /* `aria-current`, NOT `aria-selected`. This is a plain button, and ARIA only
       allows aria-selected on gridcell, option, row and tab - so the attribute
       was dropped by the accessibility tree and a screen reader read the
       selected character and every unselected one as the identical string, with
       nothing saying which one the inspector was describing. aria-current is
       allowed on any element and is announced. Absent rather than "false",
       because "not the current one" is not worth a word each. */
    <button type="button" className="bg4-person" data-state={who.state} title={hover}
            aria-label={hover} aria-current={picked ? "true" : undefined}
            disabled={!onPick}
            onClick={() => onPick?.(who)}
            data-walk={who.seat} data-walk-delay={delay}
            style={{ "--seat": color } as React.CSSProperties}>
      {/* THE CAST, generated in the bg-testbed sandbox and sliced by
          scripts/slice_floor_cast.py. The head-and-body shapes are still
          underneath: a seat this project invented has no sprite of its own, and
          a floor that drops a live agent because nobody drew it would be
          exactly the dishonesty this pane is built to avoid. The art covers
          them when it loads, and `generic` catches everything unnamed. */}
      <span className="bg4-person-art" aria-hidden="true"
            style={{ backgroundImage: `url(${spriteFor(who)})` }} />
      <span className="bg4-person-head" />
      <span className="bg4-person-body" />
      {/* THE NOTE IS CARRIED, not conjured at the office. A character walking
          with something in its hands is the whole reason the return trip reads
          as a delivery rather than as an agent wandering off. */}
      {who.carrying && <span className="bg4-person-note" />}
    </button>
  );
}

/* WHAT WAS HANDED OVER. The card is the work item's own result text and the
   status the board closed it with - no summary, no rewrite, no "completed
   successfully" standing in for a result nobody wrote. An agent that left no
   result says so, because "wrote nothing" is a thing worth seeing. */
function Note({ note }: { note: Handoff }) {
  const color = SEAT_COLOR[note.seat] || "var(--line-strong)";
  return (
    <div className="bg4-note" data-status={note.status}
         style={{ "--seat": color } as React.CSSProperties}
         /* The full 600 characters the server sent, on hover. The card clamps
            to three lines and a result is routinely longer, so without this the
            rest of it would exist nowhere on the pane. */
         title={note.result || "no result recorded"}>
      <span className="bg4-note-head">
        <Ti name={SEAT_ICON[note.seat] || "point"} size={11} />
        #{note.itemId}
        <span className="bg4-note-status">{note.status}</span>
      </span>
      <span className="bg4-note-title">{note.title}</span>
      <span className="bg4-note-body">
        {note.result || <em>no result recorded</em>}
      </span>
    </div>
  );
}

/* WHAT THE LOUNGE SAYS WHEN THE STUDIO IS EMPTY, and every choice in here is
   about making sure it cannot be read as an agent talking.

   IT IS A CAPTION ON THE ROOM, NOT SPEECH FROM A PERSON. No bubble, no tail,
   not anchored to a character, not in a seat's colour, and it sits below the
   crowd rather than beside anybody in it. The word `overheard` is on the line
   itself, in the same mono label the lounge and the queue rails use, so the
   thing that tells you what this is does not depend on the styling surviving a
   theme. A seat's real output has two homes - the transcript and the handover
   note - and neither of them looks like this.

   WHY IT IS NOT A LIVE REGION. Everything else on this floor that changes says
   something; this one is the only element on the pane that a screen reader
   announcing it would be interrupting a person for. It is readable in the tree
   and it is never announced.

   THE MUTE IS PART OF THE SAME STRIP AND IT IS DRAWN ONLY WHILE THE FLOOR IS
   QUIET. There is nothing to silence while work is running, and a permanent
   speaker icon in the middle of a working floor is chrome that means nothing
   six days out of seven. It stays visible when muted so the way back exists. */
function Banter({ line, on, onToggle }: {
  line: string; on: boolean; onToggle: () => void;
}) {
  return (
    <div className="bg4-banter" data-on={on ? "true" : undefined}>
      <button type="button" className="bg4-banter-mute" onClick={onToggle}
              aria-pressed={!on}
              title={on ? "quiet the lounge" : "let the lounge talk"}
              aria-label={on ? "quiet the lounge" : "let the lounge talk"}>
        <Ti name={on ? "volume-3" : "volume-off"} size={11} />
      </button>
      {/* The line is only in the DOM when there IS one. An empty caption box
          holding a space is a permanent widget for an occasional sentence. */}
      {on && line && (
        <p className="bg4-banter-line">
          <span className="bg4-banter-tag">overheard</span>
          {line}
        </p>
      )}
    </div>
  );
}

function Room({ seat, kind, who, delay, onPick, picked }: {
  seat: Seat; kind: "side" | "office"; who?: Occupant; delay?: number;
  onPick?: (who: Occupant) => void; picked?: boolean;
}) {
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
    /* `data-state` IS ON THE ROOM, not only on the character, because a failed
       item has to be visible when the character is not standing in the room to
       carry it - and because "room marked" is a property of the room. It is the
       occupant's state verbatim; the stylesheet decides which of them draw. */
    <div className={`bg4-room bg4-room-${kind}`} data-seat={seat.role}
         data-state={who ? who.state : "idle"}
         data-picked={picked ? "true" : undefined}
         style={{ "--seat": color || "var(--line-strong)" } as React.CSSProperties}>
      {/* THE WHOLE ROOM SELECTS THE SEAT, and the control is a LAYER rather
          than a wrapper because the character standing in the room is its own
          button - a button inside a button is invalid, and browsers resolve it
          by throwing one of them away. Absolutely positioned and first in the
          room, so the stage (positioned, and later in the DOM) keeps the
          character clickable on top of it. THE BODY AND THE STAGE BOTH DECLINE
          POINTER EVENTS in floor.css so this layer really does get the room:
          the body is positioned and later in the DOM too, and while it took the
          clicks the only part of a room that selected anything was the wall
          band across the top.

          NOT DRAWN AT ALL when there is nobody to select or nothing to open -
          the Director's office, and a room whose seat the poll said nothing
          about. A hit layer over a room that answers no click is a click the
          reader spends finding that out. */}
      {onPick && who && (
        /* aria-current for the same reason the character uses it - see Person. */
        <button type="button" className="bg4-room-hit"
                aria-current={picked ? "true" : undefined}
                aria-label={`${label} room, ${who.note}`}
                onClick={() => onPick(who)} />
      )}
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
        {/* WHERE THE PEOPLE GO, and the desk they are either at or not at. The
            desk is drawn in every room whether or not anybody is in it, because
            "at the desk" only means something if the desk is a fixed place. The
            character is placed against the stage box, so which corner it lands
            in is one attribute and not a layout of its own. */}
        <div className="bg4-room-stage">
          <span className="bg4-desk" />
          {/* Only the occupants whose zone IS this room. A seat standing at the
              Director's door or out in the lounge is drawn there and must not
              also be drawn here, or one agent is in two places. */}
          {who && (who.zone === "desk" || who.zone === "room")
            && <Person who={who} delay={delay} onPick={onPick} picked={picked} />}
        </div>
      </div>
    </div>
  );
}

export function FloorPane({ state }: { state: ConsoleState }) {
  /* The project's own seat table. Read once, not per poll: it changes when
     somebody edits seat config, which is not a thing that happens mid-session -
     the same call and the same reasoning as the composer's seat menu.

     THE ANSWERS ARE KEPT APART, because the pane says something different about
     each and it used to say "no seats configured" to all of them: `null` is "not
     read yet", `seatsError` is "the request failed", `noProject` is "there is no
     project to have seats", and a real empty array is the only one of the four
     that is a fact about this project's configuration. */
  const [seats, setSeats] = useState<Seat[] | null>(null);
  const [seatsError, setSeatsError] = useState("");
  const [noProject, setNoProject] = useState(false);
  useEffect(() => {
    readJSON<{ seats: { role?: string; title?: string }[]; project?: unknown }>(
      "/api/state", { seats: [] })
      .then((d) => {
        /* readJSON hands back the fallback tagged with __error rather than
           throwing, so an unread seat table looks exactly like a project with
           no seats unless this is checked. Left as `null` on failure: we still
           do not know what the table is. */
        if (d.__error) { setSeatsError(d.__error); return; }
        setSeatsError("");
        setNoProject(!d.project);
        setSeats((d.seats || [])
          .filter((r): r is Seat => !!r.role)
          .map((r) => ({ role: r.role, title: r.title })));
      });
  }, []);

  /* ONE CHEAP STRING PER POLL instead of a rebuild of the whole floor. See
     floorSignature: consoleState() hands back a new object and a new items
     array every 3s whether or not anything changed, so every memo below keyed on
     `state` missed on every tick and the room tree was rebuilt - which is also
     what kept handing useWalk a commit to re-measure. */
  const sig = useMemo(() => floorSignature(state), [state]);

  /* THE SEAT TABLE AND THE SEATS THE WORK IS ADDRESSED TO, UNIONED - never one
     or the other. This took the items' seats only when the table was ENTIRELY
     empty, which drops a live agent off the floor in the case that matters:
     seats.roles_for excludes disabled seats and /api/state only returns enabled
     ones, while work already filed to a seat survives seat_configure(false) and
     stays dispatchable. Disabling cinematic mid-run therefore left item #12
     running with no room, no character and nothing on the floor about it, on a
     pane whose whole claim is that position is state. The configured seats come
     first so the arrangement does not reshuffle when an off-table seat appears;
     /api/state answering with no seats at all (no project selected) still leaves
     the item seats as a true, if partial, roster. */
  const floor = useMemo(() => {
    const table: Seat[] = seats ? [...seats] : [];
    const known = new Set(table.map((s) => s.role));
    for (const item of state.items) {
      if (item.seat && !known.has(item.seat)) {
        known.add(item.seat);
        table.push({ role: item.seat });
      }
    }
    return planFloor(table);
    // `sig` stands in for state.items - see floorSignature.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seats, sig]);

  /* What closed while this session was watching. The only memory on the floor,
     and it is kept out of placeFloor so that function stays a pure reading of
     one poll. */
  const handoffs = useHandoffs(state);

  /* WHO IS WHERE, recomputed every poll and remembered between none of them.
     The roster passed in is the floor's own - the rooms that exist - so a seat
     with work but no room cannot put a character in a corridor, and a room with
     no news gets an idle occupant rather than an empty one. */
  const people = useMemo(() => {
    const roster = [...floor.left, ...floor.right,
                    ...(floor.director ? [floor.director] : [])].map((s) => s.role);
    const placed = placeFloor(roster, state, handoffs.bySeat);
    return new Map(placed.map((p) => [p.seat, p]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [floor, sig, handoffs]);

  /* HOW LONG EACH CHARACTER WAITS BEFORE IT MOVES, and the rule is the feature:
     A CHAIN TRAVELS TOGETHER. Every link of one chain is filed in the same act
     and steps off on the same frame, so the group reads as one decision leaving
     the lounge; seats that have nothing to do with each other are staggered so
     a floor-wide reshuffle does not look like one sliding object. The stagger
     is small and capped, because it is spent out of the budget useWalk has to
     land everybody before the next poll. */
  const delays = useMemo(() => {
    const out = new Map<string, number>();
    let n = 0;
    for (const p of people.values()) {
      out.set(p.seat, p.chainId ? 0 : (n++ % 4) * 70);
    }
    return out;
  }, [people]);

  /* THE LOUNGE ONLY TALKS INTO SILENCE, and `floorIsQuiet` is the whole of that
     rule (see banter.ts). It is asked of the PLACEMENT rather than of the raw
     poll, because the placement is what is on screen: if a character is drawn
     at a desk, the floor is not quiet, whatever a count says. */
  const [banterOn, setBanterOn] = useState(banterOnStored);
  const quiet = useMemo(
    () => floorIsQuiet(state, people.values(), handoffs.notes.length),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sig, people, handoffs]);
  const toggleBanter = () => setBanterOn((was) => {
    storeBanterOn(!was);
    return !was;
  });

  /* The positions the last commit left, and the box they are measured inside.
     A ref rather than state: nothing renders from it, it is the other end of
     the interpolation, and putting it in state would loop a render per poll for
     a value no JSX reads. */
  const wrap = useRef<HTMLDivElement>(null);
  const seen = useRef(new Map<string, { x: number; y: number }>());

  /* IS THIS PANE ON SCREEN AT ALL. The shell hides an inactive `.deck-view` with
     display:none rather than unmounting it, so switching to another screen
     leaves this component mounted with its timers running. usePoll is already
     gated on the same flag up in Agents; the banter rotation was not, so a floor
     left quiet and then navigated away from went on setting a new line every 15s
     for the life of the session, each one committing this pane and re-measuring
     a floor nobody can see. */
  const visible = useViewActive(wrap);
  const banter = useBanter(quiet && visible, banterOn);

  /* The queue at the Director's door, in the roster's order so it does not
     reshuffle itself between polls while nothing has changed. */
  const atDoor = [...people.values()].filter((p) => p.zone === "door");
  const inLounge = [...people.values()].filter((p) => p.zone === "lounge");
  const atOffice = [...people.values()].filter((p) => p.zone === "office");

  /* WHAT IS SELECTED IS THE SHELL'S, NOT THIS PANE'S. The inspector lives in
     another React root, which is why selection.ts is a store outside React at
     all; reading it here is what lets the room the panel is describing stay
     marked while you look at it. */
  const [sel, setSel] = useSelection();
  const pick = (who: Occupant) => setSel(selectionFor(who));
  /* The seat whose selection is open, or "". Compared by SEAT rather than by
     key so the mark survives that seat's item changing under it: the item id
     moves on the next poll, the room the reader clicked did not. */
  const picked = sel?.seat || "";

  const sides = floor.left.length + floor.right.length;

  /* WHAT MAKES THE FLOOR WORTH MEASURING AGAIN - see useWalk, which runs on
     this and on nothing else. Everything in here can move a character: the
     placement itself, the notes on the desk (which grow the office row and shift
     every room above it), the lounge caption (which does the same to the
     lounge), and the number of side rooms, which is the arrangement. What is
     deliberately NOT in here cannot change the layout - the selection is a
     box-shadow and a colour - so it must not cost the floor a measurement. */
  const walkKey = [
    sides,
    [...people.values()].map((p) => `${p.seat}:${p.state}:${p.zone}`).join(","),
    handoffs.notes.map((n) => n.itemId).join(","),
    quiet && banterOn ? banter : "",
  ].join("|");
  useWalk(wrap, seen.current, walkKey);

  if (!sides && !floor.director) {
    /* NAME THE ACTUAL CASE. This printed "no seats configured" for all four of
       them, which is a claim about the project's configuration made from a
       request that had not come back yet, or had failed, or had answered "there
       is no project". */
    return <div className="bg4-floorwrap"><div className="bg4-floor-none">
      {state.__error ? `the board is not answering: ${state.__error}`
        : seatsError ? `the seat table could not be read: ${seatsError}`
        : seats === null ? "reading the seat table…"
        : noProject ? "no project is selected"
        : "no seats configured"}
    </div></div>;
  }

  return (
    /* `data-sides` IS WHAT LETS THE FLOOR SURVIVE A SMALL SEAT TABLE. A project
       running Director plus one seat has nothing to put in the third column,
       and a three-column grid with two empty tracks is a room floating in the
       middle of a lot of nothing. The stylesheet collapses the arrangement at
       0 and 1, and the rule stays in CSS rather than becoming a second JSX
       branch to keep one floor, not three. */
    <div className="bg4-floorwrap" ref={wrap}>
      {/* THE FLOOR SAYS WHEN IT IS NO LONGER BEING TOLD ANYTHING. The poll keeps
          the last payload it received when a read fails (see Agents.refresh),
          so every character below is standing where the last ANSWERED poll put
          it. Without this line the pane would go on asserting that placement as
          the present, which is the one thing it exists not to do. */}
      {state.__error && (
        <div className="bg4-floor-stale" role="status">
          <Ti name="alert-triangle" size={12} />
          <span>
            {state.__error} · this is the last floor the board reported, not the
            floor right now
          </span>
        </div>
      )}
      <div className="bg4-studio" data-sides={Math.min(sides, 2)}>
        <div className="bg4-floor-col bg4-floor-left">
          {floor.left.map((s) =>
            <Room key={s.role} seat={s} kind="side" who={people.get(s.role)}
                  delay={delays.get(s.role)} onPick={pick}
                  picked={picked === s.role} />)}
        </div>

        {/* THE LOUNGE IS NOT A SEAT and has no room chrome - no wall band, no
            nameplate. It is the middle of the floor, which is where an agent
            stands when it holds no work, and it has to read as open ground or
            "idle" looks like an eighth department. */}
        <div className="bg4-lounge">
          <span className="bg4-lounge-label">lounge</span>
          {/* THE CROWD IS NOT POSITIONED, it is a row that wraps. Every idle
              seat stands here at once and a floor of twelve idle agents has to
              stay legible; scattering them would also imply each one is
              somewhere in particular, and the whole point of the lounge is that
              they are not. */}
          <div className="bg4-lounge-stage">
            {inLounge.map((p) =>
              <Person key={p.seat} who={p} delay={delays.get(p.seat)}
                      onPick={pick} picked={picked === p.seat} />)}
          </div>
          {/* UNDER THE CROWD, and only when there is no work anywhere. It is
              the last thing in the lounge so it reads as a caption on the room
              rather than as anything coming out of the people standing in it. */}
          {quiet && <Banter line={banter} on={banterOn} onToggle={toggleBanter} />}
        </div>

        <div className="bg4-floor-col bg4-floor-right">
          {floor.right.map((s) =>
            <Room key={s.role} seat={s} kind="side" who={people.get(s.role)}
                  delay={delays.get(s.role)} onPick={pick}
                  picked={picked === s.role} />)}
        </div>

        {/* THE OFFICE ROW IS DRAWN WHENEVER THERE IS EITHER AN OFFICE OR A
            QUEUE. A project can disable the director seat, and a waiting agent
            must not vanish with the room it was queuing outside of - it is
            still waiting, and dropping it would be the exact dishonesty this
            pane exists to avoid. With no office the queue stands on its own and
            says what it is waiting for. */}
        {(floor.director || atDoor.length > 0
          || atOffice.length > 0 || handoffs.notes.length > 0) && (
          <div className="bg4-floor-office">
            {/* Beside the office, not inside it: these agents have not been
                seen, and the distance is the message. Rendered only when
                somebody is in it - a permanent empty rail stops meaning
                anything within a day. */}
            {atDoor.length > 0 && (
              <div className="bg4-queue">
                <span className="bg4-queue-label">waiting on you</span>
                <div className="bg4-queue-line">
                  {atDoor.map((p) =>
                    <Person key={p.seat} who={p} delay={delays.get(p.seat)}
                            onPick={pick} picked={picked === p.seat} />)}
                </div>
              </div>
            )}
            {floor.director && (
              /* THE DIRECTOR'S OFFICE IS NOT A CONTROL, and that is a decision
                 rather than an omission: the main console IS the Director's
                 chat, so a panel here would be a worse copy of the screen this
                 room is standing on. No onPick means no hit layer and a
                 character that cannot be focused - not a control that opens
                 something redundant. */
              <Room seat={floor.director} kind="office"
                    who={people.get(floor.director.role)}
                    delay={delays.get(floor.director.role)} />
            )}
            {/* THE OTHER SIDE OF THE DOOR. The queue is people who want
                something from the human; this is people who brought something
                to it. They are opposite sides of the office on purpose: a line
                and a delivery both look like "a group by the door" and the two
                mean nearly opposite things.

                THE NOTES OUTLIVE THE WALK. A seat that closes an item and is
                handed the next one in the same tick never gets to make the
                trip - it is already running, and drawing it here would hide
                live work behind a courtesy - but the note it wrote is a fact
                about that item and stays on the desk either way. */}
            {(atOffice.length > 0 || handoffs.notes.length > 0) && (
              <div className="bg4-handover">
                <span className="bg4-handover-label">handed over</span>
                <div className="bg4-handover-line">
                  {atOffice.map((p) =>
                    <Person key={p.seat} who={p} delay={delays.get(p.seat)} />)}
                </div>
                <div className="bg4-notes">
                  {handoffs.notes.map((n) =>
                    <Note key={n.itemId} note={n} />)}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
