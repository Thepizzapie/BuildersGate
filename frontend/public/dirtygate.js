/* dirtygate.js — turn the dirty-tree refusal into a question with a button.
 *
 * bgate_ui/dispatch.py refuses to spawn an agent when the project's git tree
 * has uncommitted changes: it records base_commit before the run, and a diff
 * taken over a dirty tree cannot separate the agent's edits from the human's.
 * The refusal is correct. Its presentation was not — it arrived as a red toast
 * reading "commit or stash first, or dispatch with allow_dirty", which names a
 * parameter the browser had no way to send, in a product whose users are not
 * expected to know what a git tree is.
 *
 * WHY THIS PATCHES fetch RATHER THAN THE CALL SITES.
 * Dispatch is posted from at least eight places — flows.js and seven seat
 * modules — and each one rolls its own fetch wrapper and its own
 * error toast. There is no shared client to hook. Patching each would mean
 * nine near-identical edits, missing one silently reintroduces the dead end,
 * and the tenth call site somebody adds next month gets nothing. One narrow
 * interceptor covers all of them, including ones that do not exist yet.
 *
 * The patch is deliberately tiny in scope: POST, one URL shape, one error
 * code. Everything else is returned untouched, by identity — not rebuilt.
 *
 * Load order does not matter (it patches whatever fetch is current at parse
 * time and the callers all call window.fetch late), but it is registered next
 * to ask.js because it depends on askConfirm.
 */
(function () {
  "use strict";

  var DISPATCH = /\/api\/queue\/\d+\/dispatch$/;

  /* A batch dispatch loops over items one POST at a time (director.js
     dispatchAll / dispatchSelected). Asking once per item would be a dialog
     per queued task, so a yes is remembered briefly. Short and time-boxed on
     purpose: this is a safety gate, and "yes" should not quietly mean "yes for
     the rest of the session". */
  var REMEMBER_MS = 60000;
  var okUntil = 0;

  /* One dialog at a time. Two dispatches racing would otherwise stack two
     scrims and leave the second orphaned when the first resolves. */
  var pending = null;

  function isDirtyRefusal(body) {
    return !!body && body.ok === false && body.code === "dirty_tree";
  }

  function summarise(detail) {
    var paths = (detail && detail.paths) || [];
    if (!paths.length) return "";
    var shown = paths.slice(0, 8).map(function (p) { return "· " + p; }).join("\n");
    return paths.length > 8
      ? shown + "\n· …and " + (paths.length - 8) + " more"
      : shown;
  }

  function ask(body) {
    if (Date.now() < okUntil) return Promise.resolve(true);
    if (pending) return pending;
    if (typeof window.askConfirm !== "function") return Promise.resolve(false);

    var detail = body.detail || {};
    var n = ((detail.paths || []).length) || 0;
    pending = window.askConfirm({
      title: "Uncommitted changes in your project",
      body:
        "Your game has " + (n ? n + " change" + (n === 1 ? "" : "s") : "changes") +
        " that are not committed to git.\n\n" +
        "Builders Gate records where the agent started so you can review, " +
        "revert, or ship exactly what it wrote. Starting now mixes your own " +
        "unsaved work into that record, so the two become hard to tell apart " +
        "afterwards.\n\n" +
        "Committing first is the safe move. Running anyway is fine if you know " +
        "what is in there.\n\n" +
        summarise(detail),
      ok: "Run anyway",
      cancel: "Let me commit first",
    }).then(function (yes) {
      pending = null;
      if (yes) okUntil = Date.now() + REMEMBER_MS;
      return yes;
    }, function () { pending = null; return false; });
    return pending;
  }

  var native = window.fetch.bind(window);

  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var method = ((init && init.method) ||
                  (input && input.method) || "GET").toUpperCase();
    var p = native(input, init);
    if (method !== "POST" || !DISPATCH.test(String(url).split("?")[0])) return p;

    return p.then(function (res) {
      // clone() so the caller still gets an unread body if this is not ours.
      return res.clone().json().then(function (body) {
        if (!isDirtyRefusal(body)) return res;
        return ask(body).then(function (yes) {
          if (!yes) return res;                 // the refusal, and its toast
          var body2 = {};
          try { body2 = JSON.parse((init && init.body) || "{}") || {}; }
          catch (e) { body2 = {}; }
          body2.allow_dirty = true;
          return native(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body2),
          });
        });
      }, function () { return res; });          // not JSON — none of our business
    });
  };
})();
