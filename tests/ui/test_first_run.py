"""First run — the path from nothing to a project.

The audit's single blocker: every way into the product assumed a project that
already existed. These tests pin the three things that were missing — an
/api/state that can say "no project" without erroring the page out, an HTTP
create, and a CLI that prints the absolute path it wrote to.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bgate_cli import main as cli
from bgate_core.board import gitwork
from bgate_core.store import db, project
from bgate_core.board import seats
from bgate_ui.app import app
from bgate_ui.routes import project as project_routes


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


class TestWhereANewProjectLands:
    """`bgate serve` is run from a directory you chose. A double-clicked
    executable is not: a shortcut with no "Start in", or a launch from the Run
    dialog, hands the process C:\\Windows\\system32 as its cwd. The first-run
    screen read that straight out and offered to unpack a Godot game into it."""

    def test_a_writable_working_directory_is_used_as_is(self, empty, monkeypatch):
        monkeypatch.chdir(empty)
        assert project_routes.default_parent() == empty

    def test_an_active_projects_parent_is_used_for_the_next_game(
            self, monkeypatch, tmp_path):
        machine_home = tmp_path / "machine-home"
        machine_home.mkdir()
        monkeypatch.setenv("BGATE_HOME", str(machine_home))
        current = tmp_path / "builders-gate"
        project.init(current, "Builders Gate")
        monkeypatch.setenv("BGATE_ROOT", str(current))
        monkeypatch.chdir(current)

        assert project_routes.default_parent() == tmp_path
        assert project_routes._target("Hot Cargo", "") == tmp_path / "hot-cargo"

    @pytest.mark.parametrize("var,leaf", [
        ("SystemRoot", "System32"),
        ("ProgramFiles", "SomeApp"),
        ("ProgramData", "SomeApp"),
    ])
    def test_system_locations_are_refused(self, var, leaf, monkeypatch, tmp_path):
        base = tmp_path / var
        (base / leaf).mkdir(parents=True)
        monkeypatch.setenv(var, str(base))
        assert project_routes._unsuitable(base / leaf) is True

    def test_a_drive_root_is_refused(self):
        root = Path(Path.cwd().anchor or "/")
        assert project_routes._unsuitable(root) is True

    def test_the_fallback_is_under_home_and_is_not_created_by_reading(
            self, monkeypatch, tmp_path):
        """Rendering the form must not touch the disk."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(project_routes, "_unsuitable", lambda d: True)

        got = project_routes.default_parent()
        assert got == home / "BuildersGate"
        assert not got.exists()


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
        assert made["repository"]["available"] is True
        assert gitwork.probe(root)["available"] is True

        # The same poll that reported nothing now reports the real project.
        state = client.get("/api/state").json()
        assert state["project"]["name"] == "Ember Run"
        assert state["root"] == str(root)
        assert len(state["seats"]) == len(seats.ROLES)
        # And the machine-wide pointer agrees with the running server, so the
        # SessionStart hook and `bgate use` name the same game the console does.
        from bgate_core.store import project as _project
        assert Path(_project.active_root()).resolve() == root.resolve()
        assert str(root) in {str(Path(p)) for p in _project.known_projects().values()}

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


class TestOpeningOneThatExists:
    """The first-run screen could only CREATE.

    Someone with six registered games who opened the dashboard from the wrong
    directory was told there was no project and invited to make a seventh — and
    the registry the screen needed was already in the GET's ``known``.
    """

    @pytest.fixture()
    def elsewhere(self, tmp_path, monkeypatch):
        """A real project somewhere the server is NOT pointing."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("BGATE_HOME", str(home))   # registry + active pointer
        made = tmp_path / "other-game"
        project.init(made, "Other Game")
        return made

    def test_a_path_repoints_the_running_server(self, client, elsewhere):
        got = data(client.post("/api/project/select", json={"root": str(elsewhere)}))
        assert got["root"] == str(elsewhere)
        assert got["project"]["name"] == "Other Game"
        # reload, because the dashboard token is minted per project and this
        # page was served against the old one.
        assert got["reload"] is True
        # and the SERVER now agrees, which is the whole point — the old screen
        # could not do this at all.
        assert client.get("/api/state").json()["project"]["name"] == "Other Game"

    def test_a_registry_name_works_too(self, client, elsewhere):
        got = data(client.post("/api/project/select", json={"name": "other-game"}))
        assert got["root"] == str(elsewhere)

    def test_the_choice_survives_a_restart(self, client, elsewhere):
        client.post("/api/project/select", json={"root": str(elsewhere)})
        assert project.active_root() == elsewhere

    def test_a_directory_with_no_store_is_refused_by_name(self, client, tmp_path):
        bare = tmp_path / "just-a-folder"
        bare.mkdir()
        body = client.post("/api/project/select", json={"root": str(bare)}).json()
        assert body["ok"] is False
        assert body["error"]["code"] == "bad_request"
        # the message has to say what to do, not just what is wrong
        assert "adopt" in body["error"]["message"]

    def test_an_unregistered_name_lists_what_is_registered(self, client, elsewhere):
        body = client.post("/api/project/select", json={"name": "nope"}).json()
        assert body["error"]["code"] == "bad_request"
        assert body["error"]["detail"]["known"] == ["other-game"]

    def test_an_agent_may_not_repoint_the_dashboard(self, client, elsewhere,
                                                    monkeypatch):
        # An agent that can switch projects can write into another game through
        # every other route on this server.
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-3")
        body = client.post("/api/project/select",
                           json={"root": str(elsewhere)}).json()
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
