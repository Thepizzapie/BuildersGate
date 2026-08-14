import { useSyncExternalStore } from "react";

/* Which screen the shell is on.
 *
 * The four Command screens — everything, live, needs you, history — are ONE
 * deck asked four questions. The deck is a single React island, so the question
 * cannot arrive as a prop from the shell: they are separate roots, in separate
 * containers, mounted by the registry rather than by each other.
 *
 * Same shape as selection.ts, for the same reason: a store outside React is the
 * only thing that crosses a root boundary, and useSyncExternalStore is the
 * supported way to read one.
 */

let current = "floor";
const listeners = new Set<() => void>();

export function setScreen(id: string): void {
  if (id === current) return;
  current = id;
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

export function useScreen(): string {
  return useSyncExternalStore(subscribe, () => current, () => current);
}
