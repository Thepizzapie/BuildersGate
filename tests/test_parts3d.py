"""Part-aware image-to-3D: several meshes out of one plate.

Against a stub ComfyUI for the same reason test_localgen.py uses one — every
failure mode in this path is at the HTTP boundary or in the output scan, and the
one that bit during development was neither: `poll()` branched on the literal
backend name "comfy", so the second ComfyUI row polled for a status field that
protocol does not have and sat there until the timeout on a job that had
finished in three seconds. ``test_parts_backend_polls_the_history_protocol`` is
that bug.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bgate_adapters import imageto3d as i3d

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000200000002008060000007353"
    "de880000001b49444154789cedc10101000000822016ff4b17a000000000f8"
    "0d0a1c00016d0e2b0000000049454e44ae426082")
GLB = b"glTF" + b"\x02\x00\x00\x00" + b"0" * 64


class _Comfy(BaseHTTPRequestHandler):
    outputs: dict = {}
    submitted: list = []

    def log_message(self, *_a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.startswith("/api/upload/image"):
            return self._json({"name": "plate.png", "subfolder": "",
                               "type": "input"})
        if self.path.startswith("/api/prompt"):
            _Comfy.submitted.append(json.loads(raw.decode()))
            return self._json({"prompt_id": "job-7"})
        return self._json({}, 404)

    def do_GET(self):
        if self.path.startswith("/api/history/"):
            task = self.path.rsplit("/", 1)[-1]
            return self._json({task: {"outputs": _Comfy.outputs}}
                              if _Comfy.outputs else {})
        if self.path.startswith("/api/view"):
            self.send_response(200)
            self.send_header("Content-Type", "model/gltf-binary")
            self.send_header("Content-Length", str(len(GLB)))
            self.end_headers()
            self.wfile.write(GLB)
            return
        if self.path.startswith("/api/system_stats"):
            return self._json({"system": {}})
        return self._json({}, 404)


def _saved(*names):
    return {str(n): {"3d": [{"filename": name, "subfolder": "",
                             "type": "output"}]}
            for n, name in enumerate(names, start=1)}


@pytest.fixture
def comfy(monkeypatch):
    _Comfy.submitted = []
    _Comfy.outputs = _saved("head.glb", "torso.glb", "left_arm.glb")
    server = HTTPServer(("127.0.0.1", 0), _Comfy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("BGATE_COMFY_URL",
                       f"http://127.0.0.1:{server.server_address[1]}")
    yield _Comfy
    server.shutdown()


@pytest.fixture
def plate(tmp_path):
    """A REAL image, because check_input opens it before anything is sent.

    A handful of PNG bytes is not a plate: the input gate decodes it, measures
    it and refuses undersized or unreadable ones, which is exactly the guard
    that made the first draft of these tests fail on a stub image.
    """
    from PIL import Image

    p = tmp_path / "plate.png"
    Image.new("RGB", (768, 768), (30, 30, 34)).save(p)
    return p


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    path = tmp_path / "parts.json"
    path.write_text(json.dumps({
        "1": {"class_type": "LoadImage",
              "inputs": {"image": i3d.COMFY_IMAGE_TOKEN}},
        "2": {"class_type": "PartCrafter",
              "inputs": {"seed": i3d.COMFY_SEED_TOKEN}},
        "3": {"class_type": "SaveGLB", "inputs": {}},
    }), encoding="utf-8")
    monkeypatch.setenv("BGATE_COMFY_PARTS_WORKFLOW", str(path))
    return path


# ---------------------------------------------------------------------------
# The capability is declared, and it is declared honestly
# ---------------------------------------------------------------------------

def test_only_the_parts_backend_claims_parts():
    assert i3d.supports("comfy-parts", "parts") is True
    assert i3d.supports("comfy", "parts") is False


def test_the_parts_row_is_free_and_local():
    cap = i3d.capabilities("comfy-parts")
    assert cap["kind"] == "local"
    assert i3d.price_for("comfy-parts") == 0.0
    assert cap["rigged"] is False


def test_the_parts_row_does_not_claim_a_licence_it_cannot_know():
    """The graph picks the model, so the row defers to the declared one."""
    cap = i3d.capabilities("comfy-parts")
    assert cap["licence"]["code"].lower() == "conditional"
    assert "model card" in cap["licence"]["summary"].lower()


def test_both_comfy_rows_use_the_history_poll_protocol():
    """THE BUG: poll() branched on the literal name "comfy"."""
    for backend in ("comfy", "comfy-parts"):
        assert i3d.BACKENDS[backend]["poll_style"] == "history"


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

def test_parts_backend_polls_the_history_protocol(comfy, plate, workflow,
                                                  tmp_path):
    got = i3d.generate_parts(plate, tmp_path / "parts", timeout=20)
    assert got["ok"] is True, got.get("error")
    assert got["count"] == 3
    assert got["seconds"] < 20


def test_part_names_come_from_the_graph_when_they_mean_something(
        comfy, plate, workflow, tmp_path):
    got = i3d.generate_parts(plate, tmp_path / "parts", timeout=20)
    assert [p["name"] for p in got["parts"]] == ["head", "torso", "left_arm"]
    for part in got["parts"]:
        assert part["bytes"] == len(GLB)


def test_meaningless_filenames_fall_back_to_an_index(comfy, plate, workflow,
                                                     tmp_path):
    comfy.outputs = _saved("ComfyUI_00017_.glb", "ComfyUI_00018_.glb")
    got = i3d.generate_parts(plate, tmp_path / "parts", stem="piece", timeout=20)
    assert [p["name"] for p in got["parts"]] == ["piece01", "piece02"]


def test_the_result_is_ready_for_combine(comfy, plate, workflow, tmp_path):
    from pathlib import Path
    got = i3d.generate_parts(plate, tmp_path / "parts", timeout=20)
    assert [set(p) for p in got["combine"]] == [{"name", "path"}] * 3
    for entry in got["combine"]:
        assert Path(entry["path"]).is_absolute()
        assert Path(entry["path"]).is_file()


def test_a_single_mesh_result_is_flagged_not_celebrated(comfy, plate, workflow,
                                                        tmp_path):
    """A graph that merges before saving gives a monolith with extra steps."""
    comfy.outputs = _saved("merged.glb")
    got = i3d.generate_parts(plate, tmp_path / "parts", timeout=20)
    assert got["ok"] is True
    assert got["count"] == 1
    assert any("ONE mesh" in w for w in got["warnings"]), got["warnings"]


def test_a_graph_that_saves_nothing_names_the_likely_cause(comfy, plate,
                                                           workflow, tmp_path):
    comfy.outputs = {"3": {"text": ["done"]}}
    got = i3d.generate_parts(plate, tmp_path / "parts", timeout=20)
    assert got["ok"] is False
    assert "SaveGLB" in got["error"]


def test_parts_are_still_drafts(comfy, plate, workflow, tmp_path):
    got = i3d.generate_parts(plate, tmp_path / "parts", timeout=20)
    assert got["rigged"] is False
    assert got["stage"] == "draft"
    assert any(c["check"] == "parts_are_drafts" for c in got["checks"])


def test_a_backend_without_the_capability_refuses(plate, tmp_path):
    got = i3d.generate_parts(plate, tmp_path / "parts", backend="comfy",
                             timeout=5)
    assert got["ok"] is False
    assert "does not generate parts" in got["error"]


def test_an_unconfigured_workflow_says_which_variable(comfy, plate, tmp_path,
                                                      monkeypatch):
    monkeypatch.delenv("BGATE_COMFY_PARTS_WORKFLOW", raising=False)
    got = i3d.generate_parts(plate, tmp_path / "parts", timeout=10)
    assert got["ok"] is False
    assert "BGATE_COMFY_PARTS_WORKFLOW" in got["error"]
