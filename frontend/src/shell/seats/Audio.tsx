import { useCallback, useEffect, useRef, useState } from "react";
import { Ti } from "../Ti";
import { Head, Nothing, Tag, Banner, ReadError } from "./prims";
import { useJSON, useLocks } from "./api";
import { useSeatChips } from "./chips";
import { readJSON, mutate, pollQueue, toast } from "../../bridge";
import type { SeatBodyProps } from "./types";
import "./artaudio.css";

/* AUDIO — the hook table, because a sound the game never asks for is a sound
 * nobody hears.
 *
 * THE EVENTS COME FROM THE GAME'S OWN CODE. This panel used to read a cue sheet
 * — a document a seat was supposed to write by hand — so on every project that
 * had not written one it said "no cue sheet" and stopped, which is a true
 * sentence about a document and no answer at all about the build. /api/audio/hooks
 * (new) walks the project's scripts and scenes for the calls that play a sound
 * (`Audio.sfx("melee_hit")`, a `res://…wav` in a scene) and resolves each one
 * against the files on disk. An UNBOUND row is then a real finding with a file
 * and a line number behind it: the game plays a sound there and the build makes
 * no noise.
 *
 * AND AN UNBOUND ROW IS NOW ACTIONABLE. Showing the seat's central failure and
 * offering nothing to do about it is half a panel: the fix for "the game asks
 * for sfx.door_open and nothing answers" is a sound named so the hook resolves,
 * and that is one board item whose whole brief — the event, the stem the
 * resolver will look for, the call sites — this panel already holds. It files
 * it. Nobody should have to retype `sfx.door_open` into the Generate tab.
 *
 * LOUDNESS IS MEASURED NOW, and where it is not, it says which. bgate_core.loudness
 * runs ffmpeg's ebur128 — the EBU R 128 reference implementation — because
 * integrated loudness is a gated K-weighted average and nothing about it can be
 * inferred from a header. Two things come back without an integrated reading and
 * both are honest: no ffmpeg on the machine, and a one-shot shorter than the
 * 400 ms block the standard integrates over. Neither renders as a LUFS number.
 *
 * THE SHORT ONE-SHOT STILL HAS A MEASURED NUMBER AND THIS PANEL SHOWS IT. Half
 * this project's SFX are under 400 ms, so ebur128 gates every block away and
 * `lufs` is null — but the same run measures the TRUE PEAK, and loudness.py
 * deliberately keeps it on the unmeasured result for exactly this. The panel
 * used to print "not measured" over six rows that carried a peak in dBFS, which
 * is dropping a measurement on the floor and calling the file unexamined. Peak
 * is not loudness and is never compared against the -14 LUFS target; it is
 * labelled as what it is.
 */

type Site = { file: string; line: number };
type Hook = {
  event: string; family: string; name: string; file?: string | null;
  state: string; sites: Site[]; n: number;
  lufs?: number | null; true_peak?: number | null;
  loudness_state?: string; loudness_note?: string;
};
type Hooks = {
  events?: Hook[]; dynamic?: { expr: string; file: string; line: number }[];
  unresolved_paths?: { path: string; file: string; line: number }[];
  orphans?: string[]; scanned_files?: number; sound_count?: number;
  unbound?: number; target_lufs?: number; tolerance_lu?: number;
  loudness_available?: boolean; loudness_note?: string;
};
type Sound = { rel: string; name: string; bytes?: number };

const kb = (n?: number) => (typeof n === "number" ? `${Math.round(n / 1024)} kb` : "");
const isOff = (r: Hook) =>
  r.loudness_state === "too loud" || r.loudness_state === "too quiet";

/** The doctrine every audio board item carries, so the agent that picks one up
 *  is held to the same thing this panel judges it by. Same words as the audio
 *  recipes in recipes.ts — deliberately duplicated rather than exported, so a
 *  seat file never reaches into another seat's table. */
const AUDIO_RULE =
  "Bind the sound to a real game event — a sound the game never asks for is a " +
  "sound nobody hears. Target -14 LUFS and report the MEASURED loudness. Lock " +
  "the binary before editing: audio files do not merge.";

/* THE SCAN IS POLLED AND ALSO FORCED, which useJSON alone cannot do.
 *
 * The server holds a scan for 45 s (routes/audio_hooks.TTL_S) and the panel
 * polls at 20 s, so somebody who has just wired a cue waits out a cache they
 * cannot see for up to three quarters of a minute and has no way to say "look
 * again" — the endpoint takes ?refresh=1 and nothing on this page ever sent it.
 * usePoll's effect only re-runs on [ms, enabled], so changing useJSON's path to
 * carry the flag would not fetch until the next tick either. */
function useScan(active: boolean) {
  const [data, setData] = useState<Hooks & { __error?: string }>({});
  const [busy, setBusy] = useState(false);
  const load = useCallback(async (force?: boolean) => {
    if (force) setBusy(true);
    const d = await readJSON<Hooks>(
      force ? "/api/audio/hooks?refresh=1" : "/api/audio/hooks", {});
    setData(d);
    if (force) setBusy(false);
  }, []);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const tick = () => { if (alive) void load(); };
    tick();
    const id = window.setInterval(tick, 20000);
    return () => { alive = false; window.clearInterval(id); };
  }, [active, load]);
  return { data, busy, rescan: () => void load(true) };
}

/* ONE PLAYER, NOT ONE PER ROW. Twenty <audio> elements is twenty preloads of
 * files up to 5 MB, and nothing stops two of them playing at once — which for
 * a seat judging whether a stinger sits over a bed is the opposite of useful.
 *
 * document.createElement, NOT `new Audio()`: this module exports a component
 * called Audio, which shadows the global constructor at module scope. */
function usePlayer() {
  const el = useRef<HTMLAudioElement | null>(null);
  const [now, setNow] = useState("");
  useEffect(() => () => { el.current?.pause(); el.current = null; }, []);
  const toggle = useCallback((rel: string) => {
    let a = el.current;
    if (!a) {
      a = el.current = document.createElement("audio");
      a.addEventListener("ended", () => setNow(""));
    }
    if (a.src && !a.paused && now === rel) { a.pause(); setNow(""); return; }
    a.src = `/api/audio/file?rel=${encodeURIComponent(rel)}`;
    setNow(rel);
    /* A refusal is reported. A play button that silently does nothing is the
       same sentence as a file that makes no noise, and this seat has to be able
       to tell those apart. */
    void a.play().catch(() => {
      setNow("");
      toast(`the browser would not play ${rel}`, "warn");
    });
  }, [now]);
  return { now, toggle };
}

function Play({ rel, now, onToggle }: {
  rel?: string | null; now: string; onToggle: (rel: string) => void;
}) {
  if (!rel) return <span className="bga-play empty" title="no file to play" />;
  const on = now === rel;
  return (
    <button className={`bga-play${on ? " on" : ""}`}
            onClick={() => onToggle(rel)}
            title={on ? `stop ${rel}` : `play ${rel}`}>
      <Ti name={on ? "player-stop" : "player-play"} size={11} />
    </button>
  );
}

/** The loudness cell. Three states and they are three different sentences:
 *  an integrated reading judged against the target, a true peak with NO
 *  integrated reading (the short one-shot), and nothing measured at all. */
function Loud({ r }: { r: Hook }) {
  if (typeof r.lufs === "number") {
    return (
      <span className={`lufs${isOff(r) ? " warn" : ""}`}
            title={typeof r.true_peak === "number"
              ? `true peak ${r.true_peak} dBFS` : undefined}>
        {r.lufs} LUFS
      </span>
    );
  }
  if (typeof r.true_peak === "number") {
    return (
      <span className="lufs dim" title={r.loudness_note ||
        "no integrated loudness — the target is a LUFS target and this is a peak"}>
        {r.true_peak} dBFS<em>peak only</em>
      </span>
    );
  }
  return (
    <span className="lufs dim" title={r.loudness_note}>
      {r.file ? "not measured" : "—"}
    </span>
  );
}

export function Audio({ seat, active, tab }: SeatBodyProps) {
  const scan = useScan(active);
  const hooks = scan.data;
  const lib = useJSON<{ sounds?: Sound[] }>("/api/audio/list", { sounds: [] },
                                            15000, active && tab === "library");
  const locks = useLocks(active);
  const player = usePlayer();
  const [filed, setFiled] = useState<Record<string, number>>({});

  const rows = hooks.events || [];
  const sounds = lib.sounds || [];
  const unbound = rows.filter((r) => r.state === "unbound");
  const scanned = hooks.scanned_files || 0;
  const held = [...locks.held, ...locks.path_leases]
    .filter((l) => /\.(wav|mp3|ogg|flac)$/i.test(String(l.path || "")));
  const boundRows = rows.filter((r) => !!r.file);
  const measured = rows.filter((r) => typeof r.lufs === "number");
  const offTarget = rows.filter(isOff);

  /* The file a hook resolved to, keyed for the library — lower-cased because a
     call site spells a name and the disk spells a path, and on Windows the two
     disagree about case more often than about anything else. */
  const boundFiles = new Map<string, Hook>();
  for (const r of rows) if (r.file) boundFiles.set(String(r.file).toLowerCase(), r);

  /** File one board item for an unbound event, with the brief this panel
   *  already holds: the event, the stem the resolver looks for, and the lines
   *  that play it. */
  async function fileSound(r: Hook) {
    const where = r.sites.map((s) => `${s.file}:${s.line}`).join(", ");
    const target = r.family === "res"
      ? `The call is a res:// literal, so the file must land at exactly ` +
        `game/${r.name} (or ${r.name}) — the path IS the binding and nothing ` +
        `else resolves it.`
      : `Name it so the hook resolves with no code change: the resolver looks ` +
        `for a file whose stem is "${r.family}_${r.name}" first, then ` +
        `"${r.name}", under game/assets/audio.`;
    const res = await mutate("/api/queue", {
      quiet: true,
      body: {
        seat: "audio",
        title: `SFX: ${r.event} is unbound`,
        brief:
          `The game asks for ${r.event} and nothing on disk answers, so that ` +
          `moment is silent in the build. It is played ${r.n} time` +
          `${r.n === 1 ? "" : "s"}, at ${where || "call sites the scan listed"}.` +
          `\n\n${target}\n\n${AUDIO_RULE}`,
        source: "seat",
        source_ref: `audio:hook:${r.event}`,
      },
    });
    if (!res.ok) { toast(res.error || "could not file the work", "warn"); return; }
    const id = Number((res.data as { id?: number } | null)?.id || 0);
    setFiled((f) => ({ ...f, [r.event]: id }));
    pollQueue();
    toast(`filed ${r.event} on the board${id ? ` as #${id}` : ""}`, "ok");
  }

  useSeatChips(seat.role, [
    /* Only after a scan has actually run: "0 unbound" before the first read is
       a claim nothing has checked. */
    ...(scanned ? [{
      icon: unbound.length ? "plug-off" : "plug",
      label: `${unbound.length} unbound`,
      color: unbound.length ? "var(--warn)" : "var(--good)",
      title: `${rows.length} events found in ${scanned} scanned files`,
    }] : []),
    /* The seat's target is only worth publishing as a NUMBER OF FAILURES. A
       "-14 LUFS target" chip over a table with three files off it announces the
       policy and hides the breach. */
    ...(offTarget.length ? [{
      icon: "alert-triangle", label: `${offTarget.length} off target`,
      color: "var(--warn)",
      title: offTarget.map((r) => `${r.event} ${r.lufs} LUFS (${r.loudness_state})`)
        .join(" · "),
    }] : []),
    ...(held.length ? [{
      icon: "lock", label: `${held.length} locked`, color: "var(--text-3)",
      title: held.map((l) => l.path).join(" · "),
    }] : []),
    /* The target is the seat's declared discipline, so it is published only
       when it is enforceable — with no ffmpeg nothing here can measure against
       it, and a target chip over an unmeasurable column is decoration. */
    ...(hooks.loudness_available && typeof hooks.target_lufs === "number" ? [{
      icon: "wave-sine", label: `${hooks.target_lufs} LUFS target`,
      color: "var(--c-audio)",
      title: `${measured.length} of ${boundRows.length} bound hooks have an ` +
        `integrated reading (ffmpeg ebur128); the rest are shorter than the ` +
        `400 ms EBU gate and show their measured peak instead`,
    }] : []),
  ]);

  if (tab === "library") {
    /* WHICH FILES ARE ORPHANS IS DECIDED HERE, NOT TAKEN FROM THE SCAN.
       audiohooks.sound_index keys by STEM and the first writer wins, so
       music_combat.ogg hides music_combat.wav: the server's orphans list has a
       hole exactly the shape of every same-stem duplicate, and this project has
       two of them worth 6.6 MB. A file is an orphan when NO event resolved to
       it, which is the question the column is asking. */
    const orphanRels = sounds.filter((s) => !boundFiles.has(s.rel.toLowerCase()));
    return (
      <div className="bgs-pad">
        <Head label="Library" hint={`${sounds.length} files on disk`} />
        <ReadError error={lib.__error} what="the audio library" />
        {!sounds.length && !lib.__error && (
          <Nothing what="no audio in the project" how="sfx_generate and kie_music_generate write into game/assets/audio" />
        )}
        {!!sounds.length && (
          <div className="bgs-table" style={{ ["--cols" as string]: "24px 1fr 84px 116px 96px" }}>
            <div className="th">
              <span /><span>file</span><span>size</span><span>loudness</span>
              <span className="right">asked for</span>
            </div>
            {sounds.map((s) => {
              const hook = boundFiles.get(s.rel.toLowerCase());
              return (
                <div className="tr" key={s.rel}>
                  <Play rel={s.rel} now={player.now} onToggle={player.toggle} />
                  <span className="mono" title={s.rel}>{s.rel}</span>
                  <span className="mono dim">{kb(s.bytes)}</span>
                  {/* Only bound files were measured — the loudness pass runs
                      over the hook table, so an orphan has no reading and says
                      so rather than showing a blank that reads as fine. */}
                  <span className="bga-libloud">
                    {hook ? <Loud r={hook} />
                          : <span className="lufs dim"
                                  title="the loudness pass measures the files hooks resolve to; nothing asks for this one">
                              not measured
                            </span>}
                  </span>
                  <span className="right">
                    {hook
                      ? <Tag tone="good" title={`${hook.event} plays it`}>bound</Tag>
                      : scanned
                      /* "Orphan" is a claim about the GAME, and it is only sayable
                         because the scan read the game's code. Before the scan
                         existed this column called all 22 files orphaned against a
                         cue sheet nobody had written. */
                      ? <Tag tone="off" title="no call site in the project's scripts or scenes asks for this file">orphan</Tag>
                      : <Tag tone="off" title="the hook scan has not run yet">unscanned</Tag>}
                  </span>
                </div>
              );
            })}
          </div>
        )}
        {!!orphanRels.length && !!scanned && (
          <div className="bgs-reasons">
            <div>{orphanRels.length} file{orphanRels.length === 1 ? "" : "s"} nothing
              asks for, {kb(orphanRels.reduce((n, s) => n + (s.bytes || 0), 0))} of
              them. A file with no hook is a file nobody hears — it is wasted
              work, not a broken build. Play one before deleting it: the scan
              reads calls, not intentions.</div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="bgs-pad">
      <Head label="Hooks" hint="a sound the game never asks for is a sound nobody hears"
            right={
              <>
                {!!scanned && (
                  <span className="bga-ran">
                    <Ti name="file-search" size={13} />
                    {scanned} files scanned · {hooks.sound_count} on disk
                  </span>
                )}
                {/* The server holds a scan for 45 s. Without this the only way
                    to see a cue you just wired is to wait out a cache the page
                    never mentions. */}
                <button className="bgs-btn" onClick={scan.rescan} disabled={scan.busy}
                        title="re-walk the project's scripts and scenes now, ignoring the 45 s server cache">
                  <Ti name="refresh" size={12} /> {scan.busy ? "scanning…" : "rescan"}
                </button>
              </>
            } />
      <ReadError error={hooks.__error} what="the hook scan" />
      {!rows.length && !hooks.__error && (
        <Nothing what={scanned ? "the game asks for no sounds" : "no scan yet"}
                 how={scanned
                   ? `${scanned} scripts and scenes were read and none of them plays a sound by name or by res:// path — sfx_generate writes the file, but a line of game code has to ask for it`
                   : "/api/audio/hooks walks the project's scripts for the calls that play audio"} />
      )}
      {!!rows.length && (
        <>
          <div className="bga-hook head">
            <span /><span>event</span><span>bound to</span><span>loudness</span>
            <span style={{ textAlign: "right" }}>state</span>
          </div>
          {rows.map((r, i) => {
            const bad = r.state === "unbound";
            const off = isOff(r);
            const last = i === rows.length - 1 && !bad;
            return (
              <div key={r.event}>
                <div className={`bga-hook${bad ? " bad" : off ? " off" : ""}${last ? " last" : ""}`}>
                  <Play rel={r.file} now={player.now} onToggle={player.toggle} />
                  <span className="ev">
                    {r.event}{r.n > 1 && <span className="n"> ×{r.n}</span>}
                  </span>
                  <span className="file" title={r.file || "nothing answers this"}>
                    {r.file || "—"}
                  </span>
                  <Loud r={r} />
                  <span className={`st${bad ? " bad" : off ? " warn" : ""}`}>
                    {bad ? "unbound" : r.loudness_state || "wired"}
                  </span>
                </div>
                {bad && (
                  <div className={`bga-sites${last ? " last" : ""}`}>
                    <span className="w">
                      {r.sites.length ? <>played at{" "}
                        {r.sites.map((s, j) => (
                          <span key={j}>{j ? ", " : ""}<b>{s.file}:{s.line}</b></span>
                        ))}
                      </> : "no call site was recorded for this event"}
                    </span>
                    {/* THE PANEL'S ONE VERB. Showing the seat's central failure
                        and offering nothing to do about it is half a panel. */}
                    {filed[r.event] !== undefined
                      ? <span className="done">
                          <Ti name="check" size={11} /> filed
                          {filed[r.event] ? ` as #${filed[r.event]}` : ""}
                        </span>
                      : <button className="bgs-btn" onClick={() => void fileSound(r)}
                                title={`file a board item to make and bind ${r.event}`}>
                          make this sound
                        </button>}
                  </div>
                )}
              </div>
            );
          })}
        </>
      )}

      <Banner icon="lock" tone={held.length ? "good" : "off"}
              right={<span className="mono dim">{held.map((l) => l.path).join(" · ")}</span>}>
        <div className="t">
          Same lock discipline as art — audio binaries don't merge either.{" "}
          <b>{held.length} file{held.length === 1 ? "" : "s"} locked</b> by this seat right now.
        </div>
        {/* A lock is taken and dropped by the agent holding it (asset_lock /
            asset_release). There is no HTTP release — /api/locks is GET only —
            so this says who to ask instead of drawing a button that would 404. */}
        {!!held.length && (
          <div className="s">
            released by the seat that took it, with asset_release — the dashboard
            has no write on /api/locks
          </div>
        )}
      </Banner>

      {!!(hooks.dynamic || []).length && (
        <>
          <Head label="Asked for by variable"
                hint="a real call site whose name is not knowable without running the game" />
          {(hooks.dynamic || []).map((d, i) => (
            <div className="bgs-finding" key={i}>
              <Ti name="variable" size={14} color="var(--text-3)" />
              <span>
                <b>{d.expr}</b> — {d.file}:{d.line}. The scan will not guess what
                this resolves to, so whatever it plays is neither wired nor
                unbound above.
              </span>
            </div>
          ))}
        </>
      )}

      {!hooks.loudness_available && !!rows.length && (
        <div className="bgs-reasons">
          <div>{hooks.loudness_note || "loudness cannot be measured on this machine"}
            {" "}— every loudness cell reads "not measured" until there is one,
            and none of them will be filled in with a plausible value.</div>
        </div>
      )}
    </div>
  );
}
