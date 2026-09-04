/* bridge.ts — the one door between React and the dashboard that already exists.
 *
 * The migration is INCREMENTAL BY ISLAND: index.html stays the shell, the
 * classic scripts keep owning navigation, auth, toasts and every view that has
 * not been converted yet, and React mounts inside individual `.deck-view`
 * containers. That only works if there is exactly one place where React reaches
 * into the old world — otherwise `window.something` calls scatter across
 * components and the last view to be converted can never be un-wired.
 *
 * Everything the old page exposes is a FUNCTION DECLARATION in a classic
 * script, which means it really is on `window`. Top-level `const` (the page's
 * `color`, `esc`) is NOT — those live in the global lexical environment and are
 * reachable by identifier but not by property, so they are reimplemented here
 * rather than reached for. `esc` has no React equivalent worth having: JSX
 * escapes its own children.
 */

import { push as pushQuestions } from "./notify-store";
import type { askText as askTextFn } from "./ask";

type Json = Record<string, unknown>;

/** The page's mutate options — NOT a RequestInit. `quiet` suppresses the toast
 *  (an overlay shows the reason inline instead), `button` is disabled for the
 *  duration, `ok` is the toast on success. */
export type MutateOptions = {
  method?: string;
  body?: unknown;
  quiet?: boolean;
  ok?: string | null;
  button?: string | HTMLElement | null;
};

/** It never throws or rejects: a failure comes back as ok:false with a message
 *  already turned into a sentence by apiError(). */
export type MutateResult<T = unknown> = {
  ok: boolean;
  data: T | null;
  /** The parsed response body, kept even on a refusal.
   *
   *  `data` is null when ok is false — callers read it as "the thing I asked
   *  for" and an error envelope there would make a failed write look like a
   *  successful one. But some endpoints put their real answer IN the failure:
   *  /api/godot/check answers ok:false carrying the compiler errors, the exit
   *  code and the output; /api/play/rebuild carries the export stderr. A panel
   *  that needs the detail reads `body`; everything else is unaffected. */
  body?: unknown;
  error: string | null;
  code: string | null;
  status: number;
};

declare global {
  interface Window {
    readJSON?<T>(path: string, fallback: T): Promise<T & { __error?: string }>;
    mutate?(path: string, options?: MutateOptions): Promise<MutateResult>;
    toast?(message: string, kind?: string): void;
    watchAgent?(id: number): void;
    setWorkspace?(name: string, trigger?: Element | null): void;
    BGATE_SETTINGS?: Record<string, unknown>;
    /** ask.tsx installs this for the classic decks; React code should import
     *  askText from this module instead of reaching for the window copy. */
    askText?: typeof askTextFn;
    /** The shell's pollers. An island that has just changed server state asks
     *  for the next poll now instead of waiting out the interval. */
    pollState?(): Promise<void> | void;
    pollQueue?(): Promise<void> | void;
    /** The full-screen image lightbox. */
    show?(rel: string): void;
    /** Prefill the director's box. Registered by the agents console WHILE IT IS
     *  MOUNTED, which is why `compose()` below cannot simply call it. */
    BGCompose?(task: { seat?: string; title?: string; brief?: string }): void;
    /** Classic view modules, reached only through the wrappers below. */
    SpriteEdit?: { open(rel: string): void; pick(): void };
    ModelEdit?: { open(rel: string): void; pick(): void };
    AudioLab?: { open(rel: string): void; pick(): void };
    AssetLib?: { activate(force?: boolean): void; refresh(): void };
  }
}

/** A preview URL for a project-relative path. */
export const previewURL = (rel: string) =>
  `/api/preview?rel=${encodeURIComponent(rel)}`;

/** The in-page replacement for prompt(). Resolves the string, or NULL on
 *  cancel — "" means confirmed-empty and null means backed out, and the two
 *  must not be collapsed. Lives in ask.tsx now; re-exported so the islands
 *  keep one import for everything the shell provides. */
export { askText, askConfirm, askPick } from "./ask";

/** Send a task to the director's box, from any screen.
 *
 * WHY THIS IS NOT `window.BGCompose(...)` AT THE CALL SITE. The console
 * registers BGCompose in an effect, so it exists only while the agents screen is
 * mounted — which it is not, when you are standing on a seat. Switching decks
 * first and calling immediately lands before the effect has run, and the call
 * silently does nothing: exactly the failure the console's own comment describes
 * for the old atlas buttons.
 *
 * So the deck switch and the prefill are separated in time, and the prefill
 * retries for a beat rather than assuming. It gives up after ~1s and says so —
 * a button that quietly fails is worse than one that admits it.
 *
 * setTimeout, NOT requestAnimationFrame. rAF does not fire while the page is
 * not compositing — a background tab, a hidden window, the desktop shell before
 * its webview is shown — so scheduling this on frames makes the button work on
 * screen and do nothing, with no error, everywhere else. The retry is about
 * waiting for a React effect, which is not a paint.
 */
export function compose(task: { seat?: string; title?: string; brief?: string }): void {
  window.setWorkspace?.("agents");
  const started = Date.now();
  const tick = () => {
    if (window.BGCompose) { window.BGCompose(task); return; }
    if (Date.now() - started > 1000) {
      toast("the director's console did not open — say it there yourself", "warn");
      return;
    }
    setTimeout(tick, 16);
  };
  setTimeout(tick, 0);
}

export function pollState(): void { window.pollState?.(); }
export function pollQueue(): void { window.pollQueue?.(); }
export function lightbox(rel: string): void { window.show?.(rel); }

/** The status bar's vault chip, while an audit is running.
 *
 *  It belongs to the shell, not to this view, but the button that starts the
 *  work is here — and the whole point of naming that button after the badge was
 *  that a press and the badge are the same subject. The next pollState() puts
 *  the real number back. */
export function vaultChip(text: string): void {
  const b = document.querySelector("#sb-vault b");
  if (b) b.textContent = text;
}

/** GET a JSON endpoint through the page's own reader — which carries the auth
 *  token, unwraps the envelope, and reports a failure as `__error` on the
 *  fallback rather than throwing. Falls back to a plain fetch so a component
 *  can be exercised outside the shell. */
export async function readJSON<T extends Json>(
  path: string,
  fallback: T,
): Promise<T & { __error?: string }> {
  if (window.readJSON) return window.readJSON(path, fallback);
  try {
    const r = await fetch(path, { headers: { accept: "application/json" } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return { ...fallback, ...(await r.json()) };
  } catch (e) {
    return { ...fallback, __error: String((e as Error).message || e) };
  }
}

/** POST/PATCH through the page's mutator, which raises the toast on failure. */
export async function mutate<T = unknown>(
  path: string,
  options?: MutateOptions,
): Promise<MutateResult<T>> {
  if (!window.mutate) throw new Error("mutate() is not on the page");
  return window.mutate(path, options) as Promise<MutateResult<T>>;
}

export function toast(message: string, kind?: string): void {
  window.toast?.(message, kind);
}

/** Open the inspector on a running agent — the old `watchAgent(id)`. */
export function watchAgent(id: number): void {
  window.watchAgent?.(id);
}

/** A seat's colour token, resolved to its computed value. A canvas cannot read
 *  var(), and neither can an inline style that has to be compared. */
export function seatColor(seat?: string | null): string {
  if (!seat) return "var(--text-2)";
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(`--c-${seat}`)
    .trim();
  return v || "var(--text-2)";
}

/** A number from the settings registry, delivered in the page bootstrap.
 *  Clamped as well as defaulted: a stored 0 would busy-loop the poller. */
export function setting(key: string, fallback: number, lo: number, hi: number): number {
  const raw = Number((window.BGATE_SETTINGS || {})[key]);
  return Number.isFinite(raw) && raw > 0 ? Math.max(lo, Math.min(raw, hi)) : fallback;
}

/** Is this optional feature module switched off for the project?
 *  Read from the same page-bootstrap the poll intervals ride in
 *  (modules.disabled is a client-delivered setting), so the answer exists
 *  before anything renders. Unknown or unreadable answers OFF=false: a
 *  broken read must cost nothing, never a pane. */
export function moduleOff(name: string): boolean {
  const raw = (window.BGATE_SETTINGS || {})["modules.disabled"];
  return Array.isArray(raw) && raw.map(String).includes(name);
}

/** Feed the notification bell the console state we just read. The bell
 *  (shell/Bell.tsx) belongs to no view; the open questions on the console
 *  payload are what it cannot learn from the event log alone. */
export function notifyUpdate(state: unknown): void {
  try { pushQuestions(state); } catch { /* never the console's problem */ }
}
