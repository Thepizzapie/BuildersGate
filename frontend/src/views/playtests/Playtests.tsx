import { useCallback, useRef, useState } from "react";
import { Icon } from "../../components/Icon";
import { mutate } from "../../bridge";
import { useViewActive } from "../../hooks";
import { useAppState } from "../../store";
import { PlayPanel } from "./PlayPanel";
import { Notepad } from "./Notepad";
import { Review } from "./Review";
import type { SessionRow, Status } from "./api";
import "./playtests.css";

/* The Playtests deck — evidence.
 *
 * Play the current build and record a session, then open a recording for
 * video, transcript, telemetry and the director's triage. Four surfaces:
 *
 *   · PLAY & RECORD (PlayPanel) — the build in a panel and the recorder
 *     beside it. Lives here, next to the recordings it produces; on Overview
 *     the control that creates the evidence sat in a different view from the
 *     evidence.
 *   · RECORDED SESSIONS — the list, from the shell's /api/state poll via the
 *     store, so this deck never reads the most expensive endpoint twice.
 *   · THE REVIEW (Review) — one session, in an overlay over the whole page.
 *   · THE NOTEPAD (Notepad) — typed evidence on the recorder's clock, in a
 *     drawer that stays reachable from every side of the game iframe.
 *
 * The status poll is owned once, in PlayPanel, and reported up: the notepad
 * and the panel used to each ask /api/playtest/status on their own clocks. */

export default function Playtests() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const { sessions, controls } = useAppState();
  const [status, setStatus] = useState<Status>({ recording: null, processing: [] });
  const [reviewId, setReviewId] = useState<number | null>(null);
  const refresh = useRef<() => Promise<void>>(async () => {});
  const register = useCallback((fn: () => Promise<void>) => { refresh.current = fn; }, []);
  const refreshStatus = useCallback(() => refresh.current(), []);

  const rows = sessions as SessionRow[];

  return (
    <div ref={host}>
      <div className="view-heading">
        <div><span className="eyebrow">Evidence</span><h2>Playtests</h2></div>
        <p>Play the current build and record a session - then open a recording for video,
          transcript, telemetry, and the director's triage.</p>
      </div>

      <PlayPanel active={active} controls={controls} onStatus={setStatus} register={register} />

      <div className="spanel k-read">
        <div className="sec-h"><Icon name="playtests" size={15} />
          <h3 className="sec-t">Recorded sessions</h3></div>
        <div className="compact-list" id="sessions">
          {rows.length ? rows.map((s) => {
            const stalled = s.status === "failed"
              || (s.status === "processing" && s.processing_worker === "stalled");
            return (
              <div key={s.id} className="sess" onClick={() => setReviewId(s.id)}>
                <span className="nm">{s.name}</span>
                <span>{s.duration_s ? `${s.duration_s.toFixed(0)}s` : ""}</span>
                <span className="st">{s.processing_stage || s.status}</span>
                {stalled && s.audio_path && (
                  <button className="qbtn small ghost" type="button"
                    onClick={(e) => { e.stopPropagation(); void retrySession(s.id, refreshStatus); }}>
                    retry
                  </button>
                )}
              </div>
            );
          }) : <div className="empty">no playtests yet - playtest_check, then playtest_start</div>}
        </div>
      </div>

      {reviewId != null && <Review id={reviewId} close={() => setReviewId(null)} />}
      <Notepad recording={status.recording} viewActive={active} refreshStatus={refreshStatus} />
    </div>
  );
}

async function retrySession(id: number, refreshStatus: () => Promise<void>) {
  const r = await mutate(`/api/playtest/${id}/retry`, { ok: `session ${id} re-queued` });
  if (!r.ok) return;
  window.pollState?.();
  await refreshStatus();
}
