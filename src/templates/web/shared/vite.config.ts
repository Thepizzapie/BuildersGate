import { defineConfig } from "vite";

// THE DEV SERVER PROXIES THE DASHBOARD'S API, and that is not a convenience.
// telemetry.ts discovers a live recording by polling /api/playtest/status on
// its OWN origin — the same contract the Godot web export uses. Without this
// proxy the dev server answers that with its index.html, the game concludes
// nobody is recording, and a playtest captures nothing while looking fine.
export default defineConfig({
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:7788",
        changeOrigin: true,
      },
    },
  },
  build: {
    // Named chunks and no inlining, so `web_payload` can attribute the budget
    // to a file somebody can act on rather than to one anonymous bundle.
    assetsInlineLimit: 0,
    sourcemap: true,
    reportCompressedSize: true,
  },
});
