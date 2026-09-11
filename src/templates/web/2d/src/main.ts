/** Wiring only: canvas, keys, the frame loop, and telemetry out. */
import { draw, initialState, step, WORLD_H, WORLD_W, type Input } from "./game";
import { telemetry } from "./bgate/telemetry";
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

let last = performance.now();
function frame(now: number): void {
  const dt = (now - last) / 1000;
  last = now;
  for (const event of step(state, input(), dt)) {
    telemetry.emit(event.kind, event.data);
  }
  draw(ctx!, state);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
