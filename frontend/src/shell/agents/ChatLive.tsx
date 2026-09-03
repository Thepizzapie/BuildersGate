import { useCallback, useEffect, useRef, useState } from "react";
import { Button, TextInput } from "@mantine/core";
import { Ti } from "../Ti";
import { toast } from "../../bridge";
import { useEvents } from "../../hooks";
import "./chatlive.css";

/* ChatLive.tsx — live stream chat in the dashboard, and feedback sessions.
 *
 * Builders Gate is a tool for building a game. This is the part that is for
 * building the AUDIENCE around it: the people watching the stream are the
 * first playtesters the project will ever have, and this is how anything they
 * say reaches the board.
 *
 * ── THE THING TO UNDERSTAND BEFORE CHANGING ANY OF THIS ────────────────────
 *
 * EVERY STRING IN HERE CAME FROM A STRANGER ON THE INTERNET. Message text,
 * display names, the channel's own metadata. It is rendered into this
 * document, which is the same document that holds the dashboard's auth token.
 * JSX escapes its children, and nothing here uses dangerouslySetInnerHTML —
 * keep it that way. The server sanitises too (bgate_core.chatlink.sanitise
 * runs at the socket, before storage); neither layer is the reason the other
 * can be skipped.
 *
 * IT IS ALSO A PROMPT-INJECTION SURFACE, AND THAT PART IS NOT SOLVED HERE.
 * A viewer typing "ignore previous instructions" is handled server-side, and
 * the only route from chat to a work item runs through a plan a human read
 * and confirmed. This file must never grow a button that shortens that. "Stop
 * and dispatch" is not a feature; "stop, and here is the room where you can
 * read what it proposes" is, and that is what STOP does.
 *
 * ── THE PANEL, IN THREE STATES ─────────────────────────────────────────────
 *
 *   not configured   the setup card, the honest common case: nothing about
 *                    anyone's channel ships in this repository.
 *   connected        the log, plus ONE sentence saying where the words are
 *                    going: a feedback session, a playtest recording, or
 *                    nobody. Never left to be inferred.
 *   in between       connecting / reconnecting / error, each with the reason
 *                    visible.
 *
 * TWO RATES. No event kind describes a chat message, so this is one of the
 * panels that keeps a timer through useEvents' fallback: a live chat updated
 * every twelve seconds is not live, and a disconnected one polled every two
 * is a request per second for a state that changes once an hour. */

const POLL_LIVE_MS = 2500;
const POLL_IDLE_MS = 10000;
const MAX_ROWS = 200;

type Platform = {
  id?: string; label?: string; reason?: string; channel?: string; configured?: boolean;
  anonymous_limits?: string; channel_env?: string;
};
type ChatState = {
  connection?: { state?: string; state_label?: string; channel?: string; anonymous?: boolean; reason?: string };
  platforms?: Platform[];
  capture?: { owner?: string; why?: string };
  privacy?: { advise?: boolean; message?: string };
  feedback?: { session?: { id: number; prompt?: string; seen?: number;
    counts?: { total?: number; authors?: number; injection_attempts?: number } } | null };
  env_gitignored?: boolean;
};
type Msg = {
  seq: number; at?: number; author?: string; text?: string; mod?: boolean; first?: boolean;
  captured?: string; kind?: string; flags?: string[]; gap?: boolean;
};
type StopResult = { brainstorm_id?: number; counts?: { total?: number }; note?: string };

const jget = <T,>(url: string): Promise<{ data?: T } | null> =>
  fetch(url).then((r) => r.json()).catch(() => null);
const jpost = (url: string, body?: unknown) => fetch(url, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
}).then(async (r) => ({ ok: r.ok, body: await r.json().catch(() => null) as
  { data?: Record<string, unknown>; error?: { message?: string } } | null }))
  .catch(() => ({ ok: false, body: null }));
const errText = (res: { body: { error?: { message?: string } } | null }, fallback: string) =>
  res.body?.error?.message || fallback;

function dotClass(st: string) {
  if (st === "connected") return "on";
  if (st === "error" || st === "not_configured") return "bad";
  if (st === "connecting" || st === "reconnecting") return "warn";
  return "";
}

/* THIS LINK IS THE ONE THAT MATTERS. Stopping a feedback session is the
   moment the panel promises the most: "brainstorm #N is open with them in
   it". The real route is the one the director seat already uses: brainstorm
   picks its session from localStorage "bs-last-<seat>" on mount, the
   director seat picks its mode from "dir-mode" — so write both, then select
   the seat. If the workspace is mounted already, open() it directly. Every
   hop is guarded: a different part of the dashboard may not be on the page. */
function openRoom(id: number) {
  if (!id) return;
  try { localStorage.setItem("bs-last-director", String(id)); } catch { /* private mode */ }
  try { localStorage.setItem("dir-mode", "brainstorm"); } catch { /* private mode */ }
  try {
    // Only the DIRECTOR workspace. The narrative one would happily read a
    // director session by id and render it under a seat that may not file.
    const live = (window as unknown as { Brainstorm?: { active?: { seat?: string; open?(n: number): void } } })
      .Brainstorm?.active;
    if (live && live.seat === "director" && typeof live.open === "function") { live.open(id); return; }
  } catch { /* fall through to the navigation */ }
  try { window.setWorkspace?.("seats"); } catch { /* not on this page */ }
  try {
    (window as unknown as { SeatShell?: { select?(s: string): void } }).SeatShell?.select?.("director");
  } catch { /* the seat view is not on this page */ }
}

export function ChatLive() {
  const [state, setState] = useState<ChatState | null>(null);
  const [rows, setRows] = useState<Msg[]>([]);
  const cursor = useRef(0);
  const [busy, setBusy] = useState("");
  const [lastStop, setLastStop] = useState<StopResult | null>(null);
  const [channel, setChannel] = useState<string | null>(null);   // null: not yet typed in
  const [prompt, setPrompt] = useState("");
  const log = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  const conn = state?.connection || { state: "off" };
  const st = conn.state || "off";
  const connected = st === "connected";

  const poll = useCallback(async () => {
    const got = await jget<ChatState>("/api/chat");
    let next = state;
    if (got && got.data) { next = got.data; setState(got.data); }
    const live = next?.connection?.state === "connected";
    if (live) {
      const feed = await jget<{ missed?: boolean; messages?: Msg[]; seq?: number }>(
        `/api/chat/messages?since=${cursor.current}`);
      const data = feed && feed.data;
      if (data) {
        setRows((old) => {
          let out = old;
          if (data.missed && old.length) out = [...out, { seq: -1, gap: true }];
          if (data.messages?.length) out = [...out, ...data.messages];
          if (out.length > MAX_ROWS) out = out.slice(-MAX_ROWS);
          return out;
        });
        cursor.current = data.seq || cursor.current;
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.connection?.state]);
  useEvents(() => { void poll(); }, {
    kinds: ["chat.*"], fallbackMs: connected ? POLL_LIVE_MS : POLL_IDLE_MS,
  });

  /* Stick to the bottom only if the reader was already there — scrolling up
     to read something and being yanked back down is the classic chat bug. */
  useEffect(() => {
    const el = log.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [rows]);

  const reset = () => { cursor.current = 0; setRows([]); };

  const p0 = (state?.platforms || [])[0] || null;
  const configured = Boolean(p0 && p0.configured);

  const saveChannel = async () => {
    const name = (channel ?? p0?.channel ?? "").trim();
    if (!name) { toast("type your channel name first"); return; }
    setBusy("channel");
    const res = await jpost("/api/chat/config", { platform: p0 ? p0.id : "", channel: name });
    setBusy("");
    if (!res.ok) { toast(errText(res, "could not save the channel")); return; }
    await jpost("/api/chat/connect", {});
    reset();
    void poll();
  };
  const connect = async (on: boolean) => {
    setBusy("conn");
    const res = await jpost(on ? "/api/chat/connect" : "/api/chat/disconnect", {});
    setBusy("");
    if (!res.ok) toast(errText(res, "could not change the connection"));
    if (on) reset();
    void poll();
  };
  const startSession = async () => {
    setBusy("start");
    const res = await jpost("/api/chat/session", { prompt: prompt.trim() });
    setBusy("");
    if (!res.ok) { toast(errText(res, "could not start the session")); void poll(); return; }
    setLastStop(null);
    const note = res.body?.data?.announce_note;
    if (note) toast(String(note));
    void poll();
  };
  const stopSession = async () => {
    const open = state?.feedback?.session;
    if (!open) return;
    setBusy("stop");
    const res = await jpost(`/api/chat/session/${open.id}/stop`, {});
    setBusy("");
    if (!res.ok) { toast(errText(res, "could not stop the session")); void poll(); return; }
    setLastStop((res.body?.data as StopResult) || null);
    void poll();
  };

  const cap = state?.capture || {};
  const owner = cap.owner || "none";
  const capLabel = owner === "feedback_session" ? "feedback session"
    : owner === "playtest_notes" ? "playtest notes" : "nothing";
  const privacy = state?.privacy || {};
  const open = state?.feedback?.session;

  return (
    <div className="cl-wrap">
      <div className="cl-bar">
        <span className={`cl-dot ${dotClass(st)}`} />
        <span className="cl-state">{conn.state_label || st}</span>
        {conn.channel && <span className="cl-chip at">#{conn.channel}</span>}
        {conn.anonymous && connected && <span className="cl-chip">read-only</span>}
        {connected
          ? <Button size="compact-xs" variant="default" leftSection={<Ti name="player-stop" size={12} />}
                    onClick={() => void connect(false)}>disconnect</Button>
          : configured
            ? <Button size="compact-xs" leftSection={<Ti name="player-play" size={12} />}
                      disabled={!!busy} onClick={() => void connect(true)}>connect</Button>
            : null}
      </div>
      {conn.reason && <div className="cl-why">{conn.reason}</div>}

      {/* ONE SENTENCE, ALWAYS PRESENT while connected. The two capture
          mechanisms are separate features and only one can own chat at a
          time; which one is a thing the dev must be able to read. */}
      {connected && (
        <div className={`cl-cap${owner !== "none" ? " live" : ""}`}>
          capturing into <b>{capLabel}</b> — {cap.why || ""}
        </div>
      )}
      {/* A SUGGESTION, NOT AN ACTION. Turning the redaction filter on for
          them would silently change how the whole dashboard renders because
          a socket opened — so this is a sentence. */}
      {connected && privacy.advise && privacy.message && (
        <div className="cl-note"><Ti name="eye-off" size={12} /> {privacy.message}</div>
      )}

      {lastStop && (lastStop.brainstorm_id
        ? (
          /* Both of the things the human asked for are the two buttons in
             that room, and both go through the same confirm gate. */
          <div className="cl-ok">
            <Ti name="user-star" size={12} /> Closed with <b>{(lastStop.counts || {}).total || 0}</b> note(s), and{" "}
            <a href="#" onClick={(e) => { e.preventDefault(); openRoom(lastStop.brainstorm_id!); }}>
              brainstorm #{lastStop.brainstorm_id}
            </a>{" "}
            is open with them in it. Talk it through there, or press Synthesize for a
            proposed plan — it writes nothing until you confirm it. <b>Nothing has been queued.</b>
          </div>
        ) : (
          <div className="cl-note">
            Session closed with {(lastStop.counts || {}).total || 0} note(s). {lastStop.note || "No brainstorm was opened."}
          </div>
        ))}

      {!configured ? (
        !p0 ? <div className="cl-card"><p>No chat platform is registered.</p></div> : (
          /* THE INSTRUCTIVE PATH. The state a fresh clone is always in, so it
             is worth being the best-written screen in the panel. It says the
             one thing people do not expect: no account, no token. */
          <div className="cl-card">
            <h5><Ti name="list" size={12} /> connect {p0.label}</h5>
            <p>{p0.reason || "Not configured yet."}</p>
            <div className="cl-bar">
              <TextInput className="cl-in" size="xs" placeholder="your channel name" spellCheck={false}
                         autoComplete="off" aria-label="Channel name"
                         value={channel ?? p0.channel ?? ""}
                         onChange={(e) => setChannel(e.currentTarget.value)}
                         onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void saveChannel(); } }} />
              <Button size="xs" leftSection={<Ti name="player-play" size={12} />} disabled={!!busy}
                      onClick={() => void saveChannel()}>save &amp; connect</Button>
            </div>
            <p><b>No account and no token are needed to read chat.</b> Builders Gate joins
              anonymously — {p0.anonymous_limits || ""}. A token only adds the ability to post,
              and it goes in Settings → Providers, never in a file you commit.</p>
            <p>The channel is written to <code>{p0.channel_env}</code> in this project's{" "}
              <code>.env</code>, which is gitignored
              {state?.env_gitignored === false
                ? <> - <b>except it is not, in this project. Fix that before saving.</b></> : null}.
              Nothing about your channel is stored in the Builders Gate repository.</p>
          </div>
        )
      ) : open ? (
        <div className="cl-card">
          <h5><Ti name="circle-dot" size={12} /> feedback session open</h5>
          {open.prompt && <p>Asked: {open.prompt}</p>}
          <div className="cl-tally">
            <span><b>{open.counts?.total || 0}</b> kept</span>
            <span><b>{open.counts?.authors || 0}</b> viewers</span>
            <span><b>{open.seen || 0}</b> seen</span>
            {(open.counts?.injection_attempts || 0) > 0 && (
              <span className="cl-chip bad">{open.counts?.injection_attempts} filtered</span>
            )}
          </div>
          <div className="cl-bar">
            <Button size="xs" color="red" variant="light" leftSection={<Ti name="player-stop" size={12} />}
                    disabled={!!busy} onClick={() => void stopSession()}>stop &amp; synthesise</Button>
            <span className="cl-why">Stop closes the window and opens a director brainstorm
              with what chat said. It queues nothing.</span>
          </div>
        </div>
      ) : owner === "playtest_notes" ? (
        /* Refused, and the refusal is shown BEFORE it is pressed rather than
           as an error afterwards. Chat is already being captured elsewhere. */
        <div className="cl-card">
          <h5><Ti name="note" size={12} /> chat is on the recording</h5>
          <p>A playtest is recording, so what chat says is landing as notes on <b>that</b> session
            — timestamped, with a frame, in the notepad alongside your own. A feedback session
            would capture the same messages twice, so it is unavailable until the recording stops.</p>
        </div>
      ) : (
        <div className="cl-card">
          <h5><Ti name="note" size={12} /> start a feedback session</h5>
          <div className="cl-bar">
            <TextInput className="cl-in" size="xs" spellCheck={false} aria-label="What to ask chat"
                       placeholder="what are you asking chat about?" value={prompt}
                       onChange={(e) => setPrompt(e.currentTarget.value)}
                       onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void startSession(); } }} />
            <Button size="xs" leftSection={<Ti name="circle-dot" size={12} />} disabled={!!busy}
                    onClick={() => void startSession()}>start</Button>
          </div>
          <p>While it runs, what your viewers say is captured, classified and rate-limited per
            person. On stop the director reads it and you get a proposed plan to review —
            nothing is dispatched without you.</p>
        </div>
      )}

      {connected && (
        <div className="cl-log" ref={log}
             onScroll={(e) => {
               const el = e.currentTarget;
               pinned.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
             }}>
          {!rows.length
            ? <div className="cl-empty">Connected. Nothing said yet.</div>
            : rows.map((m, i) => {
              if (m.gap) return <div key={`gap${i}`} className="cl-empty">… some messages scrolled past while this tab was away</div>;
              const when = m.at
                ? new Date(m.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                : "";
              const flags = m.flags || [];
              const cls = ["cl-msg", m.mod ? "mod" : "", m.first ? "first" : "",
                m.captured ? "kept" : "", flags.includes("injection") ? "flagged" : ""].join(" ");
              const kept = m.captured === "playtest" ? "note"
                : m.captured === "feedback" ? (m.kind || "kept") : "";
              return (
                <div key={`${m.seq}-${i}`} className={cls}>
                  <span className="t">{when}</span>
                  <span className="a">{m.author}</span>
                  <span className="x">{m.text}</span>
                  {kept && <span className="k">{kept}</span>}
                </div>
              );
            })}
        </div>
      )}
    </div>
  );
}
