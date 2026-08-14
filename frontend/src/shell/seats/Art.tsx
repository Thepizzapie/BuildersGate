import { useState } from "react";
import { Ti } from "../Ti";
import { previewURL, lightbox } from "../../bridge";
import { Head, Nothing, Tag, ReadError } from "./prims";
import { useJSON, ago } from "./api";
import { useSeatChips } from "./chips";
import type { SeatBodyProps } from "./types";
import "./artaudio.css";
import "./art.css";

/* ART — the sheet, the pin, and the measurement that says whether they agree.
 *
 * The seat's brief is an order of operations: PIN THE REFERENCE, CONDITION
 * EVERY FRAME ON IT, MEASURE THE RESULT. So the workspace is those three, in
 * that order, and not a stack of unrelated boxes.
 *
 * A SHEET IS A ROW OF FRAMES AND THE MEASUREMENT HAS TO BE TOO. This panel used
 * to read `artifact.consistency`, a field the artifact table does not have, so
 * the measurement section was empty on every real project while the generator's
 * OWN audit sat unread in `metadata.alpha`. Worse, a whole-sheet average is the
 * wrong resolution for the question: one bad cell in six moves a mean by a
 * sixth and vanishes. /api/art/sheet (new) reports what generation measured AND
 * slices the row to audit each cell separately, which is why the strip can put
 * a flag under the frame that earned it.
 *
 * NOTHING HERE INVENTS A NUMBER — AND THAT INCLUDES THE THRESHOLD. A measurement
 * absent from the metadata draws no row; a sheet whose width is not a whole
 * number of cells gets no strip; a cell the audit could not read says "not
 * measured", which is a different sentence from "clean".
 *
 * The version of this panel before this one broke that rule in the one place it
 * matters most: it coloured every measure with a single invented cutoff
 * (`fraction < 0.15`, and `> 0.5` for the one where more is better). chroma.py
 * does not have one cutoff, it has six, and they are not 0.15. A frame border
 * that is 10% opaque is a HARD FAIL there (BORDER_OPAQUE_MAX = 0.06) and drew
 * GREEN here; a 0.30 soft edge is clean there (SOFT_ALPHA_MAX = 0.35) and drew
 * a warning here, which sends the seat to regenerate a sheet that passed. Both
 * directions are the same bug — a verdict the UI made up. The gates below are
 * the real constants, they are PRINTED next to the value so the reader can see
 * what judged it, a measure whose gate this file does not know draws grey
 * rather than green, and any measure the sheet's own audit flagged is a warning
 * regardless of arithmetic, because the audit outranks a recomputation.
 */

type Frame = {
  index: number; flags: string[]; review: string[]; clean: boolean;
  dirty_alpha?: number | null; white_fringe?: number | null;
  soft_alpha?: number | null; hollow?: number | null;
};
type Measure = {
  label: string; value: number; display?: string; fraction: number;
  hi_is_good: boolean; note?: string;
};
type Sheet = {
  id: number; logical_name: string; revision: number; path: string;
  status: string; kind: string; created_at?: string; review_note?: string;
  size?: string; frames?: number | null; producer?: string;
  work_item_id?: number | null; promoted?: boolean | null;
};
type Pin = { name?: string; revision?: number; path?: string; note?: string };
type RefRow = Pin & { kind?: string; updated_at?: string };
type SheetRead = {
  sheet?: Sheet | null; frames?: Frame[]; measures?: Measure[];
  pin?: Pin | null; flags?: string[]; families?: string[];
};
/* /api/locks returns more per row than the shared useLocks type keeps: the
   lease, the heartbeat, who owns it and who is queued behind it. Read here in
   full — a lock row without its lease cannot tell a live run from a dead one
   that is still holding the file. */
type Held = {
  path?: string; kind?: string; seat?: string; owner?: string; actor?: string;
  work_item_id?: number | null; since?: string | null;
  heartbeat_at?: string | null; lease_expires_at?: string | null;
  waiters?: { seat?: string; actor?: string }[];
};
type LockRead = { held?: Held[]; waiters?: Held[]; path_leases?: Held[] };

/* The gates are bgate_core/chroma.py's module constants, by the label
   artsheet.measures() gives each one. They are duplicated here rather than
   read off the wire because /api/art/sheet does not send them — see the
   cross-file note in the seat's report. A label missing from this table is
   drawn ungated (grey), never green: a new measurement must not inherit a
   pass from a threshold nobody chose for it. */
const GATE: Record<string, { max?: number; min?: number; text: string }> = {
  "dirty alpha":      { max: 0.15,  text: "≤ 0.15" },   // DIRTY_ALPHA_MAX
  "white halo":       { max: 0.20,  text: "≤ 0.20" },   // WHITE_FRINGE_MAX
  "soft alpha":       { max: 0.35,  text: "≤ 0.35" },   // SOFT_ALPHA_MAX
  "background bleed": { max: 0.06,  text: "≤ 0.06" },   // BORDER_OPAQUE_MAX
  "hollow interior":  { max: 0.12,  text: "≤ 0.12" },   // HOLLOW_FAIL
  "residual chroma":  { max: 0.04,  text: "≤ 0.04" },   // RESIDUAL_CHROMA_MAX
  "chroma headroom":  { min: 120,   text: "≥ 120" },    // MIN_SAFE_DISTANCE
};

/* chroma.audit's flag sentences open with a fixed phrase; this maps that phrase
   onto the measure row it is about, so a flagged measurement is marked from the
   AUDIT rather than from this file's arithmetic. */
const FLAG_MEASURE: Record<string, string> = {
  "background bleed": "background bleed",
  "white halo": "white halo",
  "feathered alpha": "soft alpha",
  "dirty alpha": "dirty alpha",
  "unkeyed backdrop": "residual chroma",
  "hollow interior": "hollow interior",
};

/* The per-cell keys measure_frames returns, and the same gates. `hollow` gets
   HOLLOW_REVIEW (0.05) as its soft mark because chroma.audit itself splits
   hollow into advisory-then-fail at those two numbers. */
const CELL_COLS: { key: keyof Frame; label: string; max: number }[] = [
  { key: "dirty_alpha",  label: "dirty",  max: 0.15 },
  { key: "white_fringe", label: "halo",   max: 0.20 },
  { key: "soft_alpha",   label: "soft",   max: 0.35 },
  { key: "hollow",       label: "hollow", max: 0.12 },
];

/* A pin path is absolute on disk; /api/preview only serves project-relative
 * paths. Anchoring on .bgate is the same rule the classic seat core used. */
const rel = (p: string) => String(p || "").replace(/^.*[\\/](?=\.bgate[\\/])/, "").replace(/\\/g, "/");

/** The short version of a flag sentence — the strip's cell is 100px wide and
 *  the audit's flags are a paragraph each. The full text is the title. */
const short = (flag: string) => String(flag).split(":")[0];

/** Seconds from now until a SQLite UTC stamp; negative once it has passed. */
function until(when?: string | null): number | null {
  if (!when) return null;
  const s = String(when);
  const ms = Date.parse(s.replace(" ", "T")
    + (/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? "" : "Z"));
  return Number.isFinite(ms) ? (ms - Date.now()) / 1000 : null;
}
const mmss = (s: number) => s >= 60 ? `${Math.round(s / 60)}m` : `${Math.round(s)}s`;

export function Art({ seat, active, tab }: SeatBodyProps) {
  /* Which family the panel is pinned to. Empty follows whatever generated last,
     which is the right default for a running board and the wrong one the moment
     the owner wants to look at a specific character. /api/art/sheet has taken
     ?logical_name= since it was written and nothing ever passed it. */
  const [family, setFamily] = useState("");
  const sheets = useJSON<SheetRead>(
    "/api/art/sheet" + (family ? `?logical_name=${encodeURIComponent(family)}` : ""),
    {}, 10000, active && tab === "sheets");
  const refs = useJSON<{ refs?: RefRow[] }>("/api/refs", { refs: [] }, 20000, active);
  /* Read /api/locks directly rather than through useLocks, which drops
     `__error` and the lease columns. "nothing is locked" drawn over a dead
     endpoint is the single most dangerous empty state in this seat: it is the
     answer an agent uses to decide it may overwrite a .png. */
  const lockRead = useJSON<LockRead>("/api/locks",
    { held: [], waiters: [], path_leases: [] }, 6000, active);

  const sheet = sheets.sheet || null;
  const frames = sheets.frames || [];
  const measures = sheets.measures || [];
  const families = sheets.families || [];
  const sheetFlags = sheets.flags || [];
  const held: Held[] = [...(lockRead.held || []), ...(lockRead.path_leases || [])];
  const lockWaiters = lockRead.waiters || [];

  /* THE SHEET'S OWN PIN AND THE PROJECT'S FIRST REF ARE NOT THE SAME CLAIM.
     `sheets.pin` is read out of the artifact's metadata.ref_pins — the image
     the frames were actually conditioned on. The fallback is whatever ref
     sorts first in the project (on a real project that is alphabetical: a
     different character entirely). Showing the fallback is useful; letting it
     inherit the sentence "every frame conditions on this" is a lie, so the two
     are kept apart everywhere below. */
  const ownPin = sheets.pin || null;
  const fallbackPin = (refs.refs || [])[0] || null;
  const pin: Pin | null = ownPin || fallbackPin;
  /* A pin is versioned. A sheet conditioned on r1 while the project has since
     re-pinned that name to r3 is no longer conditioned on THE reference, and
     nothing anywhere said so. */
  const current = ownPin?.name
    ? (refs.refs || []).find((r) => r.name === ownPin.name)
    : undefined;
  const stale = !!(ownPin && current && typeof current.revision === "number"
                   && typeof ownPin.revision === "number"
                   && current.revision > ownPin.revision);

  const frameFlags = frames.reduce((n, f) => n + (f.flags || []).length, 0)
                   + sheetFlags.length;
  const frameReviews = frames.reduce((n, f) => n + (f.review || []).length, 0);
  /* Cells the audit could not read. `frames` is empty for the whole sheet when
     Pillow is missing or the PNG is corrupt, and every cell then says "not
     measured" — which must not be summarised as a flag count of zero. */
  const cells = sheet?.frames || 0;
  const measured = frames.length;
  const unmeasured = cells ? Math.max(0, cells - measured) : 0;

  useSeatChips(seat.role, [
    ...(ownPin ? [{
      icon: "pin",
      label: stale ? `pin r${ownPin.revision} superseded` : "reference pinned",
      color: stale ? "var(--warn)" : "var(--c-art)",
      title: stale
        ? `this sheet was conditioned on ${ownPin.name}@r${ownPin.revision}; `
          + `the project pin is now r${current?.revision}`
        : `${ownPin.name || ownPin.path} r${ownPin.revision} — every frame conditions on this`,
    }] : fallbackPin ? [{
      /* NOT "reference pinned". The sheet named no pin; this is the project's
         first ref standing in, and it says so. */
      icon: "pin", label: "sheet names no pin", color: "var(--warn)",
      title: `the newest revision records no ref_pins; showing the project's `
           + `first reference (${fallbackPin.name || fallbackPin.path}) as context only`,
    }] : []),
    /* Only publishable once a measurement landed: "0 alpha flags" on a sheet
       nobody audited is the lie the whole seat is built to prevent. And a
       partial audit is published as partial, not rounded up to clean. */
    ...(measured || sheetFlags.length ? [{
      icon: frameFlags ? "alert-triangle" : unmeasured ? "help-circle"
          : frameReviews ? "eye-exclamation" : "checks",
      label: frameFlags
        ? `${frameFlags} alpha flag${frameFlags === 1 ? "" : "s"}`
        : unmeasured ? `${measured}/${cells} cells measured`
        : frameReviews ? `${frameReviews} for review` : "0 alpha flags",
      color: frameFlags ? "var(--warn)"
           : unmeasured || frameReviews ? "var(--text-3)" : "var(--good)",
      title: "chroma.audit over each cell of the sheet in progress",
    }] : cells ? [{
      icon: "help-circle", label: "sheet not measured", color: "var(--text-3)",
      title: `${cells} cells and no audit came back — Pillow missing, or the `
           + "file could not be read",
    }] : []),
    ...(held.length ? [{
      icon: "lock", label: `${held.length} locked`, color: "var(--text-3)",
      title: held.map((l) => l.path).join(" · "),
    }] : []),
  ]);

  /* One lock row, with the thing that makes it actionable: how long the lease
     has left. `since` alone cannot distinguish a run that is working from one
     that died holding a binary nothing else may now touch. */
  const lockRow = (l: Held, i: number, big: boolean) => {
    const left = until(l.lease_expires_at);
    const beat = l.heartbeat_at ? ago(l.heartbeat_at) : "";
    const dead = left !== null && left <= 0;
    return (
      <div key={`${l.path}${i}`}>
        <div className="bgs-lockrow">
          <Ti name="lock" size={big ? 14 : 13}
              color={`var(--c-${l.seat || "art"}, var(--warn))`} />
          <span className="p" title={l.path}>{l.path}</span>
          <span className="by" style={{ color: `var(--c-${l.seat}, var(--text-3))` }}>
            {l.seat || "?"}{l.work_item_id ? ` · item ${l.work_item_id}` : ""}
          </span>
        </div>
        {big && (
          <div className="bga-lease">
            <span>{l.since ? `held ${ago(l.since)}` : "held since unrecorded"}</span>
            {/* NOT MEASURED IS NOT CLEAN, here too: a row with no lease stamp
                has no expiry, which is not the same as an expiry far away. */}
            <span className={dead ? "warn" : ""}>
              {left === null ? "no lease recorded"
                : dead ? "lease expired — the holder stopped renewing"
                : `lease ${mmss(left)} left`}
            </span>
            <span>{beat ? `heartbeat ${beat} ago` : "no heartbeat"}</span>
            {l.actor && <span>{l.actor}</span>}
            {!!(l.waiters || []).length && (
              <span className="warn">
                {(l.waiters || []).length} waiting: {(l.waiters || []).map((w) => w.seat).join(", ")}
              </span>
            )}
            {/* The dashboard has no release endpoint — POST /api/locks/release
                does not exist — so this prints the call that DOES release it
                instead of offering a button that cannot. */}
            <span>release: <code>asset_release path="{l.path}"</code></span>
          </div>
        )}
      </div>
    );
  };

  if (tab === "locks") {
    return (
      <div className="bgs-pad">
        <Head label="Binary locks" hint="these do not merge" />
        <ReadError error={lockRead.__error} what="the lock table" />
        {!held.length && !lockRead.__error && (
          <Nothing what="nothing is locked"
                   how="asset_lock takes a lease before an agent edits a binary; an unlocked .png is one two runs can both overwrite" />
        )}
        {held.map((l, i) => lockRow(l, i, true))}
        {!!lockWaiters.length && (
          <>
            <Head label="Waiting" hint="a seat blocked on somebody else's lease" />
            {lockWaiters.map((w, i) => (
              <div className="bgs-lockrow" key={i}>
                <Ti name="clock" size={14} color="var(--warn)" />
                <span className="p">{w.path}</span>
                <span className="by">{w.seat}</span>
              </div>
            ))}
          </>
        )}
      </div>
    );
  }

  /* "approved" is a green word and this project hands it out automatically.
     The revision's own review_note says so; printing the green tag over it is
     the participation trophy this seat exists to refuse. */
  const note = String(sheet?.review_note || "");
  const autoApproved = /auto-approved|no human gate/i.test(note);

  return (
    <div className="bgs-split" style={{ ["--rail" as string]: "300px" }}>
      <div className="bgs-main">
        <Head label="Sheet in progress"
              hint={sheet
                ? `${sheet.logical_name} · r${sheet.revision}`
                  + (sheet.kind ? ` · ${sheet.kind}` : "")
                  + (cells ? ` · ${cells} frames, stitched` : "")
                  + (sheet.created_at ? ` · ${ago(sheet.created_at)} ago` : "")
                : "the newest live art revision"}
              right={sheet?.producer && (
                <span className="bga-ran">
                  <Ti name="list-check" size={13} color="var(--good)" />
                  {" "}{sheet.producer} ran{sheet.size ? ` — ${sheet.size}` : ""}
                  {sheet.work_item_id ? ` · item ${sheet.work_item_id}` : ""}
                </span>
              )} />
        <ReadError error={sheets.__error} what="the sheet in progress" />

        {/* The read already returns every art family in the project; without a
            picker the panel could only ever show the last thing generated, so
            on a project with forty sheets thirty-nine were unreachable. */}
        {families.length > 1 && (
          <div className="bga-fam">
            <Ti name="stack-2" size={13} />
            <select value={family} onChange={(e) => setFamily(e.target.value)}>
              <option value="">newest live revision ({families.length} families)</option>
              {families.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            {family && (
              <span>pinned to this family — clear to follow the board</span>
            )}
          </div>
        )}

        {!sheet && !sheets.__error && (
          <Nothing what={family ? `no live revision in ${family}` : "no art generated yet"}
                   how={family
                     ? "every revision in this family is superseded, rejected or discarded"
                     : "image_generate / sprite_plan file a revision; the newest live one is the sheet in progress"} />
        )}

        {sheet && <div className="bga-path">{sheet.path}</div>}

        {sheet && !cells && (
          /* Not a row. Slicing an image whose width is not a whole number of
             cells would measure the seam between two frames and call it a hole,
             so it is shown whole and said so. */
          <button className="bga-cell" style={{ width: 220, marginBottom: 8 }}
                  onClick={() => lightbox(sheet.path)} title={sheet.path}>
            <span className="img bga-checks"
                  style={{ display: "block",
                           backgroundImage: `url(${previewURL(sheet.path)})`,
                           backgroundSize: "contain",
                           backgroundPosition: "center" }} />
            <span className="cap">{sheet.size || "one image"}</span>
            <span className="flag unknown">not a frame row — no per-cell split</span>
          </button>
        )}

        {sheet && !!cells && (
          <div className="bga-strip" style={{ ["--cells" as string]: String(cells) }}>
            {Array.from({ length: cells }, (_, i) => {
              const f = frames.find((x) => x.index === i);
              const flags = f?.flags || [];
              const review = f?.review || [];
              const flag = flags[0];
              return (
                <button
                  className={`bga-cell${flag ? " flagged" : ""}`
                             + (!flag && review.length ? " reviewed" : "")}
                  key={i}
                  onClick={() => lightbox(sheet.path)}
                  title={[...flags, ...review].join("\n\n") || sheet.path}>
                  {/* One PNG, N slices: the sheet is already in the browser's
                      cache and cutting it server-side would be N requests for
                      pixels it holds. */}
                  <span className="img bga-checks"
                        style={{
                          backgroundImage: `url(${previewURL(sheet.path)}), none`,
                          backgroundSize: `${cells * 100}% 100%`,
                          backgroundPosition:
                            cells > 1 ? `${(i / (cells - 1)) * 100}% 0` : "0 0",
                        }} />
                  <span className="cap">frame {i + 1}</span>
                  {flag
                    ? <span className="flag">
                        <Ti name="alert-triangle" size={11} />{short(flag)}
                        {/* Showing flags[0] and nothing else made a cell with
                            four failures look like a cell with one. */}
                        {flags.length > 1 && <b className="more">+{flags.length - 1}</b>}
                      </span>
                    : review.length
                    /* chroma.audit's `review` list is advisory, not clean. A
                       green check over "possible hole: 8% of the figure is
                       enclosed transparency" is the audit's hedge deleted. */
                    ? <span className="flag review">
                        <Ti name="eye-exclamation" size={11} />{short(review[0])}
                      </span>
                    : f
                    ? <span className="flag clean">
                        <Ti name="check" size={11} />clean
                      </span>
                    : <span className="flag unknown">not measured</span>}
                </button>
              );
            })}
          </div>
        )}

        {/* THE NUMBERS THE SLICER ALREADY WROTE. measure_frames returns four
            measurements per cell and the strip could only show a flag, so the
            drift that has not crossed a gate yet — frame 3 at 0.026 soft while
            frames 1 and 2 sit at 0.023 — was fetched and discarded. That drift
            is the whole argument for auditing per cell. */}
        {!!measured && (
          <table className="bga-cells">
            <caption>
              per-cell chroma.audit · a blank cell is a measurement the audit did
              not return, not a zero
              {unmeasured ? ` · ${unmeasured} of ${cells} cells came back unmeasured` : ""}
            </caption>
            <thead>
              <tr>
                <th>frame</th>
                {CELL_COLS.map((c) => (
                  <th key={String(c.key)} title={`gate ≤ ${c.max}`}>
                    {c.label} ≤{c.max}
                  </th>
                ))}
                <th>verdict</th>
              </tr>
            </thead>
            <tbody>
              {frames.map((f) => (
                <tr key={f.index}>
                  <td>{f.index + 1}</td>
                  {CELL_COLS.map((c) => {
                    const v = f[c.key];
                    if (typeof v !== "number") {
                      return <td key={String(c.key)} className="none">not measured</td>;
                    }
                    return (
                      <td key={String(c.key)} className={v > c.max ? "over" : ""}>
                        {v.toFixed(3)}
                      </td>
                    );
                  })}
                  <td className={f.flags.length ? "over" : f.review.length ? "over" : ""}>
                    {f.flags.length ? `${f.flags.length} flag${f.flags.length === 1 ? "" : "s"}`
                      : f.review.length ? "review" : "clean"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <Head label="Consistency, measured"
              hint="conditioned on the pin, not asked politely" />
        {!measures.length && (
          <Nothing what="this sheet carries no measurement"
                   how="image_generate writes an alpha and chroma audit into the revision's metadata; a revision filed without one has nothing to plot" />
        )}
        {measures.map((m) => {
          /* The gate is chroma.py's, not this file's guess, and it is printed.
             A measure the sheet's own audit flagged is a warning whatever the
             arithmetic says — the audit that wrote the flag saw the pixels. */
          const flagged = sheetFlags.some(
            (f) => FLAG_MEASURE[short(String(f))] === m.label);
          const g = GATE[m.label];
          const over = g
            ? (g.max !== undefined ? m.value > g.max
               : g.min !== undefined ? m.value < g.min : false)
            : false;
          const bad = flagged || over;
          const ungated = !g && !flagged;
          return (
            <div className="bga-measure" key={m.label}
                 title={[m.note, flagged ? "flagged by the sheet's own audit" : "",
                         g ? `gate ${g.text}` : "no gate is defined for this measurement — not judged"]
                        .filter(Boolean).join(" · ")}>
              <span className="lb">{m.label}</span>
              <span className="track">
                <span className={bad ? "warn" : ungated ? "ungated" : ""}
                      style={{ width: `${Math.round(m.fraction * 100)}%` }} />
              </span>
              <span className={`v${bad ? " warn" : ungated ? " ungated" : ""}`}>
                {m.display ?? m.value.toFixed(3)}
              </span>
              <span className="gate">{g ? g.text : "no gate"}</span>
            </div>
          );
        })}
        {!!sheetFlags.length && (
          <div className="bgs-reasons">
            {sheetFlags.map((f, i) => <div key={i}>{f}</div>)}
          </div>
        )}
        {sheet?.review_note && (
          <div className="bgs-reasons"><div>{sheet.review_note}</div></div>
        )}
        {sheet && (
          <div style={{ marginTop: 8 }}>
            {sheet.status === "approved" && autoApproved
              ? <Tag tone="warn" title={note}>approved — no human gate</Tag>
              : <Tag tone={sheet.status === "approved" ? "good" : "warn"}>{sheet.status}</Tag>}
            {" "}
            {/* promoted:false means the integration record exists and says no;
                promoted:null means no integration record was ever written. The
                second one is not a negative answer, it is no answer. */}
            {sheet.promoted
              ? <Tag tone="good">promoted into the game</Tag>
              : sheet.promoted === false
              ? <Tag tone="off" title="registered, but the live file is elsewhere">not promoted</Tag>
              : <Tag tone="off" title="this revision carries no integration metadata at all">promotion not recorded</Tag>}
          </div>
        )}
      </div>

      <div className="bgs-rail">
        <div className="bgs-railhead">
          <Ti name="pin" size={15} color={stale ? "var(--warn)" : "var(--c-art)"} />
          <span className="lb">Pinned reference</span>
          {ownPin?.revision !== undefined && (
            <span className="hint">r{ownPin.revision}</span>
          )}
        </div>
        <ReadError error={refs.__error} what="the project's references" />
        {!pin && !refs.__error && (
          <div className="bgs-railpad">
            <Nothing what="nothing is pinned"
                     how="ref_pin makes one image THE identity target; without it every generation is a fresh guess" />
          </div>
        )}
        {pin && (
          <div className="bga-pin">
            <button className="thumb" onClick={() => lightbox(rel(pin.path || ""))}
                    title={pin.path}>
              <img src={previewURL(rel(pin.path || ""))} alt={pin.name || "pin"} />
            </button>
            <div className="nm">
              {pin.name || (rel(pin.path || "").split("/").pop())}
            </div>
            {/* The sheet's own pin can say what it binds ("every frame
                conditions on this"); a global ref's `note` is the curator's
                prose about the character and is a paragraph, not a caption. */}
            <div className={`sub${ownPin ? "" : " warn"}`}>
              {ownPin ? pin.note
                      : "this sheet names NO pin — the image above is the project's "
                        + "first reference, shown as context, and the frames were "
                        + "not conditioned on it"}
            </div>
            <div className="prov">
              {ownPin && (
                <Tag tone={stale ? "warn" : "good"}
                     title={stale
                       ? `the project re-pinned ${ownPin.name} to r${current?.revision} `
                         + "after this sheet was generated"
                       : "the revision the frames were conditioned on"}>
                  {stale
                    ? `conditioned on r${ownPin.revision} · project is on r${current?.revision}`
                    : `conditioned on r${ownPin.revision}`}
                </Tag>
              )}
              {ownPin && !current && (
                <Tag tone="warn" title="the sheet names a pin that /api/refs does not list — it was unpinned, or renamed">
                  pin no longer in the project
                </Tag>
              )}
              {!ownPin && fallbackPin?.kind && (
                <Tag tone="off" title={fallbackPin.note}>{fallbackPin.kind}</Tag>
              )}
            </div>
          </div>
        )}

        <div className="bgs-railhead top">
          <Ti name="lock" size={15} color="var(--c-art)" />
          <span className="lb">Binary locks</span>
          <span className="hint">these do not merge</span>
        </div>
        <ReadError error={lockRead.__error} what="the lock table" />
        {!held.length && !lockRead.__error && (
          <div className="bgs-railpad">
            <Nothing what="nothing locked right now" how={`${seat.title} takes a lease before it edits a binary`} />
          </div>
        )}
        {held.slice(0, 10).map((l, i) => lockRow(l, i, false))}
      </div>
    </div>
  );
}
