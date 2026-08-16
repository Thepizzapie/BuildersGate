/* WHAT THIS AGENT ACTUALLY MADE.
 *
 * The classic panel had this and the new one did not: a run that generated art
 * showed forty rows of tool calls and not one picture, so the only way to see
 * what came out was to go and find it in the library. The steps say what it
 * DID; this says what EXISTS because of it, which is the question somebody
 * clicking an art agent is usually asking.
 *
 * A STRIP, NOT THE OLD CARDS. The legacy rail put every candidate next to its
 * reference with a badge and an off-model steer button - four controls per
 * render, in a panel already dense with steps. Here it is thumbnails and a
 * status dot; the judgement calls live in the art screens that exist for them,
 * and clicking a thumbnail opens the full image.
 *
 * IT DRAWS NOTHING WHEN THERE IS NOTHING. Most runs produce no artifacts, and a
 * permanent empty "renders" heading on every tech and narrative agent is a
 * heading nobody reads twice.
 */
import { useEffect, useState } from "react";
import { lightbox, readJSON } from "../bridge";
import { Ti } from "./Ti";

type Artifact = {
  id: number;
  work_item_id?: number;
  logical_name?: string;
  revision?: number;
  path: string;
  status?: string;
  kind?: string;
};

/** Newest first, capped. A run that produced sixty frames is a sprite sheet,
 *  not sixty things to look at. */
const SHOWN = 8;

export function AgentMade({ itemId }: { itemId: number }) {
  const [made, setMade] = useState<Artifact[]>([]);

  useEffect(() => {
    let live = true;
    setMade([]);
    readJSON<{ artifacts?: Artifact[] }>("/api/artifacts", {}).then((d) => {
      if (!live) return;
      const mine = (d.artifacts || [])
        .filter((a) => a.work_item_id === itemId && a.path)
        .sort((a, b) => (b.id || 0) - (a.id || 0));
      setMade(mine.slice(0, SHOWN));
    });
    return () => { live = false; };
  }, [itemId]);

  if (!made.length) return null;

  return (
    <div className="bg4-made">
      <div className="bg4-insp-eyebrow">What it made</div>
      <div className="bg4-made-strip">
        {made.map((a) => (
          <button key={a.id} className="bg4-made-thumb"
                  onClick={() => lightbox(a.path)}
                  title={`${a.logical_name || a.path}${a.revision ? ` r${a.revision}` : ""}`
                         + `${a.status ? ` · ${a.status}` : ""}`}>
            <img src={`/api/preview?rel=${encodeURIComponent(a.path)}`} alt="" />
            {/* The review state as a dot rather than a word: approved and
                rejected are the two that change what you do next, and anything
                else is still a candidate. */}
            <span className={`dot ${a.status === "approved" ? "ok"
                                  : a.status === "rejected" ? "no" : ""}`}>
              {a.status === "approved" ? <Ti name="check" size={9} />
               : a.status === "rejected" ? <Ti name="x" size={9} /> : null}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
