"""Cross-project key bleed, and the ownership tracking that ends it.

envfile loads a project's .env into process-global os.environ. Before ownership
tracking, its never-overwrite rule meant the FIRST project to load owned every
shared name (OPENAI_API_KEY, ...) for the life of the process — so a long-lived
server (the MCP server with a project_dir per call, the dashboard after a
project switch) sent project A's credentials on project B's behalf. These tests
pin the fixed semantics:

    shell environment  >  THIS project's .env  >  ~/.bgate/.env

with "this project" meaning the one being loaded NOW, not the one that got
there first. Vars the shell set are never touched.
"""
from __future__ import annotations

import os

import pytest

from bgate_core import envfile

ENV = "OPENAI_API_KEY"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A fake home, a clean environment, and no ownership carried in from
    another test file's loads."""
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(ENV, raising=False)
    envfile.reset_cache()
    saved = dict(envfile._owned)
    envfile._owned.clear()
    yield
    envfile._owned.clear()
    envfile._owned.update(saved)
    envfile.reset_cache()


@pytest.fixture()
def two_projects(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    return a, b


class TestProjectSwitching:
    def test_switching_projects_repoints_a_project_scoped_key(self, two_projects):
        """The bleed itself: the first project's key must not win forever."""
        a, b = two_projects
        envfile.write_var(a, ENV, "sk-project-a")
        envfile.write_var(b, ENV, "sk-project-b")

        envfile.load_env(a)
        assert os.environ[ENV] == "sk-project-a"
        envfile.load_env(b)
        assert os.environ[ENV] == "sk-project-b"
        envfile.load_env(a)
        assert os.environ[ENV] == "sk-project-a"

    def test_switching_to_a_keyless_project_falls_back_to_global(self, two_projects):
        """A stale project var must UNCOVER the global layer, not shadow it."""
        a, b = two_projects
        envfile.write_var(envfile.global_dir(), ENV, "sk-global-0001")
        envfile.write_var(a, ENV, "sk-project-a")

        envfile.load_env(a)
        assert os.environ[ENV] == "sk-project-a"
        envfile.load_env(b)
        assert os.environ[ENV] == "sk-global-0001"

    def test_switching_to_a_project_with_no_key_anywhere_unsets_it(self, two_projects):
        """Project B without a key must not silently bill project A's account."""
        a, b = two_projects
        envfile.write_var(a, ENV, "sk-project-a")

        envfile.load_env(a)
        assert os.environ[ENV] == "sk-project-a"
        envfile.load_env(b)
        assert ENV not in os.environ

    def test_the_global_layer_survives_every_switch(self, two_projects):
        """~/.bgate/.env is the shared fallback; a switch must not evict it."""
        a, b = two_projects
        envfile.write_var(envfile.global_dir(), ENV, "sk-global-0001")

        envfile.load_env(a)
        envfile.load_env(b)
        envfile.load_env(a)
        assert os.environ[ENV] == "sk-global-0001"

    def test_direct_load_project_env_also_repoints(self, two_projects):
        """The adapters call load_project_env directly, not load_env; the
        project layer must beat another project's stale value on that path too."""
        a, b = two_projects
        envfile.write_var(a, ENV, "sk-project-a")
        envfile.write_var(b, ENV, "sk-project-b")

        envfile.load_project_env(a)
        assert os.environ[ENV] == "sk-project-a"
        assert ENV in envfile.load_project_env(b)
        assert os.environ[ENV] == "sk-project-b"


class TestTheShellStillWins:
    def test_a_shell_export_survives_project_switches(self, two_projects,
                                                      monkeypatch):
        a, b = two_projects
        monkeypatch.setenv(ENV, "sk-shell-9999")
        envfile.write_var(a, ENV, "sk-project-a")
        envfile.write_var(b, ENV, "sk-project-b")

        envfile.load_env(a)
        envfile.load_env(b)
        assert os.environ[ENV] == "sk-shell-9999"

    def test_an_external_change_relinquishes_ownership(self, two_projects,
                                                       monkeypatch):
        """Once something outside envfile reassigns a var envfile set, that
        value stands: no re-point, no eviction, whatever project loads next."""
        a, b = two_projects
        envfile.write_var(a, ENV, "sk-project-a")
        envfile.write_var(b, ENV, "sk-project-b")

        envfile.load_env(a)
        monkeypatch.setenv(ENV, "sk-someone-else")
        envfile.load_env(b)
        assert os.environ[ENV] == "sk-someone-else"


class TestSameProjectReloads:
    def test_rotating_a_key_in_the_same_project_takes_effect(self, two_projects):
        """The old never-overwrite rule meant the first-ever value stuck for the
        life of the process; a var envfile itself owns now refreshes."""
        a, _b = two_projects
        envfile.write_var(a, ENV, "sk-old-value")
        envfile.load_env(a)
        assert os.environ[ENV] == "sk-old-value"

        envfile.write_var(a, ENV, "sk-new-value")
        envfile.reset_cache()  # same-length rotation can land inside one mtime tick
        envfile.load_env(a)
        assert os.environ[ENV] == "sk-new-value"

    def test_a_directly_assigned_matching_value_is_adopted(self, two_projects,
                                                           monkeypatch):
        """providers._reapply assigns os.environ directly after a key save.
        Loading then ADOPTS the var (same value, not a startup shell export), so
        a later project switch can still re-point it instead of mistaking it
        for a shell export."""
        var = "BGATE_TEST_ADOPTED_KEY"  # never in the shell at process start
        a, b = two_projects
        envfile.write_var(a, var, "sk-reapplied")
        monkeypatch.setenv(var, "sk-reapplied")  # what _reapply does

        envfile.load_env(a)   # adopts
        envfile.load_env(b)   # b has no such key anywhere
        assert var not in os.environ
