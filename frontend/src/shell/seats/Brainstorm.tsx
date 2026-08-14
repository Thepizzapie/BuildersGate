import type { SeatBodyProps } from "./types";
import { Room } from "../brainstorm/Room";

/* THE SEAT'S BRAINSTORM ROOM.
 *
 * THIS TAB WENT MISSING IN THE REWRITE AND SHOULD NOT HAVE. The reference gives
 * director and narrative a Brainstorm tab each — they are the only two seats the
 * backend lets open a room — and the first React pass shipped neither, so the
 * room simply vanished from the seat.
 *
 * IT CAME BACK AS THE WRONG ROOM, WHICH WAS WORSE THAN MISSING. The first
 * restoration mounted the classic brainstorm.js workspace — chat, a writing pad,
 * a drawing pad — on the argument that re-implementing its drawing surface in
 * React would be a second editor for one scene. That argument is still true and
 * it answered a question nobody was asking. The narrative room's whole claim is
 * that you think AGAINST THE CANON: the locked facts pinned at the top because
 * they are the ones nothing may contradict, the entities in play beside the
 * turn that put them there, and a door that files canon and nothing else. The
 * classic workspace does not know canon exists. Three panes of empty scratch
 * space is not that room; it is a notepad wearing its name.
 *
 * So this mounts shell/brainstorm/Room — the room that already has the canon
 * column — scoped to the seat. Not a copy of it: the same component, with a
 * `seat` that narrows which rooms the rail lists and which one opens first, and
 * with every guarantee the full screen has still attached. See Room.tsx on why
 * scoping is only those two things.
 *
 * THE PADS ARE NOT LOST. Room opens brainstorm.js full-size in a modal over
 * itself, which is where a 2,700-line editor with its own hit-testing belongs —
 * and only one of the two is ever driving the scene, which is the thing an
 * inline second editor could not promise.
 */

export function SeatBrainstorm({ seat }: SeatBodyProps) {
  /* Keyed by the caller (Seats.tsx) so switching between the two seats that
     have a room remounts rather than re-scoping in place. */
  return (
    <div className="bgs-bshost">
      <Room seat={seat.role} />
    </div>
  );
}
