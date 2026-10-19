/// <reference types="vitest/config" />
import { defineConfig } from "vite";

// THE DEV SERVER PROXIES THE DASHBOARD'S API, and that is not a convenience.
// telemetry.ts discovers a live recording by polling /api/playtest/status on
// its OWN origin, the same contract the Godot web export uses. Without this
// proxy the dev server answers that with its index.html, the game concludes
// nobody is recording, and a playtest captures nothing while looking fine.
//
// THE PORT IS THIS PROJECT'S DASHBOARD, NOT A NUMBER. A second web project on
// one machine runs its dashboard on another port (`bgate serve --port 7790`
// is what the collision message tells you to do), and a proxy that still said
// 7788 did not fail: it recorded this game's playtests into the OTHER game's
// board, and both boards looked healthy. Builders Gate passes BGATE_PORT to
// the dev server it starts, and `bgate serve` writes the same port to
// .bgate/ui-port; 7788 is only the fallback for a dev server started by hand.
const dashboardPort = process.env.BGATE_PORT ?? "7788";

export default defineConfig({
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${dashboardPort}`,
        changeOrigin: true,
      },
    },
  },
  // .bgate/ holds the harness's preimages: byte copies of files an agent
  // edited, test files included. vitest collecting them ran every suite
  // twice and failed it on stale copies.
  test: {
    exclude: ["**/node_modules/**", ".bgate/**", "dist/**"],
  },
  build: {
    // Named chunks and no inlining, so `web_payload` can attribute the budget
    // to a file somebody can act on rather than to one anonymous bundle.
    assetsInlineLimit: 0,
    sourcemap: true,
    reportCompressedSize: true,
  },
});
