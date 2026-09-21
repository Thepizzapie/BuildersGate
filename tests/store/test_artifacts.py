from __future__ import annotations

from bgate_core.store import artifacts, assets
from bgate_core.board import queue


def test_artifact_revisions_preserve_provenance_and_supersede(root):
    image = root / ".bgate_out" / "art" / "hero.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"candidate-one")

    first = artifacts.register(
        root, "hero", image, producer="image_generate", model="gpt-image-1",
        prompt="hero idle", refs=["hero-ref"], metadata={"quality": "medium"})
    assert first["revision"] == 1
    assert first["status"] == "candidate"
    assert first["refs"] == ["hero-ref"]
    assert assets.get(root, image)["hash"] == first["hash"]


def test_workspace_groups_revisions_and_surfaces_review_evidence(root):
    image = root / "assets" / "hero.png"
    image.parent.mkdir()
    image.write_bytes(b"approved")
    approved = artifacts.register(
        root, "hero", image, producer="imagegen",
        refs=["hero-profile"], metadata={
            "profile": "hero-profile",
            "consistency": {"ok": True},
            "engine_import": {"ok": True},
        })
    artifacts.review(root, approved["id"], "approved")

    image.write_bytes(b"candidate")
    candidate = artifacts.register(
        root, "hero", image, producer="imagegen")
    work = queue.add(root, "art", "regenerate hero")
    assets.lock(
        root, image, "art", owner=f"item-{work['id']}",
        work_item_id=work["id"])

    group = artifacts.workspace(root)[0]
    assert group["logical_name"] == "hero"
    assert group["approved"]["id"] == approved["id"]
    assert group["candidates"][0]["id"] == candidate["id"]
    assert group["approved"]["profile"] == "hero-profile"
    assert group["approved"]["consistency"]["ok"] is True
    assert group["candidates"][0]["lock"]["owner"] == f"item-{work['id']}"
    assert group["candidates"][0]["lock"]["heartbeat_at"]

    # Approving the newer candidate supersedes the previously-approved revision
    # of the same logical name — the workspace's one-approved-at-a-time law.
    artifacts.review(root, candidate["id"], "approved", "cleaner palette")
    assert artifacts.get(root, approved["id"])["status"] == "superseded"
    assert artifacts.get(root, candidate["id"])["status"] == "approved"
    assert artifacts.get(root, candidate["id"])["revision"] == 2


def test_rejection_keeps_case_law(root):
    image = root / "bad.png"
    image.write_bytes(b"bad")
    item = artifacts.register(root, "enemy", image, producer="image_generate")

    reviewed = artifacts.review(root, item["id"], "rejected", "too much texture")
    assert reviewed["status"] == "rejected"
    assert reviewed["review_note"] == "too much texture"


def test_sweep_stale_moves_orphaned_sibling_not_the_live_file(root):
    # MEASURED (EXIT 67): a discarded run's loose file sat next to the live
    # sheet under the same logical name and the gallery thumbnailed it.
    out = root / ".bgate_out" / "sprites"
    out.mkdir(parents=True)
    sheet = out / "flyer_sheet.png"
    sheet.write_bytes(b"run-one-sheet")
    artifacts.register(root, "flyer", sheet, producer="image_sprites")

    # A discarded run's leftover copy of the SAME name sits in another
    # folder, never registered. Only an exact stem match is swept: a sibling
    # pose_*.png is another run's work in progress (image_sprites registers
    # each pose as it lands), and sweeping those emptied a live run once.
    orphan = out / "old" / "flyer.png"
    orphan.parent.mkdir()
    orphan.write_bytes(b"discarded-pose-frame")

    # A second, real revision of the same sheet — the run that actually shipped.
    sheet.write_bytes(b"run-two-sheet")
    second = artifacts.register(root, "flyer", sheet, producer="image_sprites")

    assert sheet.is_file()
    assert sheet.read_bytes() == b"run-two-sheet"
    assert not orphan.exists()

    stale_dir = root / ".bgate_out" / ".stale" / "flyer"
    moved = list(stale_dir.rglob("flyer.png"))
    assert len(moved) == 1
    assert moved[0].read_bytes() == b"discarded-pose-frame"
    assert artifacts.get(root, second["id"])["hash"] != ""
