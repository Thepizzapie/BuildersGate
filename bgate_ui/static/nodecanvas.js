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
 *     onReject(reason, from, to),               // a refused connection
 *     accent: "var(--ember)",
 *   });
 *   nc.mount();
 *
 * Build bodies with the widget helpers: NodeCanvas.w.number(n, "count", {...}).
 */
(function () {
  const NS = "http://www.w3.org/2000/svg";

  /* ---- port types --------------------------------------------------------
   * A port declares what flows through it. Untyped ports (or "*") connect to
   * anything, so existing graphs keep working; two declared types must match.
   * Colour is derived from the name so a new type needs no registration and
   * still reads consistently everywhere. */
  const TYPE_COLORS = {
    image: "#4aa3ff", sheet: "#4aa3ff", frames: "#57c7ff",
    text: "#9a7bff", prompt: "#9a7bff",
    ref: "#ff9f43", asset: "#ffd166",
    model: "#2ec4b6", gltf: "#2ec4b6",
    audio: "#ff6ec7", task: "#ff6a3d", any: "#7c8695",
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
      this._raf = null;
      this._paint = new Map();  // node id -> last painted signature
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
      this._bindCanvas();
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
            <span class="nc-ico"></span>
            <span class="nc-title"></span>
            <span class="nc-cost"></span>
            <span class="nc-badge"></span>
          </div>
          <div class="nc-io">
            <div class="nc-ports nc-in"></div>
            <div class="nc-ports nc-out"></div>
          </div>
          <div class="nc-body"></div>`;
        this.$world.appendChild(el);
      }

      el.style.left = n.x + "px";
      el.style.top = n.y + "px";
      el.style.width = (n.w || 240) + "px";
      el.style.setProperty("--nc-a", n.accent || this.o.accent || "var(--ember)");
      el.classList.toggle("sel", n.id === this.sel);
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

      const sig = JSON.stringify([n.ports && n.ports.in, n.ports && n.ports.out]);
      if (this._paint.get(n.id + ":ports") !== sig) {
        this._paint.set(n.id + ":ports", sig);
        el.querySelector(".nc-in").innerHTML = this._ports(n, "in");
        el.querySelector(".nc-out").innerHTML = this._ports(n, "out");
      }

      if (wantBody) {
        const body = el.querySelector(".nc-body");
        const html = (this.o.renderBody && this.o.renderBody(n)) || n.body || "";
        const focused = body.contains(document.activeElement);
        if (!focused && this._paint.get(n.id + ":body") !== html) {
          this._paint.set(n.id + ":body", html);
          body.innerHTML = html;
        }
      }
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
        // A widget owns its own pointer: no node drag, no pan, no deselect.
        if (e.target.closest(".nc-w")) {
          const node = e.target.closest(".nc-node");
          if (node) this.select(node.dataset.node);
          e.stopPropagation();
          return;
        }
        const port = e.target.closest(".nc-port-out");
        if (port) { this._startLink(port, e); e.preventDefault(); return; }
        const drag = e.target.closest("[data-drag]");
        if (drag) { this._startNodeDrag(drag.dataset.drag, e); e.preventDefault(); return; }
        const node = e.target.closest(".nc-node");
        if (node) { this.select(node.dataset.node); return; }
        this.select(null);
        this._panning = { x: e.clientX - this.pan.x, y: e.clientY - this.pan.y };
        h.classList.add("nc-panning");
      });

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
        if ((e.key === "Delete" || e.key === "Backspace") && this.sel) {
          const t = document.activeElement;
          if (t && (/INPUT|TEXTAREA|SELECT/.test(t.tagName) || t.isContentEditable)) return;
          e.preventDefault(); this.removeNode(this.sel);
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
      this.select(id);
      this._drag = { id, sx: e.clientX, sy: e.clientY, ox: n.x, oy: n.y };
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
      if (this._drag) {
        const n = this.nodes.get(this._drag.id); if (!n) return;
        n.x = this._drag.ox + (e.clientX - this._drag.sx) / this.zoom;
        n.y = this._drag.oy + (e.clientY - this._drag.sy) / this.zoom;
        const el = this._el(n.id);
        if (el) { el.style.left = n.x + "px"; el.style.top = n.y + "px"; }
        this._renderEdges(); return;
      }
      if (this._link) {
        const r = this.host.getBoundingClientRect();
        this._link.x = e.clientX - r.left; this._link.y = e.clientY - r.top;
        this._renderEdges();
      }
    }
    _onUp(e) {
      if (this._panning) { this._panning = null; this.host.classList.remove("nc-panning"); }
      if (this._drag) { const n = this.nodes.get(this._drag.id); this._drag = null; if (n && this.o.onNodeMove) this.o.onNodeMove(n); }
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
      if (this.sel === id) return;
      this.sel = id;
      this.$world.querySelectorAll(".nc-node").forEach(el => el.classList.toggle("sel", el.dataset.node === id));
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
    fit() {
      const ns = [...this.nodes.values()]; if (!ns.length) return;
      const hOf = (n) => { const el = this._el(n.id); return el ? el.offsetHeight : 200; };
      const minx = Math.min(...ns.map(n => n.x)) - 40, miny = Math.min(...ns.map(n => n.y)) - 40;
      const maxx = Math.max(...ns.map(n => n.x + (n.w || 240))) + 40;
      const maxy = Math.max(...ns.map(n => n.y + hOf(n))) + 40;
      const r = this.host.getBoundingClientRect();
      const z = Math.min(1.3, r.width / (maxx - minx), r.height / (maxy - miny));
      this.zoom = Math.max(0.3, z);
      this.pan.x = (r.width - (maxx - minx) * this.zoom) / 2 - minx * this.zoom;
      this.pan.y = (r.height - (maxy - miny) * this.zoom) / 2 - miny * this.zoom;
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
      .nc-host{position:relative;width:100%;height:100%;overflow:hidden;background:#0b0d10;border:1px solid var(--seam);border-radius:12px;--nc-edge:var(--ember)}
      .nc-grid{position:absolute;inset:0;background-image:radial-gradient(var(--grid-dot,rgba(255,255,255,.06)) 1px,transparent 1px);background-size:24px 24px}
      .nc-edges{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
      .nc-edge{fill:none;stroke:var(--nc-edge);stroke-width:2;opacity:.8}
      .nc-edge.temp{stroke-dasharray:5 4;opacity:.95}
      .nc-hit{fill:none;stroke:transparent;stroke-width:16;pointer-events:stroke;cursor:pointer}
      .nc-hit:hover{stroke:var(--nc-edge);opacity:.18}
      .nc-world{position:absolute;top:0;left:0;transform-origin:0 0}
      .nc-node{position:absolute;background:var(--plate,#14161c);border:1px solid var(--seam,#262a33);border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.35);transition:border-color .12s,box-shadow .12s}
      .nc-node.sel{border-color:var(--nc-a);box-shadow:0 0 0 1px var(--nc-a),0 10px 30px rgba(0,0,0,.5)}
      .nc-node[data-status="running"]{border-color:#4aa3ff;box-shadow:0 0 0 1px #4aa3ff55,0 10px 30px rgba(0,0,0,.5)}
      .nc-node[data-status="passed"]{border-color:#2ec4b6}
      .nc-node[data-status="failed"]{border-color:#ff5c5c}
      .nc-head{display:flex;align-items:center;gap:8px;padding:9px 11px;border-bottom:1px solid var(--seam);cursor:grab;border-radius:12px 12px 0 0;user-select:none}
      .nc-head:active{cursor:grabbing}
      .nc-ico{color:var(--nc-a);font-size:13px;width:16px;text-align:center}
      .nc-title{font-size:12.5px;font-weight:600;color:var(--bone);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
      .nc-cost{font-family:var(--mono);font-size:9.5px;color:var(--ash);opacity:.85;white-space:nowrap}
      .nc-badge{font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--nc-a);border:1px solid var(--nc-a);border-radius:20px;padding:1px 7px;opacity:.9}
      .nc-body{padding:9px 11px;font-size:12px;color:var(--ash,#9aa3b2);line-height:1.45;min-height:18px}

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
      .nc-dot{width:11px;height:11px;border-radius:50%;background:var(--plate2,#1b1f27);border:2px solid var(--pc);transition:transform .1s;flex:none}
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
      .nc-in-t,.nc-ta,.nc-sel,.nc-in-n{flex:1;min-width:0;background:var(--iron,#0e1116);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font-size:11.5px;padding:4px 7px}
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

      .nc-toolbar{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:4px;background:var(--iron,#0e1116);border:1px solid var(--seam);border-radius:10px;padding:5px 8px;z-index:5}
      .nc-tb{width:26px;height:26px;border:0;border-radius:7px;background:transparent;color:var(--ash);cursor:pointer;font-size:14px}
      .nc-tb:hover{background:var(--plate2);color:var(--bone)}
      .nc-zoom{font-family:var(--mono);font-size:11px;color:var(--ash);min-width:40px;text-align:center}
      .nc-host.nc-panning{cursor:grabbing}
      .nc-host.nc-panning .nc-world{pointer-events:none}
    `;
    document.head.appendChild(s);
  }
})();
