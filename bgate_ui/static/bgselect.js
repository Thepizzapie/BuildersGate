/* BGSelect — a styled, filterable listbox that replaces the native dropdown.
 *
 * A native <select> renders its popup through the OS, not the page: none of the
 * design system reaches it. On Windows that meant an OS-blue highlight over a
 * flat run of unstyled file paths, in an app that is otherwise ember-on-black —
 * and no way to filter a list that is routinely 20+ Godot resource paths long.
 *
 * This is PROGRESSIVE ENHANCEMENT, not a replacement. The original <select>
 * stays in the DOM and stays the source of truth:
 *   · `sel.value` reads and writes work unchanged
 *   · `onchange=` attributes and addEventListener("change") still fire
 *   · anything doing querySelector("#some-select").value keeps working
 * There are 45 selects across 17 files; none of their call sites change.
 *
 * Re-entrancy matters here: the seat shell re-renders every 3s, so enhance()
 * runs constantly over the same DOM. Everything below is idempotent.
 */
(function () {
  "use strict";

  var OPEN = null;                  // the one open popup, if any
  var FILTER_THRESHOLD = 8;         // show the filter box past this many options
  var uid = 0;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function optionsOf(sel) {
    return Array.prototype.map.call(sel.options, function (o, i) {
      return { i: i, label: o.textContent, value: o.value, disabled: o.disabled,
               group: o.parentElement && o.parentElement.tagName === "OPTGROUP"
                 ? o.parentElement.label : null };
    });
  }

  function labelFor(sel) {
    var o = sel.options[sel.selectedIndex];
    return o ? o.textContent : "";
  }

  /* ---- the button that stands in for the collapsed select ---------------- */

  function enhance(sel) {
    if (!sel || sel.dataset.bgs === "1" || sel.multiple || sel.size > 1) return;
    if (sel.closest("[data-bgs-skip]")) return;
    sel.dataset.bgs = "1";

    var id = "bgs" + (++uid);
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "bgs-btn";
    btn.id = id;
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    // Carry the select's accessible name over to the control that now has focus.
    var al = sel.getAttribute("aria-label");
    if (al) btn.setAttribute("aria-label", al);
    else if (sel.id) {
      var lab = document.querySelector('label[for="' + CSS.escape(sel.id) + '"]');
      if (lab) btn.setAttribute("aria-label", lab.textContent.trim());
    }
    btn.innerHTML = '<span class="bgs-val"></span><span class="bgs-caret" aria-hidden="true">▾</span>';

    // The select keeps every class it had, so page CSS that sized or coloured
    // it still applies — to the button, which now wears them.
    Array.prototype.forEach.call(sel.classList, function (c) { btn.classList.add(c); });
    if (sel.style.cssText) btn.style.cssText = sel.style.cssText;

    sel.classList.add("bgs-native");
    sel.setAttribute("tabindex", "-1");
    sel.setAttribute("aria-hidden", "true");
    sel.parentNode.insertBefore(btn, sel.nextSibling);
    sel._bgsBtn = btn;
    btn._bgsSel = sel;

    syncLabel(sel);

    btn.addEventListener("click", function (e) {
      e.preventDefault(); e.stopPropagation();
      if (OPEN && OPEN.sel === sel) close(); else open(sel);
    });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
        e.preventDefault(); open(sel);
      }
    });
    // Programmatic `sel.value = x` fires nothing, but most callers follow it
    // with a change event, and every rebuild replaces the options outright.
    sel.addEventListener("change", function () { syncLabel(sel); });
    new MutationObserver(function () { syncLabel(sel); })
      .observe(sel, { childList: true, subtree: true });

    if (sel.disabled) btn.disabled = true;
  }

  function syncLabel(sel) {
    var btn = sel._bgsBtn;
    if (!btn) return;
    var v = btn.querySelector(".bgs-val");
    var text = labelFor(sel);
    if (v) {
      v.textContent = text;
      v.classList.toggle("bgs-placeholder", !sel.value);
    }
    btn.disabled = sel.disabled;
    btn.title = text;
  }

  /* ---- the popup --------------------------------------------------------- */

  function open(sel) {
    close();
    var btn = sel._bgsBtn;
    if (!btn || btn.disabled) return;

    var opts = optionsOf(sel);
    var pop = document.createElement("div");
    pop.className = "bgs-pop";
    pop.setAttribute("role", "listbox");

    var filterable = opts.length > FILTER_THRESHOLD;
    pop.innerHTML =
      (filterable
        ? '<div class="bgs-filterwrap"><input class="bgs-filter" type="search" ' +
          'placeholder="Filter…" aria-label="Filter options" autocomplete="off" spellcheck="false"></div>'
        : "") +
      '<div class="bgs-list"></div>' +
      '<div class="bgs-none" hidden>no match</div>';

    var list = pop.querySelector(".bgs-list");
    var lastGroup = null;
    opts.forEach(function (o) {
      if (o.group && o.group !== lastGroup) {
        lastGroup = o.group;
        var g = document.createElement("div");
        g.className = "bgs-group"; g.textContent = o.group;
        list.appendChild(g);
      }
      var row = document.createElement("div");
      row.className = "bgs-opt" + (o.i === sel.selectedIndex ? " sel" : "") +
                      (o.disabled ? " dis" : "");
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", o.i === sel.selectedIndex ? "true" : "false");
      row.dataset.i = o.i;
      row.textContent = o.label;
      row.title = o.label;
      list.appendChild(row);
    });

    document.body.appendChild(pop);
    place(pop, btn);
    btn.setAttribute("aria-expanded", "true");

    OPEN = { sel: sel, btn: btn, pop: pop };

    var rows = function () {
      return Array.prototype.filter.call(pop.querySelectorAll(".bgs-opt"),
        function (r) { return !r.hidden && !r.classList.contains("dis"); });
    };
    var cur = pop.querySelector(".bgs-opt.sel");
    if (cur) cur.scrollIntoView({ block: "nearest" });

    function commit(row) {
      if (!row || row.classList.contains("dis")) return;
      sel.selectedIndex = Number(row.dataset.i);
      syncLabel(sel);
      // The whole point of keeping the native element: existing onchange=""
      // handlers and listeners fire exactly as they always did.
      sel.dispatchEvent(new Event("input", { bubbles: true }));
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      close();
      btn.focus();
    }

    pop.addEventListener("mousedown", function (e) { e.preventDefault(); });
    pop.addEventListener("click", function (e) {
      var row = e.target.closest(".bgs-opt");
      if (row) commit(row);
    });

    var filter = pop.querySelector(".bgs-filter");
    if (filter) {
      filter.addEventListener("input", function () {
        var q = filter.value.trim().toLowerCase();
        var shown = 0;
        Array.prototype.forEach.call(pop.querySelectorAll(".bgs-opt"), function (r) {
          var hit = !q || r.textContent.toLowerCase().indexOf(q) >= 0;
          r.hidden = !hit; if (hit) shown++;
        });
        Array.prototype.forEach.call(pop.querySelectorAll(".bgs-group"), function (g) {
          g.hidden = !!q;
        });
        pop.querySelector(".bgs-none").hidden = shown > 0;
        mark(rows()[0] || null);
      });
      setTimeout(function () { filter.focus(); }, 0);
    }

    function mark(row) {
      Array.prototype.forEach.call(pop.querySelectorAll(".bgs-opt.on"), function (r) {
        r.classList.remove("on");
      });
      if (row) { row.classList.add("on"); row.scrollIntoView({ block: "nearest" }); }
    }

    pop._onKey = function (e) {
      var rs = rows();
      var at = rs.indexOf(pop.querySelector(".bgs-opt.on") || pop.querySelector(".bgs-opt.sel"));
      if (e.key === "ArrowDown") { e.preventDefault(); mark(rs[Math.min(rs.length - 1, at + 1)] || rs[0]); }
      else if (e.key === "ArrowUp") { e.preventDefault(); mark(rs[Math.max(0, at - 1)] || rs[0]); }
      else if (e.key === "Home") { e.preventDefault(); mark(rs[0]); }
      else if (e.key === "End") { e.preventDefault(); mark(rs[rs.length - 1]); }
      else if (e.key === "Enter") { e.preventDefault(); commit(pop.querySelector(".bgs-opt.on") || rs[at]); }
      else if (e.key === "Escape" || e.key === "Tab") { close(); btn.focus(); }
    };
    document.addEventListener("keydown", pop._onKey, true);
    setTimeout(function () { document.addEventListener("mousedown", outside, true); }, 0);
  }

  function outside(e) {
    if (!OPEN) return;
    if (OPEN.pop.contains(e.target) || OPEN.btn.contains(e.target)) return;
    close();
  }

  function close() {
    if (!OPEN) return;
    var o = OPEN; OPEN = null;
    document.removeEventListener("keydown", o.pop._onKey, true);
    document.removeEventListener("mousedown", outside, true);
    o.btn.setAttribute("aria-expanded", "false");
    if (o.pop.parentNode) o.pop.parentNode.removeChild(o.pop);
  }

  // position:fixed so the popup escapes the overflow:hidden / max-height
  // scrollers that most of these selects live inside.
  function place(pop, btn) {
    var r = btn.getBoundingClientRect();
    var vh = window.innerHeight;
    pop.style.minWidth = Math.max(r.width, 180) + "px";
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - r.width - 8)) + "px";
    var below = vh - r.bottom - 12;
    var above = r.top - 12;
    if (below >= 200 || below >= above) {
      pop.style.top = (r.bottom + 4) + "px";
      pop.style.maxHeight = Math.max(140, below) + "px";
    } else {
      pop.style.maxHeight = Math.max(140, above) + "px";
      pop.style.bottom = (vh - r.top + 4) + "px";
    }
  }

  window.addEventListener("resize", close);
  window.addEventListener("scroll", close, true);

  /* ---- keep up with a DOM that re-renders every few seconds -------------- */

  function scan(root) {
    var host = root && root.querySelectorAll ? root : document;
    Array.prototype.forEach.call(host.querySelectorAll("select:not([data-bgs])"), enhance);
  }

  function boot() {
    scan(document);
    new MutationObserver(function (muts) {
      var hit = false;
      muts.forEach(function (m) {
        Array.prototype.forEach.call(m.addedNodes, function (n) {
          if (n.nodeType !== 1) return;
          if (n.tagName === "SELECT" || n.querySelector && n.querySelector("select")) hit = true;
        });
      });
      if (hit) scan(document);
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  window.BGSelect = { enhance: enhance, scan: scan, close: close, sync: syncLabel };
})();
