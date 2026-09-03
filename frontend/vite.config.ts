import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// package.json says type=module, so this config is loaded as ESM and there is
// no __dirname to lean on.
const here = dirname(fileURLToPath(import.meta.url));

/* ONE FRONTEND SOURCE TREE, ONE BUILD OUTPUT.
   This folder is the whole frontend now. `public/` holds the classic
   hand-written modules (index.html, app.css, the seat panels, the stream
   overlay pages, vendor libs, images) and `src/` holds the React shell. They
   used to live in two places — bgate_ui/static/ and here — which meant a
   reader had to know which half of the UI a screen belonged to before they
   could find the file that draws it.

   src/bgate_ui/static/ IS GENERATED. Vite copies public/ into it verbatim and
   emits the React bundle under dist/, so every served URL is exactly what it
   was (/static/app.css, /static/wf.js, /static/dist/bgate.js) and the server
   needed no route change. Do not hand-edit anything in there; it is wiped and
   rewritten on every build.

   WHAT IS COMMITTED IS dist/ ONLY (.gitignore: `/src/bgate_ui/static/*` with
   `!/src/bgate_ui/static/dist/`). dist/ is the one artefact that needs node;
   the rest of static/ is a verbatim copy of public/ and tracking it would
   store every module twice. So a fresh clone has dist/ and no index.html:
   `bgate serve` from a checkout fills the copy in itself (bgate_ui.app
   _static_dir copies public/ over at startup), while a WHEEL OR EXE NEEDS
   `npm run build` FIRST — ci.yml's wheel-smoke and release-exe.yml both run it
   before packaging, and pyproject's package-data glob ships static/ wholesale.

   The floor's img/floor and audio/floor are NOT here at all: they live in
   packaging/floor-assets/ as their own package (builders-gate-floor-assets),
   which the server mounts when it is installed.

   FIXED FILENAMES, NOT HASHED. index.html is post-processed in Python, which
   rewrites every `/static/**.js|css` reference with a `?v=<mtime>` stamp; that
   is already the cache-busting story, and a hashed name would mean editing
   index.html on every build for no gain. */
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: resolve(here, "../src/bgate_ui/static"),
    // Outside the vite root, so this needs saying explicitly. It is safe
    // BECAUSE the whole directory is generated: public/ is copied back in on
    // the same build, and nothing else is meant to live there.
    emptyOutDir: true,
    target: "es2020",
    cssCodeSplit: false,
    // Not emitted: the build output is COMMITTED, and a 900 kB map per build is
    // a megabyte of churn in every diff. `npm run dev` is the debugging path.
    sourcemap: false,
    rollupOptions: {
      input: {
        bgate: resolve(here, "src/main.tsx"),
        "floor-overlay": resolve(here, "src/floor-overlay.tsx"),
      },
      output: {
        format: "es",
        // Still under dist/, because index.html and the cache-bust stamper in
        // bgate_ui/app.py both reference /static/dist/bgate.js by that path.
        entryFileNames: (chunk) =>
          chunk.name === "bgate" ? "dist/bgate.js" : "dist/[name].js",
        chunkFileNames: "dist/bgate-[name].js",
        assetFileNames: (info) =>
          info.names?.[0]?.endsWith(".css") ? "dist/bgate.css" : "dist/[name][extname]",
      },
    },
  },
});
