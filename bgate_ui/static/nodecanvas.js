/* NodeCanvas — a small, dependency-free node-editor engine (ComfyUI pattern).
 *
 * An infinite dot-grid canvas you can pan (drag background) and zoom (wheel),
 * holding draggable node cards wired together with bezier connections between
 * typed ports. The host supplies node renderers + data; the engine owns the
 * geometry, interaction, and edge drawing.
 *
 * THE NODE IS THE INSTRUMENT. A node body may contain live widgets — text,
 * numbers, sliders, dropdowns, seeds, images — and they keep focus, caret and
 * half-typed text across repaints. That is the whole design constraint here,
 * and it is why rendering PATCHES nodes instead of replacing them: the old
 * engine did `el.outerHTML = html` on every paint, which silently forbade any
 * input inside a node and forced every parameter out into a side panel.
 *
 *   const nc = new NodeCanvas(hostEl, {
 *     nodes: [{id, type, x, y, w, title, config:{}, ports:{in:[{id,label,type}], out:[...]}}],
 *     edges: [{from:[nodeId, portId], to:[nodeId, portId]}],
 *     renderBody(node) -> html string,          // inner content of a card
 *     onSelect(node|null), onConnect(from,to), onNodeMove(node),
 *     onWidget(node, field, value),             // a widget changed
 *     onNodeChange(node, what),                 // collapsed / resized / noted
 *     onReject(reason, from, to),               // a refused connection
 *     accent: "var(--ember)",
 *     minimap: false,                           // suppress the overview map
 *   });
 *   nc.mount();
 *
 * Build bodies with the widget helpers: NodeCanvas.w.number(n, "count", {...}).
 *
 * CANVAS AFFORDANCES. A graph you can only pan is a graph you get lost in, so
 * the canvas carries the four things a node editor is unusable without: an
 * overview minimap, collapsible nodes, resizable nodes, and free-text note
 * nodes (`{kind:"note"}`) to document the wiring. All of them are additive —
 * none touches a node body, because the body is where the user is typing.
 */
(function () {
  const NS = "http://www.w3.org/2000/svg";

  /* ---- port types --------------------------------------------------------
   * A port declares what flows through it. Untyped ports (or "*") connect to
   * anything, so existing graphs keep working; two declared types must match.
   * Colour is derived from the name so a new type needs no registration and
   * still reads consistently everywhere. */
  const TYPE_COLORS = {
    image: "var(--text)", sheet: "var(--text)", frames: "var(--text)",
    text: "var(--c-narrative)", prompt: "var(--c-narrative)",
    ref: "var(--warn)", asset: "var(--warn)",
    model: "var(--accent)", gltf: "var(--accent)",
    audio: "var(--c-narrative)", task: "var(--bad)", any: "var(--text-3)",
  };
  function typeColor(type) {
    if (!type || type === "*") return TYPE_COLORS.any;
    const key = String(type).toLowerCase();
    if (TYPE_COLORS[key]) return TYPE_COLORS[key];
    let h = 0;
    for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
    return `hsl(${h % 360} 68% 62%)`;
  }
  function typesCompatible(a, b) {
    if (!a || !b || a === "*" || b === "*") return true;
    return String(a).toLowerCase() === String(b).toLowerCase();
  }

  class NodeCanvas {
    constructor(host, opts) {
      this.host = host;
      this.o = opts || {};
      this.nodes = new Map((this.o.nodes || []).map(n => [n.id, n]));
      this.edges = (this.o.edges || []).slice();
      this.pan = { x: this.o.panX || 60, y: this.o.panY || 40 };
      this.zoom = this.o.zoom || 1;
      this.sel = null;
      this._drag = null;      // { id, sx, sy, ox, oy } while moving a node
      this._panning = null;   // { x, y } while panning
      this._link = null;      // { from:[node,port], type, x, y } while wiring
      this._resize = null;    // { id, sx, sy, ow, oh } while resizing a node
      this._marq = null;      // { x0, y0, x, y, add } while box-selecting
      this._map = null;       // { drag } while dragging the minimap viewport
      this._raf = null;
      this._mraf = null;      // separate rAF for the minimap — it repaints on
                              // pan/zoom/drag and must never do so per mousemove
      this._paint = new Map();  // node id -> last painted signature
      this.selection = new Set();  // multi-select; `sel` stays the primary id
    }

    mount() {
      const h = this.host;
      h.classList.add("nc-host");
      h.innerHTML = `
        <div class="nc-grid"></div>
        <svg class="nc-edges"><defs>
          <marker id="nc-arrow" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0 L8 4 L0 8 z" fill="context-stroke"/>
          </marker>
        </defs></svg>
        <div class="nc-world"></div>
        <div class="nc-marq" hidden></div>
        <div class="nc-map" hidden><canvas class="nc-map-c"></canvas></div>
        <div class="nc-toolbar">
          <button class="nc-tb" data-a="fit" title="Fit to view">⊡</button>
          <button class="nc-tb" data-a="zin" title="Zoom in">＋</button>
          <span class="nc-zoom">100%</span>
          <button class="nc-tb" data-a="zout" title="Zoom out">－</button>
        </div>`;
      this.$grid = h.querySelector(".nc-grid");
      this.$svg = h.querySelector(".nc-edges");
      this.$world = h.querySelector(".nc-world");
      this.$zoom = h.querySelector(".nc-zoom");
      this.$marq = h.querySelector(".nc-marq");
      this.$map = h.querySelector(".nc-map");
      this.$mapc = h.querySelector(".nc-map-c");
      this._bindCanvas();
      this._bindMinimap();
      this._renderAll();
      return this;
    }

    /* ---- data ops ------------------------------------------------------- */
    setNodes(nodes, edges) {
      this.nodes = new Map(nodes.map(n => [n.id, n]));
      if (edges) this.edges = edges.slice();
      this._renderAll();
    }
    addNode(node) { this.nodes.set(node.id, node); this._renderNode(node); this._renderEdges(); }
    addEdge(from, to) {
      if (this.edges.some(e => e.from[0] === from[0] && e.from[1] === from[1] && e.to[0] === to[0] && e.to[1] === to[1])) return;
      this.edges.push({ from, to }); this._renderEdges();
    }
    removeNode(id) {
      if (!this.nodes.has(id)) return;
      this.nodes.delete(id);
      this._paint.delete(id);
      this.selection.delete(id);
      this.edges = this.edges.filter(e => e.from[0] !== id && e.to[0] !== id);
      const el = this._el(id); if (el) el.remove();
      if (this.sel === id) { this.sel = null; if (this.o.onSelect) this.o.onSelect(null); }
      this._renderEdges();
      if (this.o.onNodeRemove) this.o.onNodeRemove(id);
    }
    removeEdge(i) { this.edges.splice(i, 1); this._renderEdges(); }

    /** Repaint one node. Pass {body:false} to leave the body alone — that is
     *  what a widget change does, so typing never destroys the field. */
    refreshNode(id, opts) {
      const n = this.nodes.get(id);
      if (n) this._renderNode(n, opts);
      this._renderEdges();
    }

    /* ---- rendering ------------------------------------------------------ */
    _el(id) { return this.$world.querySelector(`[data-node="${CSS.escape(id)}"]`); }

    _renderAll() {
      const live = new Set(this.nodes.keys());
      this.$world.querySelectorAll(".nc-node").forEach(el => {
        if (!live.has(el.dataset.node)) { el.remove(); this._paint.delete(el.dataset.node); }
      });
      for (const n of this.nodes.values()) this._renderNode(n);
      this._applyTransform();
      this._renderEdges();
    }

    /* The node is built once and then patched. Anything that would blow away
     * a focused widget is gated: the body is only re-rendered when its content
     * actually changed AND nothing inside it currently has focus. */
    _renderNode(n, opts) {
      const wantBody = !opts || opts.body !== false;
      let el = this._el(n.id);
      if (!el) {
        el = document.createElement("div");
        el.className = "nc-node";
        el.dataset.node = n.id;
        el.innerHTML = `
          <div class="nc-head" data-drag="${esc(n.id)}">
            <button class="nc-caret" title="collapse">▼</button>
            <span class="nc-ico"></span>
            <span class="nc-title"></span>
            <span class="nc-cost"></span>
            <span class="nc-badge"></span>
          </div>
          <div class="nc-io">
            <div class="nc-ports nc-in"></div>
            <div class="nc-ports nc-out"></div>
          </div>
          <div class="nc-body"></div>
          <div class="nc-size" title="resize"></div>`;
        this.$world.appendChild(el);
      }

      el.style.left = n.x + "px";
      el.style.top = n.y + "px";
      el.style.width = (n.w || (n.kind === "note" ? 240 : 240)) + "px";
      el.style.setProperty("--nc-a", n.accent || this.o.accent || "var(--ember)");
      el.classList.toggle("sel", n.id === this.sel || this.selection.has(n.id));
      el.classList.toggle("nc-collapsed", !!n.collapsed);
      el.classList.toggle("nc-note-node", n.kind === "note");
      el.querySelector(".nc-caret").textContent = n.collapsed ? "▶" : "▼";
      if (n.status) el.dataset.status = n.status; else delete el.dataset.status;

      const head = el.querySelector(".nc-head");
      head.querySelector(".nc-ico").textContent = n.glyph || "◆";
      head.querySelector(".nc-title").textContent = n.title || n.type || "node";
      const cost = head.querySelector(".nc-cost");
      cost.textContent = n.cost || "";
      cost.style.display = n.cost ? "" : "none";
      const badge = head.querySelector(".nc-badge");
      badge.textContent = n.badge || "";
      badge.style.display = n.badge ? "" : "none";

      // A note is an annotation, not a step: it carries no ports at all, so it
      // can never be wired and never blocks a drag across the graph.
      const note = n.kind === "note";
      const sig = JSON.stringify(note ? "note" : [n.ports && n.ports.in, n.ports && n.ports.out]);
      if (this._paint.get(n.id + ":ports") !== sig) {
        this._paint.set(n.id + ":ports", sig);
        el.querySelector(".nc-in").innerHTML = note ? "" : this._ports(n, "in");
        el.querySelector(".nc-out").innerHTML = note ? "" : this._ports(n, "out");
      }

      if (wantBody) {
        const body = el.querySelector(".nc-body");
        const html = note ? this._noteBody(n)
          : ((this.o.renderBody && this.o.renderBody(n)) || n.body || "");
        const focused = body.contains(document.activeElement);
        if (!focused && this._paint.get(n.id + ":body") !== html) {
          this._paint.set(n.id + ":body", html);
          body.innerHTML = html;
        }
        // Height is a style, never a repaint — a note can be resized while its
        // textarea holds the caret.
        if (note) {
          const ta = body.querySelector(".nc-note-ta");
          if (ta) ta.style.height = (n.h || 110) + "px";
        }
      }
    }

    _noteBody(n) {
      const v = (n.config && n.config.text) || n.text || "";
      return `<textarea class="nc-w nc-note-ta" data-w="text"
                placeholder="${esc(n.placeholder || "note…")}">${esc(v)}</textarea>`;
    }

    _ports(n, side) {
      return ((n.ports && n.ports[side]) || []).map(p => {
        const c = typeColor(p.type);
        const label = p.label || p.id;
        const type = p.type && p.type !== "*" ? String(p.type).toUpperCase() : "";
        return `<div class="nc-port nc-port-${side}" data-node="${esc(n.id)}"
                     data-port="${esc(p.id)}" data-type="${esc(p.type || "")}"
                     style="--pc:${c}" title="${esc(label)}${type ? " · " + type : ""}">
                  <span class="nc-dot"></span>
                  <span class="nc-plabel">${esc(label)}${type ? `<b>${esc(type)}</b>` : ""}</span>
                </div>`;
      }).join("");
    }

    _renderEdges() {
      const svg = this.$svg;
      [...svg.querySelectorAll("path.nc-edge, path.nc-hit")].forEach(p => p.remove());
      for (let i = 0; i < this.edges.length; i++) {
        const e = this.edges[i];
        const a = this._portPos(e.from[0], e.from[1], "out");
        const b = this._portPos(e.to[0], e.to[1], "in");
        if (!a || !b) continue;
        const d = this._edgeD(a, b);
        const hit = document.createElementNS(NS, "path");
        hit.setAttribute("class", "nc-hit"); hit.setAttribute("d", d); hit.dataset.i = i;
        svg.appendChild(hit);
        svg.appendChild(this._edgePath(d, false, a.color));
      }
      if (this._link) {
        const a = this._portPos(this._link.from[0], this._link.from[1], "out");
        if (a) svg.appendChild(this._edgePath(this._edgeD(a, this._link), true, a.color));
      }
    }

    _edgeD(a, b) {
      const dx = Math.max(40, Math.abs(b.x - a.x) * 0.5);
      return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
    }
    _edgePath(d, temp, color) {
      const p = document.createElementNS(NS, "path");
      p.setAttribute("class", "nc-edge" + (temp ? " temp" : ""));
      p.setAttribute("d", d);
      // style, not setAttribute: the stylesheet's `stroke` would win over a
      // presentation attribute and every link would render ember regardless of
      // what type it carries.
      if (color) p.style.stroke = color;
      p.setAttribute("marker-end", "url(#nc-arrow)");
      return p;
    }

    /* Port centre in SCREEN coords (the svg overlay is not transformed).
     *
     * MEASURED, not computed. The old version guessed the offset from a fixed
     * head height and a body-height constant, which was already approximate and
     * becomes nonsense the moment a node holds widgets and every card is a
     * different height. The DOM knows where the dot is; ask it. */
    _portPos(nodeId, portId, side) {
      const el = this._el(nodeId);
      const nn = this.nodes.get(nodeId);
      // A collapsed node hides its dots. Its links must not vanish with them —
      // route every edge to the side of the title bar instead, which is what
      // makes collapsing safe to do on a wired graph.
      if (nn && nn.collapsed && el) {
        const head = el.querySelector(".nc-head");
        if (head) {
          const r = head.getBoundingClientRect(), hb = this.host.getBoundingClientRect();
          const list = (nn.ports && nn.ports[side]) || [];
          const p = list.find(x => x.id === portId) || {};
          return { x: (side === "out" ? r.right : r.left) - hb.left,
                   y: r.top - hb.top + r.height / 2, color: typeColor(p.type) };
        }
      }
      const dot = el && el.querySelector(
        `.nc-port-${side}[data-port="${CSS.escape(portId)}"] .nc-dot`);
      if (dot) {
        const r = dot.getBoundingClientRect();
        const h = this.host.getBoundingClientRect();
        const port = dot.parentElement;
        return { x: r.left - h.left + r.width / 2,
                 y: r.top - h.top + r.height / 2,
                 color: typeColor(port && port.dataset.type) };
      }
      // Not laid out yet (first paint): fall back to the old arithmetic so the
      // edge is drawn roughly right rather than dropped entirely.
      const n = this.nodes.get(nodeId);
      if (!n) return null;
      const list = (n.ports && n.ports[side]) || [];
      const idx = Math.max(0, list.findIndex(p => p.id === portId));
      const wx = n.x + (side === "out" ? (n.w || 240) : 0);
      const wy = n.y + 52 + idx * 24;
      return { x: this.pan.x + wx * this.zoom, y: this.pan.y + wy * this.zoom,
               color: typeColor((list[idx] || {}).type) };
    }

    _applyTransform() {
      this.$world.style.transform = `translate(${this.pan.x}px,${this.pan.y}px) scale(${this.zoom})`;
      this.$grid.style.backgroundPosition = `${this.pan.x}px ${this.pan.y}px`;
      this.$grid.style.backgroundSize = `${24 * this.zoom}px ${24 * this.zoom}px`;
      if (this.$zoom) this.$zoom.textContent = Math.round(this.zoom * 100) + "%";
      this._scheduleMap();
    }

    /* ---- minimap --------------------------------------------------------
     * An overview of the whole graph with the viewport as a draggable frame —
     * the only way to know where you are once the graph outgrows the screen.
     * It is a <canvas>, not DOM: a repaint is one draw call, so it can ride the
     * same rAF as the transform and never touches a node (nothing it does can
     * disturb a focused widget). */
    _bindMinimap() {
      if (this.o.minimap === false || !this.$map) return;
      const toGraph = (e) => {
        const g = this._mapGeom(); if (!g) return null;
        const r = this.$mapc.getBoundingClientRect();
        return { x: g.minx + (e.clientX - r.left) / g.s, y: g.miny + (e.clientY - r.top) / g.s };
      };
      const centerOn = (p) => {
        const r = this.host.getBoundingClientRect();
        this.pan.x = r.width / 2 - p.x * this.zoom;
        this.pan.y = r.height / 2 - p.y * this.zoom;
        this._scheduleTransform();
      };
      this.$map.addEventListener("mousedown", e => {
        const p = toGraph(e); if (!p) return;
        e.preventDefault(); e.stopPropagation();
        this._map = { on: true };
        centerOn(p);
      });
      this.$map.addEventListener("wheel", e => e.stopPropagation());
      this._mapMove = (e) => {
        if (!this.host.isConnected) { window.removeEventListener("mousemove", this._mapMove); return; }
        if (this._map) { const p = toGraph(e); if (p) centerOn(p); }
      };
      window.addEventListener("mousemove", this._mapMove);
      window.addEventListener("mouseup", () => { this._map = null; });
    }

    /** Graph bbox + scale for the minimap, or null when there is nothing to show. */
    _mapGeom() {
      const ns = [...this.nodes.values()];
      if (!ns.length || !this.$mapc) return null;
      const r = this.host.getBoundingClientRect();
      const hOf = (n) => { const el = this._el(n.id); return el ? el.offsetHeight : 120; };
      // the viewport is part of the bounds, so panning off into empty space
      // still shows you where you went
      const vx = -this.pan.x / this.zoom, vy = -this.pan.y / this.zoom;
      const xs = ns.map(n => n.x).concat([vx]);
      const ys = ns.map(n => n.y).concat([vy]);
      const xe = ns.map(n => n.x + (n.w || 240)).concat([vx + r.width / this.zoom]);
      const ye = ns.map(n => n.y + hOf(n)).concat([vy + r.height / this.zoom]);
      const minx = Math.min(...xs) - 40, miny = Math.min(...ys) - 40;
      const maxx = Math.max(...xe) + 40, maxy = Math.max(...ye) + 40;
      const cw = this.$mapc.width, ch = this.$mapc.height;
      const s = Math.min(cw / Math.max(1, maxx - minx), ch / Math.max(1, maxy - miny));
      return { minx, miny, maxx, maxy, s, cw, ch, r };
    }

    _scheduleMap() {
      if (this.o.minimap === false || !this.$map || this._mraf) return;
      this._mraf = requestAnimationFrame(() => { this._mraf = null; this._drawMap(); });
    }

    _drawMap() {
      const c = this.$mapc; if (!c) return;
      const host = this.host.getBoundingClientRect();
      // a thumbnail on a postage-stamp canvas is noise, not navigation
      const tiny = host.width < 380 || host.height < 260 || this.nodes.size < 2;
      this.$map.hidden = tiny;
      if (tiny) return;
      if (c.width !== 168) { c.width = 168; c.height = 112; }
      const g = this._mapGeom(); if (!g) return;
      const ctx = c.getContext("2d"); if (!ctx) return;
      const px = (x) => (x - g.minx) * g.s, py = (y) => (y - g.miny) * g.s;
      ctx.clearRect(0, 0, g.cw, g.ch);
      for (const n of this.nodes.values()) {
        const el = this._el(n.id);
        const h = el ? el.offsetHeight : 120;
        const on = n.id === this.sel || this.selection.has(n.id);
        ctx.fillStyle = n.kind === "note" ? "rgba(255,209,102,.28)"
          : on ? "rgba(255,122,60,.85)" : "rgba(154,163,178,.55)";
        ctx.fillRect(px(n.x), py(n.y), Math.max(2, (n.w || 240) * g.s), Math.max(2, h * g.s));
      }
      ctx.strokeStyle = "rgba(255,255,255,.75)";
      ctx.lineWidth = 1;
      ctx.strokeRect(px(-this.pan.x / this.zoom) + .5, py(-this.pan.y / this.zoom) + .5,
        Math.max(4, (g.r.width / this.zoom) * g.s), Math.max(4, (g.r.height / this.zoom) * g.s));
    }

    /* ---- interaction ---------------------------------------------------- */
    _bindCanvas() {
      const h = this.host;
      h.querySelector(".nc-toolbar").addEventListener("click", e => {
        const a = e.target.closest(".nc-tb"); if (!a) return;
        if (a.dataset.a === "zin") this._zoomBy(1.15);
        if (a.dataset.a === "zout") this._zoomBy(1 / 1.15);
        if (a.dataset.a === "fit") this.fit();
      });

      // Wheel zooms the canvas — except over a widget, where the user is
      // scrolling a textarea or spinning a number and would be furious to have
      // the whole graph zoom instead.
      h.addEventListener("wheel", e => {
        if (e.target.closest(".nc-w")) return;
        e.preventDefault();
        const r = h.getBoundingClientRect();
        this._zoomAt(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - r.left, e.clientY - r.top);
      }, { passive: false });

      h.addEventListener("mousedown", e => {
        if (e.target.closest(".nc-map") || e.target.closest(".nc-toolbar")) return;
        // A widget owns its own pointer: no node drag, no pan, no deselect.
        if (e.target.closest(".nc-w")) {
          const node = e.target.closest(".nc-node");
          if (node) this.select(node.dataset.node);
          e.stopPropagation();
          return;
        }
        const nodeEl = e.target.closest(".nc-node");
        if (e.target.closest(".nc-caret")) {
          e.preventDefault(); e.stopPropagation();
          if (nodeEl) this.toggleCollapse(nodeEl.dataset.node);
          return;
        }
        if (e.target.closest(".nc-size")) {
          e.preventDefault(); if (nodeEl) this._startResize(nodeEl.dataset.node, e);
          return;
        }
        const port = e.target.closest(".nc-port-out");
        if (port) { this._startLink(port, e); e.preventDefault(); return; }
        const drag = e.target.closest("[data-drag]");
        if (drag) {
          if (e.shiftKey) this._toggleSel(drag.dataset.drag);
          this._startNodeDrag(drag.dataset.drag, e); e.preventDefault(); return;
        }
        if (nodeEl) {
          if (e.shiftKey) this._toggleSel(nodeEl.dataset.node);
          else this.select(nodeEl.dataset.node);
          return;
        }
        // Empty canvas: plain drag still pans (that is the muscle memory).
        // A modifier — or the right button — draws a marquee instead.
        if (e.button === 2 || e.shiftKey || e.ctrlKey || e.metaKey) {
          e.preventDefault();
          this._startMarquee(e, e.shiftKey || e.ctrlKey || e.metaKey);
          return;
        }
        this.select(null);
        this._panning = { x: e.clientX - this.pan.x, y: e.clientY - this.pan.y };
        h.classList.add("nc-panning");
      });
      // right-drag is a marquee, so suppress the menu it would otherwise open
      h.addEventListener("contextmenu", e => { if (this._marqDidRun) { e.preventDefault(); this._marqDidRun = false; } });

      // Widget changes never repaint the body — that is the point.
      const onEdit = (e) => {
        const w = e.target.closest("[data-w]");
        if (!w) return;
        const nodeEl = w.closest(".nc-node"); if (!nodeEl) return;
        const n = this.nodes.get(nodeEl.dataset.node); if (!n) return;
        const field = w.dataset.w;
        let value = w.type === "checkbox" ? w.checked : w.value;
        if (w.dataset.wtype === "number") value = value === "" ? null : Number(value);
        n.config = n.config || {};
        n.config[field] = value;
        const out = w.parentElement && w.parentElement.querySelector("[data-wout]");
        if (out) out.textContent = value;
        if (this.o.onWidget) this.o.onWidget(n, field, value);
      };
      h.addEventListener("input", onEdit);
      h.addEventListener("change", onEdit);

      // Buttons inside a body (dice, pickers) report as widget actions.
      h.addEventListener("click", e => {
        const b = e.target.closest("[data-wact]"); if (!b) return;
        const nodeEl = b.closest(".nc-node"); if (!nodeEl) return;
        const n = this.nodes.get(nodeEl.dataset.node); if (!n) return;
        e.stopPropagation();
        if (this.o.onAction) this.o.onAction(n, b.dataset.wact, b.dataset.wval || "");
      });

      window.addEventListener("mousemove", e => this._onMove(e));
      window.addEventListener("mouseup", e => this._onUp(e));
      this._onKey = (e) => {
        if (!this.host.isConnected) { window.removeEventListener("keydown", this._onKey); return; }
        if ((e.key === "Delete" || e.key === "Backspace") && (this.sel || this.selection.size)) {
          const t = document.activeElement;
          if (t && (/INPUT|TEXTAREA|SELECT/.test(t.tagName) || t.isContentEditable)) return;
          e.preventDefault();
          const ids = this.selection.size ? [...this.selection] : [this.sel];
          ids.forEach(id => this.removeNode(id));
        }
      };
      window.addEventListener("keydown", this._onKey);
      this.$svg.addEventListener("click", (e) => {
        const p = e.target.closest("path.nc-hit");
        if (p) { const i = +p.dataset.i; if (i >= 0) { this.removeEdge(i); if (this.o.onNodeMove) this.o.onNodeMove(null); } }
      });
    }

    _startNodeDrag(id, e) {
      const n = this.nodes.get(id); if (!n) return;
      if (!this.selection.has(id)) this.select(id);
      // Dragging any member of a selection drags the whole selection — that is
      // what multi-select is FOR; moving them one at a time is the thing it fixes.
      const ids = this.selection.size > 1 && this.selection.has(id) ? [...this.selection] : [id];
      const items = ids.map(i => this.nodes.get(i)).filter(Boolean)
        .map(nn => ({ n: nn, ox: nn.x, oy: nn.y }));
      this._drag = { id, sx: e.clientX, sy: e.clientY, ox: n.x, oy: n.y, items };
    }

    _startResize(id, e) {
      const n = this.nodes.get(id); if (!n) return;
      const el = this._el(id);
      this._resize = { id, sx: e.clientX, sy: e.clientY,
                       ow: n.w || (el ? el.offsetWidth : 240),
                       oh: n.h || (n.kind === "note" ? 110 : 0) };
    }

    _startMarquee(e, add) {
      const r = this.host.getBoundingClientRect();
      if (e.button === 2) this._marqDidRun = true;
      this._marq = { x0: e.clientX - r.left, y0: e.clientY - r.top,
                     x: e.clientX - r.left, y: e.clientY - r.top, add };
      if (!add) this._clearSel();
      this._paintMarquee();
    }
    _paintMarquee() {
      const m = this._marq, el = this.$marq; if (!el) return;
      if (!m) { el.hidden = true; return; }
      el.hidden = false;
      el.style.left = Math.min(m.x0, m.x) + "px";
      el.style.top = Math.min(m.y0, m.y) + "px";
      el.style.width = Math.abs(m.x - m.x0) + "px";
      el.style.height = Math.abs(m.y - m.y0) + "px";
    }
    /** Everything the marquee's screen rect covers, in graph coords. */
    _marqueeHits() {
      const m = this._marq; if (!m) return [];
      const gx = (x) => (x - this.pan.x) / this.zoom, gy = (y) => (y - this.pan.y) / this.zoom;
      const x1 = gx(Math.min(m.x0, m.x)), x2 = gx(Math.max(m.x0, m.x));
      const y1 = gy(Math.min(m.y0, m.y)), y2 = gy(Math.max(m.y0, m.y));
      return [...this.nodes.values()].filter(n => {
        const el = this._el(n.id);
        const h = el ? el.offsetHeight / this.zoom : 120;
        return n.x < x2 && n.x + (n.w || 240) > x1 && n.y < y2 && n.y + h > y1;
      }).map(n => n.id);
    }

    /* ---- collapse / resize / selection (public-ish) ---------------------- */
    /** Collapse to the title bar. Class-only: the body is hidden, never rebuilt,
     *  so half-typed text inside it survives the fold. */
    toggleCollapse(id, force) {
      const n = this.nodes.get(id); if (!n) return;
      n.collapsed = force === undefined ? !n.collapsed : !!force;
      const el = this._el(id);
      if (el) {
        el.classList.toggle("nc-collapsed", !!n.collapsed);
        const c = el.querySelector(".nc-caret");
        if (c) c.textContent = n.collapsed ? "▶" : "▼";
      }
      this._renderEdges(); this._scheduleMap();
      this._changed(n, "collapsed");
    }

    /** One node's selected state, without disturbing the rest. */
    _toggleSel(id) {
      if (!this.selection.size && this.sel) this.selection.add(this.sel);
      if (this.selection.has(id) && this.selection.size > 1) this.selection.delete(id);
      else this.selection.add(id);
      this.sel = this.selection.size === 1 ? [...this.selection][0] : (this.selection.has(id) ? id : this.sel);
      this._paintSel();
      if (this.o.onSelect) this.o.onSelect(this.nodes.get(this.sel) || null);
    }
    selectMany(ids, add) {
      if (!add) this.selection.clear();
      (ids || []).forEach(i => this.nodes.has(i) && this.selection.add(i));
      this.sel = this.selection.size ? [...this.selection][this.selection.size - 1] : null;
      this._paintSel();
      if (this.o.onSelect) this.o.onSelect(this.sel ? this.nodes.get(this.sel) : null);
    }
    selected() { return [...this.selection]; }
    _clearSel() { this.selection.clear(); this._paintSel(); }
    _paintSel() {
      this.$world.querySelectorAll(".nc-node").forEach(el =>
        el.classList.toggle("sel", el.dataset.node === this.sel || this.selection.has(el.dataset.node)));
      this._scheduleMap();
    }

    /** The host's chance to persist a structural edit (collapse / resize / note). */
    _changed(n, what) {
      if (this.o.onNodeChange) this.o.onNodeChange(n, what);
      else if (this.o.onNodeMove) this.o.onNodeMove(n);
    }
    _startLink(portEl, e) {
      const r = this.host.getBoundingClientRect();
      this._link = { from: [portEl.dataset.node, portEl.dataset.port],
                     type: portEl.dataset.type || "",
                     x: e.clientX - r.left, y: e.clientY - r.top };
      // Light up what this port can legally reach, and dim what it cannot.
      this.$world.querySelectorAll(".nc-port-in").forEach(el => {
        const ok = typesCompatible(this._link.type, el.dataset.type)
          && el.dataset.node !== this._link.from[0];
        el.classList.toggle("nc-ok", ok);
        el.classList.toggle("nc-no", !ok);
      });
    }
    _onMove(e) {
      if (this._panning) {
        this.pan = { x: e.clientX - this._panning.x, y: e.clientY - this._panning.y };
        this._scheduleTransform(); return;
      }
      if (this._resize) {
        const n = this.nodes.get(this._resize.id); if (!n) return;
        const el = this._el(n.id);
        n.w = Math.max(160, Math.min(760, this._resize.ow + (e.clientX - this._resize.sx) / this.zoom));
        if (el) el.style.width = n.w + "px";
        if (n.kind === "note") {
          n.h = Math.max(48, Math.min(700, this._resize.oh + (e.clientY - this._resize.sy) / this.zoom));
          const ta = el && el.querySelector(".nc-note-ta");
          if (ta) ta.style.height = n.h + "px";
        }
        this._renderEdges(); this._scheduleMap(); return;
      }
      if (this._marq) {
        const r = this.host.getBoundingClientRect();
        this._marq.x = e.clientX - r.left; this._marq.y = e.clientY - r.top;
        this._paintMarquee();
        this.selectMany(this._marqueeHits(), this._marq.add);
        return;
      }
      if (this._drag) {
        const dx = (e.clientX - this._drag.sx) / this.zoom, dy = (e.clientY - this._drag.sy) / this.zoom;
        for (const it of this._drag.items) {
          it.n.x = it.ox + dx; it.n.y = it.oy + dy;
          const el = this._el(it.n.id);
          if (el) { el.style.left = it.n.x + "px"; el.style.top = it.n.y + "px"; }
        }
        this._renderEdges(); this._scheduleMap(); return;
      }
      if (this._link) {
        const r = this.host.getBoundingClientRect();
        this._link.x = e.clientX - r.left; this._link.y = e.clientY - r.top;
        this._renderEdges();
      }
    }
    _onUp(e) {
      if (this._panning) { this._panning = null; this.host.classList.remove("nc-panning"); }
      if (this._resize) {
        const n = this.nodes.get(this._resize.id); this._resize = null;
        if (n) this._changed(n, "resize");
      }
      if (this._marq) { this._marq = null; this._paintMarquee(); }
      if (this._drag) {
        const items = this._drag.items || [];
        this._drag = null;
        if (this.o.onNodeMove) items.forEach(it => this.o.onNodeMove(it.n));
      }
      if (this._link) {
        const tgt = e.target.closest(".nc-port-in");
        if (tgt) {
          const to = [tgt.dataset.node, tgt.dataset.port];
          const why = this._whyNot(this._link, tgt);
          if (why) {
            if (this.o.onReject) this.o.onReject(why, this._link.from, to);
          } else {
            this.addEdge(this._link.from, to);
            if (this.o.onConnect) this.o.onConnect(this._link.from, to);
          }
        }
        this._link = null;
        this.$world.querySelectorAll(".nc-ok,.nc-no").forEach(
          el => el.classList.remove("nc-ok", "nc-no"));
        this._renderEdges();
      }
    }
    /** Why this connection is refused, or "" if it is fine. */
    _whyNot(link, tgt) {
      if (tgt.dataset.node === link.from[0]) return "a node cannot feed itself";
      const a = link.type, b = tgt.dataset.type || "";
      if (!typesCompatible(a, b)) {
        return `${(a || "any").toUpperCase()} does not fit ${(b || "any").toUpperCase()}`;
      }
      return "";
    }
    _scheduleTransform() {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = null; this._applyTransform(); this._renderEdges(); });
    }

    select(id) {
      if (this.sel === id && this.selection.size <= 1) return;
      this.sel = id;
      this.selection.clear();
      if (id) this.selection.add(id);
      this._paintSel();
      if (this.o.onSelect) this.o.onSelect(id ? this.nodes.get(id) : null);
    }

    _zoomBy(f) { const r = this.host.getBoundingClientRect(); this._zoomAt(f, r.width / 2, r.height / 2); }
    _zoomAt(f, cx, cy) {
      const z2 = Math.min(2.2, Math.max(0.25, this.zoom * f));
      const k = z2 / this.zoom;
      this.pan.x = cx - (cx - this.pan.x) * k;
      this.pan.y = cy - (cy - this.pan.y) * k;
      this.zoom = z2;
      this._applyTransform(); this._renderEdges();
    }
    /* Fit what matters, at a size you can read.
     *
     * Fitting a 7-node chain of TALL widget-bearing nodes to BOTH axes landed
     * at 30% — a picture of a graph, not a graph you can use. So: fit the
     * selection when there is one, fit WIDTH first, and never go below a
     * readable floor; if the graph is then taller than the viewport, align its
     * top and let the user scroll rather than shrinking the text away. */
    fit(opts) {
      const o = opts || {};
      const pool = (this.selection.size ? [...this.selection].map(i => this.nodes.get(i)).filter(Boolean) : null);
      const ns = (pool && pool.length ? pool : [...this.nodes.values()]);
      if (!ns.length) return;
      const floor = o.min != null ? o.min : (this.o.fitMin != null ? this.o.fitMin : 0.55);
      const hOf = (n) => { const el = this._el(n.id); return el ? el.offsetHeight : 200; };
      const minx = Math.min(...ns.map(n => n.x)) - 40, miny = Math.min(...ns.map(n => n.y)) - 40;
      const maxx = Math.max(...ns.map(n => n.x + (n.w || 240))) + 40;
      const maxy = Math.max(...ns.map(n => n.y + hOf(n))) + 40;
      const gw = Math.max(1, maxx - minx), gh = Math.max(1, maxy - miny);
      const r = this.host.getBoundingClientRect();
      const z = Math.max(floor, Math.min(1.3, r.width / gw, r.height / gh));
      this.zoom = z;
      this.pan.x = (r.width - gw * z) / 2 - minx * z;
      this.pan.y = gh * z > r.height ? 24 - miny * z : (r.height - gh * z) / 2 - miny * z;
      this._applyTransform(); this._renderEdges();
    }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ---- widgets -----------------------------------------------------------
   * Every helper returns an HTML string for a node body and reads its current
   * value from `node.config`. `.nc-w` is the marker the canvas uses to keep its
   * hands off — pointer, wheel and keys all stop at a widget. */
  const val = (n, field, dflt) => {
    const v = n && n.config ? n.config[field] : undefined;
    return v === undefined || v === null || v === "" ? dflt : v;
  };
  const row = (label, control, hint) =>
    `<label class="nc-row"><span class="nc-lab">${esc(label)}</span>${control}</label>` +
    (hint ? `<div class="nc-hint">${esc(hint)}</div>` : "");

  const w = {
    text(n, field, o) {
      o = o || {};
      const v = val(n, field, o.value || "");
      if (o.rows && o.rows > 1) {
        return row(o.label || field,
          `<textarea class="nc-w nc-ta" data-w="${esc(field)}" rows="${o.rows}"
             placeholder="${esc(o.placeholder || "")}">${esc(v)}</textarea>`, o.hint);
      }
      return row(o.label || field,
        `<input class="nc-w nc-in-t" data-w="${esc(field)}" value="${esc(v)}"
           placeholder="${esc(o.placeholder || "")}">`, o.hint);
    },
    number(n, field, o) {
      o = o || {};
      const v = val(n, field, o.value != null ? o.value : 0);
      return row(o.label || field,
        `<span class="nc-num">
           <button class="nc-w nc-step" data-wact="dec" data-wval="${esc(field)}" tabindex="-1">−</button>
           <input class="nc-w nc-in-n" data-w="${esc(field)}" data-wtype="number" type="number"
             value="${esc(v)}"${o.min != null ? ` min="${o.min}"` : ""}${o.max != null ? ` max="${o.max}"` : ""}${o.step ? ` step="${o.step}"` : ""}>
           <button class="nc-w nc-step" data-wact="inc" data-wval="${esc(field)}" tabindex="-1">+</button>
         </span>`, o.hint);
    },
    slider(n, field, o) {
      o = o || {};
      const v = val(n, field, o.value != null ? o.value : 0);
      return row(o.label || field,
        `<span class="nc-sl">
           <input class="nc-w" data-w="${esc(field)}" data-wtype="number" type="range"
             value="${esc(v)}" min="${o.min != null ? o.min : 0}" max="${o.max != null ? o.max : 100}"
             step="${o.step || 1}">
           <b data-wout>${esc(v)}</b>
         </span>`, o.hint);
    },
    select(n, field, o) {
      o = o || {};
      const v = String(val(n, field, o.value || ""));
      const opts = (o.options || []).map(x => {
        const value = x.value !== undefined ? x.value : x;
        const label = x.label !== undefined ? x.label : value;
        return `<option value="${esc(value)}"${String(value) === v ? " selected" : ""}>${esc(label)}</option>`;
      }).join("");
      return row(o.label || field,
        `<select class="nc-w nc-sel" data-w="${esc(field)}">${opts}</select>`, o.hint);
    },
    toggle(n, field, o) {
      o = o || {};
      const v = !!val(n, field, !!o.value);
      return row(o.label || field,
        `<input class="nc-w nc-ck" data-w="${esc(field)}" type="checkbox"${v ? " checked" : ""}>`,
        o.hint);
    },
    seed(n, field, o) {
      o = o || {};
      const v = val(n, field, 0);
      return row(o.label || "seed",
        `<span class="nc-num">
           <input class="nc-w nc-in-n" data-w="${esc(field)}" data-wtype="number" type="number" value="${esc(v)}">
           <button class="nc-w nc-step nc-dice" data-wact="reseed" data-wval="${esc(field)}" title="randomise">⚄</button>
         </span>`, o.hint);
    },
    /** A real image on the node — the difference between a card that describes
     *  a picture and a node that shows one. */
    image(src, o) {
      o = o || {};
      if (!src) return `<div class="nc-img nc-img-empty">${esc(o.empty || "no image yet")}</div>`;
      return `<div class="nc-img"><img src="${esc(src)}" alt="${esc(o.alt || "")}"
                loading="lazy" onerror="this.parentElement.classList.add('nc-img-broken')">
              ${o.caption ? `<span class="nc-cap">${esc(o.caption)}</span>` : ""}</div>`;
    },
    note(text) { return `<div class="nc-note">${esc(text)}</div>`; },
    tag(text) { return `<span class="nc-tag">${esc(text)}</span>`; },
  };

  /** A comment node. Ports are meaningless on it, so it declares none. */
  NodeCanvas.noteNode = function (o) {
    o = o || {};
    return { id: o.id || ("note_" + Math.random().toString(36).slice(2, 8)),
             kind: "note", type: "note", title: o.title || "note", glyph: "✎",
             x: o.x || 40, y: o.y || 40, w: o.w || 240, h: o.h || 110,
             config: { text: o.text || "" } };
  };

  NodeCanvas.w = w;
  NodeCanvas.typeColor = typeColor;
  NodeCanvas.typesCompatible = typesCompatible;
  NodeCanvas.esc = esc;
  window.NodeCanvas = NodeCanvas;

  /* engine styles (injected once) */
  if (!document.getElementById("nc-style")) {
    const s = document.createElement("style");
    s.id = "nc-style";
    s.textContent = `
      .nc-host{position:relative;width:100%;height:100%;overflow:hidden;background:var(--bg);border:1px solid var(--seam);border-radius:12px;--nc-edge:var(--ember)}
      .nc-grid{position:absolute;inset:0;background-image:radial-gradient(var(--grid-dot,rgba(255,255,255,.06)) 1px,transparent 1px);background-size:24px 24px}
      .nc-edges{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
      .nc-edge{fill:none;stroke:var(--nc-edge);stroke-width:2;opacity:.8}
      .nc-edge.temp{stroke-dasharray:5 4;opacity:.95}
      .nc-hit{fill:none;stroke:transparent;stroke-width:16;pointer-events:stroke;cursor:pointer}
      .nc-hit:hover{stroke:var(--nc-edge);opacity:.18}
      .nc-world{position:absolute;top:0;left:0;transform-origin:0 0}
      .nc-node{position:absolute;background:var(--plate);border:1px solid var(--seam);border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.35);transition:border-color .12s,box-shadow .12s}
      .nc-node.sel{border-color:var(--nc-a);box-shadow:0 0 0 1px var(--nc-a),0 10px 30px rgba(0,0,0,.5)}
      .nc-node[data-status="running"]{border-color:var(--text);box-shadow:0 0 0 1px var(--text),0 10px 30px rgba(0,0,0,.5)}
      .nc-node[data-status="passed"]{border-color:var(--accent)}
      .nc-node[data-status="failed"]{border-color:var(--bad)}
      .nc-head{display:flex;align-items:center;gap:8px;padding:9px 11px;border-bottom:1px solid var(--seam);cursor:grab;border-radius:12px 12px 0 0;user-select:none}
      .nc-head:active{cursor:grabbing}
      .nc-ico{color:var(--nc-a);font-size:13px;width:16px;text-align:center}
      .nc-title{font-size:12.5px;font-weight:var(--fw-semi);color:var(--bone);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
      .nc-cost{font-family:var(--mono);font-size:9.5px;color:var(--ash);opacity:.85;white-space:nowrap}
      .nc-badge{font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--nc-a);border:1px solid var(--nc-a);border-radius:20px;padding:1px 7px;opacity:.9}
      .nc-body{padding:9px 11px;font-size:12px;color:var(--ash);line-height:1.45;min-height:18px}

      /* ports: a labelled, typed terminal — not a bare dot with a tooltip.
         They occupy their own band in normal flow so the body starts BELOW
         them; absolutely positioning them over the card made every label sit
         on top of the first widget. Only the dot hangs past the border. */
      .nc-io{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:8px 11px 0}
      .nc-io:empty{display:none}
      .nc-ports{display:flex;flex-direction:column;gap:8px}
      .nc-in{margin-left:-18px}
      .nc-out{margin-right:-18px;align-items:flex-end}
      .nc-ports:empty{display:none}
      .nc-port{display:flex;align-items:center;gap:6px;cursor:crosshair;--pc:var(--nc-a)}
      .nc-out .nc-port{flex-direction:row-reverse}
      .nc-dot{width:11px;height:11px;border-radius:50%;background:var(--plate2);border:2px solid var(--pc);transition:transform .1s;flex:none}
      .nc-port:hover .nc-dot{transform:scale(1.35);background:var(--pc)}
      .nc-plabel{font-size:9.5px;color:var(--ash);white-space:nowrap;opacity:.9;pointer-events:none;display:flex;gap:4px;align-items:baseline}
      .nc-plabel b{font-family:var(--mono);font-size:8px;letter-spacing:.06em;color:var(--pc);opacity:.95}
      .nc-port.nc-ok .nc-dot{box-shadow:0 0 0 4px color-mix(in srgb,var(--pc) 30%,transparent)}
      .nc-port.nc-no{opacity:.3}

      /* widgets — the node holds its own parameters */
      .nc-row{display:flex;align-items:center;gap:8px;margin:5px 0;cursor:default}
      .nc-lab{font-size:10.5px;color:var(--ash);opacity:.85;min-width:64px;flex:none}
      .nc-hint{font-size:10px;color:var(--ash);opacity:.6;margin:-2px 0 6px 72px}
      .nc-w{font-family:inherit}
      .nc-in-t,.nc-ta,.nc-sel,.nc-in-n{flex:1;min-width:0;background:var(--iron);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font-size:11.5px;padding:4px 7px}
      .nc-ta{resize:vertical;line-height:1.4;font-family:var(--mono);font-size:11px}
      .nc-in-t:focus,.nc-ta:focus,.nc-sel:focus,.nc-in-n:focus{outline:none;border-color:var(--nc-a)}
      .nc-num{display:flex;align-items:center;gap:3px;flex:1}
      .nc-in-n{text-align:center;-moz-appearance:textfield}
      .nc-in-n::-webkit-outer-spin-button,.nc-in-n::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
      .nc-step{width:20px;height:22px;flex:none;border:1px solid var(--seam);background:var(--iron);color:var(--ash);border-radius:6px;cursor:pointer;font-size:12px;line-height:1}
      .nc-step:hover{color:var(--bone);border-color:var(--nc-a)}
      .nc-dice{width:24px}
      .nc-sl{display:flex;align-items:center;gap:7px;flex:1}
      .nc-sl input[type=range]{flex:1;accent-color:var(--nc-a);height:3px}
      .nc-sl b{font-family:var(--mono);font-size:10.5px;color:var(--bone);min-width:26px;text-align:right}
      .nc-ck{accent-color:var(--nc-a);width:14px;height:14px}
      .nc-img{position:relative;margin:6px 0;border-radius:8px;overflow:hidden;background:var(--iron);border:1px solid var(--seam);min-height:40px}
      .nc-img img{display:block;width:100%;height:auto;max-height:220px;object-fit:contain}
      .nc-img-empty{display:flex;align-items:center;justify-content:center;padding:16px;font-size:10.5px;color:var(--ash);opacity:.6}
      .nc-img-broken img{display:none}
      .nc-cap{position:absolute;left:6px;bottom:5px;font-family:var(--mono);font-size:9px;color:var(--bone);background:rgba(0,0,0,.6);padding:1px 5px;border-radius:4px}
      .nc-note{font-size:11px;opacity:.8;margin:3px 0}
      .nc-tag{display:inline-block;font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:var(--nc-a);border:1px solid var(--nc-a);opacity:.85;border-radius:20px;padding:1px 7px;margin:3px 3px 0 0}

      .nc-toolbar{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:4px;background:var(--iron);border:1px solid var(--seam);border-radius:10px;padding:5px 8px;z-index:5}
      .nc-tb{width:26px;height:26px;border:0;border-radius:7px;background:transparent;color:var(--ash);cursor:pointer;font-size:14px}
      .nc-tb:hover{background:var(--plate2);color:var(--bone)}
      .nc-zoom{font-family:var(--mono);font-size:11px;color:var(--ash);min-width:40px;text-align:center}
      .nc-host.nc-panning{cursor:grabbing}
      .nc-host.nc-panning .nc-world{pointer-events:none}

      /* collapse / resize / marquee / minimap / notes */
      .nc-caret{width:14px;height:14px;flex:none;border:0;background:transparent;color:var(--ash);
                font-size:8px;line-height:1;cursor:pointer;padding:0;opacity:.7}
      .nc-caret:hover{color:var(--bone);opacity:1}
      .nc-node.nc-collapsed{width:auto!important;min-width:150px}
      .nc-node.nc-collapsed .nc-io,.nc-node.nc-collapsed .nc-body,.nc-node.nc-collapsed .nc-size{display:none}
      .nc-node.nc-collapsed .nc-head{border-bottom:0;border-radius:12px}
      .nc-size{position:absolute;right:0;bottom:0;width:14px;height:14px;cursor:nwse-resize;opacity:0;
               background:linear-gradient(135deg,transparent 50%,var(--nc-a) 50%);border-radius:0 0 11px 0}
      .nc-node:hover .nc-size,.nc-node.sel .nc-size{opacity:.55}
      .nc-size:hover{opacity:1!important}
      .nc-note-node{background:color-mix(in srgb,var(--warn) 8%,var(--plate));z-index:0}
      .nc-world .nc-node{z-index:1}
      .nc-world .nc-note-node{z-index:0}
      .nc-note-node .nc-body{padding:7px}
      .nc-note-ta{width:100%;box-sizing:border-box;resize:none;background:transparent;border:0;
                  color:var(--bone);font-size:11.5px;line-height:1.5;font-family:inherit;outline:none}
      .nc-marq{position:absolute;border:1px solid var(--nc-a);background:rgba(255,122,60,.10);
               border-radius:3px;pointer-events:none;z-index:4}
      .nc-map{position:absolute;right:12px;bottom:12px;z-index:5;background:rgba(10,12,15,.82);
              border:1px solid var(--seam);border-radius:8px;padding:4px;cursor:crosshair;line-height:0}
      .nc-map canvas{display:block;width:168px;height:112px;border-radius:5px}
    `;
    document.head.appendChild(s);
  }
})();
