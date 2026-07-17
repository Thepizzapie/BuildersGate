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
