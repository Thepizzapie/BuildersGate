from __future__ import annotations

from bgate_core import artifacts, assets, queue


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

    artifacts.review(root, first["id"], "approved", "strong silhouette")
    image.write_bytes(b"candidate-two")
    second = artifacts.register(root, "hero", image, producer="image_edit")
    artifacts.review(root, second["id"], "approved", "cleaner palette")

    assert artifacts.get(root, first["id"])["status"] == "superseded"
    assert artifacts.get(root, second["id"])["status"] == "approved"
    assert artifacts.get(root, second["id"])["revision"] == 2


def test_rejection_keeps_case_law(root):
    image = root / "bad.png"
    image.write_bytes(b"bad")
    item = artifacts.register(root, "enemy", image, producer="image_generate")

    reviewed = artifacts.review(root, item["id"], "rejected", "too much texture")
    assert reviewed["status"] == "rejected"
    assert reviewed["review_note"] == "too much texture"
