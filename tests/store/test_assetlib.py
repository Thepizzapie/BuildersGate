"""The machine-wide asset library: content-addressed, in BGATE_HOME, and
never able to reach outside the project it publishes from.

BGATE_HOME is redirected per test by conftest, so nothing here touches the
developer's real ~/.bgate/library.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image

from bgate_core.store import assetlib


@pytest.fixture()
def project(tmp_path):
    proj = tmp_path / "game"
    (proj / "assets" / "characters").mkdir(parents=True)
    Image.new("RGBA", (64, 32), (1, 2, 3, 4)).save(
        proj / "assets" / "characters" / "pm_paladin_walk.png")
    (proj / "assets" / "characters" / "pm_paladin_walk.rig.json").write_text(
        json.dumps({"grid": {"cols": 4, "rows": 1}}), encoding="utf-8")
    Image.new("RGBA", (32, 32), (9, 9, 9, 9)).save(
        proj / "assets" / "characters" / "pm_paladin_idle.png")
    (proj / "assets" / "characters" / "pm_paladin_idle.png.import").write_text(
        "[remap]\n", encoding="utf-8")
    (proj / "assets" / "sfx").mkdir()
    (proj / "assets" / "sfx" / "hit.wav").write_bytes(b"RIFF" + b"\0" * 40)
    return proj


class TestHome:
    def test_lives_under_bgate_home(self):
        assert assetlib.home() == Path(os.environ["BGATE_HOME"]) / "library"

    def test_empty_library_reads_empty(self):
        got = assetlib.search()
        assert got["library_size"] == 0 and got["entries"] == []


class TestPublish:
    def test_directory_publishes_assets_not_engine_cache(self, project):
        got = assetlib.publish(project, ["assets"], tags=["Paladin", "sprite"],
                               collection="paladin", project_name="Emberfall")
        names = sorted(e["name"] for e in got["published"])
        assert names == ["hit.wav", "pm_paladin_idle.png", "pm_paladin_walk.png"]
        assert got["existing"] == []
        walk = next(e for e in got["published"] if e["name"] == "pm_paladin_walk.png")
        assert walk["kind"] == "texture" and walk["dims"] == [64, 32]
        assert walk["tags"] == ["paladin", "sprite"]
        assert walk["collection"] == "paladin"
        assert walk["sources"][0]["project"] == "Emberfall"
        assert walk["sources"][0]["path"] == "assets/characters/pm_paladin_walk.png"
        assert len(walk["sidecars"]) == 1

    def test_blob_is_content_addressed(self, project):
        got = assetlib.publish(project, ["assets/sfx/hit.wav"])
        entry = got["published"][0]
        blob = assetlib.home() / "blobs" / entry["blob"]
        assert blob.is_file() and blob.name.startswith(entry["hash"])
        assert blob.read_bytes() == (project / "assets" / "sfx" / "hit.wav").read_bytes()

    def test_republish_merges_tags_and_stores_nothing_new(self, project):
        assetlib.publish(project, ["assets/sfx/hit.wav"], tags=["impact"])
        got = assetlib.publish(project, ["assets/sfx/hit.wav"], tags=["hit"],
                               collection="combat")
        assert got["published"] == []
        assert got["existing"][0]["tags"] == ["impact", "hit"]
        assert got["existing"][0]["collection"] == "combat"
        assert len(list((assetlib.home() / "blobs").iterdir())) == 1

    def test_same_bytes_new_name_is_a_new_entry_over_one_blob(self, project):
        src = project / "assets" / "sfx" / "hit.wav"
        (project / "assets" / "sfx" / "thud.wav").write_bytes(src.read_bytes())
        got = assetlib.publish(project, ["assets/sfx"])
        assert sorted(e["name"] for e in got["published"]) == ["hit.wav", "thud.wav"]
        assert len({e["blob"] for e in got["published"]}) == 1
        assert assetlib.search("thud")["matched"] == 1

    def test_outside_the_project_is_refused(self, project, tmp_path):
        stranger = tmp_path / "elsewhere.png"
        stranger.write_bytes(b"x")
        with pytest.raises(assetlib.LibraryError, match="outside the project root"):
            assetlib.publish(project, [str(stranger)])
        with pytest.raises(assetlib.LibraryError, match="outside the project root"):
            assetlib.publish(project, ["../elsewhere.png"])

    def test_import_files_are_refused_by_name(self, project):
        with pytest.raises(assetlib.LibraryError, match="engine cache"):
            assetlib.publish(project, ["assets/characters/pm_paladin_idle.png.import"])

    def test_missing_path_is_named(self, project):
        with pytest.raises(assetlib.LibraryError, match="nothing on disk"):
            assetlib.publish(project, ["assets/ghost.png"])

    def test_empty_directory_publishes_nothing_loudly(self, project):
        (project / "empty").mkdir()
        with pytest.raises(assetlib.LibraryError, match="nothing to publish"):
            assetlib.publish(project, ["empty"])


class TestSearch:
    @pytest.fixture(autouse=True)
    def _seed(self, project):
        assetlib.publish(project, ["assets/characters"], tags=["hero"],
                         collection="paladin", note="the paladin body sheets")
        assetlib.publish(project, ["assets/sfx"], tags=["impact"], collection="combat")

    def test_substring_over_name_tags_collection_note(self):
        assert assetlib.search("paladin")["matched"] == 2
        assert assetlib.search("impact")["matched"] == 1
        assert assetlib.search("body sheets")["matched"] == 2
        assert assetlib.search("dragon")["matched"] == 0

    def test_filters_and(self):
        assert assetlib.search(kind="texture")["matched"] == 2
        assert assetlib.search(kind="audio", tags=["hero"])["matched"] == 0
        assert assetlib.search(collection="combat")["matched"] == 1

    def test_counts_cover_the_whole_library(self):
        got = assetlib.search("dragon")
        assert got["matched"] == 0
        assert got["library_size"] == 3
        assert got["kinds"] == {"texture": 2, "audio": 1}
        assert got["collections"] == {"paladin": 2, "combat": 1}

    def test_get_by_id_or_unique_name(self):
        entry = assetlib.search("walk")["entries"][0]
        assert assetlib.get(entry["id"])["name"] == "pm_paladin_walk.png"
        assert assetlib.get("hit.wav")["kind"] == "audio"
        with pytest.raises(assetlib.LibraryError, match="no library entry"):
            assetlib.get("nope")


class TestImport:
    @pytest.fixture()
    def walk_id(self, project):
        got = assetlib.publish(project, ["assets/characters/pm_paladin_walk.png"])
        return got["published"][0]["id"]

    def test_lands_with_sidecar_and_res_path(self, walk_id, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        got = assetlib.import_entries(other, [walk_id])
        assert got["ok"]
        row = got["imported"][0]
        assert row["landed"] and row["path"] == "assets/library/pm_paladin_walk.png"
        assert row["res"] == "res://assets/library/pm_paladin_walk.png"
        assert row["sidecars"] == ["assets/library/pm_paladin_walk.rig.json"]
        assert (other / "assets" / "library" / "pm_paladin_walk.rig.json").is_file()

    def test_identical_present_is_reported_not_rewritten(self, walk_id, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        assetlib.import_entries(other, [walk_id])
        target = other / "assets" / "library" / "pm_paladin_walk.png"
        mtime = target.stat().st_mtime_ns
        got = assetlib.import_entries(other, [walk_id])
        assert got["ok"] and got["imported"][0]["landed"] is False
        assert target.stat().st_mtime_ns == mtime

    def test_different_present_is_refused_unless_overwrite(self, walk_id, tmp_path):
        other = tmp_path / "other"
        (other / "assets" / "library").mkdir(parents=True)
        target = other / "assets" / "library" / "pm_paladin_walk.png"
        target.write_bytes(b"mine")
        got = assetlib.import_entries(other, [walk_id])
        assert not got["ok"] and "differs" in got["refused"][0]["reason"]
        assert target.read_bytes() == b"mine"
        got = assetlib.import_entries(other, [walk_id], overwrite=True)
        assert got["ok"] and target.read_bytes() != b"mine"

    def test_dest_and_rename(self, walk_id, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        got = assetlib.import_entries(other, [walk_id], dest="art/heroes", rename="knight")
        assert got["imported"][0]["path"] == "art/heroes/knight.png"

    def test_rename_needs_exactly_one(self, walk_id, tmp_path):
        with pytest.raises(assetlib.LibraryError, match="exactly one"):
            assetlib.import_entries(tmp_path, [walk_id, walk_id], rename="x")

    def test_dest_cannot_escape(self, walk_id, tmp_path):
        with pytest.raises(assetlib.LibraryError, match="relative path"):
            assetlib.import_entries(tmp_path, [walk_id], dest="../out")

    def test_missing_blob_is_refused_with_a_remedy(self, walk_id, tmp_path):
        entry = assetlib.get(walk_id)
        (assetlib.home() / "blobs" / entry["blob"]).unlink()
        got = assetlib.import_entries(tmp_path, [walk_id])
        assert not got["ok"] and "publish it again" in got["refused"][0]["reason"]


class TestForget:
    def test_drops_entry_and_unshared_blob(self, project):
        got = assetlib.publish(project, ["assets/sfx/hit.wav"])
        eid = got["published"][0]["id"]
        out = assetlib.forget(eid)
        assert out["ok"] and out["blob_removed"]
        assert assetlib.search()["library_size"] == 0
        assert not list((assetlib.home() / "blobs").iterdir())

    def test_keeps_a_blob_another_entry_shares(self, project):
        src = project / "assets" / "sfx" / "hit.wav"
        (project / "assets" / "sfx" / "thud.wav").write_bytes(src.read_bytes())
        got = assetlib.publish(project, ["assets/sfx"])
        hit = next(e for e in got["published"] if e["name"] == "hit.wav")
        out = assetlib.forget(hit["id"])
        assert out["ok"] and not out["blob_removed"]
        assert assetlib.get("thud.wav")
        assert len(list((assetlib.home() / "blobs").iterdir())) == 1
