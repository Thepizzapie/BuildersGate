/* Draggable pane boundaries.
 *
 * WHY A MODULE AND NOT THREE HAND-ROLLED DRAG HANDLERS. Every multi-pane view
 * in this app was a fixed split chosen once, at one window size, by whoever
 * wrote the CSS. That is fine until you are actually watching a run: the
 * cockpit's graph wants the room when you are following a delegation, and the
 * transcript wants it when you are reading what the director said, and those
 * are minutes apart. A fixed ratio is wrong half the time by construction.
 *
 * IT RESIZES A CSS VARIABLE, NOT AN ELEMENT. The handle never touches the
 * panes. It writes one custom property on a container — `--ck-left-w`,
 * `--rail-w` — and the existing grid rule consumes it. That matters because
 * the panes keep their own layout rules, their own minmax() floors and their
 * own responsive collapses; a splitter that set `style.width` on a pane would
 * be fighting all three, and would win at exactly the wrong moment (a phone
 * viewport, where the layout is supposed to stack rather than split).
 *
 * The default stays in the CSS as the var's fallback, so a boundary nobody has
 * dragged is still whatever the stylesheet says — including an `fr` track,
 * which is not a length and cannot be stored. Dragging converts that boundary
 * to pixels, which is the point: you are pinning it because you want it to
 * stop moving when the window does.
 *
 *     <div class="split" data-split="cockpit" data-split-var="--ck-left-w"
 *          data-split-min="330" data-split-max="72%"></div>
 *
 * data-split       storage key (required — no key, no persistence, no handle)
 * data-split-var   the custom property to write
 * data-split-on    selector for the element that carries it (default: parent)
 * data-split-pane  selector for the pane being measured (default: the sibling
 *                  on the handle's leading side)
 * data-split-min   px number, or "N%" of the container
 * data-split-max   ditto
 * data-split-dir   "x" (default) or "y"
 * data-split-edge  "start" (default) when the pane leads the handle, "end"
 *                  when it trails — an inspector docked right grows as you
 *                  drag LEFT, and getting that backwards is the whole bug.
 *
 * Double-click or Home resets the boundary to the stylesheet's default and
 * forgets it. Arrow keys nudge, because a pointer is not the only way in and a
 * separator you cannot reach from the keyboard is a separator half the people
 * using this cannot move.
 */
(function () {
  "use strict";

  var STORE = "bgate-split-";
  var NUDGE = 16;          // one arrow press, in px — a visible step, not a pixel
  var MIN_FLOOR = 80;      // nothing may be dragged smaller than this, ever

  function readStored(key) {
    try { return parseFloat(localStorage.getItem(STORE + key)) || 0; }
    catch (e) { return 0; }
  }
  function write(key, px) {
    try { localStorage.setItem(STORE + key, String(Math.round(px))); }
    catch (e) {}
  }
  function forget(key) {
    try { localStorage.removeItem(STORE + key); } catch (e) {}
  }

  /* A bound expressed as px or as a percentage of the container. Percentages
     are what you actually want for the MAX — "the graph may not be squeezed
     below a quarter of the window" survives a resize, where "900px" does not. */
  function bound(raw, span, fallback) {
    if (raw == null || raw === "") return fallback;
    var s = String(raw).trim();
    if (s.slice(-1) === "%") {
      var pct = parseFloat(s) / 100;
      return isFinite(pct) ? span * pct : fallback;
    }
    var n = parseFloat(s);
    return isFinite(n) ? n : fallback;
  }

  function Split(handle) {
    var key = handle.getAttribute("data-split");
    if (!key) return;

    var prop = handle.getAttribute("data-split-var");
    var dir = (handle.getAttribute("data-split-dir") || "x") === "y" ? "y" : "x";
    var atEnd = handle.getAttribute("data-split-edge") === "end";
    var onSel = handle.getAttribute("data-split-on");
    var paneSel = handle.getAttribute("data-split-pane");

    var target = onSel ? document.querySelector(onSel) : handle.parentElement;
    if (!target || !prop) return;

    // The pane whose size this boundary IS. Defaults to the sibling on the
    // leading side, which is the shape every one of these has.
    var pane = paneSel
      ? document.querySelector(paneSel)
      : (atEnd ? handle.nextElementSibling : handle.previousElementSibling);

    var size = dir === "y" ? "height" : "width";
    var axis = dir === "y" ? "clientY" : "clientX";
    var drag = null;

    /* The pane's size RIGHT NOW, in px, whatever the CSS expressed it as.
       Measured rather than parsed: the default is often an fr track or a
       minmax(), and neither has a value until the browser has laid it out. */
    function current() {
      if (pane) {
        var r = pane.getBoundingClientRect();
        if (r[size]) return r[size];
      }
      var v = parseFloat(getComputedStyle(target).getPropertyValue(prop));
      return isFinite(v) ? v : 0;
    }

    function span() {
      var r = target.getBoundingClientRect();
      return r[size] || 0;
    }

    function clamp(px) {
      var s = span();
      var lo = Math.max(MIN_FLOOR, bound(handle.getAttribute("data-split-min"), s, 160));
      var hi = bound(handle.getAttribute("data-split-max"), s, s - MIN_FLOOR);
      if (hi < lo) hi = lo;
      return Math.max(lo, Math.min(px, hi));
    }

    function apply(px, persist) {
      var v = clamp(px);
      target.style.setProperty(prop, v + "px");
      handle.setAttribute("aria-valuenow", String(Math.round(v)));
      if (persist) write(key, v);
      // Nothing here re-lays-out the graph on purpose — a boundary drag must
      // not move the user's view. This is only so anything that measures on
      // resize (minimaps, canvases) gets its chance.
      window.dispatchEvent(new Event("resize"));
      return v;
    }

    function reset() {
      target.style.removeProperty(prop);
      handle.removeAttribute("aria-valuenow");
      forget(key);
      window.dispatchEvent(new Event("resize"));
    }

    handle.addEventListener("pointerdown", function (ev) {
      if (ev.button !== 0) return;
      // Measure BEFORE capture: once the handle owns the pointer, a layout
      // read is still fine, but starting from a stale number is not.
      drag = { start: ev[axis], from: current() };
      // Capture is an optimisation, not the mechanism: it keeps the drag alive
      // when the pointer outruns the handle. It throws for a pointer the
      // browser does not consider active, and an exception here would abort
      // the handler with `drag` already set — a boundary that believes it is
      // being dragged and never hears the pointerup.
      try { handle.setPointerCapture(ev.pointerId); } catch (e) {}
      handle.classList.add("dragging");
      document.body.classList.add("splitting" + (dir === "y" ? "-y" : ""));
      ev.preventDefault();
    });

    handle.addEventListener("pointermove", function (ev) {
      if (!drag) return;
      var d = ev[axis] - drag.start;
      apply(drag.from + (atEnd ? -d : d), false);
    });

    function end(ev) {
      if (!drag) return;
      drag = null;
      handle.classList.remove("dragging");
      document.body.classList.remove("splitting", "splitting-y");
      try { handle.releasePointerCapture(ev.pointerId); } catch (e) {}
      write(key, current());
    }
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);

    // Back to whatever the stylesheet wanted. The gesture people already try.
    handle.addEventListener("dblclick", function (ev) { ev.preventDefault(); reset(); });

    handle.addEventListener("keydown", function (ev) {
      var k = ev.key;
      var back = dir === "y" ? "ArrowUp" : "ArrowLeft";
      var fwd = dir === "y" ? "ArrowDown" : "ArrowRight";
      if (k === "Home") { reset(); ev.preventDefault(); return; }
      if (k !== back && k !== fwd) return;
      var step = (k === back ? -NUDGE : NUDGE) * (ev.shiftKey ? 4 : 1);
      apply(current() + (atEnd ? -step : step), true);
      ev.preventDefault();
    });

    handle.setAttribute("role", "separator");
    handle.setAttribute("tabindex", "0");
    handle.setAttribute("aria-orientation", dir === "y" ? "horizontal" : "vertical");
    if (!handle.getAttribute("aria-label")) {
      handle.setAttribute("aria-label", "Resize panel - arrow keys adjust, Home resets");
    }

    // RESTORE LAST, AND ONLY IF IT STILL FITS. A width saved on a 2560px
    // monitor and restored on a laptop is not a preference, it is a pane that
    // covers the thing next to it, so the clamp runs on the way back in too.
    var saved = readStored(key);
    if (saved > 0) {
      // Deferred one frame: at parse time the container may not be laid out
      // yet (the view is hidden until setWorkspace runs), and clamping against
      // a zero-width span would pin every boundary to its floor.
      requestAnimationFrame(function () {
        if (span() > 0) apply(saved, false);
        else target.style.setProperty(prop, saved + "px");
      });
    }
  }

  function init(scope) {
    var all = (scope || document).querySelectorAll(".split[data-split]");
    for (var i = 0; i < all.length; i++) {
      if (!all[i].__split) { all[i].__split = true; Split(all[i]); }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(); });
  } else {
    init();
  }

  window.Split = { init: init };
})();
