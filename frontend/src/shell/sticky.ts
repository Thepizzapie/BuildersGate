import { useCallback, useEffect, useRef } from "react";

/* STAY AT THE BOTTOM OF A CONVERSATION, WITHOUT TRAPPING THE READER THERE.
 *
 * Every transcript in this app was a plain scroller: a reply landed below the
 * fold and you scrolled to it by hand, every single turn. A room where four
 * seats answer one message is four manual scrolls, which is why it read as
 * "not reactive" — the content was arriving, it just never came into view.
 *
 * The naive fix (scroll to bottom whenever the list changes) is worse than the
 * bug: it yanks the page out from under somebody who has scrolled UP to read
 * what a seat said earlier, mid-sentence, every three seconds while a poll
 * runs. So this follows only while you are ALREADY at the bottom — the moment
 * you scroll away it lets go, and the moment you come back it re-attaches.
 *
 * `deps` is whatever means "there is new content": a message count, a turn
 * count. Not the array itself — a poll rebuilds those objects every tick and
 * would re-scroll on data that did not change.
 */
const NEAR = 80;   // px from the bottom that still counts as "at the bottom"

export function useStickyBottom<T>(dep: T) {
  const ref = useRef<HTMLDivElement>(null);
  /* Whether to follow. Starts true so a freshly opened conversation shows its
     newest turn rather than its oldest. */
  const stuck = useRef(true);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    stuck.current = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR;
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [onScroll]);

  useEffect(() => {
    const el = ref.current;
    if (!el || !stuck.current) return;
    /* After paint, not during it: the row that just arrived has no height yet
       when the effect runs, so scrolling now lands one message short — the
       exact off-by-one that makes an auto-scroller look broken. */
    const id = requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(id);
  }, [dep]);

  return ref;
}
