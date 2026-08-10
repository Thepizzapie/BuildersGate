/* Cinematic seat workspace — shot lists, generated shots, the assembled cut.
 *
 * Two modes:
 *   plan   — the SHOT LIST. The only free surface in this pipeline and the one
 *            that matters most: eight shots is eight paid generations, so an
 *            argument about shot 3 costs nothing here and a re-generation
 *            afterwards. Style lives here too, on the sequence, because a
 *            cutscene rendered half in one look does not cut together.
 *   takes  — generated clips awaiting a decision, and what has been kept.
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
    label: "Cinematic",
    glyph: BGICON("cinematic"),
    _bg: null,
    _root: null,
    _mode: "plan",
    _opts: null,        // /api/cinematic/options — models, styles, encoder
    _seqs: [],          // every shot list, newest first
    _seq: null,         // the open sequence, with its shots
    _cands: [],
    _kept: [],
    _jobs: [],
    _busy: false,       // a mutation is in flight; do not repaint over it
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
        }
        if (this._mode === "takes") {
          const c = (await bg.get("/api/cinematic/candidates")).data || {};
          this._cands = c.candidates || [];
          this._kept = c.kept || [];
        }
        this._jobs = ((await bg.get("/api/cinematic/jobs")).data || {}).jobs || [];
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

    _paint() {
      const bg = this._bg;
      const host = this._root && this._root.querySelector(".bg-cine");
      if (!host) return;
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
        this._tab("plan", "Shot lists") +
        this._tab("takes", "Takes") +
        "</div>";

      host.innerHTML = banner + tabs +
        '<div class="bg-cine-body"></div>' + this._jobsHtml();

      host.querySelectorAll("[data-mode]").forEach((b) => {
        b.onclick = () => { this._mode = b.dataset.mode; this._load(); };
      });

      const body = host.querySelector(".bg-cine-body");
      if (this._mode === "takes") this._paintTakes(body);
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
          bg.esc(s.name) + "</button> — " + bg.esc(s.style_label || "unstyled") +
          ", " + (s.shot_count || 0) + " shot(s), " + (s.runtime_s || 0) + "s, " +
          (s.kept || 0) + " kept <span class=\"bg-dim\">" +
          bg.esc(s.status || "") + "</span></li>").join("");
        body.innerHTML =
          "<p class=\"bg-note\">A cutscene is a SEQUENCE of shots — no model " +
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
        " — " + bg.esc(look.text || "") + "</p>" +
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
            "assembled — a cut around a missing beat ships a story that does " +
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
        '<input name="style_note" placeholder="style note — your own wording" ' +
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
      if (!live.length) return "";
      return '<div class="bg-jobs">' + live.map((j) =>
        '<div>' + bg.esc(j.kind || "job") + " " +
        bg.esc(j.sequence || "") + (j.idx ? " shot " + j.idx : "") + " — " +
        bg.esc(j.stage || j.status || "") +
        (j.orphaned ? ' <span class="bg-warn">orphaned (the dashboard ' +
          "restarted; this will never move)</span>" : "") +
        "</div>").join("") + "</div>";
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
      this._load();
    },
  };

  window.SeatWS.cinematic = CINE;
})();
