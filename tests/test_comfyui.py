"""Reading a ComfyUI, and reading the graphs we send it.

TWO HALVES, AND THE SECOND IS THE VALUABLE ONE. The HTTP client is exercised
against a stub that answers ComfyUI's documented shapes; what it mostly has to
prove is that it DEGRADES — a build that does not serve a path, a field this
version renamed, a body that is not JSON. None of those may raise, because every
caller is painting a panel and a health read that takes the panel down is worse
than one that says it failed.

The workflow reader is the half that answers the question a user cannot answer
any other way: Builders Gate rewrites specific inputs inside their graph before
submitting it, and until this existed the only way to find out which was to read
the adapter's source. The two mistakes people actually make — exporting the
editor format instead of the API format, and a graph with no prompt placeholder
that silently regenerates the same image forever — are pinned by name.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bgate_core import comfyui as C

# Shapes copied from ComfyUI's own responses. A combo input is
# [[<options>], {<meta>}] and the option list is the first element.
STATS = {
    "system": {"os": "nt", "comfyui_version": "0.3.68",
               "python_version": "3.13.1 (tags/v3.13.1) [MSC v.1942]",
               "pytorch_version": "2.8.0+cu129"},
    "devices": [{"name": "cuda:0 NVIDIA GeForce RTX 3060", "type": "cuda",
                 "vram_total": 12884901888, "vram_free": 11274289152}],
}
OBJ = {
    "CheckpointLoaderSimple": {"input": {"required": {
        "ckpt_name": [["sd_xl_base_1.0.safetensors"], {}]}}},
    "KSampler": {"input": {"required": {
        "sampler_name": [["euler", "dpmpp_2m"], {}],
        "scheduler": [["normal", "karras"], {}],
        "steps": ["INT", {"default": 20}]}}},
}
HIST = {"abc": {"status": {"status_str": "success", "completed": True},
                "outputs": {"9": {"images": [{"filename": "a.png",
                                              "subfolder": "", "type": "output"}]}}}}

API_GRAPH = {
    "3": {"class_type": "KSampler",
          "inputs": {"seed": "__BGATE_SEED__", "steps": 20,
                     "model": ["4", 0], "positive": ["6", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
    "5": {"class_type": "EmptyLatentImage",
          "inputs": {"width": "__BGATE_WIDTH__", "height": "__BGATE_HEIGHT__"}},
    "6": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "__BGATE_PROMPT__"},
          "_meta": {"title": "positive prompt"}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "BGate"}},
}
TOKENS = {"prompt": "__BGATE_PROMPT__", "negative": "__BGATE_NEGATIVE__",
          "seed": "__BGATE_SEED__", "width": "__BGATE_WIDTH__",
          "height": "__BGATE_HEIGHT__"}


class _Stub(BaseHTTPRequestHandler):
    """A ComfyUI that answers four GETs. `missing` names paths it 404s, which is
    how a build that predates or postdates one of these routes is simulated."""

    missing: set = set()

    def log_message(self, *_a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in self.missing:
            self.send_response(404)
            self.end_headers()
            return
        if path == "/api/system_stats":
            return self._send(STATS)
        if path.startswith("/api/object_info/"):
            name = path.rsplit("/", 1)[-1]
            return self._send({name: OBJ[name]} if name in OBJ else {})
        if path == "/api/queue":
            return self._send({"queue_running": [1], "queue_pending": [1, 2]})
        if path == "/api/history":
            return self._send(HIST)
        self.send_response(404)
        self.end_headers()

    def _send(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def comfy():
    _Stub.missing = set()
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


DEAD = "http://127.0.0.1:9"          # nothing is ever listening here


class TestSystemStats:
    def test_it_answers_the_question_a_user_actually_has(self, comfy):
        got = C.system_stats(comfy)
        assert got["ok"] is True
        assert got["accelerated"] is True
        # Bytes are not an answer. 12884901888 tells a human nothing.
        assert got["devices"][0]["vram_total_gb"] == 12.0
        assert "RTX 3060" in got["verdict"]

    def test_the_python_banner_is_trimmed_to_the_version(self, comfy):
        assert C.system_stats(comfy)["python_version"] == "3.13.1"

    def test_a_cpu_only_install_is_told_it_is_on_the_cpu(self, comfy,
                                                         monkeypatch):
        monkeypatch.setitem(STATS, "devices",
                            [{"name": "cpu", "type": "cpu", "vram_total": 0}])
        got = C.system_stats(comfy)
        assert got["accelerated"] is False
        assert "CPU" in got["verdict"]

    def test_a_dead_server_answers_rather_than_raising(self):
        got = C.system_stats(DEAD, timeout=0.4)
        assert got["ok"] is False
        assert DEAD in got["error"]


class TestCatalogue:
    def test_it_enumerates_what_the_install_can_see(self, comfy):
        groups = C.catalogue(comfy)["groups"]
        assert groups["checkpoints"]["items"] == ["sd_xl_base_1.0.safetensors"]
        assert groups["samplers"]["items"] == ["euler", "dpmpp_2m"]
        assert groups["schedulers"]["items"] == ["normal", "karras"]

    def test_a_class_this_install_lacks_is_empty_not_fatal(self, comfy):
        # No LoraLoader in OBJ: the stub answers {} for it.
        assert C.catalogue(comfy)["groups"]["loras"]["items"] == []

    def test_every_group_carries_its_own_explanation(self, comfy):
        for group in C.catalogue(comfy)["groups"].values():
            assert len(group["help"]) > 20

    def test_a_dead_server_degrades_to_empty_lists(self):
        got = C.catalogue(DEAD, timeout=0.4)
        assert got["errors"]
        assert all(g["items"] == [] for g in got["groups"].values())


class TestQueueAndHistory:
    def test_busy_is_distinguished_from_frozen(self, comfy):
        got = C.queue(comfy)
        assert got["running"] == 1 and got["pending"] == 2
        assert "waiting behind it" in got["verdict"]

    def test_history_hands_back_urls_a_browser_can_load(self, comfy):
        runs = C.history(comfy)["runs"]
        assert runs[0]["status"] == "success"
        assert runs[0]["images"][0]["url"].startswith(comfy + "/api/view?")
        assert "filename=a.png" in runs[0]["images"][0]["url"]


class TestDescribeWorkflow:
    def test_it_names_the_inputs_builders_gate_overwrites(self, tmp_path):
        path = tmp_path / "t2i.json"
        path.write_text(json.dumps(API_GRAPH), encoding="utf-8")
        got = C.describe_workflow(str(path), TOKENS)
        assert got["format"] == "api"
        marked = {n["id"]: [m["field"] for m in n["injected"]]
                  for n in got["injected"]}
        assert marked == {"3": ["seed"], "5": ["width", "height"], "6": ["text"]}
        # And in the user's words, not the adapter's constant name.
        prompt = [m for n in got["injected"] for m in n["injected"]
                  if m["meaning"] == "prompt"][0]
        assert "prompt you" in prompt["what"]

    def test_it_says_which_weights_the_graph_loads(self, tmp_path):
        path = tmp_path / "t2i.json"
        path.write_text(json.dumps(API_GRAPH), encoding="utf-8")
        assert got_weights(C.describe_workflow(str(path), TOKENS)) == [
            "ckpt_name: sd_xl_base_1.0.safetensors"]

    def test_the_editor_format_is_caught_and_named(self, tmp_path):
        """The single most common export mistake. Both files are .json, both are
        'a workflow', and the error ComfyUI gives for the wrong one names a node
        id and nothing else."""
        path = tmp_path / "editor.json"
        path.write_text(json.dumps({"nodes": [{"id": 3, "type": "KSampler"}],
                                    "links": []}), encoding="utf-8")
        got = C.describe_workflow(str(path), TOKENS)
        assert got["format"] == "editor"
        assert "Export (API)" in got["error"]

    def test_a_graph_with_no_prompt_placeholder_is_warned_about(self, tmp_path):
        """The failure that looks exactly like a working feature: every run
        comes back as whatever text was typed into the node at export time."""
        graph = dict(API_GRAPH)
        graph["6"] = {"class_type": "CLIPTextEncode",
                      "inputs": {"text": "a knight, oil painting"}}
        path = tmp_path / "baked.json"
        path.write_text(json.dumps(graph), encoding="utf-8")
        got = C.describe_workflow(str(path), TOKENS)
        assert "prompt" in got["missing"]
        assert any("same image, every time" in w for w in got["warnings"])

    def test_a_missing_file_says_what_to_do(self, tmp_path):
        got = C.describe_workflow(str(tmp_path / "gone.json"), TOKENS)
        assert got["exists"] is False
        assert "gone.json" in got["error"]

    def test_an_unset_path_is_reported_as_unset_not_as_broken(self):
        assert C.describe_workflow("", TOKENS)["error"] == "not set"

    def test_invalid_json_is_reported_as_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        got = C.describe_workflow(str(path), TOKENS)
        assert got["format"] == "invalid"
        assert "not valid JSON" in got["error"]

    def test_a_link_reference_is_not_mistaken_for_a_literal(self, tmp_path):
        """`"model": ["4", 0]` is a wire, not a value. Treating it as text is
        how a reader starts reporting injections into nodes that have none."""
        path = tmp_path / "t2i.json"
        path.write_text(json.dumps(API_GRAPH), encoding="utf-8")
        got = C.describe_workflow(str(path), TOKENS)
        ksampler = [n for n in got["nodes"] if n["id"] == "3"][0]
        assert [m["field"] for m in ksampler["injected"]] == ["seed"]


def got_weights(described: dict) -> list:
    return described["weights"]
