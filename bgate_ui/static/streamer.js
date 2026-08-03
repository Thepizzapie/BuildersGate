/* The streamer-mode indicator.
 *
 * The toggle lives in Settings > Privacy and the registry renders it for free.
 * This file exists for the OTHER half, which is the half that actually
 * protects anyone: a persistent, unmissable statement of whether the filter is
 * on, visible from every workspace without opening a panel.
 *
 * A redaction filter you cannot see the state of is worse than none, because
 * you rely on it. The failure is silent and one-directional — nobody discovers
 * mid-stream that paths were hidden; they discover that they were not.
 *
 * So the chip is LOUD WHEN ON and quiet when off, which is the inverse of the
 * usual status-light convention. Off is the default and the normal state for
 * every existing user, and a permanent red warning on a private machine is a
 * warning people learn to stop seeing.
 */
(function () {
  'use strict';

  var HOST_ID = 'sb-streamer';
  var POLL_MS = 15000;   // it changes when a human clicks; it is not telemetry
  var state = null;

  function host() { return document.getElementById(HOST_ID); }

  function render(s) {
    var el = host();
    if (!el) return;
    var on = !!(s && s.on);

    if (!on) {
      // Quiet: a bare chip that names the thing and how to turn it on. No
      // colour, because "not currently hiding anything" is not an alarm.
      el.className = 'schip streamer-chip';
      el.innerHTML = '<span class="l warn"></span> streamer <b>off</b>';
      el.title = (s && s.note) ||
        'Paths, your username and keys are shown in full. ' +
        'Settings > Privacy to hide them.';
      return;
    }

    el.className = 'schip streamer-chip on';
    el.innerHTML = '<span class="l"></span> streamer <b>on</b>';
    // The tooltip is where the LIMITS go. Someone trusting this deserves to
    // read what it does not cover before they trust it, not after.
    var covered = [];
    if (s.home) covered.push('home');
    if (s.user) covered.push('username');
    if (s.host) covered.push('hostname');
    if (s.roots) covered.push(s.roots + ' project path' + (s.roots > 1 ? 's' : ''));
    if (s.known_secrets) covered.push(s.known_secrets + ' known key' +
                                      (s.known_secrets > 1 ? 's' : ''));
    el.title =
      'Hiding: ' + (covered.join(', ') || 'nothing detected to hide') + '.\n' +
      (s.env_forced ? 'Forced on by BGATE_STREAMER in the environment.\n' : '') +
      'Display filter only — the .env, the database and devtools are ' +
      'unchanged, and the dashboard token is not redacted because the page ' +
      'needs it to authenticate.';
  }

  function refresh() {
    return fetch('/api/streamer')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (body) {
        if (!body) return;
        state = body.data || body;
        render(state);
      })
      .catch(function () {
        // A failed poll must NOT leave a stale "on" on screen. If we cannot
        // confirm the filter is running, say so — the whole value of the chip
        // is that it never claims protection it has not verified.
        var el = host();
        if (el) {
          el.className = 'schip streamer-chip unknown';
          el.innerHTML = '<span class="l bad"></span> streamer <b>?</b>';
          el.title = 'Could not reach the server to confirm whether the ' +
                     'redaction filter is on. Assume it is not.';
        }
      });
  }

  function mount() {
    var bar = document.querySelector('.statusbar');
    if (!bar || host()) return;
    var el = document.createElement('div');
    el.id = HOST_ID;
    el.className = 'schip streamer-chip';
    // Before the bell, after the counters: it belongs with the things that
    // describe the session rather than the floor.
    var bell = document.getElementById('nt-host');
    if (bell) bar.insertBefore(el, bell); else bar.appendChild(el);
    refresh();
    setInterval(refresh, POLL_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }

  // Settings calls this after a save so the chip does not lag the switch by a
  // poll interval — flipping a privacy control and watching nothing happen is
  // how someone concludes it did not work.
  window.StreamerChip = { refresh: refresh, state: function () { return state; } };
})();
