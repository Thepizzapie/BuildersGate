import { useLayoutEffect, type RefObject } from "react";

/* THE WALK. Movement between two placements, and nothing else.
 *
 * THE POLL STAYS AT 3s. This is the whole reason this file exists: the floor
 * became watchable by animating BETWEEN two known states, not by asking the
 * server more often. A dashboard that polls at 10Hz to look smooth is a
 * dashboard that costs a request every 100ms per open tab, forever, and the
 * console payload is already the expensive call on this page. Nothing in here
 * fetches anything.
 *
 * IT IS FLIP, AND IT IS FLIP BECAUSE THE CHARACTERS CHANGE PARENTS. A seat that
 * leaves the lounge for its desk is not one element moving inside one box - it
 * is unmounted from `.bg4-lounge-stage` and mounted into that room's stage, so
 * there is no CSS transition that could ever see the two positions. FLIP
 * measures where the character WAS at the end of the previous commit, and after
 * the new commit puts the difference back on as a transform and lets it tween
 * to zero. The layout is never faked: at every moment the DOM says exactly what
 * occupancy.ts placed, and only the paint is behind.
 *
 * THE WALK MUST FINISH BEFORE THE NEXT POLL. A tween still running when the
 * next state lands gets measured mid-corridor, and the character then walks
 * from a place it was only passing through - which reads as a teleport and
 * makes the position stop meaning anything. Duration plus delay is capped well
 * under POLL_MS for that reason; the cap is not styling.
 *
 * REDUCED MOTION SNAPS. Not "moves slower", not "fades": no inverse transform
 * is applied at all, so the character is simply drawn where the poll put it -
 * which is precisely the step-1 behaviour, and the pane was honest then too.
 */

/** Longest a character may be in transit. Below POLL_MS with room to spare. */
const MAX_MS = 1500;
/** Even a step across one room takes a moment, or it reads as a jump cut. */
const MIN_MS = 420;
/** Milliseconds per pixel of travel. A character crossing the whole floor takes
 *  visibly longer than one standing up from its desk, which is the only thing
 *  that makes the distance mean anything. */
const PER_PX = 1.2;

type Pos = { x: number; y: number };

/** Animate every `[data-walk]` inside `root` from wherever it was last commit.
 *
 * `data-walk` is the character's identity across parents (the seat), NOT the
 * element: React really does throw the node away when a seat changes zone, so
 * keying on the node would lose exactly the moves worth showing.
 *
 * `data-walk-delay` is milliseconds of head start, and it is what makes a chain
 * read as a chain: every link carries 0 and steps off on the same frame, while
 * unrelated seats are staggered so a floor-wide reshuffle does not look like
 * one object.
 */
export function useWalk(root: RefObject<HTMLElement | null>, last: Map<string, Pos>,
                        /** WHAT A COMMIT WORTH MEASURING LOOKS LIKE, as a
                         *  string. Without it this effect ran on EVERY commit -
                         *  each 3s poll (whose payload is usually identical),
                         *  each 15s banter rotation, every note arriving and
                         *  expiring, every click that moved the selection - and
                         *  paid a rect read plus a forced style recalc per
                         *  character each time. An idle twelve-seat floor was
                         *  measuring itself forever with nothing happening. The
                         *  caller builds this out of everything that can change
                         *  the LAYOUT of the floor, so a commit that cannot move
                         *  anybody does not get measured. */
                        when: string) {
  /* Layout effect, not effect: this has to run after the DOM is updated and
     BEFORE the browser paints, or the character is painted once at its new
     position and the walk starts from a frame that already gave the answer. */
  useLayoutEffect(() => {
    const box = root.current;
    if (!box) return;

    const reduce = typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    /* MEASURED AGAINST THE FLOOR, NOT THE VIEWPORT, AND IN CONTENT COORDINATES.
       `.bg4-floorwrap` scrolls - a deep floor is a taller building - so both the
       wrap's own offset and how far it has been scrolled have to come out,
       or the reader scrolling the pane would fold into the delta and send every
       character on a walk nobody's work triggered. */
    const origin = box.getBoundingClientRect();
    const ox = origin.left - box.scrollLeft, oy = origin.top - box.scrollTop;
    const seen = new Map<string, Pos>();
    /* EVERY MEASUREMENT FIRST, THEN EVERY WRITE, AND ONE REFLOW BETWEEN THEM.
       This loop used to read a rect, read a computed style, write two styles,
       force a synchronous layout, and write two more - per character. The next
       character's rect then hit a dirtied tree and forced layout again, so a
       chain leaving the lounge on one tick cost roughly two full layout passes
       per seat over a container that is a `container-type: inline-size` box with
       a `:has()` hover rule in it, all inside one layout effect before paint. On
       a machine that is also running Godot and Blender that is a visible stall
       on every dispatch. Split in three: measure all, write all the starting
       transforms, force layout ONCE, then arm every transition. */
    const movers: {
      node: HTMLElement; dx: number; dy: number; ms: number; delay: number;
    }[] = [];

    for (const node of Array.from(
      box.querySelectorAll<HTMLElement>("[data-walk]"))) {
      const key = node.dataset.walk;
      if (!key) continue;
      const r = node.getBoundingClientRect();
      /* A HIDDEN CHARACTER HAS NO POSITION. The narrow-rail layout hides a
         whole column, and a zero box would be remembered as the top-left
         corner - so when the rail widens again every one of those seats would
         sprint in from the corner of the building having done nothing. */
      if (!r.width && !r.height) continue;

      /* THE TRANSFORM COMES BACK OFF BEFORE THE POSITION IS RECORDED, and this
         is not a nicety. This effect runs on more than the poll: the handover
         notes arrive as their own state update and are part of `when`, so a
         second run lands a few milliseconds into a walk already playing. Measured
         raw, the rect of a character halfway across the floor would be filed as
         where it LIVES, the next commit would walk it back from a place it was
         only passing through, and a seat that moved once would jitter for as
         long as anything else on the pane re-rendered. Only translations are
         ever written here, so subtracting them gives the layout position
         exactly. */
      let tx = 0, ty = 0;
      const t = getComputedStyle(node).transform;
      if (t && t !== "none") {
        const m = new DOMMatrixReadOnly(t);
        tx = m.m41; ty = m.m42;
      }
      const now = { x: r.left - ox - tx, y: r.top - oy - ty };
      seen.set(key, now);

      const was = last.get(key);
      if (!was || reduce) continue;
      /* THE GATE IS THE LAYOUT MOVE, AND ONLY THAT. Sub-pixel differences are
         reflow noise - a room's height changes when a sibling's label wraps -
         and a character re-armed on every unrelated re-render would restart its
         own walk from wherever it had got to, forever approaching the desk it
         is trying to reach. */
      const mx = was.x - now.x, my = was.y - now.y;
      if (Math.hypot(mx, my) < 1) continue;

      /* THE OFFSET IT IS ALREADY CARRYING IS ADDED, NOT DISCARDED. The floor
         reflows in the middle of a walk as a matter of routine: a note landing
         at the office grows that row and shifts every room above it. Starting
         the new transform from zero there would drop whatever was left of the
         journey and teleport the character to the near-final position - which
         is exactly what a walk across the building looked like before the note
         cards existed, because the notes arrive one render after the poll. */
      const dx = mx + tx, dy = my + ty;
      const dist = Math.hypot(dx, dy);
      if (dist < 1) continue;

      /* A HEAD START IS ONLY FOR SETTING OFF. A character already in transit
         that gets re-aimed must not stop dead in the corridor for a fifth of a
         second and start again; the stagger is there to break up a group
         leaving at once, and it has done its job by then. */
      const delay = (tx || ty) ? 0
        : Math.max(0, Number(node.dataset.walkDelay) || 0);
      const ms = Math.min(MAX_MS, Math.max(MIN_MS, 380 + dist * PER_PX));
      movers.push({ node, dx, dy, ms, delay });
    }

    for (const m of movers) {
      m.node.style.transition = "none";
      m.node.style.transform = `translate(${m.dx}px, ${m.dy}px)`;
    }
    /* READ A LAYOUT PROPERTY OR THERE IS NO ANIMATION. The two style writes
       otherwise land in the same frame, the browser only ever sees the final
       one, and the characters arrive with no walk at all. This forced reflow is
       the whole trick and it is why the line is not a stray access - and ONE of
       them covers the whole floor, because it flushes every write above it. */
    if (movers.length) void box.offsetWidth;
    for (const m of movers) {
      m.node.style.transition =
        `transform ${Math.round(m.ms)}ms cubic-bezier(.33,.02,.28,1) ${m.delay}ms`;
      m.node.style.transform = "";
    }

    /* The map is the caller's, kept across polls in a ref. Replaced wholesale
       rather than merged: a seat that left the floor (its room was disabled)
       must not leave a stale position behind for a seat that returns later. */
    last.clear();
    for (const [k, v] of seen) last.set(k, v);
    /* `root` and `last` are refs whose identity never changes, so `when` is the
       whole dependency. It is REQUIRED for that reason: a caller that forgot it
       would measure the floor once at mount and then never again, which is a
       floor where nobody ever walks. */
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [when]);
}
