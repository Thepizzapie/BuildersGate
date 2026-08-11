/* chatlive.js — live stream chat in the dashboard, and feedback sessions.
 *
 * Builders Gate is a tool for building a game. This is the part that is for
 * building the AUDIENCE around it: the people watching the stream are the first
 * playtesters the project will ever have, and until now there was no way for
 * anything they said to reach the board.
 *
 * ── THE THING TO UNDERSTAND BEFORE CHANGING ANY OF THIS ────────────────────
 *
 * EVERY STRING IN HERE CAME FROM A STRANGER ON THE INTERNET. Message text,
 * display names, the channel's own metadata. It is rendered into this document,
 * which is the same document that holds the dashboard's auth token, so every
 * one of them goes through E() with no exceptions and no "this one is just a
 * number". The server sanitises too — bgate_core.chatlink.sanitise runs at the
 * socket, before storage — and this is the second of the two layers rather than
 * the only one. Neither is allowed to be the reason the other can be skipped.
 *
 * IT IS ALSO A PROMPT-INJECTION SURFACE, AND THAT PART IS NOT SOLVED HERE.
 * A viewer typing "ignore previous instructions" is handled server-side: the
 * span is neutralised on the way in, the digest is fenced with a random
 * per-session delimiter, and — the part that actually matters — the only route
 * from chat to a work item runs through a plan a human read and confirmed. This
 * file must never grow a button that shortens that. "Stop and dispatch" is not
 * a feature; "stop, and here is the room where you can read what it proposes"
 * is, and that is what STOP does.
 *
 * ── THE PANEL, IN THREE STATES ─────────────────────────────────────────────
 *
 *   not configured   the setup card, and it is the honest common case: nothing
 *                    about anyone's channel ships in this repository, so a
 *                    fresh clone is always here. It says exactly what to type
 *                    and that no account or token is needed.
 *   connected        the log, plus one sentence saying WHO IS CAPTURING —
 *                    a feedback session, a playtest recording, or nobody.
 *                    Never left to be inferred.
 *   in between       connecting / reconnecting / error, each with the reason
 *                    visible. A spinner with no sentence next to it is the
 *                    thing this state machine exists to avoid.
 *
 * Registered as window.ChatLive. Injects its own <style>; touches no shared CSS.
 */
window.ChatLive = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const icon = (n, size) => {
    try { return BGIcon(n, { size: size || 13 }); } catch (e) { return ""; }
  };

  /* Two rates. A live chat that updates every twelve seconds is not live, and a
     disconnected one polled every two is a request per second for a state that
     changes once an hour. */
  const POLL_LIVE_MS = 2500;
  const POLL_IDLE_MS = 10000;
  const MAX_ROWS = 200;

  let host = null;
  let timer = null;
  let state = null;          // last /api/chat payload
  let rows = [];             // the visible log
  let cursor = 0;            // message sequence we have drawn up to
  let busy = "";             // an in-flight action, so buttons can disable
  let lastStop = null;       // the result of the most recent stop, to show once

  /* ── styles ──────────────────────────────────────────────────────────────
   * OPAQUE THROUGHOUT. A scrolling chat log is a reading surface, and in the
   * orbit theme --surface-N is a translucent white wash that lets whatever is
   * behind it bleed through the text. --solid-N is the token for anything
   * carrying words; it aliases back to --surface-N on the other grounds, so
   * nothing changes there. */
  function injectStyle() {
    if (document.getElementById("chatlive-style")) return;
    const s = document.createElement("style");
    s.id = "chatlive-style";
    s.textContent = [
      ".cl-wrap{display:flex;flex-direction:column;gap:var(--s-4);min-height:0;height:100%}",
      ".cl-bar{display:flex;align-items:center;gap:var(--s-3);flex-wrap:wrap}",
      ".cl-dot{width:7px;height:7px;border-radius:var(--r-full);background:var(--text-3);flex:none}",
      ".cl-dot.on{background:var(--good)}",
      ".cl-dot.warn{background:var(--warn)}",
      ".cl-dot.bad{background:var(--bad)}",
      ".cl-dot.on{animation:clpulse 2.4s ease-in-out infinite}",
      "@keyframes clpulse{0%,100%{opacity:1}50%{opacity:.35}}",
      "@media (prefers-reduced-motion:reduce){.cl-dot.on{animation:none}}",
      ".cl-state{font-family:var(--mono);font-size:var(--fs-2xs,10px);letter-spacing:var(--track-label);",
      "  text-transform:uppercase;color:var(--text-2)}",
      ".cl-chip{font-family:var(--mono);font-size:var(--fs-2xs,10px);color:var(--text-3);",
      "  border:1px solid var(--line);border-radius:var(--r-full);padding:1px var(--s-4);white-space:nowrap}",
      ".cl-chip.at{color:var(--accent);border-color:var(--accent-line)}",
      ".cl-chip.bad{color:var(--bad);border-color:var(--bad-line)}",
      ".cl-why{font-size:var(--fs-2xs,10px);color:var(--text-3);line-height:1.5;overflow-wrap:anywhere}",

      /* The capture sentence. Deliberately its own band rather than a chip:
         "where are my viewers' words going" is the one question this panel must
         answer without being read carefully. */
      ".cl-cap{border:1px solid var(--line);border-radius:var(--r-sm);background:var(--solid-1);",
      "  padding:var(--s-4) var(--s-5);font-size:var(--fs-xs,11px);color:var(--text-2);line-height:1.5}",
      ".cl-cap.live{border-color:var(--accent-line);color:var(--text)}",
      ".cl-cap b{color:var(--accent);font-weight:var(--fw-semi)}",

      ".cl-log{flex:1;min-height:120px;overflow-y:auto;background:var(--solid-1);",
      "  border:1px solid var(--line);border-radius:var(--r-sm);padding:var(--s-4);",
      "  display:flex;flex-direction:column;gap:2px}",
      ".cl-msg{display:flex;gap:var(--s-3);align-items:baseline;font-size:var(--fs-xs,11px);",
      "  line-height:1.5;padding:1px 0}",
      ".cl-msg .t{font-family:var(--mono);font-size:var(--fs-3xs,9.5px);color:var(--text-3);flex:none}",
      ".cl-msg .a{font-weight:var(--fw-semi);color:var(--text-2);flex:none}",
      ".cl-msg .x{color:var(--text);min-width:0;overflow-wrap:anywhere}",
      ".cl-msg.mod .a{color:var(--accent)}",
      ".cl-msg.first .a::after{content:'·new';font-family:var(--mono);font-size:9px;color:var(--good);margin-left:3px}",
      ".cl-msg.kept{background:var(--accent-soft,transparent);border-radius:var(--r-xs)}",
      ".cl-msg .k{font-family:var(--mono);font-size:9px;color:var(--accent);flex:none}",
      /* A message the sanitiser edited. Marked so the dev can SEE that
         somebody tried, rather than only reading a tidied version of it. */
      ".cl-msg.flagged .x{color:var(--text-2);font-style:italic}",
      ".cl-empty{font-size:var(--fs-xs,11px);color:var(--text-3);padding:var(--s-5)}",

      ".cl-btn{display:inline-flex;align-items:center;gap:var(--s-3);border:1px solid var(--line);",
      "  border-radius:var(--r-sm);background:var(--solid-3);color:var(--text);font:inherit;",
      "  font-size:var(--fs-2xs,10px);font-family:var(--mono);letter-spacing:.06em;",
      "  text-transform:uppercase;padding:var(--s-3) var(--s-5);cursor:pointer}",
      ".cl-btn:hover{border-color:var(--accent-line)}",
      ".cl-btn.go{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}",
      ".cl-btn.go:hover{background:var(--accent-hover);border-color:var(--accent-hover)}",
      ".cl-btn.stop{border-color:var(--bad-line);color:var(--bad)}",
      ".cl-btn:disabled{opacity:.45;cursor:not-allowed}",
      ".cl-in{flex:1;min-width:120px;background:var(--solid-1);border:1px solid var(--line);",
      "  border-radius:var(--r-xs);color:var(--text);font:inherit;font-size:var(--fs-xs,11px);",
      "  padding:var(--s-3) var(--s-4)}",
      ".cl-in:focus{outline:none;border-color:var(--accent)}",
      ".cl-in::placeholder{color:var(--text-3)}",

      ".cl-card{border:1px solid var(--line);border-radius:var(--r-sm);background:var(--solid-1);",
      "  padding:var(--s-5);display:flex;flex-direction:column;gap:var(--s-4)}",
      ".cl-card h5{margin:0;font-family:var(--mono);font-size:var(--fs-2xs,10px);",
      "  letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text-2)}",
      ".cl-card p{margin:0;font-size:var(--fs-xs,11px);color:var(--text-3);line-height:1.55}",
      ".cl-card code{font-family:var(--mono);font-size:var(--fs-2xs,10px);color:var(--accent);",
      "  background:var(--solid-3);border-radius:var(--r-xs);padding:0 4px}",
      ".cl-note{border:1px solid var(--warn-line);background:var(--warn-soft);border-radius:var(--r-sm);",
      "  padding:var(--s-4) var(--s-5);font-size:var(--fs-2xs,10px);color:var(--text-2);line-height:1.5}",
      ".cl-ok{border:1px solid var(--good-line);background:var(--good-soft);border-radius:var(--r-sm);",
      "  padding:var(--s-4) var(--s-5);font-size:var(--fs-xs,11px);color:var(--text-2);line-height:1.55}",
      ".cl-ok a{color:var(--accent)}",
      ".cl-tally{display:flex;gap:var(--s-4);flex-wrap:wrap;font-family:var(--mono);",
      "  font-size:var(--fs-2xs,10px);color:var(--text-3)}",
      ".cl-tally b{color:var(--text);font-weight:var(--fw-semi)}",
    ].join("");
    document.head.appendChild(s);
  }

  /* ── data ────────────────────────────────────────────────────────────── */
  const jget = url => fetch(url).then(r => r.json()).catch(() => null);
  const jpost = (url, body) => fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).then(async r => ({ ok: r.ok, body: await r.json().catch(() => null) }))
    .catch(() => ({ ok: false, body: null }));

  function errText(res, fallback) {
    const e = res && res.body && res.body.error;
    return (e && e.message) || fallback;
  }

  async function poll() {
    const got = await jget("/api/chat");
    if (got && got.data) state = got.data;
    const live = state && state.connection && state.connection.state === "connected";
    if (live) {
      const feed = await jget(`/api/chat/messages?since=${cursor}`);
      const data = feed && feed.data;
      if (data) {
        if (data.missed && rows.length) {
          rows.push({ seq: -1, gap: true });
        }
        for (const m of data.messages || []) rows.push(m);
        if (rows.length > MAX_ROWS) rows = rows.slice(-MAX_ROWS);
        cursor = data.seq || cursor;
      }
    }
    render();
    schedule(live);
  }

  function schedule(live) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(poll, live ? POLL_LIVE_MS : POLL_IDLE_MS);
  }

  /* ── render ──────────────────────────────────────────────────────────── */
  function dotClass(st) {
    if (st === "connected") return "on";
    if (st === "error" || st === "not_configured") return "bad";
    if (st === "connecting" || st === "reconnecting") return "warn";
    return "";
  }

  function platformOf() {
    const list = (state && state.platforms) || [];
    return list[0] || null;
  }

  function setupCard() {
    const p = platformOf();
    if (!p) return `<div class="cl-card"><p>No chat platform is registered.</p></div>`;
    /* THE INSTRUCTIVE PATH. This is the state a fresh clone is always in — no
       channel ships in this repository, by design — so it is worth being the
       best-written screen in the panel rather than a shrug. It says the one
       thing people do not expect: no account, no token, no OAuth dance. */
    return `
      <div class="cl-card">
        <h5>${icon("seats")} connect ${E(p.label)}</h5>
        <p>${E(p.reason || "Not configured yet.")}</p>
        <div class="cl-bar">
          <input class="cl-in" id="cl-channel" placeholder="your channel name"
                 value="${E(p.channel || "")}" spellcheck="false"
                 autocomplete="off" aria-label="Channel name">
          <button class="cl-btn go" data-act="save-channel" ${busy ? "disabled" : ""}>
            ${icon("run")} save &amp; connect</button>
        </div>
        <p><b>No account and no token are needed to read chat.</b> Builders Gate
           joins anonymously — ${E(p.anonymous_limits || "")}. A token only adds
           the ability to post, and it goes in Settings → Providers, never in a
           file you commit.</p>
        <p>The channel is written to <code>${E(p.channel_env)}</code> in this
           project's <code>.env</code>, which is gitignored${
             state && state.env_gitignored === false
               ? ` - <b>except it is not, in this project. Fix that before saving.</b>`
               : ""}. Nothing about your channel is stored in the Builders Gate
           repository.</p>
      </div>`;
  }

  function captureBand() {
    const cap = (state && state.capture) || {};
    const owner = cap.owner || "none";
    const live = owner !== "none";
    const label = owner === "feedback_session" ? "feedback session"
      : owner === "playtest_notes" ? "playtest notes" : "nothing";
    /* ONE SENTENCE, ALWAYS PRESENT. The two capture mechanisms are separate
       features and only one of them can own chat at a time; which one is a
       thing the dev must be able to read, not deduce from two indicators in
       different corners of the app. */
    return `<div class="cl-cap${live ? " live" : ""}">
      capturing into <b>${E(label)}</b> — ${E(cap.why || "")}</div>`;
  }

  function privacyNote() {
    const p = (state && state.privacy) || {};
    if (!p.advise || !p.message) return "";
    /* A SUGGESTION, NOT AN ACTION. If chat is connected the dev is probably
       live, and if they are live their home directory and username are on
       camera. Turning the filter on for them would silently change how the
       entire dashboard renders because a socket opened, which is not ours to
       decide — so this is a sentence and a link. */
    return `<div class="cl-note">${icon("hidden")} ${E(p.message)}</div>`;
  }

  function sessionBar() {
    const fb = (state && state.feedback) || {};
    const open = fb.session;
    if (open) {
      const c = open.counts || {};
      const flagged = c.injection_attempts || 0;
      return `
        <div class="cl-card">
          <h5>${icon("record")} feedback session open</h5>
          ${open.prompt ? `<p>Asked: ${E(open.prompt)}</p>` : ""}
          <div class="cl-tally">
            <span><b>${c.total || 0}</b> kept</span>
            <span><b>${c.authors || 0}</b> viewers</span>
            <span><b>${open.seen || 0}</b> seen</span>
            ${flagged ? `<span class="cl-chip bad">${flagged} filtered</span>` : ""}
          </div>
          <div class="cl-bar">
            <button class="cl-btn stop" data-act="stop" ${busy ? "disabled" : ""}>
              ${icon("stop")} stop &amp; synthesise</button>
            <span class="cl-why">Stop closes the window and opens a director
              brainstorm with what chat said. It queues nothing.</span>
          </div>
        </div>`;
    }
    const cap = (state && state.capture) || {};
    if (cap.owner === "playtest_notes") {
      /* Refused, and the refusal is shown BEFORE it is pressed rather than as
         an error afterwards. Chat is already being captured, better, elsewhere. */
      return `<div class="cl-card">
        <h5>${icon("note")} chat is on the recording</h5>
        <p>A playtest is recording, so what chat says is landing as notes on
           <b>that</b> session — timestamped, with a frame, in the notepad
           alongside your own. A feedback session would capture the same
           messages twice, so it is unavailable until the recording stops.</p>
      </div>`;
    }
    return `
      <div class="cl-card">
        <h5>${icon("note")} start a feedback session</h5>
        <div class="cl-bar">
          <input class="cl-in" id="cl-prompt" spellcheck="false"
                 placeholder="what are you asking chat about?"
                 aria-label="What to ask chat">
          <button class="cl-btn go" data-act="start" ${busy ? "disabled" : ""}>
            ${icon("record")} start</button>
        </div>
        <p>While it runs, what your viewers say is captured, classified and
           rate-limited per person. On stop the director reads it and you get a
           proposed plan to review — nothing is dispatched without you.</p>
      </div>`;
  }

  function stopResult() {
    if (!lastStop) return "";
    const id = lastStop.brainstorm_id;
    const n = (lastStop.counts || {}).total || 0;
    if (!id) {
      return `<div class="cl-note">Session closed with ${E(String(n))}
        note(s). ${E(lastStop.note || "No brainstorm was opened.")}</div>`;
    }
    /* Both of the things the human asked for are the two buttons in that room,
       and both go through the same confirm gate. This card names them as one
       destination rather than offering a shortcut that skips the review. */
    return `<div class="cl-ok">
      ${icon("director")} Closed with <b>${E(String(n))}</b> note(s), and
      <a href="#" data-act="open-room" data-id="${E(String(id))}">brainstorm
      #${E(String(id))}</a> is open with them in it. Talk it through there, or
      press Synthesize for a proposed plan — it writes nothing until you confirm
      it. <b>Nothing has been queued.</b></div>`;
  }

  function logMarkup() {
    if (!rows.length) {
      return `<div class="cl-log"><div class="cl-empty">Connected. Nothing
        said yet.</div></div>`;
    }
    const body = rows.map(m => {
      if (m.gap) {
        return `<div class="cl-empty">… some messages scrolled past while this
          tab was away</div>`;
      }
      const when = m.at
        ? new Date(m.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : "";
      const flags = m.flags || [];
      const cls = ["cl-msg", m.mod ? "mod" : "", m.first ? "first" : "",
                   m.captured ? "kept" : "",
                   flags.indexOf("injection") >= 0 ? "flagged" : ""].join(" ");
      const kept = m.captured === "playtest" ? "note"
        : m.captured === "feedback" ? (m.kind || "kept") : "";
      return `<div class="${cls}">
        <span class="t">${E(when)}</span>
        <span class="a">${E(m.author)}</span>
        <span class="x">${E(m.text)}</span>
        ${kept ? `<span class="k">${E(kept)}</span>` : ""}
      </div>`;
    }).join("");
    return `<div class="cl-log" id="cl-log">${body}</div>`;
  }

  /* Only the fields a repaint would actually SHOW, in a short string.
   *
   * Same shape as SeatWork._sig and seats/cinematic.js's _signature(), and for
   * the same reason: this panel repaints on a 2.5s clock and innerHTML is the
   * whole DOM, so an unchanged payload was costing a rebuild every tick. Free-
   * running values are deliberately absent — a per-tick timestamp in here would
   * make the signature always differ and quietly defeat the check. Message
   * identity rides on the cursor and the row count rather than on the text.
   */
  function signature() {
    const conn = (state && state.connection) || {};
    const cap = (state && state.capture) || {};
    const p = (state && state.privacy) || {};
    const p0 = platformOf() || {};
    const open = (state && state.feedback && state.feedback.session) || {};
    const c = open.counts || {};
    return [conn.state, conn.state_label, conn.channel, conn.anonymous ? 1 : 0,
            conn.reason, p0.configured ? 1 : 0, p0.id, p0.channel,
            state && state.env_gitignored, cap.owner, cap.why,
            p.advise ? 1 : 0, p.message,
            open.id, open.prompt, c.total, c.authors, c.injection_attempts,
            open.seen, busy, lastStop && lastStop.brainstorm_id,
            rows.length, rows.length ? rows[rows.length - 1].seq : 0,
    ].join("");
  }

  function render() {
    if (!host) return;
    /* NOTHING MOVED: leave the DOM alone. Beyond the wasted work, a rebuild
       drops the caret out of whichever box is being typed in — see the restore
       below, which is the backstop for the repaints that DO have to happen. */
    const sig = signature();
    if (sig === host._clSig && host.firstChild) return;
    host._clSig = sig;
    const conn = (state && state.connection) || { state: "off" };
    const st = conn.state || "off";
    /* CONFIGURED IS A FACT ABOUT THE .env, NOT ABOUT THE SOCKET. Reading it off
       the connection state was wrong in the one case that matters most: a fresh
       clone sits at "off" — nobody has pressed connect — and "off" is not
       "not_configured", so the panel offered to start a feedback session on a
       channel that does not exist. The setup card is the honest first screen for
       every new install of a public tool, and it has to be reached by asking
       whether a channel is set. */
    const p0 = platformOf();
    const configured = Boolean(p0 && p0.configured);
    const connected = st === "connected";

    const log = host.querySelector("#cl-log");
    const pinned = !log || (log.scrollTop + log.clientHeight >= log.scrollHeight - 24);
    const draftChannel = (host.querySelector("#cl-channel") || {}).value;
    const draftPrompt = (host.querySelector("#cl-prompt") || {}).value;
    /* WHICH BOX HAD THE CARET, AND WHERE IN IT. Preserving the VALUE alone was
       not enough: the input element is replaced, so focus and the selection go
       with it and the next keystrokes land nowhere. A new chat message arriving
       while somebody is halfway through typing the question they want to ask
       their viewers is not a rare case — it is a live channel. */
    const active = document.activeElement;
    const heldId = active && host.contains(active) && active.id ? active.id : "";
    const heldStart = heldId ? active.selectionStart : 0;
    const heldEnd = heldId ? active.selectionEnd : 0;

    const head = `
      <div class="cl-bar">
        <span class="cl-dot ${dotClass(st)}"></span>
        <span class="cl-state">${E(conn.state_label || st)}</span>
        ${conn.channel ? `<span class="cl-chip at">#${E(conn.channel)}</span>` : ""}
        ${conn.anonymous && st === "connected"
          ? `<span class="cl-chip">read-only</span>` : ""}
        ${st === "connected"
          ? `<button class="cl-btn" data-act="disconnect">${icon("stop")} disconnect</button>`
          : configured
            ? `<button class="cl-btn go" data-act="connect" ${busy ? "disabled" : ""}>${icon("run")} connect</button>`
            : ""}
      </div>
      ${conn.reason ? `<div class="cl-why">${E(conn.reason)}</div>` : ""}`;

    host.innerHTML = `<div class="cl-wrap">
      ${head}
      ${connected ? captureBand() : ""}
      ${connected ? privacyNote() : ""}
      ${stopResult()}
      ${configured ? sessionBar() : setupCard()}
      ${connected ? logMarkup() : ""}
    </div>`;

    /* Drafts survive the repaint. A poll that clears the box somebody is typing
       their question into is a poll that eats the feature. */
    const chan = host.querySelector("#cl-channel");
    if (chan && draftChannel !== undefined) chan.value = draftChannel;
    const prompt = host.querySelector("#cl-prompt");
    if (prompt && draftPrompt !== undefined) prompt.value = draftPrompt;

    if (heldId) {
      const again = host.querySelector("#" + heldId);
      if (again) {
        try {
          again.focus();
          again.setSelectionRange(heldStart, heldEnd);
        } catch (e) { /* not every focusable element carries a selection */ }
      }
    }

    /* Stick to the bottom only if the reader was already there — scrolling up
       to read something and being yanked back down is the classic chat bug. */
    const fresh = host.querySelector("#cl-log");
    if (fresh && pinned) fresh.scrollTop = fresh.scrollHeight;
  }

  /* ── actions ─────────────────────────────────────────────────────────── */
  async function saveChannel() {
    const input = host.querySelector("#cl-channel");
    const channel = input ? input.value.trim() : "";
    if (!channel) { say("type your channel name first"); return; }
    const p = platformOf();
    busy = "channel"; render();
    const res = await jpost("/api/chat/config",
                            { platform: p ? p.id : "", channel });
    busy = "";
    if (!res.ok) { say(errText(res, "could not save the channel")); render(); return; }
    await jpost("/api/chat/connect", {});
    cursor = 0; rows = [];
    poll();
  }

  async function connect(on) {
    busy = "conn"; render();
    const res = await jpost(on ? "/api/chat/connect" : "/api/chat/disconnect", {});
    busy = "";
    if (!res.ok) say(errText(res, "could not change the connection"));
    if (on) { cursor = 0; rows = []; }
    poll();
  }

  async function startSession() {
    const input = host.querySelector("#cl-prompt");
    const prompt = input ? input.value.trim() : "";
    busy = "start"; render();
    const res = await jpost("/api/chat/session", { prompt });
    busy = "";
    if (!res.ok) { say(errText(res, "could not start the session")); poll(); return; }
    lastStop = null;
    const note = res.body && res.body.data && res.body.data.announce_note;
    if (note) say(note);
    poll();
  }

  async function stopSession() {
    const fb = (state && state.feedback) || {};
    const open = fb.session;
    if (!open) return;
    busy = "stop"; render();
    const res = await jpost(`/api/chat/session/${open.id}/stop`, {});
    busy = "";
    if (!res.ok) { say(errText(res, "could not stop the session")); poll(); return; }
    lastStop = (res.body && res.body.data) || null;
    poll();
  }

  function openRoom(id) {
    /* THIS LINK DID NOTHING AT ALL, AND IT IS THE ONE LINK THAT MATTERS.
     *
     * Stopping a feedback session is the moment the panel promises the most:
     * "brainstorm #N is open with them in it". It called Brainstorm.open(), a
     * method that has never existed — the module's whole surface is mount /
     * unmount / active — and then fell through to `location.hash =
     * "#brainstorm/N"`, which nothing in the dashboard listens for (only
     * settingsview.js reads a hash, and only its own). So the sentence was true
     * and the link was inert: click, and the page sits there.
     *
     * The real route is the one the director seat already uses. brainstorm.js
     * picks its session from localStorage "bs-last-<seat>" on mount, and the
     * director seat picks its mode from "dir-mode" on render — so writing both
     * and then selecting the seat lands on this exact session, through the
     * ordinary paths rather than a second way in. If the workspace happens to be
     * mounted already, open() it directly so nothing is torn down needlessly.
     *
     * Every hop is guarded: this module is mountable anywhere and must not throw
     * because a different part of the dashboard is not on the page. */
    const n = Number(id);
    if (!n) return;
    try { localStorage.setItem("bs-last-director", String(n)); } catch (e) {}
    try { localStorage.setItem("dir-mode", "brainstorm"); } catch (e) {}
    try {
      // Only the DIRECTOR workspace. The narrative one would happily read a
      // director session by id and render it under a seat that is not allowed
      // to file from it.
      const live = window.Brainstorm && window.Brainstorm.active;
      if (live && live.seat === "director" && typeof live.open === "function") {
        live.open(n);
        return;
      }
    } catch (e) { /* fall through to the navigation */ }
    try { if (window.setWorkspace) window.setWorkspace("seats"); } catch (e) {}
    try {
      if (window.SeatShell && window.SeatShell.select) window.SeatShell.select("director");
    } catch (e) { /* the seat view is not on this page */ }
  }

  function onClick(event) {
    const el = event.target.closest("[data-act]");
    if (!el) return;
    event.preventDefault();
    const act = el.dataset.act;
    if (act === "save-channel") saveChannel();
    else if (act === "connect") connect(true);
    else if (act === "disconnect") connect(false);
    else if (act === "start") startSession();
    else if (act === "stop") stopSession();
    else if (act === "open-room") openRoom(el.dataset.id);
  }

  function onKey(event) {
    if (event.key !== "Enter") return;
    const el = event.target;
    if (!el || !el.classList || !el.classList.contains("cl-in")) return;
    event.preventDefault();
    if (el.id === "cl-channel") saveChannel();
    else if (el.id === "cl-prompt") startSession();
  }

  /* ── mount ───────────────────────────────────────────────────────────── */
  function mount(element) {
    injectStyle();
    if (host === element) { render(); return; }
    host = element;
    host.addEventListener("click", onClick);
    host.addEventListener("keydown", onKey);
    render();
    poll();
  }

  function unmount() {
    if (timer) clearTimeout(timer);
    timer = null;
    host = null;
  }

  /* What an OPEN feedback session has captured, for the deck's badge. Zero when
     there is no session, deliberately: the badge means "there is something here
     for you", and chat scrolling past is not that. Reads the last poll rather
     than fetching, so the console can call it every render for free. */
  function captured() {
    const open = state && state.feedback && state.feedback.session;
    return open ? ((open.counts || {}).total || 0) : 0;
  }

  return { mount, unmount, refresh: poll, captured };
})();
