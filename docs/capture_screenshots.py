"""Capture README screenshots of the running dashboard.

Drives the installed Edge via Playwright's `channel="msedge"` so nothing has to
be downloaded. The dashboard is a single-page app whose views are switched by
setWorkspace()/Studio.select()/SeatShell.select(), so each shot navigates in
page rather than by URL, waits for the view to actually paint, and only then
captures.
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

W, H = 1600, 1000


def shot(page, name, setup_js, wait=2200, fit=True):
    """Capture one view, sized to its content and downscaled for the repo.

    Two passes matter here. Some views are far shorter than the viewport (the
    Overview is about 630px), and a screenshot padded with 400px of empty
    canvas looks like a rendering bug rather than a compact page — so the
    viewport is resized to the content before shooting. And the capture is at
    2x device pixels for sharpness, which is 3200px wide; that gets halved
    afterwards, because the README never renders these above ~900px and a repo
    does not need 6MB of PNG.
    """
    page.set_viewport_size({"width": W, "height": H})
    page.evaluate(setup_js)
    page.wait_for_timeout(wait)

    if fit:
        h = page.evaluate("""(() => {
            const v = document.querySelector('.deck-view.active');
            const bar = document.querySelector('.statusbar');
            return Math.ceil((v ? v.getBoundingClientRect().height : 600)
                   + (bar ? bar.getBoundingClientRect().height : 60) + 96);
        })()""")
        h = max(560, min(H, int(h)))
        if h < H:
            page.set_viewport_size({"width": W, "height": h})
            page.wait_for_timeout(700)

    path = OUT / f"{name}.png"
    page.screenshot(path=str(path))

    im = Image.open(path)
    if im.width > W:
        im = im.resize((W, round(im.height * W / im.width)), Image.LANCZOS)
    im.convert("RGB").save(path, "PNG", optimize=True)
    print(f"  {name:24s} {path.stat().st_size / 1024:7.0f} KB")


SHOTS = [
    ("overview", "setWorkspace('overview', document.querySelector('.rail-item[data-view=\"overview\"]'))", 2600),
    ("agents", "setWorkspace('agents', document.querySelector('.rail-item[data-view=\"agents\"]'))", 2200),
    ("assets", "setWorkspace('assets', document.querySelector('.rail-item[data-view=\"assets\"]'))", 3200),
    ("atlas", "setWorkspace('atlas', document.querySelector('.rail-item[data-view=\"atlas\"]'))", 3000),
    ("seat-workspaces", """
        setWorkspace('seats', document.querySelector('.rail-item[data-view="seats"]'));
        setTimeout(() => { try { SeatShell.select('director'); } catch(e){} }, 600);
     """, 3400),
    ("seat-qa", """
        setWorkspace('seats', document.querySelector('.rail-item[data-view="seats"]'));
        setTimeout(() => { try { SeatShell.select('qa'); } catch(e){} }, 600);
     """, 3400),
    ("playtests", "setWorkspace('playtests', document.querySelector('.rail-item[data-view=\"playtests\"]'))", 2400),
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
        page.evaluate("setWorkspace('studio', document.querySelector('.rail-item[data-view=\"studio\"]'))")
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

        # World bible's lore graph is the one that shows off the data.
        shot(page, "world-bible", """
            setWorkspace('world', document.querySelector('.rail-item[data-view="world"]'));
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
             "setWorkspace('overview', document.querySelector('.rail-item[data-view=\"overview\"]'))", 2800)
        shot(page, "assets-light",
             "setWorkspace('assets', document.querySelector('.rail-item[data-view=\"assets\"]'))", 3200)

        browser.close()
    print(f"\nwrote to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
