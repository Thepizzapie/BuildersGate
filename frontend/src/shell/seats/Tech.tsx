import { useEffect, useRef, useState } from "react";
import { Ti } from "../Ti";
import { mutate } from "../../bridge";
import { Head, Nothing, Tag, Banner, ReadError } from "./prims";
import { useJSON, ago } from "./api";
import { useSeatChips } from "./chips";
import type { SeatBodyProps } from "./types";
import "./tech.css";

/* TECH — does it still compile, is the build the thing you are playing, and
 * does anything that rewrites project data ask first.
 *
 * THE ENGINE CHECK IS A BUTTON, NOT A POLL. `godot_check_project` is a headless
 * import of the whole project: on any real asset count it is the ninety-second
 * case the job model exists for. Polling it would run the slowest thing in the
 * app every few seconds for a seat nobody is looking at, so it runs when asked
 * and the panel remembers the last answer for as long as the screen is open.
 *
 * EVERYTHING ELSE ON THIS SEAT IS A STATIC READ. The two rules the seat is
 * actually held to between checks — "one editable thing = one named node" and
 * "a tool that rewrites project data ships --check and defaults to dry" — used
 * to have no backend at all, and both panels said so instead of saying
 * anything. /api/tech/plumbing now walks the .tscn files and the argparse of
 * every script under scripts/ and tools/, which is cheap enough to poll and
 * derives every number it prints from a file on disk.
 */

type Check = { ok?: boolean; errors?: string[]; exit_code?: number; seconds?: number; output?: string; error?: string };

type Rule = { rule: string; detail: string; count: number; tone: string; examples: string[]; more: number };
type Gen = { path: string; check: string; check_ok: boolean; dry: string; dry_ok: boolean; note: string; writes?: string[] };
type Plumbing = {
  scenes?: { scenes: number; nodes: number; rules: Rule[]; unreadable: string[] };
  generators?: { scanned: number; dirs: string[]; rows: Gen[] };
  git?: { available: boolean; dirty: boolean; changed: number; reason: string };
  scanned_at?: string;
};
type Play = {
  built?: boolean; stale?: boolean; reason?: string; newest_source?: string;
  build_mtime?: number; source_mtime?: number;
};
type Rebuild = { ok: boolean; bytes?: number; wasm?: number; error?: string };

const RULE_ICON: Record<string, string> = { good: "check", warn: "eye", bad: "alert-hexagon" };
const TONE = { good: "good", warn: "warn", bad: "bad" } as const;
type Tone = "good" | "warn" | "bad";
const tone = (t: string): Tone => (TONE as Record<string, Tone>)[t] || "warn";

/* An epoch-seconds mtime, said the way `ago` says a timestamp — the build and
   its newest source are two clocks and "3h" beside "12m" is the whole story of
   a stale export. */
const since = (mtime?: number): string =>
  typeof mtime === "number" && mtime > 0
    ? ago(new Date(mtime * 1000).toISOString())
    : "";

const mb = (n?: number): string =>
  typeof n === "number" && n > 0 ? `${(n / 1048576).toFixed(1)} MB` : "";

/* THE CHECK ANSWERS WITH ok:false WHEN IT FINDS SOMETHING, and the page's
 * mutate() collapses any `ok:false` body into a one-line error with `data`
 * NULLED. Routed through it, a failing import — the entire reason the Findings
 * panel exists — arrived here as the string "request failed · 200" with the
 * errors, the exit code and the output already thrown away. So this posts
 * itself and reads the body whatever the verdict is; window.fetch carries the
 * dashboard token for every same-origin call, so nothing else is needed.
 */
async function postCheck(): Promise<Check> {
  let body: unknown;
  try {
    const r = await fetch("/api/godot/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    body = await r.json();
    if (!body || typeof body !== "object") {
      return { ok: false, error: `the check did not answer JSON · HTTP ${r.status}` };
    }
  } catch (e) {
    return { ok: false, error: `the check did not run — ${String((e as Error).message || e)}` };
  }
  const got = body as Record<string, unknown>;
  // The transport's own failure envelope is {ok:false, error:{code,message}};
  // the adapter's is {ok:false, error:"sentence"}. Neither is a finding.
  const err = got.error;
  if (err && typeof err === "object") {
    const m = (err as Record<string, unknown>).message;
    return { ok: false, error: String(m || "the check was refused") };
  }
  if (typeof err === "string" && err) return { ok: false, error: err };
  return got as Check;
}

export function Tech({ seat, active, tab }: SeatBodyProps) {
  const godot = useJSON<{ available?: boolean; path?: string; version?: string; project?: string }>(
    "/api/godot/status", {}, 20000, active);
  const play = useJSON<Play>("/api/play/status", {}, 8000, active);
  /* The scan is cached server-side for fifteen seconds, so this asks slowly:
     the rules change when somebody saves a scene, not between frames. */
  const plumb = useJSON<Plumbing>("/api/tech/plumbing", {}, 30000, active);
  const [check, setCheck] = useState<Check | null>(null);
  const [ranAt, setRanAt] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [rebuilt, setRebuilt] = useState<Rebuild | null>(null);
  const [rebuilding, setRebuilding] = useState(false);
  const findingsRef = useRef<HTMLDivElement | null>(null);

  const errors = check?.errors || [];
  const git = plumb.git;
  const scenes = plumb.scenes;
  /* A check that came back not-ok having parsed no error line is NOT a clean
     project: the import failed and the parser did not recognise the shape of
     what it printed. Drawing "0 findings" over it is the false green this seat
     exists to end. */
  const mute = !!check && !check.error && check.ok === false && !errors.length;

  /* A HEADLESS IMPORT IS A MINUTE OF NOTHING unless the button counts. A
     spinner that never moves is indistinguishable from a dead request at
     exactly the moment somebody is deciding whether to ship. */
  useEffect(() => {
    if (!running) return;
    setElapsed(0);
    const t = window.setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => window.clearInterval(t);
  }, [running]);

  /* THREE FIGURES, AND NONE OF THEM IS DRAWN BEFORE ITS READ LANDS. The
     findings chip needs a check that has actually run — "0 findings" on a
     project nobody has imported is the exact lie this seat exists to prevent —
     and the tree chip is absent, not green, when git cannot read the repo. */
  useSeatChips(seat.role, [
    ...(check && !check.error ? [{
      icon: "alert-hexagon",
      label: mute ? "import failed" : `${errors.length} finding${errors.length === 1 ? "" : "s"}`,
      color: errors.length || mute ? "var(--bad)" : "var(--good)",
      title: "godot_check_project, this session",
    }] : []),
    ...(git?.available ? [{
      icon: "git-branch",
      label: git.dirty ? "dirty tree" : "clean tree",
      color: git.dirty ? "var(--warn)" : "var(--good)",
      title: git.dirty
        ? `${git.changed} uncommitted path${git.changed === 1 ? "" : "s"}`
        : "nothing uncommitted",
    }] : []),
    ...(ranAt ? [{
      icon: "clock",
      label: `last check ${ago(ranAt)}`,
      title: "how long ago godot_check_project last ran here",
    }] : []),
  ]);

  async function runCheck() {
    setRunning(true);
    const got = await postCheck();
    setRunning(false);
    setRanAt(new Date().toISOString());
    setCheck(got);
  }

  async function rebuild() {
    setRebuilding(true);
    /* `quiet` because the outcome — including the export_presets.cfg sentence,
       which is the answer nine times out of ten — is drawn in place below. */
    const r = await mutate<{ bytes?: number; wasm?: number }>(
      "/api/play/rebuild", { method: "POST", body: {}, quiet: true });
    setRebuilding(false);
    setRebuilt(r.ok
      ? { ok: true, ...(r.data || {}) }
      : { ok: false, error: r.error || "the export did not run" });
  }

  const scanned = plumb.scanned_at ? `scanned ${ago(plumb.scanned_at)} ago` : "";

  /* BUILD is its own tab in the reference, and it asks a different question
   * from "does the project still compile": what is EXPORTED, and is it older
   * than the source it came from. /api/play/status answers both, and the
   * staleness reason names the file that moved.
   *
   * AND IT IS THE ONE TAB WHERE THE ANSWER HAS AN ACTION. A panel that reports
   * "stale" and offers nothing sends the reader to a terminal to run the export
   * this endpoint already exposes. */
  if (tab === "build") {
    return (
      <div className="bgs-pad">
        <Head label="Export" hint="what /play serves, and whether it is behind the source"
              right={<button className="bgs-btn" onClick={rebuild} disabled={rebuilding}>
                {rebuilding ? "exporting…" : "rebuild the web export"}
              </button>} />
        <div className="bgs-card">
          <div className="h"><span className="t">Web export</span>
            <Tag tone={!play.built ? "off" : play.stale ? "warn" : "good"}>
              {!play.built ? "never built" : play.stale ? "stale" : "current"}
            </Tag>
          </div>
          <div className="kv"><span>state</span><b>
            {play.reason || (play.built
              ? "the build matches the sources"
              : "nothing has been exported yet, so there is nothing to compare")}
          </b></div>
          {play.newest_source && (
            <div className="kv"><span>newest source</span><b className="wrap">{play.newest_source}</b></div>
          )}
          {/* The two clocks, because "stale" without a gap is a word: an export
              an hour behind a source saved a minute ago is a different decision
              from one three weeks behind. */}
          {!!since(play.build_mtime) && (
            <div className="kv"><span>exported</span><b>{since(play.build_mtime)} ago</b></div>
          )}
          {!!since(play.source_mtime) && (
            <div className="kv"><span>newest source saved</span><b>{since(play.source_mtime)} ago</b></div>
          )}
        </div>
        {rebuilding && (
          <Nothing what="exporting the web build…"
                   how={"a headless --export-release of the whole project; " +
                        "the state above is re-read when it lands"} />
        )}
        {rebuilt && (
          <Banner icon={rebuilt.ok ? "circle-check" : "alert-hexagon"}
                  tone={rebuilt.ok ? "good" : "bad"}>
            <div className="t">
              {rebuilt.ok
                ? `exported — ${mb(rebuilt.bytes) || "the pack"} of pack data`
                : rebuilt.error}
            </div>
            {rebuilt.ok && (
              <div className="s">
                index.pck {mb(rebuilt.bytes) || "—"}
                {rebuilt.wasm ? ` · index.wasm ${mb(rebuilt.wasm)}` : ""}
                {" · this is what /play serves from now on"}
              </div>
            )}
          </Banner>
        )}
        <ReadError error={play.__error} what="the build" />
      </div>
    );
  }

  if (tab === "generators") {
    const gen = plumb.generators;
    /* UNGATED FIRST, ALWAYS. The rows arrive in path order, which on a real
       project buries the four scripts that overwrite .tscn without asking among
       twenty that ask properly — and this panel exists for precisely those
       four. Sorting is the only ranking the reader gets. */
    const rows = [...(gen?.rows || [])].sort((a, b) => {
      const rank = (g: Gen) => (g.check_ok && g.dry_ok ? 2 : g.dry_ok ? 1 : 0);
      return rank(a) - rank(b) || a.path.localeCompare(b.path);
    });
    const ungated = rows.filter((g) => !g.check_ok || !g.dry_ok).length;
    /* The failure mode this panel is named after: a script that writes a .tscn
       with nothing gating the write is one invocation away from replacing hand
       placement, and it never asked. Counted off real rows, not asserted. */
    const clobber = rows.filter((g) => !g.dry_ok && (g.writes || []).includes(".tscn"));
    return (
      <div className="bgs-pad">
        <Head label="Generators" hint="ships --check, defaults to dry"
              right={gen ? <span className="bgs-dim">
                {rows.length} of {gen.scanned} scanned scripts write project data
                {ungated ? ` · ${ungated} ungated` : ""}
                {scanned ? ` · ${scanned}` : ""}
              </span> : undefined} />
        {!!clobber.length && (
          <Banner icon="alert-hexagon" tone="bad">
            <div className="t">
              {clobber.length} script{clobber.length === 1 ? "" : "s"} rewrite
              {clobber.length === 1 ? "s" : ""} .tscn with no flag gating the write
            </div>
            <div className="s">
              running one is one command away from replacing hand placement, and it
              will not ask — {clobber.slice(0, 3).map((g) => g.path).join(", ")}
              {clobber.length > 3 ? ` and ${clobber.length - 3} more` : ""}
            </div>
          </Banner>
        )}
        {rows.map((g) => (
          <div className={`bgt-gen${g.check_ok && g.dry_ok ? "" : " bad"}`} key={g.path}>
            <div className="h">
              <span className="p" title={g.path}>{g.path}</span>
              <Tag tone={g.check_ok ? "good" : "bad"}>{g.check}</Tag>
              <Tag tone={g.dry_ok ? "good" : "bad"}>{g.dry}</Tag>
            </div>
            <div className="note">{g.note}</div>
          </div>
        ))}
        {gen && !rows.length && (
          <Nothing what="no script here rewrites project data"
                   how={`${gen.scanned} python file${gen.scanned === 1 ? "" : "s"} under ` +
                        `${gen.dirs.join("/ and ")}/ were read; a generator is one that writes a ` +
                        `.tscn, .tres or .gd, and none of these do`} />
        )}
        {!gen && !plumb.__error && (
          <Nothing what="the script scan has not answered yet"
                   how="/api/tech/plumbing reads the argparse of every script under scripts/ and tools/" />
        )}
        <ReadError error={plumb.__error} what="the generator inventory" />
      </div>
    );
  }

  return (
    <div className="bgs-pad">
      {check && (
        <Banner icon={check.ok ? "circle-check" : "alert-hexagon"}
                tone={check.ok ? "good" : "bad"}
                right={<>
                  {(!!errors.length || mute) && (
                    <button className="bgs-btn"
                            onClick={() => findingsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })}>
                      open findings
                    </button>
                  )}
                  <button className="bgs-btn" onClick={runCheck} disabled={running}>
                    {running ? `checking… ${elapsed}s` : "run again"}
                  </button>
                </>}>
          <div className="t">
            {check.error
              ? check.error
              : check.ok
                ? `godot_check_project — clean in ${check.seconds ?? "?"}s`
                : mute
                  ? `godot_check_project — the import failed and named no file`
                  : `godot_check_project — ${errors.length} finding${errors.length === 1 ? "" : "s"}`}
          </div>
          {!check.error && (
            <div className="s">exit {check.exit_code} · {check.seconds}s · a headless import of the whole project</div>
          )}
        </Banner>
      )}

      {/* THE DOCTRINE, SAID WHEN IT APPLIES. "Run godot_check_project after
          structural changes" is a rule this panel can actually see being broken:
          the tree has moved and nothing has imported it since the screen opened.
          A rule nobody is reminded of at the moment it bites is decoration. */}
      {!check && !running && git?.available && git.dirty && (
        <Banner icon="alert-triangle" tone="warn"
                right={<button className="bgs-btn" onClick={runCheck} disabled={running}>
                  {running ? `checking… ${elapsed}s` : "run godot_check_project"}
                </button>}>
          <div className="t">
            {git.changed} uncommitted path{git.changed === 1 ? "" : "s"}, and nothing
            has imported the project this session
          </div>
          <div className="s">a structural change that broke the import looks exactly
            like one that did not until something imports it</div>
        </Banner>
      )}

      <Head label="Project" hint="the engine, the build, and whether the two agree"
            right={!check
              ? <button className="bgs-btn" onClick={runCheck} disabled={running}>
                  {running ? `checking… ${elapsed}s` : "run godot_check_project"}
                </button>
              : undefined} />

      <div className="bgs-two wide">
        <div className="bgs-card">
          <div className="h"><span className="t">Engine</span>
            <Tag tone={godot.available ? "good" : "bad"}>
              {godot.available ? "available" : "not found"}
            </Tag>
          </div>
          <div className="kv"><span>version</span><b>{godot.version || "—"}</b></div>
          <div className="kv"><span>project</span><b className="wrap">
            {godot.project || "no project.godot was found under this root"}
          </b></div>
          <div className="kv"><span>binary</span><b className="wrap">{godot.path || "—"}</b></div>
          <ReadError error={godot.__error} what="godot status" />
        </div>
        <div className="bgs-card">
          <div className="h"><span className="t">Build</span>
            <Tag tone={!play.built ? "bad" : play.stale ? "warn" : "good"}>
              {!play.built ? "never built" : play.stale ? "stale" : "current"}
            </Tag>
          </div>
          {/* A stale build is the failure where you play yesterday's game and
              report today's bug, so the REASON is on screen, not in a tooltip. */}
          <div className="kv"><span>reason</span><b className="wrap">{play.reason || (play.built
                ? "the build matches the sources"
                /* An empty reason is not a clean bill of health — on a project
                   that has never exported, it is simply nothing to say. */
                : "nothing has been exported yet, so there is nothing to compare")}</b></div>
          {play.newest_source && (
            <div className="kv"><span>newest source</span><b className="wrap">{play.newest_source}</b></div>
          )}
          {!!since(play.build_mtime) && (
            <div className="kv"><span>exported</span><b>{since(play.build_mtime)} ago</b></div>
          )}
          {/* The tree state lives beside the build because they are the same
              question asked twice: is what you are looking at what is on disk.
              A repo git could not read is NOT a clean one, and dropping the row
              when `available` is false said "clean" by saying nothing. */}
          <div className="kv"><span>working tree</span><b className="wrap">
            {git?.available
              ? (git.dirty ? `${git.changed} uncommitted path${git.changed === 1 ? "" : "s"}` : "clean")
              : git
                ? `unreadable — ${git.reason || "git could not answer here"}`
                : "—"}
          </b></div>
          <ReadError error={play.__error} what="the build status" />
        </div>
      </div>

      <Head label="Scene convention" hint="one editable thing = one named node"
            right={scenes ? <span className="bgs-dim">
              {scenes.nodes} nodes across {scenes.scenes} scene{scenes.scenes === 1 ? "" : "s"}
              {scanned ? ` · ${scanned}` : ""}
            </span> : undefined} />
      {(scenes?.rules || []).map((r) => (
        <div className={`bgt-rule t-${tone(r.tone)}`} key={r.rule}>
          <Ti name={RULE_ICON[r.tone] || "eye"} size={15}
              color={`var(--${tone(r.tone)})`} />
          <div className="b">
            <div className="t">{r.rule}</div>
            <div className="d">{r.detail}</div>
            {/* A rule that reports "12" and nothing else cannot be acted on;
                the examples name the scene and the node to open. */}
            {r.examples.slice(0, 3).map((e) => (
              <span className="ex" key={e} title={e}>{e}</span>
            ))}
            {/* Counted off the RULE's total, not off the six examples the API
                carries: three shown out of seven is "and 4 more", and saying
                "and 1 more" because the payload was truncated first is the
                panel quietly under-reporting its own finding. */}
            {r.count > Math.min(3, r.examples.length) && (
              <span className="ex">and {r.count - Math.min(3, r.examples.length)} more</span>
            )}
          </div>
          <span className="n">{r.count === 0 ? "ok" : r.count}</span>
        </div>
      ))}
      {!scenes && !plumb.__error && (
        <Nothing what="the scene scan has not answered yet"
                 how="/api/tech/plumbing parses every .tscn outside .godot and the tool's own backups" />
      )}
      {!!scenes?.unreadable?.length && (
        <div className="bgs-readerr">
          {scenes.unreadable.length} scene file{scenes.unreadable.length === 1 ? "" : "s"} could
          not be parsed as a Godot scene — {scenes.unreadable.slice(0, 3).join(", ")}
          {/* The audit's own blind spot, counted rather than trimmed off the
              end of a sentence: a scene it could not read is the one most
              likely to be broken, and "and 9 more" is the number that decides
              whether the four rules above are measuring the project. */}
          {scenes.unreadable.length > 3 ? ` and ${scenes.unreadable.length - 3} more` : ""}
          {" — the rules above are measured over the "}
          {scenes.scenes} scene{scenes.scenes === 1 ? "" : "s"} that did parse
        </div>
      )}
      <ReadError error={plumb.__error} what="the scene audit" />

      <div ref={findingsRef}>
        <Head label="Findings" hint="a compile failure names the file it failed on" />
      </div>
      {!check && (
        <Nothing what="not checked this session"
                 how="godot_check_project imports the project headlessly — it is the slowest call in the app, so it runs when you ask, not on a timer" />
      )}
      {/* A CHECK THAT NEVER RAN IS NOT A CHECK THAT PASSED. The error path used
          to draw this section empty — a heading over nothing, which reads as
          "no findings" to everyone who has ever seen this panel clean. */}
      {check?.error && (
        <div className="bgs-finding">
          <Ti name="alert-hexagon" size={14} color="var(--bad)" />
          <span>the check did not produce a verdict — {check.error}</span>
        </div>
      )}
      {mute && (
        <div className="bgs-finding">
          <Ti name="alert-hexagon" size={14} color="var(--bad)" />
          <span>
            the import exited {check?.exit_code} and printed no line the error parser
            recognises — the raw output is below, and this is a failure, not a pass
          </span>
        </div>
      )}
      {check && !errors.length && !check.error && !mute && (
        <Nothing what="nothing failed to import" how="the project compiles as it stands" />
      )}
      {errors.map((e, i) => (
        <div className="bgs-finding" key={i}>
          <Ti name="alert-hexagon" size={14} color="var(--bad)" />
          <span>{e}</span>
        </div>
      ))}
      {/* The tail of the engine's own output. Shown for ANY failed check, not
          only a parsed one: on the mute case it is the only evidence there is. */}
      {check?.output && !check.error && check.ok === false && (
        <pre className="bgs-out">{check.output}</pre>
      )}
    </div>
  );
}
