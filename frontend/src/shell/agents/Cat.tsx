import { useEffect, useRef, useState } from "react";
import type { ConsoleState } from "./api";

/* THE CAT.
 *
 * It went with agents_console.js and nothing replaced it. The sprite
 * (static/img/mascot_talk.png), the flipbook keyframes and every mood rule are
 * still in app.css under `.ck-mascot` / `.cat-sprite` — only the thing that set
 * the three attributes they key on was gone, so the CSS has been sitting there
 * animating nothing.
 *
 * IT IS A REACTION, NOT A NARRATOR, AND IT HAS NO WORDS. That rule is inherited
 * verbatim from the classic console, which learned it twice: first as a quote
 * machine rotating canned lines on a timer — the most animated thing on the
 * page was the one element guaranteed to be saying nothing true — and then fed
 * the director's real voice, which put the same sentence twice on screen six
 * lines apart, with the copy in the louder box. The transcript is where words
 * go. The cat carries only what text cannot: it mouths WHILE something is being
 * said, its idle tracks the floor's mood, and it plays a one-off beat when
 * something actually lands, fails, or needs a human.
 *
 * THE MOOD AND BEAT RULES ARE THE ORIGINALS. Ported rather than reinvented, in
 * the same order, because the order is the argument: something needing a human
 * outranks even a failure — a failure is visible in the transcript, an approval
 * is a thing the room is WAITING on.
 */

type Mood = "idle" | "working" | "broken" | "gate" | "auto" | "done";

function moodOf(s: ConsoleState): Mood {
  const floor = s.floor || {};
  const gates = (s.gates || []).filter((g) => g.blocking);
  if ((s.items || []).some((i) => i.status === "failed")) return "broken";
  if (floor.running) return "working";
  if (gates.length) return "gate";
  if (floor.queued) return s.autopilot?.on ? "auto" : "gate";
  if (floor.done) return "done";
  return "idle";
}

export function Cat({ state, talking }: {
  state: ConsoleState;
  /** True while a reply is actually being produced. The mouth moves only for a
   *  voice that is mid-sentence — a mouth that never stops means nothing. */
  talking?: boolean;
}) {
  const [beat, setBeat] = useState("");
  /* The previous poll's statuses. A beat is a TRANSITION, so the first poll
     produces none: without this guard, opening the console fires "landed" for
     every item that was already done before you arrived. */
  const seen = useRef<Record<number, string> | null>(null);
  const seenGates = useRef(0);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const now: Record<number, string> = {};
    for (const i of state.items || []) now[i.id] = i.status;
    const before = seen.current;
    seen.current = now;

    const gates = (state.gates || []).filter((g) => g.blocking).length;
    const wasGates = seenGates.current;
    seenGates.current = gates;
    if (!before) return;

    let landed = 0, failed = 0, released = 0, started = 0;
    for (const [id, status] of Object.entries(now)) {
      const was = before[Number(id)];
      if (was === undefined || was === status) continue;
      if (status === "failed") failed += 1;
      else if (status === "done") { landed += 1; if (was === "review") released += 1; }
      else if (status === "dispatched") started += 1;
    }

    const next = gates > wasGates ? "wants"
      : failed ? "oops"
      : released ? "released"
      : landed ? "landed"
      : started ? "dispatch" : "";
    if (!next) return;
    setBeat(next);
    window.clearTimeout(timer.current);
    // Same 1.4s the classic one used: long enough to read as a reaction, short
    // enough that it is over before the next poll can stack another on it.
    timer.current = window.setTimeout(() => setBeat(""), 1400);
  }, [state]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const mood = moodOf(state);
  return (
    <div className="ck-mascot" id="ck-mascot"
         data-mood={mood}
         {...(talking ? { "data-talking": "" } : {})}
         {...(beat ? { "data-react": beat } : {})}
         title={`the floor is ${mood}`}>
      <div className="cat-sprite" aria-hidden="true" />
    </div>
  );
}
