import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Ti } from "../Ti";
import { SEAT_COLOR, SEAT_ICON } from "../nav";
import { useSelection, type Selection } from "../selection";
import { readJSON } from "../../bridge";
import { useViewActive } from "../../hooks";
import { type ConsoleState } from "./api";
import { floorSignature, placeFloor, type Occupant } from "./occupancy";
import {
  planFloor, spotFor, type FloorPlan, type Persona, type Seat, type Spot,
} from "./floorplan";
import { FloorCanvas } from "./FloorCanvas";
import { buildNav } from "./route";
import { useHandoffs, type Handoff } from "./handoff";
import {
  banterOnStored, floorIsQuiet, readTopic, storeBanterOn, useBanter,
} from "./banter";
import { useFloorMusic, type MusicDeck } from "./floorMusic";
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
 * IT IS ONE BUILDING NOW, NOT NINE CARDS. This pane's first version drew each
 * room as an independent rounded box in a flex column with a gutter beside it,
 * which is a component list wearing the word "room": no wall was shared, no
 * doorway existed, and there was nowhere for a character to BE while it crossed
 * the floor - it teleported between two boxes with a gap in between. The
 * geometry is now computed in floorplan.ts as cell rectangles inside ONE
 * footprint, walls are drawn on the cell edges between rooms, doorways are gaps
 * cut in those walls, and the corridor is the space left over. This file does
 * not decide where anything is; it converts cells to lengths and paints.
 *
 * THE BUILDING IS PAINTED, NOT LAID OUT. It was sixty positioned elements in a
 * perspective-transformed div, and it read as a web page wearing a floor plan
 * because that is what it was. floorRender.ts draws the whole of it into one
 * canvas - tiled floors, composited sprites, depth sorted by the y of an
 * object's feet, which is how a game draws a room. This file is now the DATA:
 * it reads the poll, asks floorplan/occupancy/route where everything is, and
 * hands the answer to <FloorCanvas />.
 *
 * THE CAMERA LIVES IN THE ART. The cast and the environment sprites are drawn
 * to a high three-quarter, about 70 degrees above the floor, so painting them
 * onto a flat plane IS that camera. The DOM version applied a rotateX on top of
 * art that already had the angle in it, then counter-rotated every character to
 * undo it - which is drawing the camera twice and then arguing with itself.
 *
 * THE OCCUPANTS ARE PLACED BY occupancy.ts AND BY NOTHING IN HERE. Every
 * character on this floor stands where one row of /api/console/state put it,
 * and a seat nothing was reported about is drawn idle rather than guessed at -
 * a figure sat at a desk that is not running would be worse than an empty
 * pane, because it stops you going to look. occupancy.ts chooses the ZONE and
 * floorplan.ts turns the zone into a coordinate; neither one invents a state.
 *
 * THE MOVEMENT IS INTERPOLATED BETWEEN POLLS AND THE POLL DID NOT GET FASTER.
 * consoleState() ticks at 3s and that is the only clock this pane has; the
 * renderer walks each character from the placement it was last handed to the
 * one it is holding now. Every character is therefore drawn where the LAST poll
 * put it, where THIS one did, or somewhere on the routed line between the two -
 * never at a coordinate the server never described.
 *
 * AND IT WALKS THROUGH THE DOORWAYS TO GET THERE. The two ends of a journey are
 * the server's; the line between them is not, and on a one-building floor plan
 * that line runs through partitions. route.ts paths out of the room, along the
 * corridor and in through the destination's door, computed from the same wall
 * list the walls are PAINTED from - so a door is passable for the reason it looks
 * passable, and a plan with a seat disabled re-routes everybody for free. A
 * figure sliding through a wall would say the walls are a picture of walls,
 * which is the card layout's teleport with a tween on it.
 *
 * THE LAYOUT IS GENERATED FROM THE SEAT TABLE, NEVER FROM A LIST IN HERE.
 * Seats are per project - a project can disable qa or cinematic - so a
 * hardcoded seven-room floor would draw rooms for staff that do not exist and
 * silently omit the ones that do. What is fixed is the ARRANGEMENT RULE: craft
 * rooms ring the outside, the lounge is the middle, the Director is at the
 * bottom because the Director is who you walk to.
 *
 * CLICKING DRIVES THE SHELL'S SELECTION AND THE FLOOR OWNS NO INSPECTOR. A room
 * and the character in it are two controls that resolve to the SAME selection
 * key as the board's card for that item - `i<id>` - so clicking an agent here
 * and clicking its card over on the board are one act, not two panels
 * disagreeing about what is selected.
 */

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

/* WHAT WAS HANDED OVER. The card is the work item's own result text and the
   status the board closed it with - no summary, no rewrite, no "completed
   successfully" standing in for a result nobody wrote. An agent that left no
   result says so, because "wrote nothing" is a thing worth seeing.

   IT IS UNDER THE BUILDING, NOT IN IT. A note is text somebody reads; the
   building is a painted image, and the one thing on this pane that has to be
   legible must not be inside it. */
function Note({ note }: { note: Handoff }) {
  const color = SEAT_COLOR[note.seat] || "var(--line-strong)";
  return (
    <div className="bg4-handover-note" data-status={note.status}
         style={{ "--seat": color } as React.CSSProperties}
         /* The full 600 characters the server sent, on hover. The card clamps
            to three lines and a result is routinely longer, so without this the
            rest of it would exist nowhere on the pane. */
         title={note.result || "no result recorded"}>
      <span className="bg4-handover-note-head">
        <Ti name={SEAT_ICON[note.seat] || "point"} size={11} />
        #{note.itemId}
        <span className="bg4-handover-note-status">{note.status}</span>
      </span>
      <span className="bg4-handover-note-title">{note.title}</span>
      <span className="bg4-handover-note-body">
        {note.result || <em>no result recorded</em>}
      </span>
    </div>
  );
}

/* WHAT THE LOUNGE SAYS WHEN THE STUDIO IS EMPTY, and every choice in here is
   about making sure it cannot be read as an agent talking.

   IT IS A CAPTION ON THE BUILDING, NOT SPEECH FROM A PERSON. No bubble, no
   tail, not anchored to a character, not in a seat's colour, and it sits under
   the building rather than beside anybody on it. The word `overheard` is on
   the line itself, in the same mono label the rest of the pane uses, so the
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

/* THE SOUNDTRACK'S ONE CONTROL, and it is deliberately small: a switch, what is
   playing, a skip and a level. Everything else about the deck - the shuffle, the
   no-repeat bag, where the files live - is in floorMusic.ts.

   IT IS DRAWN WHETHER OR NOT THE FLOOR IS QUIET, unlike the banter, and the two
   differ for a reason that is not inconsistency. Banter is words, and words
   compete with an agent's real output, so they stop the moment there is work.
   Music is not words. It is what the studio sounds like while the studio works,
   and a soundtrack that cut out every time somebody started a task would be
   worse than no soundtrack.

   NOTHING IS DRAWN WHEN NO SET IS GENERATED. A mute button for silence is a
   control that answers nothing, and the line that says so belongs in the
   generator's output, not in the middle of the floor. */
function Music({ deck }: { deck: MusicDeck }) {
  if (!deck.tracks || deck.tracks.length === 0) return null;
  return (
    <div className="bg4-music" data-on={deck.on ? "true" : undefined}>
      {/* THE SWITCH LIVES ON THE FLOOR, NOT HERE. It is the radio in the
          lounge, and this button is its keyboard equivalent - a painted prop
          cannot be tabbed to, and "click the thing in the room" is not an
          instruction a screen reader can carry out. Same act, two doors. */}
      <button type="button" className="bg4-music-toggle" onClick={deck.toggle}
              aria-pressed={deck.on}
              title={deck.on ? "turn the lounge radio off" : "turn the lounge radio on"}
              aria-label={deck.on ? "turn the lounge radio off" : "turn the lounge radio on"}>
        <Ti name={deck.on ? "volume-3" : "volume-off"} size={11} />
      </button>
      {deck.on && (
        <>
          {/* THE TITLE IS THE GENERATOR'S, NOT A FILENAME. A reader who likes a
              track has to be able to name it to regenerate around it. */}
          <span className="bg4-music-now">
            {deck.now ? deck.now.title : "…"}
            {deck.now?.genre && (
              <span className="bg4-music-genre">{deck.now.genre}</span>
            )}
          </span>
          <button type="button" className="bg4-music-skip" onClick={deck.skip}
                  title="next track" aria-label="next track">
            <Ti name="chevrons-right" size={11} />
          </button>
          <input className="bg4-music-vol" type="range" min={0} max={1} step={0.05}
                 value={deck.volume} aria-label="music volume"
                 onChange={(e) => deck.setVolume(Number(e.target.value))} />
        </>
      )}
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

  /* THE BOX THE PANE IS MEASURED INSIDE, and the visibility flag below is read
     by the seat fetch as well as the banter, so both are declared before it. */
  const wrap = useRef<HTMLDivElement>(null);
  const paneVisible = useViewActive(wrap);

  /* RE-READ WHEN THE FLOOR COMES BACK ON SCREEN.
   *
   * This ran ONCE, on mount, with an empty dependency array - which was right
   * when a seat table only changed if somebody edited seat config by hand, and
   * wrong the moment the Look panel existed. Renaming a seat, re-flooring its
   * room or giving it a different character updated the database and the floor
   * went on drawing what it had read when it mounted. The shell keeps an
   * inactive pane MOUNTED with display:none, so "switch to Seats, rename one,
   * switch back" never remounted this and the change looked like it had not
   * saved.
   *
   * ON VISIBILITY RATHER THAN ON A TIMER, because a seat table is not live
   * data: it changes when a person changes it, and the only moment that matters
   * is when they come back to look. A poll would be a request every few seconds
   * for a value that is identical almost always. */
  /* Bumped by the seat-changed event below, which is what forces the read to
     happen again. A counter rather than a boolean: two edits in a row have to
     produce two reads. */
  const [seatEpoch, setSeatEpoch] = useState(0);
  useEffect(() => {
    const again = () => setSeatEpoch((n) => n + 1);
    window.addEventListener("bgate:seats-changed", again);
    return () => window.removeEventListener("bgate:seats-changed", again);
  }, []);

  useEffect(() => {
    if (!paneVisible) return;
    readJSON<{ seats: { role?: string; title?: string; persona?: Persona }[];
               project?: unknown }>("/api/state", { seats: [] })
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
          /* THE PERSONA COMES ALONG. It is what tells the floor which sprite
             walks in this room, what the floor is made of and what the
             nameplate says - all of which used to be decided by the seat's
             name inside the renderer. Dropping it here would leave the whole
             feature reading defaults. */
          .map((r) => ({ role: r.role, title: r.title, persona: r.persona })));
      });
  }, [paneVisible, seatEpoch]);

  /* ONE CHEAP STRING PER POLL instead of a rebuild of the whole floor. See
     floorSignature: consoleState() hands back a new object and a new items
     array every 3s whether or not anything changed, so every memo below keyed on
     `state` missed on every tick and the building was re-planned - which is also
     what kept handing the walk a commit to re-measure. */
  const sig = useMemo(() => floorSignature(state), [state]);

  /* THE SEAT TABLE AND THE SEATS THE WORK IS ADDRESSED TO, UNIONED - never one
     or the other. This took the items' seats only when the table was ENTIRELY
     empty, which drops a live agent off the floor in the case that matters:
     seats.roles_for excludes disabled seats and /api/state only returns enabled
     ones, while work already filed to a seat survives seat_configure(false) and
     stays dispatchable. Disabling cinematic mid-run therefore left item #12
     running with no room, no character and nothing on the floor about it, on a
     pane whose whole claim is that position is state. The configured seats come
     first so the arrangement does not reshuffle when an off-table seat appears. */
  const plan: FloorPlan = useMemo(() => {
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
     The roster passed in is the floor's own - the seats that HAVE a room - so a
     room with no news gets an idle occupant rather than an empty one. */
  const people = useMemo(() => {
    const roster = [...plan.bySeat.keys()];
    return placeFloor(roster, state, handoffs.bySeat);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan, sig, handoffs]);

  /* THE COORDINATE FOR EACH CHARACTER. The zone is occupancy.ts's answer and the
     coordinate is floorplan.ts's; this only counts how many are already in a
     zone so two agents in the lounge do not stand inside each other. Counted in
     ROSTER ORDER, which is the seat table's order, so the crowd does not
     reshuffle itself between polls while nothing has changed. */
  const spots = useMemo(() => {
    const nth = new Map<string, number>();
    const out = new Map<string, Spot>();
    for (const p of people) {
      const i = nth.get(p.zone) || 0;
      nth.set(p.zone, i + 1);
      out.set(p.seat, spotFor(plan, p.seat, p.zone, i));
    }
    return out;
  }, [people, plan]);

  /* THE LOUNGE ONLY TALKS INTO SILENCE, and `floorIsQuiet` is the whole of that
     rule (see banter.ts). It is asked of the PLACEMENT rather than of the raw
     poll, because the placement is what is on screen: if a character is drawn
     at a desk, the floor is not quiet, whatever a count says. */
  const [banterOn, setBanterOn] = useState(banterOnStored);
  const quiet = useMemo(
    () => floorIsQuiet(state, people, handoffs.notes.length),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sig, people, handoffs]);
  const toggleBanter = () => setBanterOn((was) => {
    storeBanterOn(!was);
    return !was;
  });

  /* THE ROUTER FOR THIS BUILDING. Rebuilt only when the plan is - the walls are
     a fact about the floor plan, not about who is standing on it - so an idle
     board with nothing happening never touches it, and a seat being disabled
     re-derives every route from the walls that are actually drawn. */
  const nav = useMemo(() => buildNav(plan), [plan]);

  /* IS THIS PANE ON SCREEN AT ALL. The shell hides an inactive `.deck-view` with
     display:none rather than unmounting it, so switching to another screen
     leaves this component mounted with its timers running. usePoll is already
     gated on the same flag up in Agents; the banter rotation was not, so a floor
     left quiet and then navigated away from went on setting a new line every 15s
     for the life of the session. */
  /* One measurement, read twice. Calling useViewActive again on the same ref
     would install a second observer for an answer this component already has. */
  const visible = paneVisible;
  /* WHAT THE LOUNGE CAN JOKE ABOUT TODAY, read off the same payload the floor
     is drawn from. Keyed on `sig` like everything else here: the poll hands
     back a new items array every 3s whether or not anything changed, and
     re-deriving the topic on each one would rebuild the banter deck three times
     a minute. */
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const topic = useMemo(() => readTopic(state), [sig]);
  /* WHO IS AVAILABLE TO SPEAK. Only people standing in the LOUNGE, which is
     both the plan's rule and the honest one: the whole gate on this feature is
     that nobody is working, and the lounge is where occupancy.ts puts somebody
     with nothing to do. A character talking at its own desk would be a
     character talking while at work, which is the thing that must never be
     drawn. The Director is excluded - the console is where it speaks. */
  const speakers = useMemo(
    () => people.filter((p) => p.zone === "lounge" && p.seat !== "director")
                .map((p) => p.seat),
    [people]);
  /* A SEAT'S OWN LINES, looked up through the plan so the renderer and the
     banter agree about who this character is. */
  const linesFor = useCallback(
    (seat: string) => plan.bySeat.get(seat)?.persona?.lines || [],
    [plan]);
  const banter = useBanter(quiet && visible, banterOn, undefined, undefined,
                           topic, speakers, linesFor);

  /* THE DECK IS THE PANE'S, NOT THE VIEW'S. The shell hides an inactive pane
     with display:none rather than unmounting it, so a floor navigated away from
     is still mounted - and unlike the banter rotation, that is CORRECT for
     audio: somebody who put the music on and went to look at the board wanted
     the music on, not paused because a tab changed. It stops when the pane
     unmounts, which is when the console itself goes. */
  const music = useFloorMusic();

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

  if (plan.bySeat.size === 0) {
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

      {/* THE BUILDING, PAINTED RATHER THAN LAID OUT. Every room, wall, prop and
          character is one drawImage into a single canvas, depth-sorted by the y
          of its feet - which is how a game draws a floor and is not something a
          document layout engine can be talked into. The camera angle lives in
          the ART: the cast and the environment are drawn to a high
          three-quarter, so painting them onto a flat plane IS that camera. The
          DOM version applied a rotateX on top of art that already had the angle
          baked in, which is drawing the camera twice.

          THE FLOOR IS AN IMAGE, SO IT IS DESCRIBED IN TEXT BESIDE IT. A canvas
          has no accessibility tree; the seat rail and the board both list the
          same placements as real controls, and the underfloor strip below
          carries the words. */}
      <div className="bg4-stage">
        <FloorCanvas plan={plan} people={people} spots={spots} nav={nav}
                     picked={picked} onPick={pick}
                     /* THE RADIO IN THE LOUNGE IS THE SWITCH. Not a widget in
                        the chrome - a thing in the room, which is the whole
                        point of having a room. It only answers a click when
                        there is a set to play; with no tracks generated it is
                        scenery, because a switch that does nothing is worse
                        than a prop. */
                     musicOn={music.on}
                     onRadio={music.tracks?.length ? music.toggle : undefined}
                     banter={banterOn && quiet && banter.seat && banter.line
                       ? { seat: banter.seat, line: banter.line } : null} />
      </div>
      <ul className="bg4-floor-sr">
        {people.map((p) => (
          <li key={p.seat}>
            <button type="button" onClick={() => pick(p)}
                    aria-current={picked === p.seat ? "true" : undefined}>
              {[p.seat, p.note, p.itemId ? `#${p.itemId}` : null, p.title]
                .filter(Boolean).join(" · ")}
            </button>
          </li>
        ))}
      </ul>

      {/* UNDER THE BUILDING: the things that are text. The handover notes and
          the lounge caption are read, not looked at, and text laid into a
          tilted plane is text on a slant. */}
      <div className="bg4-underfloor">
        {handoffs.notes.length > 0 && (
          <div className="bg4-handover">
            <span className="bg4-handover-label">handed over</span>
            <div className="bg4-handovers">
              {handoffs.notes.map((n) => <Note key={n.itemId} note={n} />)}
            </div>
          </div>
        )}
        {/* THE CAPTION STRIP NOW ONLY CARRIES A LINE NOBODY IS SAYING. With a
            speaker available the line is drawn as a bubble over their head on
            the floor, and printing it here as well would be the exact failure
            Cat.tsx recorded: the same sentence twice on one screen, six lines
            apart. The mute stays either way, because it is how the whole
            feature is turned off. */}
        {quiet && (
          <Banter line={banter.seat ? "" : banter.line} on={banterOn}
                  onToggle={toggleBanter} />
        )}
        <Music deck={music} />
      </div>
    </div>
  );
}
