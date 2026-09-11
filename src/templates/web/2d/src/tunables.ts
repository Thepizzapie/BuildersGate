/**
 * The feel knobs, in one module, exported, the join that makes "the jump feels
 * floaty" actionable.
 *
 * THEY ARE HERE RATHER THAN INLINE FOR ONE REASON: a recording has to be able to
 * say which values produced it. main.ts emits this object once at boot, so a
 * playtest three weeks old still knows what gravity was when it was captured.
 * A number typed straight into the physics step cannot be recovered afterwards.
 *
 * coyote_time and jump_buffer are the two that most often turn "the controls
 * are unresponsive" into a fixed bug rather than an argument.
 */
export interface Tunables {
  /** Pixels per second squared. */
  gravity: number;
  /** Extra gravity while falling, the difference between floaty and crisp. */
  fall_multiplier: number;
  /** Upward pixels per second at the moment of a jump. */
  jump_velocity: number;
  /** Horizontal pixels per second. */
  run_speed: number;
  /** Seconds after walking off a ledge during which a jump still works. */
  coyote_time: number;
  /** Seconds before landing during which a jump press is remembered. */
  jump_buffer: number;
}

export const tunables: Tunables = {
  gravity: 1800,
  fall_multiplier: 1.7,
  jump_velocity: 560,
  run_speed: 200,
  coyote_time: 0.1,
  jump_buffer: 0.12,
};
