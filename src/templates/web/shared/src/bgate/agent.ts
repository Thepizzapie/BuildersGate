/**
 * Builders Gate agent: lets a decision model PLAY this web game.
 *
 * OFF unless the page URL carries `?bgate_agent=<relay url>`, which only
 * `playtest_agent_run` / `playtest_feel_check` add. A normal visit pays one
 * URLSearchParams read and nothing else.
 *
 * The game owns two hooks:
 *
 *   agent.tick(dt, observe)   once per frame, with the frame's dt in seconds
 *                              and a function that describes the game NOW
 *   agent.isPaused()           true while a decision is in flight: skip the
 *                              simulation and any countdown that frame, so a
 *                              250 ms model call costs wall time, not game time
 *
 * observe() returns null (nothing to decide, e.g. mid-transition) or:
 *   {
 *     state: "plain sentences about what the player sees",   // or an object
 *     options: {
 *       accept: { keys: ["KeyA"], desc: "accept the diff" },  // hold keys
 *       reject: { click: "#reject", desc: "reject it" },       // click an element
 *       scroll: { keys: ["ArrowDown"], tap: ["ArrowDown"], desc: "read further" },
 *     },
 *     goal: "ship the feature without breaking prod",         // optional
 *     progress: 0.4,   // 0..1 through the scene  - the feel check needs it
 *     danger: true,    // something will hurt the player soon - and this
 *   }
 * An option's keys stay HELD while it is chosen again; a key listed under
 * `tap` is released and pressed again for a fresh press. Offer an option only
 * when it can act: an always-on option reads to the model as always relevant
 * (measured: an always-on "enter door" was chosen 673 times in a row).
 *
 * Write the state as sentences and do the arithmetic for the model ("the deadline
 * is 4 s away", "line 12 deletes a test"): raw JSON got a model 0.32 mean
 * confidence and one option 90% of the time; plain lines with the facts worked
 * out got 0.48-1.0 and sensible play.
 */

type Opt = { keys?: string[]; tap?: string[]; click?: string; desc?: string };
export interface Observation {
  state: string | Record<string, unknown>;
  options: Record<string, Opt | string>;
  goal?: string;
  progress?: number;
  danger?: boolean;
}

const params = new URLSearchParams(location.search);
const relay = params.get("bgate_agent") || "";
const base = relay.replace(/\/decide$/, "");
const runSeconds = Number(params.get("bgate_seconds") || 0);
const everyS = Number(params.get("bgate_every") || 0.1);

let paused = false;
let since = 0;
let gameTime = 0;
let done = false;
let frame = 0;
let options: Record<string, Opt> = {};
let held: string[] = [];
let pending: string[] = [];
let outbox: Record<string, unknown>[] = [];
let flushAt = 0;

function send(code: string, down: boolean): void {
  const type = down ? "keydown" : "keyup";
  const ev = new KeyboardEvent(type, { code, key: code, bubbles: true });
  window.dispatchEvent(ev);
  document.dispatchEvent(new KeyboardEvent(type, { code, key: code, bubbles: true }));
}

function normalise(raw: Observation["options"]): Record<string, Opt> {
  const out: Record<string, Opt> = {};
  for (const [name, v] of Object.entries(raw || {})) {
    out[name] = typeof v === "string" ? { desc: v } : v;
  }
  return out;
}

async function post(path: string, body: unknown): Promise<unknown> {
  const res = await fetch(base + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.ok ? res.json() : null;
}

function flush(final = false): void {
  if (!outbox.length && !final) return;
  const events = outbox;
  outbox = [];
  void post("/events", { events }).catch(() => undefined);
}

async function decide(obs: Observation): Promise<void> {
  options = normalise(obs.options);
  const described: Record<string, string> = {};
  for (const [k, v] of Object.entries(options)) described[k] = v.desc || "";
  const body: Record<string, unknown> = {
    state: obs.state, options: described, frame, hold_s: everyS,
  };
  for (const key of ["goal", "progress", "danger"] as const) {
    if (obs[key] !== undefined) body[key] = obs[key];
  }
  paused = true;
  const t0 = performance.now();
  try {
    const got = (await post("/decide", body)) as { choice?: string; confidence?: number } | null;
    const choice = String(got?.choice || "");
    const opt = options[choice];
    if (!opt) return;
    const keep = opt.keys || [];
    const tap = opt.tap || [];
    // HOLD WHAT IS CHOSEN AGAIN, RELEASE THE REST (the Godot agent's rule:
    // releasing everything between decisions cut a held action every 0.1 s).
    for (const k of [...held]) {
      if (!keep.includes(k) || tap.includes(k)) {
        send(k, false);
        held = held.filter((h) => h !== k);
      }
    }
    pending = keep.filter((k) => !held.includes(k));
    if (opt.click) (document.querySelector(opt.click) as HTMLElement | null)?.click();
    report("agent_decision", {
      choice, confidence: got?.confidence ?? 0, ms: Math.round(performance.now() - t0), frame,
    });
  } catch {
    // relay gone: the run is over or capped; the runner closes the page.
  } finally {
    paused = false;
  }
}

/** Forward one telemetry event to the relay (telemetry.ts calls this too). */
export function report(kind: string, data: Record<string, unknown> = {}): void {
  if (!relay) return;
  outbox.push({ ts: Date.now() / 1000, t: gameTime, kind, data });
}

export const agent = {
  active: relay !== "",
  isPaused: (): boolean => paused,
  tick(dt: number, observe: () => Observation | null): void {
    if (!relay || done) return;
    for (const k of pending) send(k, true);
    held = [...held, ...pending.filter((k) => !held.includes(k))];
    pending = [];
    if (paused) return;
    frame += 1;
    gameTime += dt;
    since += dt;
    if (gameTime >= flushAt) {
      flushAt = gameTime + 1;
      flush();
    }
    if (runSeconds > 0 && gameTime >= runSeconds) {
      done = true;
      for (const k of held) send(k, false);
      flush(true);
      void post("/done", { game_seconds: gameTime }).catch(() => undefined);
      return;
    }
    if (since < everyS) return;
    since = 0;
    const obs = observe();
    if (obs) void decide(obs);
  },
};
