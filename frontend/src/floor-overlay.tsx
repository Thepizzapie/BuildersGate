/* THE FLOOR AS A STREAM OVERLAY.
 *
 * The same building the console draws, mounted standalone in a transparent
 * window so OBS can layer it over gameplay. It polls the same
 * /api/console/state at the same 3s tick. The only differences are cosmetic:
 * transparent background, no inspector, no handover notes, no banter strip.
 *
 * IT IS A SECOND ENTRY POINT, NOT A SECOND RENDERER, and that is more true now
 * than it was: this file used to carry its own copies of Person, Furniture and
 * Plate, which meant every fix to the console's floor had to be made twice and
 * routinely was not. The building is one canvas painted by floorRender.ts, so
 * the overlay is the data hooks plus a <FloorCanvas />.
 */
import { createRoot } from "react-dom/client";
import { useEffect, useMemo, useState } from "react";
import { type ConsoleState, EMPTY_CONSOLE } from "./shell/agents/api";
import { floorSignature, placeFloor } from "./shell/agents/occupancy";
import {
  planFloor, spotFor, type FloorPlan, type Seat, type Spot,
} from "./shell/agents/floorplan";
import { buildNav } from "./shell/agents/route";
import { useHandoffs } from "./shell/agents/handoff";
import { FloorCanvas } from "./shell/agents/FloorCanvas";
import "./shell/agents/floor.css";

const POLL_MS = 3000;

async function fetchJSON<T>(url: string, fallback: T): Promise<T> {
  try {
    const r = await fetch(url);
    if (!r.ok) return fallback;
    return await r.json() as T;
  } catch { return fallback; }
}

function FloorOverlay() {
  const [state, setState] = useState<ConsoleState>(EMPTY_CONSOLE);
  const [seats, setSeats] = useState<Seat[]>([]);

  useEffect(() => {
    fetchJSON<{ seats: { role?: string; title?: string }[] }>(
      "/api/state", { seats: [] },
    ).then((d) => {
      setSeats((d.seats || []).filter((r): r is Seat => !!r.role));
    });
  }, []);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      const d = await fetchJSON<ConsoleState>("/api/console/state", EMPTY_CONSOLE);
      if (live) setState(d);
    };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => { live = false; window.clearInterval(id); };
  }, []);

  const sig = useMemo(() => floorSignature(state), [state]);

  const plan: FloorPlan = useMemo(() => {
    const table: Seat[] = [...seats];
    const known = new Set(table.map((s) => s.role));
    for (const item of state.items) {
      if (item.seat && !known.has(item.seat)) {
        known.add(item.seat);
        table.push({ role: item.seat });
      }
    }
    return planFloor(table);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seats, sig]);

  const handoffs = useHandoffs(state);
  const people = useMemo(() => {
    const roster = [...plan.bySeat.keys()];
    return placeFloor(roster, state, handoffs.bySeat);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan, sig, handoffs]);

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

  const nav = useMemo(() => buildNav(plan), [plan]);

  if (plan.bySeat.size === 0) return null;

  /* NOTHING ON THE OVERLAY IS A CONTROL. It is a picture on a stream; there is
     no inspector behind it to select into. */
  return (
    <div className="bg4-floorwrap" style={{ background: "transparent", padding: 0 }}>
      <div className="bg4-stage" style={{ background: "transparent" }}>
        <FloorCanvas plan={plan} people={people} spots={spots} nav={nav}
                     picked="" onPick={() => {}} />
      </div>
    </div>
  );
}

const root = document.getElementById("floor-overlay");
if (root) createRoot(root).render(<FloorOverlay />);
