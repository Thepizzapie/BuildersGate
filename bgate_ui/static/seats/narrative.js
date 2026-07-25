/* Narrative seat workspace — a storyboarding canvas.
 *
 * A pannable board of draggable story panels (title + beat + optional image)
 * wired together with SVG arrows to show story order / branches. State is a
 * single JSON doc persisted through the generic per-seat workspace store:
 *   GET  /api/workspace/narrative/storyboard  -> {data:{panels,edges}}
 *   POST /api/workspace/narrative/storyboard  {data:{...}}
 *
 * MODULE CONTRACT (see _core.js). Vanilla JS only, no deps. Never throws.
 */
(function () {
  "use strict";

  var CANVAS_W = 5000, CANVAS_H = 3600;   // logical board size (scrollable)
  var PANEL_W = 224;                       // fixed panel width for edge math

  // Starter presets — an empty canvas must offer a first move, not a shrug.
  // Each is a panel kind (shown in the header badge) + seed content.
  var PRESETS = {
    beat:    { title: "New beat", text: "" },
    outline: { title: "Story outline",
               text: "Act 1 — setup:\nAct 2 — escalation:\nAct 3 — payoff:" },
    lore:    { title: "Lore: topic",
               text: "Canon fact:\nWhy it matters to the story:" },
    note:    { title: "Note", text: "" }
  };
  var HINT_EMPTY = "empty board — click “+ Panel” or pick a starter below";
  var HINT_READY = "drag a panel header to move · scroll / drag empty canvas to pan";

  var N = {
    label: "Narrative",
    glyph: "¶",
    _bg: null,
    _root: null,          // container el
    _state: null,         // {panels:[], edges:[]}
    _selected: null,      // panel id
    _linkMode: false,
    _linkSrc: null,       // pending edge source id
    _counter: 0,
    _saveTimer: null,
    _drag: null,          // active drag context

    render: function (container, bg) {
      try {
        this._bg = bg;
        this._root = container;
        this._selected = null;
        this._linkMode = false;
        this._linkSrc = null;
        this._buildShell();
        this._load();
      } catch (e) {
        try {
          container.innerHTML = '<div class="empty">narrative workspace failed to start: ' +
            (bg && bg.esc ? bg.esc(e.message) : e.message) + "</div>";
        } catch (e2) {}
        console.error("narrative.render", e);
      }
    },

    // Called ~every 3s while the seat is active. Deliberately a no-op so it can
    // never clobber in-progress typing / dragging with a server reload.
    refresh: function () {},

    /* ---- shell / static DOM ------------------------------------------- */
    _buildShell: function () {
      var bg = this._bg;
      this._root.innerHTML =
        '<style>' + this._css() + "</style>" +
        '<div class="nar-wrap">' +
          '<div class="nar-toolbar">' +
            '<button class="nar-btn nar-primary" data-act="add">+ Panel</button>' +
            '<button class="nar-btn" data-act="link">→ Link mode</button>' +
            '<button class="nar-btn nar-danger" data-act="del">✕ Delete</button>' +
            '<span class="nar-sep"></span>' +
            '<button class="nar-btn" data-act="save">↺ Save</button>' +
            '<span class="nar-status" id="nar-status"></span>' +
            '<span class="nar-hint" id="nar-hint">drag a panel header to move · scroll / drag empty canvas to pan</span>' +
          "</div>" +
          '<div class="nar-scroll" id="nar-scroll">' +
            '<div class="nar-canvas" id="nar-canvas" style="width:' + CANVAS_W + "px;height:" + CANVAS_H + 'px">' +
              '<svg class="nar-svg" id="nar-svg" width="' + CANVAS_W + '" height="' + CANVAS_H + '">' +
                '<defs><marker id="nar-arrow" viewBox="0 0 10 10" refX="9" refY="5" ' +
                  'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
                  '<path d="M0,0 L10,5 L0,10 z" fill="#3b7f9e"></path></marker></defs>' +
              "</svg>" +
              '<div class="nar-empty" id="nar-empty" hidden>' +
                '<div class="nar-empty-inner">' +
                  '<div class="nar-empty-glyph">¶</div>' +
                  "<p>No panels yet — storyboard the game's beats here.</p>" +
                  '<p class="nar-empty-sub">Panels are draggable cards (title + text + optional image); Link mode draws the arrows between them. Start with:</p>' +
                  '<div class="nar-presets">' +
                    '<button class="nar-btn nar-primary" data-act="add" data-kind="beat">+ Beat</button>' +
                    '<button class="nar-btn" data-act="add" data-kind="outline">+ Outline</button>' +
                    '<button class="nar-btn" data-act="add" data-kind="lore">+ Lore</button>' +
                    '<button class="nar-btn" data-act="add" data-kind="note">+ Note</button>' +
                  "</div>" +
                "</div>" +
              "</div>" +
            "</div>" +
          "</div>" +
        "</div>";

      var self = this;
      // Toolbar / empty-state buttons (event-delegated so re-render is safe).
      this._root.addEventListener("click", function (e) {
        var b = e.target.closest ? e.target.closest("[data-act]") : null;
        if (!b || !self._root.contains(b)) return;
        var act = b.getAttribute("data-act");
        if (act === "add") self._addPanel(b.getAttribute("data-kind") || "beat");
        else if (act === "link") self._toggleLink();
        else if (act === "del") self._deleteSelected();
        else if (act === "save") self._save(true);
      });

      // Pan by dragging empty canvas; click empty to deselect.
      var scroll = this._root.querySelector("#nar-scroll");
      var canvas = this._root.querySelector("#nar-canvas");
      canvas.addEventListener("mousedown", function (e) {
        if (e.target !== canvas && e.target.id !== "nar-svg") return; // only bare canvas
        if (self._linkMode) { self._setLinkSrc(null); return; }
        self._select(null);
        self._startPan(e, scroll);
      });
    },

    /* ---- load / save -------------------------------------------------- */
    _load: function () {
      var self = this;
      this._bg.get("/api/workspace/narrative/storyboard")
        .then(function (r) { self._adopt((r && r.data) || {}); })
        .catch(function (e) {
          console.warn("narrative load", e);
          self._adopt({});
          self._setStatus("offline — starting empty", true);
        });
    },

    _adopt: function (data) {
      var panels = Array.isArray(data.panels) ? data.panels : [];
      var edges = Array.isArray(data.edges) ? data.edges : [];
      // sanitise + seed the id counter above any existing numeric suffix
      var clean = [];
      for (var i = 0; i < panels.length; i++) {
        var p = panels[i] || {};
        if (!p.id) continue;
        clean.push({
          id: String(p.id),
          kind: PRESETS[p.kind] ? String(p.kind) : "beat",
          title: p.title == null ? "" : String(p.title),
          text: p.text == null ? "" : String(p.text),
          img: p.img ? String(p.img) : "",
          x: this._num(p.x, 60 + i * 40),
          y: this._num(p.y, 60 + i * 30)
        });
        var m = /(\d+)/.exec(String(p.id));
        if (m) this._counter = Math.max(this._counter, parseInt(m[1], 10) || 0);
      }
      var ids = {};
      for (var j = 0; j < clean.length; j++) ids[clean[j].id] = true;
      var cleanEdges = [];
      for (var k = 0; k < edges.length; k++) {
        var e = edges[k] || {};
        if (ids[e.from] && ids[e.to] && e.from !== e.to) {
          cleanEdges.push({ from: String(e.from), to: String(e.to) });
        }
      }
      this._state = { panels: clean, edges: cleanEdges };
      this._renderAll();
    },

    _serialize: function () {
      return { panels: this._state.panels, edges: this._state.edges };
    },

    _save: function (explicit) {
      var self = this;
      if (!this._state) return;
      this._setStatus("saving…");
      this._bg.post("/api/workspace/narrative/storyboard", { data: this._serialize() })
        .then(function (r) {
          if (r && (r.ok || r.seat)) self._setStatus("saved " + self._clock());
          else { self._setStatus("save failed", true); if (explicit) self._bg.toast("save failed", true); }
        })
        .catch(function (e) {
          console.warn("narrative save", e);
          self._setStatus("save failed", true);
          if (explicit) self._bg.toast("save failed", true);
        });
    },

    _autosave: function () {
      var self = this;
      if (this._saveTimer) clearTimeout(this._saveTimer);
      this._setStatus("unsaved…");
      this._saveTimer = setTimeout(function () { self._save(false); }, 1000);
    },

    /* ---- state mutations --------------------------------------------- */
    _addPanel: function (kind) {
      if (!this._state) return;
      var preset = PRESETS[kind] || PRESETS.beat;
      var sc = this._root.querySelector("#nar-scroll");
      // drop it roughly in the current viewport, staggered
      var x = (sc ? sc.scrollLeft : 0) + 60 + (this._state.panels.length % 5) * 30;
      var y = (sc ? sc.scrollTop : 0) + 60 + (this._state.panels.length % 5) * 24;
      var id = "p" + (++this._counter) + "-" + Date.now().toString(36);
      this._state.panels.push({ id: id, kind: PRESETS[kind] ? kind : "beat",
        title: preset.title, text: preset.text, img: "", x: x, y: y });
      this._renderAll();
      this._select(id);
      this._autosave();
      var node = this._panelNode(id);
      if (node) { var t = node.querySelector(".nar-title"); if (t) { t.focus(); this._selectAll(t); } }
    },

    _deleteSelected: function () {
      if (!this._selected) { this._bg.toast("select a panel first", true); return; }
      var id = this._selected;
      this._state.panels = this._state.panels.filter(function (p) { return p.id !== id; });
      this._state.edges = this._state.edges.filter(function (e) { return e.from !== id && e.to !== id; });
      this._selected = null;
      if (this._linkSrc === id) this._linkSrc = null;
      this._renderAll();
      this._autosave();
    },

    _toggleLink: function () {
      this._linkMode = !this._linkMode;
      this._linkSrc = null;
      this._root.classList.toggle("nar-linking", this._linkMode);
      var btn = this._root.querySelector('[data-act="link"]');
      if (btn) btn.classList.toggle("nar-active", this._linkMode);
      this._setHint(this._linkMode
        ? "LINK MODE: click a source panel, then a target"
        : this._contextHint());
      this._paintLinkState();
    },

    _panelClicked: function (id) {
      if (this._linkMode) {
        if (!this._linkSrc) { this._setLinkSrc(id); }
        else if (this._linkSrc === id) { this._setLinkSrc(null); }
        else { this._addEdge(this._linkSrc, id); this._setLinkSrc(null); }
        return;
      }
      this._select(id);
    },

    _setLinkSrc: function (id) {
      this._linkSrc = id;
      this._setHint(id ? "source set — click a target panel (or the source again to cancel)"
                       : "LINK MODE: click a source panel, then a target");
      this._paintLinkState();
    },

    _addEdge: function (from, to) {
      for (var i = 0; i < this._state.edges.length; i++) {
        var e = this._state.edges[i];
        if (e.from === from && e.to === to) { this._bg.toast("already linked", true); return; }
      }
      this._state.edges.push({ from: from, to: to });
      this._drawEdges();
      this._autosave();
    },

    _select: function (id) {
      this._selected = id;
      var nodes = this._root.querySelectorAll(".nar-panel");
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].classList.toggle("nar-sel", nodes[i].getAttribute("data-id") === id);
      }
    },

    /* ---- rendering ---------------------------------------------------- */
    _renderAll: function () {
      var canvas = this._root.querySelector("#nar-canvas");
      var empty = this._root.querySelector("#nar-empty");
      if (!canvas) return;
      // wipe existing panels (keep svg + empty node)
      var old = canvas.querySelectorAll(".nar-panel");
      for (var i = 0; i < old.length; i++) old[i].remove();
      var panels = this._state.panels;
      if (empty) empty.hidden = panels.length > 0;
      for (var j = 0; j < panels.length; j++) canvas.appendChild(this._buildPanel(panels[j]));
      this._drawEdges();
      this._paintLinkState();
      if (!this._linkMode) this._setHint(this._contextHint());
    },

    // The toolbar hint must describe what the user can DO NOW: an empty board
    // has nothing to drag, so don't tell them to drag.
    _contextHint: function () {
      return (this._state && this._state.panels.length) ? HINT_READY : HINT_EMPTY;
    },

    _buildPanel: function (p) {
      var bg = this._bg, self = this;
      var node = document.createElement("div");
      node.className = "nar-panel" + (p.id === this._selected ? " nar-sel" : "");
      node.setAttribute("data-id", p.id);
      node.style.left = p.x + "px";
      node.style.top = p.y + "px";
      node.innerHTML =
        '<div class="nar-head" title="drag to move">' +
          '<span class="nar-grip">☰</span>' +
          '<span class="nar-badge">' + bg.esc(p.kind || "beat") + "</span>" +
        "</div>" +
        '<input class="nar-title" value="' + bg.esc(p.title) + '" placeholder="Title…" spellcheck="false">' +
        '<div class="nar-imgwrap">' + this._imgHtml(p) + "</div>" +
        '<textarea class="nar-text" placeholder="Describe the beat…" spellcheck="false">' +
          bg.esc(p.text) + "</textarea>";

      // select / link on mousedown anywhere on the panel
      node.addEventListener("mousedown", function (e) {
        if (self._linkMode) { e.preventDefault(); return; }
        self._select(p.id);
      });
      // link click resolves on click (so a drag doesn't trigger it)
      node.addEventListener("click", function (e) {
        if (self._linkMode) { e.preventDefault(); e.stopPropagation(); self._panelClicked(p.id); }
      });

      // drag from the header only
      var head = node.querySelector(".nar-head");
      head.addEventListener("mousedown", function (e) {
        if (self._linkMode) return;
        e.preventDefault();
        self._startDrag(e, p, node);
      });

      // editable fields — update state + autosave, never start a drag
      var title = node.querySelector(".nar-title");
      title.addEventListener("mousedown", function (e) { e.stopPropagation(); });
      title.addEventListener("input", function () { p.title = title.value; self._autosave(); });

      var text = node.querySelector(".nar-text");
      text.addEventListener("mousedown", function (e) { e.stopPropagation(); });
      text.addEventListener("input", function () { p.text = text.value; self._autosave(); });

      // image actions (delegated within panel)
      node.addEventListener("click", function (e) {
        var a = e.target.closest ? e.target.closest("[data-img]") : null;
        if (!a || self._linkMode) return;
        e.stopPropagation();
        var act = a.getAttribute("data-img");
        if (act === "pick") self._openPicker(p);
        else if (act === "clear") { p.img = ""; self._refreshImg(node, p); self._autosave(); }
      });

      return node;
    },

    _imgHtml: function (p) {
      var bg = this._bg;
      if (p.img) {
        return '<img class="nar-thumb" src="' + bg.preview(p.img) + '" alt="" ' +
          'onerror="this.classList.add(\'nar-broken\');this.removeAttribute(\'src\')">' +
          '<div class="nar-imgbar">' +
            '<button class="nar-mini" data-img="pick">change</button>' +
            '<button class="nar-mini" data-img="clear">remove</button>' +
          "</div>";
      }
      return '<button class="nar-pick" data-img="pick">⬚ pick image</button>';
    },

    _refreshImg: function (node, p) {
      var wrap = node.querySelector(".nar-imgwrap");
      if (wrap) wrap.innerHTML = this._imgHtml(p);
    },

    _drawEdges: function () {
      var svg = this._root.querySelector("#nar-svg");
      if (!svg) return;
      // clear everything except <defs>
      var kids = [].slice.call(svg.childNodes);
      for (var i = 0; i < kids.length; i++) {
        if (kids[i].nodeName && kids[i].nodeName.toLowerCase() !== "defs") svg.removeChild(kids[i]);
      }
      var pos = {};
      for (var j = 0; j < this._state.panels.length; j++) {
        var p = this._state.panels[j];
        var node = this._panelNode(p.id);
        var h = node ? node.offsetHeight || 150 : 150;
        pos[p.id] = { cx: p.x + PANEL_W / 2, cy: p.y + h / 2, w: PANEL_W, h: h, x: p.x, y: p.y };
      }
      var NS = "http://www.w3.org/2000/svg";
      for (var k = 0; k < this._state.edges.length; k++) {
        var e = this._state.edges[k];
        var a = pos[e.from], b = pos[e.to];
        if (!a || !b) continue;
        var s = this._edgePoint(a, b.cx, b.cy);
        var t = this._edgePoint(b, a.cx, a.cy);
        var line = document.createElementNS(NS, "line");
        line.setAttribute("x1", s.x); line.setAttribute("y1", s.y);
        line.setAttribute("x2", t.x); line.setAttribute("y2", t.y);
        line.setAttribute("class", "nar-edge");
        line.setAttribute("marker-end", "url(#nar-arrow)");
        svg.appendChild(line);
      }
    },

    // point on the border of rect r in the direction of (tx,ty)
    _edgePoint: function (r, tx, ty) {
      var dx = tx - r.cx, dy = ty - r.cy;
      if (dx === 0 && dy === 0) return { x: r.cx, y: r.cy };
      var hw = r.w / 2, hh = r.h / 2;
      var scale = Math.min(
        Math.abs(dx) > 0.0001 ? hw / Math.abs(dx) : Infinity,
        Math.abs(dy) > 0.0001 ? hh / Math.abs(dy) : Infinity
      );
      return { x: r.cx + dx * scale, y: r.cy + dy * scale };
    },

    _paintLinkState: function () {
      var nodes = this._root.querySelectorAll(".nar-panel");
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].classList.toggle("nar-linksrc",
          this._linkMode && nodes[i].getAttribute("data-id") === this._linkSrc);
      }
    },

    /* ---- dragging ----------------------------------------------------- */
    _startDrag: function (ev, p, node) {
      var self = this;
      var startX = ev.clientX, startY = ev.clientY;
      var origX = p.x, origY = p.y;
      node.classList.add("nar-dragging");
      var moved = false;

      function onMove(e) {
        var nx = origX + (e.clientX - startX);
        var ny = origY + (e.clientY - startY);
        nx = Math.max(0, Math.min(CANVAS_W - 40, nx));
        ny = Math.max(0, Math.min(CANVAS_H - 40, ny));
        p.x = nx; p.y = ny;
        node.style.left = nx + "px";
        node.style.top = ny + "px";
        moved = true;
        self._drawEdges();
      }
      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        node.classList.remove("nar-dragging");
        if (moved) self._autosave();
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },

    _startPan: function (ev, scroll) {
      var startX = ev.clientX, startY = ev.clientY;
      var origL = scroll.scrollLeft, origT = scroll.scrollTop;
      scroll.classList.add("nar-panning");
      function onMove(e) {
        scroll.scrollLeft = origL - (e.clientX - startX);
        scroll.scrollTop = origT - (e.clientY - startY);
      }
      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        scroll.classList.remove("nar-panning");
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },

    /* ---- image picker ------------------------------------------------- */
    _openPicker: function (p) {
      var self = this, bg = this._bg;
      var overlay = document.createElement("div");
      overlay.className = "nar-modal";
      overlay.innerHTML =
        '<div class="nar-modal-box">' +
          '<div class="nar-modal-head"><b>Attach an image</b>' +
            '<button class="nar-mini" data-close="1">close</button></div>' +
          '<div class="nar-modal-body" id="nar-pick-body">' +
            '<div class="empty">loading images…</div></div>' +
        "</div>";
      overlay.addEventListener("mousedown", function (e) {
        if (e.target === overlay || (e.target.getAttribute && e.target.getAttribute("data-close"))) {
          overlay.remove();
        }
      });
      this._root.appendChild(overlay);

      Promise.all([
        bg.get("/api/artifacts").catch(function () { return { artifacts: [] }; }),
        bg.get("/api/refs").catch(function () { return { refs: [] }; })
      ]).then(function (res) {
        var arts = (res[0] && res[0].artifacts) || [];
        var refs = (res[1] && res[1].refs) || [];
        var items = [];
        for (var i = 0; i < arts.length; i++) {
          if (arts[i] && arts[i].path) items.push({ path: arts[i].path, label: arts[i].logical_name || ("artifact " + arts[i].id), tag: "artifact" });
        }
        for (var k = 0; k < refs.length; k++) {
          if (refs[k] && refs[k].path) items.push({ path: refs[k].path, label: refs[k].name || "ref", tag: "ref" });
        }
        var body = overlay.querySelector("#nar-pick-body");
        if (!body) return;
        if (!items.length) { body.innerHTML = '<div class="empty">no artifacts or refs to attach yet</div>'; return; }
        var html = '<div class="nar-pickgrid">';
        for (var m = 0; m < items.length; m++) {
          html += '<button class="nar-pickcard" data-path="' + bg.esc(items[m].path) + '">' +
            '<img src="' + bg.preview(items[m].path) + '" alt="" onerror="this.classList.add(\'nar-broken\');this.removeAttribute(\'src\')">' +
            '<span class="nar-pickmeta"><b>' + bg.esc(items[m].label) + "</b>" +
            '<em>' + bg.esc(items[m].tag) + "</em></span></button>";
        }
        html += "</div>";
        body.innerHTML = html;
        body.addEventListener("click", function (e) {
          var c = e.target.closest ? e.target.closest("[data-path]") : null;
          if (!c) return;
          p.img = c.getAttribute("data-path");
          var node = self._panelNode(p.id);
          if (node) self._refreshImg(node, p);
          self._autosave();
          overlay.remove();
        });
      }).catch(function (e) {
        console.warn("narrative picker", e);
        var body = overlay.querySelector("#nar-pick-body");
        if (body) body.innerHTML = '<div class="empty">failed to load images</div>';
      });
    },

    /* ---- helpers ------------------------------------------------------ */
    _panelNode: function (id) { return this._root.querySelector('.nar-panel[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]'); },
    _num: function (v, d) { var n = Number(v); return isFinite(n) ? n : d; },
    _clock: function () { var d = new Date(); return d.toTimeString().slice(0, 5); },
    _setStatus: function (msg, bad) {
      var s = this._root.querySelector("#nar-status");
      if (s) { s.textContent = msg; s.className = "nar-status" + (bad ? " nar-bad" : ""); }
    },
    _setHint: function (msg) { var h = this._root.querySelector("#nar-hint"); if (h) h.textContent = msg; },
    _selectAll: function (input) { try { input.setSelectionRange(0, input.value.length); } catch (e) {} },

    _css: function () {
      return "" +
      ".nar-wrap{display:flex;flex-direction:column;height:calc(100vh - 220px);min-height:440px;color:#e6e8ee}" +
      ".nar-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 12px;background:#101319;border:1px solid #1e232c;border-radius:12px;margin-bottom:10px}" +
      ".nar-btn{background:#161b24;border:1px solid #2a313d;color:#e6e8ee;padding:7px 12px;border-radius:8px;font:inherit;font-size:13px;cursor:pointer;transition:border-color .15s,background .15s}" +
      ".nar-btn:hover{border-color:#3b7f9e}" +
      ".nar-primary{background:#173039;border-color:#3b7f9e;color:#cfe9f2}" +
      ".nar-danger:hover{border-color:#7a3535;color:#f0b3b3}" +
      ".nar-btn.nar-active{background:#3b7f9e;border-color:#3b7f9e;color:#06121a;font-weight:600}" +
      ".nar-sep{flex:1}" +
      ".nar-status{font-size:12px;color:#8a93a2;min-width:70px}" +
      ".nar-status.nar-bad{color:#f0b3b3}" +
      ".nar-hint{font-size:11px;color:#5f6b7a;width:100%;order:9;margin-top:2px}" +
      ".nar-scroll{position:relative;flex:1;overflow:auto;background:#0b0e13;border:1px solid #1e232c;border-radius:12px;" +
        "background-image:radial-gradient(#181d26 1px,transparent 1px);background-size:26px 26px;cursor:grab}" +
      ".nar-scroll.nar-panning{cursor:grabbing}" +
      ".nar-canvas{position:relative}" +
      ".nar-svg{position:absolute;top:0;left:0;pointer-events:none;z-index:1}" +
      ".nar-edge{stroke:#3b7f9e;stroke-width:2;opacity:.85}" +
      ".nar-panel{position:absolute;width:" + PANEL_W + "px;background:#101319;border:1px solid #1e232c;border-radius:12px;" +
        "box-shadow:0 4px 16px rgba(0,0,0,.45);z-index:2;overflow:hidden;user-select:none}" +
      ".nar-panel.nar-sel{border-color:#3b7f9e;box-shadow:0 0 0 1px #3b7f9e,0 6px 20px rgba(0,0,0,.5)}" +
      ".nar-panel.nar-dragging{opacity:.92;z-index:10}" +
      ".nar-linking .nar-panel{cursor:crosshair}" +
      ".nar-panel.nar-linksrc{border-color:#e8c05a;box-shadow:0 0 0 1px #e8c05a}" +
      ".nar-head{display:flex;align-items:center;gap:6px;padding:6px 9px;background:#161b24;border-bottom:1px solid #1e232c;cursor:grab}" +
      ".nar-head:active{cursor:grabbing}" +
      ".nar-grip{color:#4c5666;font-size:12px}" +
      ".nar-badge{font-size:9px;letter-spacing:.09em;text-transform:uppercase;color:#8a93a2}" +
      ".nar-title{width:100%;box-sizing:border-box;background:transparent;border:0;border-bottom:1px solid transparent;" +
        "color:#e6e8ee;font:inherit;font-size:13px;font-weight:600;padding:8px 9px 6px;outline:none}" +
      ".nar-title:focus{border-bottom-color:#3b7f9e}" +
      ".nar-imgwrap{padding:0 9px}" +
      ".nar-thumb{display:block;width:100%;height:110px;object-fit:cover;border-radius:7px;background:#07090d;border:1px solid #1e232c}" +
      ".nar-thumb.nar-broken{min-height:44px}" +
      ".nar-imgbar{display:flex;gap:6px;margin-top:5px}" +
      ".nar-pick{width:100%;padding:10px;background:#0d1016;border:1px dashed #2a313d;border-radius:7px;color:#8a93a2;" +
        "font:inherit;font-size:12px;cursor:pointer}" +
      ".nar-pick:hover{border-color:#3b7f9e;color:#cfe9f2}" +
      ".nar-mini{background:#161b24;border:1px solid #2a313d;color:#a7b0be;font:inherit;font-size:11px;padding:4px 8px;" +
        "border-radius:6px;cursor:pointer}" +
      ".nar-mini:hover{border-color:#3b7f9e;color:#e6e8ee}" +
      ".nar-text{width:100%;box-sizing:border-box;min-height:56px;resize:vertical;background:transparent;border:0;" +
        "color:#c4cbd6;font:inherit;font-size:12px;line-height:1.45;padding:8px 9px 10px;outline:none}" +
      ".nar-text:focus{background:#0d1016}" +
      ".nar-empty{position:absolute;top:0;left:0;right:0;bottom:0;display:flex;align-items:center;justify-content:center;z-index:3}" +
      ".nar-empty-inner{text-align:center;color:#6b7686;max-width:420px}" +
      ".nar-empty-glyph{font-size:44px;color:#2a313d;margin-bottom:6px}" +
      ".nar-empty-inner p{margin:0 0 14px;font-size:14px}" +
      ".nar-empty-sub{font-size:12px !important;color:#5f6b7a;line-height:1.5}" +
      ".nar-presets{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}" +
      ".nar-modal{position:fixed;inset:0;background:rgba(4,6,10,.66);z-index:9998;display:flex;align-items:center;justify-content:center}" +
      ".nar-modal-box{width:min(680px,92vw);max-height:80vh;display:flex;flex-direction:column;background:#101319;" +
        "border:1px solid #2a313d;border-radius:14px;overflow:hidden}" +
      ".nar-modal-head{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid #1e232c}" +
      ".nar-modal-body{padding:14px;overflow:auto}" +
      ".nar-pickgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px}" +
      ".nar-pickcard{display:flex;flex-direction:column;gap:6px;padding:6px;background:#0d1016;border:1px solid #1e232c;" +
        "border-radius:9px;cursor:pointer;text-align:left}" +
      ".nar-pickcard:hover{border-color:#3b7f9e}" +
      ".nar-pickcard img{width:100%;height:82px;object-fit:cover;border-radius:6px;background:#07090d}" +
      ".nar-pickcard img.nar-broken{min-height:40px}" +
      ".nar-pickmeta{display:flex;flex-direction:column;font-size:11px;line-height:1.3}" +
      ".nar-pickmeta b{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#e6e8ee}" +
      ".nar-pickmeta em{color:#6b7280;font-style:normal;font-size:10px}" +
      ".nar-wrap .empty{color:#6b7686;font-size:13px;padding:12px}";
    }
  };

  window.SeatWS = window.SeatWS || {};
  window.SeatWS.narrative = N;
})();
