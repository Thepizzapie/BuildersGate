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
from pathlib import Path

import pytest

from bgate_core import envfile, project, providers

ENV = "OPENAI_API_KEY"


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
