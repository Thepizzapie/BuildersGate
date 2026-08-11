/* Atlas — the project scan, shared. /api/screenmap walks every .tscn/.gd/.tres
 * and returns screens, assets and the edges between them. Nothing here renders
 * it: the scene builder and the code editor read the scan to name their scenes,
 * and the Asset Library uses `deploy` to prefill the queue composer.
 *
 * THIS USED TO BE A VIEW. Atlas had a `list` mode (screens as cards, every node
 * a dispatch target, dead/missing rails underneath) and a `graph` mode next to
 * it; both were removed, and with them the nav badge that counted dead and
 * missing assets — its only destination was the list. What survives is the part
 * other panels depend on. If dead/missing assets need surfacing again, that is
 * a new home for them, not a revival of this file's rendering. */
window.Atlas = (() => {
  const SEAT_FOR = { sprites:"art", texture:"art", font:"art", audio:"audio",
                     script:"gameplay", screen:"gameplay", "scene-res":"gameplay",
                     shader:"tech", other:"tech" };
  let map = null;

  /* The scan walks every .tscn/.gd/.tres in the project, so it is not a
   * per-poll request. One in-flight scan is shared by every caller and the
   * result is reused for TTL_MS. */
  const TTL_MS = 60000;
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
        if (d && !d.error){ map = d; fetchedAt = Date.now(); }
        // AN ERROR MUST NOT EVICT A GOOD MAP. Atlas.map is the shared copy the
        // scene builder and the code editor read their screen list from, so
        // overwriting it with an error object left `map.screens` undefined and
        // collapsed the scene picker to its one-option fallback — the CURRENT
        // scene and nothing else. On a project whose declared boot scene is the
        // title, that reads exactly like "the builder is locked to the title
        // page", and it outlives the error: those panels only re-render on user
        // action, so the picker stays collapsed long after the scan recovers.
        // One failed poll during a `bgate serve` restart was enough to trigger
        // it. Keep the last good scan and let the caller show d.error instead.
        else if (d && d.error && !map){ map = d; }
        return d;
      })
      .catch(e => { inflight = null; throw e; });
    return inflight;
  }

  function usage(){
    // node id -> [screen ids] that reach it directly (scene or script edge).
    // Guarded on the map's shape rather than its existence: load() deliberately
    // keeps the scan's own error object as `map` when there is no good scan to
    // fall back to, and that object has neither screens nor edges. A caller
    // that only wanted to name a node must not throw on its way to the
    // composer — "used by: no screen" is the honest answer for a project whose
    // graph could not be read.
    const use = {};
    if (!map || !map.screens || !map.edges) return use;
    const screenIds = new Set(map.screens.map(s => s.id));
    map.edges.forEach(e => {
      if (!screenIds.has(e.from) || e.via === "derived") return;
      (use[e.to] = use[e.to] || new Set()).add(e.from);
    });
    return use;
  }

  /* THE COMPOSER IS NOT ON THE OVERVIEW. #q-seat, #q-title, #q-brief-wrap and
   * #q-go all live inside #agents-board — the Agents view's `board` mode —
   * which index.html keeps behind the `hidden` attribute until that mode is
   * selected. This used to switch to the Overview and then fill, focus and
   * scrollIntoView a form inside a hidden subtree: focus() on a hidden element
   * does nothing, scrollIntoView has no box to scroll to, and the values
   * landed on inputs nobody could see. So the Asset Library's "deploy a task"
   * read as a dead button that also threw you onto the wrong page, with the
   * prefilled task sitting invisibly two navigations away.
   * Go where the form actually is, and put the view in the mode that shows it. */
  function _composer(seat, title, brief){
    setWorkspace("agents");
    // setWorkspace restores the LAST agents mode (console, for most people), so
    // asking for the board has to happen after it, not instead of it.
    if (typeof setAgentsMode === "function") setAgentsMode("board");
    const seatEl = document.getElementById("q-seat");
    if (seatEl && [...seatEl.options].some(o => o.value === seat)) seatEl.value = seat;
    const t = document.getElementById("q-title"); if (t) t.value = title;
    const wrap = document.getElementById("q-brief-wrap");
    if (wrap && wrap.hidden && typeof toggleBrief === "function") toggleBrief();
    const b = document.getElementById("q-brief"); if (b) b.value = brief;
    if (t){ t.focus(); t.scrollIntoView({block: "center"}); }
  }

  /* A node id the scan has never heard of. NOT an error, and not rare:
   * /api/screenmap derives its graph from what scenes and scripts reference,
   * so every file the asset library lists as unused is absent from it by
   * definition — 58 of them on the project this was written against. deploy()
   * used to `return` on that, which is the one button on an unused asset's
   * drawer doing nothing at all: no composer, no toast, no console line.
   * Being outside the graph IS the fact the brief should carry, so describe
   * the id instead of refusing. `exists` is true because the only caller
   * reached this from a file it had just listed off disk. */
  const _KIND_BY_EXT = {
    png:"sprites", webp:"sprites", jpg:"sprites", jpeg:"sprites",
    ogg:"audio", wav:"audio", mp3:"audio", gd:"script", tscn:"screen",
    tres:"scene-res", ttf:"font", otf:"font", gdshader:"shader",
  };
  function _unscanned(id){
    const s = String(id || "");
    if (!s) return null;
    const name = s.split("/").pop();
    const dot = name.lastIndexOf(".");
    const ext = dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
    return { id: s, path: s, label: (dot > 0 ? name.slice(0, dot) : name) || s,
             kind: _KIND_BY_EXT[ext] || "other", exists: true, orphan: true };
  }

  /* "Make a task about this asset" — the queue composer, prefilled with what
     the scan already knows about it. The Asset Library is the caller. */
  function deploy(nodeId){
    const n = (map && map.nodes && map.nodes[nodeId]) || _unscanned(nodeId);
    if (!n) return;
    const use = usage();
    const where = [...(use[nodeId] || [])]
      .map(id => (map.nodes[id] || {}).label || id).join(", ") || "no screen";
    const verb = !n.exists ? "Create missing" : n.orphan ? "Remove or wire up" : "Update";
    _composer(SEAT_FOR[n.kind] || "tech",
      `${verb} ${n.label}`,
      `Atlas target: ${n.id} (${n.path})\nKind: ${n.kind} · used by: ${where}` +
      (n.orphan ? "\nStatus: DEAD - on disk but referenced by nothing." : "") +
      (!n.exists ? "\nStatus: MISSING - referenced but not on disk." : "") +
      "\n\nScope: ");
  }

  return { deploy,
           /* The scan other panels need. `ensure` is the one to call from a
              module that may run before anything has loaded it — `map` is only
              populated once something has. */
           ensure: load, get map(){ return map; } };
})();
