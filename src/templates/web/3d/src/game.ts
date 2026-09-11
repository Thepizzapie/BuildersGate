/**
 * The simulation: a third-person character on a ground plane with a few props.
 *
 * NO THREE.JS IN THIS FILE, and that is the point. Everything here is plain
 * numbers, so the movement can be regression-tested with no WebGL, no canvas
 * and no browser, see game.test.ts. main.ts owns the scene graph and does
 * nothing but read this state and place meshes at it. A controller that can
 * only be exercised by looking at it is a controller whose jump nobody can
 * test.
 *
 * Right-handed, Y up, metres and seconds, the same convention three.js and
 * glTF both use, so a model dropped in needs no conversion.
 */
import { tunables, type Tunables } from "./tunables";

export interface Vec3 { x: number; y: number; z: number; }

export interface Prop {
  /** Centre of the box. */
  x: number; y: number; z: number;
  /** Half-extents. */
  hx: number; hy: number; hz: number;
  kind: "platform" | "crate" | "ramp";
}

export interface Input {
  /** -1..1 in camera space; main.ts maps the keys. */
  forward: number;
  strafe: number;
  jump: boolean;
}

export interface GameEvent { kind: string; data: Record<string, unknown>; }

export interface State {
  pos: Vec3;
  vel: Vec3;
  /** Radians; the direction the character faces, not the camera. */
  yaw: number;
  radius: number;
  height: number;
  grounded: boolean;
  airborne_for: number;
  jump_pressed_for: number;
  jump_held: boolean;
  takeoff_y: number;
  peak_y: number;
  falls: number;
}

export const GROUND_Y = 0;
export const GROUND_HALF = 25;

export const PROPS: Prop[] = [
  { x: 4, y: 0.5, z: -3, hx: 2, hy: 0.5, hz: 2, kind: "platform" },
  { x: -5, y: 0.6, z: -6, hx: 0.6, hy: 0.6, hz: 0.6, kind: "crate" },
  { x: -3.5, y: 0.6, z: -6, hx: 0.6, hy: 0.6, hz: 0.6, kind: "crate" },
  { x: 0, y: 1.6, z: -10, hx: 3, hy: 0.4, hz: 1.5, kind: "platform" },
  { x: 8, y: 0.4, z: 4, hx: 1.5, hy: 0.4, hz: 3, kind: "ramp" },
];

export function initialState(): State {
  return {
    pos: { x: 0, y: 0, z: 0 },
    vel: { x: 0, y: 0, z: 0 },
    yaw: 0,
    radius: 0.4,
    height: 1.8,
    grounded: true,
    airborne_for: 0,
    jump_pressed_for: Infinity,
    jump_held: false,
    takeoff_y: 0,
    peak_y: 0,
    falls: 0,
  };
}

/** Shortest signed angle from a to b, so turning never takes the long way. */
export function angleDelta(a: number, b: number): number {
  let d = (b - a) % (Math.PI * 2);
  if (d > Math.PI) d -= Math.PI * 2;
  if (d < -Math.PI) d += Math.PI * 2;
  return d;
}

/** Highest surface under the character at (x, z), and what it belongs to. */
export function supportAt(x: number, z: number, radius: number):
    { y: number; prop: Prop | null } {
  let best = Math.abs(x) <= GROUND_HALF && Math.abs(z) <= GROUND_HALF
    ? GROUND_Y : -Infinity;
  let owner: Prop | null = null;
  for (const p of PROPS) {
    if (Math.abs(x - p.x) > p.hx + radius) continue;
    if (Math.abs(z - p.z) > p.hz + radius) continue;
    const top = p.y + p.hy;
    if (top > best) { best = top; owner = p; }
  }
  return { y: best, prop: owner };
}

/**
 * Advance the world by `dt` seconds.
 *
 * dt IS CLAMPED for the same reason the 2D template clamps it: a backgrounded
 * tab hands rAF several seconds at once, and an unclamped step drops the
 * character through the floor, which presents as broken collision and is not.
 */
export function step(s: State, input: Input, dt: number,
                     knobs: Tunables = tunables): GameEvent[] {
  const events: GameEvent[] = [];
  dt = Math.min(dt, 1 / 30);

  const mag = Math.hypot(input.forward, input.strafe);
  if (mag > 1e-3) {
    const nx = input.strafe / mag;
    const nz = -input.forward / mag;
    s.vel.x = nx * knobs.run_speed;
    s.vel.z = nz * knobs.run_speed;
    // FACE WHERE YOU ARE GOING, over time rather than instantly: a character
    // that snaps to a new heading reads as a sprite being flipped, not a body
    // turning.
    const want = Math.atan2(nx, nz);
    s.yaw += angleDelta(s.yaw, want) *
             Math.min(1, knobs.turn_speed * dt);
  } else {
    s.vel.x = 0;
    s.vel.z = 0;
  }

  if (input.jump && !s.jump_held) s.jump_pressed_for = 0;
  s.jump_held = input.jump;

  const may_jump = s.grounded || s.airborne_for <= knobs.coyote_time;
  if (s.jump_pressed_for <= knobs.jump_buffer && may_jump) {
    s.vel.y = knobs.jump_velocity;
    s.grounded = false;
    s.takeoff_y = s.pos.y;
    s.peak_y = s.pos.y;
    s.jump_pressed_for = Infinity;
    s.airborne_for = knobs.coyote_time + 1;
    events.push({ kind: "jump", data: { x: s.pos.x, z: s.pos.z } });
  }

  const g = s.vel.y < 0 ? knobs.gravity * knobs.fall_multiplier : knobs.gravity;
  s.vel.y -= g * dt;

  s.pos.x += s.vel.x * dt;
  s.pos.z += s.vel.z * dt;
  s.pos.y += s.vel.y * dt;

  const was_grounded = s.grounded;
  const support = supportAt(s.pos.x, s.pos.z, s.radius);
  s.grounded = false;
  if (s.vel.y <= 0 && s.pos.y <= support.y + 1e-3) {
    s.pos.y = support.y;
    s.vel.y = 0;
    s.grounded = true;
  }

  s.peak_y = Math.max(s.peak_y, s.pos.y);
  s.airborne_for = s.grounded ? 0 : s.airborne_for + dt;
  s.jump_pressed_for += dt;

  if (s.grounded && !was_grounded) {
    events.push({
      kind: "land",
      data: {
        air_time: Number(s.airborne_for.toFixed(3)),
        peak_h: Number((s.peak_y - s.takeoff_y).toFixed(2)),
        on: support.prop ? support.prop.kind : "ground",
      },
    });
  }

  if (s.pos.y < -30) {
    s.falls += 1;
    events.push({ kind: "fell_out", data: { falls: s.falls } });
    const fresh = initialState();
    fresh.falls = s.falls;
    Object.assign(s, fresh);
  }

  return events;
}

/** Where the camera wants to be this frame, given the character. */
export function cameraTarget(s: State, knobs: Tunables = tunables): Vec3 {
  return {
    x: s.pos.x - Math.sin(s.yaw) * knobs.camera_distance,
    y: s.pos.y + knobs.camera_height,
    z: s.pos.z - Math.cos(s.yaw) * knobs.camera_distance,
  };
}
