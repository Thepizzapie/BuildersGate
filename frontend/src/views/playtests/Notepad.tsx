import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../../components/Icon";
import { mutate, previewURL, readJSON, toast } from "../../bridge";
import { useEvents } from "../../hooks";
import { KINDS, sessionClock, type Note, type Recording } from "./api";

/* The playtest notepad, and the frame grab that goes with it.
 *
 * A playtest has only ever captured what you SAY. That is the right default —
 * talking costs no hands while you are playing — but it is the only channel
 * there is, and it fails in ways that are not rare: a shared room, a call, a
 * dead mic, and above all anything with a number or a proper noun in it.
 * Whisper does not hear "armour 4 should be 40". Those observations used to
 * leave the session entirely.
 *
 * What lands here is NOT a parallel note store. Every note is POSTed to
 * /api/playtest/<id>/notes and written as a transcript segment plus a feedback
 * item — the same pair a spoken remark produces — so it appears in the
 * transcript at the right second and flows through triage, promote, merge,
 * dismiss and the bug report with nothing downstream adapted.
 *
 * ── TWO THINGS WERE MEASURED BEFORE THIS WAS DESIGNED ──────────────────────
 *
 * 1. A PARENT-DOCUMENT HOTKEY CANNOT WORK WHILE YOU ARE PLAYING. With the game
 *    iframe focused, a real F2 keypress fired the IFRAME's listener and not the
 *    parent's — 1 hit inside, 0 outside. So the hotkey is installed INTO the
 *    build's document (same origin: /play/ is served by the dashboard), in the
 *    capture phase, where it is also swallowed before Godot's own input handler
 *    can see it. The parent keeps its own listener for when focus is in the
 *    dashboard rather than the game; between them the key works from either
 *    side, and neither one can double-fire, because the event only ever
 *    exists in one of the two documents.
 *
 * 2. THE CANVAS READS BACK REAL PIXELS. Godot 4's web export creates its WebGL2
 *    context with preserveDrawingBuffer:true (verified on the live build), so
 *    canvas.toDataURL() from the parent returns the frame rather than a blank.
 *    That is why the frame is grabbed LIVE instead of extracted from the
 *    recording: it needs no recording to be finalised and no ffmpeg seek, and
 *    it is the only route that works at all before the mp4's moov atom is
 *    written at stop. Notes against a NATIVE Godot window have no canvas in
 *    this page and get their frame backfilled server-side when the session
 *    stops (playtest._backfill_note_frames).
 *
 * ── WHY THE PAD ANCHORS ITSELF ON OPEN ─────────────────────────────────────
 * You see something at 0:42, hit the hotkey, and type for fifteen seconds. If
 * the note were stamped when you pressed save it would sit fifteen seconds
 * downstream of the thing it describes. So opening the pad freezes BOTH the
 * timestamp and the frame at that instant, and the note carries them however
 * long the typing takes.
 *
 * Mounted through a portal on <body>: the drawer is fixed to the viewport and
 * the tab has to outlive a deck switch while a note is half-typed. */

export const HOTKEY = "F2";
const POLL_MS = 4000;
const SEATS = ["director", "narrative", "gameplay", "tech", "art", "audio",
               "cinematic", "qa", "unassigned"];
/* Downscale only what is genuinely huge. A 4K canvas as lossless PNG is ~8 MB,
   which is a slow POST and a slow write for no extra evidence; at or under
   this width the frame is sent untouched, so a pixel-art bug is still
   inspectable pixel for pixel. */
const MAX_FRAME_W = 1920;
/* One physical press must produce one toggle. Two listeners exist because the
   key can arrive in either document, and normally exactly one of them sees
   it — but that depends on where focus is at the instant of the press, and a
   toggle that fires twice opens and shuts the pad so fast it looks dead.
   Observed once while testing. 60ms is far under any human double-tap. */
const SAME_PRESS_MS = 60;
/** Fired by the play panel when a build boots, so the hotkey can be re-armed
 *  inside the fresh document without waiting for the next tick. */
export const GAMEFRAME_EVENT = "bgate:gameframe";

/* ── the running build, and what can be read out of it ────────────────── */
const gameFrame = () => document.getElementById("gameframe") as HTMLIFrameElement | null;

function gameCanvas(): HTMLCanvasElement | null {
  const f = gameFrame();
  if (!f) return null;
  // Same origin (/play/ is this dashboard), so this never throws in practice.
  // Guarded anyway: an iframe mid-navigation has no document at all.
  try { return f.contentDocument?.querySelector("canvas") || null; }
  catch { return null; }
}

function focusGame() {
  const c = gameCanvas();
  const f = gameFrame();
  try { (c || f || document.body).focus(); } catch { /* nothing to focus */ }
}

/** Grab the frame. Never throws, and always explains itself, because a
 *  capture button that quietly produces nothing is worse than one that is
 *  honestly unavailable. */
export function grabFrame(): { data: string; why: string } {
  if (!gameFrame()) {
    return { data: "", why: "no build is running in the dashboard. Boot the "
                          + "current build to attach frames - or write the "
                          + "note without one." };
  }
  const canvas = gameCanvas();
  if (!canvas || !canvas.width || !canvas.height) {
    return { data: "", why: "the build has not put a canvas on screen yet." };
  }
  try {
    let source: HTMLCanvasElement = canvas;
    if (canvas.width > MAX_FRAME_W) {
      const scaled = document.createElement("canvas");
      scaled.width = MAX_FRAME_W;
      scaled.height = Math.round(canvas.height * (MAX_FRAME_W / canvas.width));
      scaled.getContext("2d")?.drawImage(canvas, 0, 0, scaled.width, scaled.height);
      source = scaled;
    }
    const url = source.toDataURL("image/png");
    // A blank readback still encodes to a valid, tiny PNG. Length is the cheap
    // tell that we got a picture rather than an empty buffer.
    if (!url || url.length < 1024) {
      return { data: "", why: "the canvas read back empty - the build may be "
                            + "between frames." };
    }
    return { data: url, why: "" };
  } catch (err) {
    return { data: "", why: "the browser refused to read the canvas: "
                          + ((err as Error)?.message || String(err)) };
  }
}

type Props = {
  /** The live recording, from the deck's status poll. Null when nothing is. */
  recording: Recording | null;
  /** Is the Playtests deck on screen. The tab hides elsewhere. */
  viewActive: boolean;
  /** Ask the deck for a fresh status read — the pad wants the session it
   *  belongs to the instant it opens, not on the next tick. */
  refreshStatus: () => void | Promise<void>;
};

export function Notepad({ recording, viewActive, refreshStatus }: Props) {
  const [open, setOpen] = useState(false);
  const [anchorTs, setAnchorTs] = useState(0);        // epoch seconds; 0 until armed
  const [frameData, setFrameData] = useState("");
  const [frameWhy, setFrameWhy] = useState("");
  const [notes, setNotes] = useState<Note[]>([]);
  const [draft, setDraft] = useState("");
  const [kind, setKind] = useState("");
  const [seat, setSeat] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);

  /* The listeners below are installed once and read the latest state through
     refs; re-registering the capture-phase hotkey on every render is how a
     press ends up handled by two generations of the same handler. */
  const openRef = useRef(open); openRef.current = open;
  const activeRef = useRef(viewActive); activeRef.current = viewActive;
  const sessionRef = useRef(recording); sessionRef.current = recording;
  const lastHotkey = useRef(0);

  const session = recording;
  const anchorClock = anchorTs ? sessionClock(anchorTs, session?.started_epoch) : "";

  /* Freeze the moment this note is ABOUT: the timestamp and the frame, both
     taken now, both carried through however long the typing runs. */
  const armNote = useCallback(() => {
    setAnchorTs(Date.now() / 1000);
    const shot = grabFrame();
    setFrameData(shot.data);
    setFrameWhy(shot.why);
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    setAnchorTs(0);
    setFrameData("");
    setFrameWhy("");
    // Hand the keyboard back, or the next WASD goes into a panel nobody can see.
    focusGame();
  }, []);

  const doOpen = useCallback(() => {
    // ANCHOR BEFORE ANY AWAIT. The frame and the timestamp have to be the
    // instant the key went down; waiting for a status round trip would hand
    // every note whatever the game had drawn by the time it came back.
    armNote();
    setOpen(true);
    // Now find out which session it belongs to. anchorTs is already fixed;
    // the only thing this can change is the mm:ss LABEL.
    void refreshStatus();
  }, [armNote, refreshStatus]);

  const toggle = useCallback(() => {
    if (openRef.current) close(); else doOpen();
  }, [close, doOpen]);

  /* ── the hotkey, on both sides of the iframe boundary ─────────────────── */
  const onHotkey = useCallback((event: KeyboardEvent) => {
    if (event.key !== HOTKEY) return;
    // Capture phase inside the build: swallow it so a project that binds F2
    // does not fire that action every time somebody reaches for the notepad.
    event.preventDefault();
    event.stopImmediatePropagation();
    const now = Date.now();
    if (now - lastHotkey.current < SAME_PRESS_MS) return;
    lastHotkey.current = now;
    toggle();
  }, [toggle]);

  /* The inner window is replaced on every navigation and its listeners go
     with it, so the flag lives ON that window: a fresh one has no flag and is
     re-armed on the next tick or the next boot. */
  const armGame = useCallback(() => {
    const f = gameFrame();
    if (!f) return;
    let win: (Window & { __bgNotesArmed?: boolean }) | null;
    try { win = f.contentWindow as typeof win; } catch { return; }
    if (!win || !win.document || win.__bgNotesArmed) return;
    win.__bgNotesArmed = true;
    win.addEventListener("keydown", onHotkey, true);
  }, [onHotkey]);

  useEffect(() => {
    // The parent half of the hotkey. It does not fire while the game has
    // focus (measured — see the header), which is exactly why armGame()
    // exists; this covers the other case, when focus is in the dashboard.
    const onKey = (event: KeyboardEvent) => {
      // Escape closes from anywhere while the pad is open — not only with
      // focus in the compose box, or anyone who clicked the frame or the
      // page behind it has no keyboard way out.
      if (event.key === "Escape" && openRef.current
          && document.activeElement !== gameFrame()) {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== HOTKEY) return;
      if (!activeRef.current && !openRef.current) return;
      // The build owns the keyboard right now, so the listener installed
      // INSIDE it owns this key. Handling it here as well is how one press
      // became two toggles.
      if (document.activeElement === gameFrame()) return;
      onHotkey(event);
    };
    document.addEventListener("keydown", onKey, true);
    window.addEventListener(GAMEFRAME_EVENT, armGame);
    armGame();
    return () => {
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener(GAMEFRAME_EVENT, armGame);
    };
  }, [onHotkey, armGame, close]);

  /* ── the saved notes ──────────────────────────────────────────────────── */
  const sid = session?.id ?? 0;
  const refreshNotes = useCallback(async () => {
    armGame();
    if (!sid) return;
    const got = await readJSON<{ notes?: Note[] }>(`/api/playtest/${sid}/notes`, {});
    if (!got.__error) setNotes(got.notes || []);
  }, [sid, armGame]);
  /* The bus says when a recording starts or stops; the notes list itself
     grows between events (a viewer in chat, this pad), so the tick stays
     fast only while one is live — and only while somebody can see it. */
  useEvents(refreshNotes, {
    kinds: ["playtest.*"], key: sid, fallbackMs: POLL_MS,
    enabled: sid > 0 && (viewActive || open),
  });
  useEffect(() => { if (!sid) setNotes([]); }, [sid]);

  /* The shell reserves the drawer's width while it is open (shell.css). */
  useEffect(() => {
    document.body.classList.toggle("ptn-open", open);
    return () => { document.body.classList.remove("ptn-open"); };
  }, [open]);
  useEffect(() => { if (open) area.current?.focus(); }, [open, sid]);

  /* ── actions ──────────────────────────────────────────────────────────── */
  async function save() {
    const text = draft.trim();
    if (!text) { toast("nothing to save - type the note first"); return; }
    const live = sessionRef.current;
    if (!live) { toast("no recording is running - a note needs a session clock"); return; }
    const payload: Record<string, unknown> = { text, ts: anchorTs || Date.now() / 1000 };
    if (kind) payload.kind = kind;
    if (seat) payload.seat = seat;
    if (frameData) payload.frame = frameData;
    // Quiet: the failure sentence is this pad's, and the words stay in the
    // box. Losing a note to a dropped request would be the one unrecoverable
    // failure in this feature.
    const r = await mutate<Note>(`/api/playtest/${live.id}/notes`, { body: payload, quiet: true });
    if (!r.ok) {
      toast(r.code === "unreachable"
        ? "note not saved - the dashboard is unreachable. Your text is still here."
        : "note not saved - " + r.error);
      return;
    }
    const saved = r.data || { text };
    if (saved.frame_error) toast("note saved, but the frame was rejected - " + saved.frame_error);
    setNotes((n) => [...n, saved]);
    setDraft("");
    // Re-arm: the NEXT note is about a new moment, and it should carry that
    // moment's timestamp and frame rather than this one's.
    armNote();
    area.current?.focus();
  }

  function regrab() {
    // Re-grabbing means "THIS is the moment", so the stamp moves with it.
    armNote();
    area.current?.focus();
  }

  function onComposeKey(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Escape") { event.preventDefault(); close(); return; }
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void save(); }
  }

  /* ── render ───────────────────────────────────────────────────────────── */
  const guests = notes.filter((n) => n.mine === false).length;
  const mine = notes.length - guests;
  /* Counted separately because "12 notes" over a list where nine are
     strangers' is a number that means something different from what it
     looks like. */
  const label = guests ? `${mine} yours · ${guests} from chat` : `${notes.length} typed`;

  return createPortal(
    <div id="ptnotes-host">
      <button
        type="button"
        className={`ptn-tab${session ? " rec" : ""}`}
        hidden={!viewActive}
        onClick={toggle}
        title={session
          ? `Notepad - recording session ${session.id}. ${HOTKEY} from anywhere, `
            + `including while the game has focus.`
          : `Notepad - press ${HOTKEY}. Notes need a running recording to land on.`}
      >
        <Icon name="note" size={15} /><span className="lb">notes</span>
        <span className="kb">{HOTKEY}</span>
      </button>

      <aside className={`ptn-pad${open ? " on" : ""}`} aria-hidden={!open}
             aria-label="Playtest notepad">
        <div className="ptn-head">
          <Icon name="note" size={15} /><h4>notepad</h4>
          <button className="ptn-x" type="button" aria-label="Close notepad" onClick={close}>
            &#10005;
          </button>
        </div>
        <div className="ptn-body">
          {!session ? (
            /* No session, no note. A note's timestamp is SECONDS FROM SESSION
               START, and with nothing recording there is no zero to measure
               from — say that rather than showing a box that throws the
               words away. */
            <>
              <div className="ptn-warn"><b>Nothing is recording.</b><br />
                A note is stamped in seconds from the start of a session — that is what
                lines it up with the video, the transcript and the telemetry. With no
                session running there is no clock to stamp it against, so the notepad
                stays shut. Hit <b>● record</b> above, then press
                <kbd>{HOTKEY}</kbd> again.</div>
              <div className="ptn-hint">Frames come straight off the running build's
                canvas, so booting the build first means every note can carry one.</div>
            </>
          ) : (
            <>
              <div className="ptn-when">
                <span className="ptn-chip at">{anchorClock || "--:--"}</span>
                <span className="ptn-chip">session {session.id}</span>
                <span className="ptn-hint">stamped when the pad opened, not when you save</span>
              </div>
              <div className="ptn-shot">
                {frameData
                  ? <img src={frameData} alt="frame captured with this note" />
                  : <div className="none">{frameWhy || "no frame attached."}</div>}
                <div className="ptn-shotbar">
                  <button className="ptn-btn" type="button" onClick={regrab}>
                    <Icon name="export_image" /> grab frame now
                  </button>
                  {frameData && (
                    <button className="ptn-btn" type="button"
                            onClick={() => { setFrameData(""); setFrameWhy("frame dropped."); }}>
                      <Icon name="delete" /> drop
                    </button>
                  )}
                </div>
              </div>
              <textarea
                ref={area}
                className="ptn-ta"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={onComposeKey}
                placeholder={`What just happened? Type it - this lands in the transcript at ${anchorClock || "this moment"}.`}
              />
              <div className="ptn-row">
                <select className="ptn-kind" aria-label="Kind" value={kind}
                        onChange={(e) => setKind(e.target.value)}>
                  <option value="">kind · auto</option>
                  {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
                </select>
                <select className="ptn-seat" aria-label="Seat" value={seat}
                        onChange={(e) => setSeat(e.target.value)}>
                  <option value="">seat · auto</option>
                  {SEATS.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <div className="ptn-row">
                <button className="ptn-btn go" type="button" onClick={() => void save()}>
                  <Icon name="note" /> save note
                </button>
                <span className="ptn-hint"><kbd>Enter</kbd> saves · <kbd>Shift</kbd>+<kbd>Enter</kbd>
                  {" "}newline · <kbd>Esc</kbd> back to the game</span>
              </div>
              <div className="ptn-sec">this session · {label}</div>
              <div className="ptn-list">
                {notes.length ? notes.slice().reverse().map((note, i) => {
                  /* `mine` is computed server-side (an empty author means the
                     project owner), so this only has to render the difference. */
                  const guest = note.mine === false;
                  return (
                    <div key={note.id ?? `n${i}`} className={`ptn-item${guest ? " guest" : ""}`}>
                      {note.frame_rel && <img src={previewURL(note.frame_rel)} alt="" />}
                      <div className="tx">
                        {guest && <span className="who">{note.author || "viewer"}</span>}
                        {note.text}
                        <div className="mt">
                          <span className="tm">{note.clock || ""}</span>
                          <span className="ptn-chip">{note.kind}</span>
                          <span className="ptn-chip">{note.seat}</span>
                          {guest && <span className="ptn-chip guest">from chat</span>}
                        </div>
                      </div>
                    </div>
                  );
                }) : <div className="ptn-empty">Nothing written yet this session.</div>}
              </div>
            </>
          )}
        </div>
      </aside>
    </div>,
    document.body,
  );
}
