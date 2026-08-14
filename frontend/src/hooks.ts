import { useEffect, useRef, useState } from "react";

/* Is the deck-view containing this element the one on screen?
 *
 * The shell shows one `.deck-view.active` at a time and every classic view
 * checks that class before doing any work — `refreshOverview` opened with
 * exactly that guard. A React island that polls regardless would put every
 * converted view's traffic on the wire at once, which is a regression the
 * moment there are two of them. MutationObserver rather than a poll, because
 * the class flips from a click, not from time. */
export function useViewActive(ref: React.RefObject<HTMLElement | null>): boolean {
  const [active, setActive] = useState(true);
  useEffect(() => {
    const view = ref.current?.closest(".deck-view") as HTMLElement | null;
    if (!view) return; // mounted outside the deck — always live
    const read = () => setActive(view.classList.contains("active"));
    read();
    const obs = new MutationObserver(read);
    obs.observe(view, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, [ref]);
  return active;
}

/* Run `fn` now and every `ms`, but only while `enabled`.
 *
 * The interval is rebuilt when the cadence changes, and the callback is held in
 * a ref so a component that closes over fresh state does not restart the timer
 * on every render — the old page's `setInterval(refreshOverview, 4000)` never
 * had to think about that, and it is the first thing an interval in React gets
 * wrong. */
/** `key` RE-ARMS THE POLL, AND WITHOUT IT A PARAMETERISED READ IS STALE.
 *
 *  The callback lives in a ref so a re-render does not restart the timer — that
 *  is right, and it is also why the effect used to key on `[ms, enabled]` alone.
 *  The consequence: when the URL changed, the interval quietly began fetching
 *  the new one, but the IMMEDIATE fetch at the top of the effect never re-fired.
 *  So picking a different lore entity, sequence or storyboard showed the
 *  previous one's data until the next tick — up to 60 seconds on the slower
 *  panels, with nothing on screen admitting it was stale.
 *
 *  Callers whose read is parameterised pass that parameter as `key` (useJSON
 *  passes its path). A caller with a fixed URL passes nothing and behaves
 *  exactly as before.
 */
export function usePoll(fn: () => void, ms: number, enabled = true,
                        key?: unknown): void {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!enabled) return;
    saved.current();
    const id = window.setInterval(() => saved.current(), ms);
    return () => window.clearInterval(id);
  }, [ms, enabled, key]);
}
