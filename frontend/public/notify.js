/* notify.js — the header bell, and the drawer behind it.
 *
 * The complaint this answers: agents finish work and nothing tells you. The
 * event log (bgate_core.events, GET /api/events) now records every consequential
 * transition; this is the surface that reads it. One badge for "how much has
 * happened that you have not seen", one panel that says WHAT happened and which
 * item it happened to, and one card for a director question that is waiting on a
 * sentence from you.
 *
 * BE HONEST ABOUT WHAT THIS CHANNEL IS. The bell only tells you things while the
 * dashboard is open in front of you — a closed tab is not notified, and no
 * amount of polling changes that. The drawer's footer says so rather than
 * letting a badge imply a delivery guarantee it does not have; the channels that
 * survive a closed tab are the desktop window title and notify.webhook.
 *
 * IT OWNS NO TIMER WHILE SOMETHING ELSE IS DRIVING IT. The console already polls
 * on a cadence that tracks whether the floor is busy, and a second independent
 * interval would double the request rate to answer a question the first poll is
 * already awake for. So the driver calls Notify.update(state) after its own poll,
 * and the only timer here is a watchdog that is re-armed by every update() and
 * therefore never fires while a driver exists. Unmounted-but-visible with nobody
 * driving (a page where the console view was never opened) is the one case it
 * covers, slowly.
 *
 * THE BADGE IS NOT COUNTED FROM THE POLL. /api/events?since=<seq> walks forward
 * every tick; the unread count comes from the server's stored `ui` cursor, which
 * moves only when a human dismisses it. Deriving the badge from the batch that
 * just arrived is how a bell reads zero because the poller ate the events.
 */
(function () {
  "use strict";

  /* ── borrowed idioms (agents_console.js) ─────────────────────────────────
     esc() before every interpolation, one delegated listener per module, and
     window.readJSON / window.mutate / window.toast for every request so a
     failure is visible instead of swallowed. */
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const trunc = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  const SEATS = ["director", "narrative", "gameplay", "tech", "art", "audio", "cinematic", "qa"];
  // A seat name lands inside var(--c-…): whitelisted, never interpolated raw —
  // esc() stops an attribute escape but not a CSS one, and the value is written
  // by an agent.
  const seatColor = s => `var(--c-${SEATS.includes(s) ? s : "tech"})`;

  // Cadence. MIN_MS is a floor under the driver: the console ticks at 3s while
  // an agent is running and the bell does not need /api/events that often, so a
  // fast driver cannot turn into a fast poll here.
  const MIN_MS = 2500;
  const OPEN_MS = 4000;      // drawer open: you are reading it, keep it live
  const SHUT_MS = 15000;     // drawer shut: only the badge is on screen
  const WATCHDOG_MS = 45000; // nobody driving — one slow catch-up, not a poll
  const CACHE_MAX = 300;     // rows kept in memory; the log itself is the record
  const SHOW_MAX = 60;       // rows painted, newest first
  const TAIL_LIMIT = 100;    // a cold drawer wants the newest, not the oldest

  /* The event vocabulary, as a label and a tone. Tones map onto the theme's
     semantic colours (--good/--warn/--bad/--info) and nothing else — a bell that
     invents its own palette stops matching the app it is bolted to. An unknown
     kind renders with its raw name and the muted tone rather than vanishing:
     this UI must not be the reason a newly-emitted kind is invisible. */
  const KINDS = {
    "item.done": { label: "done", tone: "ok" },
    "item.review": { label: "review", tone: "warn" },
    "item.failed": { label: "failed", tone: "bad" },
    "item.approved": { label: "approved", tone: "ok" },
    "item.rejected": { label: "rejected", tone: "warn" },
    "item.aging": { label: "aging", tone: "warn" },
    "chain.filed": { label: "chain", tone: "info" },
    "chain.advanced": { label: "handoff", tone: "info" },
    "chain.stalled": { label: "stalled", tone: "bad" },
    "gate.mode": { label: "gate", tone: "info" },
    "budget.refused": { label: "budget", tone: "bad" },
    "director.question": { label: "question", tone: "warn" },
    "agent.spawned": { label: "spawned", tone: "mute" },
    "agent.exited": { label: "exited", tone: "mute" },
  };
  const meta = kind => KINDS[kind] || { label: String(kind || "event"), tone: "mute" };

  // 16px bell, stroke-only so it inherits the button's colour in both grounds.
  const BELL = `<svg viewBox="0 0 16 16" width="15" height="15" fill="none"
    stroke="currentColor" stroke-width="1.4" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">
    <path d="M4 6.5a4 4 0 0 1 8 0c0 2.4.6 3.6 1.2 4.2.3.3.1.8-.3.8H3.1c-.4 0-.6-.5-.3-.8C3.4 10.1 4 8.9 4 6.5Z"/>
    <path d="M6.4 13.4a1.8 1.8 0 0 0 3.2 0"/></svg>`;

  /* SQLite writes `datetime('now')`, which is UTC with no offset in it. A bare
     Date.parse of "2026-07-29 20:15:00" is read as LOCAL time by some engines,
     which is how a two-minute-old event renders as "in 5h". Same fix as
     seats/art.js. */
  const stampMs = when => {
    const text = String(when || "");
    if (!text) return 0;
    const ms = Date.parse(text.replace(" ", "T")
      + (/[zZ]|[+-]\d\d:?\d\d$/.test(text) ? "" : "Z"));
    return Number.isFinite(ms) ? ms : 0;
  };
  const ago = when => {
    const ms = stampMs(when);
    if (!ms) return "";
    const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 45) return "just now";
    if (s < 5400) return `${Math.round(s / 60)}m ago`;
    if (s < 172800) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  };

  const num = v => { const n = Number(v); return Number.isFinite(n) ? n : 0; };

  const Notify = {
    host: null, mounted: false, open: false,

    // Server truth, replaced wholesale on every read.
    events: [],            // newest first, capped at CACHE_MAX
    seq: 0,                // the cursor this module has READ UP TO (poll position)
    head: 0,
    readSeq: 0,            // the stored `ui` cursor — what the badge counts against
    unread: 0, unreadTotal: 0, unreadByKind: {},
    notifyKinds: [], vocabulary: [], inApp: true,
    gap: false, older: false, cold: true,
    questions: [],         // normalised open/answered ask_human questions
    drafts: {},            // seq -> half-typed answer, survives a repaint

    // _driven: a caller has handed us open_questions at least once, so the event
    // cache must stop guessing at them. See _absorb.
    _fetching: false, _lastFetch: 0, _watchdog: 0, _sig: "", _err: "", _drain: 0,
    _driven: false,

    /* ---- mount ----------------------------------------------------------
     * `container` is the header slot the wire agent gives us. Returns false
     * rather than throwing when it is not in the DOM yet, so a caller can retry
     * on the next tick — the console's mount() has the same contract. */
    mount(container) {
      if (this.mounted) return true;
      const host = typeof container === "string"
        ? document.getElementById(container)
        : (container || document.getElementById("nt-host"));
      if (!host) return false;
      this.host = host;
      this.mounted = true;
      host.classList.add("nt-wrap");
      // No <style> block here: every nt-* rule lives in app.css, in the
      // components layer next to .schip and .lamp. Shipping a second copy inside
      // the module meant two definitions of the same drawer, with the injected
      // one silently winning because it parses later — so a fix to app.css would
      // have had no effect and nothing would have said why.
      host.innerHTML = `
        <button class="nt-bell" id="nt-bell" type="button" aria-expanded="false"
                aria-label="Notifications" title="What has happened">
          ${BELL}<span class="nt-badge hidden" id="nt-badge" aria-live="polite"></span>
        </button>
        `;

      /* THE DRAWER IS A CHILD OF <body>, NOT OF THE BELL'S HOST.
         It used to sit inside #nt-host, which lives in the shell header — and
         in the orbit ground that header carries a backdrop-filter. An element
         with backdrop-filter is a BACKDROP ROOT: a descendant's own filter can
         only sample content inside it, so the drawer's frost had nothing but
         the 42px header behind it and rendered as a clear pane. The same
         nesting sealed the drawer into the header's stacking context, which is
         why the page's panels painted straight over it.

         At body level it frosts the actual page and needs no z-index contest
         with the stage. It is positioned in CSS against the viewport; the bell
         stays where it is and still owns the toggle. */
      const drawer = document.createElement("div");
      drawer.className = "nt-drawer";
      drawer.id = "nt-drawer";
      drawer.setAttribute("role", "dialog");
      drawer.setAttribute("aria-label", "Notifications");
      drawer.hidden = true;
      document.body.appendChild(drawer);
      this.drawer = drawer;

      /* ONE delegated click listener for the whole module. Rows, the mark-read
         button and the reply buttons are all rebuilt on every repaint, so
         per-element handlers would have to be rebound each time and one missed
         rebind is a dead button nobody notices. */
      /* BOUND TO BOTH, because the drawer is no longer inside the host.
         It is appended to <body> (see above), so delegation on the host alone
         reached the bell and nothing else — every control in the drawer, mark
         all read and the reply boxes included, was dead. */
      const onClick = e => {
        try { this._onClick(e); } catch (err) { this._warn(err); }
      };
      // A draft answer lives in this.drafts, not in the DOM: the drawer repaints
      // whenever the log moves and a textarea's value would go with it.
      const onInput = e => {
        const box = e.target && e.target.closest && e.target.closest("[data-nt-reply]");
        if (box) this.drafts[box.dataset.ntReply] = box.value;
      };
      host.addEventListener("click", onClick);
      host.addEventListener("input", onInput);
      drawer.addEventListener("click", onClick);
      drawer.addEventListener("input", onInput);
      document.addEventListener("click", e => {
        if (!this.open || !this.host) return;
        // BOTH, for the same reason the handlers above are bound twice: the
        // drawer is a sibling of the host now, not a child, so testing the
        // host alone treated every click INSIDE the drawer as a click outside
        // it and shut the thing the moment you touched it.
        if (this.host.contains(e.target)) return;
        if (this.drawer && this.drawer.contains(e.target)) return;
        this.close();
      });
      document.addEventListener("keydown", e => {
        if (e.key === "Escape" && this.open) this.close();
      });

      this.paint();
      this.refresh(true);
      this._arm();
      return true;
    },

    /* ---- being driven ---------------------------------------------------
     * The console calls this after its own poll with the state it just read.
     * Two things come out of it: the drive heartbeat (which keeps the watchdog
     * asleep) and the open-question list, which is on the console payload
     * already and is authoritative in a way the event log is not — a question
     * stays open until it is answered, so a cursor that has walked past the
     * event would show it once and never again. */
    update(state) {
      if (!this.mounted) return false;
      this._arm();
      if (state && !state.__error && Array.isArray(state.questions)) {
        this._driven = true;
        this.questions = state.questions.map(q => this._question(q));
        this._paintSoon();
      }
      this.refresh(false);
      return true;
    },

    // The seat/console modules are handed state via render(); same entry point.
    render(state) { return this.update(state); },

    /* One timer, and it is a watchdog rather than a poll: every update() pushes
       it back, so while the console is driving us it never fires. Undriven, it
       is a slow catch-up so the badge is not frozen on a page whose owner never
       opened the Agents view. */
    _arm() {
      clearTimeout(this._watchdog);
      this._watchdog = setTimeout(() => {
        this.refresh(true);
        this._arm();
      }, WATCHDOG_MS);
    },

    /* ---- reading the log ------------------------------------------------ */
    async refresh(force) {
      if (!this.mounted || this._fetching) return;
      // A hidden tab must not keep polling; the next update() or the watchdog
      // picks it up when the page comes back.
      if (!force && document.visibilityState === "hidden") return;
      const gap = Math.max(MIN_MS, this.open ? OPEN_MS : SHUT_MS);
      if (!force && Date.now() - this._lastFetch < gap) return;
      this._fetching = true;
      try {
        // COLD OPEN ASKS FOR THE TAIL, NOT since=0. `since` omitted means "the
        // newest limit events"; since=0 is the literal cursor read, which on a
        // fortnight of history hands back the two hundred OLDEST rows — the
        // least interesting screenful in the database.
        const url = this.cold
          ? `/api/events?limit=${TAIL_LIMIT}`
          : `/api/events?since=${this.seq}&limit=200`;
        const d = await window.readJSON(url, { events: [] });
        if (d && d.__error) { this._err = String(d.__error); this.paint(); return; }
        this._err = "";
        this._absorb(d);
        this.paint();
        /* `more` means the limit truncated a FORWARD read, so there is a newer
           page waiting and the next tick would be a page behind. Drained here,
           with a hard cap: a bug that leaves `more` permanently true must cost
           three requests, not a spin. Tail reads never set it — see the
           endpoint's note — so this cannot recurse on a cold open. */
        if (d && d.more && !d.tail && this._drain < 3) {
          this._drain += 1;
          this._fetching = false;
          this._lastFetch = 0;
          await this.refresh(true);
          return;
        }
        this._drain = 0;
      } catch (e) {
        this._warn(e);
      } finally {
        this._fetching = false;
        this._lastFetch = Date.now();
      }
    },

    /* Fold one /api/events payload into local state. Every key here is one the
       endpoint documents; nothing is inferred from the batch. */
    _absorb(d) {
      const batch = Array.isArray(d.events) ? d.events : [];
      const newest = batch.slice().reverse();       // server sends oldest first
      if (d.tail) {
        this.events = newest;
        this.older = !!d.older;
        this.cold = false;
      } else if (newest.length) {
        const seen = new Set(newest.map(e => num(e.id)));
        this.events = newest.concat(this.events.filter(e => !seen.has(num(e.id))));
      }
      if (this.events.length > CACHE_MAX) this.events.length = CACHE_MAX;
      // seq is the poll position and moves forward only. head/read_seq/unread
      // are the server's, and the badge is the server's number — never a count
      // of what happens to be in this.events.
      this.seq = Math.max(this.seq, num(d.seq));
      this.head = num(d.head);
      this.readSeq = num(d.read_seq);
      this.unread = num(d.unread);
      this.unreadTotal = num(d.unread_total);
      this.unreadByKind = (d.unread_by_kind && typeof d.unread_by_kind === "object")
        ? d.unread_by_kind : {};
      this.notifyKinds = Array.isArray(d.notify_kinds) ? d.notify_kinds : [];
      this.vocabulary = Array.isArray(d.vocabulary) ? d.vocabulary : [];
      this.inApp = d.in_app !== false;
      this.gap = !!d.gap;
      /* Questions from the event cache are the FALLBACK, and only until somebody
         drives us. open_questions is a live query against the payloads; the
         cached event is a snapshot, so a question answered in another tab would
         be resurrected from it as an open card with a reply box that 409s. Once
         a driver has spoken, an empty list means there are none. */
      if (!this._driven && !this.questions.length) {
        this.questions = this._questionsFromEvents();
      }
    },

    _questionsFromEvents() {
      return this.events
        .filter(e => e.kind === "director.question")
        .map(e => this._question({
          event_seq: e.id, asked_at: e.created_at, ...(e.payload || {}),
        }))
        .slice(0, 6);
    },

    /* One question shape whatever it came from, with every key present. A card
       that has to test for the existence of `answer` renders differently on two
       projects, which is how a "no questions" empty state ships broken. */
    _question(q) {
      const src = q || {};
      return {
        seq: num(src.event_seq || src.seq),
        item_id: num(src.item_id),
        seat: String(src.seat || ""),
        question: String(src.question || ""),
        asked_at: String(src.asked_at || ""),
        asked_by: String(src.asked_by || ""),
        refs: Array.isArray(src.refs) ? src.refs.map(String) : [],
        answer: String(src.answer || ""),
        answered_at: String(src.answered_at || ""),
      };
    },

    /* ---- the drawer ----------------------------------------------------- */
    toggle() { this.open ? this.close() : this.openDrawer(); },

    openDrawer() {
      this.open = true;
      this._sig = "";              // force one repaint on the way in
      this.paint();
      this.refresh(true);
    },

    close() {
      this.open = false;
      this.paint();
    },

    _onClick(e) {
      const t = e.target;
      if (!t || !t.closest) return;
      if (t.closest("#nt-bell")) { this.toggle(); return; }
      const act = t.closest("[data-nt]");
      if (!act) return;
      const what = act.dataset.nt;
      if (what === "readall") { this.markRead(act); return; }
      if (what === "open") { this.openItem(num(act.dataset.ntItem)); return; }
      if (what === "answer") { this.answer(act.dataset.ntSeq, act); return; }
      if (what === "reload") { this.cold = true; this.refresh(true); return; }
    },

    /* Mark everything read. `seq` is omitted on purpose: the server resolves it
       against the head it can see, so a page that has been open across a dozen
       emits cannot clear a range it never received — and cannot fail to clear
       the ones that landed while the click was in flight either. */
    async markRead(btn) {
      const r = await window.mutate("/api/events/read", { body: {}, button: btn, quiet: true });
      if (!r.ok) { window.toast(`the bell could not be cleared - ${r.error}`); return; }
      const d = r.data || {};
      this.readSeq = num(d.read_seq);
      this.unread = num(d.unread);
      this.unreadTotal = num(d.unread_total);
      this.unreadByKind = (d.unread_by_kind && typeof d.unread_by_kind === "object")
        ? d.unread_by_kind : {};
      this.head = num(d.head) || this.head;
      this.paint();
    },

    /* An event row is a way back to the work it is about. Both handoffs are
       guarded: this module can be mounted on a page where the graph does not
       exist, and a dead click is better than a thrown one. */
    openItem(id) {
      if (!id) return;
      try {
        if (window.AgentsGraph && AgentsGraph.select && AgentsGraph.select("task_" + id)) {
          this.close();
          return;
        }
      } catch (e) { /* fall through to the log */ }
      try {
        if (window.watchAgent) { window.watchAgent(id); this.close(); return; }
      } catch (e) { this._warn(e); }
    },

    async answer(seq, btn) {
      const key = String(seq || "");
      const text = String(this.drafts[key] || "").trim();
      if (!text) {
        window.toast("write the answer first - an empty reply is not an answer");
        return;
      }
      const r = await window.mutate("/api/console/answer",
        { body: { seq: Number(key), answer: text }, button: btn, quiet: true });
      if (!r.ok) {
        // 409 is "somebody already answered this one" — the stored answer is
        // authoritative and the draft is not, so resync rather than retry.
        window.toast(r.error);
        if (r.status === 409) { this.cold = true; this.refresh(true); }
        return;
      }
      const d = r.data || {};
      delete this.drafts[key];
      // The reply's whole value is WHERE it landed: a live agent got it as a
      // steer, a finished one left a handoff note. Saying "sent" would hide the
      // difference that matters.
      window.toast(String(d.delivery || "answer recorded"), "ok");
      if (d.delivery_error) window.toast(`partly delivered - ${d.delivery_error}`);
      this.questions = this.questions.map(q =>
        q.seq === Number(key) ? { ...q, answer: text } : q);
      this.cold = true;
      this.refresh(true);
    },

    /* ---- painting -------------------------------------------------------
     * The badge is written by hand (textContent, attributes) and the drawer body
     * by innerHTML behind a signature check. Repainting a drawer that has not
     * changed would drop a reply mid-sentence and lose the caret, and the log
     * moves on every completion. */
    _paintSoon() { if (this.mounted) this.paint(); },

    paint() {
      if (!this.mounted) return;
      try { this._paintBell(); } catch (e) { this._warn(e); }
      try { this._paintDrawer(); } catch (e) { this._warn(e); }
    },

    _paintBell() {
      const bell = document.getElementById("nt-bell");
      const badge = document.getElementById("nt-badge");
      if (!bell || !badge) return;
      // notify.in_app off means the bell does not RING. The drawer still opens
      // and still lists everything — the setting turns off the interruption, not
      // the record, and hiding the history as well would make the switch look
      // like a data loss.
      const ring = this.inApp ? this.unread : 0;
      badge.textContent = ring > 99 ? "99+" : String(ring);
      badge.classList.toggle("hidden", ring <= 0);
      bell.classList.toggle("on", this.open);
      bell.classList.toggle("muted", !this.inApp);
      bell.setAttribute("aria-expanded", this.open ? "true" : "false");
      bell.setAttribute("aria-label", ring
        ? `Notifications - ${ring} unread`
        : "Notifications");
      bell.title = this._err ? `the event log is unreachable - ${this._err}`
        : !this.inApp ? "notifications are off (notify.in_app) - the drawer still lists everything"
          : ring ? `${ring} unread${this.unreadTotal > ring ? ` · ${this.unreadTotal} events in all` : ""}`
            : "nothing new";
    },

    _paintDrawer() {
      const box = document.getElementById("nt-drawer");
      if (!box) return;
      box.hidden = !this.open;
      if (!this.open) return;
      const sig = this._signature();
      if (sig === this._sig) return;
      this._sig = sig;

      // What the caret was on, so a repaint that lands mid-sentence does not
      // steal it. The draft text itself is already in this.drafts.
      const active = document.activeElement;
      const focused = active && active.dataset && active.dataset.ntReply;
      const caret = focused ? active.selectionStart : 0;

      box.innerHTML = this._head() + `<div class="nt-body">`
        + this._questionCards() + this._banners() + this._rows()
        + `</div>` + this._foot();

      if (focused) {
        const again = box.querySelector(`[data-nt-reply="${focused}"]`);
        if (again) {
          again.focus();
          try { again.setSelectionRange(caret, caret); } catch (e) { /* not fatal */ }
        }
      }
    },

    /* Everything the drawer draws from, in one string. Deliberately includes the
       question answers and the error: a card that flips to "answered" is a
       repaint, and a failure that appears must not be held back by a signature
       that only watched the ids. */
    _signature() {
      const rows = this.events.slice(0, SHOW_MAX)
        .map(e => `${e.id}`).join(",");
      const qs = this.questions
        .map(q => `${q.seq}:${q.answer ? 1 : 0}`).join(",");
      return [this.readSeq, this.head, this.unread, this.unreadTotal,
        this.inApp ? 1 : 0, this.gap ? 1 : 0, this.older ? 1 : 0,
        this._err, qs, rows].join("|");
    },

    _head() {
      const ring = this.inApp ? this.unread : 0;
      return `<div class="nt-head">
        <span class="nt-title">Notifications</span>
        ${ring ? `<span class="nt-count">${ring} unread</span>` : ""}
        <span class="nt-acts">
          <button class="nt-act" type="button" data-nt="reload" title="Re-read the log">refresh</button>
          <button class="nt-act primary" type="button" data-nt="readall"
                  ${this.unreadTotal ? "" : "disabled"}>mark all read</button>
        </span>
      </div>`;
    },

    _banners() {
      let out = "";
      if (this._err) {
        out += `<div class="nt-err">the event log is unreachable - ${esc(this._err)}</div>`;
      }
      // A pruned range is reported, never silently skipped: "you missed 40
      // events" and "nothing happened" must not look the same.
      if (this.gap) {
        out += `<div class="nt-gap">some older events were pruned before this
          page read them — the log keeps 14 days</div>`;
      }
      if (this.older) {
        out += `<div class="nt-note">showing the most recent ${TAIL_LIMIT} —
          there is more history behind this window</div>`;
      }
      return out;
    },

    /* A question is a card, not a row: it is the one event kind that is waiting
       on the human for a sentence rather than for attention. Unanswered first
       and answered ones dropped — the answer is on the event and in the handoff
       thread, and a card that stays after it is answered reads as still open. */
    _questionCards() {
      const open = this.questions.filter(q => q.seq && !q.answer);
      if (!open.length) return "";
      return open.map(q => {
        const draft = this.drafts[String(q.seq)] || "";
        const who = q.seat || "director";
        return `<div class="nt-q">
          <div class="nt-q-top">
            <span class="nt-k warn">question</span>
            <span class="nt-item" style="color:${seatColor(who)}">${esc(who)}${
              q.item_id ? ` · #${q.item_id}` : ""}</span>
            <span class="nt-when">${esc(ago(q.asked_at))}</span>
          </div>
          <div class="nt-q-q">${esc(q.question)}</div>
          ${q.refs.length ? `<div class="nt-q-refs">${
            q.refs.map(r => `<code>${esc(trunc(r, 60))}</code>`).join(" ")}</div>` : ""}
          <textarea class="nt-q-reply" rows="2" data-nt-reply="${q.seq}"
            placeholder="answer it in a sentence - it is delivered to the agent that asked"
            >${esc(draft)}</textarea>
          <div class="nt-q-row">
            <span class="nt-q-hint">${q.item_id
              ? "a running agent gets this as a steer; a finished one gets a handoff note"
              : "filed as a decision the next session reads"}</span>
            <button class="nt-act primary" type="button" data-nt="answer"
                    data-nt-seq="${q.seq}">send</button>
          </div>
        </div>`;
      }).join("");
    },

    _rows() {
      if (!this.events.length) {
        return `<div class="nt-empty">nothing has happened yet — this fills up
          when an agent finishes, a chain hands off, or something needs you</div>`;
      }
      return this.events.slice(0, SHOW_MAX).map(e => {
        const m = meta(e.kind);
        const item = this.itemOf(e);
        const fresh = num(e.id) > this.readSeq;
        // A kind outside notify.kinds is still recorded and still listed; it
        // just did not ring. Marked so the drawer explains its own badge. An
        // EMPTY notify.kinds rings for nothing (the server counts it that way
        // too, and so does the router) — notify.in_app is the mute, this list is
        // which kinds count.
        const rings = this.notifyKinds.indexOf(e.kind) >= 0;
        const seat = this.seatOf(e);
        return `<button class="nt-row${fresh ? " nt-new" : ""}${rings ? "" : " quiet"}"
                type="button" ${item ? `data-nt="open" data-nt-item="${item}"` : ""}
                ${item ? `title="Open #${item}"` : ""}>
          <span class="nt-top">
            <span class="nt-dot ${m.tone}"></span>
            <span class="nt-k ${m.tone}">${esc(m.label)}</span>
            ${item ? `<span class="nt-item" style="color:${seatColor(seat)}">#${item}${
              seat ? ` ${esc(seat)}` : ""}</span>` : ""}
            <span class="nt-when">${esc(ago(e.created_at))}</span>
          </span>
          <span class="nt-line">${this.lineOf(e)}</span>
        </button>`;
      }).join("");
    },

    _foot() {
      const kinds = this.notifyKinds.length
        ? `ringing for ${this.notifyKinds.length} of ${this.vocabulary.length || 14} kinds`
        : "ringing for nothing - notify.kinds is empty";
      return `<div class="nt-foot">
        <span>${esc(kinds)}${this.inApp ? "" : " · muted (notify.in_app)"}</span>
        <span class="nt-honest">the bell only reaches you while this page is
          open — a webhook is the channel that survives a closed tab</span>
      </div>`;
    },

    /* ---- reading a payload ----------------------------------------------
     * Every branch tolerates a payload that is not the shape it expects. These
     * dicts are written by five different producers, one of them replaces an
     * oversized payload with a truncation marker, and a drawer that throws on a
     * surprising key is a drawer that goes blank exactly when something unusual
     * happened. */
    itemOf(e) {
      const p = (e && e.payload) || {};
      const head = p.head || {};
      const id = num(p.item) || num(head.item) || num(p.from) || num(p.item_id);
      if (id) return id;
      // `ref` is an item id for the item.* kinds and a chain id for the chain
      // ones; only a numeric ref is a link worth offering.
      const ref = num(e && e.ref);
      return ref || 0;
    },

    seatOf(e) {
      const p = (e && e.payload) || {};
      const head = p.head || {};
      return String(p.seat || head.seat || p.from_seat || "");
    },

    lineOf(e) {
      const p = (e && e.payload) || {};
      const kind = String((e && e.kind) || "");
      if (p._truncated) {
        return `<i>a payload too large to store - ${num(p.chars)} chars</i>`;
      }
      if (kind === "chain.advanced") {
        return `${esc(trunc(p.from_title || "a link", 44))} landed - `
          + `<b>#${num(p.to)} ${esc(p.to_seat || "next")}</b> is ready`
          + (num(p.waiting) > 1 ? ` · ${num(p.waiting) - 1} more behind it` : "");
      }
      if (kind === "chain.stalled") {
        // TWO producers write this kind. heartbeat.py sends {chain_id, head:{…}};
        // steerbox's stale-question reminder sends {question_seq, question} and no
        // head at all, and reading it with the chain branch renders "chain  has
        // not moved for 0m — #0 is stuck", which is a reminder about nothing.
        if (p.question_seq) {
          return `still waiting on your answer - `
            + esc(trunc(p.question || "a director question", 100));
        }
        const head = p.head || {};
        return `chain <b>${esc(trunc(p.chain_id || "", 24))}</b> has not moved for `
          + `${num(p.idle_min)}m - #${num(head.item)} ${esc(head.seat || "")} is `
          + `${esc(head.status || "stuck")}`
          + (p.reason ? ` · ${esc(trunc(p.reason, 90))}` : "");
      }
      if (kind === "item.aging") {
        return `<b>${esc(trunc(p.title || "an item", 44))}</b> has been waiting `
          + `${num(p.idle_min)}m for your approval`;
      }
      if (kind === "gate.mode") {
        return `sign-off is now <b>${esc(p.mode || "?")}</b>`
          + (p.previous ? ` (was ${esc(p.previous)})` : "")
          + (p.env_override ? ` · ${esc(trunc(p.env_override, 70))}` : "");
      }
      if (kind === "budget.refused") {
        return `<b>${esc(p.what || "a dispatch")}</b> was refused - `
          + esc(trunc(p.reason || "over a ceiling", 110));
      }
      if (kind === "director.question") {
        return esc(trunc(p.question || "the director asked you something", 120));
      }
      if (p.title) {
        const title = `<b>${esc(trunc(p.title, 48))}</b>`;
        const note = p.result ? ` - ${esc(trunc(p.result, 90))}` : "";
        const chain = p.chain_id ? ` · link ${num(p.chain_pos)}` : "";
        return title + chain + note;
      }
      if (p.question) return esc(trunc(p.question, 120));
      if (p.reason) return esc(trunc(p.reason, 120));
      const ref = String((e && e.ref) || "");
      return ref ? `ref ${esc(trunc(ref, 60))}` : "&mdash;";
    },

    _warn(e) { try { console.warn("[notify]", e); } catch (_) { } },

  };

  window.Notify = Notify;
})();
