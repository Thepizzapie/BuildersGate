import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { mutate, readJSON, toast } from "../../bridge";
import { Triage } from "./Triage";
import { clock, heard, type Check, type Review as ReviewData } from "./api";

/* The review overlay for one recorded session: the video, the director's
 * triage directly under it, what the telemetry captured, and the transcript.
 *
 * TRIAGE SITS DIRECTLY UNDER THE RECORDING, above the telemetry readout.
 * Deciding what to do with the feedback is the reason this overlay opens; it
 * used to be the fifth thing down, under two summaries, a hash dump and a
 * settings log.
 *
 * THE VIDEO SURVIVES A TRIAGE ACTION. A promote or dismiss refetches the
 * session and re-renders this tree, but the <video> element is the same node
 * with the same src, so React leaves it alone: the recording keeps playing at
 * the second you left it, and the scroll position stays put.
 *
 * Session time vs video time. The capture starts a beat after the session
 * does, so every timestamp the backend stores (transcript, markers, telemetry)
 * is in SESSION time while the video's own clock starts at zero. playtest.py
 * already subtracts video_offset_s when it extracts a frame; seeking has to do
 * the same or the human watches a moment several seconds off from the one the
 * agent was handed. Reads 0 when the backend omits the field, and behaves
 * exactly as before, never worse.
 *
 * Mounted through a portal on <body>: the overlay is fixed over the whole
 * page and outlives the deck it was opened from. */

const fmtV = (v: unknown): string =>
  typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v);

type Props = { id: number; close: () => void };

export function Review({ id, close }: Props) {
  const [d, setD] = useState<ReviewData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeLine, setActiveLine] = useState(-1);
  const [stuck, setStuck] = useState(false);
  const [teleWhy, setTeleWhy] = useState<ReactNode>("checking why…");
  const videoEl = useRef<HTMLVideoElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const stage = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    const got = await readJSON<Partial<ReviewData>>(`/api/playtest/${id}`, {});
    if (got.__error || !got.session) {
      setError(got.__error || "no response");
      return;
    }
    setError(null);
    setD(got as ReviewData);
  }, [id]);

  useEffect(() => {
    setD(null); setError(null); setActiveLine(-1); setTeleWhy("checking why…");
    void load();
  }, [load]);

  /* Refetch and repaint. Called by triage after every mutation; the payload
     is owned here so the video above it is never rebuilt. */
  const reload = useCallback(async () => {
    const fresh = await readJSON<Partial<ReviewData>>(`/api/playtest/${id}`, {});
    if (!fresh.__error && fresh.items) setD(fresh as ReviewData);
  }, [id]);

  const activeReviewOffset =
    d ? (Number(d.session.video_offset_s ?? d.video_offset_s ?? 0) || 0) : 0;
  const toVideoTime = (t: unknown) => Math.max(0, (Number(t) || 0) - activeReviewOffset);

  function seekReview(t: number) {
    const video = videoEl.current;
    if (!video) return;
    video.currentTime = toVideoTime(t);
    video.play().catch(() => {});
  }

  function syncTranscript() {
    const video = videoEl.current;
    if (!video || !d) return;
    const at = video.currentTime + activeReviewOffset;   // back into session time
    const lines = d.transcript || [];
    const idx = lines.findIndex((s) => at >= Number(s.t_start) && at <= Number(s.t_end));
    setActiveLine(idx);
  }
  useEffect(() => {
    if (activeLine < 0) return;
    document.getElementById(`transcript-${activeLine}`)?.scrollIntoView({ block: "nearest" });
  }, [activeLine]);

  /* Shrink the pinned video once triage has scrolled under it.
     A sentinel and an IntersectionObserver never fired once inside this
     overlay (backdrop-filter makes .review-overlay a containing block and the
     observer's root clip lands elsewhere). So this measures the thing it
     actually wants to know: sticky has engaged exactly when the stage's top
     has reached the scroller's top — AGAINST THE PADDING BOX, because a sticky
     offset resolves against the scrollport and .review-body carries padding.
     rAF-throttled, and state is only touched when it changes. */
  useEffect(() => {
    const scroller = body.current;
    if (!scroller) return;
    let queued = false;
    const check = () => {
      queued = false;
      const live = stage.current;
      if (!live) return;
      const style = getComputedStyle(scroller);
      const port = scroller.getBoundingClientRect().top + (parseFloat(style.paddingTop) || 0);
      setStuck(live.getBoundingClientRect().top <= port - 1);
    };
    const onScroll = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(check);
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    check();
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [d]);

  /* WHY A RECORDING HAS NO TELEMETRY, and the one-press fix when there is
     one. Asked only when a recording actually came back without events — the
     moment it costs somebody something. */
  const noTelemetry = Boolean(d && !Object.keys(d.telemetry?.by_kind || {}).length);
  useEffect(() => {
    if (!noTelemetry) return;
    let gone = false;
    (async () => {
      const pf = await readJSON<{ checks?: Record<string, Check> }>("/api/playtest/preflight", {});
      if (gone) return;
      const t = (pf.checks || {}).telemetry || {};
      if (t.ok) {
        setTeleWhy("the addon is installed - the game emitted nothing this session");
        return;
      }
      setTeleWhy(<>{t.reason || "the game has no telemetry autoload"}{" "}
        {t.installable && (
          <button className="qbtn small" type="button" onClick={(e) => void installTelemetry(e.currentTarget)}>
            Install the addon
          </button>
        )}</>);
    })();
    return () => { gone = true; };
  }, [noTelemetry, id]);

  async function installTelemetry(btn: HTMLButtonElement) {
    btn.disabled = true; btn.textContent = "installing…";
    const r = await mutate("/api/playtest/telemetry/install", { quiet: true });
    if (!r.ok) {
      toast("could not install the addon - " + r.error);
      btn.disabled = false; btn.textContent = "Install the addon";
      return;
    }
    /* SAYS WHAT IT DID AND WHAT IS STILL NEEDED. The autoload is registered,
       but Godot has to reopen the project before it runs. */
    setTeleWhy("addon installed - reopen the game in Godot, then record again and events will arrive");
    toast("telemetry addon installed", "ok");
  }

  /* ── render ───────────────────────────────────────────────────────────── */
  let content: ReactNode;
  if (error) {
    content = <div className="empty err">could not load session {id} - {error}</div>;
  } else if (!d) {
    content = <div className="empty">loading session {id}…</div>;
  } else {
    const duration = Math.max(Number(d.session.duration_s || 0), 1);
    const T = d.telemetry || { by_kind: {}, settings: [], moments: [], fps: null, total: 0 };
    const snapshot = d.iteration;
    content = <>
      {d.coverage_warnings?.length ? (
        <div className="coverage-warnings">{d.coverage_warnings.map((w, i) =>
          <span key={i} className="coverage-warning">{w.message}</span>)}</div>
      ) : null}
      <div className="review-summary">
        <span>{d.session.status}</span><span>{Number(d.session.duration_s || 0).toFixed(1)}s</span>
        <span>{d.counts.items} feedback items</span><span>build {d.session.build_ref || "unknown"}</span>
        {d.telemetry_backed
          ? <span className="chip">{d.counts.events} telemetry events</span>
          : <span className="chip drift">no telemetry</span>}
      </div>
      {snapshot && (
        <div className="work-link">
          <b>iteration {snapshot.id}</b>
          <span>commit {(snapshot.source_commit || "unversioned").slice(0, 12)}</span>
          <span>dirty {(snapshot.dirty_fingerprint || "none").slice(0, 12)}</span>
          <span>source {(snapshot.source_fingerprint || "none").slice(0, 12)}</span>
          <span>export {(snapshot.export_hash || "missing").slice(0, 12)}</span>
          <span>{snapshot.active_artifact_ids.length} active assets</span>
          <span>tests {snapshot.tests?.status || "unknown"}</span>
          <span>telemetry schema v{snapshot.telemetry_schema_version}</span>
        </div>
      )}
      {d.has_video && (
        <div className={`video-stage${stuck ? " ptr-stuck" : ""}`} ref={stage}>
          {/* Byte-range served by app.py, so every marker and line below is a
              seek the transport can honour. */}
          <video ref={videoEl} className="review-video" id="review-video" controls
                 src={`/api/playtest/${id}/video`} onTimeUpdate={syncTranscript} />
          <div className="marker-track">{(d.timeline_markers || []).map((m, i) =>
            <button key={i} type="button" className="timeline-marker"
              style={{ left: `${Math.min(100, m.t / duration * 100)}%` }}
              title={`${m.kind} · ${m.text}`} onClick={() => seekReview(m.t)} />)}</div>
        </div>
      )}
      <Triage data={d} reload={reload} seek={seekReview} close={close} />
      <div className="tele-panel">
        <h2>What the recording captured</h2>
        <div className="tele-kinds">
          {Object.keys(T.by_kind).length
            ? Object.entries(T.by_kind).sort((a, b) => b[1] - a[1]).map(([k, n]) =>
              <span key={k} className="chip">{k} ×{n}</span>)
            /* THE BADGE NAMES ITS CAUSE, AND OFFERS THE FIX. The absence costs
               the user something exactly here, so here is where it says why. */
            : <><span className="chip drift">no telemetry - this session was audio only</span>
                <span className="tele-why" id="tele-why">{teleWhy}</span></>}
          {T.fps && <span className="chip">fps {T.fps.min}–{T.fps.max} · avg {T.fps.avg}</span>}
        </div>

        <h3>Tuning changes you made · {T.settings.length}</h3>
        {T.settings.length ? (
          <div className="tuning-list">{T.settings.map((s, i) => (
            <button key={i} type="button" className="tuning-row"
              onClick={() => seekReview(Number(s.t))} title={`jump to ${clock(s.t)} in the video`}>
              <span className="tuning-time">{clock(s.t)}</span>
              {s.group && <span className="chip">{s.group}</span>}
              <span className="tuning-key">{s.prop || s.key}</span>
              <span className="tuning-delta">{fmtV(s.from)} → <b>{fmtV(s.to)}</b></span>
              {(s.count || 0) > 1 && <span className="tuning-count">{s.count} nudges</span>}
            </button>))}</div>
        ) : <p className="empty" style={{ margin: 0 }}>No settings were changed during this session.</p>}

        {T.moments.length > 0 && <>
          <h3>Gameplay moments · {T.moments.length}</h3>
          <div className="moment-track">{T.moments.map((m, i) =>
            <button key={i} type="button" className={`moment-dot k-${m.kind}`}
              style={{ left: `${Math.min(100, m.t / duration * 100)}%` }}
              title={`${m.kind} @ ${clock(m.t)}`} onClick={() => seekReview(Number(m.t))} />)}</div>
        </>}
      </div>
      <div><h2 style={{ marginTop: 4 }}>Transcript</h2>
        <div className="transcript" id="review-transcript">
          {(d.transcript || []).length ? (d.transcript || []).map((s, index) => {
            // NOT "confidence -1.17": one translation in one place (api.heard)
            // so this chip and the triage tag can never drift apart.
            const h = heard(s.confidence);
            return (
              <div key={index} className={`transcript-line${activeLine === index ? " active" : ""}`}
                id={`transcript-${index}`} onClick={() => seekReview(Number(s.t_start))}>
                <span style={{ color: "var(--text-3)" }}>{Number(s.t_start).toFixed(1)}s</span> {s.text}
                {s.source === "typed" && <span className="chip">typed</span>}
                {h && <span className="chip" title={h.tip}>{h.word}</span>}
              </div>
            );
          }) : "no transcript"}
        </div>
      </div>
    </>;
  }

  return createPortal(
    <div className="review-overlay" id="review-overlay" style={{ display: "block" }}>
      <div className="review-shell">
        <div className="review-head">
          <h3 id="review-title">{d ? `Playtest ${id} - ${d.session.name}` : "Playtest review"}</h3>
          <button className="qbtn ghost" type="button" onClick={close}>close</button>
        </div>
        <div className="review-body" id="review-body" ref={body}>{content}</div>
      </div>
    </div>,
    document.body,
  );
}
