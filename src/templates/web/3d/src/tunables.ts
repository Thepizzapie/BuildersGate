/**
 * The feel knobs, in one module, exported, the join that makes "the movement
 * feels wrong" actionable.
 *
 * Emitted once at boot by main.ts so a recording knows which numbers produced
 * it. A value typed straight into the physics step cannot be recovered from a
 * video three weeks later.
 *
 * Metres and seconds throughout. gravity is 18 rather than 9.81 because a
 * real-gravity character in a game-scale world reads as slow motion, the
 * number that matches physics is almost never the number that feels right,
 * and pretending otherwise is how a jump ends up "floaty" with nobody able to
 * say why.
 */
export interface Tunables {
  gravity: number;
  fall_multiplier: number;
  jump_velocity: number;
  run_speed: number;
  /** How fast the character turns to face where it is going, radians/sec. */
  turn_speed: number;
  coyote_time: number;
  jump_buffer: number;
  /** Metres behind and above the character the camera sits. */
  camera_distance: number;
  camera_height: number;
  /** 0 = rigid, 1 = instant. How quickly the camera catches up. */
  camera_lag: number;
}

export const tunables: Tunables = {
  gravity: 18,
  fall_multiplier: 1.6,
  jump_velocity: 7,
  run_speed: 5,
  turn_speed: 10,
  coyote_time: 0.12,
  jump_buffer: 0.12,
  camera_distance: 7,
  camera_height: 3.2,
  camera_lag: 0.12,
};
