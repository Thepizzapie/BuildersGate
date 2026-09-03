"""The local 2D path, against a ComfyUI that is not there and one that is.

A STUB SERVER, NOT A MOCKED FUNCTION. Everything that can go wrong in this
adapter is at the HTTP and JSON-substitution boundary: a graph missing its
placeholder, an editor-format export, a prompt containing a quote, a graph that
runs and saves nothing, a preview node winning over the save node. Patching
`_run` would test none of it, so the tests below stand up a real socket and speak
ComfyUI's actual protocol at it.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from bgate_adapters import localgen

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff030000060005574bcbb1000000"
    "0049454e44ae426082")


class _Comfy(BaseHTTPRequestHandler):
    """Just enough ComfyUI: accept a graph, remember it, serve an image back."""

    submitted: list = []
    uploads: list = []
    outputs: dict = {}

    def log_message(self, *_args):        # keep pytest output readable
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path.startswith("/api/upload/image"):
            _Comfy.uploads.append(raw)
            return self._json({"name": "ref.png", "subfolder": "", "type": "input"})
        if self.path.startswith("/api/prompt"):
            _Comfy.submitted.append(json.loads(raw.decode()))
            return self._json({"prompt_id": "job-1"})
        return self._json({"error": "no route"}, 404)

    def do_GET(self):
        if self.path.startswith("/api/history/"):
            task = self.path.rsplit("/", 1)[-1]
            return self._json({task: {"outputs": _Comfy.outputs}})
        if self.path.startswith("/api/view"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
            return
        if self.path.startswith("/api/system_stats"):
            return self._json({"system": {"comfyui_version": "stub"}})
        return self._json({"error": "no route"}, 404)


@pytest.fixture
def comfy(monkeypatch):
    _Comfy.submitted, _Comfy.uploads = [], []
    _Comfy.outputs = {"9": {"images": [{"filename": "out_0001.png",
                                        "subfolder": "", "type": "output"}]}}
    server = HTTPServer(("127.0.0.1", 0), _Comfy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BGATE_COMFY_URL",
                       f"http://127.0.0.1:{server.server_address[1]}")
    yield _Comfy
    server.shutdown()


def _workflow(tmp_path, name="t2i.json", *, prompt=True, image=False,
              editor_format=False):
    if editor_format:
        # A real editor export carries the widget values, so the token IS in
        # there — which is precisely why the format check has to exist
        # separately from the token check.
        body = {"nodes": [{"id": 1, "type": "CLIPTextEncode",
                           "widgets_values": [localgen.PROMPT_TOKEN]}]}
    else:
        body = {
            "3": {"class_type": "KSampler",
                  "inputs": {"seed": localgen.SEED_TOKEN, "steps": 20}},
            "5": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": localgen.WIDTH_TOKEN,
                             "height": localgen.HEIGHT_TOKEN}},
            "6": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": localgen.PROMPT_TOKEN if prompt else "baked"}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "out"}},
        }
        if image:
            body["10"] = {"class_type": "LoadImage",
                          "inputs": {"image": localgen.IMAGE_TOKEN}}
    path = tmp_path / name
    # The tokens are raw JSON strings in the file, so dumps then a literal swap
    # is exactly what a user's exported graph looks like.
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# It says what is missing before it says no
# ---------------------------------------------------------------------------

def test_unconfigured_is_unavailable_and_names_the_variable(monkeypatch):
    monkeypatch.delenv(localgen.TXT2IMG_ENV, raising=False)
    got = localgen.available()
    assert got["available"] is False
    assert localgen.TXT2IMG_ENV in got["reason"]
    assert got["usd"] == 0.0


def test_a_workflow_path_that_does_not_exist_is_named(monkeypatch, tmp_path):
    monkeypatch.setenv(localgen.TXT2IMG_ENV, str(tmp_path / "nope.json"))
    got = localgen.available()
    assert got["available"] is False
    assert "does not exist" in got["reason"]


def test_licence_is_unknown_until_a_model_is_declared(monkeypatch):
    monkeypatch.delenv(localgen.MODEL_ENV, raising=False)
    assert localgen.model_licence()["code"] == localgen.UNKNOWN
    monkeypatch.setenv(localgen.MODEL_ENV, "flux-dev")
    row = localgen.model_licence()
    assert row["code"] == localgen.CONDITIONAL
    assert "non-commercial" in row["summary"]


def test_an_unknown_model_is_unknown_not_free(monkeypatch):
    """Absence of a licence is never permission."""
    monkeypatch.setenv(localgen.MODEL_ENV, "something-nobody-has-heard-of")
    assert localgen.model_licence()["code"] == localgen.UNKNOWN


# ---------------------------------------------------------------------------
# The graph is configuration, and a bad one is refused with instructions
# ---------------------------------------------------------------------------

def test_graph_without_the_prompt_token_is_refused(tmp_path, monkeypatch):
    path = _workflow(tmp_path, prompt=False)
    monkeypatch.setenv(localgen.TXT2IMG_ENV, str(path))
    with pytest.raises(localgen.LocalGenError) as exc:
        localgen.build_prompt("generate", prompt="a knight")
    assert localgen.PROMPT_TOKEN in str(exc.value)
    assert "baked into the graph" in str(exc.value)


def test_editor_format_export_is_named_as_such(tmp_path, monkeypatch):
    path = _workflow(tmp_path, editor_format=True)
    monkeypatch.setenv(localgen.TXT2IMG_ENV, str(path))
    with pytest.raises(localgen.LocalGenError) as exc:
        localgen.build_prompt("generate", prompt="a knight")
    assert "EDITOR format" in str(exc.value)


def test_edit_graph_without_an_image_token_is_refused(tmp_path, monkeypatch):
    path = _workflow(tmp_path, name="edit.json", image=False)
    monkeypatch.setenv(localgen.EDIT_ENV, str(path))
    with pytest.raises(localgen.LocalGenError) as exc:
        localgen.build_prompt("edit", prompt="x", image_name="ref.png")
    assert localgen.IMAGE_TOKEN in str(exc.value)


def test_substitution_survives_a_prompt_with_quotes(tmp_path, monkeypatch):
    path = _workflow(tmp_path)
    monkeypatch.setenv(localgen.TXT2IMG_ENV, str(path))
    body = localgen.build_prompt(
        "generate", prompt='a "knight" with a \\ backslash', seed=7,
        width=768, height=512)
    graph = body["prompt"]
    assert graph["6"]["inputs"]["text"] == 'a "knight" with a \\ backslash'
    assert graph["3"]["inputs"]["seed"] == 7
    assert graph["5"]["inputs"]["width"] == 768
    assert graph["5"]["inputs"]["height"] == 512


def test_size_parsing_falls_back_to_square():
    assert localgen.parse_size("768x512") == (768, 512)
    assert localgen.parse_size("nonsense") == (1024, 1024)


# ---------------------------------------------------------------------------
# End to end against the stub
# ---------------------------------------------------------------------------

def test_generate_writes_the_image_and_prices_it_at_zero(comfy, tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv(localgen.TXT2IMG_ENV, str(_workflow(tmp_path)))
    out = tmp_path / "art" / "knight.png"
    got = localgen.generate("a knight", out, size="768x512", seed=42,
                            timeout=20)
    assert got["ok"] is True, got.get("error")
    assert out.read_bytes() == PNG
    assert got["usd"] == 0.0
    assert got["provider"] == "local"
    # The graph that actually went over the wire carried this request.
    sent = comfy.submitted[-1]["prompt"]
    assert sent["6"]["inputs"]["text"] == "a knight"
    assert sent["3"]["inputs"]["seed"] == 42


def test_edit_uploads_the_reference_first(comfy, tmp_path, monkeypatch):
    monkeypatch.setenv(localgen.EDIT_ENV,
                       str(_workflow(tmp_path, name="edit.json", image=True)))
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG)
    got = localgen.edit("hold the identity", [str(ref)], tmp_path / "o.png",
                        timeout=20)
    assert got["ok"] is True, got.get("error")
    assert comfy.uploads, "the reference never went up"
    assert comfy.submitted[-1]["prompt"]["10"]["inputs"]["image"] == "ref.png"


def test_edit_without_a_reference_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv(localgen.EDIT_ENV,
                       str(_workflow(tmp_path, name="edit.json", image=True)))
    got = localgen.edit("x", [], tmp_path / "o.png", timeout=5)
    assert got["ok"] is False
    assert "reference" in got["error"]


def test_a_graph_that_saves_nothing_says_so(comfy, tmp_path, monkeypatch):
    comfy.outputs = {"9": {"text": ["some log line"]}}
    monkeypatch.setenv(localgen.TXT2IMG_ENV, str(_workflow(tmp_path)))
    got = localgen.generate("a knight", tmp_path / "o.png", timeout=8)
    assert got["ok"] is False
    assert "SaveImage" in got["error"]


def test_saved_output_beats_a_preview(comfy, tmp_path, monkeypatch):
    """A typical graph has a PreviewImage too, at whatever size it felt like."""
    comfy.outputs = {
        "8": {"images": [{"filename": "preview.png", "subfolder": "",
                          "type": "temp"}]},
        "9": {"images": [{"filename": "final.png", "subfolder": "",
                          "type": "output"}]},
    }
    monkeypatch.setenv(localgen.TXT2IMG_ENV, str(_workflow(tmp_path)))
    got = localgen.generate("a knight", tmp_path / "o.png", timeout=8)
    assert got["ok"] is True, got.get("error")
    assert got["outputs"] == 1


def test_doctor_row_is_optional_and_never_raises(monkeypatch):
    monkeypatch.delenv(localgen.TXT2IMG_ENV, raising=False)
    row = localgen.doctor_row()
    assert row["name"] == "local_image"
    assert row["optional"] is True
    assert row["available"] is False


def test_chroma_dispatches_to_local(monkeypatch, tmp_path):
    """The point of the whole exercise: chroma's one door accepts 'local'."""
    from bgate_core.art import chroma
    seen = {}

    def fake_generate(prompt, out_path, **kw):
        seen.update(prompt=prompt, out_path=str(out_path), kw=kw)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(PNG)
        return {"ok": True, "path": str(out_path), "usd": 0.0,
                "provider": "local"}

    monkeypatch.setattr("bgate_adapters.localgen.generate", fake_generate)
    got = chroma.generate("a knight", tmp_path / "k.png", provider="local",
                          keyed=False)
    assert got.get("provider") == "local", got
    assert seen["prompt"].startswith("a knight")


def test_chroma_still_names_the_providers_it_knows(tmp_path):
    from bgate_core.art import chroma
    got = chroma.generate("x", tmp_path / "x.png", provider="wat", keyed=False)
    assert got["ok"] is False
    assert "local" in got["error"] and "krea" in got["error"]
