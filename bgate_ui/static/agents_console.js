/* agents_console.js — the left half of the Agents cockpit: talk, cat, replies.
 *
 * You say a sentence. The director reads it, answers you, and delegates the
 * pieces. Its children appear on the graph next door with an edge back to the
 * sentence that caused them. That is the whole loop, and this file owns three
 * quarters of it:
 *
 *   · the transcript      — your messages and the director's answers
 *   · the cat             — the floor's mood, in one animated frame and a line
 *   · agent responses     — what every OTHER seat has just said or produced
 *
 * It also owns the ONE poll. /api/console/state answers the whole cockpit in a
 * single request, and the graph is handed the same payload rather than fetching
 * its own — the old view made 1 + 1 + N requests every 3.5 seconds to draw less.
 *
 * THE CAT. The mascot is a real character with real moods, because a floor with
 * seven agents on it has a state that a number cannot carry: idle, working,
 * something wants you, something broke. Drawn as pixel geometry so it inherits
 * the theme and needs no asset — and replaced by a real sprite sheet the moment
 * the project has an approved artifact named "mascot", which is the honest way
 * to let a game's own art take over its tooling.
 */
(function () {
  "use strict";

  // POLL CADENCE, MATCHED TO WHETHER ANYTHING IS ACTUALLY HAPPENING.
  // A fixed 3s tick meant an idle board — nothing running, nothing queued —
  // still rebuilt the whole cockpit twenty times a minute and re-read every live
  // agent's log server-side to do it. 3s is right when you are watching an agent
  // work; it is pure heat when the floor is empty.
  //
  // The numbers come from the settings registry (console.poll_live_ms /
  // console.poll_idle_ms), delivered in the page bootstrap as
  // window.BGATE_SETTINGS — not a second fetch on load. THE FALLBACK IS NOT
  // DECORATION: a page served by an older build, or by a dashboard whose project
  // could not be read, has no bootstrap, and a console that then polls at NaN ms
  // is a console that never refreshes. Clamped as well as defaulted, because a
  // stored 0 would busy-loop the browser against the most expensive endpoint on
  // the server.
  const cfg = (key, fallback, lo, hi) => {
    const raw = Number((window.BGATE_SETTINGS || {})[key]);
    return Number.isFinite(raw) && raw > 0
      ? Math.max(lo, Math.min(raw, hi)) : fallback;
  };
  const POLL_LIVE_MS = cfg("poll_live_ms", 3000, 500, 60000);
  const POLL_IDLE_MS = cfg("poll_idle_ms", 12000, 1000, 300000);

  const SEATS = ["director", "narrative", "gameplay", "tech", "art", "audio", "qa"];
  // A seat name lands inside var(--c-…). esc() stops an attribute escape but not
  // a CSS one, and the value is agent-authored — so it is whitelisted, never
  // interpolated raw.
  const seatColor = s => `var(--c-${SEATS.includes(s) ? s : "tech"})`;

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const trunc = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };

  /* ── the cat ─────────────────────────────────────────────────────────────
     One line per mood, rotated. They are jokes, but they are jokes ABOUT THE
     STATE — "nobody's working" is information, and a mascot that says nothing
     true is decoration you stop seeing after a day. */
  const LINES = {
    idle: [
      "Floor's quiet. Suspiciously quiet.",
      "Seven seats, zero agents. Say somethin'.",
      "I've been sittin' on this fence post since sunup.",
      "No work on the board. That's either done or denial.",
      "Type in the box, partner. I don't read minds, I nap.",
    ],
    working: [
      "We're cookin'. Don't touch the tree.",
      "Agents ridin' out. Nobody spooks the horses.",
      "That's the sound of somebody else doin' the work.",
      "Steady on. I'll holler if one wanders off.",
      "Runnin' hot and nobody's on fire. Good day.",
    ],
    gate: [
      "Somethin's waitin' on YOU. Yeah. You.",
      "There's a gate open and a cat can't sign for it.",
      "Approvals don't approve themselves, boss.",
      "Work's stacked up at the fence. Go look.",
    ],
    broken: [
      "One of 'em went down. Ain't pretty.",
      "We got a failure. I'm not lickin' that clean.",
      "Somethin' broke. Read the log, then blame me.",
      "That agent hit a wall at full gallop.",
    ],
    done: [
      "Board's clear. That's a rare sight.",
      "All shipped. I'm takin' the afternoon.",
      "Nothin' queued, nothin' burnin'. Suspicious.",
    ],
    auto: [
      "Auto-deploy's on. Work hands itself off now.",
      "Autopilot engaged. I'm just here for the hat.",
    ],
  };

  /* THE MASCOT IS A SHIPPED SPRITE, not drawn geometry.
     It used to be inline SVG so it would theme itself and need no asset. That
     was the right call while it was a placeholder and the wrong one once the
     pipeline could make a real character: the drawn cat could not be the same
     cat the games use, and a tool whose own art is a wireframe is a poor advert
     for an art pipeline.
     Four cells of 128px: rest, half, wide, blink. The talk cycle walks the
     first three and back with steps(), so the mouth moves while it is
     delivering a line; blink lives in cell 3 and fires on its own timer when
     idle, because a blink on every syllable is a twitch. */
  const CAT_SPRITE = `<div class="cat-sprite" aria-hidden="true"></div>`;

  const AgentsConsole = {
    root: null, mounted: false, timer: null, state: null,
    mood: "idle", line: "", _lineAt: 0, _lineIx: 0, _spriteRel: "",
    _sending: false, _lastTurnSig: "", _pinBottom: true, _talkTimer: 0,
    // Who the composer is talking to. null = the director; a number = steer
    // that running agent directly. The director can steer too (agent_steer),
    // this is the human doing it without going through it.
    target: null,
    steers: [],
    // Half-typed answers to open questions, seq -> text. Kept out of the DOM so
    // a repaint of this column cannot take a sentence with it.
    answers: {}, _askSig: "",
    // An archived session being read instead of the live one. The poll keeps
    // running underneath — the graph stays live while you read history.
    viewing: null,

    /* ---- mount ---------------------------------------------------------- */
    mount() {
      if (this.mounted) return true;
      const chat = document.getElementById("ck-chat");
      const graphHost = document.getElementById("ck-canvas");
      if (!chat || !graphHost) return false;
      this.mounted = true;

      const mascot = document.getElementById("ck-cat");
      if (mascot) mascot.innerHTML = CAT_SPRITE;

      const form = document.getElementById("ck-say-form");
      const input = document.getElementById("ck-say");
      if (form) form.onsubmit = e => { e.preventDefault(); this.say(); };
      if (input) {
        input.onkeydown = e => {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.say(); }
        };
        input.oninput = () => {
          input.style.height = "auto";
          input.style.height = Math.min(150, input.scrollHeight) + "px";
        };
      }
      chat.addEventListener("scroll", () => {
        this._pinBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
      });

      const auto = document.getElementById("ck-auto");
      if (auto) auto.onclick = () => this.toggleAuto();
      document.querySelectorAll("#ck-gate [data-gate]").forEach(b =>
        b.onclick = () => this.setGate(b.dataset.gate, b));
      const filter = document.getElementById("ck-filter");
      if (filter) filter.onclick = () => {
        const mode = window.AgentsGraph
          ? AgentsGraph.setFilter(AgentsGraph.filter === "active" ? "all" : "active")
          : "active";
        filter.textContent = mode === "active" ? "in flight" : "everything";
        filter.title = mode === "active"
          ? "Showing work that is running, queued, broken, or just landed"
          : "Showing every item in the window, finished ones included";
      };
      const clear = document.getElementById("ck-target-x");
      if (clear) clear.onclick = () => this.aim(null);
      const all = document.getElementById("ck-deploy-all");
      if (all) all.onclick = () => this.deployAll();
      const clearQ = document.getElementById("ck-clear-queue");
      if (clearQ) clearQ.onclick = () => this.clearQueue();
      const panic = document.getElementById("ck-panic");
      if (panic) panic.onclick = () => this.panic();
      const clearBtn = document.getElementById("ck-clear");
      if (clearBtn) clearBtn.onclick = () => this.clearSession();
      const history = document.getElementById("ck-history");
      if (history) history.onclick = () => this.openHistory();
      const relayout = document.getElementById("ck-relayout");
      if (relayout) relayout.onclick = () => window.AgentsGraph && AgentsGraph.relayout();
      const fit = document.getElementById("ck-fit");
      if (fit) fit.onclick = () => window.AgentsGraph && AgentsGraph.fit();

      if (window.AgentsGraph) {
        AgentsGraph.mount(graphHost, document.getElementById("ck-detail"));
      }
      this.findSprite();
      this.poll();
      this.retime(POLL_LIVE_MS);
      return true;
    },

    /* One timer, re-armed only when the cadence actually changes. */
    retime(ms) {
      if (this._pollMs === ms && this.timer) return;
      this._pollMs = ms;
      if (this.timer) clearInterval(this.timer);
      this.timer = setInterval(() => {
        if (!this.visible()) return;
        this.poll();
      }, ms);
    },

    /* Is there anything worth a fast tick? Running agents, queued work about to
       be picked up, or something held for approval. An empty board is not. */
    busy() {
      const f = (this.state || {}).floor || {};
      const a = (this.state || {}).autopilot || {};
      return !!(f.running || f.dispatched
        || (a.on && f.queued) || f.review);
    },

    visible() {
      const view = document.getElementById("view-agents");
      const cockpit = document.getElementById("cockpit");
      return !!(view && view.classList.contains("active")
        && cockpit && !cockpit.hidden
        && document.visibilityState !== "hidden");
    },

    activate() {
      if (!this.mount()) return;
      this.poll();
      if (window.AgentsGraph) AgentsGraph.activate();
    },

    /* ---- the one poll --------------------------------------------------- */
    async poll() {
      const state = await window.readJSON("/api/console/state", { turns: [], items: [] });
      if (state.__error) { this.renderError(state.__error); return; }
      const before = this._sig;
      this.state = state;
      this._sig = this.signature(state);
      // THE BELL IS DRIVEN FROM HERE AND HAS NO POLL OF ITS OWN. notify.js keeps
      // one watchdog that every update() pushes back, so while this view is open
      // it never fires and /api/events is read on the drawer's own throttle
      // rather than on a second interval racing this one.
      //
      // ABOVE the repaint skip on purpose. Placed after it, the bell would stop
      // being driven the moment the payload stopped changing — an idle board is
      // exactly when the badge is the only thing on the page that still moves,
      // and it would have gone quiet for the 45s the watchdog takes to notice.
      if (window.Notify) { try { Notify.update(state); } catch (e) { /* never the console's problem */ } }
      this.retime(this.busy() ? POLL_LIVE_MS : POLL_IDLE_MS);
      // NOTHING MOVED: skip the repaint. The graph's rebuild walks every node and
      // the panels rebuild their innerHTML wholesale, and on an idle board that
      // work produced a pixel-identical page. A steer being typed, a dragged
      // node and a scrolled rail all survive better for not being touched.
      if (before && before === this._sig && !this._forceRender) return;
      this._forceRender = false;
      try {
        // An agent that has finished cannot be steered, and a composer still
        // aimed at it would swallow the next thing typed.
        if (this.target) {
          const still = (state.agents || []).some(
            a => a.state === "running" && Number(a.item_id) === this.target.id);
          if (!still) { window.toast(`#${this.target.id} finished — talking to the director again`); this.aim(null); }
        }
        this.renderChat();
        this.renderQueue();
        this.renderQuestions();
        this.renderReview();
        this.renderReplies();
        this.renderAuto();
        this.renderGate();
        this.renderMood();
        if (window.AgentsGraph) AgentsGraph.apply(state);
      } catch (e) { try { console.warn("[agents-console]", e); } catch (_) {} }
    },

    /* A 32-bit digest of the whole payload.
     *
     * Deliberately hashed from the FULL state rather than a hand-picked list of
     * fields: a signature that misses a field is a cockpit that silently stops
     * updating, which is a far worse bug than one wasted stringify. One pass,
     * and only the number is kept — holding the string itself would trade paint
     * time for GC churn every three seconds. */
    signature(state) {
      let text;
      try { text = JSON.stringify(state); } catch (e) { return 0; }
      // THE UNREAD COUNT IS NOT ON THIS PAYLOAD. It comes from /api/events, read
      // by notify.js on its own throttle, and it moves for things this payload
      // cannot show — an event pruned, another tab pressing "mark all read", an
      // answer landing somewhere else. Folded in because the question cards in
      // the column below are painted behind this signature, so without it a card
      // could sit there claiming to be open until something unrelated moved on
      // the board.
      if (window.Notify && window.Notify.mounted) {
        text += `|nt${Notify.readSeq}:${Notify.unread}:${Notify.head}`;
      }
      let h = 5381;
      for (let i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) | 0;
      return h || 1;
    },

    renderError(message) {
      const chat = document.getElementById("ck-chat");
      if (chat) chat.innerHTML = `<div class="empty err">the console is unreachable — ${esc(message)}</div>`;
    },

    /* ---- transcript ----------------------------------------------------- */
    renderChat() {
      const box = document.getElementById("ck-chat");
      if (!box) return;
      this.renderSessionBar();

      // Reading an archived session: same turns, same replies, plus the one
      // thing you came back for — the log of each run.
      if (this.viewing) {
        const v = this.viewing;
        const rows = (v.turns || []).map(t => {
          const r = t.reply || {};
          return `<div class="ck-turn">
            <div class="ck-msg you"><div class="ck-who">you</div>
              <div class="ck-txt">${esc(t.said || t.title)}</div></div>
            <div class="ck-msg dir"><div class="ck-who">director
                <span class="ck-arch">#${t.id} · ${esc(t.status)}</span></div>
              <div class="ck-txt">${esc(r.text || "(no answer recorded)")}</div>
              <button class="qbtn small ghost" data-log="${t.id}">log</button></div>
          </div>`;
        }).join("") || `<div class="ck-empty">this session has no turns</div>`;
        box.innerHTML = `<div class="ck-archbar">reading an archived session —
          <button class="cg-link" data-back="1">back to live</button></div>` + rows;
        box.querySelectorAll("[data-log]").forEach(b =>
          b.onclick = () => window.watchAgent && watchAgent(Number(b.dataset.log)));
        const back = box.querySelector("[data-back]");
        if (back) back.onclick = () => this.backToLive();
        this._lastTurnSig = "archived:" + (v.session || {}).id;
        return;
      }

      const turns = ((this.state || {}).turns || []);
      if (!turns.length && !this._sending && !this.steers.length) {
        box.innerHTML = `<div class="ck-welcome">
          <p>Tell the director what you want. It reads the board, answers you,
             and hands the pieces to the seats that can do them.</p>
          <p class="ck-eg">“the hub screen feels dead — give it parallax and a
             day/night tint” · “our enemy sprites are off-model, fix the set”</p>
        </div>`;
        return;
      }
      const sig = turns.map(t => `${t.id}:${t.status}:${(t.reply || {}).running ? 1 : 0}:${((t.reply || {}).text || "").length}`).join("|")
        + "//" + this.syncSteers();
      const rows = turns.map(t => {
        const r = t.reply || {};
        const answer = r.text
          ? `<div class="ck-msg dir"><div class="ck-who">director</div>
               <div class="ck-txt">${esc(r.text)}</div>
               ${r.cost ? `<div class="ck-cost">$${Number(r.cost).toFixed(3)}</div>` : ""}</div>`
          : r.running
            ? `<div class="ck-msg dir live"><div class="ck-who">director <span class="ck-dots"><i></i><i></i><i></i></span></div>
                 <div class="ck-txt thinking">${esc(r.thinking || "reading the board…")}</div>
                 <div class="ck-steps">${r.step_count || 0} steps</div></div>`
            : t.status === "failed"
              ? `<div class="ck-msg dir bad"><div class="ck-who">director</div>
                   <div class="ck-txt">that turn failed — open its log from the graph</div></div>`
              : `<div class="ck-msg dir"><div class="ck-who">director</div>
                   <div class="ck-txt thinking">not dispatched — nothing has read this yet</div>
                   <button class="qbtn small" data-deploy="${t.id}">deploy it</button></div>`;
        return `<div class="ck-turn" data-turn="${t.id}">
          <div class="ck-msg you"><div class="ck-who">you</div>
            <div class="ck-txt">${esc(t.said || t.title)}</div></div>
          ${answer}
        </div>`;
      }).join("");
      const pending = this._sending
        ? `<div class="ck-turn"><div class="ck-msg you"><div class="ck-who">you</div>
             <div class="ck-txt">${esc(this._sending)}</div></div>
           <div class="ck-msg dir live"><div class="ck-who">director <span class="ck-dots"><i></i><i></i><i></i></span></div>
             <div class="ck-txt thinking">waking up…</div></div></div>` : "";
      if (sig !== this._lastTurnSig || pending) {
        this._lastTurnSig = sig;
        box.innerHTML = rows + this.steerHTML() + pending;
        if (this._pinBottom) box.scrollTop = box.scrollHeight;
      }
      // Clicking a turn selects it on the graph — the sentence and its
      // consequences are the same object seen twice.
      box.querySelectorAll(".ck-turn").forEach(el => el.onclick = () => {
        if (window.AgentsGraph) AgentsGraph.select("turn_" + el.dataset.turn);
      });
      box.querySelectorAll("[data-deploy]").forEach(b => b.onclick = e => {
        e.stopPropagation();
        this.deploy(Number(b.dataset.deploy), b);
      });
    },

    /* Steers you sent, in the transcript with the rest of the conversation —
       a message to an agent is part of the same exchange, and a steer that
       vanished into a chip on another panel is a steer you cannot remember
       sending. "queued" flips to "read" when the agent's own log echoes it. */
    syncSteers() {
      const steps = (this.state || {}).steps || {};
      const now = Date.now();
      this.steers = this.steers.filter(s => now - s.at < 240000);
      this.steers.forEach(s => {
        if (s.state !== "queued") return;
        const echoed = (steps[String(s.id)] || []).some(
          x => x.kind === "steer" && String(x.text || "").includes(s.text.slice(0, 40)));
        if (echoed) s.state = "read";
      });
      return this.steers.map(s => s.id + ":" + s.state).join(",");
    },

    steerHTML() {
      return this.steers.slice(-6).map(s => {
        const note = s.state === "sending" ? "sending…"
          : s.state === "error" ? "not delivered — " + esc(s.err || "")
            : s.state === "read" ? "the agent has read it"
              : "queued — the agent reads it when its current step ends";
        return `<div class="ck-turn"><div class="ck-msg steer ${esc(s.state)}">
          <div class="ck-who">you → agent #${s.id}</div>
          <div class="ck-txt">${esc(s.text)}</div>
          <div class="ck-steps">${note}</div></div></div>`;
      }).join("");
    },

    /* Point the composer at one running agent (or back at the director).
       Called from the graph's detail rail and by the "@41 …" prefix. */
    aim(itemId, item) {
      this.target = itemId ? { id: Number(itemId), seat: (item && item.seat) || "",
                               title: (item && item.title) || "" } : null;
      // The composer's aim is client state, not payload state, so the next poll
      // would skip the repaint that shows it.
      this._forceRender = true;
      const chip = document.getElementById("ck-target");
      const input = document.getElementById("ck-say");
      if (chip) {
        chip.hidden = !this.target;
        const label = chip.querySelector(".ck-target-l");
        if (label && this.target) {
          label.textContent = `steering #${this.target.id}`
            + (this.target.seat ? ` · ${this.target.seat}` : "");
          label.title = this.target.title || "";
        }
      }
      if (input) {
        input.placeholder = this.target
          ? "say it straight to that agent — it reads this at the end of its current step…"
          : "tell the director what you want — it answers, then delegates…";
        input.focus();
      }
      const send = document.getElementById("ck-send");
      if (send) send.textContent = this.target ? "steer" : "send";
    },

    async say() {
      const input = document.getElementById("ck-say");
      let text = (input && input.value || "").trim();
      if (!text) return;
      if (this._sending) return;

      // "@41 slow down" aims one message without changing the target — the
      // keyboard path for the same thing the rail's button does.
      const at = text.match(/^@#?(\d+)\s+([\s\S]+)$/);
      const aimed = at ? Number(at[1]) : (this.target && this.target.id) || 0;
      if (at) text = at[2].trim();
      if (aimed) {
        if (input) { input.value = ""; input.style.height = "auto"; }
        await this.steer(aimed, text);
        return;
      }
      this._sending = text;
      if (input) { input.value = ""; input.style.height = "auto"; input.disabled = true; }
      // THE COMPOSER MUST COME BACK, whatever happens in between. It disables
      // itself while a turn is in flight and _sending gates re-entry — so a
      // throw anywhere below (a render against a null state after a failed
      // poll, say) used to leave the box disabled and the gate stuck closed
      // forever, and only a reload got the page back.
      let r;
      try {
        this.renderChat();
        r = await window.mutate("/api/console/say", { body: { text }, button: "ck-send" });
      } catch (err) {
        try { console.warn("[agents-console] say", err); } catch (e) {}
        // _local marks a failure mutate() never saw, so it never toasted it.
        r = { ok: false, _local: true,
              error: "the console could not send that — reload if it persists" };
      } finally {
        this._sending = false;
        if (input) { input.disabled = false; input.focus(); }
      }
      if (!r.ok) {
        if (r._local) window.toast(r.error);
        // The message is not lost — the operator gets their sentence back.
        if (input) input.value = text;
        this._lastTurnSig = "";
        this.renderChat();
        return;
      }
      const data = r.data || {};
      if (data.dispatched === false && data.refusal) {
        // A dirty tree is the one refusal the browser can actually resolve, and
        // dirtygate.js already owns that conversation — but it only hooks
        // /api/queue/{id}/dispatch. Re-dispatching the turn through that URL is
        // what gets the operator the "run anyway" button instead of a dead end.
        if (data.refusal.code === "dirty_tree" && data.turn_id) {
          const again = await window.mutate(`/api/queue/${data.turn_id}/dispatch`, { quiet: true });
          if (!again.ok) window.toast(`queued but not dispatched — ${data.refusal.message}`);
        } else {
          window.toast(`queued but not dispatched — ${data.refusal.message}`);
        }
      }
      this._lastTurnSig = "";
      this.speak(data.dispatched === false ? "broken" : "working", true);
      this.poll();
    },

    /* A message straight to a working agent. It lands as a user turn in that
       agent's live session, which it reads when its current step ends — so the
       chip says "queued" until the agent's own log echoes it back. */
    async steer(itemId, text) {
      const entry = { id: Number(itemId), text, state: "sending", at: Date.now() };
      this.steers.push(entry);
      this._lastTurnSig = "";
      this.renderChat();
      const r = await window.mutate(`/api/queue/${itemId}/steer`,
                                    { body: { text }, quiet: true });
      entry.state = r.ok ? "queued" : "error";
      entry.err = r.error;
      if (!r.ok) window.toast(r.error);
      this._lastTurnSig = "";
      this.renderChat();
      this.speak("working", true);
      this.poll();
    },

    /* ---- the kill switch ------------------------------------------------
     * One button, one call, no arguments: in the moment you need this you are
     * not going to enumerate item ids. It turns auto-deploy off FIRST — killing
     * agents while the loop is still on just dispatches a replacement into the
     * gap — then kills every tree, reaps orphans from any earlier dashboard,
     * and settles the items so the board stops claiming work is running. */
    async panic() {
      const running = ((this.state || {}).floor || {}).running || 0;
      const yes = await window.askConfirm({
        title: running ? `Stop ${running} running agent${running === 1 ? "" : "s"}?`
                       : "Stop everything?",
        body: "Every agent on this project is killed, its process tree with it, "
            + "and auto-deploy is turned off so nothing takes its place.\n\n"
            + "Work in progress is lost — the items are marked stopped, not "
            + "done, so you can see exactly what was interrupted and re-queue "
            + "it.\n\nFrom a terminal, the same thing is `bgate panic`.",
        ok: "stop everything", cancel: "leave them running", danger: true,
      });
      if (!yes) return;
      const r = await window.mutate("/api/console/killswitch",
                                    { body: { reason: "stopped from the console" },
                                      button: "ck-panic" });
      if (!r.ok) return;
      const d = r.data || {};
      const bits = [`${(d.stopped || []).length} agent(s) stopped`];
      if ((d.orphans || []).length) bits.push(`${d.orphans.length} orphan(s) reaped`);
      if (d.autopilot) bits.push("auto-deploy off");
      window.toast(bits.join(" · "), "ok");
      (d.errors || []).forEach(e => window.toast(e));
      this.speak("broken", true);
      this.poll();
    },

    /* ---- sessions: clear the console, come back to it later -------------
     * "Clear" files the conversation and moves a cut line. It deletes nothing:
     * every turn is still a work item and every run's log is still on disk,
     * which is the only reason clearing is safe to offer at all. */
    async clearSession() {
      const turns = ((this.state || {}).turns || []).length;
      if (!turns) { window.toast("nothing to clear"); return; }
      const yes = await window.askConfirm({
        title: "File this conversation?",
        body: `The ${turns} turn${turns === 1 ? "" : "s"} above move into history `
            + "and the console starts fresh.\n\nNothing is deleted — the work "
            + "items stay on the board and every agent's log stays where it is. "
            + "You can open the session again from 'history'.",
        ok: "file it", cancel: "keep it open",
      });
      if (!yes) return;
      const r = await window.mutate("/api/console/clear",
                                    { button: "ck-clear", ok: "filed — fresh console" });
      if (!r.ok) return;
      this.viewing = null;
      this._lastTurnSig = "";
      this.steers = [];
      this.poll();
    },

    async openHistory() {
      const sessions = ((this.state || {}).sessions || []);
      if (!sessions.length) {
        window.toast("no earlier sessions yet — 'clear' files the current one");
        return;
      }
      const pick = await window.askPick({
        title: "Earlier conversations",
        placeholder: "filter…",
        empty: "no sessions match",
        items: sessions.map(s => ({
          value: s.id,
          label: s.title || `session ${s.id}`,
          meta: `${s.turns} turn${s.turns === 1 ? "" : "s"} · #${s.from_id}–#${s.to_id}`,
          tags: [{ text: (s.at || "").slice(0, 16) }],
        })),
      });
      if (pick == null) return;
      const data = await window.readJSON(`/api/console/session/${pick}`, null);
      if (!data || data.__error) {
        window.toast(`could not open that session — ${(data && data.__error) || "gone"}`);
        return;
      }
      this.viewing = data;
      this._lastTurnSig = "";
      this.renderChat();
    },

    backToLive() {
      this.viewing = null;
      this._lastTurnSig = "";
      this.renderChat();
      this.renderSessionBar();
    },

    renderSessionBar() {
      const label = document.getElementById("ck-sess");
      if (!label) return;
      const s = this.state || {};
      if (this.viewing) {
        const v = this.viewing.session || {};
        label.innerHTML = `<b>archived</b> · ${esc(trunc(v.title || "session", 34))}`;
        label.classList.add("archived");
      } else {
        const n = (s.turns || []).length;
        label.textContent = n ? `this session · ${n} turn${n === 1 ? "" : "s"}`
                              : "this session";
        label.classList.remove("archived");
      }
      const clear = document.getElementById("ck-clear");
      if (clear) clear.textContent = this.viewing ? "back to live" : "clear";
      if (clear) clear.onclick = this.viewing ? () => this.backToLive()
                                              : () => this.clearSession();
    },

    /* ---- the queue: work that exists but is not happening ---------------
     * Deploying is the human's move, and it is the only door onto the graph.
     * That is the whole shape of this page: the director files work here, you
     * decide what actually runs, and the canvas next door shows exactly what
     * is live — never a wishlist. (Auto-deploy, if you turn it on, presses this
     * button for you.) */
    /* Undispatched chat turns are queued work like any other. A message whose
       dispatch was refused (or that you cancelled at the dirty-tree prompt)
       used to appear nowhere you could act on it — the transcript said "queued"
       and the one panel that can deploy did not list it. */
    queuedItems() {
      const s = this.state || {};
      return (s.turns || []).filter(t => t.status === "queued")
        .map(t => ({ ...t, seat: "director", title: t.said || t.title, _turn: true }))
        .concat((s.items || []).filter(
          i => i.status === "queued" && i.source !== "chat"));
    },

    renderQueue() {
      const box = document.getElementById("ck-queue");
      if (!box) return;
      const queued = this.queuedItems();
      const n = document.getElementById("ck-queue-n");
      if (n) n.textContent = String(queued.length);
      const all = document.getElementById("ck-deploy-all");
      if (all) all.disabled = !queued.length;
      const clear = document.getElementById("ck-clear-queue");
      if (clear) clear.disabled = !queued.length;

      box.innerHTML = queued.slice(0, 20).map(i => {
        // A chain link whose predecessor has not landed cannot be deployed, and
        // the button used to be offered anyway — one click, one refusal, no
        // explanation of what it was waiting for.
        const w = i.waiting_on;
        const held = i.ready === false && w;
        return `
        <div class="ck-q${i._turn ? " turn" : ""}${held ? " held" : ""}" data-id="${i.id}">
          <span class="ck-rep-seat" style="color:${seatColor(i.seat)}">${
            i._turn ? "your ask" : esc(i.seat)}</span>
          <span class="ck-rep-t" title="${esc(i.brief_preview || i.title)}">${esc(trunc(i.title, 52))}</span>
          ${held
            ? `<span class="ck-hold" title="${esc(w.title || "")}">waits on #${w.id} ${esc(w.seat)} · ${esc(w.status)}</span>`
            : `<button class="qbtn small" data-deploy="${i.id}">deploy</button>`}
          <button class="ck-x" data-discard="${i.id}" title="Discard this ticket"
                  aria-label="Discard">×</button>
        </div>`; }).join("")
        || `<div class="ck-empty">nothing waiting — ask for something above</div>`;
      box.querySelectorAll("[data-deploy]").forEach(b =>
        b.onclick = () => this.deploy(Number(b.dataset.deploy), b));
      box.querySelectorAll("[data-discard]").forEach(b =>
        b.onclick = () => this.discard(Number(b.dataset.discard), b));
    },

    /* Discard, not delete: the item goes to 'cancelled' with a reason, so a
       ticket that was called off still reads as called-off on the board and in
       the timeline rather than vanishing without an account of itself. */
    async discard(id, btn) {
      const r = await window.mutate(`/api/queue/${id}/cancel`,
        { body: { reason: "discarded from the console queue" },
          ok: `#${id} discarded`, button: btn });
      if (!r.ok) return;
      this.poll();
    },

    async clearQueue() {
      const queued = this.queuedItems();
      if (!queued.length) { window.toast("the queue is already empty"); return; }
      const yes = await window.askConfirm({
        title: `Discard ${queued.length} ticket${queued.length === 1 ? "" : "s"}?`,
        body: "Everything waiting in the queue is called off. They are marked "
            + "cancelled with a reason — not deleted — so the board still "
            + "accounts for them.\n\nAnything already running is untouched.",
        ok: "discard them", cancel: "keep them", danger: true,
      });
      if (!yes) return;
      let gone = 0;
      for (const item of queued) {
        const r = await window.mutate(`/api/queue/${item.id}/cancel`,
          { body: { reason: "queue cleared from the console" }, quiet: true });
        if (r.ok) gone += 1;
      }
      window.toast(gone ? `discarded ${gone} ticket(s)` : "nothing could be discarded",
                   gone ? "ok" : undefined);
      this.poll();
    },

    async deploy(id, btn) {
      // Straight at /api/queue/{id}/dispatch so dirtygate.js can offer "run
      // anyway" on an uncommitted tree instead of a dead-end toast.
      const r = await window.mutate(`/api/queue/${id}/dispatch`,
                                    { ok: `#${id} deployed`, button: btn });
      if (!r.ok) return;
      this.speak("working", true);
      this.poll();
    },

    async deployAll() {
      // A chain link whose predecessor has not landed would refuse, and one
      // refusal aborts the loop — so "deploy all" on a board with one chain in
      // it used to stop dead at the second link and report the chain as an
      // error. Deploy what is READY; the rest follow their own predecessors.
      const all = this.queuedItems();
      const queued = all.filter(i => i.ready !== false);
      const held = all.length - queued.length;
      if (!queued.length) {
        window.toast(held ? `${held} item(s) are waiting on earlier links` : "nothing to deploy");
        return;
      }
      const btn = document.getElementById("ck-deploy-all");
      if (btn) btn.disabled = true;
      let sent = 0;
      for (const item of queued) {
        // Sequential on purpose: dispatch refuses past the concurrency cap, and
        // firing twenty at once turns one cap refusal into twenty toasts.
        const r = await window.mutate(`/api/queue/${item.id}/dispatch`, { quiet: true });
        if (r.ok) { sent += 1; continue; }
        window.toast(sent ? `deployed ${sent} — then stopped: ${r.error}` : r.error);
        break;
      }
      if (sent) window.toast(`deployed ${sent} item(s)`
        + (held ? ` — ${held} still waiting on earlier links` : ""), "ok");
      this.poll();
    },

    /* ---- agent responses — LIVE agents only ------------------------------
     * This panel used to fill with the last thing every finished run said,
     * which meant the loudest thing on the page was a wall of "killed:
     * exceeded the runtime budget" from last week. What an agent said when it
     * died belongs to its log; this is a window on what is happening NOW. */
    renderReplies() {
      const box = document.getElementById("ck-replies");
      if (!box) return;
      const s = this.state || {};
      const live = new Set((s.agents || []).filter(a => a.state === "running")
        .map(a => Number(a.item_id)));
      const items = (s.items || []).filter(i => i.source !== "chat");
      const steps = s.steps || {};

      const running = items.filter(i => live.has(Number(i.id))).map(i => {
        const feed = steps[String(i.id)] || [];
        const last = [...feed].reverse().find(x => x.kind === "tool" || x.kind === "say"
          || x.kind === "result");
        const line = !last ? "starting up…"
          : last.kind === "tool" ? `<b>${esc(last.name || "tool")}</b> ${esc(trunc(last.hint || "", 70))}`
            : esc(trunc(last.text || "", 110));
        return `<button class="ck-rep live" data-id="${i.id}">
          <span class="ck-rep-seat" style="color:${seatColor(i.seat)}">${esc(i.seat)}</span>
          <span class="ck-rep-t">${esc(trunc(i.title, 54))}</span>
          <span class="ck-rep-l">${line}</span></button>`;
      });

      box.innerHTML = running.join("")
        || `<div class="ck-empty">no agent is working — deploy something from the queue</div>`;
      box.querySelectorAll(".ck-rep").forEach(el => el.onclick = () => {
        if (window.AgentsGraph && AgentsGraph.select("task_" + el.dataset.id)) return;
        if (window.watchAgent) watchAgent(Number(el.dataset.id));
      });
      const n = document.getElementById("ck-replies-n");
      if (n) n.textContent = String(running.length);
    },

    /* ---- auto-deploy ---------------------------------------------------- */
    renderAuto() {
      const btn = document.getElementById("ck-auto");
      const why = document.getElementById("ck-auto-why");
      if (!btn) return;
      const a = (this.state && this.state.autopilot) || {};
      btn.classList.toggle("on", !!a.on);
      btn.setAttribute("aria-pressed", a.on ? "true" : "false");
      const label = btn.querySelector(".ck-auto-l");
      if (label) label.textContent = a.on ? "auto-deploy on" : "auto-deploy off";
      if (why) {
        const ref = a.last_refusal;
        // An autopilot that is quietly refusing looks exactly like one that has
        // nothing to do. Say which it is.
        why.textContent = !a.on
          ? "queued work waits for you to press dispatch"
          : ref ? `held: ${ref.message}`
            : "queued work dispatches itself as slots free up";
        why.classList.toggle("warn", !!(a.on && ref));
      }
    },

    /* ---- the approval gate ---------------------------------------------- */
    renderGate() {
      const host = document.getElementById("ck-gate");
      if (!host) return;
      const g = (this.state && this.state.gate) || {};
      const active = g.mode || "agent";
      host.querySelectorAll("[data-gate]").forEach(b => {
        b.classList.toggle("on", b.dataset.gate === active);
        b.setAttribute("aria-pressed", b.dataset.gate === active ? "true" : "false");
      });
      // A panel showing 'agent' while BGATE_QA_GATE=0 forces 'none' is the most
      // expensive lie a settings control can tell, so the override says so.
      host.classList.toggle("forced", !!g.env_override);
      host.title = g.env_override
        ? `${g.env_override} — this control is overridden`
        : (g.labels && g.labels[active]) || "";
    },

    async setGate(mode, btn) {
      const g = (this.state && this.state.gate) || {};
      if (g.mode === mode) return;
      const r = await window.mutate("/api/gate",
        { body: { mode }, button: btn,
          ok: (g.labels && g.labels[mode]) || `gate: ${mode}` });
      if (!r.ok) return;
      this.poll();
    },

    /* ---- a seat asked YOU something -------------------------------------
     * ask_human is not a work item on purpose — a question that becomes a queued
     * row is a row somebody has to dispatch in order to read it. So it arrives on
     * this payload as state.questions and is answered from here, which is the one
     * panel already sitting next to the conversation the question came out of.
     *
     * The bell renders the same list in the drawer for when you are in another
     * view. Both post the same body to the same endpoint; neither is the primary.
     *
     * ONLY UNANSWERED ONES. An answered card that stays reads as still open, and
     * the answer is already on the event and in the handoff thread. */
    openQuestions() {
      const raw = (this.state || {}).questions;
      if (!Array.isArray(raw)) return [];
      return raw
        .map(q => ({
          seq: Number(q.event_seq || q.seq) || 0,
          item: Number(q.item_id) || 0,
          seat: String(q.seat || ""),
          question: String(q.question || ""),
          asked_at: String(q.asked_at || ""),
          refs: Array.isArray(q.refs) ? q.refs.map(String) : [],
          answer: String(q.answer || ""),
        }))
        .filter(q => q.seq && !q.answer);
    },

    renderQuestions() {
      const wrap = document.getElementById("ck-askwrap");
      const box = document.getElementById("ck-ask");
      if (!wrap || !box) return;
      const open = this.openQuestions();
      wrap.hidden = !open.length;
      const n = document.getElementById("ck-ask-n");
      if (n) n.textContent = String(open.length);
      if (!open.length) { box.innerHTML = ""; this._askSig = ""; return; }

      // Its own signature, like the transcript's. The rest of this column
      // repaints whenever anything on the board moves, and rebuilding a textarea
      // somebody is mid-sentence in is how a half-typed answer disappears.
      const sig = open.map(q => q.seq).join(",");
      if (sig !== this._askSig) {
        this._askSig = sig;
        // The caret, so a repaint that DOES land while you are typing (a new
        // question arriving) puts you back where you were. The text itself is in
        // this.answers, not in the DOM, for the same reason.
        const active = document.activeElement;
        const focused = active && active.dataset && active.dataset.answer;
        const caret = focused ? active.selectionStart : 0;

        box.innerHTML = open.map(q => {
          const who = q.seat || "director";
          return `<div class="ck-ask" data-seq="${q.seq}">
            <div class="ck-ask-top">
              <span class="ck-rep-seat" style="color:${seatColor(who)}">${esc(who)}</span>
              ${q.item ? `<span class="ck-ask-item">#${q.item}</span>` : ""}
              <span class="ck-spacer"></span>
              <span class="ck-hint">${esc(q.asked_at.slice(11, 16))}</span>
            </div>
            <div class="ck-ask-q">${esc(q.question)}</div>
            ${q.refs.length ? `<div class="ck-ask-refs">${q.refs
              .map(r => `<code>${esc(trunc(r, 52))}</code>`).join(" ")}</div>` : ""}
            <textarea class="ck-ask-reply" rows="2" data-answer="${q.seq}"
              placeholder="answer it in a sentence"></textarea>
            <div class="ck-ask-row">
              <span class="ck-hint">${q.item
                ? "a running agent reads it as a steer; a finished one gets a handoff note"
                : "filed as a decision the next session reads"}</span>
              <button class="qbtn small" data-send="${q.seq}">answer</button>
            </div>
          </div>`;
        }).join("");

        // value, not markup: a draft with "</textarea>" in it would otherwise
        // close the element early, and esc() inside a textarea is its own trap.
        box.querySelectorAll("[data-answer]").forEach(t => {
          t.value = this.answers[t.dataset.answer] || "";
          t.oninput = () => { this.answers[t.dataset.answer] = t.value; };
        });
        if (focused) {
          const again = box.querySelector(`[data-answer="${focused}"]`);
          if (again) {
            again.focus();
            try { again.setSelectionRange(caret, caret); } catch (e) { /* not fatal */ }
          }
        }
      }
      box.querySelectorAll("[data-send]").forEach(b =>
        b.onclick = () => this.answer(Number(b.dataset.send), b));
    },

    /* The answer goes where the asker can still read it, and the toast says
       WHICH of the three paths that was — a steer to a live agent, a handoff note
       for a finished one, a decision on the record. "Sent" would hide the one
       difference that matters. */
    async answer(seq, btn) {
      const key = String(seq || "");
      const text = String(this.answers[key] || "").trim();
      if (!text) {
        window.toast("write the answer first — an empty reply is not an answer");
        return;
      }
      const r = await window.mutate("/api/console/answer",
        { body: { seq: Number(key), answer: text }, button: btn, quiet: true });
      if (!r.ok) {
        window.toast(r.error);
        // 409 is somebody else answering first; the stored answer wins and the
        // draft does not, so re-read rather than retry.
        if (r.status === 409) { this._askSig = ""; this.poll(); }
        return;
      }
      const d = r.data || {};
      delete this.answers[key];
      window.toast(String(d.delivery || "answer recorded"), "ok");
      if (d.delivery_error) window.toast(`partly delivered — ${d.delivery_error}`);
      this._askSig = "";
      // The bell is showing the same question; make it re-read rather than wait
      // out its throttle with a card that is no longer open.
      if (window.Notify) { try { Notify.refresh(true); } catch (e) {} }
      this.poll();
    },

    /* ---- what is waiting on the human ----------------------------------- */
    reviewItems() {
      return ((this.state || {}).items || []).filter(i => i.status === "review");
    },

    renderReview() {
      const wrap = document.getElementById("ck-reviewwrap");
      const box = document.getElementById("ck-review");
      if (!wrap || !box) return;
      const held = this.reviewItems();
      wrap.hidden = !held.length;
      const n = document.getElementById("ck-review-n");
      if (n) n.textContent = String(held.length);
      if (!held.length) { box.innerHTML = ""; return; }
      box.innerHTML = held.slice(0, 20).map(i => `
        <div class="ck-q review" data-id="${i.id}">
          <span class="ck-rep-seat" style="color:${seatColor(i.seat)}">${esc(i.seat)}</span>
          <span class="ck-rep-t" title="${esc(i.result || i.brief_preview || i.title)}">${esc(trunc(i.title, 44))}</span>
          <button class="qbtn small" data-approve="${i.id}">approve</button>
          <button class="qbtn small ghost" data-rejectq="${i.id}">reject</button>
        </div>`).join("");
      box.querySelectorAll("[data-approve]").forEach(b =>
        b.onclick = () => this.approve(Number(b.dataset.approve), b));
      box.querySelectorAll("[data-rejectq]").forEach(b =>
        b.onclick = () => this.rejectItem(Number(b.dataset.rejectq), b));
    },

    async approve(id, btn) {
      const r = await window.mutate(`/api/queue/${id}/approve`,
        { body: {}, button: btn, ok: `#${id} approved — anything chained behind it can start` });
      if (!r.ok) return;
      this.poll();
    },

    /* Rejection needs a reason for the same purpose a QA fail does: it is
       appended to the brief, so the next agent reads what to change instead of
       repeating the run that was turned down. */
    async rejectItem(id, btn) {
      const reason = await window.askText({
        title: `Reject #${id}`,
        body: "Say exactly what is wrong and what would fix it. This is appended "
            + "to the item's brief, so the next agent on it reads what you wrote "
            + "instead of repeating the run you turned down.",
        placeholder: "the parity assertion is missing — add it and re-bake…",
        ok: "send it back", required: true,
      });
      if (reason == null || !String(reason).trim()) return;
      const r = await window.mutate(`/api/queue/${id}/reject`,
        { body: { reason: String(reason).trim() }, button: btn,
          ok: `#${id} sent back for another round` });
      if (!r.ok) return;
      this.poll();
    },

    async toggleAuto() {
      const a = (this.state && this.state.autopilot) || {};
      const next = !a.on;
      const r = await window.mutate("/api/console/autopilot",
        { body: { on: next }, button: "ck-auto",
          ok: next ? "auto-deploy on — queued work dispatches itself"
                   : "auto-deploy off" });
      if (!r.ok) return;
      const sent = ((r.data || {}).tick || {}).dispatched || [];
      if (sent.length) window.toast(`auto-dispatched ${sent.length} queued item(s)`, "ok");
      this.speak(next ? "auto" : "idle", true);
      this.poll();
    },

    /* ---- the cat -------------------------------------------------------- */
    moodOf() {
      const s = this.state || {};
      const floor = s.floor || {};
      const gates = (s.gates || []).filter(g => g.blocking);
      if ((s.items || []).some(i => i.status === "failed")) return "broken";
      if (floor.running) return "working";
      if (gates.length) return "gate";
      if (floor.queued) return (s.autopilot && s.autopilot.on) ? "auto" : "gate";
      if (floor.done) return "done";
      return "idle";
    },

    renderMood() {
      const mood = this.moodOf();
      const changed = mood !== this.mood;
      this.mood = mood;
      const host = document.getElementById("ck-mascot");
      if (host) host.dataset.mood = mood;
      // A new line on a mood change, otherwise every ~14 seconds so it does not
      // read as frozen and does not read as a slot machine either.
      if (changed || !this.line || Date.now() - this._lineAt > 14000) this.speak(mood, changed);
    },

    speak(mood, force) {
      const pool = LINES[mood] || LINES.idle;
      if (!pool.length) return;
      this._lineIx = (this._lineIx + 1) % pool.length;
      this.line = pool[force ? this._lineIx : Math.floor(Math.random() * pool.length)];
      this._lineAt = Date.now();
      // The mouth moves only while a line is FRESH. A permanently talking cat
      // is a screensaver; one that moves when it says something is a speaker.
      const host = document.getElementById("ck-mascot");
      if (host) {
        host.setAttribute("data-talking", "1");
        clearTimeout(this._talkTimer);
        this._talkTimer = setTimeout(
          () => host.removeAttribute("data-talking"),
          Math.min(6000, 900 + this.line.length * 45));
      }
      const bubble = document.getElementById("ck-bubble");
      if (bubble) {
        bubble.textContent = this.line;
        bubble.classList.remove("pop");
        void bubble.offsetWidth;   // restart the animation
        bubble.classList.add("pop");
      }
    },

    /* A project that has drawn its own mascot outranks the built-in cat. The
       rule is one line of policy: an APPROVED artifact whose logical name is
       "mascot" takes the frame. */
    async findSprite() {
      try {
        const d = await window.readJSON("/api/artifacts?logical_name=mascot", { artifacts: [] });
        const arts = (d.artifacts || []).filter(a => a.status === "approved");
        if (!arts.length) return;
        const art = arts[0];
        const frames = (art.metadata && art.metadata.frames) || {};
        const count = Object.keys(frames).length;
        const host = document.getElementById("ck-cat");
        if (!host) return;
        this._spriteRel = art.path;
        host.innerHTML = count > 1
          ? `<div class="ck-sprite spr-mount" data-rel="${esc(art.path)}" data-count="${count}" data-fps="8"></div>`
          : `<img class="ck-sprite" src="/api/preview?rel=${encodeURIComponent(art.path)}" alt="project mascot">`;
        if (count > 1 && window.SpriteAnim) SpriteAnim.mountAll(host);
      } catch (e) { /* the drawn cat is the fallback and it is always there */ }
    },
  };

  window.AgentsConsole = AgentsConsole;
})();
