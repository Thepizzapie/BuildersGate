"""The Retro Diffusion adapter: request shaping and the job loop, offline.

The live API is exercised by one skipif-keyed smoke test; everything else
runs against a stub transport, because what this adapter owes the pipeline
is the CONTRACT — subject-only prompts, RGB-flattened inputs, the async poll,
refusals with the vendor's own words — not the vendor's uptime.
"""
from __future__ import annotations

import base64
import json

import pytest

from bgate_adapters import retrodiffusion as rd


@pytest.fixture()
def keyed(monkeypatch):
    monkeypatch.setenv(rd.ENV_KEY, "rdpk-test-not-real")


@pytest.fixture()
def transport(monkeypatch):
    """Capture requests; script responses by (method, path prefix)."""
    calls = []
    script = {}

    def fake(method, path, payload, root, timeout):
        calls.append({"method": method, "path": path, "payload": payload})
        for (m, prefix), responses in script.items():
            if m == method and path.startswith(prefix):
                return responses.pop(0) if isinstance(responses, list) else responses
        raise AssertionError(f"unscripted call {method} {path}")

    monkeypatch.setattr(rd, "_request", fake)
    monkeypatch.setattr(rd, "POLL_SECONDS", 0.0)
    return {"calls": calls, "script": script}


def _frame(tmp_path, size=(96, 80)):
    from PIL import Image

    img = Image.new("RGBA", size, (0, 0, 0, 0))
    img.paste((200, 40, 40, 255), (10, 10, 80, 70))
    p = tmp_path / "start.png"
    img.save(p)
    return str(p)


class TestValidation:
    def test_unknown_action_is_refused_before_the_wire(self, keyed, tmp_path):
        with pytest.raises(rd.RetroDiffusionError, match="unknown action"):
            rd.animate(_frame(tmp_path), "moonwalk")

    def test_frame_counts_are_the_vendors_set(self, keyed, tmp_path):
        with pytest.raises(rd.RetroDiffusionError, match="frames must be"):
            rd.animate(_frame(tmp_path), "walking", frames=7)

    def test_size_outside_the_api_range_is_refused(self, keyed, tmp_path):
        with pytest.raises(rd.RetroDiffusionError, match="outside RD"):
            rd.animate(_frame(tmp_path), "walking", size=(512, 512))

    def test_no_key_names_the_env_var_and_where_keys_come_from(self, monkeypatch):
        monkeypatch.delenv(rd.ENV_KEY, raising=False)
        monkeypatch.setattr(rd, "api_key", lambda root=None: "")
        probe = rd.available()
        assert probe["available"] is False
        assert rd.ENV_KEY in probe["reason"] and "devtools" in probe["reason"]


class TestAnimate:
    def _script(self, transport, sheet_bytes=b"png"):
        transport["script"][("POST", "/inferences")] = {
            "status": "accepted", "task_id": "t-1"}
        transport["script"][("GET", "/inferences/tasks/t-1")] = [
            {"status": "running"},
            {"status": "succeeded",
             "result": {"base64_images": [base64.b64encode(sheet_bytes).decode()],
                        "balance_cost": 0.14, "remaining_balance": 9.86}},
        ]

    def test_the_request_matches_the_vendor_contract(self, keyed, transport,
                                                     tmp_path):
        self._script(transport)
        got = rd.animate(_frame(tmp_path), "walking", frames=4,
                         prompt="confident steps")
        [submit] = [c for c in transport["calls"] if c["method"] == "POST"]
        body = submit["payload"]
        assert body["prompt_style"] == "rd_advanced_animation__walking"
        assert body["prompt"] == "confident steps"      # subject only, no style words
        assert body["width"] == 96 and body["height"] == 80  # from the frame
        assert body["frames_duration"] == 4
        assert body["return_spritesheet"] is True
        # remove_bg must NOT be sent: RD keys holes through pale garments;
        # the adapter's key_background floods from the border instead.
        assert "remove_bg" not in body
        assert body["async"] is True
        # input flattened to RGB PNG — decodes, and carries no alpha
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(base64.b64decode(body["input_image"])))
        assert img.mode == "RGB"
        assert got["ok"] and got["usd"] == 0.14 and got["frames"] == 4

    def test_polling_stops_on_failure_with_the_vendors_words(self, keyed,
                                                             transport, tmp_path):
        transport["script"][("POST", "/inferences")] = {
            "status": "accepted", "task_id": "t-2"}
        transport["script"][("GET", "/inferences/tasks/t-2")] = [
            {"status": "failed",
             "error": {"status_code": 400, "detail": "Not enough balance."}}]
        with pytest.raises(rd.RetroDiffusionError, match="Not enough balance"):
            rd.animate(_frame(tmp_path), "walking", frames=4)

    def test_a_success_with_no_images_is_an_error_not_a_none(self, keyed,
                                                             transport, tmp_path):
        transport["script"][("POST", "/inferences")] = {
            "status": "accepted", "task_id": "t-3"}
        transport["script"][("GET", "/inferences/tasks/t-3")] = [
            {"status": "succeeded", "result": {"base64_images": []}}]
        with pytest.raises(rd.RetroDiffusionError, match="no images"):
            rd.animate(_frame(tmp_path), "walking", frames=4)


class TestErrors:
    def test_both_vendor_error_shapes_surface_as_words(self, keyed, monkeypatch):
        """The API answers errors as {"detail": {...}} AND {"detail": [...]};
        both must reach the caller as the vendor's sentence, not a repr."""
        import io
        import urllib.error
        import urllib.request

        shapes = [
            ({"detail": {"code": "inference_failed",
                         "message": "Unable to run inference."}},
             "Unable to run inference"),
            ({"detail": [{"msg": "Not enough balance."}]},
             "Not enough balance"),
        ]
        for shape, expect in shapes:
            def boom(req, timeout=0, _shape=shape):
                raise urllib.error.HTTPError(
                    req.full_url, 400, "Bad Request", {},
                    io.BytesIO(json.dumps(_shape).encode()))

            monkeypatch.setattr(urllib.request, "urlopen", boom)
            with pytest.raises(rd.RetroDiffusionError, match=expect):
                rd._get("/inferences/credits")


@pytest.mark.slow
@pytest.mark.skipif(not rd.available().get("available"),
                    reason="needs a real RD key")
class TestLive:
    def test_balance_answers(self):
        got = rd.balance()
        assert isinstance(got.get("balance"), (int, float))
