"""ITEM 4: the gallery must not show dead files.

MEASURED: the dashboard card "image_sprites flyer x3" showed a gator
thumbnail from a `.bgate_out/sprites/flyer_*.png` written by a run whose
outputs were later discarded, because every revision of one logical name can
share ONE on-disk path and a later write silently changes what an OLDER
revision's row would show if read straight off `path`.
"""
from __future__ import annotations

from pathlib import Path

from bgate_core.store import artifacts


def _write(root, rel: str, data: bytes) -> Path:
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


class TestContentAddressedThumbnails:
    def test_each_revision_gets_its_own_immutable_copy(self, root):
        _write(root, "art/flyer.png", b"FIRST-BYTES")
        r1 = artifacts.register(root, "flyer", "art/flyer.png", producer="test")
        content_path_1 = r1["metadata"]["content_path"]
        assert (Path(root) / content_path_1).read_bytes() == b"FIRST-BYTES"

        # A second revision overwrites the SAME shared path...
        _write(root, "art/flyer.png", b"SECOND-BYTES")
        r2 = artifacts.register(root, "flyer", "art/flyer.png", producer="test")
        content_path_2 = r2["metadata"]["content_path"]

        # ...and revision 1's own pinned copy still reads the ORIGINAL bytes,
        # even though `path` on disk now holds revision 2's bytes.
        assert (Path(root) / content_path_1).read_bytes() == b"FIRST-BYTES"
        assert (Path(root) / content_path_2).read_bytes() == b"SECOND-BYTES"
        assert content_path_1 != content_path_2

    def test_is_current_tells_live_from_stale_without_touching_status(self, root):
        _write(root, "art/flyer.png", b"FIRST-BYTES")
        r1 = artifacts.register(root, "flyer", "art/flyer.png", producer="test")
        assert artifacts.is_current(root, r1) is True

        _write(root, "art/flyer.png", b"SECOND-BYTES")
        r2 = artifacts.register(root, "flyer", "art/flyer.png", producer="test")

        # r1 is now stale on disk - a LATER write happened - but its review
        # `status` is untouched: a pending tournament or approval over r1
        # must not be silently rewritten by the fact that r2 exists.
        r1_after = artifacts.get(root, r1["id"])
        assert r1_after["status"] == "candidate"
        assert artifacts.is_current(root, r1_after) is False
        assert artifacts.is_current(root, artifacts.get(root, r2["id"])) is True

    def test_identical_bytes_registered_twice_share_one_copy(self, root):
        _write(root, "art/a.png", b"SAME-BYTES")
        r1 = artifacts.register(root, "flyer2", "art/a.png", producer="test")
        _write(root, "art/a.png", b"SAME-BYTES")
        r2 = artifacts.register(root, "flyer2", "art/a.png", producer="test")
        assert r1["metadata"]["content_path"] == r2["metadata"]["content_path"]
