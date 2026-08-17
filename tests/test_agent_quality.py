"""The mechanisms behind agents wandering, over-conditioning and bluffing.

Three complaints from the field, each pinned to its mechanism:

  * irrelevant REFERENCES — every global pin went into every brief and every
    layered set, and recall OR-joined its query words so any one word matched;
  * off-brief WORK — partly prompt (tested by reading, not here), partly the
    irrelevant surface above;
  * unverified "done" — a machine's completion claiming work over a run the
    hook watched write nothing read as consistent.
"""
from __future__ import annotations

from pathlib import Path


from bgate_core import db, queue, refs, search, seats, task_refs

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


def _pin(root, name: str, kind: str) -> None:
    src = Path(root) / f"{name}.png"
    src.write_bytes(PNG)
    refs.pin(root, name, str(src), kind=kind, note=f"{kind} pin")


# --- recall: all words first ------------------------------------------------

class TestRecallMatchesAllWordsFirst:
    def _seed(self, root):
        conn = db.connect(root)
        with db.tx(root) as tx:
            search.reindex(tx, "lore:hero", "lore.character", "The hero",
                           "the player character walks and jumps")
            search.reindex(tx, "lore:market", "lore.place", "The market",
                           "a market where the player buys things")
            search.reindex(tx, "lore:walk", "lore.system", "Walk cycles",
                           "walk animation frames for the player character")
        return conn

    def test_a_multiword_query_needs_all_its_words(self, root):
        conn = self._seed(root)
        got = search.find(conn, "player walk animation")
        # Only the row with all three words — not every row containing
        # "player", which is what the OR-join returned.
        assert [r["ref"] for r in got] == ["lore:walk"]

    def test_one_wrong_word_still_finds_the_neighbourhood(self, root):
        conn = self._seed(root)
        got = search.find(conn, "player walk zorbulon")
        # The AND pass finds nothing; the OR fallback still answers.
        assert got, "the fallback did not fire"
        assert "lore:walk" in [r["ref"] for r in got]


# --- layered refs: anchors keep their identity set --------------------------

class TestAnchoredTasksDoNotInheritEveryPin:
    def test_task_anchors_admit_only_style_globals(self, root):
        for name, kind in (("hero", "character"), ("villain", "character"),
                           ("look", "style"), ("battle", "concept")):
            _pin(root, name, kind)
        item = queue.add(root, "art", "a ui bar")
        anchor = Path(root) / "bar-anchor.png"
        anchor.write_bytes(PNG)
        refs.pin(root, "bar-anchor", str(anchor), kind="ui")
        task_refs.add(root, int(item["id"]), "bar-anchor")
        layered = task_refs.resolve_for_task(root, int(item["id"]))
        scopes = {(r["ref"], r["scope"]) for r in layered}
        assert ("bar-anchor", "task") in scopes
        assert ("look", "global") in scopes          # the project look rides
        # the unrelated identities no longer bleed in
        assert not {r for r in layered
                    if r["scope"] == "global" and r["kind"] != "style"}

    def test_an_unanchored_task_keeps_the_global_set(self, root):
        for name, kind in (("hero", "character"), ("look", "style")):
            _pin(root, name, kind)
        item = queue.add(root, "art", "anything")
        layered = task_refs.resolve_for_task(root, int(item["id"]))
        assert {r["ref"] for r in layered} == {"hero", "look"}


# --- briefs: pins go to the seats that generate ------------------------------

class TestPinsGoToGeneratingSeats:
    def test_art_gets_the_shelf_and_tech_does_not(self, root):
        _pin(root, "hero", "character")
        assert seats.brief(root, "art")["pinned_refs"]
        assert seats.brief(root, "tech")["pinned_refs"] == []


# --- completion: a no-writes 'done' is marked as the claim it is -------------

class TestUnbackedCompletionsAreMarked:
    def test_a_machine_done_with_no_writes_carries_the_note(self, root, monkeypatch):
        item = queue.add(root, "gameplay", "implement the dash")
        queue.set_status(root, int(item["id"]), "dispatched")
        monkeypatch.setenv("BGATE_ACTOR", f"agent:item-{item['id']}")
        done = queue.complete(root, int(item["id"]), result="dash implemented")
        assert "HARNESS NOTE" in done["result"]
        assert "NO file writes" in done["result"]

    def test_a_human_close_is_not_accused(self, root):
        item = queue.add(root, "gameplay", "implement the dash")
        queue.set_status(root, int(item["id"]), "dispatched")
        done = queue.complete(root, int(item["id"]), result="closed by hand")
        assert "HARNESS NOTE" not in done["result"]

    def test_a_chat_turn_is_not_accused(self, root, monkeypatch):
        item = queue.add(root, "narrative", "what is the hero's name?",
                         source="chat")
        queue.set_status(root, int(item["id"]), "dispatched")
        monkeypatch.setenv("BGATE_ACTOR", f"agent:item-{item['id']}")
        done = queue.complete(root, int(item["id"]), result="Scoville.")
        assert "HARNESS NOTE" not in done["result"]

    def test_a_failed_run_is_not_accused_twice(self, root, monkeypatch):
        item = queue.add(root, "gameplay", "implement the dash")
        queue.set_status(root, int(item["id"]), "dispatched")
        monkeypatch.setenv("BGATE_ACTOR", f"agent:item-{item['id']}")
        failed = queue.complete(root, int(item["id"]),
                                result="could not", failed=True)
        assert "HARNESS NOTE" not in failed["result"]
