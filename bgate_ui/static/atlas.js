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
    const use = {};
    const screenIds = new Set(map.screens.map(s => s.id));
    map.edges.forEach(e => {
      if (!screenIds.has(e.from) || e.via === "derived") return;
      (use[e.to] = use[e.to] || new Set()).add(e.from);
    });
    return use;
  }

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

  /* "Make a task about this asset" — the queue composer, prefilled with what
     the scan already knows about it. The Asset Library is the caller. */
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
