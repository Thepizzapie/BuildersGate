import { useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Themed } from "../../theme";
import { useViewActive } from "../../hooks";
import { ProviderStudio } from "./ProviderKeys";
import { LocalStudio } from "./LocalGenerators";

/* Studio → Generators: the same fix, framed by what you were trying to make.
 *
 * The Studio deck is still classic (frontend/public/flows.js) and builds its
 * tab strip from window.StudioFlows, so this tab is REGISTERED there rather
 * than hardcoded — the same door providerkeys.js used. flows.js hands us its
 * body element and we grow a React root inside it.
 *
 * NOT THROUGH mountIslands(). That helper ends by re-activating the current
 * deck, and activating Studio rebuilds this very tab — a loop. A root of our
 * own, unmounted when flows.js empties the body for another tab (a
 * MutationObserver on the host, because innerHTML="" fires no event on the
 * child it discards), is the whole contract.
 */

declare global {
  interface Window {
    StudioFlows?: Record<string, { label: string; icon?: string;
                                   build(host: HTMLElement, api?: unknown): void }>;
  }
}

function StudioGenerators() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  return (
    <div ref={host} data-panel="studio-generators">
      <ProviderStudio active={active} />
      <LocalStudio active={active} />
    </div>
  );
}

let root: Root | null = null;
let el: HTMLElement | null = null;
let watcher: MutationObserver | null = null;

function teardown() {
  watcher?.disconnect(); watcher = null;
  root?.unmount(); root = null;
  el = null;
}

function build(host: HTMLElement) {
  if (root && el && el.isConnected && el.parentElement === host) return;
  teardown();
  el = document.createElement("div");
  host.appendChild(el);
  root = createRoot(el);
  root.render(<Themed><StudioGenerators /></Themed>);
  watcher = new MutationObserver(() => { if (el && !el.isConnected) teardown(); });
  watcher.observe(host, { childList: true });
}

window.StudioFlows = window.StudioFlows || {};
window.StudioFlows.providers = { label: "Generators", icon: "lock", build };
