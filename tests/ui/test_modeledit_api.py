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
from pathlib import Path
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
# The skeleton, as something you can move
# ---------------------------------------------------------------------------
def test_coincident_endpoints_group_into_one_joint():
    """AN ELBOW IS TWO BONE ENDS AT ONE COORDINATE. Move the forearm's head
    without the upper arm's tail and the limb comes apart when it bends: the
    exporter keeps the gap and the engine renders it. The joint is therefore
    the unit of editing, and this is the grouping that makes that true."""
    from bgate_ui.routes.modeledit import _joints

    bones = [
        {"name": "Hips", "parent": "", "head": [0, 1.0, 0], "tail": [0, 1.15, 0]},
        {"name": "Spine", "parent": "Hips", "head": [0, 1.15, 0], "tail": [0, 1.35, 0]},
        {"name": "UpperArm", "parent": "Spine", "head": [0.18, 1.35, 0], "tail": [0.45, 1.35, 0]},
        {"name": "LowerArm", "parent": "UpperArm", "head": [0.45, 1.35, 0], "tail": [0.7, 1.35, 0]},
    ]
    joints = _joints(bones)
    elbow = next(j for j in joints if j["position"] == [0.45, 1.35, 0.0])
    assert sorted((e["bone"], e["end"]) for e in elbow["ends"]) == [
        ("LowerArm", "head"), ("UpperArm", "tail")]
    # Named for the bone that STARTS there: an elbow is where the forearm begins.
    assert elbow["label"] == "LowerArm"
    assert elbow["leaf"] is False
    tip = next(j for j in joints if j["position"] == [0.7, 1.35, 0.0])
    assert tip["leaf"] is True and tip["label"].endswith("tip")


def test_endpoints_a_float_apart_are_still_one_joint():
    """A rig that survived a glTF round trip carries coincident endpoints back
    at float precision, not bit-identical. Grouping on equality would split
    every joint in the skeleton into two that drag apart."""
    from bgate_ui.routes.modeledit import _joints

    joints = _joints([
        {"name": "A", "head": [0, 0, 0], "tail": [0, 1.0, 0]},
        {"name": "B", "head": [0, 1.0000001, 0], "tail": [0, 2.0, 0]},
    ])
    shared = [j for j in joints if len(j["ends"]) == 2]
    assert len(shared) == 1, [j["ends"] for j in joints]


def test_a_bone_edit_must_change_something(client, game):
    r = client.post("/api/model3d/skeleton",
                    json={"rel": MODEL, "bones": {"Hips": {}}})
    assert r.status_code == 400
    assert r.json()["error"]["detail"]["bone"] == "Hips"


def test_a_nan_coordinate_is_refused(client, game):
    """NaN fails every comparison including its own, so a range check alone
    lets it through and Blender writes a corrupted armature."""
    import json as _json

    body = _json.dumps({"rel": MODEL,
                        "bones": {"Hips": {"head": [0, float("nan"), 0]}}})
    r = client.post("/api/model3d/skeleton", content=body,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "finite" in r.json()["error"]["message"]


def test_a_malformed_coordinate_is_refused(client, game):
    for point in ([0, 1], "here", [0, "up", 0], [0, 1, 2, 3]):
        r = client.post("/api/model3d/skeleton",
                        json={"rel": MODEL, "bones": {"Hips": {"head": point}}})
        assert r.status_code == 400, point


def test_an_empty_edit_is_refused(client, game):
    for payload in ({}, {"bones": {}}, {"bones": []}):
        r = client.post("/api/model3d/skeleton", json={"rel": MODEL, **payload})
        assert r.status_code == 400, payload


def test_a_drag_further_than_the_model_allows_is_refused(client, game):
    """The gizmo works in world units, and one slipped drag against a distant
    camera plane throws a wrist across the room. The limit is a fraction of
    the model's own height, so it means the same thing on a mug and a tower."""
    r = client.post("/api/model3d/skeleton", json={
        "rel": MODEL, "limit": 0.9,
        "bones": {"Hips": {"head": [0, 40.0, 0], "was": {"head": [0, 1.0, 0]}}}})
    assert r.status_code == 400
    detail = r.json()["error"]["detail"]
    assert detail["bone"] == "Hips" and detail["moved"] > 0.9


def test_a_move_inside_the_limit_reaches_blender(client, game, monkeypatch):
    from bgate_ui.routes import modeledit

    seen = {}

    class FakeBlender:
        _RIG_SOURCE = "# rig kit\n"

        @staticmethod
        def available():
            return {"available": True}

        @staticmethod
        def run_script(body, **kw):
            seen["export"] = kw.get("export_glb")
            Path(kw["export_glb"]).write_bytes(FAKE_GLB)
            return {"ok": True, "seconds": 9.0}

    monkeypatch.setattr(modeledit, "_blender", lambda: FakeBlender)
    monkeypatch.setattr(modeledit, "_run", lambda script, payload, timeout, export_glb=None: (
        seen.update(payload=payload, script=script),
        Path(export_glb).write_bytes(FAKE_GLB),
        {"ok": True, "armature": "Skeleton", "bones": 23, "seconds": 9.0,
         "applied": [{"name": "Hips"}], "missing": [], "max_move": 0.02,
         "rebound": True, "rigged": True, "bound_with": "ARMATURE_AUTO",
         "unweighted": 3, "unweighted_pct": 0.01, "attempts": []})[-1])
    r = client.post("/api/model3d/skeleton", json={
        "rel": MODEL, "limit": 0.9,
        "bones": {"Hips": {"head": [0, 1.02, 0], "was": {"head": [0, 1.0, 0]}}}})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["out"] == "assets/models/hero.bones.glb"
    assert d["rebound"] is True and d["rigged"] is True
    # `was` is a client-side guard rail, not something Blender should be told.
    assert seen["payload"]["bones"] == {"Hips": {"head": [0.0, 1.02, 0.0]}}
    assert seen["payload"]["rebind"] is True
    # The rig kit has to be PREPENDED: run_script only appends it, and only
    # when exporting, so a script calling bgate_rig_enter at its top level
    # would not see it.
    assert seen["script"].startswith("# rig kit")


def test_keeping_the_weights_is_an_explicit_choice(client, game, monkeypatch):
    """Weights were solved by heat around the joints that existed at bind
    time. Moving one and keeping them ships a character that tears at exactly
    that joint, so rebind defaults on and off has to be asked for."""
    from bgate_ui.routes import modeledit

    seen = {}

    class FakeBlender:
        _RIG_SOURCE = ""

        @staticmethod
        def available():
            return {"available": True}

    monkeypatch.setattr(modeledit, "_blender", lambda: FakeBlender)
    monkeypatch.setattr(modeledit, "_run", lambda script, payload, timeout, export_glb=None: (
        seen.update(payload=payload),
        Path(export_glb).write_bytes(FAKE_GLB),
        {"ok": True, "rebound": False, "rigged": None, "applied": [],
         "missing": [], "max_move": 0.0, "bones": 23})[-1])

    client.post("/api/model3d/skeleton",
                json={"rel": MODEL, "bones": {"Hips": {"roll": 0.1}}})
    assert seen["payload"]["rebind"] is True

    r = client.post("/api/model3d/skeleton", json={
        "rel": MODEL, "rebind": False, "bones": {"Hips": {"roll": 0.1}}})
    assert seen["payload"]["rebind"] is False
    # `rigged` is None, not False: nothing was measured, and saying "not
    # rigged" would be a verdict this run never reached.
    assert r.json()["data"]["rigged"] is None


def test_a_failed_edit_leaves_no_half_written_export(client, game, monkeypatch):
    """The runner exports whatever the script left behind, refusal or not, and
    a .glb on disk after a failed run reads as delivered work."""
    from bgate_ui.routes import modeledit

    class FakeBlender:
        _RIG_SOURCE = ""

        @staticmethod
        def available():
            return {"available": True}

    def boom(script, payload, timeout, export_glb=None):
        Path(export_glb).write_bytes(b"half a mesh")
        return {"ok": False, "error": "Hips would be 0.000001 m long"}

    monkeypatch.setattr(modeledit, "_blender", lambda: FakeBlender)
    monkeypatch.setattr(modeledit, "_run", boom)
    r = client.post("/api/model3d/skeleton",
                    json={"rel": MODEL, "bones": {"Hips": {"roll": 0.1}}})
    assert r.status_code == 502
    assert not (game / "assets" / "models" / "hero.bones.glb").exists()


def test_the_skeleton_read_needs_blender(client, game, monkeypatch):
    from bgate_ui.routes import modeledit

    class NoBlender:
        _RIG_SOURCE = ""

        @staticmethod
        def available():
            return {"available": False, "reason": "not on the path"}

    monkeypatch.setattr(modeledit, "_blender", lambda: NoBlender)
    r = client.get("/api/model3d/skeleton", params={"rel": MODEL})
    assert r.status_code == 503


def test_the_skeleton_read_reports_joints_and_a_move_limit(client, game, monkeypatch):
    from bgate_ui.routes import modeledit

    class FakeBlender:
        _RIG_SOURCE = ""

        @staticmethod
        def available():
            return {"available": True}

    monkeypatch.setattr(modeledit, "_blender", lambda: FakeBlender)
    monkeypatch.setattr(modeledit, "_run", lambda *a, **k: {
        "ok": True, "seconds": 2.0, "meshes": 1, "measure": {"height": 1.8},
        "armatures": [{"name": "Skeleton", "at_origin": True, "bones": [
            {"name": "Hips", "parent": "", "head": [0, 1.0, 0],
             "tail": [0, 1.15, 0], "influences": 900},
            {"name": "Spine", "parent": "Hips", "head": [0, 1.15, 0],
             "tail": [0, 1.35, 0], "influences": 0}]}]})
    r = client.get("/api/model3d/skeleton", params={"rel": MODEL})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["height"] == 1.8
    assert d["max_move"] == round(1.8 * modeledit.MAX_JOINT_MOVE, 4)
    joints = d["armatures"][0]["joints"]
    assert len(joints) == 3
    waist = next(j for j in joints if j["label"] == "Spine")
    assert len(waist["ends"]) == 2
    # A bone nothing is weighted to is the one worth seeing, so the count is
    # reported per bone rather than rolled into a total.
    assert d["armatures"][0]["bones"][1]["influences"] == 0


# ---------------------------------------------------------------------------
# Weight repair
# ---------------------------------------------------------------------------
def _from_script(*names):
    """Pull functions out of the shipped Blender source and run them here.

    THE SOURCE UNDER TEST IS THE SOURCE THAT SHIPS. The island walk is pure
    graph code and the segment distance is pure arithmetic; both are wrong in
    ways no stubbed round trip would catch, and neither needs bpy. Copying
    them into the test would prove the copy.
    """
    import ast
    import textwrap

    from bgate_ui.routes import modeledit

    tree = ast.parse(textwrap.dedent(modeledit._WEIGHTS_REPAIR_BODY))
    wanted = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(wanted) == len(names), [n.name for n in wanted]
    scope: dict = {}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<script>", "exec"),
         scope)
    return [scope[n] for n in names]


class _Vec3:
    """Enough mathutils.Vector for the distance function."""

    def __init__(self, x, y, z):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __sub__(self, o):
        return _Vec3(self.x - o.x, self.y - o.y, self.z - o.z)

    def __add__(self, o):
        return _Vec3(self.x + o.x, self.y + o.y, self.z + o.z)

    def __mul__(self, k):
        return _Vec3(self.x * k, self.y * k, self.z * k)

    def dot(self, o):
        return self.x * o.x + self.y * o.y + self.z * o.z

    @property
    def length_squared(self):
        return self.dot(self)

    @property
    def length(self):
        return self.length_squared ** 0.5


class _Mesh:
    def __init__(self, edges):
        self.data = type("D", (), {"edges": [type("E", (), {"vertices": e})()
                                             for e in edges]})()


def test_islands_walk_the_mesh_edges_not_the_vertex_list():
    """Two patches of one bone's paint with no edge between them are two
    islands, however close their indices are. Index adjacency would call a
    thigh and the OTHER thigh one region and report the bleed as clean."""
    (islands,) = _from_script("_islands")
    # 0-1-2 joined, 7-8 joined, nothing between them
    mesh = _Mesh([(0, 1), (1, 2), (7, 8), (2, 9), (9, 0)])
    parts = islands(mesh, {0, 1, 2, 7, 8})
    assert [len(p) for p in parts] == [3, 2], parts
    assert sorted(parts[0]) == [0, 1, 2]
    assert sorted(parts[1]) == [7, 8]


def test_an_island_walk_ignores_edges_leaving_the_member_set():
    """Edge 2-9 exists in the mesh and 9 is not weighted to this bone. Walking
    it would drag unrelated geometry into the bone's region and hide a split."""
    (islands,) = _from_script("_islands")
    mesh = _Mesh([(0, 1), (1, 2), (2, 9), (9, 8), (8, 7)])
    parts = islands(mesh, {0, 1, 2, 7, 8})
    assert [sorted(p) for p in parts] == [[0, 1, 2], [7, 8]]


def test_a_lone_vertex_is_its_own_island():
    (islands,) = _from_script("_islands")
    parts = islands(_Mesh([(0, 1)]), {0, 1, 5})
    assert [len(p) for p in parts] == [2, 1]


def test_islands_are_largest_first_because_the_largest_one_is_kept():
    (islands,) = _from_script("_islands")
    mesh = _Mesh([(0, 1), (4, 5), (5, 6), (6, 7)])
    parts = islands(mesh, {0, 1, 4, 5, 6, 7})
    assert len(parts[0]) == 4 and len(parts[1]) == 2


def test_the_nearest_bone_is_measured_to_the_segment_not_the_joint():
    """A vertex beside the middle of a thigh is nearest THAT thigh, even when
    the other thigh's hip joint is closer than this thigh's own hip. Measuring
    to joints would send the mid-thigh strays to the wrong leg."""
    (distance,) = _from_script("_seg_distance")
    point = _Vec3(0.10, 0.70, 0.0)
    left = distance(point, _Vec3(0.09, 0.96, 0), _Vec3(0.09, 0.52, 0))
    right = distance(point, _Vec3(-0.09, 0.96, 0), _Vec3(-0.09, 0.52, 0))
    assert left < right
    assert abs(left - 0.01) < 1e-6


def test_a_point_past_the_end_of_a_bone_clamps_to_its_tip():
    (distance,) = _from_script("_seg_distance")
    # Straight above the head of an upward bone: the nearest point on the
    # segment is the head, not an extrapolation of the line.
    d = distance(_Vec3(0, 2.0, 0), _Vec3(0, 1.0, 0), _Vec3(0, 1.5, 0))
    assert abs(d - 0.5) < 1e-9


def test_a_zero_length_bone_does_not_divide_by_zero(client, game):
    (distance,) = _from_script("_seg_distance")
    d = distance(_Vec3(0, 1, 0), _Vec3(0, 0, 0), _Vec3(0, 0, 0))
    assert abs(d - 1.0) < 1e-9


def test_repair_settings_are_validated_before_blender(client, game):
    for payload, field in (({"mode": "paint"}, "mode"),
                           ({"smooth": 9}, "smooth"),
                           ({"threshold": 2.0}, "threshold"),
                           ({"min_bleed": 0}, "min_bleed")):
        r = client.post("/api/model3d/weights/repair",
                        json={"rel": MODEL, **payload})
        assert r.status_code == 400, (field, r.json())
        assert field in r.json()["error"]["detail"]


def test_a_zero_is_a_value_and_not_an_absence(client, game):
    """`int(payload.get(k) or default)` reads naturally and is wrong for every
    field whose zero means something. min_bleed 0 became 3, threshold 0 became
    0.02 and fps 0 became 30, so the one input the bound check exists to
    refuse got a silent substitution instead of a 400."""
    r = client.post("/api/model3d/weights/repair",
                    json={"rel": MODEL, "min_bleed": 0})
    assert r.status_code == 400 and "min_bleed" in r.json()["error"]["detail"]

    r = client.post("/api/model3d/weights/repair",
                    json={"rel": MODEL, "threshold": 0})
    assert r.status_code == 400 and "threshold" in r.json()["error"]["detail"]

    r = client.post("/api/model3d/animate", json={"rel": MODEL, "fps": 0})
    assert r.status_code == 400 and "fps" in r.json()["error"]["detail"]

    # And a field that is genuinely absent still gets its default.
    from bgate_ui.routes import modeledit
    assert modeledit._number({}, "fps", 30) == 30
    assert modeledit._number({"fps": None}, "fps", 30) == 30
    assert modeledit._number({"fps": 0}, "fps", 30) == 0


def test_repair_refuses_a_bone_list_that_is_not_names(client, game):
    r = client.post("/api/model3d/weights/repair",
                    json={"rel": MODEL, "bones": [1, 2]})
    assert r.status_code == 400


def test_a_repair_that_found_nothing_writes_no_file(client, game, monkeypatch):
    """A near-identical copy of the mesh left on disk is a second source of
    truth somebody adopts by accident. Nothing fixed, nothing written."""
    from bgate_ui.routes import modeledit

    class FakeBlender:
        _RIG_SOURCE = ""

        @staticmethod
        def available():
            return {"available": True}

    def run(script, payload, timeout, export_glb=None):
        Path(export_glb).write_bytes(FAKE_GLB)
        return {"ok": True, "fixed": [], "skipped": [], "seconds": 12.0}

    monkeypatch.setattr(modeledit, "_blender", lambda: FakeBlender)
    monkeypatch.setattr(modeledit, "_run", run)
    r = client.post("/api/model3d/weights/repair", json={"rel": MODEL})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["out"] is None and d["bones_repaired"] == 0
    assert "nothing was written" in d["note"]
    assert not (game / "assets" / "models" / "hero.weights.glb").exists()


def test_a_repair_reports_where_every_stray_went(client, game, monkeypatch):
    from bgate_ui.routes import modeledit

    class FakeBlender:
        _RIG_SOURCE = ""

        @staticmethod
        def available():
            return {"available": True}

    def run(script, payload, timeout, export_glb=None):
        Path(export_glb).write_bytes(FAKE_GLB)
        return {"ok": True, "seconds": 31.0, "vertices_moved": 214,
                "bones_repaired": 1, "smoothed": 428, "smooth_passes": 2,
                "unweighted": 0, "unweighted_pct": 0.0,
                "fixed": [{"bone": "LeftUpperLeg", "mesh": "Body",
                           "islands_before": 2, "shells": 1, "stray": 214,
                           "moved_to": {"RightUpperLeg": 214}}],
                "skipped": []}

    monkeypatch.setattr(modeledit, "_blender", lambda: FakeBlender)
    monkeypatch.setattr(modeledit, "_run", run)
    r = client.post("/api/model3d/weights/repair",
                    json={"rel": MODEL, "smooth": 2})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["out"] == "assets/models/hero.weights.glb"
    assert d["fixed"][0]["moved_to"] == {"RightUpperLeg": 214}
    assert d["vertices_moved"] == 214 and d["smooth_passes"] == 2


# ---------------------------------------------------------------------------
# Posed clips: the keys a viewport hands back
# ---------------------------------------------------------------------------
def _quat(axis, degrees):
    import math

    half = math.radians(degrees) / 2.0
    s = math.sin(half)
    return (math.cos(half), axis[0] * s, axis[1] * s, axis[2] * s)


def test_a_posed_clip_arcs_between_its_keys_and_leaves_the_rest_at_rest():
    """The whole point of the kind: rotate one bone, key it, and get a track
    that goes there and comes back with nothing else moving."""
    import math

    from bgate_adapters import humanpose as hp

    rig = hp.RigFrame(**hp.canonical_rig())
    poses, notes = hp.posed_clip(rig, [
        {"t": 0.0, "bones": {}},
        {"t": 0.5, "bones": {"LeftLowerArm": _quat((1, 0, 0), 90)}},
        {"t": 1.0, "bones": {}}], fps=30)
    assert notes["kind"] == "bones" and notes["frames"] == 31
    assert notes["posed_bones"] == ["LeftLowerArm"]
    track = hp.bake(rig, poses)["rotations"]
    arm = track["LeftLowerArm"]
    assert abs(arm[0][0] - 1.0) < 1e-6                      # rest at the start
    assert abs(arm[15][0] - math.cos(math.radians(45))) < 1e-3   # 90 at the peak
    assert abs(arm[-1][0] - 1.0) < 1e-6                     # rest at the end
    # EVERY frame carries EVERY bone, identity where nothing was said: a
    # channel that appears mid-clip is a pop.
    assert all(abs(q[0] - 1.0) < 1e-9 for q in track["RightLowerArm"])
    assert len(track["RightLowerArm"]) == notes["frames"]


def test_a_key_is_a_whole_pose_so_an_omitted_bone_returns_to_rest():
    """The other reading, holding the previous key's value, makes "what is
    this bone doing at t=2" a question you answer by scanning backwards."""
    from bgate_adapters import humanpose as hp

    rig = hp.RigFrame(**hp.canonical_rig())
    poses, _ = hp.posed_clip(rig, [
        {"t": 0.0, "bones": {"LeftLowerArm": _quat((1, 0, 0), 90)}},
        {"t": 1.0, "bones": {"RightLowerArm": _quat((1, 0, 0), 90)}}], fps=10)
    end = hp.bake(rig, poses)["rotations"]["LeftLowerArm"][-1]
    assert abs(end[0] - 1.0) < 1e-6, end


def test_a_sign_flipped_key_does_not_slerp_the_long_way_round():
    """q and -q are the same rotation. Slerped naively the bone takes a full
    turn to reach somewhere it was already next to, and correcting the SAMPLES
    afterwards cannot undo an interpolation that already went the wrong way."""
    from bgate_adapters import humanpose as hp

    rig = hp.RigFrame(**hp.canonical_rig())
    q = _quat((1, 0, 0), 90)
    poses, _ = hp.posed_clip(rig, [
        {"t": 0.0, "bones": {"LeftLowerArm": q}},
        {"t": 1.0, "bones": {"LeftLowerArm": tuple(-c for c in q)}}], fps=30)
    track = hp.bake(rig, poses)["rotations"]["LeftLowerArm"]
    worst = max(abs(track[i][0] - track[i - 1][0]) for i in range(1, len(track)))
    assert worst < 0.01, f"the bone travelled: largest step {worst}"


def test_an_unnormalised_quaternion_is_normalised_not_refused():
    from bgate_adapters import humanpose as hp

    rig = hp.RigFrame(**hp.canonical_rig())
    poses, _ = hp.posed_clip(
        rig, [{"t": 0.0, "bones": {"LeftLowerArm": (2.0, 0.0, 0.0, 0.0)}},
              {"t": 0.5, "bones": {}}], fps=10)
    first = hp.bake(rig, poses)["rotations"]["LeftLowerArm"][0]
    assert abs(sum(c * c for c in first) - 1.0) < 1e-6


def test_a_posed_clip_closes_its_loop():
    from bgate_adapters import humanpose as hp

    rig = hp.RigFrame(**hp.canonical_rig())
    _, notes = hp.posed_clip(rig, [
        {"t": 0.0, "bones": {}},
        {"t": 1.0, "bones": {"LeftLowerArm": _quat((1, 0, 0), 40)}}],
        fps=10, loop=True)
    assert notes["keys"] == 3 and notes["loop"] is True


def test_a_bone_the_rig_does_not_have_is_ignored_not_fatal():
    """A browser sends the skeleton it loaded; a rig re-fitted since then can
    legitimately have lost a bone, and one stale name must not sink the clip."""
    from bgate_adapters import humanpose as hp

    rig = hp.RigFrame(**hp.canonical_rig())
    _, notes = hp.posed_clip(rig, [
        {"t": 0.0, "bones": {"NoSuchBone": _quat((1, 0, 0), 30),
                             "LeftLowerArm": _quat((1, 0, 0), 30)}},
        {"t": 0.5, "bones": {}}], fps=10)
    assert notes["posed_bones"] == ["LeftLowerArm"]


def test_the_new_kind_is_reachable_through_build_clip():
    from bgate_adapters import humanpose as hp

    assert "bones" in hp.CLIP_KINDS
    rig = hp.RigFrame(**hp.canonical_rig())
    _, notes = hp.build_clip(rig, {"name": "point", "kind": "bones", "keys": [
        {"t": 0.0, "bones": {}},
        {"t": 0.4, "bones": {"LeftLowerArm": _quat((1, 0, 0), 30)}}]}, fps=24)
    assert notes["kind"] == "bones" and notes["name"] == "point"


def test_posed_keys_are_bounded_because_a_browser_chose_them(client, game):
    from bgate_ui.routes import modeledit

    too_many = [{"t": i * 0.01, "bones": {}}
                for i in range(modeledit.MAX_POSE_KEYS + 1)]
    r = client.post("/api/model3d/animate", json={
        "rel": MODEL, "clips": [{"name": "a", "kind": "bones", "keys": too_many}]})
    assert r.status_code == 400
    assert r.json()["error"]["detail"]["asked"] == modeledit.MAX_POSE_KEYS + 1


def test_a_posed_clip_needs_keys(client, game):
    for spec in ({"name": "a", "kind": "bones"},
                 {"name": "a", "kind": "bones", "keys": []},
                 {"name": "a", "kind": "bones", "keys": "soon"}):
        r = client.post("/api/model3d/animate",
                        json={"rel": MODEL, "clips": [spec]})
        assert r.status_code == 400, spec


def test_a_broken_quaternion_is_refused_before_blender(client, game):
    """A zero quaternion has no rotation to normalise toward and a NaN passes
    every bound check. Both reach Blender as a bone that vanishes."""
    import json as _json

    bad = [([1, 0, 0], "four numbers"), ([0, 0, 0, 0], "zero"),
           (["a", 0, 0, 1], "four numbers")]
    for quat, _why in bad:
        r = client.post("/api/model3d/animate", json={"rel": MODEL, "clips": [
            {"name": "a", "kind": "bones",
             "keys": [{"t": 0, "bones": {"Hips": quat}}]}]})
        assert r.status_code == 400, quat
        assert r.json()["error"]["detail"]["clip"] == "a"

    body = _json.dumps({"rel": MODEL, "clips": [
        {"name": "a", "kind": "bones",
         "keys": [{"t": 0, "bones": {"Hips": [float("nan"), 0, 0, 1]}}]}]})
    r = client.post("/api/model3d/animate", content=body,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and "finite" in r.json()["error"]["message"]


def test_a_key_outside_the_clip_window_is_refused(client, game):
    for when in (-1.0, 100000.0):
        r = client.post("/api/model3d/animate", json={"rel": MODEL, "clips": [
            {"name": "a", "kind": "bones",
             "keys": [{"t": when, "bones": {}}]}]})
        assert r.status_code == 400, when


def test_a_valid_posed_clip_reaches_blender_intact(client, game, monkeypatch):
    """The exact payload the browser posts after posing a bone and keying it."""
    from bgate_ui.routes import modeledit

    seen = {}

    class FakeBlender:
        _RIG_SOURCE = ""

        @staticmethod
        def available():
            return {"available": True}

        @staticmethod
        def animate(model, out, **kw):
            seen.update(kw)
            Path(out).write_bytes(FAKE_GLB)
            return {"ok": True, "clips": [{"name": "point", "ok": True}],
                    "support": {}, "collisions": {}, "sheets": [], "seconds": 1}

    monkeypatch.setattr(modeledit, "_blender", lambda: FakeBlender)
    r = client.post("/api/model3d/animate", json={"rel": MODEL, "fps": 24, "clips": [
        {"name": "point", "kind": "bones", "loop": False, "keys": [
            {"t": 0, "bones": {"Hips": [1, 0, 0, 0], "Spine": [1, 0, 0, 0]}},
            {"t": 0.5, "bones": {"Hips": [1, 0, 0, 0],
                                 "Spine": [0.955336, 0, 0, 0.29552]}}]}]})
    assert r.status_code == 200, r.json()
    clip = seen["clips"][0]
    assert clip["kind"] == "bones" and len(clip["keys"]) == 2
    assert clip["keys"][1]["bones"]["Spine"] == [0.955336, 0.0, 0.0, 0.29552]


# ---------------------------------------------------------------------------
# The live Blender bridge
# ---------------------------------------------------------------------------
class _FakeLive:
    """A live Blender that is not there, or is. Set `answers` per call."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def available(self, port=9876, **kw):
        self.calls.append(("available", port, None))
        return self.answers.get("available",
                                {"available": False, "port": port})

    def run(self, script, **kw):
        self.calls.append(("run", kw.get("port"), script))
        got = self.answers.get("run", {"ok": True, "result": {}})
        if callable(got):
            return got(script, **kw)
        return got

    def export_glb(self, out, **kw):
        self.calls.append(("export", kw.get("port"), out))
        got = self.answers.get("export")
        if callable(got):
            return got(out, **kw)
        return got or {"ok": False, "error": "nothing"}

    def view(self, out, **kw):
        self.calls.append(("view", kw.get("port"), out))
        got = self.answers.get("view")
        if callable(got):
            return got(out, **kw)
        return got or {"ok": False, "error": "nothing"}

    def reset(self, **kw):
        self.calls.append(("reset", kw.get("port"), None))
        return self.answers.get("reset", {"ok": True})


def test_nothing_listening_is_a_state_and_not_an_error(client, game, monkeypatch):
    """A machine without the add-on running is the normal case. A 503 for it
    would make an idle panel look like a broken one."""
    from bgate_ui.routes import modeledit

    monkeypatch.setattr(modeledit, "_live", lambda: _FakeLive())
    r = client.get("/api/model3d/live", params={"port": 9876})
    assert r.status_code == 200
    assert r.json()["data"]["available"] is False


def test_a_live_probe_that_throws_is_still_a_state(client, game, monkeypatch):
    """The adapter opens a socket; a refused connection raises rather than
    returning, and the panel still needs an answer."""
    from bgate_ui.routes import modeledit

    class Exploding:
        @staticmethod
        def available(**kw):
            raise OSError("connection refused")

    monkeypatch.setattr(modeledit, "_live", lambda: Exploding)
    r = client.get("/api/model3d/live")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["available"] is False and "OSError" in d["error"]


def test_the_port_is_bounded(client, game):
    r = client.get("/api/model3d/live", params={"port": 99999})
    assert r.status_code == 400
    for payload in ({"port": 0}, {"port": -1}, {"port": 70000}):
        r = client.post("/api/model3d/live/reset", json=payload)
        assert r.status_code == 400, payload


def test_a_live_script_must_be_a_non_empty_string_of_bounded_size(client, game):
    from bgate_ui.routes import modeledit

    for script in (None, "", "   ", 42, []):
        r = client.post("/api/model3d/live/run", json={"script": script})
        assert r.status_code == 400, script
    r = client.post("/api/model3d/live/run",
                    json={"script": "x" * (modeledit.LIVE_MAX_SCRIPT + 1)})
    assert r.status_code == 400
    assert r.json()["error"]["detail"]["bytes"] > modeledit.LIVE_MAX_SCRIPT


def test_a_quote_in_a_model_name_cannot_inject_python_into_the_live_blender(
        client, game, monkeypatch):
    """Same lesson as open_in_blender, and the same fix. Models arrive
    downloaded from providers and generated by agents, so their names are as
    external as any request field; a single quote ends a raw string literal
    and the rest of the name runs as Python inside the user's Blender."""
    import ast

    from bgate_ui.routes import modeledit

    evil = "hero'); __import__('os').system('calc'); #.glb"
    models = game / "assets" / "models"
    (models / evil).write_bytes(FAKE_GLB)

    fake = _FakeLive(run={"ok": True, "result": {"imported": ["Body"], "count": 1}})
    monkeypatch.setattr(modeledit, "_live", lambda: fake)
    r = client.post("/api/model3d/live/import",
                    json={"rel": f"assets/models/{evil}"})
    assert r.status_code == 200, r.json()

    script = next(s for (kind, _p, s) in fake.calls if kind == "run")
    called = []
    for node in ast.walk(ast.parse(script)):
        if isinstance(node, ast.Call):
            fn = node.func
            called.append(fn.id if isinstance(fn, ast.Name)
                          else getattr(fn, "attr", ""))
    assert "system" not in called and "__import__" not in called, called


def test_the_import_can_add_or_replace(client, game, monkeypatch):
    from bgate_ui.routes import modeledit

    fake = _FakeLive(run={"ok": True, "result": {"count": 1}})
    monkeypatch.setattr(modeledit, "_live", lambda: fake)

    client.post("/api/model3d/live/import", json={"rel": MODEL})
    assert "bg_wipe()" not in fake.calls[-1][2]

    client.post("/api/model3d/live/import", json={"rel": MODEL, "wipe": True})
    assert "bg_wipe()" in fake.calls[-1][2]


def test_the_live_export_stays_inside_the_project(client, game, monkeypatch):
    from bgate_ui.routes import modeledit

    monkeypatch.setattr(modeledit, "_live", lambda: _FakeLive())
    r = client.post("/api/model3d/live/export",
                    json={"rel": "../../escaped.glb"})
    assert r.status_code in (400, 403, 404)

    r = client.post("/api/model3d/live/export",
                    json={"rel": "assets/models/hero.blend"})
    assert r.status_code == 400
    assert "glb" in r.json()["error"]["message"]


def test_a_live_export_that_wrote_nothing_leaves_nothing(client, game, monkeypatch):
    """The adapter reports the failure; a zero-byte file left behind would
    still show up in the picker as something to open."""
    from bgate_ui.routes import modeledit

    def half(out, **kw):
        Path(out).write_bytes(b"")
        return {"ok": False, "error": "the export wrote no file"}

    monkeypatch.setattr(modeledit, "_live", lambda: _FakeLive(export=half))
    r = client.post("/api/model3d/live/export",
                    json={"rel": "assets/models/back.glb"})
    assert r.status_code == 502
    assert not (game / "assets" / "models" / "back.glb").exists()


def test_a_live_export_comes_back_as_a_project_path(client, game, monkeypatch):
    from bgate_ui.routes import modeledit

    def good(out, **kw):
        Path(out).write_bytes(FAKE_GLB)
        return {"ok": True, "scene": {"objects": 3}, "export": {"exported": True}}

    monkeypatch.setattr(modeledit, "_live", lambda: _FakeLive(export=good))
    r = client.post("/api/model3d/live/export",
                    json={"rel": "assets/models/hero.live.glb", "port": 9999})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["rel"] == "assets/models/hero.live.glb" and d["port"] == 9999
    assert (game / "assets" / "models" / "hero.live.glb").is_file()


def test_the_viewport_photo_lands_under_bgate_out_and_is_previewable(
        client, game, monkeypatch):
    from bgate_ui.routes import modeledit

    def shot(out, **kw):
        Path(out).write_bytes(b"\x89PNG\r\n\x1a\n")
        return {"ok": True, "view": {"captured": True}}

    monkeypatch.setattr(modeledit, "_live", lambda: _FakeLive(view=shot))
    r = client.post("/api/model3d/live/view", json={})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["rel"].startswith(".bgate_out/live_view/")
    assert d["url"] == "/api/preview?rel=" + d["rel"]
    assert (game / d["rel"]).is_file()


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


def test_the_skeleton_editor_is_loaded_and_calls_its_own_endpoints():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'src="/static/modeledit_bones.js"' in html
    js = (STATIC / "modeledit_bones.js").read_text(encoding="utf-8")
    assert "/api/model3d/skeleton" in js
    # It renders into the tools column rather than owning one, so it must go
    # through that module's shared markup and not a second copy of it.
    assert "ModelTools.repaint" in js and "ModelTools.ui" in js


def test_a_sibling_module_can_repaint_the_tools_column():
    """tick() only rebuilds when the MODEL changed. Without a repaint the
    joints panel sat on its pre-read text with a skeleton already drawn in the
    viewport behind it."""
    js = (STATIC / "modeledit_tools.js").read_text(encoding="utf-8")
    assert "repaint: render," in js
    assert "ui: {panel, row" in js


def test_the_pose_editor_is_loaded_and_bakes_through_the_animate_route():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'src="/static/modeledit_pose.js"' in html
    js = (STATIC / "modeledit_pose.js").read_text(encoding="utf-8")
    # It bakes through the clip route rather than inventing a second one, so
    # the support and self-intersection gates still run over the result.
    assert "/api/model3d/animate" in js
    assert '"bones"' in js and "kind:" in js


def test_the_pose_timeline_is_not_clamped_to_its_last_key():
    """Clamping the playhead to the last key means the first key pins it at
    zero and no second key can be set later: the clip cannot grow past the
    moment it was started."""
    js = (STATIC / "modeledit_pose.js").read_text(encoding="utf-8")
    assert "clamp(num(t, 0), 0, clipLen)" in js
    assert "clamp(num(t, 0), 0, Math.max(clipSeconds()" not in js


def test_the_tools_column_reaches_every_live_blender_operation():
    """One helper builds the URL, so the operation NAMES are the call sites
    worth checking: a typo in one is a 404 nobody sees until they press it."""
    js = (STATIC / "modeledit_tools.js").read_text(encoding="utf-8")
    assert '"/api/model3d/live/" + what' in js
    assert '"/api/model3d/live?port="' in js
    for op in ("check", "import", "view", "export", "reset"):
        assert f'liveDo("{op}"' in js, f"nothing calls liveDo({op!r})"


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
