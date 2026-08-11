/* wf_steps_tools.js — the palette, built from the server's own tool table.
 *
 * WHAT THIS FIXES
 * ---------------
 * The Studio canvas advertised about forty-five cards across 3D, world, models
 * and agents. Behind almost all of them there was no executor: pressing run on
 * a "music" card either queued a Claude session to go and do it by hand, or did
 * nothing at all. Two node types in the entire palette actually called
 * anything.
 *
 * bgate_core/wfnodes.py turned every MCP tool this product owns into a runnable
 * node type. This file draws them — and it draws them from
 * GET /api/workflows/nodes, which is the SAME table the executor calls with.
 *
 * WHY THE CARDS ARE NOT WRITTEN OUT BY HAND
 * -----------------------------------------
 * A hand-written card holds a copy of the tool's argument names. The first time
 * one of those signatures changes, the copy is wrong, and the way you find out
 * is a 422 in the middle of a paid run — discovered by the user, after the
 * money. One table, two consumers, no drift: a field on a card exists because
 * the tool takes it.
 *
 * WHAT A CARD PROMISES
 * --------------------
 *   * PAID nodes are badged as paid, on the card, before you press anything.
 *     Nothing on this canvas fires a paid tool except a human pressing run on
 *     that node.
 *   * A node that WRITES INTO THE GAME (Godot, Blender, scene surgery) says so,
 *     and the engine makes those take the line one at a time, because two of
 *     them at once is last-write-wins.
 *   * ▶ runs only that node. Nothing else in the graph moves.
 *
 * Frontend only, vanilla JS, IIFE, never throws.
 */
(function () {
  "use strict";
  if (!window.WF || typeof WF.registerStep !== "function") return;
  if (!window.NodeCanvas || !NodeCanvas.w) return;

  var w = NodeCanvas.w;
  var esc = NodeCanvas.esc;
  var toast = function (m, bad) { return window.BGWS ? BGWS.toast(m, bad) : console.log(m); };

  /* Categories this file contributes that wf.js's own list does not know.
     wf.js owns CATS as a closure constant, so a step in an unlisted category
     would register fine and never appear in the palette — present in the
     registry, invisible on screen, which is the worst of both. Rather than
     reaching into that file, the palette render is wrapped and the missing
     sections are appended after it. */
  var EXTRA_CATS = [
    { id: "engine", label: "Engine · Godot" },
    { id: "level", label: "Levels" },
    { id: "audio", label: "Music · Voice" },
    { id: "video", label: "Storyboard · Cinematic" },
  ];

  /* ---------------------------------------------------------------------- */
  /* widgets                                                                 */
  /* ---------------------------------------------------------------------- */

  function widget(n, a) {
    var label = a.label || a.name;
    switch (a.widget) {
      case "hidden": return "";
      case "area":
        return w.text(n, a.field, { label: label, rows: 3, placeholder: a.help || "" });
      case "number":
        return w.number(n, a.field, { label: label, value: a.default == null ? 0 : a.default });
      case "toggle":
        return w.toggle(n, a.field, { label: label, value: !!a.default });
      case "select":
        return w.select(n, a.field, {
          label: label, value: a.default == null ? "" : String(a.default),
          options: (a.options || []).map(function (o) { return { value: o, label: o }; }),
        });
      default:
        return w.text(n, a.field, { label: label, placeholder: a.help || "" });
    }
  }

  /* Which fields go on the CARD and which stay in the inspector.
     A card with twenty rows is not a node, it is a form nobody can see past.
     Required fields and anything wired are on the card; the long tail of
     timeouts and overrides lives in the inspector, where it is still editable
     and does not cost a single pixel of canvas. */
  function cardArgs(def) {
    var shown = (def.args || []).filter(function (a) {
      return a.widget !== "hidden"
        && (a.required || a.source === "text" || a.source === "path"
            || a.widget === "toggle" || a.widget === "select");
    });
    // Always show at least something to type into, or the card is a label.
    if (!shown.length) shown = (def.args || []).slice(0, 3);
    return shown.slice(0, 7);
  }

  /* What the node PRODUCED, from the run's own record of this node — never a
     name-matched lookup, because two sibling cards making the same thing would
     otherwise show each other's results. */
  function producedRow(n, def) {
    var made = WF.nodeArtifacts(n.id);
    var out = WF.nodeOutput(n.id) || {};
    var html = "";
    if (made.length) {
      html += '<div class="wf-cands">' + made.slice(0, 4).map(function (a) {
        var url = WF.artUrl(a.path);
        return '<span class="wf-cand">' + (url
          ? '<img src="' + esc(url) + '" loading="lazy" onerror="this.style.visibility=\'hidden\'">'
          : "") + '<span class="wf-cand-n">#' + esc(a.artifact_id) + "</span></span>";
      }).join("") + "</div>";
    }
    var files = (out.paths || []).length;
    if (!made.length && files) html += w.note(files + " file" + (files === 1 ? "" : "s") + " produced");
    // A data tool's answer IS its result. Showing a slice of it is the
    // difference between a green node and a node you can believe.
    if (!made.length && !files && out.text) {
      html += '<div class="wf-tool-out">' + esc(String(out.text).slice(0, 220)) + "</div>";
    }
    if (!html) html = w.note("nothing run yet");
    return html;
  }

  function runRow(n) {
    // The canvas node's own optimistic status FIRST: WF.runNode sets it the
    // instant you click, while nodeStatus() reads the last poll. Trusting only
    // the poll left the button live during the gap and a second click bounced
    // off the engine with "already running".
    var st = n.status || WF.nodeStatus(n.id);
    var busy = st === "running" || st === "queued";
    return '<div class="wf-act"><button class="nc-w wf-run1" data-wact="run" data-wval="'
      + esc(n.id) + '"' + (busy ? " disabled" : "")
      + ' title="run only this node">' + (busy ? esc(st) + "…" : "▶ run this node")
      + "</button></div>";
  }

  function badges(def) {
    var out = "";
    if (def.paid) {
      out += '<div class="wf-warn">PAID - this node calls a provider and the bill is real. '
        + "Nothing fires it but you pressing run.</div>";
    }
    if (def.exclusive) {
      out += w.note("writes into the game project - runs one at a time");
    }
    return out;
  }

  /* ---------------------------------------------------------------------- */
  /* registration                                                            */
  /* ---------------------------------------------------------------------- */

  function defaultsOf(def) {
    var d = {};
    (def.args || []).forEach(function (a) {
      d[a.field || a.name] = a.default == null ? "" : a.default;
    });
    return d;
  }

  function registerTool(def) {
    var shown = cardArgs(def);
    WF.registerStep({
      type: def.type,
      category: def.category,
      label: def.label,
      glyph: def.glyph || "⚙",
      accent: def.accent || "var(--ember)",
      /* Declared 'tool' because that is what it is. The server derives the kind
         from the type regardless (workflows.kind_for reads the same registry),
         so this cannot be used to sneak a game-writing node past the
         single-file rule. */
      kind: "tool",
      /* Carried onto the step so the LIBRARY can count a template's paid steps
         without re-fetching the table. A template card that opens into three
         provider calls has to say so before it is opened — the owner has been
         billed once tonight by a card that did not. */
      paid: !!def.paid,
      defaults: defaultsOf(def),
      ports: function () {
        return {
          in: (def.ports_in || ["in"]).map(function (p) { return { id: p, label: p }; }),
          out: (def.ports_out || ["out"]).map(function (p) { return { id: p, label: def.produces || p }; }),
        };
      },
      badge: function () { return def.paid ? "paid" : ""; },
      body: function (n) {
        return producedRow(n, def)
          + shown.map(function (a) { return widget(n, a); }).join("")
          + badges(def)
          + runRow(n);
      },
      config: function (n) {
        var html = '<div class="wf-insp-p">' + esc(def.summary || "") + "</div>"
          + '<div class="wf-insp-p">Calls <code>' + esc(def.tool) + "</code> directly - "
          + "no seat, no queue item, no Claude session between the card and the tool.</div>";
        if (def.paid) {
          html += '<div class="wf-warn">This node spends money when it runs. It is reachable, '
            + "and it only ever fires because a person pressed run on it.</div>";
        }
        // EVERY argument, including the ones the card hides. The inspector is
        // where the long tail lives; a field that exists only in the payload is
        // a field the user cannot reach.
        html += '<div class="wf-insp-p"><b>Arguments</b></div>';
        (def.args || []).forEach(function (a) {
          html += widget(n, a);
          if (a.source === "text") html += w.note("a wired-in text output wins over this field; use {input} to compose");
          if (a.source === "path") html += w.note("takes the upstream node's file when this is blank");
          if (a.source === "root") html += w.note("blank = this Builders Gate project");
          if (a.help) html += w.note(a.help);
        });
        return html;
      },
    });
  }

  function registerFlow(def) {
    WF.registerStep({
      type: def.type, category: def.category || "control", label: def.label,
      glyph: def.glyph || "⌇", accent: def.accent || "var(--spark)",
      // Glue is passive: it calls nothing and costs nothing, so it finishes
      // inline on the same tick that starts it.
      kind: "passive",
      defaults: defaultsOf(def),
      ports: function () {
        return { in: [{ id: "in", label: "in" }], out: [{ id: "out", label: "out" }] };
      },
      body: function (n) {
        var out = WF.nodeOutput(n.id) || {};
        var head = out.text
          ? '<div class="wf-tool-out">' + esc(String(out.text).slice(0, 200)) + "</div>"
          : "";
        return head + (def.args || []).map(function (a) { return widget(n, a); }).join("")
          + runRow(n);
      },
      config: function (n) {
        return '<div class="wf-insp-p">' + esc(def.summary || "") + "</div>"
          + (def.args || []).map(function (a) { return widget(n, a); }).join("");
      },
      onAction: function (n, action) { if (action === "run") WF.runNode(n.id); },
    });
  }

  /* One action path for every card this file registers. */
  function attachRun(def) {
    var step = WF.steps[def.type];
    if (step && !step.onAction) {
      step.onAction = function (n, action) { if (action === "run") WF.runNode(n.id); };
    }
  }

  /* ---------------------------------------------------------------------- */
  /* palette: append the categories wf.js does not know about                */
  /* ---------------------------------------------------------------------- */

  var origPalette = WF._renderPalette;
  WF._renderPalette = function () {
    try { origPalette.call(this); } catch (e) { /* never break the builder */ }
    var pal = document.getElementById("wf-palette");
    if (!pal) return;
    var byCat = {};
    Object.keys(this.steps).forEach(function (t) {
      var s = this.steps[t];
      (byCat[s.category] = byCat[s.category] || []).push(s);
    }, this);
    var html = "";
    EXTRA_CATS.forEach(function (c) {
      var list = byCat[c.id] || [];
      if (!list.length) return;
      html += '<div class="wf-pal-cat">' + esc(c.label) + "</div>"
        + list.map(function (s) {
          return '<button class="wf-pi" style="--a:' + (s.accent || "var(--ember)")
            + '" onclick="WF.addStep(\'' + esc(s.type) + '\')"><span class="g">'
            + esc(s.glyph || "◇") + "</span> " + esc(s.label) + "</button>";
        }).join("");
    });
    if (html) pal.insertAdjacentHTML("beforeend", html);
  };

  /* ---------------------------------------------------------------------- */
  /* the run gate                                                            */
  /* ---------------------------------------------------------------------- */
  /* wf.js decides "is there anything runnable in this graph" with
   *   plan.nodes.some(n => n.seat || n.kind === "consistency" || n.kind === "generate")
   * which predates the tool kind entirely, so a graph made only of tool nodes
   * was refused with "no runnable step" before it ever reached the server.
   *
   * Rather than copying that method (a copy drifts), the step registry is
   * presented in the OLDER vocabulary for the duration of the call: tool steps
   * report kind "generate" while the plan is compiled, and are put back
   * immediately. This is safe and not a lie the engine can act on — wf.js's own
   * step contract says the server re-derives the kind from the type, and
   * workflows.kind_for does exactly that from the same registry this palette was
   * built from. A tool node cannot become a generate node by saying so.
   */
  function withLegacyKind(name) {
    var orig = WF[name];
    if (typeof orig !== "function") return;
    WF[name] = function () {
      var self = this, args = arguments, swapped = [];
      Object.keys(self.steps).forEach(function (t) {
        if (self.steps[t] && self.steps[t].kind === "tool") {
          self.steps[t].kind = "generate";
          swapped.push(t);
        }
      });
      var restore = function () {
        swapped.forEach(function (t) { if (self.steps[t]) self.steps[t].kind = "tool"; });
      };
      var out;
      try { out = orig.apply(self, args); }
      catch (e) { restore(); throw e; }
      if (out && typeof out.then === "function") return out.then(
        function (v) { restore(); return v; },
        function (e) { restore(); throw e; });
      restore();
      return out;
    };
  }
  withLegacyKind("_ensureRun");
  withLegacyKind("run");

  /* ---------------------------------------------------------------------- */
  /* load the table                                                          */
  /* ---------------------------------------------------------------------- */

  var loaded = false;
  function load() {
    if (loaded) return;
    loaded = true;
    fetch("/api/workflows/nodes").then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (res) {
      var d = res && res.ok && res.data;
      if (!d) {
        // Say it once, in the palette's own terms. A silent failure here reads
        // exactly like "this build has no tool nodes", which is the one thing
        // it must not be confused with.
        toast("the tool palette could not be read from this server (GET /api/workflows/nodes) - "
          + "the model and control cards still work", true);
        return;
      }
      (d.tools || []).forEach(function (def) { registerTool(def); attachRun(def); });
      (d.flow || []).forEach(function (def) { registerFlow(def); });
      // The builder may already be open: re-render the palette and the cards so
      // a saved graph's tool nodes stop rendering as the unknown-type fallback.
      try { WF._renderPalette(); } catch (e) {}
      try { WF._repaintNodes(); } catch (e) {}
      // The LIBRARY may be the thing on screen, and until this table arrived it
      // had no way to know which of a template's steps spend money — every card
      // would have read "free". Repaint it now that the answer exists.
      try { if (document.getElementById("wf-lib-body")) WF._renderLibrary(); } catch (e) {}
    }).catch(function () {
      toast("the tool palette request failed - the dashboard may be restarting", true);
    });
  }
  load();

  if (!document.getElementById("wf-tools-style")) {
    var s = document.createElement("style");
    s.id = "wf-tools-style";
    s.textContent = ".wf-tool-out{font-family:var(--mono);font-size:10px;line-height:1.45;"
      + "color:var(--ash);background:var(--void);border:1px solid var(--seam);"
      + "border-radius:6px;padding:5px 7px;margin:4px 0;max-height:78px;overflow:auto;"
      + "white-space:pre-wrap;word-break:break-word}";
    document.head.appendChild(s);
  }
})();
