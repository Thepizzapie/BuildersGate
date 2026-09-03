import { useCallback, useEffect, useReducer, useRef, type ReactNode } from "react";
import { mutate, readJSON, toast } from "../../bridge";
import { askConfirm } from "../../ask";
import { useEvents } from "../../hooks";
import { GAMEFRAME_EVENT } from "./Notepad";
import type { Check, PlayStatus, Preflight, Status } from "./api";

/* Play & record — the build in a panel, and the recorder beside it.
 *
 * TWO THINGS, TWO CONTROLS. Running the build and recording a session are
 * independent: you can play without recording, and a recording of a native
 * game does not use the panel's build at all. So each gets its own
 * start/stop, each says which of the two it acts on, and neither ever
 * silently does the other's job.
 *
 * THE STATUS POLL IS THE ONLY THING ON THE PAGE that notices a recording has
 * started, stopped or died — including one started from an MCP tool — so it
 * runs whether or not this deck is on screen, on playtest.* events plus a
 * slow timer, and at the documented fast tick only while a session is live.
 * The PREFLIGHT is the expensive one (it opens the microphone and loads a
 * whisper model), so it runs only while the panel is visible, never while
 * recording, cheapest probe first, and backs off once it has come back ready. */

/* The literal thing to install, per check. NO `pip install` AND NO `source
   checkout` IN THIS TABLE: somebody running the packaged app cannot act on
   either. Anything the app can fetch for itself gets a BUTTON instead (see
   PT_INSTALLABLE and bgate_core/toolbin). What is left is only the genuinely
   human steps: plug in a microphone, start the game, make a project. */
const PT_FIXES: Record<string, string> = {
  mic: "no usable input device - connect a microphone and enable it in the OS sound settings",
  window: "no game window to capture - start the game first, then re-check; otherwise the whole desktop gets recorded",
  native_game: "no game yet - create or open a game project first",
};
/* Checks the app can satisfy on its own, mapped to the toolbin name. */
const PT_INSTALLABLE: Record<string, string> = { ffmpeg: "ffmpeg", ffprobe: "ffmpeg" };
const ptFix = (name: string) => PT_FIXES[name] || "";

const PT_FRESH_MS = 180000;    // 3 min once ready
const PT_RETRY_MS = 20000;     // floor between probes while broken
const STATUS_LIVE_MS = 4000;   // the fast tick, only while a session is live
const STATUS_IDLE_MS = 30000;

type Holder =
  | { kind: "idle" }
  | { kind: "rebuilding" }
  | { kind: "failed"; error: string };

type Model = {
  ready: boolean;
  recording: boolean;
  nativeGame: boolean;
  /* Set while a kill is in flight and until the server stops reporting a live
     session: the poll auto-boots the frame whenever status says something is
     recording, so a tick landing between the kill and the database catching
     up would re-launch the build the kill had just ended. */
  aborting: boolean;
  /* Who owns the message line. The preflight writes the resting line and
     parks a copy in `idleMsg`; the poll writes over it while a session is
     recording, processing or broken, and hands it back when none is true. */
  transient: boolean;
  idleMsg: ReactNode;
  msg: ReactNode;
  lamp: string;
  why: [string, Check][] | null;
  whyHidden: boolean;
  frame: string | null;
  holder: Holder;
  fresh: ReactNode;
  busyRec: boolean;
  busyBuild: boolean;
  target: "web" | "native";
  goal: string;
  installing: Record<string, string>;
};

const INITIAL: Model = {
  ready: false, recording: false, nativeGame: false, aborting: false, transient: false,
  idleMsg: "", msg: "checking playtest gear…", lamp: "lamp", why: null, whyHidden: true,
  frame: null, holder: { kind: "idle" }, fresh: "", busyRec: false, busyBuild: false,
  target: "web", goal: "", installing: {},
};

type Props = {
  /** Is the Playtests deck on screen. Gates the preflight, not the status. */
  active: boolean;
  /** The controls line, from /api/state via the store. */
  controls: unknown[] | null;
  /** Reported up so the notepad and the sessions list share one poll. */
  onStatus: (s: Status) => void;
  /** Handed to the notepad: a fresh read on demand. */
  register: (refresh: () => Promise<void>) => void;
};

export function PlayPanel({ active, controls, onStatus, register }: Props) {
  /* One mutable model behind a render tick. The recorder is a state machine
     driven from async functions that read state AFTER an await — a useState
     snapshot would be the value at the start of the call, which is how a
     failed start left the button reading "starting…" forever in an earlier
     life. Writes land on the ref immediately and the tick repaints. */
  const m = useRef<Model>({ ...INITIAL });
  const [, tick] = useReducer((x: number) => x + 1, 0);
  const patch = useCallback((p: Partial<Model>) => { Object.assign(m.current, p); tick(); }, []);
  const activeRef = useRef(active); activeRef.current = active;
  const _ptLastCheck = useRef(0);
  const _ptDoctorGone = useRef(false);

  /* ── the build ────────────────────────────────────────────────────────── */
  const bootFrame = useCallback((sessionId: number | string | null = null) => {
    const session = sessionId ? `&bgate_session=${encodeURIComponent(String(sessionId))}` : "";
    patch({ frame: `/play/?t=${Date.now()}${session}`, holder: { kind: "idle" } });
  }, [patch]);

  /* NOT bootFrame(). bootFrame() with no session id boots the game AGAIN; a
     kill has to leave the panel empty. */
  const clearFrame = useCallback(() => patch({ frame: null, holder: { kind: "idle" } }), [patch]);

  /* IS THE EXPORT CURRENT WITH THE SOURCE, ANSWERED ON SCREEN. It names the
     file: "level-2.json is newer than the build" is a sentence somebody can
     act on; "stale" is one they have to investigate. Cheap and on demand —
     it walks the game directory server-side, so it runs when the panel opens
     and after a build, not on a timer. */
  const refreshBuildFreshness = useCallback(async () => {
    const st = await readJSON<PlayStatus>("/api/play/status", {});
    if (st.__error) { patch({ fresh: "" }); return; }
    if (!st.built) {
      patch({ fresh: <><span style={{ color: "var(--warn)" }}>no build yet</span> ·{" "}
        {st.reason || "never exported"} — booting will export it first</> });
      return;
    }
    if (st.stale) {
      /* A REBUILD BUTTON ON A MACHINE THAT CANNOT EXPORT IS A TRAP. `blocked`
         is the same probe `doctor` reports, and it names both versions. */
      if (st.blocked) {
        patch({ fresh: <><span style={{ color: "var(--ember)" }}>cannot build</span> · {st.blocked}</> });
        return;
      }
      patch({ fresh: <><span style={{ color: "var(--warn)" }}>build is behind</span> ·{" "}
        {st.newest_source || "a source file"} is newer — booting rebuilds first,{" "}
        <button className="qbtn small ghost" type="button" onClick={() => void rebuildNow()}>rebuild now</button></> });
      return;
    }
    patch({ fresh: <><span style={{ color: "var(--good)" }}>build is current</span> ·{" "}
      nothing in the project is newer than the export</> });
  }, [patch]);

  /* Rebuild WITHOUT booting. Somebody who has just landed a change and wants
     the export refreshed before they hand the machine to a playtester should
     not have to launch the game to get it. */
  async function rebuildNow() {
    patch({ fresh: "exporting from current source…" });
    const r = await mutate("/api/play/rebuild", { quiet: true });
    if (!r.ok) {
      /* THE FAILURE STAYS ON SCREEN. A toast is gone in four seconds and this
         is the one message that names what to install. */
      patch({ fresh: <><span style={{ color: "var(--ember)" }}>rebuild failed</span> ·{" "}
        {r.error || "godot could not export"}{" "}
        <button className="qbtn small ghost" type="button" onClick={() => void rebuildNow()}>try again</button></> });
      toast("rebuild failed - " + r.error);
      return;
    }
    await refreshBuildFreshness();
  }

  async function loadGame() {
    // Never serve a stale build — rebuild if the source is newer than the export.
    const st = await readJSON<PlayStatus>("/api/play/status", {});
    if (st.stale) {
      patch({ holder: { kind: "rebuilding" } });
      const r = await mutate("/api/play/rebuild", { quiet: true });
      if (!r.ok) {
        /* NAMES THE CAUSE, and offers the stale build anyway - a missing
           export template is not a reason to refuse to play what is built. */
        patch({ holder: { kind: "failed", error: r.error || "" } });
        toast("rebuild failed - " + r.error);
        return;
      }
    }
    bootFrame();
    void refreshBuildFreshness();
  }

  /* ── preflight ────────────────────────────────────────────────────────── */
  function togglePtWhy() { patch({ whyHidden: !m.current.whyHidden }); }

  function renderPreflight(p: Preflight) {
    const failed = Object.entries(p.checks || {}).filter(([, c]) => !(c.ok ?? c.available));
    /* BLOCKING ONLY. The server marks each check required or not and `ready`
       already means "nothing blocking" — the transcriber only decides whether
       you get a transcript, and it must not hold the Record button down. */
    const bad = failed.filter(([, c]) => c.required !== false);
    const degraded = failed.filter(([, c]) => c.required === false);
    const ready = Boolean(p.ready);
    if (ready) {
      /* READY IS READY. NOTHING ELSE GOES HERE. Where the transcript's absence
         belongs is on the recording itself, once there is one to open. */
      const line = "playtest ready - record, play, talk";
      patch({ ready, lamp: "lamp", idleMsg: line, why: null, whyHidden: true,
              ...(m.current.transient ? {} : { msg: line }) });
      void refreshBuildFreshness();
      return;
    }
    const line = <>record unavailable · {bad.length} check{bad.length === 1 ? "" : "s"} failing{" "}
      <button className="qbtn small ghost" type="button" onClick={togglePtWhy}>what is missing?</button></>;
    /* No window list here: it printed the title of every window open on the
       desktop into a panel about installing ffmpeg. */
    patch({ ready, lamp: "lamp bad", idleMsg: line, why: bad.concat(degraded), whyHidden: false,
            ...(m.current.transient ? {} : { msg: line }) });
  }

  /* Only while the panel holding the button is on screen — this deck's own
     .active flag, not a hard-coded view id — and the tab is visible. */
  function panelVisible() {
    return activeRef.current && document.visibilityState !== "hidden";
  }

  async function ptPreflight(force: boolean) {
    if (m.current.recording) return;
    if (!force && !panelVisible()) return;
    const now = Date.now();
    if (!force && now - _ptLastCheck.current < (m.current.ready ? PT_FRESH_MS : PT_RETRY_MS)) return;
    _ptLastCheck.current = now;
    const native = m.current.target === "native";

    // Cheap path: the doctor probes the binaries WITHOUT opening an audio
    // device or spawning a whisper probe. If the toolchain itself is missing
    // we can say exactly what to install and never touch the hardware at all.
    if (!_ptDoctorGone.current) {
      const d = await readJSON<Record<string, Check> & { checks?: Record<string, Check> }>("/api/doctor", {});
      if (d.__error) {
        _ptDoctorGone.current = true;   // no such endpoint on this build — stop asking
      } else {
        const rows = (d.checks || d) as Record<string, Check>;
        /* ONLY WHAT ACTUALLY BLOCKS RECORDING. Server-side and client-side
           both have to agree on what is optional; the transcriber is not in
           this list because this path returns early with a preflight it made
           up itself. */
        const need = native ? ["ffmpeg", "godot"] : ["ffmpeg"];
        const missing = need.filter((k) => rows[k] && rows[k].available === false);
        if (missing.length) {
          renderPreflight({ ready: false,
            checks: Object.fromEntries(missing.map((k) => [k, rows[k]])) });
          return;
        }
      }
    }
    // Toolchain is fine (or unknown) — now the expensive check, the only one
    // that can answer "is there a mic" and "what would we capture".
    const p = await readJSON<Preflight>(`/api/playtest/preflight?native=${native}`, {});
    if (p.__error) {
      patch({ msg: `could not check playtest gear - ${p.__error}` });
      return;
    }
    renderPreflight(p);
  }

  async function ptInstall(tool: string) {
    patch({ installing: { ...m.current.installing, [tool]: "downloading…" } });
    try {
      await mutate(`/api/tools/${tool}/install`, { ok: `installing ${tool}` });
      /* Poll until it lands, then re-run the preflight so the row goes green
         without the user hunting for a refresh. Bounded, and only while an
         install is in flight. */
      for (let i = 0; i < 300; i++) {
        const s = await readJSON<{ present?: boolean;
          running?: { state?: string; done?: number; total?: number; error?: string } }>(`/api/tools/${tool}`, {});
        const run = s.running || {};
        if (run.total) {
          patch({ installing: { ...m.current.installing,
            [tool]: `${Math.round(100 * (run.done || 0) / run.total)}%` } });
        }
        if (run.state === "done" || s.present) break;
        if (run.state === "failed") { toast(run.error || "install failed", "err"); break; }
        await new Promise((r) => setTimeout(r, 1000));
      }
    } finally {
      const rest = { ...m.current.installing };
      delete rest[tool];
      patch({ installing: rest });
      void ptPreflight(true);
    }
  }

  /* ── the recorder ─────────────────────────────────────────────────────── */
  async function ptPoll() {
    const s = await readJSON<Status>("/api/playtest/status", { recording: null, processing: [] });
    if (s.__error) {
      /* NOT SILENT. This poller is the only thing on the page that notices a
         recording has started, stopped or died; say it once and keep polling. */
      if (!m.current.transient) {
        patch({ transient: true, msg: `could not read the recorder - ${s.__error}` });
      }
      return;
    }
    onStatus({ recording: s.recording || null, processing: s.processing || [] });

    if (s.recording) {
      const rec = s.recording;
      patch({ recording: true, nativeGame: !!rec.native, transient: true,
              lamp: "lamp" + (rec.telemetry_events > 0 ? "" : " waiting") });
      const frame = m.current.frame;
      if (!m.current.aborting && !rec.native
          && (!frame || !frame.includes(`bgate_session=${rec.id}`))) {
        bootFrame(rec.id);
      }
      patch({ msg: `RECORDING session ${rec.id} - ${rec.telemetry_events} telemetry events` });
      return;
    }
    if (m.current.recording) patch({ recording: false });

    /* PARTITION, DO NOT FILTER. A session whose post-processing failed is the
       one thing this list can hold that matters: read processing_error and
       offer the retry next to the button the user is looking at. */
    const proc = s.processing || [];
    const failed = proc.filter((v) => v.stage === "failed");
    const busy = proc.filter((v) => v.stage !== "failed");
    if (failed.length) {
      const f = failed[0];
      patch({ transient: true, msg: <>session {f.id} failed to process{f.error ? ` - ${f.error}` : ""}{" "}
        <button className="qbtn small ghost" type="button" onClick={() => void retrySession(f.id)}>retry</button></> });
      return;
    }
    if (busy.length) {
      patch({ transient: true, msg: `${busy[0].stage || "processing"} session ${busy[0].id}…` });
      return;
    }
    // Nothing recording and nothing processing: the panel is about the Record
    // button again. Hand it back the preflight's own line.
    patch({ aborting: false, nativeGame: false });
    if (m.current.transient) patch({ transient: false, msg: m.current.idleMsg });
  }

  async function retrySession(id: number) {
    const r = await mutate(`/api/playtest/${id}/retry`, { ok: `session ${id} re-queued` });
    if (!r.ok) return;
    window.pollState?.();
    void ptPoll();
  }

  const live = m.current.recording;
  /* The recorder's own state changes arrive as playtest.* from the routes; a
     session started from an MCP tool is what the timer is for. Fast only
     while one is live — the same cadence the notepad kept. */
  useEvents(ptPoll, { kinds: ["playtest.*"], fallbackMs: live ? STATUS_LIVE_MS : STATUS_IDLE_MS });
  /* The 30s tick doubles as the preflight's clock: it self-throttles (3 min
     once ready, 20s while broken) and returns at once unless the panel is on
     screen. It used to open the microphone every 15 seconds, forever. */
  useEvents(() => void ptPreflight(false), { kinds: [], fallbackMs: STATUS_IDLE_MS, enabled: active });
  /* The functions above are re-created per render and read only refs, so the
     latest one is always the right one to hand out. */
  const latest = useRef({ ptPoll, ptPreflight });
  latest.current = { ptPoll, ptPreflight };
  useEffect(() => { register(() => latest.current.ptPoll()); }, [register]);
  useEffect(() => {
    void latest.current.ptPoll().then(() => latest.current.ptPreflight(true));
    const onVis = () => {
      if (document.visibilityState === "visible") void latest.current.ptPreflight(false);
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);
  useEffect(() => { if (active) void latest.current.ptPreflight(false); }, [active]);

  /* Guards the whole start/stop transition, and makes the button say what is
     happening — a second click during the seconds a start takes used to race
     the first into start(), and both won. */
  async function toggleRecord() {
    if (m.current.busyRec) return;
    patch({ busyRec: true });
    try { await _toggleRecord(); }
    finally { patch({ busyRec: false }); }
  }

  async function _toggleRecord() {
    if (!m.current.recording) {
      const target = m.current.target;
      /* ONLY REBUILD WHEN WE ARE ABOUT TO BOOT THE BUILD FOR THIS RECORDING.
         A build already running is the build being recorded, full stop —
         rebuilding under it would either stamp a build_ref that lies or throw
         away the run the person is sitting in front of. */
      const alreadyRunning = !!m.current.frame;
      const buildStatus = (target === "web" && !alreadyRunning)
        ? await readJSON<PlayStatus>("/api/play/status", {}) : { stale: false };
      if (target === "web" && buildStatus.stale) {
        patch({ msg: "rebuilding current build before recording…" });
        const rebuilt = await mutate("/api/play/rebuild", { quiet: true });
        if (!rebuilt.ok) {
          patch({ msg: "record blocked: current build failed - " + rebuilt.error });
          toast("record blocked - the current build failed to export: " + rebuilt.error);
          return;
        }
      }
      const started = await mutate<{ recording?: unknown; session_id?: number; error?: string;
        native_launch?: { pid?: number } }>("/api/playtest/start", {
        body: {
          name: m.current.goal.trim() || "Evaluate current build " + new Date().toLocaleTimeString(),
          launch_native: target === "native",
        } });
      const got = started.ok ? (started.data || {}) : {};
      if (got.recording) {
        /* RELOAD ONLY IF THE FRAME IS NOT ALREADY ON THIS SESSION. The reload
           is how the session id reaches the game, but it is also a full
           restart — pressing record while already playing threw away the run. */
        patch({ recording: true, nativeGame: target === "native" });
        if (target === "web") {
          const f = m.current.frame;
          if (!f || !f.includes(`bgate_session=${got.session_id}`)) bootFrame(got.session_id ?? null);
        }
        patch({ msg: target === "native"
          ? `RECORDING - native Godot launched (pid ${got.native_launch?.pid || "?"})`
          : "RECORDING - play and say what you like and what needs fixing" });
      } else {
        const why = started.ok ? (got.error || "the recorder did not start") : started.error;
        patch({ msg: "start failed: " + why });
        if (started.ok) toast("recording did not start - " + why);
        void ptPreflight(true);   // whatever broke, re-check the gear and say what
      }
    } else {
      const stopped = await mutate<{ ok?: boolean; session_id?: number; error?: string }>(
        "/api/playtest/stop", { quiet: true });
      const got = stopped.ok ? (stopped.data || {}) : {};
      patch({ recording: false });
      const fine = stopped.ok && got.ok !== false;
      if (fine && m.current.target === "web") bootFrame();
      const why = stopped.ok ? (got.error || "?") : stopped.error;
      patch({ msg: fine
        ? `session ${got.session_id} transcribing - a director triage item will land in the queue`
        : "stop failed: " + why });
      if (!fine) toast("stop failed - " + why);
    }
  }

  /* Start or stop the BUILD. Never touches the recording. */
  async function toggleBuild() {
    if (m.current.busyBuild) return;
    const on = !!m.current.frame || m.current.nativeGame;
    patch({ busyBuild: true });
    try {
      if (on) await stopBuild();
      else await loadGame();
    } finally {
      patch({ busyBuild: false });
    }
  }

  /* Close the build. Refuses to quietly end a recording that is using it. */
  async function stopBuild() {
    if (m.current.recording) {
      const go = await askConfirm({
        title: "A recording is running on this build.",
        body: "Stopping the build now ends the recording too, and it is "
            + "discarded - no transcript, no triage.",
        ok: "stop anyway", danger: true });
      if (!go) return;
    }
    const r = await mutate<{ sessions_stopped?: unknown[] }>("/api/playtest/abort", { quiet: true });
    const d = r.ok ? (r.data || {}) : {};
    patch({ recording: false, nativeGame: false, aborting: true });
    clearFrame();
    patch({ msg: !r.ok ? "could not stop the build - " + r.error
      : d.sessions_stopped?.length ? "build stopped - recording discarded" : "build stopped" });
    if (!r.ok) toast("stop build failed - " + r.error);
    void ptPreflight(true);
  }

  /* ── render ───────────────────────────────────────────────────────────── */
  const s = m.current;
  const buildOn = !!s.frame || s.nativeGame;
  /* ONE PLACE DECIDES WHAT BOTH BUTTONS SAY, so no path can leave a button
     lying about what it would do. Record is disabled until the preflight says
     the gear is there — except while a session is running, when stopping
     must always be possible. */
  const buildLabel = s.busyBuild ? (buildOn ? "stopping…" : "starting…")
    : buildOn ? "■ stop build" : "▶ start build";
  const recLabel = s.busyRec ? (s.recording ? "stopping…" : "starting…")
    : s.recording ? "■ stop recording" : "● start recording";

  /* The controls hint: the real bindings live in the game's own project.godot
     [input] section, so this renders whatever the backend reports and
     otherwise promises nothing. */
  const controlsLine = Array.isArray(controls) && controls.length
    ? controls.map((c) => typeof c === "string" ? c
        : `${(((c as { keys?: string[] }).keys || []).join("/")) || "-"} ${String((c as { action?: string }).action || "").replace(/_/g, " ")}`)
        .join(" · ")
    : "";

  return (
    <div className="panel playpanel" style={{ marginBottom: 20 }}>
      <div className="ptbar">
        <span className={s.lamp} id="pt-lamp" />
        <span id="pt-msg">{s.msg}</span>
        <input className="pt-goal" id="pt-goal" placeholder="Iteration goal" maxLength={160}
               value={s.goal} onChange={(e) => patch({ goal: e.target.value })} />
        <select className="pt-target" id="pt-target" aria-label="Playtest target" value={s.target}
                onChange={(e) => { patch({ target: e.target.value as Model["target"] }); void ptPreflight(true); }}>
          <option value="web">web build</option>
          <option value="native">native Godot</option>
        </select>
        <span className="pt-controls">
          <button className={`qbtn small${buildOn ? " pt-on" : ""}`} id="pt-build" type="button"
                  disabled={s.busyBuild} onClick={() => void toggleBuild()}
                  title={buildOn ? "Close the running build" : "Boot the current build in this panel"}>
            {buildLabel}
          </button>
          <button className={`qbtn small${s.recording ? " recording" : ""}`} id="pt-btn" type="button"
                  disabled={s.busyRec || (!s.ready && !s.recording)} onClick={() => void toggleRecord()}
                  title={s.recording ? "End the session, then transcribe and triage it" : "Record a playtest session"}>
            {recLabel}
          </button>
        </span>
      </div>
      {/* What is actually missing, in full, with the button or the sentence
          that fixes it. What is deliberately absent: any terminal instruction. */}
      <div className="pt-why" id="pt-why" hidden={s.whyHidden || !s.why}>
        {s.why && s.why.length > 0 && (
          <ul className="pt-checks">{s.why.map(([name, c]) => {
            const tool = PT_INSTALLABLE[name];
            const optional = c.required === false;
            return (
              <li key={name}><b>{name}</b>{optional && <> <span className="r">optional</span></>}
                <span className="r">{optional ? (c.costs || c.reason || "") : (c.reason || "unavailable")}</span>
                {tool ? (
                  <button className="qbtn small" type="button" disabled={Boolean(s.installing[tool])}
                          onClick={() => void ptInstall(tool)}>
                    {s.installing[tool] || `Install${c.size_mb ? ` (${c.size_mb} MB)` : ""}`}
                  </button>
                ) : ptFix(name) ? <span className="fix">→ {ptFix(name)}</span> : null}
              </li>
            );
          })}</ul>
        )}
      </div>
      <div id="play-holder">
        {s.frame ? (
          /* The id is load-bearing: app.css sizes #gameframe, and the notepad
             finds the canvas through it. Keyed on the src so a new boot is a
             new document and a re-render is not. */
          <iframe key={s.frame} id="gameframe" src={s.frame} allow="autoplay; fullscreen"
                  onLoad={() => window.dispatchEvent(new CustomEvent(GAMEFRAME_EVENT))} />
        ) : s.holder.kind === "rebuilding" ? <>
          <div className="playbtn" style={{ cursor: "default" }}>rebuilding current build…</div>
          <div className="playhint">exporting the game from latest source - a few seconds</div>
        </> : s.holder.kind === "failed" ? <>
          <div className="playhint" style={{ color: "var(--ember)" }}>rebuild failed: {s.holder.error}</div>
          <button className="playbtn" type="button" onClick={() => bootFrame()}>boot anyway (stale)</button>
        </> : <>
          <button className="playbtn" type="button" onClick={() => void loadGame()}>▶ boot current build</button>
          {/* WHETHER THE BUILD IS CURRENT, SAID RATHER THAN ASSUMED. */}
          <div className="playhint" id="play-fresh">{s.fresh}</div>
          <div className="playhint" id="play-controls">
            {controlsLine
              ? `${controlsLine} · F1 opens live tuning`
              : "Controls come from your game's own input map - F1 opens live tuning over the running build."}
          </div>
        </>}
      </div>
    </div>
  );
}
