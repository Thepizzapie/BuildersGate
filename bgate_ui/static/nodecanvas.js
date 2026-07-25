/* NodeCanvas — a small, dependency-free node-editor engine (Weavy/n8n pattern).
 *
 * An infinite dot-grid canvas you can pan (drag background) and zoom (wheel),
 * holding draggable node cards wired together with bezier connections between
 * typed ports. The host supplies node renderers + data; the engine owns the
 * geometry, interaction, and edge drawing. Used by the asset-generation flow
 * and the agent-orchestration flow.
 *
 *   const nc = new NodeCanvas(hostEl, {
 *     nodes: [{id, type, x, y, w, title, ports:{in:[{id,label}], out:[...]}, body?}],
 *     edges: [{from:[nodeId, portId], to:[nodeId, portId]}],
 *     renderBody(node) -> html string,          // inner content of a card
 *     onSelect(node|null), onConnect(from,to), onNodeMove(node),
 *     accent: "var(--ember)",
 *   });
 *   nc.mount();
 */
(function () {
  const NS = "http://www.w3.org/2000/svg";

  class NodeCanvas {
    constructor(host, opts) {
      this.host = host;
      this.o = opts || {};
      this.nodes = new Map((this.o.nodes || []).map(n => [n.id, n]));
      this.edges = (this.o.edges || []).slice();
      this.pan = { x: this.o.panX || 60, y: this.o.panY || 40 };
      this.zoom = this.o.zoom || 1;
      this.sel = null;
      this._drag = null;      // { node, dx, dy } while moving a node
      this._panning = null;   // { x, y } while panning
      this._link = null;      // { from:[node,port], x, y } while dragging a connection
      this._raf = null;
    }

    mount() {
      const h = this.host;
      h.classList.add("nc-host");
      h.innerHTML = `
        <div class="nc-grid"></div>
        <svg class="nc-edges"><defs>
          <marker id="nc-arrow" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0 L8 4 L0 8 z" fill="var(--nc-edge, #ff6a3d)"/>
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
      this.edges = this.edges.filter(e => e.from[0] !== id && e.to[0] !== id);
      const el = this.$world.querySelector(`[data-node="${id}"]`); if (el) el.remove();
      if (this.sel === id) { this.sel = null; if (this.o.onSelect) this.o.onSelect(null); }
      this._renderEdges();
      if (this.o.onNodeRemove) this.o.onNodeRemove(id);
    }
    removeEdge(i) { this.edges.splice(i, 1); this._renderEdges(); }

    /* ---- rendering ------------------------------------------------------ */
    _renderAll() {
      this.$world.innerHTML = "";
      for (const n of this.nodes.values()) this._renderNode(n);
      this._applyTransform();
      this._renderEdges();
    }

    _renderNode(n) {
      let el = this.$world.querySelector(`[data-node="${n.id}"]`);
      const accent = n.accent || this.o.accent || "var(--ember)";
      const ports = (side) => (n.ports && n.ports[side] || []).map(p =>
        `<div class="nc-port nc-port-${side}" data-node="${n.id}" data-port="${p.id}" title="${esc(p.label || p.id)}"><span></span></div>`).join("");
      const html = `
        <div class="nc-node ${n.id === this.sel ? "sel" : ""}" data-node="${n.id}"
             style="left:${n.x}px;top:${n.y}px;width:${n.w || 220}px;--nc-a:${accent}">
          <div class="nc-head" data-drag="${n.id}">
            <span class="nc-ico">${n.glyph || "◆"}</span>
            <span class="nc-title">${esc(n.title || n.type || "node")}</span>
            ${n.badge ? `<span class="nc-badge">${esc(n.badge)}</span>` : ""}
          </div>
          <div class="nc-body">${(this.o.renderBody && this.o.renderBody(n)) || (n.body || "")}</div>
          <div class="nc-ports nc-in">${ports("in")}</div>
          <div class="nc-ports nc-out">${ports("out")}</div>
        </div>`;
      if (el) el.outerHTML = html; else this.$world.insertAdjacentHTML("beforeend", html);
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
        svg.appendChild(this._edgePath(d));
      }
      if (this._link) {
        const a = this._portPos(this._link.from[0], this._link.from[1], "out");
        if (a) svg.appendChild(this._edgePath(this._edgeD(a, this._link), true));
      }
    }

    _edgeD(a, b) {
      const dx = Math.max(40, Math.abs(b.x - a.x) * 0.5);
      return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
    }
    _edgePath(d, temp) {
      const p = document.createElementNS(NS, "path");
      p.setAttribute("class", "nc-edge" + (temp ? " temp" : ""));
      p.setAttribute("d", d);
      p.setAttribute("marker-end", "url(#nc-arrow)");
      return p;
    }

    // port center in SCREEN coords (svg overlay is not transformed)
    _portPos(nodeId, portId, side) {
      const n = this.nodes.get(nodeId);
      if (!n) return null;
      const list = (n.ports && n.ports[side]) || [];
      const idx = Math.max(0, list.findIndex(p => p.id === portId));
      const w = n.w || 220;
      const headH = 34, portGap = 22, portTop = (n._bodyH || 60) + headH + 14;
      const wx = n.x + (side === "out" ? w : 0);
      const wy = n.y + portTop + idx * portGap;
      return { x: this.pan.x + wx * this.zoom, y: this.pan.y + wy * this.zoom };
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
      // toolbar
      h.querySelector(".nc-toolbar").addEventListener("click", e => {
        const a = e.target.closest(".nc-tb"); if (!a) return;
        if (a.dataset.a === "zin") this._zoomBy(1.15);
        if (a.dataset.a === "zout") this._zoomBy(1 / 1.15);
        if (a.dataset.a === "fit") this.fit();
      });
      h.addEventListener("wheel", e => {
        e.preventDefault();
        const r = h.getBoundingClientRect();
        this._zoomAt(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - r.left, e.clientY - r.top);
      }, { passive: false });

      h.addEventListener("mousedown", e => {
        const port = e.target.closest(".nc-port-out");
        if (port) { this._startLink(port, e); e.preventDefault(); return; }
        const drag = e.target.closest("[data-drag]");
        if (drag) { this._startNodeDrag(drag.dataset.drag, e); e.preventDefault(); return; }
        const node = e.target.closest(".nc-node");
        if (node) { this.select(node.dataset.node); return; }
        // empty canvas → pan (and deselect)
        this.select(null);
        this._panning = { x: e.clientX - this.pan.x, y: e.clientY - this.pan.y };
        h.classList.add("nc-panning");
      });
      window.addEventListener("mousemove", e => this._onMove(e));
      window.addEventListener("mouseup", e => this._onUp(e));
      // Delete key removes the selected node (unless typing in a field).
      this._onKey = (e) => {
        if (!this.host.isConnected) { window.removeEventListener("keydown", this._onKey); return; }
        if ((e.key === "Delete" || e.key === "Backspace") && this.sel) {
          const t = document.activeElement;
          if (t && /INPUT|TEXTAREA|SELECT/.test(t.tagName)) return;
          e.preventDefault(); this.removeNode(this.sel);
        }
      };
      window.addEventListener("keydown", this._onKey);
      // Click an edge to remove it.
      this.$svg.addEventListener("click", (e) => {
        const p = e.target.closest("path.nc-hit");
        if (p) { const i = +p.dataset.i; if (i >= 0) { this.removeEdge(i); if (this.o.onNodeMove) this.o.onNodeMove(null); } }
      });
    }

    _startNodeDrag(id, e) {
      const n = this.nodes.get(id); if (!n) return;
      this._drag = { id, sx: e.clientX, sy: e.clientY, ox: n.x, oy: n.y };
    }
    _startLink(portEl, e) {
      this._link = { from: [portEl.dataset.node, portEl.dataset.port],
                     x: e.clientX - this.host.getBoundingClientRect().left,
                     y: e.clientY - this.host.getBoundingClientRect().top };
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
        const el = this.$world.querySelector(`[data-node="${n.id}"]`);
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
          if (to[0] !== this._link.from[0]) {
            this.addEdge(this._link.from, to);
            if (this.o.onConnect) this.o.onConnect(this._link.from, to);
          }
        }
        this._link = null; this._renderEdges();
      }
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
      const minx = Math.min(...ns.map(n => n.x)) - 40, miny = Math.min(...ns.map(n => n.y)) - 40;
      const maxx = Math.max(...ns.map(n => n.x + (n.w || 220))) + 40;
      const maxy = Math.max(...ns.map(n => n.y + 200)) + 40;
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

  window.NodeCanvas = NodeCanvas;

  /* engine styles (injected once) */
  if (!document.getElementById("nc-style")) {
    const s = document.createElement("style");
    s.id = "nc-style";
    s.textContent = `
      .nc-host{position:relative;width:100%;height:100%;overflow:hidden;background:#0b0d10;border:1px solid var(--seam);border-radius:12px;--nc-edge:var(--ember)}
      .nc-grid{position:absolute;inset:0;background-image:radial-gradient(var(--grid-dot,rgba(255,255,255,.06)) 1px,transparent 1px);background-size:24px 24px}
      .nc-edges{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
      .nc-edge{fill:none;stroke:var(--nc-edge);stroke-width:2;opacity:.75}
      .nc-edge.temp{stroke-dasharray:5 4;opacity:.9}
      .nc-hit{fill:none;stroke:transparent;stroke-width:16;pointer-events:stroke;cursor:pointer}
      .nc-hit:hover{stroke:var(--nc-edge);opacity:.18}
      .nc-world{position:absolute;top:0;left:0;transform-origin:0 0}
      .nc-node{position:absolute;background:var(--plate,#14161c);border:1px solid var(--seam,#262a33);border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.35);user-select:none;transition:border-color .12s,box-shadow .12s}
      .nc-node.sel{border-color:var(--nc-a);box-shadow:0 0 0 1px var(--nc-a),0 10px 30px rgba(0,0,0,.5)}
      .nc-head{display:flex;align-items:center;gap:8px;padding:9px 11px;border-bottom:1px solid var(--seam);cursor:grab;border-radius:12px 12px 0 0}
      .nc-head:active{cursor:grabbing}
      .nc-ico{color:var(--nc-a);font-size:13px;width:16px;text-align:center}
      .nc-title{font-size:12.5px;font-weight:600;color:var(--bone);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
      .nc-badge{font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--nc-a);border:1px solid var(--nc-a);border-radius:20px;padding:1px 7px;opacity:.9}
      .nc-body{padding:11px;font-size:12px;color:var(--ash,#9aa3b2);line-height:1.45;min-height:20px}
      .nc-ports{position:absolute;top:38px;display:flex;flex-direction:column;gap:14px}
      .nc-in{left:-7px}.nc-out{right:-7px;align-items:flex-end}
      .nc-port{width:14px;height:14px;display:flex;align-items:center;justify-content:center;cursor:crosshair}
      .nc-port span{width:11px;height:11px;border-radius:50%;background:var(--plate2,#1b1f27);border:2px solid var(--nc-a);transition:transform .1s}
      .nc-port:hover span{transform:scale(1.3);background:var(--nc-a)}
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
