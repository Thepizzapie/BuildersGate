import "@mantine/core/styles.layer.css";
import { createRoot, type Root } from "react-dom/client";
import { flushSync } from "react-dom";
import type { ComponentType } from "react";
import { Themed } from "./theme";
import { Shell } from "./shell/Shell";
/* Side effect only: registers the Studio "Generators" tab on window.StudioFlows,
   which the classic Studio deck (flows.js) builds its tab strip from. */
import "./shell/settings/StudioGenerators";
/* Side effect only: askText / askConfirm / askPick on window, which the
   classic decks reach the way they reach toast(). React code imports them
   from bridge.ts. */
import "./ask";
/* Side effect only: window.Handoff — the "put it in the game" panel the
   sprite editor, audio lab and 3D viewer open from their toolbars. */
import "./shell/handoff/Handoff";
import { TitleBar } from "./shell/TitleBar";
import { push } from "./store";

/* main.tsx — the island registry.
 *
 * There is no React router and no React shell. index.html is still the
 * application: it owns the rail, the workspace switch, auth, toasts and every
 * view that has not been converted. React takes one container at a time.
 *
 * A container opts in by carrying `data-react="<key>"`. Mounting is by
 * attribute rather than by id so a view can be converted, reverted, or split in
 * half without this file learning anything about the page's structure — and so
 * an island can appear inside a panel that classic code injects later, which is
 * what `mountIslands()` on window is for.
 *
 * This script is a MODULE, so it is deferred: the DOM is parsed and every
 * classic script has run before a single component mounts. That is the reason
 * the bridge can read `window.readJSON` at call time without a guard dance. */

const ISLANDS: Record<string, ComponentType> = {
  /* The design bible, its reference anchors and the lore graph — the
     producer's write surface. It was world.js + bible_refs.js. */
  /* Play & record, the recorded sessions, the review overlay and the notepad
     drawer. It was ptnotes.js + ptreview.js + the recorder script in
     index.html; the overlay and the drawer portal onto <body>. */
  /* The 4a shell — the rail, the screen column, the header and the inspector.
     It renders AROUND the existing stage rather than owning it. */
  shell: Shell,
  /* The screen the redesign added. A deck like any other, so setWorkspace()
     switches it and nothing in the shell special-cases it. The floor's four
     questions (everything / live / needs you / history) are ONE deck reading
     the current screen from shell/screen.ts — four decks would have been four
     polls of the same log. (The board — lanes by seat — went with the 4a
     shell; shell/nav.ts says why, and index.html no longer carries its deck.) */
  /* One workspace per craft. Art's is a queue of candidates you approve;
     gameplay's is a table of tunables with what playtests measured beside
     them — a renamed kanban lane was never any of those. */
  /* The app's own window caption, drawn only inside the frameless desktop
     window. In a browser tab it mounts and immediately renders null. */
  titlebar: TitleBar,
};

type IslandLoader = () => Promise<ComponentType>;
const LOADERS: Record<string, IslandLoader> = {
  overview: () => import("./views/Overview").then((m) => m.default),
  firstrun: () => import("./views/FirstRun").then((m) => m.default),
  assets: () => import("./views/assets/Assets").then((m) => m.default),
  world: () => import("./views/world/World").then((m) => m.default),
  playtests: () => import("./views/playtests/Playtests").then((m) => m.default),
  floor: () => import("./shell/Floor").then((m) => m.Floor),
  agents: () => import("./shell/agents/Agents").then((m) => m.Agents),
  brainstorm: () => import("./shell/brainstorm/Room").then((m) => m.Room),
  settings: () => import("./shell/settings/Settings").then((m) => m.Settings),
  seats: () => import("./shell/seats/Seats").then((m) => m.Seats),
};

const mounted = new WeakMap<Element, Root>();

export function mountIslands(scope: ParentNode = document): void {
  scope.querySelectorAll<HTMLElement>("[data-react]").forEach((el) => {
    if (mounted.has(el)) return;
    const key = el.dataset.react || "";
    const View = ISLANDS[key];
    const loader = LOADERS[key];
    if (!View && !loader) {
      console.warn(`[bgate] no React island registered for "${key}"`);
      return;
    }
    const root = createRoot(el);
    mounted.set(el, root);
    /* SYNCHRONOUS ON PURPOSE, and this is not a performance choice.
       root.render() schedules a commit; it does not perform one. The classic
       shell around this island looks its panels up by id — activateWorkspace()
       hands the assets deck to assetlib.js, which does
       `getElementById("asset-lib-root")` and RETURNS SILENTLY if the node is
       not there yet, with no retry. Deferred module, concurrent render: the
       node was still a promise when the shell went looking, so the library
       rendered as nothing at all and only a rail click brought it back.
       flushSync means the island's DOM exists before this function returns. */
    if (View) {
      flushSync(() => root.render(<Themed><View /></Themed>));
      return;
    }
    /* A lazy root gets one first commit. Replacing a temporary child from many
       resolved import callbacks in the same task made React 19 reconcile
       against an already-removed node and left several screens empty. */
    void loader().then((Loaded) => {
      flushSync(() => root.render(<Themed><Loaded /></Themed>));
      const current = document.querySelector(".deck-view.active")?.id.replace(/^view-/, "");
      if (current) window.activateWorkspace?.(current);
    }).catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      flushSync(() => root.render(<div className="empty">This screen could not load: {message}</div>));
    });
  });
  /* The shell activated the restored deck before any of this existed. Ask it
     again now that the containers are real — the same reason index.html re-runs
     activateWorkspace on DOMContentLoaded once its module scripts have parsed.
     Idempotent by contract: every owner's activate() is safe to call twice. */
  const current = document.querySelector(".deck-view.active")?.id.replace(/^view-/, "");
  if (current) window.activateWorkspace?.(current);
}

declare global {
  interface Window {
    mountIslands?: typeof mountIslands;
    /** index.html — hands a deck to whichever classic module owns it. */
    activateWorkspace?(name: string): boolean | void;
    /* pollState() in index.html reads /api/state for the whole shell and pushes
       it here, rather than every island fetching the same body again. */
    BGState?: { push: typeof push };
  }
}
window.mountIslands = mountIslands;
window.BGState = { push };

mountIslands();
