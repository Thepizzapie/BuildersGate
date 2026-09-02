"""One entry per folder in the project list, however the registry got that way.

THE BUG THIS PINS TURNED THE PACKAGED APP INTO A BLACK WINDOW. known_projects()
is keyed by NAME, and names are unique by construction — but the values are
roots, and nothing stopped two names pointing at one folder. Renaming a project
does exactly that: the new name is registered and the old one is never retired.

Every caller turns this mapping into a list of choices keyed by root, and the
dashboard's picker is a Mantine Select, which THROWS on a duplicate value
rather than rendering it twice. An exception thrown while React renders unmounts
the tree containing it, so the app opened a window, painted nothing, and sat
there black — while the server behind it answered every request with a 200. One
doubly-registered folder took out the entire interface, and nothing in the logs
said so.

So the invariant is not cosmetic and it belongs here rather than in the one
component that happened to crash first.
"""
from __future__ import annotations

import json

import pytest

from bgate_core.store import db, project


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """Write the machine-wide registry directly, and make real folders for it."""
    reg = tmp_path / "projects.json"
    monkeypatch.setattr(project, "_registry_path", lambda: reg)

    def write(entries: dict[str, str]) -> None:
        reg.write_text(json.dumps(entries), encoding="utf-8")

    def folder(name: str) -> str:
        """A folder that _read_registry() accepts: it wants a real game.db."""
        p = tmp_path / name
        (p / db.DB_DIRNAME).mkdir(parents=True, exist_ok=True)
        (p / db.DB_DIRNAME / db.DB_FILENAME).touch()
        return str(p)

    return write, folder


class TestOneEntryPerFolder:
    def test_two_names_for_one_folder_collapse(self, registry):
        """THE REGRESSION. A renamed project leaves both names registered."""
        write, folder = registry
        root = folder("example-game-v2")
        write({"Cops and Robbers": root, "example-game-v2": root})

        known = project.known_projects()

        assert list(known.values()) == [root], "one folder must appear once"
        assert len(set(known.values())) == len(known)

    def test_first_registration_wins(self, registry):
        """Stable ordering: adding a project must not reshuffle the menu."""
        write, folder = registry
        root = folder("game")
        write({"first": root, "second": root})

        assert list(project.known_projects()) == ["first"]

    def test_case_and_separator_differences_are_one_folder(self, registry):
        """Windows agrees C:\\Games\\X and c:/games/x are the same place."""
        write, folder = registry
        root = folder("game")
        write({"a": root, "b": root.upper().replace("\\", "/")})

        known = project.known_projects()
        assert len(known) == 1, f"same folder registered twice: {known}"

    def test_distinct_folders_all_survive(self, registry):
        """The dedupe must not eat genuinely different projects."""
        write, folder = registry
        one, two = folder("one"), folder("two")
        write({"one": one, "two": two})

        assert set(project.known_projects().values()) == {one, two}

    def test_missing_folders_still_filtered(self, registry, tmp_path):
        """The pre-existing promise: a registry row is a breadcrumb, not a fact."""
        write, folder = registry
        alive = folder("alive")
        write({"alive": alive, "gone": str(tmp_path / "never-existed")})

        assert list(project.known_projects()) == ["alive"]

    def test_blank_roots_do_not_collapse_into_one_entry(self, registry):
        """An empty root is dropped, not deduped into a single empty choice."""
        write, folder = registry
        alive = folder("alive")
        write({"alive": alive, "broken": "", "alsobroken": ""})

        assert list(project.known_projects()) == ["alive"]
