# frontend

The dashboard's whole UI lives here. It used to live in two places — the classic
modules under `bgate_ui/static/` and the React shell here — which meant finding
the file that draws a screen started with knowing which half of the product it
belonged to.

    public/   the classic hand-written modules: index.html, app.css, the seat
              panels under seats/, the stream overlay pages, vendor libraries
              and images. Plain scripts on the page, no build step of their own.
    src/      the React shell: the chrome (rail, screens, inspector) and the
              screens that have been rewritten — orchestration, brainstorm,
              settings, seats, board, floor.

Both halves ship together. `npm run build` copies `public/` verbatim into
`bgate_ui/static/` and emits the React bundle beside it as `dist/bgate.js` and
`dist/bgate.css`, so every served URL is what it always was: `/static/app.css`,
`/static/wf.js`, `/static/dist/bgate.js`.

## bgate_ui/static is generated

Do not edit anything in there. It is emptied and rewritten on every build, so a
fix made in the output is a fix that disappears the next time somebody builds.
Edit the file here that produced it.

It is also COMMITTED, which is unusual enough to say why: the dashboard is
distributed as a Python wheel and as a PyInstaller exe, and neither has node. A
`pip install builders-gate` with no toolchain still has to serve a dashboard.

## Working on it

    cd frontend
    npm install
    npm run build          # then reload the dashboard
    npx tsc --noEmit       # types only, no emit

The page is served by `bgate serve` (FastAPI mounts the built tree), not by a
Vite dev server — `npm run dev` exists but the app expects its own backend on
127.0.0.1:7788, so building and reloading is the shorter loop.

A screen that is still a classic module is mounted by index.html and drawn by
its own `public/*.js`; a screen that is React is a `data-react` host in
index.html that `src/main.tsx` mounts into. The rail and the screen list in
`src/shell/nav.ts` are the map of which is which.
