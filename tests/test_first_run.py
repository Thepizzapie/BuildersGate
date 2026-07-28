"""First run — the path from nothing to a project.

The audit's single blocker: every way into the product assumed a project that
already existed. These tests pin the three things that were missing — an
/api/state that can say "no project" without erroring the page out, an HTTP
create, and a CLI that prints the absolute path it wrote to.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_cli import main as cli
from bgate_core import db, project
from bgate_ui.app import app


@pytest.fixture()
def empty(tmp_path, monkeypatch):
    """A machine with no project: the server points at a bare directory."""
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    db.close_all()


@pytest.fixture()
def client(empty):
    return TestClient(app)


def data(response) -> dict:
    body = response.json()
    assert body["ok"] is True, body
    return body["data"]


class TestStateWithoutAProject:
    def test_answers_200_with_a_null_project_and_a_hint(self, client):
        response = client.get("/api/state")
        assert response.status_code == 200      # NOT 503 — the page has to render
        body = response.json()
        assert body["project"] is None
        assert body["hint"]
        assert "bgate init" in body["hint"]

    def test_carries_empty_collections_so_the_pollers_do_not_break(self, client):
        body = client.get("/api/state").json()
        for key in ("seats", "assets", "artifacts", "sessions", "previews"):
            assert body[key] == []

    def test_project_endpoint_reports_where_it_would_create_one(self, client, empty):
        got = data(client.get("/api/project"))
        assert got["project"] is None
        assert got["cwd"] == str(empty)
        assert "2d" in got["kinds"]


class TestCreateOverHttp:
    def test_creates_scaffolds_and_flips_the_state(self, client, empty, monkeypatch):
        monkeypatch.delenv("BGATE_ROOT")   # let the route point the server itself
        made = data(client.post("/api/project",
                                json={"name": "Ember Run", "kind": "2d",
                                      "pitch": "one long fall"}))
        root = empty / "ember-run"
        assert made["root"] == str(root)
        assert made["project"]["name"] == "Ember Run"
        assert made["files"] > 0
        assert (root / ".bgate" / "game.db").exists()
        assert (root / "project.godot").exists()

        # The same poll that reported nothing now reports the real project.
        state = client.get("/api/state").json()
        assert state["project"]["name"] == "Ember Run"
        assert state["root"] == str(root)
        assert len(state["seats"]) == 7

    def test_3d_template_sets_the_dimension(self, client, monkeypatch):
        monkeypatch.delenv("BGATE_ROOT")
        made = data(client.post("/api/project", json={"name": "Deep", "kind": "3d"}))
        assert made["project"]["dimension"] == "3d"

    def test_a_nameless_project_is_refused(self, client):
        body = client.post("/api/project", json={"kind": "2d"}).json()
        assert body["ok"] is False
        assert body["error"]["code"] == "bad_request"

    def test_an_unknown_template_is_refused(self, client):
        body = client.post("/api/project", json={"name": "x", "kind": "isometric"}).json()
        assert body["error"]["code"] == "bad_request"

    def test_an_occupied_directory_is_a_conflict_not_a_stomp(self, client, empty,
                                                             monkeypatch):
        monkeypatch.delenv("BGATE_ROOT")
        target = empty / "taken"
        target.mkdir()
        (target / "notes.txt").write_text("someone's work", encoding="utf-8")
        body = client.post("/api/project",
                           json={"name": "Taken", "kind": "2d"}).json()
        assert body["ok"] is False
        assert body["error"]["code"] == "conflict"
        assert body["error"]["detail"]["force_available"] is True
        assert (target / "notes.txt").read_text(encoding="utf-8") == "someone's work"

    def test_an_agent_may_not_create_the_studios_project(self, client, monkeypatch):
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-3")
        body = client.post("/api/project", json={"name": "Sneaky"}).json()
        assert body["error"]["code"] == "forbidden"


class TestCli:
    def test_init_prints_the_absolute_root_it_created(self, empty, capsys):
        code = cli.init_project("Ember Run", kind="2d")
        assert code == 0
        printed = capsys.readouterr().out
        root = empty / "ember-run"

        # The path is the whole point of the command — it must be absolute and
        # on a line of its own, greppable by a setup script.
        assert str(root) in printed
        assert any(line.strip() == str(root) for line in printed.splitlines())
        assert (root / ".bgate" / "game.db").exists()
        assert project.get(root)["name"] == "Ember Run"

    def test_argv_parsing_honours_kind_and_dir(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        dest = tmp_path / "elsewhere" / "deep"
        monkeypatch.setattr(cli.sys, "argv",
                            ["bgate", "init", "Deep Dive", "--kind", "3d",
                             "--dir", str(dest)])
        assert cli.main() == 0
        assert str(dest.resolve()) in capsys.readouterr().out
        assert project.get(dest)["dimension"] == "3d"
        db.close_all()

    def test_a_bad_kind_exits_nonzero_without_writing(self, empty, capsys):
        code = cli.init_project("Nope", kind="isometric")
        assert code == 2
        assert not (empty / "nope").exists()

    def test_init_is_listed_in_the_help(self, capsys, monkeypatch):
        monkeypatch.setattr(cli.sys, "argv", ["bgate"])
        cli.main()
        assert "bgate init" in capsys.readouterr().out


class TestServeBanner:
    """`python -m bgate_ui` printed nothing at all, so the command that starts
    the product looked like a hang."""

    def _run(self, monkeypatch, capsys) -> str:
        import uvicorn

        from bgate_ui import app as app_module

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        app_module.serve(port=7999)
        return capsys.readouterr().out

    def test_prints_the_url_and_the_project(self, root, monkeypatch, capsys):
        monkeypatch.setenv("BGATE_ROOT", str(root))
        printed = self._run(monkeypatch, capsys)
        assert "http://127.0.0.1:7999" in printed
        assert str(root) in printed

    def test_says_how_to_make_one_when_there_is_none(self, empty, monkeypatch, capsys):
        monkeypatch.delenv("BGATE_ROOT")
        printed = self._run(monkeypatch, capsys)
        assert "http://127.0.0.1:7999" in printed
        assert "bgate init" in printed
