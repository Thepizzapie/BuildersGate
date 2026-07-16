from __future__ import annotations

import pytest

from bgate_core import bible, db, lore, project, search


class TestProject:
    def test_init_creates_db_and_row(self, root):
        assert db.db_path(root).exists()
        assert project.get(root)["name"] == "Test Game"

    def test_init_is_idempotent(self, root):
        project.init(root, "Renamed", dimension="3d")
        got = project.get(root)
        assert got["name"] == "Renamed"
        assert got["dimension"] == "3d"

    def test_rejects_unknown_engine(self, tmp_path):
        with pytest.raises(ValueError, match="engine"):
            project.init(tmp_path, "Bad", engine="unreal")

    def test_resolve_root_walks_up(self, root):
        nested = root / "src" / "systems"
        nested.mkdir(parents=True)
        assert db.resolve_root(nested) == root

    def test_resolve_root_returns_none_outside(self, tmp_path):
        assert db.resolve_root(tmp_path) is None


class TestBible:
    def test_add_and_read(self, root):
        section = bible.add(root, "pillar", "Tension over spectacle")
        assert section["id"] > 0
        assert bible.list_sections(root, "pillar")[0]["title"] == "Tension over spectacle"

    def test_rejects_unknown_kind(self, root):
        with pytest.raises(ValueError, match="kind"):
            bible.add(root, "vibes", "nope")

    def test_cut_line_partitions_scope(self, root):
        bible.add(root, "scope_tier", "Core loop", rank=1)
        bible.add(root, "scope_tier", "One enemy type", rank=2)
        bible.add(root, "cut_line", "--- ship it ---", rank=3)
        bible.add(root, "scope_tier", "Multiplayer", rank=4)

        view = bible.overview(root)
        assert [s["title"] for s in view["in_scope"]] == ["Core loop", "One enemy type"]
        assert [s["title"] for s in view["cut"]] == ["Multiplayer"]
        assert bible.in_scope(root, 2) is True
        assert bible.in_scope(root, 9) is False

    def test_everything_in_scope_without_cut_line(self, root):
        assert bible.in_scope(root, 999) is True

    def test_update_reindexes_search(self, root):
        section = bible.add(root, "loop", "placeholder")
        bible.update(root, section["id"], title="Scavenge, craft, survive")
        conn = db.connect(root)
        assert search.find(conn, "scavenge")
        assert not search.find(conn, "placeholder")


class TestLore:
    def test_add_entity_and_brief(self, root):
        lore.add_entity(root, "faction", "The Ashen Order", summary="Zealots.")
        brief = lore.brief(root, "the-ashen-order")
        assert brief["entity"]["name"] == "The Ashen Order"
        assert brief["facts"] == []

    def test_duplicate_name_rejected(self, root):
        lore.add_entity(root, "faction", "The Ashen Order")
        with pytest.raises(ValueError, match="already exists"):
            lore.add_entity(root, "character", "The Ashen Order")

    def test_links_resolve_both_directions(self, root):
        lore.add_entity(root, "character", "Sera Vane")
        lore.add_entity(root, "faction", "The Ashen Order")
        lore.link(root, "Sera Vane", "leads", "The Ashen Order")

        out = lore.links_of(root, "sera-vane")
        assert (out[0]["dir"], out[0]["rel"], out[0]["slug"]) == ("out", "leads", "the-ashen-order")
        assert lore.links_of(root, "the-ashen-order")[0]["dir"] == "in"

    def test_link_is_idempotent(self, root):
        lore.add_entity(root, "character", "Sera Vane")
        lore.add_entity(root, "faction", "The Ashen Order")
        lore.link(root, "Sera Vane", "leads", "The Ashen Order")
        lore.link(root, "Sera Vane", "leads", "The Ashen Order", note="updated")
        links = lore.links_of(root, "sera-vane")
        assert len(links) == 1
        assert links[0]["note"] == "updated"

    def test_facts_are_indexed_for_recall(self, root):
        lore.add_entity(root, "place", "Cinder Vault")
        lore.add_fact(root, "Cinder Vault", "The vault was sealed for seven years.")
        conn = db.connect(root)
        assert any(r["ref"] == "entity:cinder-vault" for r in search.find(conn, "sealed"))

    def test_deleting_entity_cascades_facts(self, root):
        entity = lore.add_entity(root, "item", "Emberglass")
        lore.add_fact(root, "Emberglass", "It cannot be forged twice.")
        with db.tx(root) as conn:
            conn.execute("DELETE FROM lore_entity WHERE id = ?", (entity["id"],))
        conn = db.connect(root)
        assert conn.execute("SELECT count(*) FROM canon_fact").fetchone()[0] == 0


class TestSearch:
    def test_punctuation_does_not_break_fts(self, root):
        bible.add(root, "pillar", "Tension over spectacle")
        conn = db.connect(root)
        assert search.find(conn, "tension! (spectacle)?")

    def test_empty_query_returns_nothing(self, root):
        conn = db.connect(root)
        assert search.find(conn, "   ") == []
