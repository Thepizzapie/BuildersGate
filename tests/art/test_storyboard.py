"""The storyboard: the free half of a cutscene, and the line it draws.

WHAT IS ACTUALLY WORTH PINNING HERE. Most of this module is CRUD and CRUD tests
mostly restate the schema. The things that earn a test are the refusals, because
every one of them stands between a user and money they cannot get back:

  * approving a frame with no image, which would let a shot be bought against
    prose alone
  * promoting a board that is not ready, same consequence one level up
  * a re-plan silently discarding an image that was already paid for
  * a reorder that drops a frame, which discards one the same way
  * a path escaping the project, since these files are uploaded to a provider

The generation path itself is not tested against a live provider — that spends
money on every run. chroma.generate is faked, and what is checked is what this
module decides BEFORE and AFTER that call: the conditioning it assembles, and
the state it leaves the row in when the provider fails.
"""
from __future__ import annotations

import base64
import json

import pytest

from bgate_core.cine import cinematic, storyboard
from bgate_core.store import db


@pytest.fixture()
def board(root):
    storyboard.plan(root, "Atrium Ambush", [
        {"beat": "Wide on the empty atrium", "camera": "wide"},
        {"beat": "Ledger steps out of the lift", "camera": "medium",
         "duration": 4},
        {"beat": "The lights cut", "camera": "close", "dialogue": "Not again."},
    ], premise="A layoff goes wrong", style="noir", style_note="rain on glass")
    return "atrium-ambush"


def plate(root, name="a"):
    """A file that exists inside the project, which is all attach requires."""
    art = root / "design" / "cinematics" / "plates"
    art.mkdir(parents=True, exist_ok=True)
    path = art / f"{name}.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
    return f"design/cinematics/plates/{name}.png"


class TestPlanning:
    def test_a_board_is_written_with_its_frames_in_order(self, root, board):
        out = storyboard.board(root, board)
        assert [f["idx"] for f in out["frames"]] == [1, 2, 3]
        assert out["frames"][1]["duration"] == 4
        assert out["style"] == "noir"

    def test_a_frame_needs_a_beat_or_an_action(self, root):
        with pytest.raises(storyboard.StoryboardError, match="neither a beat"):
            storyboard.plan(root, "empty", [{"camera": "wide"}])

    def test_an_unpinned_cast_is_warned_about_loudly(self, root, board):
        out = storyboard.plan(root, board, None)
        assert any("NO CAST REFERENCES" in w for w in out["warnings"])

    def test_a_cast_is_stored_as_names_not_resolved_paths(self, root):
        """refs.pin versions a re-pin into a new file and moves the pointer.
        Storing today's path would keep boarding against a character art has
        since redrawn."""
        out = storyboard.plan(root, "cast", [{"beat": "x"}],
                              cast_refs=["ledger", "kpi@r2"])
        assert out["cast_refs"] == ["ledger", "kpi@r2"]

    def test_editing_the_board_without_frames_leaves_the_frames_alone(
            self, root, board):
        storyboard.plan(root, board, None, style="anime")
        out = storyboard.board(root, board)
        assert out["style"] == "anime"
        assert len(out["frames"]) == 3

    def test_a_path_that_escapes_the_project_is_refused(self, root):
        """These get uploaded to a third party. Containment is not cosmetic."""
        with pytest.raises(Exception, match="outside the project"):
            storyboard.plan(root, "escape", [
                {"beat": "x", "image_path": "../../../../etc/passwd"}])


class TestAReplanDoesNotDiscardWhatWasPaidFor:
    def test_an_image_survives_a_rewrite_at_the_same_index(self, root, board):
        storyboard.frame_attach(root, board, 2, image=plate(root))
        storyboard.plan(root, board, [
            {"beat": "totally different opening"},
            {"beat": "rewritten second beat"},
        ])
        out = storyboard.board(root, board)
        assert out["frames"][1]["has_image"], "the drawing was thrown away"
        assert out["frames"][1]["beat"] == "rewritten second beat"

    def test_an_approval_does_not_survive_losing_its_image(self, root, board):
        """An approval is given FOR an image. Carrying it onto a frame that no
        longer has one is how a shot gets bought unanchored."""
        assert storyboard._frame_status("approved", "") == "empty"


class TestApprovalGuardsTheSpend:
    def test_approving_a_frame_with_no_image_is_refused(self, root, board):
        with pytest.raises(storyboard.StoryboardError, match="has no image"):
            storyboard.frame_set(root, board, 1, status="approved")

    def test_an_unknown_status_is_refused_rather_than_stored(self, root, board):
        with pytest.raises(storyboard.StoryboardError, match="not one of"):
            storyboard.frame_set(root, board, 1, status="shipped")

    def test_an_image_cannot_be_moved_through_frame_set(self, root, board):
        """It goes through attach or generate so `source` is always recorded."""
        with pytest.raises(storyboard.StoryboardError, match="cannot set"):
            storyboard.frame_set(root, board, 1, image_path="x.png")

    def test_how_a_frame_arrived_is_recorded(self, root, board):
        storyboard.frame_attach(root, board, 1, image=plate(root))
        assert storyboard.board(root, board)["frames"][0]["source"] == "uploaded"

    def test_attach_takes_exactly_one_of_image_or_ref(self, root, board):
        with pytest.raises(storyboard.StoryboardError, match="exactly one"):
            storyboard.frame_attach(root, board, 1)


class TestReordering:
    def test_a_reorder_rewrites_every_index(self, root, board):
        storyboard.frame_reorder(root, board, [3, 2, 1])
        beats = [f["beat"] for f in storyboard.board(root, board)["frames"]]
        assert beats[0].startswith("The lights cut")

    def test_a_partial_reorder_is_refused_rather_than_interpreted(
            self, root, board):
        """Dropping a frame here would discard an image somebody paid for."""
        with pytest.raises(storyboard.StoryboardError, match="exactly once"):
            storyboard.frame_reorder(root, board, [1, 2])

    def test_inserting_shifts_the_frames_below_it(self, root, board):
        storyboard.frame_add(root, board, beat="a door slams", after=1)
        out = storyboard.board(root, board)
        assert [f["idx"] for f in out["frames"]] == [1, 2, 3, 4]
        assert out["frames"][1]["beat"] == "a door slams"
        assert out["frames"][2]["beat"].startswith("Ledger steps")


class TestPromotionIsTheLine:
    def approve_all(self, root, board):
        for i in (1, 2, 3):
            storyboard.frame_attach(root, board, i, image=plate(root, f"p{i}"),
                                    approve=True)

    def test_an_unready_board_is_refused_and_says_which_frames(
            self, root, board):
        out = storyboard.promote(root, board)
        assert out["ok"] is False
        assert "1, 2, 3" in json.dumps(out["blockers"])

    def test_every_frame_becomes_a_shot_anchored_on_its_image(
            self, root, board):
        self.approve_all(root, board)
        out = storyboard.promote(root, board, model="seedance-2")
        assert out["ok"], out
        seq = cinematic.sequence(root, board)
        assert len(seq["shots"]) == 3
        assert all(s["first_frame"] for s in seq["shots"])

    def test_the_look_the_board_was_approved_under_is_what_gets_bought(
            self, root, board):
        self.approve_all(root, board)
        storyboard.promote(root, board, model="seedance-2")
        seq = cinematic.sequence(root, board)
        assert seq["style"] == "noir"
        assert seq["style_note"] == "rain on glass"

    def test_cut_frames_do_not_travel(self, root, board):
        self.approve_all(root, board)
        storyboard.frame_cut(root, board, 2)
        storyboard.promote(root, board, model="seedance-2")
        assert len(cinematic.sequence(root, board)["shots"]) == 2

    def test_a_promoted_board_refuses_to_have_its_frames_rewritten(
            self, root, board):
        self.approve_all(root, board)
        storyboard.promote(root, board, model="seedance-2")
        with pytest.raises(storyboard.StoryboardError, match="already promoted"):
            storyboard.plan(root, board, [{"beat": "rewrite of a decision"}])

    def test_a_failed_plan_leaves_the_board_unpromoted(self, root, board):
        """A board marked promoted with no sequence to point at is worse than a
        refusal — the next call reads it as done."""
        self.approve_all(root, board)
        out = storyboard.promote(root, board, model="no/such-model-v9")
        assert out["ok"] is False
        assert storyboard.board(root, board)["status"] != "promoted"

    def test_allow_unanchored_has_to_be_typed(self, root, board):
        out = storyboard.promote(root, board, model="seedance-2",
                                 allow_unanchored=True)
        assert out["ok"] and out["anchored"] == 0


class TestGeneration:
    def fake_chroma(self, monkeypatch, root, *, ok=True):
        seen = {}

        def _generate(prompt, out_path, **kw):
            seen["prompt"] = prompt
            seen["refs"] = list(kw.get("ref_paths") or [])
            if ok:
                from pathlib import Path

                Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
                return {"ok": True, "model": "fake", "estimated_usd": 0.04,
                        "seconds": 1.0}
            return {"ok": False, "error": "provider said no"}

        from bgate_core.art import chroma

        monkeypatch.setattr(chroma, "generate", _generate)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        return seen

    def test_the_cast_is_passed_as_conditioning_on_every_frame(
            self, root, board, monkeypatch):
        """This is the drift the whole subsystem exists to prevent."""
        from bgate_core.art import refs

        refs.pin(root, "ledger", str(root / plate(root, "ledger")),
                 kind="character")
        storyboard.plan(root, board, None, cast_refs=["ledger"])
        seen = self.fake_chroma(monkeypatch, root)

        storyboard.frame_generate(root, board, 1)
        assert any("ledger" in p for p in seen["refs"]), seen["refs"]

    def test_use_cast_false_drops_it_for_a_shot_with_nobody_in_it(
            self, root, board, monkeypatch):
        from bgate_core.art import refs

        refs.pin(root, "ledger", str(root / plate(root, "ledger")),
                 kind="character")
        storyboard.plan(root, board, None, cast_refs=["ledger"])
        seen = self.fake_chroma(monkeypatch, root)

        storyboard.frame_generate(root, board, 1, use_cast=False)
        assert not seen["refs"]

    def test_a_generated_frame_is_drafted_never_approved(
            self, root, board, monkeypatch):
        """Approval is what lets a shot be bought against this frame. The thing
        that drew it does not get to decide that."""
        self.fake_chroma(monkeypatch, root)
        storyboard.frame_generate(root, board, 1)
        frame = storyboard.board(root, board)["frames"][0]
        assert frame["status"] == "drafted"
        assert frame["source"] == "generated"

    def test_a_provider_failure_does_not_leave_the_frame_generating(
            self, root, board, monkeypatch):
        """A row stuck at 'generating' reads as work in flight forever."""
        self.fake_chroma(monkeypatch, root, ok=False)
        out = storyboard.frame_generate(root, board, 1)
        assert out["ok"] is False
        assert storyboard.board(root, board)["frames"][0]["status"] == "empty"

    def test_a_frame_with_nothing_to_draw_is_refused_before_the_spend(
            self, root, monkeypatch):
        self.fake_chroma(monkeypatch, root)
        storyboard.plan(root, "blank", [{"beat": "placeholder"}])
        storyboard.frame_set(root, "blank", 1, beat="", action="")
        with pytest.raises(storyboard.StoryboardError, match="nothing to draw"):
            storyboard.frame_generate(root, "blank", 1)

    def test_conditioning_is_capped_at_what_a_provider_will_read(self, root):
        """Past four the later ones are ignored anyway, so the cap is applied
        where it can be said out loud rather than discovered."""
        names = [plate(root, f"r{i}") for i in range(6)]
        paths, missing = storyboard._resolve_all(root, names)
        assert len(paths) == 4
        assert not missing

    def test_a_missing_reference_is_reported_not_raised(self, root):
        paths, missing = storyboard._resolve_all(root, ["nope-not-pinned"])
        assert missing == ["nope-not-pinned"]


class TestDeletion:
    def test_images_survive_a_deleted_board_by_default(self, root, board):
        storyboard.frame_attach(root, board, 1, image=plate(root))
        storyboard.delete(root, board)
        assert (root / "design/cinematics/plates/a.png").exists()

    def test_frames_go_with_the_board(self, root, board):
        b = storyboard._board_row(root, board)
        storyboard.delete(root, board)
        left = db.connect(root).execute(
            "SELECT COUNT(*) FROM story_frame WHERE board_id=?",
            (b["id"],)).fetchone()[0]
        assert left == 0


class TestTheRoutes:
    @pytest.fixture()
    def client(self, root, monkeypatch):
        from fastapi.testclient import TestClient

        from bgate_ui.app import app

        monkeypatch.setenv("BGATE_ROOT", str(root))
        return TestClient(app)

    def test_the_route_module_actually_loaded(self, client):
        from bgate_ui import routes

        assert "storyboard" in routes.REGISTERED
        assert not [f for f in routes.FAILURES if f["module"] == "storyboard"]

    def test_a_refusal_comes_back_as_a_400_a_user_can_read(
            self, client, board):
        r = client.post("/api/storyboard/frame/set",
                        json={"name": board, "idx": 1, "status": "approved"})
        assert r.status_code == 400
        assert "has no image" in r.text

    def test_an_upload_lands_inside_the_board_and_attaches(
            self, client, board, root):
        blob = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 40).decode()
        r = client.post("/api/storyboard/frame/upload",
                        json={"name": board, "idx": 1,
                              "data": f"data:image/png;base64,{blob}"})
        assert r.status_code == 200, r.text
        rel = r.json()["data"]["path"]
        assert rel.startswith(storyboard.BOARD_DIRNAME)
        assert (root / rel).is_file()

    def test_a_non_image_upload_is_refused(self, client, board):
        r = client.post("/api/storyboard/frame/upload",
                        json={"name": board, "idx": 1, "ext": "exe",
                              "data": base64.b64encode(b"MZ").decode()})
        assert r.status_code == 415

    def test_generating_a_frame_that_does_not_exist_answers_immediately(
            self, client, board):
        """Not as a job that fails a minute later in a panel nobody is on."""
        r = client.post("/api/storyboard/frame/generate",
                        json={"name": board, "idx": 99})
        assert r.status_code == 404
