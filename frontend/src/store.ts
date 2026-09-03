import { useSyncExternalStore } from "react";

/* store.ts — /api/state, pushed in by the shell rather than fetched again.
 *
 * pollState() in index.html already reads /api/state every few seconds and
 * hands the result to a dozen renderers. An island that fetched it a second
 * time would double the most expensive poll on the page and would be a few
 * hundred milliseconds out of step with the shell that draws the status bar
 * above it — so the classic poller stays the single reader and calls
 * window.BGState.push(s) where it used to call renderAssetWorkspace().
 *
 * useSyncExternalStore is exactly this shape: one mutable value outside React,
 * many components reading it, an imperative setter. It also gets tearing right,
 * which a useState + custom event pair does not.
 *
 * This is the pattern for every remaining view whose data already arrives on the
 * shell's poll. Views with their OWN endpoint (the Overview's five reads) keep
 * fetching for themselves — pushing everything through here would rebuild the
 * god-object that pollState already is.
 */

export type Artifact = {
  id: number;
  logical_name: string;
  path: string;
  kind: string;
  status: string;
  revision: number;
  producer?: string | null;
  model?: string | null;
  profile?: string | null;
  prompt?: string | null;
  review_note?: string | null;
  refs?: unknown[];
  consistency?: unknown;
  engine_import?: unknown;
  used_in_current_build?: boolean;
  lock?: { seat?: string; work_item_id?: number } | null;
  work_item?: { id: number; status: string; result?: string } | null;
};

export type AssetGroup = {
  logical_name: string;
  approved?: Artifact | null;
  candidates?: Artifact[];
  revisions: Artifact[];
  feedback: { session_id: number; kind: string; text: string; status: string }[];
};

export type TrackedAsset = {
  path: string; kind: string; bytes?: number; lock_seat?: string | null;
};

export type Verify = {
  ok?: boolean;
  counts?: { modified?: number; missing?: number; pending?: number };
  modified?: { path: string }[];
  missing?: string[];
  untracked_hash?: string[];
};

export type Project = { name: string; engine?: string; dimension?: string };

export type AppState = {
  asset_groups: AssetGroup[];
  assets: TrackedAsset[];
  verify: Verify;
  /** Canon entity names — the asset library's buckets are derived from them. */
  canon: string[];
  /** Null until a project is open, which is also what the first-run card asks. */
  project: Project | null;
  root: string;
  sessions: unknown[];
  /** The game's own input map, when the backend reports one (state.controls
   *  or project.controls). The play panel promises no button it lacks. */
  controls: unknown[] | null;
};

const EMPTY: AppState = {
  asset_groups: [], assets: [], verify: {}, canon: [],
  project: null, root: "", sessions: [], controls: null,
};

let current: AppState = EMPTY;
const listeners = new Set<() => void>();

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/** Called by pollState() in index.html with the raw /api/state body. */
export function push(raw: Record<string, unknown>): void {
  const lore = (raw.lore || {}) as { canon?: { name?: string }[] };
  current = {
    asset_groups: (raw.asset_groups as AssetGroup[]) || [],
    assets: (raw.assets as TrackedAsset[]) || [],
    verify: (raw.verify as Verify) || {},
    canon: (lore.canon || []).map((e) => e?.name || "").filter(Boolean),
    project: (raw.project as Project) || null,
    root: String(raw.root || ""),
    sessions: (raw.sessions as unknown[]) || [],
    controls: (raw.controls as unknown[])
      || ((raw.project as { controls?: unknown[] } | null)?.controls) || null,
  };
  listeners.forEach((fn) => fn());
}

export function useAppState(): AppState {
  return useSyncExternalStore(subscribe, () => current, () => EMPTY);
}
