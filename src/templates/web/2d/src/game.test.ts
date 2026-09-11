/**
 * The feel, asserted. `step` is pure, so the jump can be regression-tested with
 * no canvas and no browser, which is the whole reason it lives apart from
 * main.ts.
 */
import { describe, expect, it } from "vitest";
import { initialState, SOLIDS, step, WORLD_H, type Input } from "./game";
import { tunables } from "./tunables";

const idle: Input = { left: false, right: false, jump: false };
const jump: Input = { ...idle, jump: true };
const DT = 1 / 60;

function run(state: ReturnType<typeof initialState>, input: Input, frames: number) {
  const events = [];
  for (let i = 0; i < frames; i++) events.push(...step(state, input, DT));
  return events;
}

describe("jump", () => {
  it("leaves the ground and comes back to it", () => {
    const s = initialState();
    expect(s.grounded).toBe(true);
    const takeoff = run(s, jump, 1);
    expect(takeoff.map((e) => e.kind)).toContain("jump");
    expect(s.grounded).toBe(false);

    const events = run(s, idle, 120);
    const landed = events.find((e) => e.kind === "land");
    expect(landed).toBeDefined();
    expect(Number(landed!.data.peak_h)).toBeGreaterThan(40);
  });

  it("does not double jump", () => {
    const s = initialState();
    const events = run(s, jump, 30);
    expect(events.filter((e) => e.kind === "jump")).toHaveLength(1);
  });

  it("still works just after walking off a ledge (coyote time)", () => {
    const s = initialState();
    s.grounded = false;
    s.airborne_for = tunables.coyote_time * 0.5;
    const events = run(s, jump, 1);
    expect(events.map((e) => e.kind)).toContain("jump");
  });

  it("does not work long after leaving the ground", () => {
    const s = initialState();
    s.grounded = false;
    s.airborne_for = tunables.coyote_time * 5;
    const events = run(s, jump, 1);
    expect(events.map((e) => e.kind)).not.toContain("jump");
  });
});

describe("collision", () => {
  it("stands on the ground rather than sinking through it", () => {
    const s = initialState();
    run(s, idle, 60);
    expect(s.grounded).toBe(true);
    expect(s.y + s.h).toBeCloseTo(SOLIDS[0]!.y, 3);
  });

  it("survives the delta a backgrounded tab hands it", () => {
    // rAF delivers several seconds at once when a tab comes back. Unclamped,
    // one step teleports the player through the floor and it reads as broken
    // collision.
    const s = initialState();
    step(s, idle, 5);
    expect(s.y).toBeLessThan(WORLD_H);
    expect(s.grounded).toBe(true);
  });

  it("respawns after falling down the hole", () => {
    const s = initialState();
    s.x = 320;              // the gap between the two ground slabs
    s.grounded = false;
    const events = run(s, idle, 240);
    expect(events.map((e) => e.kind)).toContain("fell_out");
    expect(s.falls).toBe(1);
    expect(s.grounded).toBe(true);
  });
});
