"""The art pane's Style tab: training a house style is a run the panel watches.

The four /api/art/style routes had no caller until the Style tab. What the tab
needs from them is a run it can follow: the stage the adapter is on while it
runs, and the error kept on screen when it fails. No network: `krea.train` is
stubbed.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from bgate_core.art import refs, styles
from bgate_core.store import settings

REPO = Path(__file__).resolve().parents[2]


def _png(path, w=1024, h=1024):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), (12, 12, 16)).save(path)
    return str(path)


@pytest.fixture()
def anchors(root, tmp_path):
    for i in range(6):
        refs.pin(root, f"concept-{i}", _png(tmp_path / f"c{i}.png"))
    return root


@pytest.fixture()
def client(root, monkeypatch):
    from fastapi.testclient import TestClient
    from bgate_ui.app import app
    from bgate_ui.routes import art_style
    monkeypatch.setenv("BGATE_ROOT", str(root))
    monkeypatch.setenv("KREA_API_KEY", "test-key")
    art_style._running.clear()
    yield TestClient(app)
    art_style._running.clear()


def _running(client) -> dict:
    got = client.get("/api/art/style").json()
    return (got.get("data") or got)["running"]


def _until(client, status: str) -> dict:
    for _ in range(100):
        run = _running(client)
        if run.get("status") == status:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run never reached {status}: {_running(client)}")


class TestARunThePanelCanFollow:
    def test_the_stage_is_readable_while_it_runs_then_it_lands(self, client,
                                                                anchors, monkeypatch):
        from bgate_adapters import krea
        release = threading.Event()

        def train(name, paths, *, on_stage=None, **kw):
            on_stage({"stage": "uploading", "done": 2, "total": len(paths)})
            on_stage({"stage": "training", "job_id": "job-7", "total": len(paths)})
            release.wait(5)
            return {"ok": True, "style_id": "s-7", "job_id": "job-7",
                    "images": len(paths)}

        monkeypatch.setattr(krea, "train", train)
        assert client.post("/api/art/style/train",
                           json={"name": "Neon"}).status_code == 200
        for _ in range(100):
            run = _running(client)
            if run.get("stage") == "training":
                break
            time.sleep(0.02)
        assert run["status"] == "running" and run["job_id"] == "job-7"
        assert run["total"] == 6 and run["started"] > 0

        release.set()
        run = _until(client, "done")
        assert run["style_id"] == "s-7" and run["ended"] >= run["started"]
        data = client.get("/api/art/style").json()
        data = data.get("data") or data
        assert data["active"]["style_id"] == "s-7"

    def test_a_failed_run_keeps_its_reason_and_where_it_stopped(self, client,
                                                                 anchors, monkeypatch):
        from bgate_adapters import krea

        def train(name, paths, *, on_stage=None, **kw):
            on_stage({"stage": "uploading", "done": 3, "total": len(paths)})
            return {"ok": False, "error": "Krea upload refused (HTTP 402)"}

        monkeypatch.setattr(krea, "train", train)
        client.post("/api/art/style/train", json={"name": "Broke"})
        run = _until(client, "failed")
        assert run["error"] == "Krea upload refused (HTTP 402)"
        assert run["stage"] == "uploading" and run["done"] == 3

    def test_training_reads_the_shelf_the_panel_shows(self, client, anchors,
                                                      monkeypatch):
        """The panel's dataset follows art.style_dataset; the route used to
        always train on pins, so the anchors shown and the ones uploaded could
        be two different sets."""
        from bgate_adapters import krea
        settings.set(anchors, "art.style_dataset", "both")
        seen: list = []
        real = styles.dataset
        monkeypatch.setattr(styles, "dataset",
                            lambda root, names=None, source="pins":
                            seen.append(source) or real(root, names, source=source))
        monkeypatch.setattr(krea, "train", lambda name, paths, **kw:
                            {"ok": True, "style_id": "s1", "images": len(paths)})
        client.post("/api/art/style/train", json={"name": "Both"})
        assert seen == ["both"]


class TestTheAdapterReportsItsStages:
    def test_a_raising_callback_never_fails_the_run(self, tmp_path, monkeypatch):
        from bgate_adapters import krea
        paths = [_png(tmp_path / f"a{i}.png") for i in range(5)]
        monkeypatch.setattr(krea, "upload",
                            lambda p, **kw: {"image_url": f"https://x/{Path(p).name}"})
        monkeypatch.setattr(krea, "train_style", lambda *a, **kw: {"job_id": "j1"})
        monkeypatch.setattr(krea, "poll",
                            lambda *a, **kw: {"result": {"style_id": "s1"}})
        heard: list = []

        def on_stage(info):
            heard.append(info["stage"])
            raise RuntimeError("panel went away")

        got = krea.train("Neon", paths, on_stage=on_stage)
        assert got["ok"] is True and got["style_id"] == "s1"
        assert heard == ["uploading"] * 5 + ["training"]


class TestTheTabIsWired:
    def _src(self, *parts: str) -> str:
        return (REPO.joinpath(*parts)).read_text(encoding="utf-8")

    def test_the_tab_calls_all_four_routes(self):
        src = self._src("frontend", "src", "shell", "seats", "ArtStyle.tsx")
        assert '"/api/art/style"' in src
        assert '"/api/art/style/train"' in src
        assert '"/api/art/style/active"' in src
        assert 'method: "DELETE"' in src

    def test_the_art_seat_has_a_style_tab(self):
        assert 'id: "style"' in self._src("frontend", "src", "shell", "seats", "Seats.tsx")
        assert 'tab === "style"' in self._src("frontend", "src", "shell", "seats", "Art.tsx")

    def test_the_built_bundle_carries_it(self):
        dist = REPO / "src" / "bgate_ui" / "static" / "dist"
        assert any("/api/art/style/train" in p.read_text(encoding="utf-8")
                   for p in dist.glob("*.js"))
