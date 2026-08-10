"""The machine-wide key store, and the precedence between the three layers.

    shell environment  >  project .env  >  ~/.bgate/.env

Every test here is about which layer is IN FORCE, because that is the only thing
about this feature that can be silently wrong: a key that fails to save reports
itself, and a key that saved into the layer you did not mean looks exactly like
one that worked.

No test in this file may touch the real ~/.bgate — BGATE_HOME is redirected for
all of them, autouse, because the alternative is a suite that writes credentials
into the developer's own machine-wide store.
"""
from __future__ import annotations

import os

import pytest

from bgate_core import envfile, project, providers

ENV = "OPENAI_API_KEY"


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A fake home and a clean environment, for every test in this file."""
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(ENV, raising=False)
    envfile.reset_cache()
    yield
    envfile.reset_cache()


@pytest.fixture()
def game(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    project.init(str(root), "KeyProbe", dimension="2d")
    return str(root)


def source_of(root=None) -> str:
    return providers.status_for(root, "openai")["source"]


class TestTheStoreItself:
    def test_the_global_env_lives_beside_the_project_registry(self, tmp_path):
        """Same directory the active-project pointer and the registry use. A
        second user-level location would be a second thing to find and back up."""
        assert envfile.global_dir() == project.user_dir()
        assert envfile.global_path() == project.user_dir() / ".env"

    def test_a_global_key_needs_no_project_at_all(self):
        """The whole point. A tool that wants a credential and not a game should
        not have to invent a project to hold one."""
        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        assert os.environ[ENV] == "sk-global-0001"
        assert source_of() == "global_file"
        assert providers.status_for(None, "openai")["configured"] is True

    def test_the_file_is_not_world_readable(self):
        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        mode = envfile.global_path().stat().st_mode & 0o777
        if os.name == "posix":
            assert mode == 0o600, oct(mode)

    def test_a_project_write_still_needs_a_project(self):
        with pytest.raises(providers.ProviderError, match="no project here"):
            providers.set_key(None, "openai", "sk-x", scope="project")

    def test_an_unknown_scope_is_refused_rather_than_guessed(self, game):
        """A key in the wrong file and a key that did not save look identical
        from the outside, and only one of them is fixed by trying again."""
        with pytest.raises(providers.ProviderError, match="unknown scope"):
            providers.set_key(game, "openai", "sk-x", scope="glob@l")
        assert ENV not in os.environ


class TestPrecedence:
    def test_the_project_beats_the_global_store(self, game):
        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        providers.set_key(game, "openai", "sk-project-0002", scope="project")
        assert os.environ[ENV] == "sk-project-0002"
        assert source_of(game) == "env_file"

    def test_writing_the_global_key_does_not_stamp_over_a_project_key(self, game):
        """Order of writes must not change the outcome. Assigning the value that
        was just written — the obvious single-store implementation — gets this
        backwards and the process then disagrees with the documented rule."""
        providers.set_key(game, "openai", "sk-project-0002", scope="project")
        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        assert os.environ[ENV] == "sk-project-0002"
        assert source_of(game) == "env_file"

    def test_clearing_the_project_key_uncovers_the_global_one(self, game):
        """The failure this is here to stop: "remove this project's override"
        reading as "break generation here" until someone restarts the server."""
        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        providers.set_key(game, "openai", "sk-project-0002", scope="project")
        providers.clear_key(game, "openai", scope="project")
        assert os.environ[ENV] == "sk-global-0001"
        assert source_of(game) == "global_file"

    def test_clearing_both_leaves_nothing_live(self, game):
        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        providers.set_key(game, "openai", "sk-project-0002", scope="project")
        providers.clear_key(game, "openai", scope="project")
        providers.clear_key(game, "openai", scope="global")
        assert ENV not in os.environ
        assert source_of(game) == "unset"

    def test_a_shell_export_beats_both_files(self, game, monkeypatch):
        """Predates the global store and must survive it: a save that silently
        overwrote a shell export would make the panel agree with itself and
        disagree with the process."""
        monkeypatch.setenv(ENV, "sk-shell-9999")
        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        providers.set_key(game, "openai", "sk-project-0002", scope="project")
        assert os.environ[ENV] == "sk-shell-9999"
        assert source_of(game) == "environment"

    def test_clearing_does_not_delete_a_shell_export(self, game, monkeypatch):
        monkeypatch.setenv(ENV, "sk-shell-9999")
        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        providers.clear_key(game, "openai", scope="global")
        assert os.environ[ENV] == "sk-shell-9999"

    def test_load_env_applies_the_layers_in_order(self, game):
        envfile.write_var(envfile.global_dir(), ENV, "sk-global-0001")
        envfile.write_var(game, ENV, "sk-project-0002")
        envfile.reset_cache()
        os.environ.pop(ENV, None)
        loaded = envfile.load_env(game)
        assert os.environ[ENV] == "sk-project-0002"
        assert ENV in loaded["project"]
        # The global layer was READ and declined to overwrite; it reports the
        # keys it applied, and this was not one of them.
        assert ENV not in loaded["global"]

    def test_load_env_with_no_project_reads_the_global_layer_alone(self):
        envfile.write_var(envfile.global_dir(), ENV, "sk-global-0001")
        envfile.reset_cache()
        os.environ.pop(ENV, None)
        loaded = envfile.load_env(None)
        assert loaded["project"] == []
        assert ENV in loaded["global"]
        assert os.environ[ENV] == "sk-global-0001"


class TestWhatTheStatusRowSays:
    def test_scope_names_the_store_a_change_would_edit(self, game):
        """Not the same question as `source`. A key inherited from the global
        file answers "global" so a panel edits the file the value comes from
        rather than shadowing it with a copy the user forgets exists."""
        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        row = providers.status_for(game, "openai")
        assert row["scope"] == "global"
        assert row["in_global_file"] is True
        assert row["in_env_file"] is False

        providers.set_key(game, "openai", "sk-project-0002", scope="project")
        row = providers.status_for(game, "openai")
        assert row["scope"] == "project"
        assert row["in_env_file"] and row["in_global_file"]

    def test_status_never_returns_a_key(self, game):
        providers.set_key(game, "openai", "sk-project-0002", scope="project")
        blob = repr(providers.status(game))
        assert "sk-project-0002" not in blob
        assert providers.status_for(game, "openai")["last4"] == "0002"

    def test_status_works_with_no_project(self):
        """"What can this machine generate" had a right answer before any
        particular game existed, and used to be unreachable without one."""
        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        rows = providers.status(None)
        assert {r["id"] for r in rows} == set(providers.ids())
        assert "openai" in providers.configured(None)

    def test_the_global_store_is_not_a_git_leak_risk(self):
        """~/.bgate is not a repository, so there is nothing there to leak a key
        into — the gitignore guard answers that correctly rather than being
        skipped for the global path."""
        assert providers.env_is_ignored(envfile.global_dir()) is True


class TestTheScratchDropPoint:
    """Where a generation goes when it belongs to no game.

    A real project, because everything downstream of a generation needs one —
    the artifact registry, the spend ledger, `.bgate_out`. A bare output folder
    would be a second, thinner path for "work that is not part of anything", and
    the first question anyone asks of one is what it cost.
    """

    def test_it_is_not_created_until_something_needs_it(self):
        """A user who never generates outside a project never gets a directory
        they did not ask for."""
        assert not (project.scratch_root(create=False) / ".bgate").exists()

    def test_creating_it_makes_a_real_project(self):
        root = project.scratch_root()
        assert (root / ".bgate" / "game.db").exists()
        assert project.get(root)["name"] == project.SCRATCH_LABEL
        assert project.is_scratch(root)

    def test_creating_it_twice_is_the_same_project(self):
        """Idempotent, and NOT re-initialised: re-running init would rewrite the
        name and pitch, so a second generation must not quietly reset a database
        the first one has already put artifacts in."""
        first = project.scratch_root()
        marker = project.set_dimension(first, "3d")["dimension"]
        second = project.scratch_root()
        assert first == second
        assert project.get(second)["dimension"] == marker

    def test_it_carries_no_game(self):
        """A drop point for images, not a place to build. A tool that needs an
        engine should say so in its own words rather than find a stub."""
        root = project.scratch_root()
        assert project.game_dir(root) is None

    def test_it_is_the_bottom_of_the_chain_not_the_top(self, game, tmp_path):
        """Anyone who has a project keeps landing in it. Scratch only ever
        catches someone who has none — otherwise a mistyped directory quietly
        fills a folder nobody looks in."""
        assert str(project.require_root(game, scratch=True)) == game

        project.set_active(game)
        try:
            got = project.require_root(str(tmp_path / "nowhere"), scratch=True)
            assert str(got) == game, "the remembered project must still win"
        finally:
            project.clear_active()

    def test_without_the_flag_it_still_refuses(self, tmp_path):
        """The default is unchanged: a caller that has no business inventing a
        destination still gets the error it always got."""
        with pytest.raises(LookupError):
            project.require_root(str(tmp_path / "nowhere"))

    def test_the_alias_is_how_you_ASK_for_it(self):
        for token in ("scratch", "global", "SCRATCH", " global "):
            assert project.resolve_alias(token) == project.scratch_root()
        assert project.resolve_alias("my-game") is None
        assert project.resolve_alias("") is None


class TestTheScratchDropPointOnTheMcpSurface:
    def test_generation_tools_fall_back_and_others_do_not(self, tmp_path,
                                                          monkeypatch):
        from bgate_mcp import server

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("BGATE_ROOT", raising=False)
        # _root() is what every non-generation tool calls, and it must keep
        # refusing: godot_run against an empty scratch project is a confusing
        # failure a long way from its cause.
        with pytest.raises(LookupError):
            server._root()
        assert str(project.scratch_root(create=False)) == \
            server._scratch_root()

    def test_the_alias_works_as_project_dir(self, tmp_path, monkeypatch):
        from bgate_mcp import server

        monkeypatch.chdir(tmp_path)
        token = server._CALL_ROOT.set("scratch")
        try:
            assert server._root() == str(project.scratch_root())
        finally:
            server._CALL_ROOT.reset(token)

    @pytest.mark.anyio
    async def test_project_status_says_when_it_is_the_scratch_one(self,
                                                                  monkeypatch):
        """Otherwise "where did my sprite sheet go" has an answer nothing on any
        surface states, and the honest one is a directory under ~/.bgate that
        was created for you."""
        import json

        from bgate_mcp import server

        monkeypatch.setenv("BGATE_ROOT", str(project.scratch_root()))
        result = await server.mcp.call_tool("project_status", {})
        content = result[0] if isinstance(result, tuple) else result
        got = json.loads(content[0].text)
        assert got["scratch"] is True
        assert got["project"]["name"] == project.SCRATCH_LABEL


class TestTheCli:
    def test_listing_works_outside_a_project(self, capsys):
        from bgate_cli.main import keys

        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        assert keys("list") == 0
        out = capsys.readouterr().out
        assert "sk-global-0001" not in out, "the CLI printed a key"
        assert "0001" in out
        assert str(envfile.global_path()) in out

    def test_clear_round_trips_through_the_cli(self, capsys):
        from bgate_cli.main import keys

        providers.set_key(None, "openai", "sk-global-0001", scope="global")
        assert keys("clear", "openai", use_global=True) == 0
        assert ENV not in os.environ

    def test_an_unknown_provider_lists_the_real_ones(self, capsys):
        from bgate_cli.main import keys

        assert keys("set", "openai-typo") == 2
        assert "known" in capsys.readouterr().out.lower()
