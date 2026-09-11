/**
 * The movement, asserted with no WebGL. game.ts holds no three.js precisely so
 * this file can exist.
 */
import { describe, expect, it } from "vitest";
import { angleDelta, cameraTarget, initialState, step, supportAt, type Input } from "./game";
import { tunables } from "./tunables";

const idle: Input = { forward: 0, strafe: 0, jump: false };
const DT = 1 / 60;

function run(s: ReturnType<typeof initialState>, input: Input, frames: number) {
  const events = [];
  for (let i = 0; i < frames; i++) events.push(...step(s, input, DT));
  return events;
}

describe("jump", () => {
  it("leaves the ground and lands again", () => {
    const s = initialState();
    const takeoff = run(s, { ...idle, jump: true }, 1);
    expect(takeoff.map((e) => e.kind)).toContain("jump");
    const events = run(s, idle, 180);
    const landed = events.find((e) => e.kind === "land");
    expect(landed).toBeDefined();
    expect(Number(landed!.data.peak_h)).toBeGreaterThan(0.8);
    expect(landed!.data.on).toBe("ground");
  });

  it("does not double jump", () => {
    const s = initialState();
    const jumps = run(s, { ...idle, jump: true }, 40)
      .filter((e) => e.kind === "jump");
    expect(jumps).toHaveLength(1);
  });

  it("honours coyote time and then stops honouring it", () => {
    const near = initialState();
    near.grounded = false;
    near.airborne_for = tunables.coyote_time * 0.5;
    expect(run(near, { ...idle, jump: true }, 1).map((e) => e.kind))
      .toContain("jump");

    const late = initialState();
    late.grounded = false;
    late.airborne_for = tunables.coyote_time * 5;
    expect(run(late, { ...idle, jump: true }, 1).map((e) => e.kind))
      .not.toContain("jump");
  });
});

describe("support", () => {
  it("finds the ground, and a prop above it", () => {
    expect(supportAt(0, 0, 0.4).y).toBe(0);
    const onPlatform = supportAt(4, -3, 0.4);
    expect(onPlatform.y).toBe(1);
    expect(onPlatform.prop?.kind).toBe("platform");
  });

  it("is nothing at all beyond the ground plane", () => {
    expect(supportAt(999, 999, 0.4).y).toBe(-Infinity);
  });

  it("lands the character on a platform rather than through it", () => {
    const s = initialState();
    s.pos.x = 4;
    s.pos.z = -3;
    s.pos.y = 6;
    s.grounded = false;
    const events = run(s, idle, 240);
    expect(s.grounded).toBe(true);
    expect(s.pos.y).toBe(1);
    expect(events.find((e) => e.kind === "land")?.data.on).toBe("platform");
  });

  it("falls out of the world past the ground plane and respawns", () => {
    const s = initialState();
    s.pos.x = 999;
    s.grounded = false;
    const events = run(s, idle, 600);
    expect(events.map((e) => e.kind)).toContain("fell_out");
    expect(s.falls).toBe(1);
  });
});

describe("turning", () => {
  it("takes the short way round", () => {
    expect(angleDelta(0.1, Math.PI * 2 - 0.1)).toBeCloseTo(-0.2, 5);
    expect(angleDelta(Math.PI * 2 - 0.1, 0.1)).toBeCloseTo(0.2, 5);
  });

  it("faces the direction of travel", () => {
    const s = initialState();
    run(s, { forward: 1, strafe: 0, jump: false }, 60);
    expect(Math.abs(angleDelta(s.yaw, Math.PI))).toBeLessThan(0.05);
    expect(s.pos.z).toBeLessThan(-1);
  });
});

describe("camera", () => {
  it("sits behind and above the character", () => {
    const s = initialState();
    const want = cameraTarget(s);
    expect(want.y).toBeCloseTo(tunables.camera_height, 5);
    expect(Math.hypot(want.x - s.pos.x, want.z - s.pos.z))
      .toBeCloseTo(tunables.camera_distance, 5);
  });
});

describe("timestep", () => {
  it("survives the delta a backgrounded tab hands it", () => {
    const s = initialState();
    step(s, idle, 5);
    expect(s.pos.y).toBe(0);
    expect(s.grounded).toBe(true);
  });
});
