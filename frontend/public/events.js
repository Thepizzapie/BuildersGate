/* events.js — the classic decks' end of the event bus.
 *
 * ONE EventSource FOR EVERY CLASSIC MODULE. The server streams its event table
 * at /api/events/stream (SSE, `event:` = kind, `id:` = row id, so the browser
 * resumes with Last-Event-ID on its own). Before this, the queue, the ledger,
 * the seat floor and the playtest recorder each ran a timer every 2-4 seconds
 * asking whether anything had happened; now each one refetches when a row
 * says so, and keeps only a slow timer for what no event describes.
 *
 * The React shell has its own copy of this logic (frontend/src/hooks.ts,
 * useEvents) — the two do not share a socket, but two per tab is the ceiling
 * rather than one per panel.
 *
 * Usage, from any module loaded after this file:
 *   BGEvents.watch(fn, { kinds: ["item.*", "agent.*"], every: 30000 });
 *     runs fn now, on every matching event (coalesced), on reconnect, and on
 *     the `every` timer. `every` may be a function, for a cadence that depends
 *     on state ("fast while recording"). Returns a stop function.
 *   BGEvents.on(["item.done"], fn)   — the raw event, no timer.
 *   BGEvents.up()                     — is the socket connected right now.
 */
(function () {
  "use strict";
  var URL = "/api/events/stream";
  var DEGRADED_MS = 5000;   // the cadence while the socket is down
  var COALESCE_MS = 150;    // a chain landing is six rows and one refetch

  var listeners = [];
  var stateListeners = [];
  var registered = {};
  var source = null;
  var up = false;
  var retryMs = 1000;
  var retryTimer = 0;

  function matches(pattern, kind) {
    if (pattern === "*" || pattern === kind) return true;
    return pattern.slice(-2) === ".*" && kind.indexOf(pattern.slice(0, -1)) === 0;
  }

  function dispatch(raw) {
    var ev = null;
    try { ev = JSON.parse(String(raw.data)); } catch (e) { ev = null; }
    if (!ev || !ev.kind) return;
    listeners.slice().forEach(function (l) {
      try {
        if (l.kinds.some(function (k) { return matches(k, ev.kind); })) l.fn(ev);
      } catch (e) { /* one bad panel must not silence the rest */ }
    });
  }

  function listenFor(es, kinds) {
    (kinds || []).forEach(function (k) {
      if (!k || registered[k] || k === "hello" || k === "vocabulary") return;
      registered[k] = true;
      es.addEventListener(k, dispatch);
    });
  }

  function setUp(next) {
    if (up === next) return;
    up = next;
    stateListeners.slice().forEach(function (fn) {
      try { fn(next); } catch (e) {}
    });
  }

  function connect() {
    if (source || typeof EventSource === "undefined") return;
    var es = new EventSource(URL);
    source = es;
    registered = {};
    es.addEventListener("hello", function (e) {
      try { listenFor(es, JSON.parse(String(e.data)).kinds || []); } catch (err) {}
      retryMs = 1000;
      setUp(true);
    });
    es.addEventListener("vocabulary", function (e) {
      try { listenFor(es, JSON.parse(String(e.data)).kinds || []); } catch (err) {}
    });
    // The browser retries a dropped socket itself (and carries Last-Event-ID
    // when it does); a server that answered with an error closes it for good,
    // and that one we rebuild with backoff. Either way `up` flips, and every
    // watcher refetches on the way back.
    es.onerror = function () {
      setUp(false);
      if (es.readyState === EventSource.CLOSED) {
        source = null;
        clearTimeout(retryTimer);
        retryTimer = setTimeout(connect, retryMs);
        retryMs = Math.min(retryMs * 2, 30000);
      }
    };
  }

  function on(kinds, fn) {
    connect();
    var entry = { kinds: Array.isArray(kinds) ? kinds : [kinds || "*"], fn: fn };
    listeners.push(entry);
    return function () {
      var i = listeners.indexOf(entry);
      if (i >= 0) listeners.splice(i, 1);
    };
  }

  function watch(fn, opts) {
    opts = opts || {};
    var kinds = opts.kinds || ["*"];
    var every = opts.every == null ? 30000 : opts.every;
    var timer = 0, debounce = 0, stopped = false;

    function cadence() {
      var ms = typeof every === "function" ? every() : every;
      if (!up) return Math.min(ms || DEGRADED_MS, DEGRADED_MS);
      return ms;
    }
    function run() { if (!stopped) { try { fn(); } catch (e) {} } }
    // A self-rescheduling timeout rather than setInterval, so a cadence given
    // as a function is re-read every tick and a state change moves the clock.
    function arm() {
      clearTimeout(timer);
      var ms = cadence();
      if (ms > 0) timer = setTimeout(function () { run(); arm(); }, ms);
    }
    var off = on(kinds, function () {
      clearTimeout(debounce);
      debounce = setTimeout(run, COALESCE_MS);
    });
    var onState = function (next) { if (next) run(); arm(); };
    stateListeners.push(onState);

    run();
    arm();
    return function () {
      stopped = true;
      off();
      clearTimeout(timer); clearTimeout(debounce);
      var i = stateListeners.indexOf(onState);
      if (i >= 0) stateListeners.splice(i, 1);
    };
  }

  window.BGEvents = { on: on, watch: watch, up: function () { return up; },
                      connect: connect, matches: matches };
})();
