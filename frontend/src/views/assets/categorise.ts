import type { AssetGroup } from "../../store";

/* Which bucket a logical name belongs in.
 *
 * DERIVED, NEVER NAMED. This once hardcoded two character names from one game
 * and floated them to the top of every project's library. A bucket now comes
 * from the project's own canon entities (/api/state ships lore.canon) or,
 * failing that, from the logical name's own prefix — so "tommy_idle" and
 * "tommy_punch" still group, in a project this code has never heard of.
 */

export const GENERIC_CATS = ["arenas & world", "hud", "misc", "trials"];

const slug = (s: string) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "");

export const IMG_RE = /\.(png|jpe?g|webp|svg)$/i;

export function assetCategory(name: string, canon: string[]): string {
  const n = (name || "").toLowerCase();
  if (n.startsWith("_trial") || n.startsWith("trial")) return "trials";
  const flat = slug(n);
  for (const entity of canon) {
    const s = slug(entity);
    if (s.length >= 3 && flat.includes(s)) return String(entity).toLowerCase();
  }
  if (/icon|portrait|\bhud\b/.test(n)) return "hud";
  if (/arena|^bg_|background|stage|parallax|tileset/.test(n)) return "arenas & world";
  const prefix = n.split(/[_\-.]/)[0];
  if (prefix && prefix.length >= 3 && prefix !== n) return prefix;
  return "misc";
}

/** Project-specific buckets lead — those are what you came looking for; the
 *  generic ones follow in a stable order. */
export function catOrder(cats: string[]): string[] {
  const generic = cats.filter((c) => GENERIC_CATS.includes(c))
    .sort((a, b) => GENERIC_CATS.indexOf(a) - GENERIC_CATS.indexOf(b));
  return cats.filter((c) => !GENERIC_CATS.includes(c)).sort().concat(generic);
}

export type GroupStatus = "approved" | "review" | "rejected";

export function groupStatus(g: AssetGroup): GroupStatus {
  if (g.approved) return "approved";
  if (g.candidates && g.candidates.length) return "review";
  return "rejected";
}

export function groupThumb(g: AssetGroup): string | null {
  const pick = g.approved || g.candidates?.[0] || g.revisions?.[g.revisions.length - 1];
  return pick && IMG_RE.test(pick.path || "") ? pick.path : null;
}
