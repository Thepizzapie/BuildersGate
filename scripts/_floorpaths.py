"""The two paths every floor-art script needs: this checkout, and the sandbox.

WHERE THE SANDBOX IS, ASKED FOR RATHER THAN HARDCODED.

This was an absolute path to one machine's Desktop, which is three separate
problems in one line: it only ran for the person who wrote it, it put a home
directory and an account name into a public repository, and the leak test that
guards against exactly that (tests/test_streamer.py) failed on main because of
it.

BGATE_CAST_PROJECT is the env var, --project is the flag where a script has one,
and the default is a sibling `bg-testbed` beside this checkout - which is where
it actually lives for the person who wrote it, so the convenience is kept
without the address.

IT LIVES HERE BECAUSE IT WAS COPIED FOUR TIMES. The cast scripts each carried
their own byte-identical `_sandbox()` under that same comment, and
gen_floor_layers.py and gen_floor_rooms.py carried the `Path.home()/Desktop`
form the comment describes as fixed - so setting BGATE_CAST_PROJECT moved four
scripts and silently did nothing for the other two.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def sandbox() -> Path:
    from os import environ
    asked = environ.get("BGATE_CAST_PROJECT", "").strip()
    if asked:
        return Path(asked).expanduser().resolve()
    return (REPO.parent / "bg-testbed").resolve()


# THE FLOOR'S ART AND AMBIENCE LIVE IN THEIR OWN PACKAGE. They were 30MB of
# PNG and MP3 under frontend/public that pyproject excluded from the wheel;
# now they are the `builders-gate-floor-assets` distribution, built from
# packaging/floor-assets. Every generator writes there.
FLOOR_ASSETS = REPO / "packaging" / "floor-assets" / "builders_gate_floor_assets"
FLOOR_IMG = FLOOR_ASSETS / "img"
FLOOR_AUDIO = FLOOR_ASSETS / "audio"
