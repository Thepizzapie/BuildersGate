/* Seat -> agent name, shared by the stream overlays (agent-feed, agent-ticker).
 *
 * Seats are the pipeline's internal vocabulary; on stream they read better as
 * people. Rename freely — the overlays pick these up on their next reload, and
 * anything not listed falls back to the seat name in caps.
 */
window.AGENT_NAMES = {
  director:  "Vale",
  narrative: "Quill",
  gameplay:  "Roan",
  art:       "Iris",
  qa:        "Marlow",
  tech:      "Kepler",
  audio:     "Sable",
  cinematic: "Reel"
};

window.agentName = function (seat) {
  if (!seat) return "System";
  return window.AGENT_NAMES[seat] || seat.toUpperCase();
};

/* Deterministic two-tone assignment so seats stay visually distinct without
 * introducing colours outside the overlay palette. */
window.agentTone = function (seat) {
  var order = Object.keys(window.AGENT_NAMES);
  var i = order.indexOf(seat);
  if (i < 0) return "c";
  return ["a", "b"][i % 2];
};
