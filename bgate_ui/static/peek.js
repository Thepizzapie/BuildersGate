/* peek.js — look at the file an agent is working on, without leaving the page.
 *
 * WHAT THIS FIXES. The live rail could NAME every file a run touched and open
 * none of them. Watching an agent said "wrote game/scenes/floor_0.tscn" and the
 * only way to find out whether that scene had anything in it was a second
 * editor window and a manual hunt for a path you had to copy out of prose. For
 * anything visual it was worse: the agent is looking at a sprite sheet, you are
 * looking at the word "sheet".
 *
 * Three views of the same path, because "what is in this file" and "what did
 * this run do to it" are different questions and the rail asked neither:
 *   FILE  the text, with line numbers, or the picture, or the audio.
 *   DIFF  what this run changed in it, against its own base commit.
 *   RAW   the bytes, in a tab, for when you want the whole thing.
 *
 * Deliberately NOT an editor. Read-only: the console watches work, it does not
 * do it, and a text box here would be a way to edit a file an agent is holding.
 */
(function () {
  "use strict";

  const IMAGE = /\.(png|jpe?g|webp|gif|svg)$/i;
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let host = null;      // the overlay element, created once
  let current = null;   // {rel, itemId, view, set, ix}

  function shell() {
    if (host) return host;
    host = document.createElement("div");
    host.className = "pk-wrap";
    host.hidden = true;
    host.innerHTML =
      `<div class="pk-card" role="dialog" aria-modal="true" aria-label="File viewer">
         <div class="pk-head">
           <button class="pk-nav" id="pk-prev" type="button" aria-label="Previous">‹</button>
           <span class="pk-count" id="pk-count"></span>
           <button class="pk-nav" id="pk-next" type="button" aria-label="Next">›</button>
           <span class="pk-name" id="pk-name"></span>
           <span class="pk-meta" id="pk-meta"></span>
           <span class="pk-sp"></span>
           <span class="pk-tabs" id="pk-tabs">
             <button class="pk-tab on" data-view="file" type="button">file</button>
             <button class="pk-tab" data-view="diff" type="button">diff</button>
           </span>
           <a class="pk-tab" id="pk-raw" target="_blank" rel="noopener">raw</a>
           <button class="pk-x" id="pk-x" type="button" aria-label="Close">×</button>
         </div>
         <div class="pk-body" id="pk-body"></div>
       </div>`;
    document.body.appendChild(host);
    host.addEventListener("click", e => { if (e.target === host) close(); });
    host.querySelector("#pk-x").onclick = close;
    host.querySelector("#pk-prev").onclick = () => step(-1);
    host.querySelector("#pk-next").onclick = () => step(1);
    host.querySelectorAll("#pk-tabs .pk-tab").forEach(b =>
      b.onclick = () => show(b.dataset.view));
    // Escape closes; arrows walk the set you opened it from. Comparing an art
    // agent's five renders means five opens and five closes without this, which
    // is the one thing you actually do in a viewer for a run that made a batch.
    document.addEventListener("keydown", e => {
      if (host.hidden) return;
      if (e.key === "Escape") { e.stopPropagation(); close(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    });
    return host;
  }

  function close() {
    if (host) host.hidden = true;
    current = null;
  }

  /* Move within the set the viewer was opened from, keeping the current view. */
  function step(by) {
    if (!current || !current.set || current.set.length < 2) return;
    const n = current.set.length;
    const ix = ((current.ix + by) % n + n) % n;
    open(current.set[ix], { itemId: current.itemId, view: current.view,
                            set: current.set, ix });
  }

  function paintNav() {
    const set = (current && current.set) || [];
    const many = set.length > 1;
    ["pk-prev", "pk-next"].forEach(id => {
      const b = document.getElementById(id);
      if (b) b.hidden = !many;
    });
    const c = document.getElementById("pk-count");
    if (c) {
      c.hidden = !many;
      c.textContent = many ? `${current.ix + 1}/${set.length}` : "";
    }
  }

  function body(html) {
    const el = document.getElementById("pk-body");
    if (el) el.innerHTML = html;
  }

  function lineRows(data) {
    const first = Number(data.first_line || 1);
    return `<pre class="pk-code"><code>${(data.lines || []).map((line, i) =>
      `<span class="pk-ln">${first + i}</span>${esc(line) || " "}`).join("\n")}</code></pre>`
      + (data.truncated
        ? `<div class="pk-note">showing ${(data.lines || []).length} of ${
             data.lines_total} lines — open raw for the rest</div>`
        : "");
  }

  /* A unified diff, coloured. No parser: the first character of a hunk line is
     the entire grammar, and a diff renderer that tries to be clever about
     rename detection is a diff renderer that hides a line. */
  function diffRows(text) {
    const rows = String(text || "").split("\n").map(line => {
      const c = line[0];
      const k = line.startsWith("+++") || line.startsWith("---") ? "meta"
        : line.startsWith("@@") ? "hunk"
        : c === "+" ? "add" : c === "-" ? "del"
        : line.startsWith("diff ") || line.startsWith("index ") ? "meta" : "ctx";
      return `<div class="pk-dl k-${k}">${esc(line) || " "}</div>`;
    });
    return `<div class="pk-diff">${rows.join("")}</div>`;
  }

  async function show(view) {
    if (!current) return;
    current.view = view;
    document.querySelectorAll("#pk-tabs .pk-tab").forEach(b =>
      b.classList.toggle("on", b.dataset.view === view));

    const { rel, itemId } = current;
    if (view === "diff") {
      if (!itemId) {
        body(`<div class="pk-note">no run selected — a diff is always "what did
              THIS run change", so open the file from an agent's rail.</div>`);
        return;
      }
      body(`<div class="pk-note">reading the diff…</div>`);
      const d = await window.readJSON(
        `/api/queue/${itemId}/diff?path=${encodeURIComponent(rel)}`, null);
      if (!d || d.__error) { body(`<div class="pk-note err">${esc((d || {}).__error || "could not read the diff")}</div>`); return; }
      if (d.available === false) { body(`<div class="pk-note">${esc(d.reason || "no diff available")}</div>`); return; }
      const file = (d.files || [])[0];
      if (!file) { body(`<div class="pk-note">this run did not change ${esc(rel)}</div>`); return; }
      if (file.binary) {
        body(`<div class="pk-note">binary — ${esc(file.status || "changed")}${
          file.bytes_before != null ? `, ${file.bytes_before} → ${file.bytes_after} bytes` : ""}</div>`
          + (IMAGE.test(rel) ? `<img class="pk-img" src="/api/preview?rel=${encodeURIComponent(rel)}" alt="">` : ""));
        return;
      }
      body(`<div class="pk-note">+${file.added || 0} −${file.removed || 0} · ${
        esc(file.status || "modified")}</div>` + diffRows(file.diff)
        + (file.truncated ? `<div class="pk-note">diff truncated</div>` : ""));
      return;
    }

    body(`<div class="pk-note">opening…</div>`);
    const d = await window.readJSON(
      `/api/peek?rel=${encodeURIComponent(rel)}${itemId ? `&item_id=${itemId}` : ""}`, null);
    if (!d || d.__error) { body(`<div class="pk-note err">${esc((d || {}).__error || "could not read it")}</div>`); return; }

    const meta = document.getElementById("pk-meta");
    if (meta) meta.textContent = [
      d.kind, d.bytes ? `${Number(d.bytes).toLocaleString()} b` : "",
      d.lines_total ? `${d.lines_total} lines` : "",
      d.worktree ? "in the run's worktree" : "",
    ].filter(Boolean).join(" · ");

    if (d.kind === "image") {
      // FIT BY DEFAULT, 1:1 ON CLICK. A 32px sprite blown to 1100px and a 2048px
      // sheet squeezed to 1100px are both the wrong picture, and for pixel art
      // the difference is the whole judgement — you cannot tell a clean edge from
      // a bilinear smear at either extreme. The toggle is on the image itself
      // because that is what people click.
      body(`<img class="pk-img" src="${esc(d.url)}" alt=""
                 title="click for actual size">`);
      const img = host.querySelector(".pk-img");
      if (img) img.onclick = () => {
        img.classList.toggle("actual");
        img.title = img.classList.contains("actual")
          ? "click to fit" : "click for actual size";
      };
    }
    else if (d.kind === "audio") body(`<audio class="pk-audio" controls src="${esc(d.url)}"></audio>`);
    else if (d.kind === "text") body(lineRows(d));
    else if (d.kind === "missing") body(`<div class="pk-note">nothing at that path — it may not have been written yet</div>`);
    else body(`<div class="pk-note">${esc(d.note || d.kind || "not readable here")}</div>`);
  }

  /* rel is project-relative; itemId scopes the diff (and finds the file inside
     an isolated run's worktree). */
  function open(rel, opts) {
    if (!rel) return;
    opts = opts || {};
    shell().hidden = false;
    const set = Array.isArray(opts.set) && opts.set.length ? opts.set : [String(rel)];
    const ix = Number.isInteger(opts.ix) && opts.ix >= 0 ? opts.ix
      : Math.max(0, set.indexOf(String(rel)));
    current = { rel: String(rel), itemId: Number(opts.itemId || 0) || 0,
                view: "file", set, ix };
    paintNav();
    const name = document.getElementById("pk-name");
    if (name) { name.textContent = String(rel); name.title = String(rel); }
    const raw = document.getElementById("pk-raw");
    if (raw) raw.href = IMAGE.test(rel)
      ? `/api/preview?rel=${encodeURIComponent(rel)}`
      : `/api/peek?rel=${encodeURIComponent(rel)}${current.itemId ? `&item_id=${current.itemId}` : ""}`;
    show(opts.view || "file");
  }

  window.Peek = { open, close };

  /* ONE delegated listener for the whole page. The rail is re-rendered from
     innerHTML on every poll, so per-element handlers would have to be re-bound
     every three seconds and any missed re-bind is a dead link. */
  document.addEventListener("click", e => {
    const hit = e.target.closest && e.target.closest("[data-peek]");
    if (!hit) return;
    e.preventDefault();
    e.stopPropagation();          // a chip inside a node must not select the node
    // The set is whatever was rendered NEXT TO the thing you clicked — the strip,
    // the tile grid, the chip row. Nothing has to declare a gallery; the
    // container the rail already built IS the gallery.
    const box = hit.closest(".cg-strip, .cg-made, .cg-shots, .cg-fchips");
    const set = box
      ? [...box.querySelectorAll("[data-peek]")].map(el => el.dataset.peek)
      : [hit.dataset.peek];
    open(hit.dataset.peek, {
      itemId: hit.dataset.peekItem, view: hit.dataset.peekView,
      set, ix: set.indexOf(hit.dataset.peek),
    });
  }, true);
})();
