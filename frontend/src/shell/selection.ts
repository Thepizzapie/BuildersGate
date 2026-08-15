import { useSyncExternalStore } from "react";

/* WHAT IS SELECTED, shared by every screen and the inspector.
 *
 * The inspector is a permanent column, and the thing it describes is chosen in
 * a different React root — the floor and the board are their own island each,
 * because they live inside their own `.deck-view` and the shell must not own
 * the decks. React context cannot cross that boundary; a store outside React
 * can, and useSyncExternalStore is the supported way to read one.
 *
 * Deliberately tiny and structural: a key, what kind of thing it is, and the
 * ids needed to go and read it. The inspector does its own fetching — passing
 * a whole agent payload through here would make every poll in the floor a
 * re-render of the inspector.
 */

export type Selection = {
  key: string;
  kind: "agent" | "event" | "item";
  itemId?: number;
  eventId?: number;
  title?: string;
  seat?: string;
  /* WHAT THE FLOOR SAID ABOUT THE SEAT, in occupancy.ts's own word - "running",
     "waiting", "idle". One word from the poll, not a payload, and it is here
     because the inspector cannot re-derive it: with no item id there is nothing
     to fetch, and "this seat has nothing on" is exactly the case where the
     panel should offer to give it something rather than show an empty log. */
  seatState?: string;
} | null;

let current: Selection = null;
const listeners = new Set<() => void>();

export function setSelection(next: Selection): void {
  current = next;
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

export function useSelection(): [Selection, (next: Selection) => void] {
  const sel = useSyncExternalStore(subscribe, () => current, () => null);
  return [sel, setSelection];
}
