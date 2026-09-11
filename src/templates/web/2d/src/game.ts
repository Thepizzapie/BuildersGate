/**
 * The game: a player, ground, a ledge to fall off, and the telemetry that makes
 * the feel arguable with numbers.
 *
 * SEPARATE FROM main.ts SO IT CAN BE TESTED WITHOUT A BROWSER. `step` is a pure
 * function of (state, input, dt) and returns the events it produced; game.test.ts
 * drives it with no canvas, no rAF and no DOM. A game loop that can only be
 * exercised by looking at it is a game loop whose jump nobody can regression-test.
 */
import { tunables, type Tunables } from "./tunables";

export interface Solid {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Input {
  left: boolean;
  right: boolean;
  jump: boolean;
}

export interface GameEvent {
  kind: string;
  data: Record<string, unknown>;
}

export interface State {
  x: number;
  y: number;
  vx: number;
  vy: number;
  w: number;
  h: number;
  grounded: boolean;
  /** Seconds since leaving the ground; drives coyote time. */
  airborne_for: number;
  /** Seconds since the jump key went down; drives the jump buffer. */
  jump_pressed_for: number;
  jump_held: boolean;
  /** Height of the current jump's apex above its takeoff, for telemetry. */
  takeoff_y: number;
  peak_y: number;
  falls: number;
}

export const WORLD_W = 640;
export const WORLD_H = 360;

/** Ground with a hole in it, so there is somewhere to fall from on the first run. */
export const SOLIDS: Solid[] = [
  { x: 0, y: 300, w: 260, h: 60 },
  { x: 380, y: 300, w: 260, h: 60 },
  { x: 250, y: 210, w: 110, h: 16 },
];

export function initialState(): State {
  return {
    x: 60, y: 268, vx: 0, vy: 0, w: 20, h: 32,
    grounded: true, airborne_for: 0, jump_pressed_for: Infinity,
    jump_held: false, takeoff_y: 268, peak_y: 268, falls: 0,
  };
}

function overlaps(s: State, solid: Solid): boolean {
  return s.x < solid.x + solid.w && s.x + s.w > solid.x &&
         s.y < solid.y + solid.h && s.y + s.h > solid.y;
}

/**
 * Advance the world by `dt` seconds. Returns whatever happened worth recording.
 *
 * dt IS CLAMPED. A backgrounded tab hands rAF a delta of several seconds on
 * return, and an unclamped step teleports the player through the floor — which
 * presents as "the collision is broken" and is not.
 */
export function step(s: State, input: Input, dt: number,
                     knobs: Tunables = tunables): GameEvent[] {
  const events: GameEvent[] = [];
  dt = Math.min(dt, 1 / 30);

  s.vx = (input.right ? 1 : 0) * knobs.run_speed -
         (input.left ? 1 : 0) * knobs.run_speed;

  if (input.jump && !s.jump_held) s.jump_pressed_for = 0;
  s.jump_held = input.jump;

  const may_jump = s.grounded || s.airborne_for <= knobs.coyote_time;
  if (s.jump_pressed_for <= knobs.jump_buffer && may_jump) {
    s.vy = -knobs.jump_velocity;
    s.grounded = false;
    s.takeoff_y = s.y;
    s.peak_y = s.y;
    s.jump_pressed_for = Infinity;
    s.airborne_for = knobs.coyote_time + 1; // spent — no double jump
    events.push({ kind: "jump", data: { x: s.x, coyote: !s.grounded } });
  }

  // Heavier on the way down: one multiplier is the whole difference between a
  // floaty jump and a crisp one, and it is the first thing to reach for when
  // somebody says the character feels like a balloon.
  const g = s.vy > 0 ? knobs.gravity * knobs.fall_multiplier : knobs.gravity;
  s.vy += g * dt;

  s.x = Math.max(0, Math.min(WORLD_W - s.w, s.x + s.vx * dt));

  const was_grounded = s.grounded;
  s.y += s.vy * dt;
  s.grounded = false;
  for (const solid of SOLIDS) {
    if (!overlaps(s, solid)) continue;
    // Landing only — a head-bump keeps the player under the platform rather
    // than snapping them on top of it.
    if (s.vy >= 0 && s.y + s.h - s.vy * dt <= solid.y + 1) {
      s.y = solid.y - s.h;
      s.vy = 0;
      s.grounded = true;
    } else if (s.vy < 0 && s.y - s.vy * dt >= solid.y + solid.h - 1) {
      s.y = solid.y + solid.h;
      s.vy = 0;
    }
  }

  s.peak_y = Math.min(s.peak_y, s.y);
  s.airborne_for = s.grounded ? 0 : s.airborne_for + dt;
  s.jump_pressed_for += dt;

  if (s.grounded && !was_grounded) {
    events.push({
      kind: "land",
      data: {
        x: s.x,
        air_time: Number(s.airborne_for.toFixed(3)),
        peak_h: Number((s.takeoff_y - s.peak_y).toFixed(1)),
      },
    });
  }

  if (s.y > WORLD_H + 80) {
    s.falls += 1;
    events.push({ kind: "fell_out", data: { falls: s.falls, x: s.x } });
    const fresh = initialState();
    fresh.falls = s.falls;
    Object.assign(s, fresh);
  }

  return events;
}

export function draw(ctx: CanvasRenderingContext2D, s: State): void {
  ctx.fillStyle = "#12141a";
  ctx.fillRect(0, 0, WORLD_W, WORLD_H);
  ctx.fillStyle = "#2b3242";
  for (const solid of SOLIDS) ctx.fillRect(solid.x, solid.y, solid.w, solid.h);
  ctx.fillStyle = s.grounded ? "#e4b363" : "#e08e45";
  ctx.fillRect(Math.round(s.x), Math.round(s.y), s.w, s.h);
}
