/**
 * Builders Gate telemetry, the bridge between "it feels wrong" and a number.
 *
 * SAME WIRE CONTRACT AS THE GODOT AUTOLOAD, deliberately. A web game has no
 * environment variable and no file access, so it cannot be told which recording
 * it belongs to; it DISCOVERS the active one by polling the same-origin app at
 * /api/playtest/status and POSTs batched events to that session's
 * /api/playtest/<id>/events. That is the transport the Godot web export already
 * proved, and matching it byte for byte means the recorder, the aligner and
 * every downstream analysis work on a web game with no changes at all.
 *
 * Every event carries `ts`, a UNIX WALL-CLOCK timestamp, not seconds-since-boot.
 * The recorder's clock starts when RECORDING starts, which is not when the game
 * starts; wall clock is the only axis the two share. `t` is included for human
 * reading, but the aligner uses `ts`.
 *
 * COSTS NOTHING WHEN NOBODY IS RECORDING. With no active session the poll is one
 * fetch every few seconds and every emit is a no-op, open the game normally and
 * this does not touch the network after the first miss.
 *
 *     import { telemetry } from "./bgate/telemetry";
 *     telemetry.emit("jump", { air_time: 0.92, peak_h: 2.4 });
 */

export const SCHEMA_VERSION = 1;

const FLUSH_INTERVAL_MS = 1000;
const POLL_INTERVAL_MS = 2000;
/** Cap so a long silent stretch cannot grow unbounded. */
const BUFFER_MAX = 400;

export interface TelemetryEvent {
  schema: number;
  kind: string;
  ts: number;
  t: number;
  data: Record<string, unknown>;
}

class Telemetry {
  private t0 = Date.now();
  private session = -1;
  private buffer: TelemetryEvent[] = [];
  private polling = false;
  private flushing = false;
  private timer: ReturnType<typeof setInterval> | null = null;

  /** Begin discovering an active recording. Safe to call more than once. */
  start(): void {
    if (this.timer !== null) return;
    if (typeof fetch !== "function") return; // node test runner, not a browser
    this.timer = setInterval(() => {
      void this.tick();
    }, Math.min(FLUSH_INTERVAL_MS, POLL_INTERVAL_MS));
  }

  stop(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
  }

  emit(kind: string, data: Record<string, unknown> = {}): void {
    const now = Date.now();
    this.buffer.push({
      schema: SCHEMA_VERSION,
      kind,
      ts: now / 1000,
      t: (now - this.t0) / 1000,
      data,
    });
    // OLDEST DROPPED, NOT NEWEST. A capped buffer that refuses new events keeps
    // the first minute of a ten-minute session, which is never the minute
    // anybody is asking about.
    if (this.buffer.length > BUFFER_MAX) {
      this.buffer.splice(0, this.buffer.length - BUFFER_MAX);
    }
  }

  private async tick(): Promise<void> {
    if (this.session < 0) return this.poll();
    return this.flush();
  }

  private async poll(): Promise<void> {
    if (this.polling) return;
    this.polling = true;
    try {
      const res = await fetch("/api/playtest/status", { cache: "no-store" });
      if (!res.ok) return;
      const got = (await res.json()) as { id?: number; status?: string };
      if (typeof got.id === "number" && got.id >= 0 && got.status === "recording") {
        this.session = got.id;
      }
    } catch {
      // Not served by the dashboard, or offline. Both mean "nobody is
      // recording", which is not an error and must never reach the console:
      // a game that logs a red line every two seconds while working perfectly
      // teaches its developer to ignore the console.
    } finally {
      this.polling = false;
    }
  }

  private async flush(): Promise<void> {
    if (this.flushing || this.buffer.length === 0) return;
    this.flushing = true;
    const batch = this.buffer;
    this.buffer = [];
    try {
      const res = await fetch(`/api/playtest/${this.session}/events`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ events: batch }),
      });
      if (!res.ok) throw new Error(String(res.status));
    } catch {
      // PUT IT BACK. A dropped batch is a hole in the recording that nothing
      // downstream can tell apart from "the player did nothing".
      this.buffer = batch.concat(this.buffer).slice(-BUFFER_MAX);
      if (this.session >= 0) this.session = -1; // re-discover
    } finally {
      this.flushing = false;
    }
  }
}

export const telemetry = new Telemetry();
