"""Env loading + the painted-art adapter.

The .env loader is fully tested (it guards a SECRET — precedence and non-logging
matter). The API call itself is tested only when a key is present, and with the
cheapest possible request.
"""
from __future__ import annotations

import os

import pytest

from bgate_adapters import imagegen
from bgate_core import envfile


@pytest.fixture(autouse=True)
def fresh_cache():
    envfile.reset_cache()
    yield
    envfile.reset_cache()


class TestEnvFile:
    def test_loads_keys(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BGATE_TEST_SECRET", raising=False)
        (tmp_path / ".env").write_text(
            "# comment\n\nBGATE_TEST_SECRET=hunter2\nBGATE_OTHER='quoted'\n",
            encoding="utf-8")
        loaded = envfile.load_project_env(tmp_path)
        assert "BGATE_TEST_SECRET" in loaded
        assert os.environ["BGATE_TEST_SECRET"] == "hunter2"
        assert os.environ["BGATE_OTHER"] == "quoted"
        for k in ("BGATE_TEST_SECRET", "BGATE_OTHER"):
            os.environ.pop(k, None)

    def test_shell_env_wins_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BGATE_TEST_SECRET", "from-shell")
        (tmp_path / ".env").write_text("BGATE_TEST_SECRET=from-file", encoding="utf-8")
        loaded = envfile.load_project_env(tmp_path)
        assert loaded == []
        assert os.environ["BGATE_TEST_SECRET"] == "from-shell"

    def test_returns_keys_never_values(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BGATE_TEST_SECRET", raising=False)
        (tmp_path / ".env").write_text("BGATE_TEST_SECRET=sk-super-secret",
                                       encoding="utf-8")
        loaded = envfile.load_project_env(tmp_path)
        assert "sk-super-secret" not in str(loaded)
        os.environ.pop("BGATE_TEST_SECRET", None)

    def test_blank_values_and_missing_file_are_fine(self, tmp_path):
        (tmp_path / ".env").write_text("EMPTY=\n#c\n", encoding="utf-8")
        assert envfile.load_project_env(tmp_path) == []
        assert envfile.load_project_env(tmp_path / "nowhere") == []

    def test_loads_once_per_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BGATE_TEST_SECRET", raising=False)
        env = tmp_path / ".env"
        env.write_text("BGATE_TEST_SECRET=v1", encoding="utf-8")
        envfile.load_project_env(tmp_path)
        env.write_text("BGATE_TEST_SECRET=v2", encoding="utf-8")
        envfile.load_project_env(tmp_path)  # cached — no reload
        assert os.environ["BGATE_TEST_SECRET"] == "v1"
        os.environ.pop("BGATE_TEST_SECRET", None)


class TestModelRouting:
    """The mode picks the model — learned in production: gpt-image-2 rejects
    transparency and was flaky on sprites; gpt-image-1 owns alpha work."""

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        for var in ("BGATE_IMAGE_MODEL", "BGATE_IMAGE_MODEL_TRANSPARENT",
                    "BGATE_IMAGE_MODEL_OPAQUE"):
            monkeypatch.delenv(var, raising=False)

    def test_transparent_routes_to_image_1(self):
        assert imagegen._model_for(True) == "gpt-image-1"

    def test_opaque_routes_to_image_2(self):
        assert imagegen._model_for(False) == "gpt-image-2"

    def test_global_override_forces_both(self, monkeypatch):
        monkeypatch.setenv("BGATE_IMAGE_MODEL", "gpt-image-1")
        assert imagegen._model_for(True) == "gpt-image-1"
        assert imagegen._model_for(False) == "gpt-image-1"

    def test_per_mode_overrides(self, monkeypatch):
        monkeypatch.setenv("BGATE_IMAGE_MODEL_OPAQUE", "gpt-image-3")
        assert imagegen._model_for(False) == "gpt-image-3"
        assert imagegen._model_for(True) == "gpt-image-1"

    def test_available_reports_both_routes(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
        got = imagegen.available()
        assert got["model_transparent"] == "gpt-image-1"
        assert got["model_opaque"] == "gpt-image-2"


class TestRoutingReachesTheApi:
    """Regression: the routing refactor changed available()'s shape and edit()
    kept reading probe['model'] — every edit() call raised KeyError. These
    tests drive BOTH entry points through a stubbed client all the way to the
    API call, so a routing/shape change that breaks one path fails loudly.
    """

    @pytest.fixture()
    def stub(self, tmp_path, monkeypatch):
        import base64
        import types

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
        captured = {}

        class _Images:
            def generate(self, **kw):
                captured["generate"] = kw
                return types.SimpleNamespace(data=[types.SimpleNamespace(
                    b64_json=base64.b64encode(b"\x89PNG\r\n\x1a\nstub").decode(),
                    revised_prompt=None)])

            def edit(self, **kw):
                captured["edit"] = kw
                return self.generate()

        class _Client:
            def __init__(self, timeout=None):
                self.images = _Images()

        monkeypatch.setattr("openai.OpenAI", _Client)
        ref = tmp_path / "ref.png"
        ref.write_bytes(b"\x89PNG\r\n\x1a\nref")
        return {"captured": captured, "ref": ref, "tmp": tmp_path}

    def test_generate_routes_and_saves(self, stub):
        got = imagegen.generate("x", str(stub["tmp"] / "g.png"), transparent=True)
        assert got["ok"] is True, got
        assert stub["captured"]["generate"]["model"] == "gpt-image-1"
        got2 = imagegen.generate("x", str(stub["tmp"] / "g2.png"), transparent=False)
        assert got2["model"] == "gpt-image-2"

    def test_edit_routes_and_saves(self, stub):
        """The path the KeyError killed — must reach the API and save."""
        got = imagegen.edit("x", [str(stub["ref"])], str(stub["tmp"] / "e.png"),
                            transparent=True)
        assert got["ok"] is True, got
        assert stub["captured"]["edit"]["model"] == "gpt-image-1"
        assert got["model"] == "gpt-image-1"
        assert (stub["tmp"] / "e.png").exists()

    def test_edit_opaque_routes_to_image_2(self, stub):
        got = imagegen.edit("x", [str(stub["ref"])], str(stub["tmp"] / "e2.png"),
                            transparent=False)
        assert got["ok"] is True
        assert stub["captured"]["edit"]["model"] == "gpt-image-2"


class TestSingleFrameEnforcement:
    """USER RULE: one frame per generation. Multi-pose sheet prompts are where
    gpt-image loses the character — refused at the adapter, not advised."""

    @pytest.mark.parametrize("prompt", [
        "a sprite sheet of the character",
        "one row of six poses, evenly spaced",
        "generate 4 frames of a walk cycle",
        "character turnaround sheet, front and side",
        "three stances left to right",
    ])
    def test_multi_pose_prompts_refused(self, prompt, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
        got = imagegen.generate(prompt, str(tmp_path / "x.png"))
        assert got["ok"] is False
        assert "image_sprites" in got["error"]

    def test_edit_is_guarded_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
        ref = tmp_path / "r.png"
        ref.write_bytes(b"\x89PNG\r\n\x1a\nx")
        got = imagegen.edit("this character in a pose grid", [str(ref)],
                            str(tmp_path / "x.png"))
        assert got["ok"] is False
        assert "image_sprites" in got["error"]

    def test_single_pose_prompts_pass_the_guard(self):
        for prompt in ("the character throwing a single jab",
                       "a market colosseum backdrop at night",
                       "portrait of a tomato boxer"):
            assert imagegen._reject_multi_pose(prompt, False) is None

    def test_allow_multi_overrides_for_legit_group_art(self):
        assert imagegen._reject_multi_pose(
            "roster splash with three stances", True) is None


class TestAdapter:
    def test_available_reports_missing_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        got = imagegen.available()
        assert got["available"] is False
        assert ".env" in got["reason"]

    def test_bad_size_rejected(self):
        with pytest.raises(ValueError, match="size"):
            imagegen.generate("x", "out.png", size="640x360")

    def test_generate_without_key_is_an_error_result(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        got = imagegen.generate("a red tomato", str(tmp_path / "t.png"))
        assert got["ok"] is False


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"),
                    reason="no OPENAI_API_KEY — set it to run the live API test")
class TestLiveGeneration:
    def test_generates_a_real_png(self, tmp_path):
        got = imagegen.generate(
            "flat solid orange circle on white, minimal test image",
            str(tmp_path / "probe.png"), size="1024x1024", quality="low")
        assert got["ok"] is True, got.get("error")
        data = (tmp_path / "probe.png").read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
