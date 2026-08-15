/* THE CANVAS THE FLOOR IS DRAWN ON, and the whole of React's involvement in it.
 *
 * This component owns exactly two things: the <canvas> element, and the fact
 * that a new placement has arrived. It never re-renders per frame — the poll is
 * 3s and the floor animates at 60, so driving frames through React would mean
 * rebuilding a component tree eight times a second to move one image. The
 * renderer runs its own requestAnimationFrame loop and reads the latest
 * DrawState it was handed.
 *
 * WHY A CANVAS AND NOT THE DOM IT REPLACED. The DOM version placed sixty-odd
 * absolutely-positioned elements inside a perspective-transformed div and asked
 * the browser's layout engine to composite them into a room. It never read as
 * one: CSS lays out documents, and the result was a web page wearing a floor
 * plan. Everything that decides WHERE anything is — floorplan.ts, occupancy.ts,
 * route.ts — is unchanged and shared; only the paint moved.
 */
import { useEffect, useRef } from "react";
import { type FloorPlan, type Spot } from "./floorplan";
import { type Occupant } from "./occupancy";
import { type Nav } from "./route";
import { FloorRenderer } from "./floorRender";

export function FloorCanvas({ plan, people, spots, nav, picked, onPick,
                              musicOn = false, onRadio, banter = null }: {
  plan: FloorPlan;
  people: Occupant[];
  spots: Map<string, Spot>;
  nav: Nav;
  picked: string;
  onPick: (who: Occupant) => void;
  /** Is the lounge radio playing. Drawn, not just tracked. */
  musicOn?: boolean;
  /** Clicked the lounge radio. Omitted on a mount that has no soundtrack to
   *  switch - the stream overlay - and then the radio is scenery. */
  onRadio?: () => void;
  /** What the lounge is saying and who is saying it, or null for silence. */
  banter?: { seat: string; line: string } | null;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const engine = useRef<FloorRenderer | null>(null);

  /* The click handler is re-pointed rather than re-bound, so a new selection
     callback never costs the renderer its loop or its character positions. */
  const pickRef = useRef(onPick);
  pickRef.current = onPick;
  const peopleRef = useRef(people);
  peopleRef.current = people;
  const radioRef = useRef(onRadio);
  radioRef.current = onRadio;

  useEffect(() => {
    if (!canvas.current) return;
    const r = new FloorRenderer(canvas.current);
    r.onClick = (seat) => {
      const who = peopleRef.current.find((p) => p.seat === seat);
      if (who) pickRef.current(who);
    };
    /* ALWAYS INSTALLED, AND THE REF DECIDES WHETHER IT DOES ANYTHING. This
       mount effect runs ONCE, and at that moment the track manifest has not
       been fetched yet - so `onRadio` is undefined, and binding the handler
       conditionally pinned it to null for the life of the pane. The radio drew
       correctly, hovered correctly, and answered no click ever. The renderer
       needs to know whether a radio is live for its cursor, so the null case is
       expressed by the ref being empty rather than by the hook being absent. */
    r.onRadio = () => radioRef.current?.();
    engine.current = r;
    r.resize();
    r.start();
    const ro = new ResizeObserver(() => r.resize());
    if (canvas.current.parentElement) ro.observe(canvas.current.parentElement);
    return () => { ro.disconnect(); r.destroy(); engine.current = null; };
  }, []);

  useEffect(() => {
    const r = engine.current;
    if (!r) return;
    r.radioLive = !!onRadio;
    r.update({ plan, people, spots, nav, picked, musicOn, banter });
    r.resize();
  }, [plan, people, spots, nav, picked, musicOn, onRadio, banter]);

  return <canvas className="bg4-canvas" ref={canvas} />;
}
