"""A rejected or superseded revision is retired canon: refused to the
wiring tools, named in asset_verify while a scene still uses it."""
from __future__ import annotations

from bgate_core.board import canon
from bgate_core.store import artifacts, assets


def _file(root, rel: str, payload: bytes = b"png") -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    return rel


def test_reject_retires_the_file_and_the_gate_refuses_it(root):
    rel = _file(root, "assets/sprites/rock_v1.png", b"one")
    rev = artifacts.register(root, "rock", rel, producer="image_generate")
    assert artifacts.usable(root, rel)["ok"]
    artifacts.review(root, rev["id"], "rejected", "wrong style", actor="reviewer")
    row = canon.retired_match(root, rel)
    assert row and row["successor"] == ""
    verdict = artifacts.usable(root, rel)
    assert verdict["ok"] is False
    assert verdict["status"] == "retired"
    assert "rock_v1.png" in verdict["reason"]


def test_approving_a_replacement_names_it_as_successor_and_frees_the_live_file(root):
    old = _file(root, "assets/sprites/rock_v1.png", b"one")
    new = _file(root, "assets/sprites/rock_v2.png", b"two")
    first = artifacts.register(root, "rock", old)
    artifacts.review(root, first["id"], "approved", actor="reviewer")
    assert artifacts.usable(root, old)["ok"]
    second = artifacts.register(root, "rock", new)
    artifacts.review(root, second["id"], "approved", actor="reviewer")
    # v1 was superseded by the approval: retired with v2 as its successor
    row = canon.retired_match(root, old)
    assert row and row["successor"] == new
    assert artifacts.usable(root, old)["successor"] == new
    assert artifacts.usable(root, new)["ok"]
    assert artifacts.live_path(root, "rock") == new


def test_a_re_approved_path_is_usable_whatever_an_older_row_says(root):
    rel = _file(root, "assets/sprites/rock.png", b"one")
    first = artifacts.register(root, "rock", rel)
    artifacts.review(root, first["id"], "rejected", actor="reviewer")
    assert not artifacts.usable(root, rel)["ok"]
    (root / rel).write_bytes(b"two")
    second = artifacts.register(root, "rock", rel)
    artifacts.review(root, second["id"], "approved", actor="reviewer")
    assert artifacts.usable(root, rel)["ok"]
    assert canon.retired_match(root, rel) is None


def test_stale_wired_names_the_scene_still_using_a_rejected_file(root):
    rel = _file(root, "assets/sprites/rock_v1.png", b"one")
    rev = artifacts.register(root, "rock", rel)
    artifacts.review(root, rev["id"], "rejected", actor="reviewer")
    scene = root / "scenes" / "cave.tscn"
    scene.parent.mkdir(parents=True, exist_ok=True)
    scene.write_text('[ext_resource type="Texture2D" path="res://assets/sprites/rock_v1.png" id="1"]\n',
                     encoding="utf-8")
    rows = artifacts.stale_wired(root, root)
    assert [r["path"] for r in rows] == [rel]
    assert rows[0]["referenced_by"] == ["scenes/cave.tscn"]
    assert rows[0]["status"] == "rejected"
    # and the audit carries it
    report = assets.verify(root)
    assert report["counts"]["stale_wired"] == 1


def test_an_unregistered_hand_made_file_is_not_quarantined(root):
    rel = _file(root, "assets/sprites/hand_drawn.png", b"x")
    assert artifacts.usable(root, rel) == {
        "ok": True, "status": "", "path": rel, "successor": "", "reason": ""}
