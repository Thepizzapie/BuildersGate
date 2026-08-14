import type { ReactNode } from "react";
import { Ti } from "../Ti";

/* The furniture every seat workspace is built out of.
 *
 * The design's argument is that a seat's workspace should be shaped to its
 * CRAFT, so there is deliberately no "seat panel" component here — art's frame
 * strip and qa's verdict cards have nothing to share. What they DO share is the
 * grammar: a small caps label with a sentence of doctrine beside it, a table
 * whose header is the doctrine made into columns, and an empty state that names
 * what would fill it.
 */

/** A section header: the label, and the one line that says why the section
 *  exists. The hint is doctrine, not decoration — "the measured number sits
 *  next to the knob" is the reason that table has four columns. */
export function Head({ label, hint, right }: {
  label: string; hint?: string; right?: ReactNode;
}) {
  return (
    <div className="bgs-head">
      <span className="lb">{label}</span>
      {hint && <span className="hint">{hint}</span>}
      <span className="sp" />
      {right}
    </div>
  );
}

/* AN EMPTY PANEL AND A BROKEN ONE LOOK IDENTICAL unless the empty one says what
 * would fill it. Carried over from SeatStage.nothing() in the classic core,
 * where it was written after "no models found" was drawn over a 584-sheet
 * project. `how` names the step, the tool or the file that produces the thing.
 */
export function Nothing({ what, how }: { what: string; how?: string }) {
  return (
    <div className="bgs-nothing">
      <b>{what}</b>
      {how && <span>{how}</span>}
    </div>
  );
}

export type Tone = "good" | "warn" | "bad" | "off" | "seat";

/** A small state word. `tone` is the only colour decision in the whole seat
 *  layer, so a reader learns it once. */
export function Tag({ tone = "off", children, title }: {
  tone?: Tone; children: ReactNode; title?: string;
}) {
  return <span className={`bgs-tag t-${tone}`} title={title}>{children}</span>;
}

/** A metered bar. Drawn ONLY where a real fraction exists — see useFloor's
 *  note on the same subject. Callers pass 0..1 or nothing. */
export function Bar({ v, tone = "good" }: { v: number; tone?: Tone }) {
  const pct = Math.max(0, Math.min(1, v)) * 100;
  return (
    <span className="bgs-bar">
      <span className={`t-${tone}`} style={{ width: `${pct}%` }} />
    </span>
  );
}

/** The banner a seat opens with when its craft has a standing warning: art's
 *  pin, cinematic's price, tech's findings. */
export function Banner({ icon, tone = "warn", children, right }: {
  icon: string; tone?: Tone; children: ReactNode; right?: ReactNode;
}) {
  return (
    <div className={`bgs-banner t-${tone}`}>
      <Ti name={icon} size={18} />
      <div className="b">{children}</div>
      <span className="sp" />
      {right}
    </div>
  );
}

/** A read failure, said plainly. The bridge never throws — it hands back the
 *  fallback with `__error` on it — so a panel that ignores that field renders
 *  an empty list and calls a dead endpoint "nothing yet". */
export function ReadError({ error, what }: { error?: string; what: string }) {
  if (!error) return null;
  return <div className="bgs-readerr">could not read {what} — {error}</div>;
}
