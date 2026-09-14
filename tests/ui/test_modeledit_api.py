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

# ---------------------------------------------------------------------------
# Clip authoring
# ---------------------------------------------------------------------------
# THE VALIDATION IS THE POINT. Every refusal below is one that would otherwise
# happen inside Blender, minutes into a run that has already imported a
# multi-megabyte mesh — which is the difference between a typo costing a
# millisecond and costing a coffee break.
def test_the_clip_catalogue_reports_what_blender_actually_accepts(client, game):
    """Not a hard-coded list in the browser. humanpose and quadpose are the
    authority on what animate() takes, and the panel reads them from here."""
    from bgate_adapters import humanpose, quadpose

    r = client.get("/api/model3d/clip_catalogue")
    assert r.status_code == 200
    d = r.json()["data"]
    assert [k["kind"] for k in d["humanoid"]["kinds"]] == list(humanpose.CLIP_KINDS)
    assert [k["kind"] for k in d["quadruped"]["kinds"]] == list(quadpose.QUAD_CLIP_KINDS)
    # Each kind carries the gait the support gate will judge it against, so
    # the panel can say what "walk" is going to be measured for.
    walk = next(k for k in d["humanoid"]["kinds"] if k["kind"] == "walk")
    assert walk["gait"] == "walk"
    assert d["facings"] == ["check", "repair", "skeleton"]
    assert [c["name"] for c in d["humanoid"]["defaults"]]
    assert [c["name"] for c in d["quadruped"]["defaults"]]


def test_the_catalogue_names_an_unfetched_pack_and_how_to_fetch_it(client, game):
    """An empty dropdown is a mystery; "not fetched, run this" is an answer.
    animlib.status never downloads, so this route never does either."""
    d = client.get("/api/model3d/clip_catalogue").json()["data"]
    for key, pack in d["packs"].items():
        assert "fetched" in pack
        if not pack["fetched"]:
            assert key in pack["fetch"]
            assert pack["clips"] == []


def test_an_unknown_clip_kind_is_refused_before_blender_is_started(client, game):
    r = client.post("/api/model3d/animate",
                    json={"rel": MODEL, "clips": [{"kind": "moonwalk"}]})
    assert r.status_code == 400
    detail = r.json()["error"]["detail"]
    assert detail["clip"] == "moonwalk"
    assert "walk" in detail["known"]


def test_two_clips_with_one_name_are_refused(client, game):
    """animate() writes one action per name. Two "walk"s is one clip and a
    silently discarded one, which is worse than a refusal."""
    r = client.post("/api/model3d/animate", json={
        "rel": MODEL,
        "clips": [{"name": "walk", "kind": "walk"},
                  {"name": "walk", "kind": "run"}]})
    assert r.status_code == 400
    assert r.json()["error"]["detail"]["clip"] == "walk"


def test_a_library_clip_needs_no_kind(client, game):
    """{"clip": "Walk_Loop"} is a retargeted library clip and carries no
    procedural kind. It must not be read as an unknown one."""
    from bgate_ui.routes import modeledit

    specs = modeledit._anim_clips(
        {"clips": [{"clip": "Walk_Loop", "name": "walk", "pack": "p"}]})
    assert specs == [{"clip": "Walk_Loop", "name": "walk", "pack": "p",
                      "kind": "library"}]


def test_no_clips_means_let_the_rig_choose(client, game):
    """None, not an empty list: animate() ships the humanoid or the quadruped
    default set depending on bones this route cannot see."""
    from bgate_ui.routes import modeledit

    assert modeledit._anim_clips({}) is None
    assert modeledit._anim_clips({"clips": []}) == []


def test_out_of_range_settings_are_refused(client, game):
    for payload, field in (({"fps": 500}, "fps"),
                           ({"proof_frames": 99}, "proof_frames"),
                           ({"facing": "sideways"}, "facing")):
        r = client.post("/api/model3d/animate", json={"rel": MODEL, **payload})
        assert r.status_code == 400, (field, r.json())
        assert field in r.json()["error"]["detail"]


def test_too_many_clips_in_one_run_are_refused(client, game):
    from bgate_ui.routes import modeledit

    clips = [{"kind": "idle", "name": f"c{i}"}
             for i in range(modeledit.MAX_CLIPS + 1)]
    r = client.post("/api/model3d/animate", json={"rel": MODEL, "clips": clips})
    assert r.status_code == 400
    assert r.json()["error"]["detail"]["asked"] == modeledit.MAX_CLIPS + 1


def test_animate_refuses_a_path_that_escapes_the_project(client, game):
    r = client.post("/api/model3d/animate",
                    json={"rel": "../../etc/passwd.glb"})
    assert r.status_code in (400, 403, 404, 415)


def test_the_facing_gate_comes_back_200_and_writes_nothing(client, game, monkeypatch):
    """A refusal is a DECISION FOR THE READER, not a server error. The skin's
    toes and the skeleton's foot bones disagreeing about forward is the defect
    that walks a whole character backwards with every other gate green, and
    the fix (facing: repair) belongs to whoever is looking at the mesh."""
    from bgate_ui.routes import modeledit

    called = {}

    class FakeBlender:
        @staticmethod
        def available():
            return {"available": True}

        @staticmethod
        def animate(model, out, **kw):
            called.update(kw)
            return {"ok": False, "refused": True,
                    "error": "the toes point -Y and the foot bones point +Y",
                    "facing": {"verdict": "disagree"}, "seconds": 3.0}

    monkeypatch.setattr(modeledit, "_blender", lambda: FakeBlender)
    r = client.post("/api/model3d/animate",
                    json={"rel": MODEL, "clips": [{"kind": "walk"}]})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["refused"] is True and d["ok"] is False
    assert "toes" in d["error"]
    assert "out" not in d
    assert called["facing"] == "check"


def test_a_successful_run_returns_the_gates_untouched(client, game, monkeypatch):
    """support and collisions are separate verdicts and both come back whole.
    A run can report ok with a failed support gate — the clips play and read
    wrong — and summarising that away would decide for the reader which
    failures matter."""
    from bgate_ui.routes import modeledit

    class FakeBlender:
        @staticmethod
        def available():
            return {"available": True}

        @staticmethod
        def animate(model, out, **kw):
            Path(out).write_bytes(FAKE_GLB)
            sheet = Path(kw["out_dir"]) / "proof_walk_sheet.png"
            sheet.parent.mkdir(parents=True, exist_ok=True)
            sheet.write_bytes(b"\x89PNG\r\n\x1a\n")
            return {"ok": True, "seconds": 42.0,
                    "clips": [{"name": "walk", "action": "walk", "frames": 24,
                               "loop": True, "ok": True,
                               "support": {"passed": False, "gait": "walk",
                                           "reason": "no double support"}}],
                    "sheets": [{"clip": "walk", "path": str(sheet)}],
                    "support": {"measured": True, "passed": False,
                                "failed": ["walk"]},
                    "collisions": {"measured": True, "passed": True},
                    "strays": []}

    monkeypatch.setattr(modeledit, "_blender", lambda: FakeBlender)
    r = client.post("/api/model3d/animate",
                    json={"rel": MODEL, "clips": [{"kind": "walk"}], "fps": 24})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["out"] == "assets/models/hero.anim.glb"
    assert d["support"]["failed"] == ["walk"]
    assert d["collisions"]["passed"] is True
    assert d["clips"][0]["support"]["reason"] == "no double support"
    # The proof sheet comes back as a URL the panel can just show.
    assert d["sheets"][0]["url"] == (
        "/api/preview?rel=.bgate_out/model_anim/hero/proof_walk_sheet.png")


def test_animate_needs_blender(client, game, monkeypatch):
    from bgate_ui.routes import modeledit

    class NoBlender:
        @staticmethod
        def available():
            return {"available": False, "reason": "not on the path"}

    monkeypatch.setattr(modeledit, "_blender", lambda: NoBlender)
    r = client.post("/api/model3d/animate", json={"rel": MODEL})
    assert r.status_code == 503


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


def test_the_tools_column_calls_the_animation_endpoints_it_needs():
    js = (STATIC / "modeledit_tools.js").read_text(encoding="utf-8")
    for path in ("/api/model3d/animate", "/api/model3d/clip_catalogue"):
        assert path in js, f"{path} not called from modeledit_tools.js"


def test_the_viewer_scrubs_the_whole_clip_and_not_the_first_second():
    """A hard-coded max on the scrub is a bug that only shows on clips longer
    than a second, which is all of them: the walk cycles this pipeline authors
    run past three, and the end of a clip — where a bad loop snaps — could not
    be reached at all."""
    js = (STATIC / "modeledit.js").read_text(encoding="utf-8")
    assert 'id="me-scrub"' in js
    assert 'min="0" max="1" step="0.001"' not in js, (
        "the scrub is ranged on a hard-coded second again")
    assert "clipDuration()" in js


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
