"""`untrack` — the other half of `track`, which did not exist until now.

THE GAP. The presentation gate reports a tracked file that is gone from disk
as a blocker and tells the reader to "restore the file, or asset_release it
from tracking". `release` releases a LOCK and has never removed a row, so the
second half of that sentence named an operation the product did not have.
Rows for intermediates that were converted and then deleted therefore sat on
the gate forever, and the only real remedy was hand-editing SQLite.

Found on the benchmark game with five at once: four .mp3 masters superseded by
the .ogg files the game actually preloads, and a stand-in .glb superseded by
the rigged character that shipped.

The two refusals matter more than the success. Untracking a file that is still
in the build is the one direction this gate cannot see afterwards.
"""
from __future__ import annotations

import pytest

from bgate_core import assets, db


@pytest.fixture()
def root(tmp_path):
    db.connect(tmp_path)
    return tmp_path


def _make(root, rel: str, body: bytes = b"x" * 64):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


# ---------------------------------------------------------------------------
# The thing it exists for
# ---------------------------------------------------------------------------

def test_a_tracked_file_that_is_gone_can_be_untracked(root):
    p = _make(root, "assets/audio/music/theme.mp3")
    assets.track(root, "assets/audio/music/theme.mp3")
    p.unlink()                      # converted to .ogg and deleted, as happens

    row = assets.untrack(root, "assets/audio/music/theme.mp3")
    assert row["path"] == "assets/audio/music/theme.mp3"
    with pytest.raises(LookupError):
        assets.get(root, "assets/audio/music/theme.mp3")


def test_untrack_returns_the_row_it_removed_so_it_can_be_restored(root):
    p = _make(root, "assets/x.glb", b"y" * 128)
    assets.track(root, "assets/x.glb")
    p.unlink()

    row = assets.untrack(root, "assets/x.glb")
    assert row["bytes"] == 128
    assert row["hash"]
    # and the row really is enough to put it back
    _make(root, "assets/x.glb", b"y" * 128)
    again = assets.track(root, "assets/x.glb")
    assert again["hash"] == row["hash"]


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------

def test_it_refuses_a_file_that_is_still_on_disk(root):
    _make(root, "assets/live.ogg")
    assets.track(root, "assets/live.ogg")

    with pytest.raises(ValueError, match="still on disk"):
        assets.untrack(root, "assets/live.ogg")
    # and it is still tracked afterwards — a refusal must not half-apply
    assert assets.get(root, "assets/live.ogg")["path"] == "assets/live.ogg"


def test_force_untracks_a_live_file_because_sometimes_that_is_meant(root):
    _make(root, "assets/live.ogg")
    assets.track(root, "assets/live.ogg")

    assets.untrack(root, "assets/live.ogg", force=True)
    with pytest.raises(LookupError):
        assets.get(root, "assets/live.ogg")


def test_it_refuses_a_locked_asset_even_with_force(root):
    p = _make(root, "assets/held.glb")
    assets.track(root, "assets/held.glb")
    assets.lock(root, "assets/held.glb", "art", owner="agent-1")
    p.unlink()

    # force is about the file being present, NOT about stealing a live claim:
    # removing the row turns the holder's release() into a LookupError.
    with pytest.raises(ValueError, match="locked"):
        assets.untrack(root, "assets/held.glb", force=True)
    assert assets.get(root, "assets/held.glb")["lock_seat"] == "art"


def test_untracking_something_never_tracked_refuses(root):
    with pytest.raises(LookupError):
        assets.untrack(root, "assets/nothing-here.png")


def test_waiters_are_cleared_with_the_row(root):
    """A waiter on a path nobody tracks any more can never be woken."""
    p = _make(root, "assets/queued.glb")
    assets.track(root, "assets/queued.glb")
    assets.lock(root, "assets/queued.glb", "art", owner="agent-1")
    try:
        assets.lock(root, "assets/queued.glb", "audio", owner="agent-2")
    except Exception:
        pass                        # the second seat queues or is refused
    assets.force_release(root, "assets/queued.glb")
    p.unlink()

    assets.untrack(root, "assets/queued.glb")
    left = db.connect(root).execute(
        "SELECT COUNT(*) AS n FROM asset_waiter WHERE asset_path = ?",
        ("assets/queued.glb",)).fetchone()["n"]
    assert left == 0


# ---------------------------------------------------------------------------
# delivered_but_unwired: a superseded artifact is not an orphan
# ---------------------------------------------------------------------------

def test_a_superseded_artifact_is_not_reported_as_an_orphan(root):
    """The gate offers two ways out and only one of them used to work.

    Its text is "wire it, or mark the artifact rejected if it is not going in".
    Marking it changed nothing, because the join never read status — so the
    second option did not clear the blocker and the row returned every run.

    Found on two raw synthesis sources whose processed siblings are what the
    game loads.
    """
    from bgate_core import artifacts

    _make(root, "assets/audio/hum_raw.wav")
    artifacts.register(root, "sfx_hum_raw", "assets/audio/hum_raw.wav",
                       producer="sfx_generate")
    assets.track(root, "assets/audio/hum_raw.wav")

    # nothing in the project references it, so it starts out an orphan
    before = assets.delivered_but_unwired(root)
    assert any("hum_raw" in r["path"] for r in before), before

    art = [a for a in artifacts.list_revisions(root)
           if a["logical_name"] == "sfx_hum_raw"][0]
    artifacts.review(root, art["id"], "superseded",
                     note="the processed hum.wav is what ships",
                     actor="a-human")

    after = assets.delivered_but_unwired(root)
    assert not any("hum_raw" in r["path"] for r in after), after


def test_an_ordinary_orphan_is_still_reported(root):
    """CONTROL: the filter must not swallow orphans that ARE a problem."""
    from bgate_core import artifacts

    _make(root, "assets/audio/forgotten.wav")
    artifacts.register(root, "sfx_forgotten", "assets/audio/forgotten.wav",
                       producer="sfx_generate")
    assets.track(root, "assets/audio/forgotten.wav")

    rows = assets.delivered_but_unwired(root)
    assert any("forgotten" in r["path"] for r in rows), rows
