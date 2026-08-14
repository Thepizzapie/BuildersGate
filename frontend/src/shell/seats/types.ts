import type { Seat } from "./api";

/* What every seat body is handed.
 *
 * `active` is the poll gate — it is true only while the seats deck is on
 * screen AND this seat is the selected one, so switching seats stops the old
 * one's traffic rather than accumulating eight pollers over an afternoon.
 * `tab` is the seat's own stage, which each body interprets for itself: the
 * tabs are ordered by the work, not by the panels, which is the one thing the
 * classic seat shell got right (see SeatStage in static/seats/_core.js).
 */
export type SeatBodyProps = {
  seat: Seat;
  active: boolean;
  tab: string;
};

/** A tab in the seat header. `hint` becomes the title attribute — the tabs are
 *  stages of a craft and the labels are short. */
export type SeatTab = { id: string; label: string; icon: string; hint?: string };
