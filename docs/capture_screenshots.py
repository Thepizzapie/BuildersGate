"""Capture README screenshots of the running dashboard.

Drives the installed Edge via Playwright's `channel="msedge"` so nothing has to
be downloaded. The dashboard is a single-page app whose views are switched by
setWorkspace()/Studio.select(), so each shot navigates in page rather than by
URL, waits for the view to paint, and only then captures.

THE SHELL IS REACT NOW, and two things here were written for the old one:

  * `.rail-item[data-view="x"]` no longer exists — the rail is
    frontend/src/shell. setWorkspace() survives as the deck switch and the
    React shell follows it, so a shot names the deck and nothing else. The dead
    selector returned null, which setWorkspace tolerated, so these kept working
    with a second argument that did nothing.
  * FITTING TO CONTENT IS WRONG for a full-height shell. The old decks were
    documents that could be shorter than the viewport; this is a 100vh grid
    with its own internal scrollers, so measuring the deck and shrinking the
    window to it cropped the rail and the footer away. Fixed viewport now.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

# Point at whatever dashboard is already running. The screenshots are only worth
# taking against a project with real content in it, so this deliberately does
# not start its own server against an empty directory.
BASE = os.environ.get("BGATE_URL", "http://127.0.0.1:7788")
OUT = Path(__file__).resolve().parent / "screenshots"

# The sprite editor shot needs a sheet that exists in whatever project is
# loaded; override for anything other than the project these were shot against.
SHEET = os.environ.get("BGATE_SHEET",
                       "game/assets/characters/compliance_drone_idle.png")

# Which brainstorm room the shot opens, matched against the text of its row in
# the rooms rail. Every room is titled "brainstorm", so the turn count is what
# actually distinguishes them.
ROOM = os.environ.get("BGATE_ROOM", "25 turns")

W, H = 1600, 1000


# Which shots to take. Empty means all of them; BGATE_ONLY="world-bible,agents"
# takes those two and leaves every other file on disk alone, which is what you
# want when some of the set is hand-taken and must not be overwritten.
ONLY = {n.strip() for n in os.environ.get("BGATE_ONLY", "").split(",") if n.strip()}


def wanted(name: str) -> bool:
    return not ONLY or name in ONLY


def shot(page, name, setup_js, wait=2200):
    """Capture one view and downscale it for the repo.

    Captured at 2x device pixels for sharpness, 3200px wide, and halved
    afterwards, because the README never renders these above ~900px and a repo
    does not need 6MB of PNG.
    """
    if not wanted(name):
        print(f"  {name:24s} skipped")
        return
    page.set_viewport_size({"width": W, "height": H})
    page.evaluate(setup_js)
    page.wait_for_timeout(wait)

    path = OUT / f"{name}.png"
    page.screenshot(path=str(path))

    im = Image.open(path)
    if im.width > W:
        im = im.resize((W, round(im.height * W / im.width)), Image.LANCZOS)
    im.convert("RGB").save(path, "PNG", optimize=True)
    print(f"  {name:24s} {path.stat().st_size / 1024:7.0f} KB")


SHOTS = [
    ("overview", "setWorkspace('overview')", 2600),
    ("agents", "setWorkspace('agents')", 2200),
    ("assets", "setWorkspace('assets')", 3200),
    ("atlas", "setWorkspace('atlas')", 3000),
    ("seat-workspaces", """
        setWorkspace('seats');
        setTimeout(() => { try { SeatShell.select('director'); } catch(e){} }, 600);
     """, 3400),
    ("seat-qa", """
        setWorkspace('seats');
        setTimeout(() => { try { SeatShell.select('qa'); } catch(e){} }, 600);
     """, 3400),
    ("playtests", "setWorkspace('playtests')", 2400),
    # THE ROOM. Its own screen since seats became joinable — the transcript,
    # the roster and the one door out are the three things the shot has to
    # show, and all three are only on this deck.
    #
    # WHICH ROOM IS NOT LEFT TO CHANCE. The deck opens whatever was last read,
    # which on this machine was a session about the tool rather than about the
    # game — a fine conversation and a poor advertisement. BGATE_ROOM picks the
    # room by a string in its rail row (the turn count is the stable one, since
    # every room is titled "brainstorm").
    ("brainstorm", """
        setWorkspace('brainstorm');
        setTimeout(() => { try {
          const want = "__ROOM__";
          const rows = [...document.querySelectorAll('.bg4-roomrow')];
          const pick = rows.find(r => r.textContent.includes(want)) || rows[0];
          if (pick) pick.click();
        } catch (e) {} }, 900);
     """.replace("__ROOM__", ROOM), 4200),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": W, "height": H},
                                device_scale_factor=2)   # crisp on retina/hidpi
        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3500)          # modules boot and first poll lands

        print("dark:")
        page.evaluate("setTheme('dark')")
        page.wait_for_timeout(500)
        for name, js, wait in SHOTS:
            shot(page, name, js, wait)

        # Studio flows — these need activate() to resolve the lazy modules.
        page.evaluate("setWorkspace('studio')")
        page.wait_for_timeout(900)
        page.evaluate("Studio.activate()")
        page.wait_for_timeout(1800)
        for flow, nm, wait in [("workflows", "studio-workflows", 2600),
                               ("agent", "studio-agent-flow", 3000)]:
            shot(page, nm, f"Studio.select('{flow}')", wait)

        # The two editors, with real content loaded.
        # Not an f-string: the body is JavaScript and full of braces.
        shot(page, "sprite-editor", """
            Studio.select('sprite');
            setTimeout(() => { try {
              SpriteEdit.open("__SHEET__");
              setTimeout(() => { try { SpriteEdit.setOnion('both'); SpriteEdit.setFrame(3);
                                       SpriteEdit.previewToggle(true); } catch(e){} }, 2200);
            } catch(e){} }, 500);
        """.replace("__SHEET__", SHEET), 6000)

        shot(page, "audio-lab", """
            Studio.select('audio');
            setTimeout(async () => { try {
              const l = await fetch('/api/audio/lab/list').then(r=>r.json());
              const s = (l.sounds||[]).find(x=>/music_title/.test(x.rel)) || (l.sounds||[])[0];
              AudioLab.open(s.rel);
            } catch(e){} }, 600);
        """, 6500)

        # Layers mode, with a second sound actually layered and offset — an
        # empty timeline shows nothing about what the mode is for.
        shot(page, "audio-layers", """
            (async () => { try {
              const l = await fetch('/api/audio/lab/list').then(r=>r.json());
              const b = (l.sounds||[]).find(x=>/music_combat/.test(x.rel))
                     || (l.sounds||[]).filter(x=>x.rel !== AudioLab.state.rel)[0];
              if (b) await AudioLab.addTrack(b.rel);
              await new Promise(r=>setTimeout(r,2000));
              if (AudioLab.state.tracks[0]) AudioLab.trackField(0,'offset_s',6.5);
              AudioLab.setMode('layers');
            } catch(e){} })();
        """, 7000)

        # SHUT THE EDITORS BEFORE MOVING ON. The sprite editor and the audio lab
        # are FULL-SCREEN overlays, not decks — setWorkspace() switches the deck
        # underneath and leaves the overlay covering it, so every shot after
        # this point came out as a picture of the audio lab. Measured: the
        # world-bible shot was the audio lab, and so were both light-ground
        # shots.
        page.evaluate("""(() => {
            try { AudioLab.close(); } catch (e) {}
            try { SpriteEdit.close(); } catch (e) {}
            try { Studio.select('workflows'); } catch (e) {}
        })()""")
        page.wait_for_timeout(1200)

        # World bible's lore graph is the one that shows off the data.
        shot(page, "world-bible", """
            setWorkspace('world');
            setTimeout(() => { try {
              const t = [...document.querySelectorAll('#world-subnav .seat-tab')]
                        .find(b => /lore/i.test(b.textContent));
              if (t) t.click();
            } catch(e){} }, 900);
        """, 5000)

        # One light-ground shot, because the theme switch is worth showing.
        print("light:")
        page.evaluate("setTheme('light')")
        page.wait_for_timeout(700)
        shot(page, "overview-light",
             "setWorkspace('overview')", 2800)
        shot(page, "assets-light",
             "setWorkspace('assets')", 3200)

        browser.close()
    print(f"\nwrote to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
