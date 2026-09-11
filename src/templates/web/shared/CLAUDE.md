# __PROJECT_NAME__

A Builders Gate project. This file is what your Claude session reads first.

## What this is

A web game: TypeScript, [vite](https://vite.dev) for the dev server and the
build, [vitest](https://vitest.dev) for tests. No framework — the game loop is
in `src/game.ts` and it is yours to change.

## The loop

```bash
npm install          # once, and after any dependency change
npm run dev          # http://127.0.0.1:5173
npm test             # vitest
npm run build        # dist/
```

From an agent session the same four moves are `engine_status`, `web_dev`,
`web_test_run` and `web_build`. Prefer them: they take the engine lock, archive
the evidence and, in `web_build`'s case, refuse a payload nobody could download.

## The payload is the product

`web_build` measures what a first visit costs over the wire and fails when it
is over budget (25 MB by default). This is not fussiness. A Godot 3D game
exported to the web at 661 MB is what this check exists to prevent — it ran
perfectly and no player would ever have waited for it.

When it fails, read `payload.biggest`. It is almost always one texture, one
model, or an uncompressed audio file.

## Telemetry

`src/bgate/telemetry.ts` is the bridge between "it feels wrong" and a number.

```ts
import { telemetry } from "./bgate/telemetry";
telemetry.emit("jump", { air_time: 0.92, peak_h: 2.4 });
```

It costs nothing when nobody is recording. During a playtest it discovers the
live session from the dashboard and streams into it, so the events land beside
the video and the voice track on one clock.

Emit an event for anything you might later want to argue about with a number:
jumps, deaths, retries, time in a room. The feel tunables live in
`src/tunables.ts` — keep them there and emit them once at boot, so a recording
knows which values produced it.

## What NOT to do

- Do not commit `dist/` or `node_modules/`.
- Do not add a dependency without checking what it costs the payload.
- Do not reach for a framework because the game got complicated. The game loop
  getting complicated is a reason to split `game.ts`, not to add React.
