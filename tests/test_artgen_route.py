"""The art route's two doors: the filename it writes to, and the references
it conditions on.

Both are reachable WITHOUT A SEAT — this is the dashboard's own loopback API,
not an MCP tool — so neither may accept a path. The MCP side can afford to
resolve-and-contain because an agent naming a file is a legitimate thing; here
a path is always someone reaching, and the tests below are the proof that
neither door lets one through.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import refs as _refs
from bgate_ui.app import app
from bgate_ui.routes.artgen import ART_DIR, _out_path


def _why(response) -> str:
    """The message out of whichever envelope the app wrapped it in."""
    body = response.json()
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("detail") or body)
    return str(body.get("detail") if isinstance(body, dict) else body)


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


class TestTheFilenameIsAName:
    """`filename` is joined onto .bgate_out/art and written to, so anything
    that can express a separator or a parent can express somewhere else on
    the disk."""

    @pytest.mark.parametrize("asked", [
        "../../x.png", "..\\..\\x.png", "/etc/passwd", "a/../../b.png",
    ])
    def test_traversal_lands_in_the_art_directory(self, root, asked):
        out = _out_path(root, asked)
        assert out.parent == (root / ART_DIR).resolve()

    @pytest.mark.parametrize("asked", ["", "..", ".png", "x;rm -rf.png",
                                       "a b.png", "‮x.png"])
    def test_a_name_that_is_not_plain_is_refused(self, root, asked):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            _out_path(root, asked)
        assert exc.value.status_code == 400

    def test_a_plain_name_passes_and_gains_its_extension(self, root):
        assert _out_path(root, "tommy").name == "tommy.png"
        assert _out_path(root, "a-b_c.1.png").name == "a-b_c.1.png"


class TestReferencesComeFromThePinTable:
    """refs.resolve() lets an existing path pass through untouched. That is
    right for an MCP tool and wrong here, so this route does not call it: a
    caller's name SELECTS a pin and the pin's own path is what is used."""

    def test_an_unpinned_name_is_refused(self, client):
        got = client.post("/api/art/generate",
                          json={"prompt": "a mug", "filename": "mug.png",
                                "refs": ["nothing-is-pinned"]})
        assert got.status_code == 400
        assert "not a pinned reference" in _why(got)

    def test_a_path_is_refused_even_when_the_file_exists(self, client, root):
        secret = root / "secret.png"
        secret.write_bytes(b"\x89PNG\r\n\x1a\n")
        got = client.post("/api/art/generate",
                          json={"prompt": "a mug", "filename": "mug.png",
                                "refs": [str(secret)]})
        assert got.status_code == 400, (
            "an existing path resolved as a reference — the route would "
            "condition on any file the server can read")

    def test_a_pinned_name_is_accepted(self, client, root):
        src = root / "anchor.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        pinned = _refs.pin(root, "hero", str(src))
        got = client.post("/api/art/generate",
                          json={"prompt": "a mug", "filename": "mug.png",
                                "refs": ["hero"]})
        assert got.status_code == 200, _why(got)
        assert got.json()["state"] == "queued"
        # And what it will generate against is the pin's own copy in
        # .bgate/refs, not the file the caller happened to pin from.
        assert _refs.get(root, "hero")["path"] == pinned["path"]
        assert str(src) != pinned["path"]
