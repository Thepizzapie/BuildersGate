/* Atlas — the project map. Screens wired to every asset they use, derived
 * live from /api/screenmap (scenes + scripts + SpriteFrames, no manifest).
 * Every node is a dispatch surface: click → the queue composer opens
 * prefilled with the node's identity, so "fix that sheet" is one click. */
window.Atlas = (() => {
  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const GLYPH = { screen:"⊞", sprites:"▦", texture:"▧", audio:"♪", script:"⌁",
                  "scene-res":"⬡", font:"F", shader:"◐", other:"·" };
  const KIND_ORDER = ["sprites", "texture", "audio", "script", "scene-res",
                      "font", "shader", "other"];
  const SEAT_FOR = { sprites:"art", texture:"art", font:"art", audio:"audio",
                     script:"gameplay", screen:"gameplay", "scene-res":"gameplay",
                     shader:"tech", other:"tech" };
  let map = null;

  /* The scan walks every .tscn/.gd/.tres in the project, so it is not a
   * per-poll request. One in-flight scan is shared by every caller, the result
   * is reused for TTL_MS, and the dead/missing COUNTS are cached in
   * sessionStorage so the nav badge can be right on first paint — it used to
   * appear only after someone opened Atlas, which is exactly when they no
   * longer needed telling. */
  const TTL_MS = 60000;
  const SUMMARY_KEY = "atlas-summary";
  let fetchedAt = 0, inflight = null;

  function load(force){
    if (!force && map && Date.now() - fetchedAt < TTL_MS) return Promise.resolve(map);
    if (inflight) return inflight;
    // `force` has to reach the SERVER's cache too, not just skip this one.
    // The scan is memoised for 90s behind /api/screenmap, so a "reread" that
    // asked for the same URL got the same stale graph handed straight back and
    // the button looked broken — which is the whole thing it exists to rule out.
    inflight = fetch(force ? "/api/screenmap?fresh=1" : "/api/screenmap")
      .then(r => r.json())
      .then(d => {
        inflight = null;
        if (d && !d.error){ map = d; fetchedAt = Date.now(); cacheSummary(d); }
        // AN ERROR MUST NOT EVICT A GOOD MAP. Atlas.map is the shared copy the
        // scene builder and the code editor read their screen list from, so
        // overwriting it with an error object left `map.screens` undefined and
        // collapsed the scene picker to its one-option fallback — the CURRENT
        // scene and nothing else. On a project whose declared boot scene is the
        // title, that reads exactly like "the builder is locked to the title
        // page", and it outlives the error: those panels only re-render on user
        // action, so the picker stays collapsed long after the scan recovers.
        // One failed poll during a `bgate serve` restart was enough to trigger
        // it. Keep the last good scan and let the caller show d.error instead —
        // activate() already paints it from its own `d`, not from here.
        else if (d && d.error && !map){ map = d; }
        return d;
      })
      .catch(e => { inflight = null; throw e; });
    return inflight;
  }

  function cacheSummary(d){
    try {
      sessionStorage.setItem(SUMMARY_KEY, JSON.stringify({
        at: Date.now(),
        dead: (d.orphans || []).length,
        missing: (d.missing || []).length,
      }));
    } catch (e) {}
  }

  function paintBadge(dead, missing){
    const badge = document.getElementById("rc-atlas");
    if (!badge) return;
    const n = (dead || 0) + (missing || 0);
    badge.textContent = n;
    badge.style.display = n ? "" : "none";
    badge.title = n ? `${dead} dead · ${missing} missing asset(s) — open Atlas to see them` : "";
  }

  /* Called at load: paints from the cached counts immediately and only scans
     when that cache is stale or absent. Never renders the panel. */
  function badge(){
    let cached = null;
    try { cached = JSON.parse(sessionStorage.getItem(SUMMARY_KEY) || "null"); } catch (e) {}
    if (cached && typeof cached.dead === "number") paintBadge(cached.dead, cached.missing);
    if (cached && Date.now() - (cached.at || 0) < TTL_MS) return Promise.resolve(cached);
    return load().then(d => {
      if (d && !d.error) paintBadge((d.orphans || []).length, (d.missing || []).length);
      return d;
    }).catch(() => null);
  }

  async function activate(){
    const grid = document.getElementById("atlas-grid");
    const fresh = map && Date.now() - fetchedAt < TTL_MS;
    if (fresh && !map.error) render();          // paint the cached map at once
    else if (grid && !map) grid.innerHTML = '<div class="empty">scanning the project…</div>';
    let d;
    try {
      d = await load(!fresh);
    } catch (e) {
      if (!fresh && grid) grid.innerHTML = `<div class="empty">scan failed: ${E(String(e))}</div>`;
      return;
    }
    if (d && d.error){
      if (grid) grid.innerHTML = `<div class="empty">${E(d.error)}</div>`;
      return;
    }
    render();
  }

  function usage(){
    // node id -> [screen ids] that reach it directly (scene or script edge).
    const use = {};
    const screenIds = new Set(map.screens.map(s => s.id));
    map.edges.forEach(e => {
      if (!screenIds.has(e.from) || e.via === "derived") return;
      (use[e.to] = use[e.to] || new Set()).add(e.from);
    });
    return use;
  }

  function thumb(n){
    return n.preview
      ? `<img class="thumb" loading="lazy" src="/api/preview?rel=${encodeURIComponent(n.preview)}" alt="">`
      : `<span class="thumb">${GLYPH[n.kind] || "·"}</span>`;
  }

  function nodeRow(n, use){
    const shared = (use[n.id] || new Set()).size;
    const sheets = map.edges
      .filter(e => e.from === n.id && (e.via === "tres" || e.via === "derived"))
      .map(e => map.nodes[e.to]).filter(x => x && x.preview).slice(0, 6);
    const badge = !n.exists ? `<span class="bg">missing</span>`
      : shared > 1 ? `<span class="bg shared">×${shared} screens</span>` : "";
    return `<div class="atlas-node${n.exists ? "" : " missing"}"
                 onclick="Atlas.deploy('${E(n.id)}')" title="${E(n.id)}">
      ${thumb(n)}<span class="lb">${E(n.label)}</span>
      ${sheets.length ? `<span class="sheets">${sheets.map(s =>
        `<img loading="lazy" src="/api/preview?rel=${encodeURIComponent(s.preview)}" title="${E(s.label)}">`).join("")}</span>` : ""}
      ${badge}</div>`;
  }

  function render(){
    const use = usage();
    const stats = document.getElementById("atlas-stats");
    const assets = Object.values(map.nodes).filter(n => n.kind !== "screen");
    stats.innerHTML = [
      `<span class="chip"><b>${map.screens.length}</b>screens</span>`,
      `<span class="chip"><b>${assets.length}</b>assets</span>`,
      `<span class="chip"><b>${map.edges.length}</b>links</span>`,
      map.orphans.length ? `<span class="chip bad" style="cursor:pointer"
          title="Assets on disk referenced by nothing — no scene, script, or SpriteFrame. Click to see the list and clean them up."
          onclick="Atlas.showDead()"><b>${map.orphans.length}</b>dead ▾</span>` : "",
      map.missing.length ? `<span class="chip bad" style="cursor:pointer"
          title="Referenced in scenes/scripts but not found on disk. Click to see them."
          onclick="Atlas.showMissing()"><b>${map.missing.length}</b>missing ▾</span>` : "",
    ].join("");
    paintBadge(map.orphans.length, map.missing.length);
    cacheSummary(map);

    document.getElementById("atlas-grid").innerHTML = map.screens.map(s => {
      const mine = map.edges.filter(e => e.from === s.id)
        .map(e => map.nodes[e.to]).filter(Boolean);
      const seen = new Set(); const uniq = [];
      mine.forEach(n => { if (!seen.has(n.id)){ seen.add(n.id); uniq.push(n); } });
      const groups = KIND_ORDER.map(k => {
        const ns = uniq.filter(n => n.kind === k);
        if (!ns.length) return "";
        return `<div class="atlas-kind">${k} · ${ns.length}</div>`
             + ns.map(n => nodeRow(n, use)).join("");
      }).join("");
      return `<div class="atlas-screen"><header>
          <span class="nm">${E(s.label)}</span>
          <span class="ct">${uniq.length} assets</span></header>
        <div class="pad-b">${groups || `<div class="atlas-empty">no references</div>`}</div></div>`;
    }).join("");

    const rails = [];
    if (map.orphans.length){
      rails.push(`<div class="atlas-rail dead" id="atlas-rail-dead"><header>dead assets · on disk, referenced by nothing
          <button class="qbtn small" onclick="Atlas.deployCleanup()">deploy cleanup task</button></header>
        <div class="pad-b">${map.orphans.map(id => nodeRow(map.nodes[id], use)).join("")}</div></div>`);
    }
    if (map.missing.length){
      rails.push(`<div class="atlas-rail miss" id="atlas-rail-miss"><header>missing · referenced but not on disk</header>
        <div class="pad-b">${map.missing.map(id => nodeRow(map.nodes[id], use)).join("")}</div></div>`);
    }
    document.getElementById("atlas-rails").innerHTML = rails.join("");
  }

  // The "N dead / N missing" header chips are click targets — jump to and flash
  // the matching rail so the number is never a dead-end (the list + cleanup
  // action live below the fold).
  function _flashTo(id){
    const el = document.getElementById(id);
    if (!el) return;
    el.scrollIntoView({behavior: "smooth", block: "start"});
    el.style.transition = "box-shadow .2s";
    el.style.boxShadow = "0 0 0 2px var(--ember)";
    setTimeout(() => { el.style.boxShadow = ""; }, 1200);
  }
  function showDead(){ _flashTo("atlas-rail-dead"); }
  function showMissing(){ _flashTo("atlas-rail-miss"); }

  function _composer(seat, title, brief){
    setWorkspace("overview");
    const seatEl = document.getElementById("q-seat");
    if (seatEl && [...seatEl.options].some(o => o.value === seat)) seatEl.value = seat;
    const t = document.getElementById("q-title"); if (t) t.value = title;
    const wrap = document.getElementById("q-brief-wrap");
    if (wrap && wrap.hidden && typeof toggleBrief === "function") toggleBrief();
    const b = document.getElementById("q-brief"); if (b) b.value = brief;
    if (t){ t.focus(); t.scrollIntoView({block: "center"}); }
  }

  function deploy(nodeId){
    const n = map && map.nodes[nodeId];
    if (!n) return;
    const use = usage();
    const where = [...(use[nodeId] || [])]
      .map(id => (map.nodes[id] || {}).label || id).join(", ") || "no screen";
    const verb = !n.exists ? "Create missing" : n.orphan ? "Remove or wire up" : "Update";
    _composer(SEAT_FOR[n.kind] || "tech",
      `${verb} ${n.label}`,
      `Atlas target: ${n.id} (${n.path})\nKind: ${n.kind} · used by: ${where}` +
      (n.orphan ? "\nStatus: DEAD — on disk but referenced by nothing." : "") +
      (!n.exists ? "\nStatus: MISSING — referenced but not on disk." : "") +
      "\n\nScope: ");
  }

  function deployCleanup(){
    const list = map.orphans.map(id => `- ${id}`).join("\n");
    _composer("tech", `Clean up ${map.orphans.length} dead assets`,
      "Atlas found these assets on disk referenced by NOTHING (no scene, script, "
      + "or SpriteFrames). Verify each is truly unused, then delete it (and its "
      + ".import) or wire it up if it was meant to ship:\n" + list);
  }

  // The badge is a startup concern, not an "open the panel" concern.
  try {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => setTimeout(badge, 1200));
    } else {
      setTimeout(badge, 1200);
    }
  } catch (e) {}

  return { activate, deploy, deployCleanup, showDead, showMissing, badge,
           /* The scan other panels need. `ensure` is the one to call from a
              module that may run before Atlas was ever opened — `map` is only
              populated once something has loaded it. */
           ensure: load, get map(){ return map; } };
})();
