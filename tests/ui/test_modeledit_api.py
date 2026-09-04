"""The model editor's API — read-only over the mesh, read/write over the
sidecar. Mirrors test_spriteedit_api.py's shape: refusal is the interesting
behaviour (what must not be reachable, what must not be written) since a
browser is putting bytes on disk over HTTP.
"""
from __future__ import annotations

import base64
import inspect
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bgate_core.three_d import modelmap
from bgate_ui.app import app

# Not a real glTF binary — the server never parses geometry, only serves the
# bytes back and validates the sidecar, so a stand-in blob with the right
# extension exercises every code path a real .glb would.
FAKE_GLB = b"glTF" + b"\x02\x00\x00\x00" + b"\x00" * 32


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def game(root):
    """A Godot project at the root with one model in it."""
    (root / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="Test"\n',
        encoding="utf-8")
    models = root / "assets" / "models"
    models.mkdir(parents=True)
    (models / "hero.glb").write_bytes(FAKE_GLB)
    return root


MODEL = "assets/models/hero.glb"


# ---------------------------------------------------------------------------
# Opening / listing
# ---------------------------------------------------------------------------
def test_open_reports_paths_and_an_empty_sidecar(client, game):
    r = client.get("/api/model3d/open", params={"rel": MODEL})
    assert r.status_code == 200
    d = r.json()
    assert d["res_path"] == "res://assets/models/hero.glb"
    assert d["viewable"] is True
    # Versioned by mtime and size, so a rewritten file is a new URL.
    assert d["raw_url"].startswith("/api/model3d/raw/assets/models/hero.glb?v=")
    assert d["model"]["sockets"] == []
    assert "main_hand" in d["known_slots"]


def test_open_refuses_a_path_that_escapes_the_project(client, game):
    r = client.get("/api/model3d/open", params={"rel": "../../etc/passwd"})
    assert r.status_code in (403, 415, 404)


def test_only_known_3d_formats_open(client, game):
    (game / "note.txt").write_bytes(b"x")
    r = client.get("/api/model3d/open", params={"rel": "note.txt"})
    assert r.status_code == 415


def test_fbx_and_blend_are_listed_but_flagged_unviewable(client, game):
    (game / "assets" / "models" / "extra.fbx").write_bytes(b"x")
    d = client.get("/api/model3d/list").json()
    hit = next(m for m in d["models"] if m["rel"].endswith("extra.fbx"))
    assert hit["viewable"] is False
    open_resp = client.get("/api/model3d/open",
                           params={"rel": "assets/models/extra.fbx"})
    assert open_resp.json()["viewable"] is False


def test_list_finds_the_model_and_flags_it_unannotated(client, game):
    d = client.get("/api/model3d/list").json()
    hit = next(m for m in d["models"] if m["rel"] == MODEL)
    assert hit["annotated"] is False
    assert hit["ext"] == ".glb"


# ---------------------------------------------------------------------------
# Serving the raw bytes
# ---------------------------------------------------------------------------
def test_raw_serves_the_model_bytes(client, game):
    r = client.get(f"/api/model3d/raw/{MODEL}")
    assert r.status_code == 200
    assert r.content == FAKE_GLB
    assert r.headers["content-type"] == "model/gltf-binary"


def test_raw_serves_a_gltf_companion_beside_it(client, game):
    (game / "assets" / "models" / "buffer.bin").write_bytes(b"\x01\x02\x03")
    r = client.get("/api/model3d/raw/assets/models/buffer.bin")
    assert r.status_code == 200
    assert r.content == b"\x01\x02\x03"


def test_raw_refuses_a_disallowed_suffix(client, game):
    (game / "assets" / "models" / "secret.env").write_text("KEY=1")
    r = client.get("/api/model3d/raw/assets/models/secret.env")
    assert r.status_code == 415


def test_raw_refuses_a_path_that_escapes_the_project(client, game):
    r = client.get("/api/model3d/raw/../../etc/passwd")
    assert r.status_code in (403, 404, 415)


def test_raw_404s_a_missing_file_inside_the_project(client, game):
    r = client.get("/api/model3d/raw/assets/models/ghost.glb")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# The sidecar
# ---------------------------------------------------------------------------
def test_save_writes_the_sidecar_and_comes_back_on_open(client, game):
    payload = {
        "camera": {"position": [1, 2, 3], "target": [0, 0, 0], "fov": 45},
        "display": {"mode": "wireframe", "grid": False},
        "nodes": {"Sword_low": {"visible": True, "color": "#ff0000"}},
        "sockets": [{"name": "main_hand", "node": "Hand_R",
                     "position": [0.1, 0.2, 0.3], "rotation": [0, 90, 0],
                     "note": "grip"}],
    }
    r = client.post("/api/model3d/save", json={"rel": MODEL, "model": payload})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["sidecar"] == "assets/models/hero.model3d.json"
    assert (game / d["sidecar"]).is_file()

    reopened = client.get("/api/model3d/open", params={"rel": MODEL}).json()
    assert reopened["model"]["display"]["mode"] == "wireframe"
    assert reopened["model"]["sockets"][0]["name"] == "main_hand"
    assert reopened["model"]["nodes"]["Sword_low"]["color"] == "#ff0000"

    listed = client.get("/api/model3d/list").json()
    hit = next(m for m in listed["models"] if m["rel"] == MODEL)
    assert hit["annotated"] is True


def test_a_bad_display_mode_is_refused(client, game):
    r = client.post("/api/model3d/save",
                    json={"rel": MODEL, "model": {"display": {"mode": "chrome"}}})
    assert r.status_code == 400
    assert "display.mode" in r.text


def test_duplicate_socket_names_are_refused(client, game):
    payload = {"sockets": [{"name": "grip", "position": [0, 0, 0]},
                           {"name": "grip", "position": [1, 1, 1]}]}
    r = client.post("/api/model3d/save", json={"rel": MODEL, "model": payload})
    assert r.status_code == 400
    assert "duplicate socket" in r.text


def test_reset_deletes_the_sidecar(client, game):
    client.post("/api/model3d/save",
               json={"rel": MODEL, "model": {"notes": "wip"}})
    assert (game / "assets/models/hero.model3d.json").is_file()
    r = client.post("/api/model3d/reset", json={"rel": MODEL})
    assert r.status_code == 200
    assert r.json()["data"]["removed"] is True
    assert not (game / "assets/models/hero.model3d.json").is_file()


def test_sockets_share_the_same_slot_taxonomy_as_sprite_rigs(client, game):
    from bgate_core.art import rigmap
    assert modelmap.KNOWN_SLOTS == rigmap.KNOWN_SLOTS


# ---------------------------------------------------------------------------
# Snapshot / preview
# ---------------------------------------------------------------------------
def _png_b64(size=(64, 64), colour=(10, 20, 30, 255)) -> str:
    buf = io.BytesIO()
    Image.new("RGBA", size, colour).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_snapshot_saves_under_bgate_out_and_is_previewable(client, game):
    r = client.post("/api/model3d/snapshot",
                    json={"rel": MODEL,
                          "png": "data:image/png;base64," + _png_b64()})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["preview"].startswith(".bgate_out/model_previews/hero.")
    assert (game / d["preview"]).is_file()
    preview = client.get(d["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"


def test_snapshot_refuses_non_png_bytes(client, game):
    r = client.post("/api/model3d/snapshot",
                    json={"rel": MODEL,
                          "png": base64.b64encode(b"not a png").decode()})
    assert r.status_code == 400


def test_snapshot_refuses_garbage_base64(client, game):
    r = client.post("/api/model3d/snapshot",
                    json={"rel": MODEL, "png": "!!!not base64!!!"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# The UI actually ships this
# ---------------------------------------------------------------------------
from pathlib import Path  # noqa: E402

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "public"


def test_the_editor_is_loaded_by_the_shell():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'src="/static/modeledit.js"' in html, "modeledit.js is never loaded"
    assert (STATIC / "modeledit.js").is_file()


def test_the_editor_uses_the_shared_endpoints_not_invented_ones():
    js = (STATIC / "modeledit.js").read_text(encoding="utf-8")
    for path in ("/api/model3d/open", "/api/model3d/list", "/api/model3d/save",
                 "/api/model3d/snapshot", "/api/model3d/reset"):
        assert path in js, f"{path} not called from modeledit.js"


def test_three_js_is_vendored_not_fetched_from_a_cdn():
    vendor = STATIC / "vendor" / "three"
    assert (vendor / "build" / "three.module.min.js").is_file()
    assert (vendor / "LICENSE").is_file()
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "unpkg.com" not in html and "cdn." not in html.lower()


def test_a_quote_in_a_model_name_cannot_inject_python_into_blender():
    """A filename is not trusted input, and --python-expr is source code.

    `open_in_blender` builds a Blender `--python-expr` script with the model's
    path in it. That path used to be pasted into a RAW string literal, which
    cannot escape anything - so a single quote in the filename ended the literal
    and the rest of the name was executed as Python inside Blender, with the
    user's privileges.

    A quote is legal in a filename on Windows, macOS and Linux, and models
    arrive here downloaded from providers and generated by agents, so their
    names are as external as any request field. `_model` does not help: it
    refuses paths outside the project and unknown suffixes, and says nothing
    about the characters in the name.

    This asserts the generated script PARSES to only the calls it is meant to
    contain, which is the property that matters and the one a reader can check.
    """
    import ast

    from bgate_ui.routes import modeledit

    # CODE ONLY, NOT COMMENTS. The fix's own comment quotes the vulnerable line
    # to explain it, so a naive substring check over the source trips on the
    # explanation rather than on any real code.
    src = inspect.getsource(modeledit.model_open_in_blender)
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "filepath=r'" not in code, (
        "the path is being pasted into a raw string literal again; use repr()")
    assert "repr(path)" in code, "the path literal is no longer built by repr()"

    # The generated shape, with a name that closes the literal and runs code.
    evil = "/proj/x'); __import__('os').system('calc'); #.glb"
    script = ("import bpy\n"
              "def _go():\n"
              f" bpy.ops.import_scene.gltf(filepath={repr(evil)})\n")
    tree = ast.parse(script)
    called = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            called.append(fn.id if isinstance(fn, ast.Name) else
                          getattr(fn, "attr", ""))
    assert "system" not in called and "__import__" not in called, (
        f"the filename injected executable code: {called}")
    assert called == ["gltf"], called
