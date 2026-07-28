/* flow_asset.js — the full Asset-generation node editor for Builders Gate.
 *
 * A Weavy / n8n-style visual pipeline built on the shared NodeCanvas engine:
 *
 *     Reference(s) ┐
 *                  ├─▶ Generate (gpt-image) ─▶ Candidate ─▶ Rig → Godot
 *        Prompt  ──┘        │
 *                  Edit (image-edit) …
 *
 * Node types: Reference · Prompt · Generate · Edit · Candidate · Rig→Godot.
 * Registered as window.StudioFlows.asset; the Studio dispatcher (flows.js)
 * calls build(host, api). Frontend-only, vanilla JS, wired to live endpoints.
 * Everything is guarded — this module must never throw uncaught.
 */
(function () {
  "use strict";

  // Injected once; the engine + studio shell already provide most classes,
  // these are the asset-flow-specific extras (prefix .fa-).
  function injectStyle() {
    if (document.getElementById("flow-asset-style")) return;
    var s = document.createElement("style");
    s.id = "flow-asset-style";
    s.textContent = [
      ".fa-wrap{display:flex;height:100%;border:1px solid var(--seam);border-radius:12px;overflow:hidden;background:var(--iron)}",
      ".fa-palette{width:196px;flex:none;background:var(--iron);border-right:1px solid var(--seam);padding:14px 12px;overflow-y:auto}",
      ".fa-canvas{flex:1;position:relative;min-width:0}",
      ".fa-insp{width:264px;flex:none;background:var(--iron);border-left:1px solid var(--seam);padding:15px;overflow-y:auto}",
      ".fa-ph{font-family:var(--mono);font-size:9px;letter-spacing:.22em;text-transform:uppercase;color:var(--ash2);margin:0 0 8px}",
      ".fa-ph.mt{margin-top:18px}",
      ".fa-pi{display:flex;align-items:center;gap:9px;width:100%;text-align:left;padding:9px 11px;margin-bottom:6px;background:var(--plate);border:1px solid var(--seam);border-radius:9px;color:var(--bone);font:inherit;font-size:12.5px;cursor:pointer}",
      ".fa-pi:hover{border-color:var(--ember);background:var(--plate2)}",
      ".fa-pi .g{color:var(--ember);width:15px;text-align:center;flex:none}",
      ".fa-hint{font-size:11.5px;color:var(--ash);line-height:1.55}.fa-hint b{color:var(--bone)}",
      ".fa-btn{display:block;width:100%;padding:8px;margin-top:8px;background:var(--plate);border:1px solid var(--seam);border-radius:8px;color:var(--bone);font:inherit;font-size:12px;cursor:pointer}",
      ".fa-btn:hover{border-color:var(--ember)}",
      // node bodies
      ".fa-thumb{width:100%;height:98px;object-fit:contain;background:#000;border-radius:6px;display:block}",
      ".fa-thumb.ph{display:flex;align-items:center;justify-content:center;color:var(--ash2);font-family:var(--mono);font-size:10px;letter-spacing:.1em}",
      ".fa-ta{width:100%;min-height:66px;resize:vertical;background:var(--void);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:12px;padding:7px;box-sizing:border-box}",
      ".fa-ta:focus{outline:none;border-color:var(--ember)}",
      ".fa-run{width:100%;padding:9px;background:var(--ember);color:#111;border:0;border-radius:8px;font:inherit;font-weight:600;font-size:12px;cursor:pointer}",
      ".fa-run:hover{filter:brightness(1.08)}",
      ".fa-run.busy{background:var(--plate2);color:var(--ash);cursor:default}",
      ".fa-sub{text-align:center;font-size:10px;color:var(--ash2);margin-top:6px;font-family:var(--mono)}",
      ".fa-nodestat{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10.5px;color:var(--ash);margin-top:8px}",
      ".fa-dot{width:7px;height:7px;border-radius:50%;background:var(--seam2);flex:none}",
      ".fa-dot.live{background:var(--good);box-shadow:0 0 6px var(--good);animation:fa-pulse 1.1s infinite}",
      ".fa-dot.warn{background:var(--warn)}.fa-dot.bad{background:var(--bad)}.fa-dot.good{background:var(--good)}",
      "@keyframes fa-pulse{0%,100%{opacity:1}50%{opacity:.35}}",
      ".fa-rig{font-size:11.5px;color:var(--ash);line-height:1.45}",
      // inspector
      ".fa-ie{color:var(--ash2);font-size:12px;line-height:1.5}",
      ".fa-ih{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--bone);margin-bottom:12px}",
      ".fa-ih .g{color:var(--ember)}",
      ".fa-img{width:100%;border-radius:8px;background:#000;margin-bottom:10px;display:block}",
      ".fa-kv{display:flex;justify-content:space-between;gap:10px;font-size:12px;padding:4px 0;border-bottom:1px solid var(--seam)}",
      ".fa-kv span:first-child{color:var(--ash2);font-family:var(--mono);font-size:11px}",
      ".fa-kv span:last-child{color:var(--bone);text-align:right;word-break:break-word}",
      ".fa-p{font-size:12px;color:var(--ash);line-height:1.55;margin-top:8px}",
      ".fa-sel{width:100%;margin-top:6px;background:var(--void);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:12px;padding:7px}",
      ".fa-chips{display:flex;flex-wrap:wrap;gap:5px;margin:8px 0}",
      ".fa-chip{font-family:var(--mono);font-size:10px;color:var(--ember);border:1px solid var(--seam2);border-radius:20px;padding:2px 8px}",
      ".fa-acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}",
      ".fa-a{padding:6px 11px;background:var(--ember);color:#111;border:0;border-radius:7px;font:inherit;font-weight:600;font-size:11.5px;cursor:pointer}",
      ".fa-a.ghost{background:transparent;color:var(--bone);border:1px solid var(--seam2)}",
      ".fa-a.ghost:hover{border-color:var(--ember)}",
      ".fa-a.bad{background:transparent;color:var(--bad);border:1px solid var(--bad)}",
      ".fa-badge{display:inline-block;font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.08em;padding:2px 8px;border-radius:20px;border:1px solid var(--seam2);color:var(--ash)}",
      ".fa-badge.candidate{color:var(--warn);border-color:var(--warn)}",
      ".fa-badge.approved{color:var(--good);border-color:var(--good)}",
      ".fa-badge.rejected{color:var(--bad);border-color:var(--bad)}",
      // live activity feed
      ".fa-feed{margin-top:10px;border-top:1px solid var(--seam);padding-top:10px}",
      ".fa-feed h5{margin:0 0 8px;font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--ash2)}",
      ".fa-step{font-size:11px;line-height:1.45;padding:4px 0;border-bottom:1px solid var(--seam);color:var(--ash)}",
      ".fa-step .k{font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.06em;margin-right:6px}",
      ".fa-step.tool .k{color:var(--accent-3,#3b7f9e)}.fa-step.say .k{color:var(--bone)}",
      ".fa-step.result .k{color:var(--good)}.fa-step.steer .k{color:var(--ember)}",
      ".fa-final{margin-top:8px;font-size:11.5px;color:var(--bone);background:var(--plate);border:1px solid var(--seam);border-radius:8px;padding:8px}"
    ].join("\n");
    document.head.appendChild(s);
  }

  // ---- small helpers (all guarded) --------------------------------------
  var GLYPH = { reference: "▦", prompt: "✎", model: "✦", edit: "✂", candidate: "▣", rig: "⬡" };
  var REF_REL = function (name) { return ".bgate/refs/" + name + ".png"; };

  function reqRel(rel) { return "/api/preview?rel=" + encodeURIComponent(rel || ""); }

  // ==========================================================================
  // The flow instance — one per build(); exposed as window.AssetFlow so the
  // inline handlers in node bodies / inspector can reach it.
  // ==========================================================================
  function AssetFlow(host, api) {
    this.host = host;
    this.api = api;
    this.esc = (api && api.esc) || function (s) { return String(s == null ? "" : s); };
    this.nc = null;
    this.nodes = [];              // our mirror of node objects (source of truth for data)
    this.edges = [];              // {from:[id,port], to:[id,port]}
    this.refs = [];               // /api/refs
    this.artsById = {};           // artifact id -> artifact
    this.seenArt = {};            // artifact id -> true (already has a node)
    this.selId = null;
    this._addN = 0;               // cascade offset for palette-added nodes
    this._saveT = null;
    this._artT = null;
    this._actT = null;
    this._dead = false;
  }

  AssetFlow.prototype.g = function (p) {
    try { return this.api.get(p); } catch (e) { return Promise.resolve({}); }
  };
  AssetFlow.prototype.p = function (p, b) {
    try { return this.api.post(p, b); } catch (e) { return Promise.resolve({}); }
  };
  AssetFlow.prototype.toast = function (m, bad) {
    try { this.api.toast(m, bad); } catch (e) { /* noop */ }
  };

  AssetFlow.prototype.node = function (id) {
    for (var i = 0; i < this.nodes.length; i++) if (this.nodes[i].id === id) return this.nodes[i];
    return null;
  };

  // ---- teardown (clear timers before a rebuild) --------------------------
  AssetFlow.prototype.teardown = function () {
    this._dead = true;
    if (this._saveT) { clearTimeout(this._saveT); this._saveT = null; }
    if (this._artT) { clearInterval(this._artT); this._artT = null; }
    if (this._actT) { clearInterval(this._actT); this._actT = null; }
  };

  // ---- build -------------------------------------------------------------
  AssetFlow.prototype.build = function () {
    var self = this;
    injectStyle();
    this.host.innerHTML =
      '<div class="fa-wrap">' +
        '<div class="fa-palette">' +
          '<div class="fa-ph">Add node</div>' +
          '<button class="fa-pi" data-add="reference"><span class="g">▦</span>Reference</button>' +
          '<button class="fa-pi" data-add="prompt"><span class="g">✎</span>Prompt</button>' +
          '<button class="fa-pi" data-add="model"><span class="g">✦</span>Generate</button>' +
          '<button class="fa-pi" data-add="edit"><span class="g">✂</span>Edit image</button>' +
          '<button class="fa-pi" data-add="candidate"><span class="g">▣</span>Candidate</button>' +
          '<button class="fa-pi" data-add="rig"><span class="g">⬡</span>Rig → Godot</button>' +
          '<div class="fa-ph mt">Pipeline</div>' +
          '<div class="fa-hint">Wire <b>Reference</b> + <b>Prompt</b> into <b>Generate</b>, then run it — the art agent is dispatched with your prompt and the connected refs. New <b>Candidate</b>s land as it produces them; approve, reject or regenerate from the inspector, then feed <b>Rig → Godot</b>.</div>' +
          '<button class="fa-btn" data-act="refresh">↻ Refresh candidates</button>' +
          '<button class="fa-btn" data-act="fit">⊡ Fit view</button>' +
        '</div>' +
        '<div class="fa-canvas" id="fa-canvas"></div>' +
        '<div class="fa-insp" id="fa-insp"><div class="fa-ie">Select a node to inspect it.</div></div>' +
      '</div>';

    this.$canvas = this.host.querySelector("#fa-canvas");
    this.$insp = this.host.querySelector("#fa-insp");

    // palette wiring
    this.host.querySelectorAll(".fa-pi").forEach(function (b) {
      b.onclick = function () { self.addPaletteNode(b.getAttribute("data-add")); };
    });
    this.host.querySelectorAll(".fa-btn").forEach(function (b) {
      b.onclick = function () {
        var a = b.getAttribute("data-act");
        if (a === "refresh") self.refreshArtifacts(true);
        else if (a === "fit" && self.nc) { try { self.nc.fit(); } catch (e) {} }
      };
    });

    // Load persisted layout + live data in parallel, then mount.
    Promise.all([
      this.g("/api/refs"),
      this.g("/api/artifacts"),
      this.g("/api/workspace/art/asset-flow")
    ]).then(function (r) {
      if (self._dead) return;
      self.refs = (r[0] && r[0].refs) || [];
      var arts = (r[1] && r[1].artifacts) || [];
      arts.forEach(function (a) { self.artsById[a.id] = a; });
      var saved = (r[2] && r[2].data) || {};
      if (saved && saved.nodes && saved.nodes.length) self.restore(saved);
      else self.seed(arts);
      // Everything that exists at load time is "known" — only candidates
      // produced AFTER the editor opens get auto-added (else history floods).
      Object.keys(self.artsById).forEach(function (id) { self.seenArt[id] = true; });
      self.mount();
    }).catch(function () {
      if (self._dead) return;
      self.seed([]); self.mount();
    });
  };

  // ---- seed from real data ----------------------------------------------
  AssetFlow.prototype.seed = function (arts) {
    var nodes = [], edges = [];
    var refList = (this.refs || []).slice(0, 3);
    refList.forEach(function (r, i) {
      nodes.push({
        id: "ref" + i, type: "reference", glyph: GLYPH.reference, title: r.name,
        badge: r.kind, x: 30, y: 30 + i * 150, w: 178,
        data: { refName: r.name, kind: r.kind, note: r.note || "" },
        ports: { out: [{ id: "o", label: "ref" }] }
      });
    });
    var py = 30 + Math.max(refList.length, 1) * 150 + 8;
    nodes.push({
      id: "prompt0", type: "prompt", glyph: GLYPH.prompt, title: "Prompt",
      x: 30, y: py, w: 252, data: { text: "" },
      ports: { out: [{ id: "o", label: "prompt" }] }
    });
    nodes.push({
      id: "gen0", type: "model", glyph: GLYPH.model, title: "Generate · gpt-image",
      badge: "run", x: 372, y: 150, w: 238, data: { itemId: null },
      ports: { in: [{ id: "ref", label: "refs" }, { id: "prompt", label: "prompt" }], out: [{ id: "o", label: "image" }] }
    });
    refList.forEach(function (r, i) { edges.push({ from: ["ref" + i, "o"], to: ["gen0", "ref"] }); });
    edges.push({ from: ["prompt0", "o"], to: ["gen0", "prompt"] });

    // recent candidates on the right
    var cands = (arts || []).filter(function (a) {
      return a.status === "candidate" || a.status === "approved";
    }).slice(0, 6);
    var self = this;
    cands.forEach(function (a, i) {
      nodes.push(self.candidateNode(a, 690, 30 + i * 160));
      self.seenArt[a.id] = true;
    });
    if (cands[0]) edges.push({ from: ["gen0", "o"], to: ["cand" + cands[0].id, "i"] });

    // a Rig → Godot sink
    nodes.push({
      id: "rig0", type: "rig", glyph: GLYPH.rig, title: "Rig → Godot",
      badge: "sink", x: 980, y: 60, w: 190, data: { note: "Approved candidates are imported to the Godot project as sprites/textures." },
      ports: { in: [{ id: "i", label: "asset" }] }
    });

    this.nodes = nodes;
    this.edges = edges;
  };

  AssetFlow.prototype.candidateNode = function (a, x, y) {
    return {
      id: "cand" + a.id, type: "candidate", glyph: GLYPH.candidate,
      title: a.logical_name || ("artifact " + a.id), badge: a.status,
      accent: a.status === "approved" ? "var(--good)" : (a.status === "rejected" ? "var(--bad)" : "var(--ember)"),
      x: x, y: y, w: 202, data: { artId: a.id },
      ports: { in: [{ id: "i", label: "in" }], out: [{ id: "o", label: "ok" }] }
    };
  };

  // ---- restore persisted layout (re-hydrate live data) -------------------
  AssetFlow.prototype.restore = function (saved) {
    var self = this;
    var refByName = {};
    (this.refs || []).forEach(function (r) { refByName[r.name] = r; });
    var nodes = [];
    (saved.nodes || []).forEach(function (sn) {
      if (!sn || !sn.id || !sn.type) return;
      var n = {
        id: sn.id, type: sn.type, glyph: sn.glyph || GLYPH[sn.type] || "◆",
        title: sn.title || sn.type, badge: sn.badge, accent: sn.accent,
        x: +sn.x || 40, y: +sn.y || 40, w: +sn.w || 220,
        data: sn.data || {}, ports: self.portsFor(sn.type)
      };
      // re-hydrate references + candidates from fresh live data
      if (n.type === "reference" && n.data.refName && refByName[n.data.refName]) {
        var rf = refByName[n.data.refName];
        n.title = rf.name; n.badge = rf.kind; n.data.kind = rf.kind; n.data.note = rf.note || "";
      }
      if (n.type === "candidate" && n.data.artId != null) {
        var a = self.artsById[n.data.artId];
        if (a) {
          n.title = a.logical_name || n.title; n.badge = a.status;
          n.accent = a.status === "approved" ? "var(--good)" : (a.status === "rejected" ? "var(--bad)" : "var(--ember)");
        }
        self.seenArt[n.data.artId] = true;
      }
      nodes.push(n);
    });
    this.nodes = nodes.length ? nodes : this.nodes;
    this.edges = (saved.edges || []).filter(function (e) {
      return e && e.from && e.to && e.from.length === 2 && e.to.length === 2;
    });
    if (!nodes.length) this.seed([]);   // corrupt save → fall back
  };

  AssetFlow.prototype.portsFor = function (type) {
    switch (type) {
      case "reference": return { out: [{ id: "o", label: "ref" }] };
      case "prompt": return { out: [{ id: "o", label: "prompt" }] };
      case "model": return { in: [{ id: "ref", label: "refs" }, { id: "prompt", label: "prompt" }], out: [{ id: "o", label: "image" }] };
      case "edit": return { in: [{ id: "img", label: "image" }, { id: "prompt", label: "edit" }], out: [{ id: "o", label: "image" }] };
      case "candidate": return { in: [{ id: "i", label: "in" }], out: [{ id: "o", label: "ok" }] };
      case "rig": return { in: [{ id: "i", label: "asset" }] };
      default: return { in: [{ id: "i" }], out: [{ id: "o" }] };
    }
  };

  // ---- mount the canvas --------------------------------------------------
  AssetFlow.prototype.mount = function () {
    var self = this;
    try {
      var NC = this.api.NodeCanvas;
      this.nc = new NC(this.$canvas, {
        nodes: this.nodes, edges: this.edges, accent: "var(--ember)",
        renderBody: function (n) { return self.renderBody(n); },
        onSelect: function (n) { self.onSelect(n); },
        onConnect: function (from, to) { self.onConnect(from, to); },
        onNodeMove: function (n) { self.scheduleSave(); }
      });
      this.nc.mount();
      this.nc.fit();
      if (this.api.setCanvas) this.api.setCanvas(this.nc);
    } catch (e) {
      this.$canvas.innerHTML = '<div class="fa-ie" style="padding:24px">canvas error: ' + this.esc(e.message) + '</div>';
      return;
    }
    // auto-refresh candidates every ~4s
    this._artT = setInterval(function () { self.refreshArtifacts(false); }, 4000);
  };

  // ---- node body rendering ----------------------------------------------
  AssetFlow.prototype.renderBody = function (n) {
    try { return this._renderBody(n); } catch (e) { return ""; }
  };
  AssetFlow.prototype._renderBody = function (n) {
    var esc = this.esc;
    if (n.type === "reference") {
      if (!n.data.refName) return '<div class="fa-thumb ph">no ref chosen</div>';
      return '<img class="fa-thumb" src="' + reqRel(REF_REL(n.data.refName)) + '" onerror="this.classList.add(&quot;ph&quot;);this.removeAttribute(&quot;src&quot;)">';
    }
    if (n.type === "candidate") {
      var a = this.artsById[n.data.artId];
      if (!a) return '<div class="fa-thumb ph">artifact ' + esc(n.data.artId) + ' gone</div>';
      return '<img class="fa-thumb" src="' + reqRel(a.path) + '" onerror="this.classList.add(&quot;ph&quot;)">';
    }
    if (n.type === "prompt") {
      return '<textarea class="fa-ta" placeholder="Describe the asset to generate…" ' +
        'oninput="window.AssetFlow&&AssetFlow.onPrompt(&quot;' + n.id + '&quot;,this.value)">' +
        esc(n.data.text || "") + '</textarea>';
    }
    if (n.type === "model" || n.type === "edit") {
      var busy = !!n.data.running;
      var label = n.type === "edit" ? "Run edit" : "Run generate";
      var btn = '<button class="fa-run' + (busy ? " busy" : "") + '" ' +
        'onclick="window.AssetFlow&&AssetFlow.run(&quot;' + n.id + '&quot;,event)">' +
        (busy ? "● working…" : "▶ " + label) + '</button>';
      var sub = '<div class="fa-sub">' + (n.type === "edit" ? "dispatches art (image-edit)" : "dispatches the art agent") + '</div>';
      var stat = "";
      if (n.data.itemId) {
        stat = '<div class="fa-nodestat"><span class="fa-dot ' + (busy ? "live" : "good") + '"></span>item #' + esc(n.data.itemId) + (busy ? " running" : " dispatched") + '</div>';
      }
      return btn + sub + stat;
    }
    if (n.type === "rig") {
      return '<div class="fa-rig">' + esc(n.data.note || "Target: Godot project. Approved assets import here.") + '</div>';
    }
    return "";
  };

  // ---- selection / inspector --------------------------------------------
  AssetFlow.prototype.onSelect = function (n) {
    // stop any per-node activity poll when selection changes
    if (this._actT) { clearInterval(this._actT); this._actT = null; }
    this.selId = n ? n.id : null;
    this.renderInspector(n);
    if (n && (n.type === "model" || n.type === "edit") && n.data.itemId) this.startActivityPoll(n);
  };

  AssetFlow.prototype.renderInspector = function (n) {
    try { this.$insp.innerHTML = this._inspHtml(n); this._wireInsp(n); }
    catch (e) { this.$insp.innerHTML = '<div class="fa-ie">inspector error</div>'; }
  };

  AssetFlow.prototype._inspHtml = function (n) {
    var esc = this.esc;
    if (!n) return '<div class="fa-ie">Select a node to inspect it.</div>';
    var head = '<div class="fa-ih"><span class="g">' + (n.glyph || "◆") + '</span>' + esc(n.title || n.type) + '</div>';
    var kv = function (k, v) { return '<div class="fa-kv"><span>' + esc(k) + '</span><span>' + esc(v) + '</span></div>'; };

    if (n.type === "reference") {
      var body = "";
      if (n.data.refName) body += '<img class="fa-img" src="' + reqRel(REF_REL(n.data.refName)) + '" onerror="this.style.opacity=.12">';
      var opts = '<option value="">— pick a reference —</option>' + (this.refs || []).map(function (r) {
        return '<option value="' + esc(r.name) + '"' + (r.name === n.data.refName ? " selected" : "") + '>' + esc(r.name) + " · " + esc(r.kind) + '</option>';
      }).join("");
      body += '<div class="fa-p">Reference image fed into Generate. Choose which global ref this node supplies:</div>';
      body += '<select class="fa-sel" data-role="pickref">' + opts + '</select>';
      if (n.data.kind) body += kv("kind", n.data.kind);
      if (n.data.note) body += '<div class="fa-p">' + esc(n.data.note) + '</div>';
      body += this._delBtn(n);
      return head + body;
    }

    if (n.type === "prompt") {
      return head + '<div class="fa-p">The generation prompt. It becomes the art work item\'s brief and is passed to every Generate node it feeds.</div>' +
        '<textarea class="fa-ta" style="min-height:120px;margin-top:8px" data-role="prompt" placeholder="Describe the asset…">' + esc(n.data.text || "") + '</textarea>' +
        this._delBtn(n);
    }

    if (n.type === "model" || n.type === "edit") {
      var up = this.inbound(n.id);
      var refNames = up.refs, prompt = up.promptText;
      var b = '';
      b += '<div class="fa-p">' + (n.type === "edit"
        ? "Image-edit variant. Feeds a source image + edit prompt to the art seat (image_edit)."
        : "Generates a new image via gpt-image from the connected references and prompt.") + '</div>';
      b += '<div class="fa-ph mt">Connected refs</div>';
      b += refNames.length ? '<div class="fa-chips">' + refNames.map(function (x) { return '<span class="fa-chip">' + esc(x) + '</span>'; }).join("") + '</div>'
        : '<div class="fa-ie">none — drag a Reference out-port into this node.</div>';
      b += '<div class="fa-ph mt">Prompt</div>';
      b += '<div class="fa-p">' + (prompt ? esc(prompt.slice(0, 240)) : '<span class="fa-ie">none — connect a Prompt node.</span>') + '</div>';
      b += '<div class="fa-acts"><button class="fa-a" data-role="run">' + (n.data.running ? "● working…" : "▶ Run") + '</button>';
      if (n.data.itemId) b += '<button class="fa-a ghost" data-role="watch">watch log</button>';
      b += '</div>';
      b += '<div id="fa-feed"></div>';
      b += this._delBtn(n);
      return head + b;
    }

    if (n.type === "candidate") {
      var a = this.artsById[n.data.artId];
      if (!a) return head + '<div class="fa-ie">Artifact ' + esc(n.data.artId) + ' is no longer available.</div>' + this._delBtn(n);
      var s = '<img class="fa-img" src="' + reqRel(a.path) + '" onerror="this.style.opacity=.12">';
      s += '<div style="margin-bottom:10px"><span class="fa-badge ' + esc(a.status) + '">' + esc(a.status) + '</span></div>';
      s += kv("logical", a.logical_name || "—") + kv("revision", "r" + (a.revision != null ? a.revision : "?")) +
           kv("model", a.model || "—") + kv("producer", a.producer || "—");
      var md = a.metadata || {};
      var qr = md.qa_review || (typeof md === "object" && md ? md.qa_review : null);
      if (qr && (qr.verdict || qr.score != null)) s += kv("QA", (qr.verdict || "?") + " · " + (qr.score != null ? qr.score + "/100" : ""));
      if (a.prompt) s += '<div class="fa-p">' + esc(String(a.prompt).slice(0, 220)) + '…</div>';
      s += '<div class="fa-acts">' +
        '<button class="fa-a" data-role="approve">approve</button>' +
        '<button class="fa-a bad" data-role="reject">reject</button>' +
        '<button class="fa-a ghost" data-role="regen">regenerate</button>' +
        '</div>';
      return head + s;
    }

    if (n.type === "rig") {
      return head + '<div class="fa-p">' + esc(n.data.note || "Rig → Godot sink.") + '</div>' +
        '<div class="fa-p">Wire an approved <b>Candidate</b> into this node to mark it for Godot import. This is the pipeline\'s terminal target.</div>' +
        this._delBtn(n);
    }
    return head;
  };

  AssetFlow.prototype._delBtn = function (n) {
    return '<div class="fa-acts" style="margin-top:16px"><button class="fa-a ghost" data-role="del">remove node</button></div>';
  };

  // wire inspector controls (no inline handlers for the dynamic ones)
  AssetFlow.prototype._wireInsp = function (n) {
    if (!n) return;
    var self = this, insp = this.$insp;
    var q = function (sel) { return insp.querySelector(sel); };
    var pick = q('[data-role="pickref"]');
    if (pick) pick.onchange = function () { self.setRef(n.id, this.value); };
    var pta = q('[data-role="prompt"]');
    if (pta) pta.oninput = function () { self.onPrompt(n.id, this.value, true); };
    var run = q('[data-role="run"]'); if (run) run.onclick = function () { self.run(n.id); };
    var watch = q('[data-role="watch"]'); if (watch) watch.onclick = function () {
      if (window.watchAgent && n.data.itemId) try { window.watchAgent(n.data.itemId); } catch (e) {}
    };
    var ap = q('[data-role="approve"]'); if (ap) ap.onclick = function () { self.review(n.data.artId, "approved"); };
    var rj = q('[data-role="reject"]'); if (rj) rj.onclick = function () { self.review(n.data.artId, "rejected"); };
    var rg = q('[data-role="regen"]'); if (rg) rg.onclick = function () { self.regen(n.data.artId); };
    var del = q('[data-role="del"]'); if (del) del.onclick = function () { self.removeNode(n.id); };
  };

  // ---- edges / connectivity ---------------------------------------------
  AssetFlow.prototype.onConnect = function (from, to) {
    // engine already added it internally; mirror into our array + persist
    var exists = this.edges.some(function (e) {
      return e.from[0] === from[0] && e.from[1] === from[1] && e.to[0] === to[0] && e.to[1] === to[1];
    });
    if (!exists) this.edges.push({ from: from.slice(), to: to.slice() });
    this.scheduleSave();
    // refresh inspector if a generate/edit node is selected (its inputs changed)
    var sel = this.selId ? this.node(this.selId) : null;
    if (sel && (sel.type === "model" || sel.type === "edit")) this.renderInspector(sel);
  };

  // gather refs + prompt feeding a generate/edit node
  AssetFlow.prototype.inbound = function (id) {
    var self = this, refs = [], prompts = [], images = [];
    this.edges.forEach(function (e) {
      if (e.to[0] !== id) return;
      var src = self.node(e.from[0]);
      if (!src) return;
      if (src.type === "reference" && src.data.refName) refs.push(src.data.refName);
      else if (src.type === "prompt") { if (src.data.text) prompts.push(src.data.text); }
      else if (src.type === "candidate") { var a = self.artsById[src.data.artId]; if (a) images.push(a.logical_name || a.path); }
    });
    return { refs: refs, promptText: prompts.join("\n").trim(), images: images };
  };

  // ---- prompt edit -------------------------------------------------------
  AssetFlow.prototype.onPrompt = function (id, val, fromInsp) {
    var n = this.node(id); if (!n) return;
    n.data.text = val;
    // keep the other view (node body / inspector) roughly in sync without full re-render
    if (!fromInsp && this.selId === id) {
      var ta = this.$insp.querySelector('[data-role="prompt"]');
      if (ta && ta.value !== val) ta.value = val;
    }
    this.scheduleSave();
  };

  // ---- reference picker --------------------------------------------------
  AssetFlow.prototype.setRef = function (id, name) {
    var n = this.node(id); if (!n) return;
    var rf = null;
    (this.refs || []).forEach(function (r) { if (r.name === name) rf = r; });
    n.data.refName = name || "";
    n.data.kind = rf ? rf.kind : "";
    n.data.note = rf ? (rf.note || "") : "";
    n.title = name || "Reference";
    n.badge = rf ? rf.kind : "";
    this.rerenderNode(n);
    this.renderInspector(n);
    this.scheduleSave();
  };

  AssetFlow.prototype.rerenderNode = function (n) {
    try { if (this.nc && this.nc.addNode) this.nc.addNode(n); } catch (e) {}
  };

  // ---- run a generate / edit node ---------------------------------------
  AssetFlow.prototype.run = function (id, ev) {
    if (ev && ev.stopPropagation) ev.stopPropagation();
    var self = this;
    var n = this.node(id); if (!n) return;
    if (n.data.running) { this.toast("already running", true); return; }
    var up = this.inbound(id);
    var prompt = (up.promptText || "").trim();
    if (!prompt) { this.toast("connect a Prompt node with text first", true); return; }
    var isEdit = n.type === "edit";
    var refLabel = up.refs.length ? " [" + up.refs.join(", ") + "]" : "";
    var title = (isEdit ? "Edit: " : "Gen: ") + prompt.slice(0, 52);
    var brief = isEdit
      ? "Edit art image using image_edit. Match refs" + refLabel + ": " + prompt
      : "Generate art matching refs" + refLabel + ": " + prompt;

    n.data.running = true;
    this.rerenderNode(n);
    if (this.selId === id) this.renderInspector(n);

    this.p("/api/queue", { seat: "art", title: title, brief: brief, priority: 3 }).then(function (item) {
      if (self._dead) return;
      if (!item || !item.id) { n.data.running = false; self.rerenderNode(n); self.toast("could not queue", true); return; }
      n.data.itemId = item.id;
      self.p("/api/queue/" + item.id + "/dispatch", {}).then(function (r) {
        if (self._dead) return;
        if (r && r.ok === false) { self.toast("dispatch failed: " + (r.error || ""), true); }
        else { self.toast("art agent dispatched · item #" + item.id); }
        self.rerenderNode(n);
        if (self.selId === id) { self.renderInspector(n); self.startActivityPoll(n); }
        self.scheduleSave();
      });
    });
  };

  // ---- live agent activity poll (for a selected generate/edit node) ------
  AssetFlow.prototype.startActivityPoll = function (n) {
    var self = this;
    if (this._actT) { clearInterval(this._actT); this._actT = null; }
    if (!n || !n.data.itemId) return;
    var itemId = n.data.itemId;
    var tick = function () {
      Promise.all([self.g("/api/agents"), self.g("/api/agent-activity/" + itemId)]).then(function (r) {
        if (self._dead || self.selId !== n.id) return;
        var agents = (r[0] && r[0].agents) || [];
        var live = agents.some(function (a) { return a.item_id === itemId && a.state === "running"; });
        var act = r[1] || {};
        var running = live || !!act.running;
        if (n.data.running !== running) { n.data.running = running; self.rerenderNode(n); }
        self.renderFeed(act, running);
        if (!running) { clearInterval(self._actT); self._actT = null; self.refreshArtifacts(false); }
      });
    };
    tick();
    this._actT = setInterval(tick, 3000);
  };

  AssetFlow.prototype.renderFeed = function (act, running) {
    var feed = this.$insp.querySelector("#fa-feed");
    if (!feed) return;
    var esc = this.esc;
    var steps = (act && act.steps) || [];
    var html = '<div class="fa-feed"><h5>' + (running ? "● live activity" : "activity") + '</h5>';
    if (!steps.length) html += '<div class="fa-ie">' + (running ? "waiting for the agent…" : "no activity yet.") + '</div>';
    else html += steps.slice(-12).map(function (s) {
      var k = s.kind || "say";
      var txt = s.kind === "tool" ? (s.name + (s.hint ? " · " + s.hint : "")) : (s.text || "");
      return '<div class="fa-step ' + esc(k) + '"><span class="k">' + esc(k) + '</span>' + esc(txt) + '</div>';
    }).join("");
    if (act && act.final && act.final.text) {
      html += '<div class="fa-final">✓ ' + esc(act.final.subtype || "done") + ' — ' + esc(String(act.final.text).slice(0, 240)) + '</div>';
    }
    html += '</div>';
    feed.innerHTML = html;
  };

  // ---- candidate review --------------------------------------------------
  AssetFlow.prototype.review = function (artId, status) {
    if (artId == null) return;
    var self = this;
    this.p("/api/artifacts/" + artId + "/review", { status: status }).then(function () {
      if (self._dead) return;
      self.toast("marked " + status);
      // reflect locally then pull fresh
      if (self.artsById[artId]) self.artsById[artId].status = status;
      var n = self.node("cand" + artId);
      if (n) { n.badge = status; n.accent = status === "approved" ? "var(--good)" : "var(--bad)"; self.rerenderNode(n); if (self.selId === n.id) self.renderInspector(n); }
      self.refreshArtifacts(false);
    });
  };
  AssetFlow.prototype.regen = function (artId) {
    if (artId == null) return;
    var self = this;
    this.p("/api/artifacts/" + artId + "/regenerate", { reason: "from asset flow editor" }).then(function () {
      if (self._dead) return; self.toast("regenerate queued"); self.refreshArtifacts(false);
    });
  };

  // ---- auto-refresh artifacts -------------------------------------------
  AssetFlow.prototype.refreshArtifacts = function (manual) {
    var self = this;
    this.g("/api/artifacts").then(function (r) {
      if (self._dead || !self.nc) return;
      var arts = (r && r.artifacts) || [];
      var added = 0, updated = 0;
      // update statuses of existing candidate nodes + index
      arts.forEach(function (a) {
        var prev = self.artsById[a.id];
        self.artsById[a.id] = a;
        var n = self.node("cand" + a.id);
        if (n && (!prev || prev.status !== a.status)) {
          n.badge = a.status;
          n.accent = a.status === "approved" ? "var(--good)" : (a.status === "rejected" ? "var(--bad)" : "var(--ember)");
          self.rerenderNode(n); updated++;
        }
      });
      // add nodes for brand-new candidates (don't clobber user-moved nodes)
      var fresh = arts.filter(function (a) {
        return (a.status === "candidate" || a.status === "approved") && !self.seenArt[a.id];
      }).slice(0, 12);
      // stack them to the right, below existing candidate nodes
      var baseY = 30, maxX = 690;
      self.nodes.forEach(function (nd) {
        if (nd.type === "candidate") { baseY = Math.max(baseY, nd.y + 160); maxX = Math.max(maxX, nd.x); }
      });
      var gen = self.nodes.filter(function (nd) { return nd.type === "model"; })[0];
      fresh.forEach(function (a, i) {
        var node = self.candidateNode(a, maxX, baseY + i * 160);
        self.nodes.push(node);
        self.seenArt[a.id] = true;
        try { self.nc.addNode(node); } catch (e) {}
        if (gen) { self.edges.push({ from: [gen.id, "o"], to: [node.id, "i"] }); }
        added++;
      });
      if (added && self.nc) { try { self.nc._renderEdges && self.nc._renderEdges(); } catch (e) {} self.scheduleSave(); }
      if (manual) self.toast(added ? added + " new candidate(s)" : (updated ? updated + " updated" : "up to date"));
      // refresh inspector if a candidate is selected
      var sel = self.selId ? self.node(self.selId) : null;
      if (sel && sel.type === "candidate") self.renderInspector(sel);
    });
  };

  // ---- add / remove nodes ------------------------------------------------
  AssetFlow.prototype.addPaletteNode = function (type) {
    if (!this.nc) return;
    var id = type + "_" + Date.now().toString(36) + (this._addN);
    this._addN = (this._addN + 1) % 1000;
    var off = (this._addN % 6) * 26;
    var base = { id: id, type: type, glyph: GLYPH[type] || "◆", x: 140 + off, y: 110 + off, data: {}, ports: this.portsFor(type) };
    if (type === "reference") { base.title = "Reference"; base.w = 178; base.data = { refName: "" }; }
    else if (type === "prompt") { base.title = "Prompt"; base.w = 252; base.data = { text: "" }; }
    else if (type === "model") { base.title = "Generate · gpt-image"; base.badge = "run"; base.w = 238; base.data = { itemId: null }; }
    else if (type === "edit") { base.title = "Edit · image-edit"; base.badge = "edit"; base.w = 238; base.data = { itemId: null }; }
    else if (type === "candidate") { base.title = "Candidate"; base.w = 202; base.data = { artId: null }; }
    else if (type === "rig") { base.title = "Rig → Godot"; base.badge = "sink"; base.w = 190; base.data = { note: "Approved candidates import to the Godot project." }; }
    else base.title = type;
    this.nodes.push(base);
    try { this.nc.addNode(base); } catch (e) {}
    this.nc.select && this.nc.select(id);
    this.scheduleSave();
  };

  AssetFlow.prototype.removeNode = function (id) {
    this.nodes = this.nodes.filter(function (n) { return n.id !== id; });
    this.edges = this.edges.filter(function (e) { return e.from[0] !== id && e.to[0] !== id; });
    // candidate node removed → allow it to reappear on refresh? keep it hidden.
    try { this.nc.setNodes(this.nodes, this.edges); } catch (e) {}
    if (this.selId === id) { this.selId = null; this.renderInspector(null); }
    this.scheduleSave();
  };

  // ---- persistence -------------------------------------------------------
  AssetFlow.prototype.scheduleSave = function () {
    var self = this;
    if (this._saveT) clearTimeout(this._saveT);
    this._saveT = setTimeout(function () { self._saveT = null; self.save(); }, 700);
  };
  AssetFlow.prototype.save = function () {
    if (this._dead) return;
    var payload = {
      nodes: this.nodes.map(function (n) {
        return {
          id: n.id, type: n.type, glyph: n.glyph, title: n.title, badge: n.badge, accent: n.accent,
          x: Math.round(n.x), y: Math.round(n.y), w: n.w,
          data: {
            text: n.data.text, refName: n.data.refName, kind: n.data.kind, note: n.data.note,
            artId: n.data.artId, itemId: n.data.itemId
          }
        };
      }),
      edges: this.edges
    };
    this.p("/api/workspace/art/asset-flow", { data: payload });
  };

  // ==========================================================================
  // Register with the Studio dispatcher.
  // ==========================================================================
  window.StudioFlows = window.StudioFlows || {};
  window.StudioFlows.asset = {
    label: "Asset flow",
    glyph: "⬡",
    build: function (host, api) {
      try {
        if (!host) return;
        if (!api || !api.NodeCanvas) {
          host.innerHTML = '<div class="empty" style="padding:24px;color:var(--ash)">NodeCanvas engine unavailable.</div>';
          return;
        }
        if (window.AssetFlow && window.AssetFlow.teardown) {
          try { window.AssetFlow.teardown(); } catch (e) {}
        }
        var flow = new AssetFlow(host, api);
        window.AssetFlow = flow;
        flow.build();
      } catch (e) {
        try { host.innerHTML = '<div class="empty" style="padding:24px;color:var(--bad)">asset flow error: ' + String(e && e.message) + '</div>'; } catch (_) {}
      }
    }
  };
})();
