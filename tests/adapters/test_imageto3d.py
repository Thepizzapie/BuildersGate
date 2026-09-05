"""The image-to-3D adapter, offline.

Nothing here touches the network, spawns nvidia-smi for real, or loads a model.
What is under test is the part that will actually bite:

  * the ZERO-CONFIGURATION answer. This is an optional capability on a machine
    that may have no GPU and no keys, and "nothing is set up" has to be a
    complete, actionable report rather than an exception or a crash.
  * PRICING, which is three-valued. 0.0 for a local backend, a real number for a
    hosted one that publishes a rate, and None for one that does not — and None
    must never quietly become 0.0, because the spend gate reads a number as
    permission.
  * PAYLOAD SHAPE PER BACKEND. Each has its own schema and sending the wrong one
    is a 400, not a default. This is the failure krea.py hit on its first live
    call (aspect_ratio to flux) and the reason these are pinned per backend
    rather than against one shared shape.
  * LICENCE, which is a gate. A backend with a conditional licence must never be
    selected automatically, because this tool does not know its user's revenue,
    territory or monthly actives.
"""
from __future__ import annotations

import base64
import json
import subprocess
import urllib.error

import pytest

from bgate_adapters import imageto3d


ALL_KEYS = ("TRIPO_API_KEY", "MESHY_API_KEY", "STABILITY_API_KEY",
            "RODIN_API_KEY")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No keys, no cached GPU, no inherited overrides.

    The GPU probe caches on purpose — a dashboard polling status() must not
    spawn a process per poll — so every test has to clear it or the first one to
    run decides the answer for all the others.
    """
    monkeypatch.setattr(imageto3d, "_gpu_cache", None, raising=False)
    for name in ALL_KEYS + ("BGATE_IMAGETO3D_PYTHON", "BGATE_COMFY_URL",
                            "BGATE_COMFY_WORKFLOW", "BGATE_HUNYUAN3D_URL",
                            imageto3d.MODEL_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def no_gpu(monkeypatch):
    """A machine with no NVIDIA driver — nvidia-smi is not on PATH."""
    def boom(*a, **k):
        raise FileNotFoundError("nvidia-smi")
    monkeypatch.setattr(imageto3d.subprocess, "run", boom)
    monkeypatch.setattr(imageto3d, "_gpu_cache", None, raising=False)


@pytest.fixture()
def has_gpu(monkeypatch):
    """The target box: RTX 3060, 12 GB, driver 595.95."""
    def fake(*a, **k):
        return subprocess.CompletedProcess(
            a[0] if a else [], 0,
            stdout="NVIDIA GeForce RTX 3060, 12288 MiB, 595.95\n", stderr="")
    monkeypatch.setattr(imageto3d.subprocess, "run", fake)
    monkeypatch.setattr(imageto3d, "_gpu_cache", None, raising=False)


@pytest.fixture()
def plate(tmp_path):
    """A plate that check_input ACCEPTS, with or without Pillow.

    Written as a real 512x512 PNG when Pillow is there, because check_input
    measures it and a 1x1 file would be refused for being under the floor —
    which would make every test using this fixture pass for the wrong reason.
    Without Pillow the size checks do not run, so any bytes will do.
    """
    f = tmp_path / "hero.png"
    try:
        from PIL import Image
    except ImportError:
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        return f
    Image.new("RGBA", (512, 512), (0, 0, 0, 0)).save(f)
    return f


# ---------------------------------------------------------------------------
class TestNothingConfigured:
    """The answer a user with no GPU and no keys gets. It must be complete."""

    def test_status_reports_every_backend_rather_than_raising(self, no_gpu):
        got = imageto3d.status()
        assert got["ok"] is False
        assert {row["backend"] for row in got["backends"]} == set(imageto3d.BACKENDS)
        assert got["usable"] == []

    def test_the_reason_names_what_to_set_and_says_nothing_else_breaks(self, no_gpu):
        reason = imageto3d.status()["reason"]
        for key in ALL_KEYS:
            assert key in reason
        # The known bgate doctor trap in reverse: a missing optional capability
        # must not read as a broken install.
        assert "affected" in reason

    def test_every_backend_carries_a_reason_when_it_is_unavailable(self, no_gpu):
        for row in imageto3d.status()["backends"]:
            assert row["available"] is False
            assert row["reason"], f"{row['backend']} refused without saying why"

    def test_no_gpu_is_reported_as_a_fact_not_an_error(self, no_gpu):
        card = imageto3d.gpu()
        assert card["available"] is False
        assert "nvidia-smi not found" in card["reason"]
        assert "unaffected" in card["reason"]

    def test_an_unknown_backend_answers_with_the_known_ones(self):
        got = imageto3d.available("not-a-backend")
        assert got["available"] is False
        assert "tripo" in got["reason"] and "comfy" in got["reason"]

    def test_there_is_no_default_backend(self):
        """Picking one silently would be a licence decision made for a
        stranger."""
        assert imageto3d.DEFAULT_BACKEND == ""


class TestGpuProbe:
    def test_it_never_imports_torch_anywhere_at_any_depth(self):
        """The rule the whole optional-capability design rests on.

        Checked against the AST rather than the text, because the module talks
        ABOUT not importing torch in a comment and a substring search would
        match its own warning. Nested imports count: a lazy `import torch`
        inside a function is still an import this process must never do.

        MEASURED on the target box: torch was installed but CPU-only, so a torch
        probe would have reported no GPU on a machine with a perfectly good one.
        """
        import ast

        with open(imageto3d.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        banned = {"torch", "torchvision", "diffusers", "transformers",
                  "trimesh", "pytorch3d"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, alias.name
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned, node.module

    def test_it_parses_name_vram_and_driver(self, has_gpu):
        card = imageto3d.gpu()
        assert card["available"] is True
        assert card["name"] == "NVIDIA GeForce RTX 3060"
        assert card["vram_gb"] == 12.0
        assert card["driver"] == "595.95"

    def test_the_result_is_cached_so_a_poll_is_not_a_process_per_call(self, monkeypatch):
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return subprocess.CompletedProcess([], 0, stdout="GPU, 8192 MiB, 1\n",
                                               stderr="")
        monkeypatch.setattr(imageto3d.subprocess, "run", fake)
        monkeypatch.setattr(imageto3d, "_gpu_cache", None, raising=False)
        imageto3d.gpu()
        imageto3d.gpu()
        imageto3d.gpu()
        assert calls["n"] == 1
        imageto3d.gpu(refresh=True)
        assert calls["n"] == 2

    def test_a_small_card_is_reported_with_the_number_not_just_refused(self, monkeypatch):
        monkeypatch.setattr(imageto3d.subprocess, "run", lambda *a, **k:
                            subprocess.CompletedProcess([], 0,
                                                        stdout="GTX 1050, 4096 MiB, 500\n",
                                                        stderr=""))
        monkeypatch.setattr(imageto3d, "_gpu_cache", None, raising=False)
        card = imageto3d.gpu()
        assert card["vram_gb"] == 4.0
        assert "4.0 GB" in card["reason"]

    def test_fits_vram_does_not_pass_an_unknown_requirement_off_as_a_check(self, has_gpu):
        got = imageto3d.fits_vram(None)
        assert got["ok"] is True
        assert "not checked" in got["reason"]

    def test_fits_vram_refuses_when_the_number_does_not_fit(self, has_gpu):
        got = imageto3d.fits_vram(21.0)          # hunyuan textured on a 12 GB card
        assert got["ok"] is False
        assert "21.0" in got["reason"] and "12.0" in got["reason"]

    def test_the_runner_interpreter_does_not_default_to_this_one(self):
        """The opposite of transcribe.whisper_python, deliberately: faster-whisper
        installs beside this tool and an inference stack does not."""
        assert imageto3d.runner_python() == ""


class TestDoctorRow:
    def test_absent_is_shaped_like_every_other_doctor_row(self, no_gpu):
        row = imageto3d.doctor_row()
        assert set(row) == {"available", "path", "version", "min_required", "reason"}
        assert row["available"] is False

    def test_absent_says_it_is_optional(self, no_gpu):
        """bgate doctor exits non-zero on any missing row, so a row that does
        not say 'optional' reads as a broken install."""
        assert "Optional" in imageto3d.doctor_row()["reason"]

    def test_present_reports_the_card_and_no_reason(self, has_gpu):
        row = imageto3d.doctor_row()
        assert row["available"] is True
        assert "RTX 3060" in row["path"]
        assert "595.95" in row["version"]
        assert row["reason"] == ""


class TestPricing:
    def test_a_local_backend_costs_nothing_and_says_so(self):
        assert imageto3d.price_for("comfy") == 0.0
        assert imageto3d.price_for("hunyuan-local") == 0.0

    def test_tripo_prices_from_its_published_credit_rate(self):
        """30 credits at $0.01 — both numbers off Tripo's own pricing table."""
        assert imageto3d.credits_for("tripo") == 30
        assert imageto3d.price_for("tripo") == 0.30

    def test_the_price_follows_the_request_not_the_backend(self):
        assert imageto3d.price_for("tripo", texture=False) == 0.20
        assert imageto3d.price_for("tripo", quad=True) == 0.35
        assert imageto3d.price_for("tripo", texture=False, quad=True) == 0.25
        assert imageto3d.price_for("tripo", rig=True) == 0.55

    def test_animations_are_per_clip(self):
        assert imageto3d.price_for("tripo", animations=3) == 0.60

    def test_stability_is_the_cheapest_hosted_option(self):
        assert imageto3d.price_for("stability") == 0.10

    def test_an_unpublished_rate_is_None_and_never_zero(self):
        """The whole point. The spend gate treats a number as permission, so an
        invented price is worse than a missing one."""
        assert imageto3d.credits_for("meshy") == 30    # the credits ARE published
        assert imageto3d.price_for("meshy") is None    # the dollar rate is not
        assert imageto3d.price_for("rodin") is None

    def test_a_missing_price_comes_with_the_reason(self):
        assert "no per-credit dollar rate" in imageto3d.price_note("meshy")
        assert "Business plan" in imageto3d.price_note("rodin")
        assert imageto3d.price_note("tripo") == ""
        assert imageto3d.price_note("comfy") == ""

    def test_count_multiplies(self):
        assert imageto3d.price_for("stability", count=4) == 0.40

    def test_an_unknown_backend_raises_rather_than_quoting_a_default(self):
        with pytest.raises(imageto3d.ImageTo3DError, match="unknown backend"):
            imageto3d.price_for("who")


class TestLicence:
    def test_every_backend_declares_one(self):
        for name, spec in imageto3d.BACKENDS.items():
            licence = spec["licence"]
            assert licence["code"] in (imageto3d.FREE, imageto3d.CONDITIONAL,
                                       imageto3d.FORBIDDEN), name
            assert licence["summary"], name

    def test_only_unrestricted_licences_may_be_chosen_automatically(self):
        assert imageto3d.AUTO_LICENCES == (imageto3d.FREE,)

    def test_hunyuan_states_the_region_carve_out(self):
        summary = imageto3d.BACKENDS["hunyuan-local"]["licence"]["summary"]
        for region in ("European Union", "United Kingdom", "South Korea"):
            assert region in summary
        assert "1 million" in summary

    def test_stability_does_not_inherit_the_community_licence_revenue_cap(self):
        """The obvious mistake: the US$1M threshold governs SELF-HOSTING the
        weights, not paying per credit for the hosted endpoint."""
        licence = imageto3d.BACKENDS["stability"]["licence"]
        assert licence["code"] == imageto3d.FREE
        assert "no revenue threshold" in licence["summary"]

    def test_comfy_admits_it_cannot_clear_the_licence_for_you(self):
        """The graph decides which model runs, and the adapter cannot read it."""
        licence = imageto3d.BACKENDS["comfy"]["licence"]
        assert licence["code"] == imageto3d.CONDITIONAL
        assert "cannot clear the licence" in licence["summary"]


class TestModelLicence:
    """A local backend is a TRANSPORT. ComfyUI is MIT; that says nothing about
    the weights the graph loaded, and conflating the two always errs
    permissively."""

    def test_undeclared_is_conditional_not_permissive(self):
        got = imageto3d.model_licence()
        assert got["code"] == imageto3d.CONDITIONAL
        assert imageto3d.MODEL_ENV in got["summary"]

    def test_a_typo_is_not_permission(self):
        assert imageto3d.model_licence("trelis")["code"] == imageto3d.CONDITIONAL

    def test_the_mit_models_are_unrestricted(self):
        for name in ("trellis", "trellis2", "triposr"):
            assert imageto3d.model_licence(name)["code"] == imageto3d.FREE, name

    def test_the_non_commercial_ones_are_marked_forbidden(self):
        for name in ("partpacker", "zero123plus"):
            assert imageto3d.model_licence(name)["code"] == imageto3d.FORBIDDEN, name

    def test_hunyuan_carries_its_territory_clause_here_too(self):
        summary = imageto3d.model_licence("hunyuan3d")["summary"]
        assert "European Union" in summary and "South Korea" in summary

    def test_stability_weights_carry_the_revenue_cap_the_hosted_api_does_not(self):
        """The distinction that is easy to get backwards: self-hosting the
        weights is capped at US$1M, paying per credit for the hosted endpoint is
        not."""
        assert imageto3d.model_licence("sf3d")["code"] == imageto3d.CONDITIONAL
        assert "1,000,000" in imageto3d.model_licence("sf3d")["summary"]
        assert imageto3d.BACKENDS["stability"]["licence"]["code"] == imageto3d.FREE

    def test_declaring_the_model_resolves_the_backend_licence(self, monkeypatch, has_gpu):
        monkeypatch.setenv(imageto3d.MODEL_ENV, "trellis2")
        assert imageto3d.available("comfy")["licence"]["code"] == imageto3d.FREE
        monkeypatch.setenv(imageto3d.MODEL_ENV, "hunyuan3d")
        assert imageto3d.available("comfy")["licence"]["code"] == imageto3d.CONDITIONAL

    def test_undeclared_leaves_the_backend_unclearable(self, has_gpu):
        got = imageto3d.available("comfy")
        assert got["licence"]["code"] == imageto3d.CONDITIONAL
        assert "cannot clear the licence" in got["licence"]["summary"]


class TestChoose:
    def _reachable(self, monkeypatch, *names):
        def fake(backend, root=None, *, probe=False):
            spec = imageto3d.BACKENDS[backend]
            return {"backend": backend, "kind": spec["kind"],
                    "label": spec["label"], "licence": dict(spec["licence"]),
                    "available": backend in names, "reason": "",
                    "implemented": spec.get("implemented", True)}
        monkeypatch.setattr(imageto3d, "available", fake)

    def test_local_is_preferred_over_hosted(self, monkeypatch):
        # comfy is conditional, so make the FREE-licensed hosted one reachable
        # too and confirm the ranking is not what picks it.
        self._reachable(monkeypatch, "comfy", "stability")
        got = imageto3d.choose()
        # comfy ranks first but is conditional, so stability wins on LICENCE.
        assert got["backend"] == "stability"
        assert got["candidates"][0] == "comfy"

    def test_it_refuses_rather_than_pick_a_conditional_backend(self, monkeypatch):
        self._reachable(monkeypatch, "comfy", "hunyuan-local", "tripo")
        got = imageto3d.choose()
        assert got["backend"] == ""
        assert "conditional licence" in got["reason"]
        # and it names them, so the caller can offer the choice
        assert "hunyuan-local" in got["reason"]
        assert set(got["candidates"]) == {"comfy", "hunyuan-local", "tripo"}

    def test_nothing_reachable_falls_through_to_the_status_reason(self, monkeypatch, no_gpu):
        self._reachable(monkeypatch)
        got = imageto3d.choose()
        assert got["backend"] == "" and got["candidates"] == []
        assert got["reason"]


class TestPayloadPerBackend:
    def test_hunyuan_uses_its_own_field_names(self):
        payload = imageto3d.build_payload(
            "hunyuan-local", image="BASE64", texture=True, seed=7,
            steps=30, guidance=5.5, octree_resolution=256, face_count=40000)
        assert payload["image"] == "BASE64"
        assert payload["texture"] is True
        assert payload["num_inference_steps"] == 30
        assert payload["guidance_scale"] == 5.5
        assert payload["octree_resolution"] == 256
        assert payload["face_count"] == 40000
        assert payload["type"] == "glb"

    def test_tripo_nests_the_plate_under_file_and_refuses_inline_bytes(self):
        """Tripo does not accept a data URI. The two-hop upload is not optional
        and skipping it is a 400."""
        with pytest.raises(imageto3d.ImageTo3DError, match="file token"):
            imageto3d.build_payload("tripo", image="data:image/png;base64,AA")
        payload = imageto3d.build_payload("tripo", image_token="tok-1")
        assert payload["file"] == {"type": "png", "file_token": "tok-1"}
        assert payload["type"] == "image_to_model"
        assert payload["model_version"] == "v2.5-20250123"

    def test_meshy_takes_the_plate_inline_under_its_own_key(self):
        payload = imageto3d.build_payload("meshy", image="data:image/png;base64,AA")
        assert payload["image_url"].startswith("data:image/png")
        assert payload["should_texture"] is True
        assert payload["ai_model"] == "meshy-6"

    def test_meshy_quad_is_an_enum_not_a_boolean(self):
        """Every other backend takes a bool. Writing True into `topology` would
        be a 400 that reads like a schema mystery."""
        payload = imageto3d.build_payload("meshy", image="x", quad=True)
        assert payload["topology"] == "quad"
        assert "quad" not in payload

    def test_meshy_turns_on_remesh_or_the_request_is_silently_ignored(self):
        """should_remesh defaults FALSE on meshy-6, which makes topology and
        target_polycount no-ops. A request that quietly does none of what was
        asked is the failure this module exists not to have."""
        assert imageto3d.build_payload(
            "meshy", image="x", quad=True)["should_remesh"] is True
        assert imageto3d.build_payload(
            "meshy", image="x", face_count=20000)["should_remesh"] is True
        assert "should_remesh" not in imageto3d.build_payload("meshy", image="x")

    def test_the_polycount_has_four_different_names(self):
        """face_count / face_limit / target_polycount / vertex_count, all
        meaning roughly the same thing."""
        assert "face_count" in imageto3d.build_payload(
            "hunyuan-local", image="x", face_count=1000)
        assert "face_limit" in imageto3d.build_payload(
            "tripo", image_token="t", face_count=1000)
        assert "target_polycount" in imageto3d.build_payload(
            "meshy", image="x", face_count=1000)

    def test_an_unsupported_option_is_refused_and_names_who_offers_it(self):
        """pose_mode is Meshy's alone, and it is the most useful parameter here
        — so asking for it elsewhere must point at Meshy, not fail silently."""
        with pytest.raises(imageto3d.ImageTo3DError) as exc:
            imageto3d.build_payload("hunyuan-local", image="x", pose="t-pose")
        assert "meshy" in str(exc.value)

    def test_an_unsupported_option_is_never_silently_dropped(self):
        with pytest.raises(imageto3d.ImageTo3DError, match="pbr"):
            imageto3d.build_payload("hunyuan-local", image="x", pbr=True)

    def test_a_format_the_backend_cannot_produce_is_refused(self):
        with pytest.raises(imageto3d.ImageTo3DError, match="does not return"):
            imageto3d.build_payload("hunyuan-local", image="x", out_format="fbx")

    def test_a_multipart_backend_is_not_built_this_way(self):
        with pytest.raises(imageto3d.ImageTo3DError, match="multipart"):
            imageto3d.build_payload("stability", image="x")

    def test_a_workflow_backend_is_not_built_this_way_either(self):
        with pytest.raises(imageto3d.ImageTo3DError, match="workflow graph"):
            imageto3d.build_payload("comfy", image="x")

    def test_no_option_leaks_between_backends(self):
        """Each backend's payload contains only keys it declared."""
        payload = imageto3d.build_payload("hunyuan-local", image="x", seed=1)
        assert "model_version" not in payload
        assert "ai_model" not in payload


class TestComfyWorkflow:
    def test_no_workflow_configured_explains_the_whole_setup(self):
        with pytest.raises(imageto3d.ImageTo3DError) as exc:
            imageto3d.build_comfy_prompt("plate.png")
        message = str(exc.value)
        assert "Save (API format)" in message
        assert imageto3d.COMFY_IMAGE_TOKEN in message
        assert "BGATE_COMFY_WORKFLOW" in message

    def test_a_workflow_without_the_placeholder_is_refused(self, tmp_path):
        wf = tmp_path / "wf.json"
        wf.write_text(json.dumps({"1": {"class_type": "LoadImage",
                                        "inputs": {"image": "baked-in.png"}}}))
        with pytest.raises(imageto3d.ImageTo3DError, match="no .* placeholder"):
            imageto3d.build_comfy_prompt("plate.png", workflow_path=str(wf))

    def test_the_plate_and_seed_are_substituted(self, tmp_path):
        wf = tmp_path / "wf.json"
        wf.write_text(json.dumps({
            "1": {"class_type": "LoadImage",
                  "inputs": {"image": imageto3d.COMFY_IMAGE_TOKEN}},
            "2": {"class_type": "Sampler",
                  "inputs": {"seed": imageto3d.COMFY_SEED_TOKEN}},
        }).replace(f'"{imageto3d.COMFY_SEED_TOKEN}"', imageto3d.COMFY_SEED_TOKEN))
        body = imageto3d.build_comfy_prompt("plate.png", seed=42,
                                            workflow_path=str(wf))
        assert body["prompt"]["1"]["inputs"]["image"] == "plate.png"
        assert body["prompt"]["2"]["inputs"]["seed"] == 42
        assert body["client_id"] == "builders-gate"

    def test_a_missing_workflow_file_says_which_one(self, tmp_path):
        with pytest.raises(imageto3d.ImageTo3DError, match="does not exist"):
            imageto3d.build_comfy_prompt("p.png",
                                         workflow_path=str(tmp_path / "gone.json"))

    def test_outputs_are_found_by_suffix_not_by_node_class(self):
        """3D-Pack renames its saver nodes between releases, so keying on one
        would break on somebody else's upgrade."""
        history = {"job-1": {"outputs": {
            "9": {"images": [{"filename": "preview.png", "subfolder": "",
                              "type": "output"}]},
            "12": {"result": [{"filename": "hero.glb", "subfolder": "3d",
                               "type": "output"}]},
        }}}
        found = imageto3d._comfy_outputs(history, "job-1")
        assert [f["filename"] for f in found] == ["hero.glb"]
        assert found[0]["subfolder"] == "3d"

    def test_a_run_that_saved_nothing_usable_finds_nothing(self):
        history = {"j": {"outputs": {"1": {"images": [{"filename": "a.png"}]}}}}
        assert imageto3d._comfy_outputs(history, "j") == []


class TestInputPlate:
    def test_a_missing_plate_is_refused_before_anything_is_generated(self, tmp_path):
        got = imageto3d.check_input(tmp_path / "nope.png")
        assert got["ok"] is False and "no such image" in got["reason"]

    def test_an_unsupported_type_names_the_ones_that_work(self, tmp_path):
        f = tmp_path / "plate.tga"
        f.write_bytes(b"x")
        got = imageto3d.check_input(f)
        assert got["ok"] is False
        assert "png" in got["reason"] and "webp" in got["reason"]

    def test_bare_base64_and_a_data_uri_are_not_the_same_thing(self, plate):
        """Sending one where the other is expected is a decode error on the
        server, which surfaces as a useless 500."""
        assert imageto3d.data_uri(plate).startswith("data:image/png;base64,")
        bare = imageto3d.image_b64(plate)
        assert not bare.startswith("data:")
        assert base64.b64decode(bare) == plate.read_bytes()

    def test_a_missing_file_raises_from_both_encoders(self, tmp_path):
        for call in (imageto3d.data_uri, imageto3d.image_b64):
            with pytest.raises(imageto3d.ImageTo3DError, match="not found"):
                call(tmp_path / "gone.png")


class TestErrors:
    def _http(self, monkeypatch, code, body=b"{}"):
        def boom(*a, **k):
            raise urllib.error.HTTPError("u", code, "err", {}, None)
        monkeypatch.setattr("urllib.request.urlopen", boom)
        from bgate_adapters import _http
        monkeypatch.setattr(_http, "_sleep", lambda *_: None)

    def test_401_points_at_the_key_by_name(self, monkeypatch):
        self._http(monkeypatch, 401)
        with pytest.raises(imageto3d.ImageTo3DError, match="TRIPO_API_KEY"):
            imageto3d._request("tripo", "/task", "k", payload={}, method="POST")

    def test_402_explains_the_balance_rather_than_the_code(self, monkeypatch):
        self._http(monkeypatch, 402)
        with pytest.raises(imageto3d.ImageTo3DError, match="no credit"):
            imageto3d._request("meshy", "/image-to-3d", "k", payload={},
                               method="POST")

    def test_429_says_to_slow_the_fan_out(self, monkeypatch):
        self._http(monkeypatch, 429)
        with pytest.raises(imageto3d.ImageTo3DError, match="rate-limited"):
            imageto3d._request("meshy", "/x", "k")

    def test_400_says_schemas_are_per_backend(self, monkeypatch):
        self._http(monkeypatch, 400)
        with pytest.raises(imageto3d.ImageTo3DError, match="own schema"):
            imageto3d._request("tripo", "/task", "k", payload={}, method="POST")

    def test_a_local_404_says_the_wrong_thing_is_running(self, monkeypatch):
        """A hosted 404 is a bug; a local one almost always means the port
        belongs to some other server."""
        self._http(monkeypatch, 404)
        with pytest.raises(imageto3d.ImageTo3DError, match="not the server"):
            imageto3d._request("hunyuan-local", "/send", "", payload={},
                               method="POST")

    def test_unreachable_local_says_start_it_not_check_your_key(self, monkeypatch):
        def boom(*a, **k):
            raise urllib.error.URLError("connection refused")
        monkeypatch.setattr("urllib.request.urlopen", boom)
        from bgate_adapters import _http
        monkeypatch.setattr(_http, "_sleep", lambda *_: None)
        with pytest.raises(imageto3d.ImageTo3DError) as exc:
            imageto3d._request("hunyuan-local", "/send", "", payload={},
                               method="POST")
        assert "is the server running" in str(exc.value)
        assert "BGATE_HUNYUAN3D_URL" in str(exc.value)

    def test_a_non_json_response_is_named_as_such(self, monkeypatch):
        class Resp:
            def read(self):
                return b"<html>nope</html>"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        monkeypatch.setattr(imageto3d.urllib.request, "urlopen",
                            lambda *a, **k: Resp())
        with pytest.raises(imageto3d.ImageTo3DError, match="non-JSON"):
            imageto3d._request("meshy", "/x", "k")


class TestSubmitGuards:
    def test_a_documented_but_unwired_backend_refuses_up_front(self, plate):
        with pytest.raises(imageto3d.ImageTo3DError, match="not wired"):
            imageto3d.submit("rodin", plate)

    def test_a_bad_plate_is_refused_before_any_call(self, tmp_path):
        with pytest.raises(imageto3d.ImageTo3DError, match="no such image"):
            imageto3d.submit("meshy", tmp_path / "gone.png")

    def test_a_hosted_backend_with_no_key_refuses_before_any_call(self, plate):
        with pytest.raises(imageto3d.ImageTo3DError, match="MESHY_API_KEY"):
            imageto3d.submit("meshy", plate)

    def test_poll_refuses_on_a_synchronous_backend(self):
        with pytest.raises(imageto3d.ImageTo3DError, match="synchronous"):
            imageto3d.poll("stability", "t")


class TestResultShape:
    """The result must not make a caller branch on where the geometry came
    from, and must never let a draft be mistaken for an asset."""

    def test_a_failed_generation_is_a_result_not_an_exception(self, tmp_path, no_gpu):
        got = imageto3d.generate(tmp_path / "gone.png", tmp_path / "out.glb",
                                 backend="meshy")
        assert got["ok"] is False
        assert got["stage"] == "input"
        assert "no such image" in got["error"]

    def test_it_carries_the_keys_the_pipeline_reads(self, tmp_path, no_gpu):
        got = imageto3d.generate(tmp_path / "gone.png", tmp_path / "out.glb",
                                 backend="tripo")
        assert set(got) >= {"ok", "path", "bytes", "backend", "kind", "seconds",
                            "usd", "checks", "warnings", "notes",
                            "licence", "draft", "textured", "rigged", "stage"}

    def test_the_quote_is_on_the_result_before_anything_is_spent(self, tmp_path, no_gpu):
        got = imageto3d.generate(tmp_path / "gone.png", tmp_path / "o.glb",
                                 backend="tripo")
        assert got["usd"] == 0.30

    def test_an_unpriceable_backend_carries_None_and_the_reason(self, tmp_path, no_gpu):
        got = imageto3d.generate(tmp_path / "gone.png", tmp_path / "o.glb",
                                 backend="meshy")
        assert got["usd"] is None
        assert any("per-credit" in w for w in got["warnings"])

    def test_choosing_nothing_is_reported_at_the_choose_stage(self, tmp_path, no_gpu):
        got = imageto3d.generate(tmp_path / "a.png", tmp_path / "o.glb")
        assert got["ok"] is False
        assert got["stage"] == "choose"

    def test_a_draft_is_labelled_a_draft(self):
        """Nothing downstream may treat this as a finished asset — the whole
        reason the harness exists."""
        blank = imageto3d._result("tripo")
        assert blank["draft"] is True
        assert blank["rigged"] is False

    def test_the_next_steps_name_the_conventions_by_number(self):
        joined = " ".join(imageto3d.NEXT_STEPS)
        assert "+Y" in joined          # facing, decided 2026-07-30
        assert "1.8" in joined         # metres, the canonical humanoid
        assert "bgate_rig_repair" in joined
        assert "blender_combine" in joined


class TestBackendTable:
    def test_local_backends_come_first(self):
        assert imageto3d.LOCAL[0] == "comfy"
        assert set(imageto3d.LOCAL) & set(imageto3d.HOSTED) == set()

    def test_no_local_backend_needs_a_key(self):
        for name in imageto3d.LOCAL:
            assert imageto3d.BACKENDS[name]["env"] == ""

    def test_every_local_backend_says_what_it_needs_and_where_weights_live(self):
        for name in imageto3d.LOCAL:
            spec = imageto3d.BACKENDS[name]
            assert spec["weights"], name
            assert spec["windows"], name

    def test_no_hosted_backend_claims_a_vram_requirement(self):
        for name in imageto3d.HOSTED:
            assert imageto3d.BACKENDS[name].get("vram_gb") is None

    def test_the_five_minute_url_is_recorded_where_it_bites(self):
        """Tripo expires a finished-model URL in five minutes, which is why
        generate() downloads inside the polling loop."""
        assert imageto3d.BACKENDS["tripo"]["url_ttl_s"] == 300
        assert imageto3d.BACKENDS["meshy"]["url_ttl_s"] == 259200

    def test_supports_is_asked_before_pricing_not_after(self):
        assert imageto3d.supports("meshy", "pose") is True
        assert imageto3d.supports("hunyuan-local", "pose") is False
        assert imageto3d.supports("tripo", "rig") is True

    def test_capabilities_answers_without_a_network(self):
        got = imageto3d.capabilities("hunyuan-local")
        assert got["kind"] == "local"
        assert got["vram_gb"] == 16.0          # 2.0 with texture
        assert got["async"] is True
        assert got["licence"]["code"] == imageto3d.CONDITIONAL

    def test_hunyuan_advertises_glb_only_because_of_a_server_bug(self):
        """The /status route hardcodes a .glb filename while the worker writes
        <uid>.<type>, so asking for obj polls for a file that never appears."""
        assert imageto3d.BACKENDS["hunyuan-local"]["formats"] == ("glb",)

    def test_the_shape_only_figure_is_recorded_separately(self):
        """On a 12 GB card the supported Hunyuan configuration is shape-only,
        and the two numbers have to be distinguishable to say so."""
        spec = imageto3d.BACKENDS["hunyuan-local"]
        assert spec["vram_gb_shape_only"] == 6.0
        assert spec["vram_gb"] > spec["vram_gb_shape_only"]

    def test_the_synchronous_local_backend_has_nothing_to_poll(self):
        assert imageto3d.BACKENDS["trellis-cpp"]["poll_path"] == ""
        assert imageto3d.capabilities("trellis-cpp")["async"] is False

    def test_an_undeclared_licence_is_not_treated_as_permissive(self):
        """trellis.cpp ships no LICENSE file. Absence is all-rights-reserved,
        which is a worse position than an explicit restriction — there is
        nothing to read."""
        licence = imageto3d.BACKENDS["trellis-cpp"]["licence"]
        assert licence["code"] == imageto3d.CONDITIONAL
        assert "no declared licence" in licence["summary"].lower()

    def test_comfy_talks_to_the_api_prefixed_routes(self):
        """ComfyUI registers every route twice; the /api form does not share a
        namespace with the web UI's own SPA routing."""
        spec = imageto3d.BACKENDS["comfy"]
        for key in ("submit_path", "poll_path", "health_path", "upload_path",
                    "view_path"):
            assert spec[key].startswith("/api/"), key


class TestKreaBackend:
    """Krea shipped unreachable, which is the failure this class pins.

    `krea.generate_3d` landed as a Python function that no MCP tool called, so a
    user whose only key is KREA_API_KEY — the key the setup docs tell them to
    configure, the one already sitting in the project's .env — could not produce
    a mesh from a session at all. They needed a Stability/Tripo/Meshy key or a
    local GPU server instead. It is a BACKEND now rather than a bespoke tool, so
    it inherits choose(), the licence gate, the price quote and the common
    result shape instead of reimplementing each.
    """

    def test_krea_is_a_backend(self):
        assert "krea" in imageto3d.BACKENDS
        spec = imageto3d.BACKENDS["krea"]
        assert spec["kind"] == "hosted"
        assert spec["env"] == "KREA_API_KEY"

    def test_it_appears_in_status_with_a_reason(self, no_gpu):
        rows = {b["backend"]: b for b in imageto3d.status()["backends"]}
        assert "krea" in rows, sorted(rows)
        assert rows["krea"]["implemented"] is True
        assert rows["krea"]["available"] is False
        assert "KREA_API_KEY" in rows["krea"]["reason"]

    def test_the_price_comes_from_kreas_own_table(self):
        """One price table. A measurement added in krea.py reaches this quote
        without being copied, and a model nobody has been invoiced for still
        answers None rather than a guess."""
        from bgate_adapters import krea
        assert imageto3d.price_for("krea") == krea.price_for_3d()

    def test_its_licence_is_conditional_so_choose_will_not_take_it(self):
        """Krea runs open-weight models, so the service's terms and the model's
        terms are two different questions. Neither is ours to assume."""
        assert imageto3d.BACKENDS["krea"]["licence"]["code"] == imageto3d.CONDITIONAL

    def test_a_missing_plate_is_refused_before_anything_is_spent(self, monkeypatch):
        monkeypatch.setenv("KREA_API_KEY", "test-key")
        got = imageto3d.generate("no/such/plate.png", "out.glb", backend="krea")
        assert got["ok"] is False
        assert got["stage"] == "input"
        # the quote is attached even on refusal — a caller asking "what would
        # this cost" must get an answer without a successful run
        assert got["usd"] == 0.3


class TestBackendDiscovery:
    """What a caller can LEARN about a backend before committing to it."""

    def test_supports_is_surfaced_per_backend(self, no_gpu):
        """The knobs differ per backend and the difference decides what a user
        can control. hunyuan-local takes face_count; trellis-cpp does not, so
        on trellis-cpp there is no way to ask the generator for less geometry
        and post-generation decimation is the only density lever there is. An
        agent that cannot see this passes options that are silently dropped."""
        rows = {b["backend"]: b for b in imageto3d.status()["backends"]}
        assert "face_count" in rows["hunyuan-local"]["supports"]
        assert "face_count" not in rows["trellis-cpp"]["supports"]
        assert rows["trellis-cpp"]["supports"] == ["resolution", "seed"]

    def test_comfy_without_a_workflow_is_not_available(self, has_gpu, monkeypatch):
        """comfy runs the USER's ComfyUI graph and cannot invent one.

        It used to report available=True with no workflow configured, so
        choose() could hand back a backend that fails at generation time —
        after the server is running and the plate has been paid for.
        """
        monkeypatch.delenv("BGATE_COMFY_WORKFLOW", raising=False)
        row = imageto3d.available("comfy")
        assert row["available"] is False
        assert "BGATE_COMFY_WORKFLOW" in row["reason"]
        assert "API format" in row["reason"]

    def test_comfy_with_a_workflow_that_is_not_there_says_so(self, has_gpu,
                                                             monkeypatch):
        monkeypatch.setenv("BGATE_COMFY_WORKFLOW", "no/such/graph.json")
        row = imageto3d.available("comfy")
        assert row["available"] is False
        assert "not a file" in row["reason"]

    def test_a_backend_needing_no_graph_is_unaffected(self, has_gpu, monkeypatch):
        monkeypatch.delenv("BGATE_COMFY_WORKFLOW", raising=False)
        assert imageto3d.available("trellis-cpp")["available"] is True
