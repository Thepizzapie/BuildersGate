/* Cinematic seat workspace — shot lists, generated shots, the assembled cut.
 *
 * Three modes:
 *   board  — the STORYBOARD, and the first surface anyone should touch. A
 *            premise becomes a script and a beat per frame for a fraction of a
 *            cent; each frame is drawn as an IMAGE, which is two orders of
 *            magnitude cheaper than the video shot it exists to stop you buying
 *            blind. Nothing here bills the video provider. Promotion is the one
 *            button that changes that, and it is deliberately hard to hit by
 *            accident.
 *   plan   — the SHOT LIST. The only free surface in this pipeline and the one
 *            that matters most: eight shots is eight paid generations, so an
 *            argument about shot 3 costs nothing here and a re-generation
 *            afterwards. Style lives here too, on the sequence, because a
 *            cutscene rendered half in one look does not cut together.
 *   takes  — generated clips awaiting a decision, and what has been kept.
 *
 * WHY THE BOARD SHOWS WHERE EVERY FRAME CAME FROM. A frame a human drew and a
 * frame a model guessed are not the same evidence for spending video money, so
 * the badge on each card says which. Approving a generated frame believing a
 * person picked it is the mistake the source badge exists to prevent.
 *
 * WHY THE CARDS SAY "installed" AND NOT "approved". Godot plays Ogg Theora and
 * nothing else; the .mp4 a model returns produces NO import error, it is simply
 * never loaded, so a scene runs perfectly with a blank rectangle. Keeping a clip
 * TRANSCODES it, and `installed` is measured against the asset registry rather
 * than inferred, so a superseded take cannot claim to be the clip in the game.
 *
 * Contract (see _core.js): window.SeatWS.cinematic = { label, glyph, render, refresh }.
 * `bg` is window.BGWS. Never touch another seat's DOM; never throw uncaught.
 */
(function () {
  const BGICON = (n) => (window.BGIcon ? BGIcon(n, { size: 15 }) : "");
  window.SeatWS = window.SeatWS || {};

  const CINE = {
    // Display name only — the seat id, this file's name and every cinematic_*
    // tool stay as they are. See bgate_core/seats.py for why.
    label: "Video",
    glyph: BGICON("cinematic"),
    _bg: null,
    _root: null,
    _mode: "board",
    _opts: null,        // /api/cinematic/options — models, styles, encoder
    _seqs: [],          // every shot list, newest first
    _seq: null,         // the open sequence, with its shots
    _boards: [],        // every storyboard, newest first
    _board: null,       // the open board, with its frames
    _pins: [],          // /api/refs — the cast and style anchors to condition on
    _cands: [],
    _kept: [],
    _jobs: [],
    _stuck: [],         // paid generations nobody collected. See _stuckHtml.
    _est: null,         // the open sequence's bill, before any of it is bought
    _busy: false,       // a mutation is in flight; do not repaint over it
    _sig: "",           // what the panel already shows. See _signature().
    _painted: false,    // the FORM is built once — never repaint someone's typing

    render(container, bg) {
      this._bg = bg;
      this._root = container;
      this._painted = false;
      container.innerHTML = '<div class="bg-cine"></div>';
      this._load();
    },

    refresh() {
      if (this._busy) return;
      this._load({ quiet: true });
    },

    async _load(o) {
      const bg = this._bg;
      if (!bg) return;
      try {
        if (!this._opts) {
          this._opts = (await bg.get("/api/cinematic/options")).data || {};
        }
        const seqs = (await bg.get("/api/cinematic/sequences")).data || {};
        this._seqs = seqs.sequences || [];
        if (this._seq && this._seq.name) {
          const one = await bg.get(
            "/api/cinematic/sequences?name=" + encodeURIComponent(this._seq.name));
          this._seq = (one.data || {}).sequence || this._seq;
          const est = await bg.get(
            "/api/cinematic/estimate?name=" + encodeURIComponent(this._seq.name));
          this._est = est.data || null;
        }
        if (this._mode === "takes") {
          const c = (await bg.get("/api/cinematic/candidates")).data || {};
          this._cands = c.candidates || [];
          this._kept = c.kept || [];
        }
        if (this._mode === "board") {
          this._boards =
            ((await bg.get("/api/storyboard/boards")).data || {}).boards || [];
          this._pins = ((await bg.get("/api/refs")).refs) || [];
          if (this._board && this._board.name) {
            const one = await bg.get(
              "/api/storyboard/board/" + encodeURIComponent(this._board.name));
            this._board = one.data || this._board;
          }
        }
        const jd = (await bg.get("/api/cinematic/jobs")).data || {};
        this._jobs = jd.jobs || [];
        this._stuck = (jd.stuck && jd.stuck.shots) || jd.stuck || [];
        if (!Array.isArray(this._stuck)) this._stuck = [];
        if (this._mode === "board") {
          const sj = ((await bg.get("/api/storyboard/jobs")).data || {}).jobs || [];
          this._jobs = this._jobs.concat(sj);
        }
      } catch (e) {
        if (!(o && o.quiet)) this._fail(e);
        return;
      }
      this._paint();
    },

    _fail(e) {
      const host = this._root && this._root.querySelector(".bg-cine");
      if (host) {
        host.innerHTML =
          '<div class="bg-empty">could not reach the cutscene API: ' +
          this._bg.esc(String(e && e.message ? e.message : e)) + "</div>";
      }
    },

    /* WHAT THIS PANEL LOOKS LIKE RIGHT NOW, as a short string.
     *
     * The shell calls refresh() every three seconds and _paint() replaces
     * host.innerHTML, so the whole panel was being destroyed and rebuilt twenty
     * times a minute whether or not anything had changed. That is not merely
     * wasteful: it takes the <video> element with it, so a take reset to 0:00
     * every three seconds and could not be watched to the end — on a seat whose
     * entire review step is "watch the clip before you keep it". Typing in the
     * plan form died the same way.
     *
     * _core.js has solved this since it was written (SeatWork._sig); this panel
     * simply never adopted it. Only the fields a repaint would actually show
     * are in the signature — a timestamp that ticks on its own would defeat it. */
    _signature() {
      const seq = this._seq || {};
      const board = this._board || {};
      return [
        this._mode,
        (this._seqs || []).map((s) => s.name + ":" + (s.kept || 0)).join(","),
        seq.name + ":" + (seq.shots || []).map((s) =>
          s.idx + s.status + (s.artifact ? s.artifact.id : "")).join("."),
        (this._boards || []).map((b) => b.name + ":" + (b.frames || 0)).join(","),
        board.name + ":" + (board.frames || []).map((f) =>
          f.idx + f.status + (f.has_image ? "1" : "0")).join("."),
        (this._cands || []).map((c) => c.artifact_id).join(","),
        (this._kept || []).map((k) => k.artifact_id + (k.installed ? "i" : "")).join(","),
        (this._jobs || []).map((j) => j.id + ":" + j.status + ":" + (j.stage || "")).join(","),
        (this._stuck || []).map((s) => s.idx + ":" + s.state).join(","),
        this._est ? String(this._est.usd) + this._est.known : "",
      ].join("|");
    },

    _paint() {
      const bg = this._bg;
      const host = this._root && this._root.querySelector(".bg-cine");
      if (!host) return;

      // Nothing moved: leave the DOM, and whatever is playing or focused in it,
      // exactly where it is.
      const sig = this._signature();
      if (sig === this._sig && host.firstChild) return;
      this._sig = sig;
      const o = this._opts || {};
      const enc = o.encoder || {};

      // THE TWO AVAILABILITIES ARE SHOWN SEPARATELY because they fail
      // differently and the fix is different. A key buys a shot; an ffmpeg with
      // libtheora makes a bought shot playable. A single disabled button with
      // no reason is how a user spends an afternoon on the wrong problem.
      let banner = "";
      if (!o.provider_available) {
        banner += '<div class="bg-warn">No video provider configured. ' +
          bg.esc(o.reason || "set KIE_API_KEY in the project .env") + "</div>";
      }
      if (!enc.ok) {
        banner += '<div class="bg-warn">Shots can be generated but none can be ' +
          'delivered: ' + bg.esc(enc.reason || "no Ogg Theora encoder") + "</div>";
      }

      const tabs =
        '<div class="bg-tabs">' +
        this._tab("board", "Storyboard") +
        this._tab("plan", "Shot lists") +
        this._tab("takes", "Takes") +
        "</div>";

      host.innerHTML = banner + tabs +
        '<div class="bg-cine-body"></div>' + this._jobsHtml();

      host.querySelectorAll("[data-mode]").forEach((b) => {
        b.onclick = () => { this._mode = b.dataset.mode; this._load(); };
      });
      // Wired on the host, not the body: the unfinished-generations block sits
      // outside the tab body and must work from whichever tab is open.
      host.querySelectorAll("[data-rec2]").forEach((el) => {
        el.onclick = () => this._act("/api/cinematic/recover",
          { name: el.dataset.seq, idx: Number(el.dataset.rec2) });
      });

      const body = host.querySelector(".bg-cine-body");
      if (this._mode === "takes") this._paintTakes(body);
      else if (this._mode === "board") this._paintBoard(body);
      else this._paintPlan(body);
    },

    _tab(id, label) {
      return '<button class="bg-tab' + (this._mode === id ? " on" : "") +
        '" data-mode="' + id + '">' + this._bg.esc(label) + "</button>";
    },

    _paintPlan(body) {
      const bg = this._bg;
      const o = this._opts || {};

      if (!this._seq) {
        const rows = this._seqs.map((s) =>
          '<li><button class="bg-link" data-open="' + bg.esc(s.name) + '">' +
          bg.esc(s.name) + "</button> - " + bg.esc(s.style_label || "unstyled") +
          ", " + (s.shot_count || 0) + " shot(s), " + (s.runtime_s || 0) + "s, " +
          (s.kept || 0) + " kept <span class=\"bg-dim\">" +
          bg.esc(s.status || "") + "</span></li>").join("");
        body.innerHTML =
          "<p class=\"bg-note\">A cutscene is a SEQUENCE of shots - no model " +
          "generates past about 15 seconds. Planning is free; every shot after " +
          "it costs money. Argue with the list here.</p>" +
          (rows ? "<ul class=\"bg-list\">" + rows + "</ul>"
                : '<div class="bg-empty">no shot lists yet</div>') +
          this._planFormHtml();
        body.querySelectorAll("[data-open]").forEach((b) => {
          b.onclick = () => {
            this._seq = { name: b.dataset.open };
            this._load();
          };
        });
        this._wireForm(body);
        return;
      }

      const seq = this._seq;
      const look = seq.style_resolved || {};
      const shots = (seq.shots || []).map((s) => {
        const art = s.artifact || null;
        const can = s.status === "generated" || s.status === "kept";
        return '<li class="bg-shot"><b>' + s.idx + ". " + bg.esc(s.slug) +
          '</b> <span class="bg-dim">' + s.duration + "s · " +
          bg.esc(s.status) + "</span><br>" +
          '<span class="bg-dim">' + bg.esc(s.prompt || "") + "</span><br>" +
          (art && art.installed
            ? '<span class="bg-ok">installed ' + bg.esc(art.installed_path) + "</span> "
            : "") +
          '<button class="bg-btn" data-gen="' + s.idx + '">' +
          (can ? "re-generate" : "generate") + "</button> " +
          (can && art
            ? '<button class="bg-btn" data-keep="' + art.id + '">keep</button> ' +
              '<button class="bg-btn" data-drop="' + art.id + '">discard</button> '
            : "") +
          (s.task_id && !can
            ? '<button class="bg-btn" data-rec="' + s.idx + '">recover</button>'
            : "") +
          "</li>";
      }).join("");

      body.innerHTML =
        '<button class="bg-link" data-back="1">&larr; all shot lists</button>' +
        "<h3>" + bg.esc(seq.name) + "</h3>" +
        '<p class="bg-dim">' + bg.esc(seq.logline || "") + "</p>" +
        '<p class="bg-note"><b>Style:</b> ' + bg.esc(look.label || "unstyled") +
        " - " + bg.esc(look.text || "") + "</p>" +
        this._estimateHtml() +
        '<p class="bg-dim">model ' + bg.esc(seq.model || o.default_model || "") +
        " · " + bg.esc(seq.aspect_ratio) + " · " + bg.esc(seq.resolution) +
        " · " + (seq.runtime_s || 0) + "s · " + (seq.kept || 0) + "/" +
        (seq.shots || []).length + " kept</p>" +
        (seq.style_refs && seq.style_refs.length
          ? '<p class="bg-dim">style refs: ' +
            bg.esc(seq.style_refs.join(", ")) + "</p>"
          : "") +
        '<ul class="bg-list">' + shots + "</ul>" +
        (seq.ready_to_assemble
          ? '<button class="bg-btn primary" data-assemble="1">assemble the cut</button>'
          : '<p class="bg-dim">every shot has to be kept before the cut can be ' +
            "assembled - a cut around a missing beat ships a story that does " +
            "not make sense.</p>");

      const b = body.querySelector("[data-back]");
      if (b) b.onclick = () => { this._seq = null; this._load(); };
      body.querySelectorAll("[data-gen]").forEach((el) => {
        el.onclick = () => this._act("/api/cinematic/generate",
          { name: seq.name, idx: Number(el.dataset.gen) });
      });
      body.querySelectorAll("[data-keep]").forEach((el) => {
        el.onclick = () => this._act("/api/cinematic/keep",
          { artifact_id: Number(el.dataset.keep) });
      });
      body.querySelectorAll("[data-drop]").forEach((el) => {
        el.onclick = () => this._act("/api/cinematic/discard",
          { artifact_id: Number(el.dataset.drop) });
      });
      body.querySelectorAll("[data-rec]").forEach((el) => {
        el.onclick = () => this._act("/api/cinematic/recover",
          { name: seq.name, idx: Number(el.dataset.rec) });
      });
      const asm = body.querySelector("[data-assemble]");
      if (asm) asm.onclick = () => this._act("/api/cinematic/assemble",
        { name: seq.name });
    },

    /* What the open sequence costs to buy, printed where the argument about
     * whether shot 3 earns its place actually happens.
     *
     * AN UNKNOWN PRICE IS PRINTED AS UNKNOWN. The estimate deliberately leaves
     * unpriced shots OUT of the total and flags them, so the number here is
     * shown as a floor rather than a figure — a partial sum rendered as a
     * total is the same lie as a zero. */
    _estimateHtml() {
      const bg = this._bg;
      const e = this._est;
      if (!e || !e.shots) return "";
      const money = e.usd === null || e.usd === undefined
        ? "no price configured"
        : (e.known ? "~$" : "at least ~$") + Number(e.usd).toFixed(2);
      return '<p class="bg-note bg-cine-est"><b>Estimated to buy:</b> ' +
        money + " for " + e.shots + " shot(s)" +
        (e.credits ? " (~" + e.credits + " credits)" : "") +
        (e.known ? "" : ' <span class="bg-warn">' + bg.esc(e.basis || "") +
          "</span>") +
        '<br><span class="bg-dim">' + bg.esc(e.note || "") + "</span></p>";
    },

    _planFormHtml() {
      const bg = this._bg;
      const o = this._opts || {};
      const styles = o.styles || {};
      const opts = Object.keys(styles).map((k) =>
        '<option value="' + bg.esc(k) + '">' +
        bg.esc(styles[k].label || k) + "</option>").join("");
      const models = Object.keys(o.models || {}).map((k) =>
        '<option value="' + bg.esc(k) + '">' +
        bg.esc((o.models[k] || {}).label || k) + "</option>").join("");
      return '<form class="bg-form bg-cine-new">' +
        "<h4>New shot list</h4>" +
        '<input name="name" placeholder="sequence name" required> ' +
        '<input name="logline" placeholder="logline"> ' +
        '<select name="style">' + opts + "</select> " +
        '<select name="model">' + models + "</select><br>" +
        '<input name="style_note" placeholder="style note, your own wording" ' +
        'style="width:60%"><br>' +
        '<textarea name="shots" rows="5" style="width:100%" ' +
        'placeholder="one shot per line:  camera | action | seconds"></textarea>' +
        '<button class="bg-btn primary" type="submit">plan (free)</button>' +
        "</form>";
    },

    _wireForm(body) {
      const form = body.querySelector(".bg-cine-new");
      if (!form) return;
      form.onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(form);
        // "camera | action | seconds", one per line. Deliberately plain text:
        // the point of this surface is that a whole sequence can be typed and
        // argued with in one go, and a per-shot widget grid makes that slower
        // than the thing it is replacing.
        const shots = String(f.get("shots") || "").split("\n")
          .map((l) => l.trim()).filter(Boolean).map((line) => {
            const p = line.split("|").map((x) => x.trim());
            return p.length >= 2
              ? { camera: p[0], action: p[1], duration: Number(p[2]) || 5 }
              : { action: p[0], duration: 5 };
          });
        if (!shots.length) return;
        await this._act("/api/cinematic/plan", {
          name: f.get("name"), logline: f.get("logline"),
          style: f.get("style"), style_note: f.get("style_note"),
          model: f.get("model"), shots,
        });
      };
    },

    /* ---- storyboard ------------------------------------------------------
     *
     * The frame grid is the whole feature. Everything else on this tab exists
     * to get a picture into one of those cells: write it, draw it, drop it in,
     * or point at a pin. */

    _paintBoard(body) {
      const bg = this._bg;
      if (!this._board) { this._paintBoardList(body); return; }
      const b = this._board;
      const ready = b.ready || {};

      const cast = (b.cast_refs || []).length
        ? (b.cast_refs || []).map((n) =>
            '<span class="bg-chip">' + bg.esc(n) + "</span>").join(" ")
        : '<span class="bg-warn">no cast pinned. Every frame is drawn from ' +
          "prose alone and the look will wander between them.</span>";

      body.innerHTML =
        '<button class="bg-link" data-back="1">&larr; all storyboards</button>' +
        "<h3>" + bg.esc(b.name) + "</h3>" +
        (b.logline ? '<p class="bg-dim">' + bg.esc(b.logline) + "</p>" : "") +
        '<div class="bg-cine-cast">Cast: ' + cast + "</div>" +
        (b.script && b.script.prose
          ? "<details><summary>script</summary><pre>" +
            bg.esc(b.script.prose) + "</pre></details>"
          : "") +
        '<div class="bg-board-grid">' +
        (b.frames || []).map((f) => this._frameCard(f)).join("") +
        "</div>" +
        this._promoteHtml(ready) +
        this._frameAddHtml();

      this._wireBoard(body);
    },

    _frameCard(f) {
      const bg = this._bg;
      const cut = f.status === "cut";
      // The source badge is not decoration: it is the difference between a
      // frame a person chose and one a model guessed, and a shot gets bought
      // against it either way.
      const badge = f.has_image
        ? '<span class="bg-chip ' +
          (f.source === "generated" ? "bg-chip-gen" : "bg-chip-human") + '">' +
          bg.esc(f.source) + "</span>"
        : "";
      const art = f.has_image
        ? '<img class="bg-board-img" loading="lazy" src="' +
          bg.preview(f.image_path) + '" alt="frame ' + f.idx + '">'
        : f.missing_image
          ? '<div class="bg-board-img bg-warn">the file this frame pointed at ' +
            "is gone: " + bg.esc(f.missing_image) + "</div>"
          : '<div class="bg-board-img bg-board-empty" data-drop="' + f.idx +
            '">drop an image here, or draw it</div>';

      return '<figure class="bg-board-cell' + (cut ? " bg-board-cut" : "") +
        '" data-frame="' + f.idx + '">' +
        '<div class="bg-board-num">' + f.idx + " " + badge +
        '<span class="bg-dim"> ' + (f.duration || 5) + "s</span></div>" +
        art +
        "<figcaption>" +
        '<div class="bg-board-beat">' + bg.esc(f.beat || f.action || "") +
        "</div>" +
        (f.camera ? '<div class="bg-dim">' + bg.esc(f.camera) + "</div>" : "") +
        (f.dialogue ? "<div>&ldquo;" + bg.esc(f.dialogue) + "&rdquo;</div>" : "") +
        "</figcaption>" +
        '<div class="bg-board-acts">' +
        '<button class="bg-btn" data-draw="' + f.idx + '">' +
        (f.has_image ? "redraw" : "draw") + "</button> " +
        '<button class="bg-btn" data-up="' + f.idx + '">upload</button> ' +
        (f.has_image && f.status !== "approved"
          ? '<button class="bg-btn primary" data-ok="' + f.idx +
            '">approve</button> ' : "") +
        (f.status === "approved"
          ? '<span class="bg-ok">approved</span> ' : "") +
        (cut ? "" : '<button class="bg-btn" data-cut="' + f.idx + '">cut</button>') +
        "</div></figure>";
    },

    _promoteHtml(ready) {
      const bg = this._bg;
      const o = this._opts || {};
      const models = Object.keys(o.models || {}).map((k) =>
        '<option value="' + bg.esc(k) + '">' +
        bg.esc((o.models[k] || {}).label || k) + "</option>").join("");
      // THE ONE BUTTON HERE THAT SPENDS. Its blockers are printed rather than
      // the button being merely disabled: "you cannot do this" with no reason
      // is how somebody spends an afternoon on the wrong problem.
      return '<form class="bg-form bg-board-promote">' +
        "<h4>Promote to a shot list</h4>" +
        '<p class="bg-dim">Everything above is free. Every shot below this ' +
        "button is a paid generation.</p>" +
        (ready.promotable
          ? ""
          : '<div class="bg-warn">' +
            (ready.blockers || []).map((x) => bg.esc(x)).join("<br>") +
            "</div>") +
        '<input name="sequence_name" placeholder="sequence name (defaults to ' +
        'the board name)"> ' +
        '<select name="model">' + models + "</select> " +
        '<label class="bg-dim"><input type="checkbox" name="allow_unanchored"> ' +
        "promote anyway</label> " +
        '<button class="bg-btn primary" type="submit">promote</button>' +
        "</form>";
    },

    _frameAddHtml() {
      return '<form class="bg-form bg-board-add">' +
        '<input name="beat" placeholder="add a beat" style="width:40%"> ' +
        '<input name="camera" placeholder="camera"> ' +
        '<input name="duration" type="number" min="1" max="30" value="5" ' +
        'style="width:5em"> ' +
        '<button class="bg-btn" type="submit">add frame</button>' +
        "</form>";
    },

    _paintBoardList(body) {
      const bg = this._bg;
      const rows = this._boards.map((b) => {
        const done = (b.frame_status || {}).approved || 0;
        return '<li><button class="bg-link" data-open="' + bg.esc(b.name) +
          '">' + bg.esc(b.name) + "</button> " +
          '<span class="bg-dim">' + (b.frames || 0) + " frame(s), " + done +
          " approved, " + bg.esc(b.status) + "</span></li>";
      }).join("");

      body.innerHTML =
        (rows ? "<ul>" + rows + "</ul>"
              : '<div class="bg-empty">no storyboards yet. A board is where a ' +
                "scene gets worked out for free, before any of it is bought." +
                "</div>") +
        this._boardFormHtml();
      this._wireBoardList(body);
    },

    _boardFormHtml() {
      const bg = this._bg;
      const o = this._opts || {};
      const styles = o.styles || {};
      const sopts = Object.keys(styles).map((k) =>
        '<option value="' + bg.esc(k) + '">' +
        bg.esc(styles[k].label || k) + "</option>").join("");
      // Only character and style pins are offered: a UI pin or a concept sketch
      // conditions a storyboard frame toward the wrong thing entirely.
      const pins = (this._pins || [])
        .filter((p) => p.kind === "character" || p.kind === "style")
        .map((p) =>
          '<label class="bg-chip"><input type="checkbox" name="cast" value="' +
          bg.esc(p.name) + '"> ' + bg.esc(p.name) +
          ' <span class="bg-dim">' + bg.esc(p.kind) + "</span></label>")
        .join(" ");

      return '<form class="bg-form bg-board-new">' +
        "<h4>New storyboard</h4>" +
        '<input name="name" placeholder="scene name" required> ' +
        '<select name="style">' + sopts + "</select> " +
        '<input name="frames" type="number" min="1" max="24" value="6" ' +
        'style="width:5em" title="how many beats"><br>' +
        '<input name="style_note" placeholder="style note, your own wording" ' +
        'style="width:60%"><br>' +
        '<textarea name="premise" rows="3" style="width:100%" ' +
        'placeholder="what happens in this scene? one or two sentences"></textarea>' +
        '<div class="bg-board-pins">' +
        (pins ? "Cast and style anchors: " + pins
              : '<span class="bg-warn">No character or style references are ' +
                "pinned. Pin some first, or every frame will be drawn from " +
                "prose alone and drift between panels.</span>") +
        "</div>" +
        '<button class="bg-btn primary" type="submit" data-write="1">' +
        "write the script</button> " +
        '<span class="bg-dim">costs a fraction of a cent. Draws nothing.</span>' +
        "</form>";
    },

    _wireBoardList(body) {
      body.querySelectorAll("[data-open]").forEach((el) => {
        el.onclick = async () => {
          const r = await this._bg.get(
            "/api/storyboard/board/" + encodeURIComponent(el.dataset.open));
          this._board = r.data || null;
          this._paint();
        };
      });
      const form = body.querySelector(".bg-board-new");
      if (!form) return;
      form.onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(form);
        const cast = f.getAll("cast");
        const name = String(f.get("name") || "").trim();
        const premise = String(f.get("premise") || "").trim();
        if (!name) return;
        // No premise means there is nothing for a writer to work from, so the
        // board is created empty and hand-authored rather than a model being
        // paid to invent a scene the author did not ask for.
        const path = premise
          ? "/api/storyboard/script" : "/api/storyboard/plan";
        const payload = {
          name, style: f.get("style"), style_note: f.get("style_note"),
          cast_refs: cast,
        };
        if (premise) {
          payload.premise = premise;
          payload.frames = Number(f.get("frames")) || 6;
        } else {
          payload.frames = [{ beat: "open on" }];
        }
        await this._act(path, payload);
        const r = await this._bg.get(
          "/api/storyboard/board/" + encodeURIComponent(
            name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")));
        this._board = (r && r.data) || null;
        this._paint();
      };
    },

    _wireBoard(body) {
      const name = this._board.name;
      const back = body.querySelector("[data-back]");
      if (back) back.onclick = () => { this._board = null; this._paint(); };

      body.querySelectorAll("[data-draw]").forEach((el) => {
        el.onclick = () => this._act("/api/storyboard/frame/generate",
          { name, idx: Number(el.dataset.draw) });
      });
      body.querySelectorAll("[data-ok]").forEach((el) => {
        el.onclick = () => this._act("/api/storyboard/frame/set",
          { name, idx: Number(el.dataset.ok), status: "approved" });
      });
      body.querySelectorAll("[data-cut]").forEach((el) => {
        el.onclick = () => this._act("/api/storyboard/frame/cut",
          { name, idx: Number(el.dataset.cut) });
      });
      body.querySelectorAll("[data-up]").forEach((el) => {
        el.onclick = () => this._pickFile(name, Number(el.dataset.up));
      });

      // Drop straight onto an empty cell. The same upload path as the button,
      // because a storyboard is a thing people drag pictures into.
      body.querySelectorAll(".bg-board-cell").forEach((cell) => {
        const idx = Number(cell.dataset.frame);
        cell.ondragover = (ev) => { ev.preventDefault(); cell.classList.add("bg-drop"); };
        cell.ondragleave = () => cell.classList.remove("bg-drop");
        cell.ondrop = (ev) => {
          ev.preventDefault();
          cell.classList.remove("bg-drop");
          const file = ev.dataTransfer && ev.dataTransfer.files &&
            ev.dataTransfer.files[0];
          if (file) this._upload(name, idx, file);
        };
      });

      const add = body.querySelector(".bg-board-add");
      if (add) add.onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(add);
        if (!String(f.get("beat") || "").trim()) return;
        await this._act("/api/storyboard/frame/add", {
          name, beat: f.get("beat"), camera: f.get("camera"),
          duration: Number(f.get("duration")) || 5,
        });
      };

      const promote = body.querySelector(".bg-board-promote");
      if (promote) promote.onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(promote);
        if (!window.confirm(
            "Promote " + name + " to a shot list? Every shot in it is a paid " +
            "generation once you start buying them.")) return;
        await this._act("/api/storyboard/promote", {
          name, sequence_name: f.get("sequence_name"), model: f.get("model"),
          allow_unanchored: !!f.get("allow_unanchored"),
        });
      };
    },

    _pickFile(name, idx) {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "image/png,image/jpeg,image/webp,image/gif";
      input.onchange = () => {
        if (input.files && input.files[0]) this._upload(name, idx, input.files[0]);
      };
      input.click();
    },

    _upload(name, idx, file) {
      const reader = new FileReader();
      reader.onload = () => {
        this._act("/api/storyboard/frame/upload",
          { name, idx, data: String(reader.result || "") });
      };
      reader.readAsDataURL(file);
    },

    _paintTakes(body) {
      const bg = this._bg;
      const card = (c) =>
        '<li><b>' + bg.esc(c.logical_name) + "</b> r" + c.revision +
        ' <span class="bg-dim">' + bg.esc(c.kind || "shot") +
        (c.duration_s ? " · " + c.duration_s + "s" : "") + "</span><br>" +
        '<video controls preload="metadata" width="320" src="' +
        bg.preview(c.path) + '"></video><br>' +
        (c.installed
          ? '<span class="bg-ok">installed ' + bg.esc(c.installed_path) + "</span>"
          : c.install_stale
            ? '<span class="bg-warn">superseded — another take is the one in ' +
              "the game</span>"
            : c.install_missing
              ? '<span class="bg-warn">approved but the file is gone from the ' +
                "engine project</span>"
              : "") +
        '<br><button class="bg-btn" data-keep="' + c.artifact_id + '">keep</button> ' +
        '<button class="bg-btn" data-drop="' + c.artifact_id + '">discard</button> ' +
        '<button class="bg-btn" data-inst="' + c.artifact_id + '">re-install</button>' +
        "</li>";

      body.innerHTML =
        "<h4>Awaiting a decision</h4>" +
        (this._cands.length
          ? '<ul class="bg-list">' + this._cands.map(card).join("") + "</ul>"
          : '<div class="bg-empty">nothing waiting</div>') +
        "<h4>Kept</h4>" +
        (this._kept.length
          ? '<ul class="bg-list">' + this._kept.map(card).join("") + "</ul>"
          : '<div class="bg-empty">nothing kept yet</div>');

      body.querySelectorAll("[data-keep]").forEach((el) => {
        el.onclick = () => this._act("/api/cinematic/keep",
          { artifact_id: Number(el.dataset.keep) });
      });
      body.querySelectorAll("[data-drop]").forEach((el) => {
        el.onclick = () => this._act("/api/cinematic/discard",
          { artifact_id: Number(el.dataset.drop) });
      });
      body.querySelectorAll("[data-inst]").forEach((el) => {
        el.onclick = () => this._act("/api/cinematic/install",
          { artifact_id: Number(el.dataset.inst) });
      });
    },

    _jobsHtml() {
      const bg = this._bg;
      const live = (this._jobs || []).filter((j) =>
        j.status && j.status !== "done" && j.status !== "failed" &&
        j.status !== "cancelled");

      // A JOB THAT FAILS IN A SECOND WAS INVISIBLE. Only non-terminal jobs were
      // listed, so clicking generate on a shot that gets refused up front —
      // missing anchor, dead budget, no encoder — queued a job, failed it, and
      // filtered it out before the next repaint. The button read as dead and
      // the honest refusal the server had written was never shown to anyone.
      const broke = (this._jobs || [])
        .filter((j) => j.status === "failed")
        .slice(-3);
      const failed = broke.length
        ? '<div class="bg-warn"><b>Refused</b>' + broke.map((j) =>
            "<div>" + bg.esc(j.kind || "job") +
            (j.idx ? " shot " + j.idx : "") + ": " +
            bg.esc(this._why(j)) + "</div>").join("") + "</div>"
        : "";
      if (!live.length) return failed + this._stuckHtml();
      // ORPHANED IS THREE-STATE, not a boolean, and rendering it as one is how
      // a paid job that nobody could classify got shown as healthy. true means
      // it started before this process and will never move; null means its
      // timestamp could not be read, which is not a clean bill of health.
      const orphan = (j) => {
        if (j.orphaned === true) {
          return ' <span class="bg-warn">orphaned (the dashboard restarted; ' +
            "this will never move)</span>";
        }
        if (j.orphaned === null || j.orphaned === undefined) {
          return ' <span class="bg-warn">could not tell whether this is still ' +
            "alive: " + bg.esc(j.orphan_reason || "its timestamp did not parse") +
            "</span>";
        }
        return "";
      };
      return failed + '<div class="bg-jobs">' + live.map((j) =>
        '<div>' + bg.esc(j.kind || "job") + " " +
        bg.esc(j.sequence || "") + (j.idx ? " shot " + j.idx : "") + " - " +
        bg.esc(j.stage || j.status || "") + orphan(j) +
        "</div>").join("") + "</div>" + this._stuckHtml();
    },

    /* The server writes a sentence explaining every refusal. It is buried two
     * levels down in the job record, and showing "failed" instead of it is how
     * a fixable problem reads as a broken button. */
    _why(j) {
      const r = j.result || {};
      return j.error || r.error || j.stage || j.status || "no reason recorded";
    },

    /* Shots that were PAID FOR and never collected. Shown next to the live
     * jobs because that is where somebody looks when a generation "failed" —
     * and the wrong move at that moment is to press generate again, which pays
     * a second time for a clip the provider is already holding. */
    _stuckHtml() {
      const bg = this._bg;
      const rows = (this._stuck || []).filter((s) => s.state !== "running");
      if (!rows.length) return "";
      const line = (s) =>
        '<div>' + bg.esc(s.sequence || "") + " shot " + s.idx + ": " +
        (s.state === "recoverable"
          ? '<b class="bg-warn">already paid for and waiting</b> - recover it ' +
            "rather than generating again"
          : bg.esc(s.state === "lost"
              ? "submit never returned a task id, so probably nothing was charged"
              : s.state)) +
        (s.state === "recoverable"
          ? ' <button class="bg-btn" data-rec2="' + s.idx +
            '" data-seq="' + bg.esc(s.sequence || "") + '">recover</button>'
          : "") +
        "</div>";
      return '<div class="bg-jobs bg-warn"><b>Unfinished generations</b>' +
        rows.map(line).join("") + "</div>";
    },

    /* Every mutation goes through here so `_busy` is set in exactly one place.
     * Without it the 3s refresh repaints mid-click and the button a human is
     * pressing is replaced by a new one — which reads as a dead button and is
     * answered by clicking again, i.e. by buying a second shot. */
    async _act(path, body) {
      if (this._busy) return;
      this._busy = true;
      try {
        const r = await this._bg.post(path, body);
        if (r && r.error) {
          window.alert(String(r.error));
        } else if (r && r.data && r.data.warnings) {
          window.alert(r.data.warnings.join("\n\n"));
        }
      } catch (e) {
        window.alert(String(e && e.message ? e.message : e));
      } finally {
        this._busy = false;
      }
      // A deliberate action always repaints, even if the signature happens to
      // land the same — a click that visibly does nothing is the complaint that
      // started this whole thread.
      this._sig = "";
      this._load();
    },
  };

  window.SeatWS.cinematic = CINE;
})();
