"""The Overview's Work history: what finished, and what the verdict really was.

The point of every test here is the SECOND half of that sentence. A history
panel that renders rows is easy; one that cannot be made to imply a check which
never happened is the thing worth pinning. Under `gate.mode = none` — the mode
the reference project is actually set to — most completed items have no
independent verdict at all, and the failure mode this file exists to prevent is
that case rendering as a blank cell or, worse, as a pass.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bgate_core import db, gates, queue
from bgate_ui.app import app
from bgate_ui.routes import history as H


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    H._reset_cache()
    yield TestClient(app)
    H._reset_cache()


def _close(root, item_id, result="did the thing", failed=False, closer="agent:item-9"):
    """Close an item the way a DISPATCHED AGENT does.

    queue.complete stamps closed_by/gate_skip from ``activity.current_actor()``,
    which under pytest is the developer — a human hand-close, not the agent
    self-close this suite is mostly about. Restamping both columns together
    keeps the row in a shape production can actually produce; setting only
    closed_by leaves gate_skip=1 beside an agent closer, which nothing does.
    """
    queue.complete(root, item_id, result=result, failed=failed)
    with db.tx(root) as conn:
        conn.execute("UPDATE work_item SET closed_by = ?, gate_skip = 0 "
                     "WHERE id = ?", (closer, item_id))
    return queue.get(root, item_id)


def _gate_round(root, target_id, verdict_text, status="done"):
    """File and close a QA-gate child exactly as bgate_ui.qa_gate does."""
    gate = queue.add(root, "qa", f"QA gate: verify #{target_id}",
                     brief="verify it", source="qa-gate",
                     source_ref=str(target_id))
    queue.complete(root, gate["id"], result=verdict_text,
                   failed=(status == "failed"))
    return gate["id"]


class TestWhatCountsAsFinished:
    def test_running_work_is_not_history(self, root, client):
        queue.add(root, "art", "still queued")
        live = queue.add(root, "art", "dispatched now")
        queue.set_status(root, live["id"], "dispatched")
        done = queue.add(root, "art", "finished")
        _close(root, done["id"])

        body = client.get("/api/history").json()
        ids = [i["id"] for i in body["items"]]
        assert ids == [done["id"]], "only finished work belongs in the history"

    def test_newest_first(self, root, client):
        first = queue.add(root, "art", "older")
        second = queue.add(root, "tech", "newer")
        _close(root, first["id"])
        _close(root, second["id"])
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET updated_at = '2020-01-01 00:00:00' "
                         "WHERE id = ?", (first["id"],))
        ids = [i["id"] for i in client.get("/api/history").json()["items"]]
        assert ids[0] == second["id"]

    def test_the_gates_own_runs_are_hidden_by_default(self, root, client):
        item = queue.add(root, "gameplay", "the work")
        _close(root, item["id"])
        gate_id = _gate_round(root, item["id"], "VERDICT: PASS. all good")

        default = [i["id"] for i in client.get("/api/history").json()["items"]]
        assert gate_id not in default, ("a gate run is already shown AS the "
                                        "verdict of the item it reviewed")
        widened = [i["id"] for i in
                   client.get("/api/history?gate_runs=true").json()["items"]]
        assert gate_id in widened


class TestTheVerdictIsEvidence:
    def test_pass_only_comes_from_a_real_verdict_line(self, root, client):
        item = queue.add(root, "gameplay", "the work")
        _close(root, item["id"])
        _gate_round(root, item["id"], "VERDICT: PASS — it holds, here is why")

        verdict = client.get("/api/history").json()["items"][0]["verdict"]
        assert verdict["kind"] == "pass"
        assert verdict["label"] == "PASS"

    def test_fail_reads_the_same_marker(self, root, client):
        item = queue.add(root, "art", "the work")
        _close(root, item["id"])
        _gate_round(root, item["id"], "VERDICT: FAIL\n1. the thing is wrong")

        verdict = client.get("/api/history").json()["items"][0]["verdict"]
        assert verdict["kind"] == "fail"

    def test_a_gate_run_that_decided_nothing_is_not_a_pass(self, root, client):
        """THE ONE THAT MATTERS. A reviewer that finished without writing a
        verdict decided nothing, and a gate that reports that as a pass is a
        gate that does not gate."""
        item = queue.add(root, "gameplay", "the work")
        _close(root, item["id"])
        _gate_round(root, item["id"], "I looked at some files and then stopped.")

        verdict = client.get("/api/history").json()["items"][0]["verdict"]
        assert verdict["kind"] == "unknown"
        assert verdict["kind"] != "pass"
        assert "VERDICT" in verdict["why"]

    def test_a_dead_gate_run_is_an_error_not_a_verdict(self, root, client):
        item = queue.add(root, "art", "the work")
        _close(root, item["id"])
        _gate_round(root, item["id"], "crashed", status="failed")

        assert client.get("/api/history").json()["items"][0]["verdict"]["kind"] \
            == "error"

    def test_the_latest_decided_round_stands(self, root, client):
        """A FAIL reopens the item; the fix round spawns a fresh gate. Reporting
        the first verdict forever would mark fixed work as broken."""
        item = queue.add(root, "gameplay", "the work")
        _close(root, item["id"])
        _gate_round(root, item["id"], "VERDICT: FAIL — no")
        queue.reopen(root, item["id"], "fix it")
        _close(root, item["id"], result="fixed")
        _gate_round(root, item["id"], "VERDICT: PASS — now it holds")

        verdict = client.get("/api/history").json()["items"][0]["verdict"]
        assert verdict["kind"] == "pass"
        assert verdict["rounds"] == 2

    def test_a_cancelled_later_round_does_not_erase_the_pass(self, root, client):
        item = queue.add(root, "gameplay", "the work")
        _close(root, item["id"])
        _gate_round(root, item["id"], "VERDICT: PASS — holds")
        stray = queue.add(root, "qa", "QA gate: verify again", source="qa-gate",
                          source_ref=str(item["id"]))
        queue.set_status(root, stray["id"], "cancelled")

        assert client.get("/api/history").json()["items"][0]["verdict"]["kind"] \
            == "pass"

    def test_the_round_cap_escalation_is_neither_pass_nor_fail(self, root, client):
        item = queue.add(root, "art", "contested work")
        _close(root, item["id"])
        queue.add(root, "director", "QA loop", source="qa-gate-escalation",
                  source_ref=str(item["id"]))

        verdict = client.get("/api/history").json()["items"][0]["verdict"]
        assert verdict["kind"] == "escalated"
        assert "human" in verdict["why"]


class TestTheHonestNoGateCase:
    def test_an_agent_closing_its_own_item_says_exactly_that(self, root, client):
        gates.set_mode(root, "none")
        item = queue.add(root, "gameplay", "unchecked work")
        _close(root, item["id"], closer=f"agent:item-{item['id']}")

        verdict = client.get("/api/history").json()["items"][0]["verdict"]
        assert verdict["kind"] == "ungated"
        assert verdict["label"] == "no gate"
        assert verdict["short"] == "closed on the agent's own word"
        # The failure this whole panel is guarding against.
        assert verdict["kind"] not in ("pass", "approved")
        assert verdict["why"].strip(), "an unverified row must never be blank"

    def test_a_hand_close_that_skipped_the_gate_says_so(self, root, client):
        item = queue.add(root, "art", "hand-closed work")
        queue.complete(root, item["id"], result="done by hand", skip_gate=True)
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET closed_by = 'Sam' WHERE id = ?",
                         (item["id"],))

        verdict = client.get("/api/history").json()["items"][0]["verdict"]
        assert verdict["kind"] == "ungated"
        assert "skipped" in verdict["short"]

    def test_a_failed_run_is_not_verified_rather_than_failed_qa(self, root, client):
        item = queue.add(root, "tech", "broken work")
        _close(root, item["id"], result="exit 1", failed=True)

        verdict = client.get("/api/history").json()["items"][0]["verdict"]
        assert verdict["kind"] == "na"
        assert verdict["kind"] != "fail", ("a crashed run is not a QA failure — "
                                           "nothing judged it")

    def test_the_panel_is_told_which_gate_is_live(self, root, client):
        gates.set_mode(root, "none")
        body = client.get("/api/history").json()
        assert body["gate"]["mode"] == "none"
        # Printed verbatim by the panel; it is the whole disclosure.
        assert body["gate"]["label"] == gates.LABELS["none"]


class TestFilters:
    def test_seat_outcome_and_text(self, root, client):
        art = queue.add(root, "art", "paint the HUD")
        tech = queue.add(root, "tech", "wire the HUD")
        dead = queue.add(root, "art", "a broken thing")
        _close(root, art["id"])
        _close(root, tech["id"])
        _close(root, dead["id"], failed=True)

        by_seat = client.get("/api/history?seat=art").json()
        assert {i["id"] for i in by_seat["items"]} == {art["id"], dead["id"]}

        by_outcome = client.get("/api/history?outcome=failed").json()
        assert [i["id"] for i in by_outcome["items"]] == [dead["id"]]

        by_text = client.get("/api/history?q=HUD").json()
        assert {i["id"] for i in by_text["items"]} == {art["id"], tech["id"]}

    def test_facets_exclude_their_own_filter_and_honour_the_others(self, root, client):
        for seat, failed in (("art", False), ("art", True), ("tech", False)):
            item = queue.add(root, seat, "HUD work")
            _close(root, item["id"], failed=failed)
        queue_other = queue.add(root, "art", "unrelated")
        _close(root, queue_other["id"])

        facets = client.get("/api/history?q=HUD&seat=art").json()["facets"]
        # Outcomes ignore the seat? No — they honour it, and ignore only outcome.
        assert facets["outcomes"] == {"done": 1, "failed": 1}
        # Seats ignore the seat filter, so switching seat is possible from here.
        assert {s["seat"] for s in facets["seats"]} == {"art", "tech"}

    def test_paging_reports_a_true_total(self, root, client):
        for n in range(7):
            item = queue.add(root, "art", f"item {n}")
            _close(root, item["id"])
        page = client.get("/api/history?limit=3").json()["page"]
        assert page == {"limit": 3, "offset": 0, "total": 7, "next_offset": 3}
        last = client.get("/api/history?limit=3&offset=6").json()["page"]
        assert last["next_offset"] is None


def _write_log(root, item_id, events):
    path = root / ".bgate" / "agents" / f"item-{item_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                    encoding="utf-8")
    return path


def _say(text):
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class TestTheLogIsWindowed:
    def test_a_long_transcript_never_ships_whole(self, root, client):
        item = queue.add(root, "gameplay", "chatty")
        _close(root, item["id"])
        _write_log(root, item["id"],
                   [{"type": "bgate_run_start", "item_id": item["id"]}]
                   + [_say(f"line {n}") for n in range(500)])

        body = client.get(f"/api/history/{item['id']}/log?limit=60").json()
        assert body["total"] == 501            # the run marker plus 500 says
        assert len(body["steps"]) == 60, "the browser must never get all of it"

    def test_it_opens_at_the_end_where_the_answer_is(self, root, client):
        item = queue.add(root, "tech", "chatty")
        _close(root, item["id"])
        _write_log(root, item["id"], [_say(f"line {n}") for n in range(300)])

        body = client.get(f"/api/history/{item['id']}/log?limit=20").json()
        assert body["offset"] == 280
        assert body["steps"][-1]["text"] == "line 299"

    def test_runs_are_separated_and_selectable(self, root, client):
        item = queue.add(root, "art", "redispatched")
        _close(root, item["id"])
        _write_log(root, item["id"], [
            {"type": "bgate_run_start", "item_id": item["id"]}, _say("first run"),
            {"type": "bgate_run_start", "item_id": item["id"]}, _say("second run"),
        ])
        all_runs = client.get(f"/api/history/{item['id']}/log").json()
        assert all_runs["runs"] == 2
        only_two = client.get(f"/api/history/{item['id']}/log?run=2").json()
        assert [s["text"] for s in only_two["steps"]] == ["run 2 started",
                                                          "second run"]

    def test_search_returns_indices_and_lands_on_the_first_hit(self, root, client):
        item = queue.add(root, "gameplay", "chatty")
        _close(root, item["id"])
        events = [_say(f"line {n}") for n in range(200)]
        events[40] = _say("the needle is here")
        events[150] = _say("the needle again")
        _write_log(root, item["id"], events)

        body = client.get(f"/api/history/{item['id']}/log?q=needle&limit=10").json()
        assert body["matches"] == [40, 150]
        assert body["offset"] == 40, "the window opens on the first hit"

    def test_a_clipped_step_expands_by_byte_offset(self, root, client):
        item = queue.add(root, "tech", "verbose")
        _close(root, item["id"])
        long = "x" * (H.TEXT_CAP * 3)
        _write_log(root, item["id"], [_say("short"), _say(long)])

        listed = client.get(f"/api/history/{item['id']}/log").json()
        step = listed["steps"][-1]
        assert len(step["text"]) == H.TEXT_CAP
        assert step["full"] == len(long), "the client must know what it is missing"

        whole = client.get(
            f"/api/history/{item['id']}/log/step?off={step['off']}").json()
        assert whole["text"] == long

    def test_the_index_is_rebuilt_when_the_log_grows(self, root, client):
        item = queue.add(root, "art", "growing")
        _close(root, item["id"])
        path = _write_log(root, item["id"], [_say("one")])
        assert client.get(f"/api/history/{item['id']}/log").json()["total"] == 1
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_say("two")) + "\n")
        assert client.get(f"/api/history/{item['id']}/log").json()["total"] == 2

    def test_no_log_on_disk_says_so_instead_of_erroring(self, root, client):
        item = queue.add(root, "narrative", "never dispatched")
        _close(root, item["id"])
        body = client.get(f"/api/history/{item['id']}/log").json()
        assert body["steps"] == []
        assert "no agent log" in body["note"]

    def test_the_brief_and_result_ride_along_with_the_log(self, root, client):
        item = queue.add(root, "gameplay", "the work", brief="do exactly this")
        _close(root, item["id"], result="here is what I did")
        body = client.get(f"/api/history/{item['id']}/log").json()
        assert body["item"]["brief"] == "do exactly this"
        assert body["item"]["result"] == "here is what I did"

    def test_an_unknown_item_is_a_404_not_an_empty_page(self, root, client):
        assert client.get("/api/history/999999/log").status_code == 404


# ---------------------------------------------------------------------------
# What the run PRODUCED
#
# The complaint, verbatim: "on overview agent work specifically art, i cant see
# the work generated". The drawer showed the verdict, the prose and the
# transcript, and none of the pictures. These pin the four sources that answer
# it and, more importantly, the two ways the answer can be WRONG: claiming a
# file the run only read, and letting a path out of a transcript address
# anything outside the project.
# ---------------------------------------------------------------------------
def _wrote(root, item_id, *rels, tool="Write"):
    """Fake the harness's PreToolUse write record for an execution."""
    path = root / ".bgate" / "writes" / f"item-{item_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rel in rels:
            fh.write(json.dumps({"t": "2026-08-09 15:22:48", "path": rel,
                                 "seat": "art", "tool": tool}) + "\n")


_PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _png(root, rel, mtime=None):
    """A real (tiny) PNG on disk — the endpoint stats every path it reports."""
    import os
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_PNG_1PX)
    if mtime is not None:
        os.utime(target, (mtime, mtime))
    return target


def _tool_use(name, **inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": name, "input": inp}]}}


def _tool_result(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": text}]}}


class TestProducedWork:
    def test_the_harness_write_log_is_the_file_list(self, root, client):
        item = queue.add(root, "art", "make the lights")
        _close(root, item["id"])
        target = root / "game" / "assets" / "cookie.tres"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        _wrote(root, item["id"], "game/assets/cookie.tres")

        body = client.get(f"/api/history/{item['id']}/work").json()
        assert [f["rel"] for f in body["produced"]] == ["game/assets/cookie.tres"]
        assert body["produced"][0]["origin"] == "harness"
        assert body["counts"]["observed"] == 1

    def test_harness_bookkeeping_is_counted_not_listed(self, root, client):
        """`.bgate/` writes are Builders Gate's own paperwork. Listing them
        beside a deliverable is the conflation writelog.split exists to undo."""
        item = queue.add(root, "gameplay", "work")
        _close(root, item["id"])
        _wrote(root, item["id"], ".bgate/progress/item-1.jsonl")

        body = client.get(f"/api/history/{item['id']}/work").json()
        assert body["produced"] == []
        assert body["harness"]["count"] == 1

    def test_a_recorded_path_that_is_gone_is_counted_not_linked(self, root, client):
        item = queue.add(root, "tech", "work")
        _close(root, item["id"])
        _wrote(root, item["id"], "game/deleted_later.gd")

        body = client.get(f"/api/history/{item['id']}/work").json()
        assert body["produced"] == []
        assert body["missing"] == 1, "a dead path is not something to click"

    def test_a_registered_artifact_brings_its_review_state(self, root, client):
        from bgate_core import artifacts
        item = queue.add(root, "art", "portraits")
        _close(root, item["id"])
        _png(root, "game/assets/hero.png")
        art = artifacts.register(root, "hero", "game/assets/hero.png",
                                 producer="art", work_item_id=item["id"])
        artifacts.qa_verdict(root, art["id"], passed=True, note="on model",
                             score=91)

        body = client.get(f"/api/history/{item['id']}/work").json()
        row = body["produced"][0]
        assert row["origin"] == "artifact"
        assert row["image"] is True
        assert row["logical_name"] == "hero"
        assert row["qa"]["verdict"] == "pass"

    def test_a_registered_artifact_is_not_doubled_by_the_write_log(self, root, client):
        from bgate_core import artifacts
        item = queue.add(root, "art", "portraits")
        _close(root, item["id"])
        _png(root, "game/assets/hero.png")
        artifacts.register(root, "hero", "game/assets/hero.png",
                           producer="art", work_item_id=item["id"])
        _wrote(root, item["id"], "game/assets/hero.png")

        body = client.get(f"/api/history/{item['id']}/work").json()
        assert len(body["produced"]) == 1
        assert body["produced"][0]["origin"] == "artifact", \
            "the registry is the richer record of the same file"

    def test_the_transcript_recovers_files_with_no_write_log(self, root, client):
        """The fallback for runs that predate the harness write log."""
        item = queue.add(root, "tech", "older run")
        _close(root, item["id"])
        (root / "game").mkdir(parents=True, exist_ok=True)
        (root / "game" / "old.gd").write_text("pass", encoding="utf-8")
        _write_log(root, item["id"], [
            _tool_use("Write", file_path=str(root / "game" / "old.gd")),
            _tool_use("Read", file_path=str(root / "game" / "other.gd")),
        ])
        body = client.get(f"/api/history/{item['id']}/work").json()
        assert [f["rel"] for f in body["produced"]] == ["game/old.gd"]
        assert body["produced"][0]["origin"] == "transcript"

    def test_reads_are_counted_never_listed(self, root, client):
        item = queue.add(root, "gameplay", "reader")
        _close(root, item["id"])
        _write_log(root, item["id"], [
            _tool_use("Read", file_path="a.gd"),
            _tool_use("Grep", path="b.gd"),
        ])
        body = client.get(f"/api/history/{item['id']}/work").json()
        assert body["produced"] == []
        assert body["read_only"]["count"] == 2


class TestUntrustedPathsFromTheTranscript:
    """Transcript paths are AGENT OUTPUT. Everything here is hostile input."""

    def test_a_path_escaping_the_project_is_dropped(self, root, client, tmp_path):
        outside = tmp_path.parent / "outside_secret.png"
        outside.write_bytes(_PNG_1PX)
        item = queue.add(root, "art", "escaper")
        _close(root, item["id"])
        _write_log(root, item["id"], [
            _tool_use("Write", file_path=str(outside)),
            _tool_use("Write", file_path="../../../etc/passwd"),
            _tool_result(f"wrote {outside.as_posix()}"),
        ])
        body = client.get(f"/api/history/{item['id']}/work").json()
        rels = [f["rel"] for f in body["produced"] + body["captures"]]
        assert rels == [], f"a path outside the project was surfaced: {rels}"

    def test_a_traversal_dressed_as_a_relative_path_is_dropped(self, root, client):
        item = queue.add(root, "art", "traverser")
        _close(root, item["id"])
        _wrote(root, item["id"], "game/../../elsewhere.png")
        body = client.get(f"/api/history/{item['id']}/work").json()
        assert body["produced"] == []

    def test_a_path_that_is_not_a_file_is_dropped(self, root, client):
        item = queue.add(root, "tech", "phantom")
        _close(root, item["id"])
        (root / "game").mkdir(parents=True, exist_ok=True)
        _write_log(root, item["id"], [_tool_use("Write", file_path="game")])
        body = client.get(f"/api/history/{item['id']}/work").json()
        assert body["produced"] == [], "a directory is not a produced file"


class TestCapturesAreAttributed:
    """THE #300 BUG. Scanning a transcript for image paths finds every frame the
    agent READ as well as every frame it made, and #300's drawer duly offered
    another item's backdrop studies as its own output."""

    def _ran_at(self, root, item_id, when):
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET created_at = ?, updated_at = ? "
                         "WHERE id = ?", (when, when, item_id))

    def test_a_frame_named_for_the_item_counts(self, root, client):
        item = queue.add(root, "art", "shots")
        _close(root, item["id"])
        _png(root, f".bgate_out/shots/item{item['id']}_a-2026.png")
        body = client.get(f"/api/history/{item['id']}/work").json()
        assert len(body["captures"]) == 1
        assert body["captures"][0]["by_name"] is True

    def test_another_items_reference_frame_is_not_claimed(self, root, client):
        """It is in the transcript, it is under .bgate_out, and it predates the
        run. Contact is not authorship."""
        item = queue.add(root, "art", "reader")
        _close(root, item["id"])
        old = _png(root, ".bgate_out/art/i284_backdrop.png", mtime=1_600_000_000)
        self._ran_at(root, item["id"], "2026-08-09 15:20:00")
        _write_log(root, item["id"],
                   [_tool_result('{"path": "' + old.as_posix() + '"}')])

        body = client.get(f"/api/history/{item['id']}/work").json()
        assert body["captures"] == [], "a frame the run only read was claimed"

    def test_a_frame_written_during_the_run_counts(self, root, client):
        import datetime as _dt
        item = queue.add(root, "art", "renderer")
        _close(root, item["id"])
        when = _dt.datetime(2026, 8, 9, 15, 20, tzinfo=_dt.timezone.utc)
        fresh = _png(root, ".bgate_out/art/untagged_render.png",
                     mtime=when.timestamp())
        self._ran_at(root, item["id"], "2026-08-09 15:20:00")
        _write_log(root, item["id"],
                   [_tool_result('{"path": "' + fresh.as_posix() + '"}')])

        body = client.get(f"/api/history/{item['id']}/work").json()
        assert [c["rel"] for c in body["captures"]] == \
            [".bgate_out/art/untagged_render.png"]
        assert body["captures"][0]["by_name"] is False

    def test_item_33_does_not_claim_item_334s_captures(self, root, client):
        """The digit-boundary collision a bare substring match gets wrong."""
        small = queue.add(root, "art", "item 33")
        _close(root, small["id"])
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET id = 33 WHERE id = ?",
                         (small["id"],))
        _png(root, ".bgate_out/shots/item334_frame.png")
        _png(root, ".bgate_out/shots/item33_frame.png")

        body = client.get("/api/history/33/work").json()
        assert [c["name"] for c in body["captures"]] == ["item33_frame.png"]


class TestWorkEndpointShape:
    def test_it_says_whether_there_is_a_diff_without_computing_one(self, root, client):
        item = queue.add(root, "tech", "work")
        _close(root, item["id"])
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET base_commit = 'abc123def456789' "
                         "WHERE id = ?", (item["id"],))
        body = client.get(f"/api/history/{item['id']}/work").json()
        assert body["diff"] == {"available": True, "base": "abc123def456"}

    def test_no_base_commit_means_no_diff_offered(self, root, client):
        item = queue.add(root, "tech", "work")
        _close(root, item["id"])
        assert client.get(f"/api/history/{item['id']}/work"
                          ).json()["diff"]["available"] is False

    def test_an_unknown_item_is_a_404(self, root, client):
        assert client.get("/api/history/999999/work").status_code == 404

    def test_the_list_payload_stays_free_of_all_this(self, root, client):
        """Artifacts are resolved when a row is OPENED. Statting every produced
        file for forty rows is the one thing that would make the list slow."""
        item = queue.add(root, "art", "work")
        _close(root, item["id"])
        row = client.get("/api/history").json()["items"][0]
        assert "produced" not in row and "captures" not in row
