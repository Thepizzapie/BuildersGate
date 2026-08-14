import { useEffect, useRef } from "react";

/* The page's icon set, usable from React.
 *
 * icons.js owns every glyph in the dashboard and hands them out two ways:
 * `BGIcon(name)` returns markup, and `BGIcon.upgrade(root)` swaps
 * `[data-icon]` placeholders in place. React must not fight the second one —
 * an upgraded span is a DOM mutation inside a node React believes it owns, and
 * the next render throws it away.
 *
 * So this renders an EMPTY span React never writes children into
 * (dangerouslySetInnerHTML with a stable string is the contract that says "the
 * DOM under here is mine"), and asks BGIcon for the markup once per name. If
 * icons.js has not loaded, the span stays empty, which is what the classic
 * page does too. */

declare global {
  interface Window {
    BGIcon?: ((name: string, opts?: { size?: number }) => string) & {
      has?(name: string): boolean;
      upgrade?(root: Element): void;
      /** The mark itself — a theme may restyle the UI, not the brand. */
      logo?(opts?: { size?: number; flat?: boolean }): string;
    };
  }
}

export function Icon({ name, size = 14 }: { name: string; size?: number }) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const bg = window.BGIcon;
    if (bg && (!bg.has || bg.has(name))) el.innerHTML = bg(name, { size });
  }, [name, size]);
  return <span className="ic" ref={ref} aria-hidden="true" />;
}
