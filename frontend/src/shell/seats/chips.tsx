import { createContext, useContext, useEffect, useRef } from "react";

/* THE HEADER CHIPS BELONG TO THE SEAT, NOT TO THE SHELL.
 *
 * The reference gives every seat three figures on its topbar — "18 findings",
 * "4 unbound", "-14 LUFS target", "1 UNKNOWN" — and they are not the same three
 * numbers with different labels. They come from whatever that craft reads:
 * tech's are the project check, audio's are the hook table, qa's is a verdict
 * that has not been written. The shell cannot compute any of them, and the
 * version that tried drew the only four it could (running / open / locked /
 * promoted) on all eight seats, which is why every workspace's header looked
 * identical whatever craft you were in.
 *
 * So the BODY publishes them and the topbar renders them. The body is the thing
 * that already holds the read; passing the same data up through props would
 * mean the shell polls every seat's endpoints to draw a header for one.
 *
 * A chip whose value is not known yet is simply not published. NOTHING HERE
 * INVENTS A FIGURE — an empty topbar means that seat's reads have not landed,
 * and that is a truthful state; a "0 findings" chip on a project nobody has
 * checked is not.
 */

export type Chip = {
  /** tabler icon suffix, e.g. "alert-hexagon" */
  icon: string;
  /** the whole label, including the number: "18 findings" */
  label: string;
  /** a CSS colour or token. Omit for the neutral dim. */
  color?: string;
  /** why this number is what it is — the topbar has no room to explain itself */
  title?: string;
};

type Sink = (seat: string, chips: Chip[]) => void;

export const ChipSink = createContext<Sink>(() => {});

/** Publish this seat's header chips. Call it unconditionally at the top of a
 *  seat body with whatever is known; pass `[]` while a read is in flight.
 *
 *  The chips are re-published on every change and cleared when the body
 *  unmounts, so switching seats can never leave the previous craft's numbers on
 *  the new craft's header — which is the failure mode a global would have. */
export function useSeatChips(seat: string, chips: Chip[]): void {
  const sink = useContext(ChipSink);
  /* The array is a fresh literal on every render, so it cannot be the effect's
     dependency. Its CONTENT can: the chips are a handful of short strings. */
  const key = JSON.stringify(chips);
  const last = useRef("");
  useEffect(() => {
    if (last.current === key) return;
    last.current = key;
    sink(seat, chips);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, seat]);
  useEffect(() => () => sink(seat, []), [seat, sink]);
}
