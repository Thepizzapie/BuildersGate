/** Wiring only: canvas, keys, the frame loop, and telemetry out. */
import { draw, initialState, step, WORLD_H, WORLD_W, type Input } from "./game";
import { telemetry } from "./bgate/telemetry";
import { agent, type Observation } from "./bgate/agent";
import { tunables } from "./tunables";

const canvas = document.querySelector<HTMLCanvasElement>("#game");
if (!canvas) throw new Error("no #game canvas in the page");
const ctx = canvas.getContext("2d");
if (!ctx) throw new Error("this browser has no 2d canvas context");

canvas.width = WORLD_W;
canvas.height = WORLD_H;

const held = new Set<string>();
addEventListener("keydown", (e) => {
  held.add(e.code);
  // Space and the arrows scroll the page otherwise, which reads in a capture
  // as "the jump is intermittent".
  if (["Space", "ArrowLeft", "ArrowRight", "ArrowUp"].includes(e.code)) {
    e.preventDefault();
  }
});
addEventListener("keyup", (e) => held.delete(e.code));

function input(): Input {
  return {
    left: held.has("ArrowLeft") || held.has("KeyA"),
    right: held.has("ArrowRight") || held.has("KeyD"),
    jump: held.has("Space") || held.has("ArrowUp") || held.has("KeyW"),
  };
}

const state = initialState();

telemetry.start();
// ONCE AT BOOT, so a recording knows which numbers produced it. A tunable typed
// into the physics step cannot be recovered from a video three weeks later.
telemetry.emit("tunables", { ...tunables });

// WHAT A BOT SEES (playtest_agent_run / playtest_feel_check). Sentences, the
// arithmetic done, and only the options that can act right now. Replace this
// with your game's own view as soon as there is more than a ledge to describe.
function observe(): Observation {
  const opts: Observation["options"] = {
    left: { keys: ["KeyA"], desc: "run left" },
    right: { keys: ["KeyD"], desc: "run right" },
  };
  if (state.grounded) {
    opts.jump_right = { keys: ["Space", "KeyD"], tap: ["Space"], desc: "jump to the right" };
  }
  return {
    state: `Player at x=${Math.round(state.x)} of ${WORLD_W}, ` +
      (state.grounded ? "on the ground." : "in the air."),
    options: opts,
    goal: "reach the right edge",
    progress: Math.min(1, Math.max(0, state.x / WORLD_W)),
    danger: false,
  };
}

let last = performance.now();
function frame(now: number): void {
  const dt = (now - last) / 1000;
  last = now;
  agent.tick(dt, observe);
  // Lockstep: while a bot's decision is in flight the world holds still.
  if (!agent.isPaused()) {
    for (const event of step(state, input(), dt)) {
      telemetry.emit(event.kind, event.data);
    }
  }
  draw(ctx!, state);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
