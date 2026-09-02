"""Generated music: the adapter's Suno contract, and the asset it becomes.

THE HTTP SEAM IS STUBBED, ALWAYS. A real Suno call costs credits and takes
minutes, so ``kie._request`` and ``kie.download`` are replaced with a fake that
answers exactly the shapes kie's reference documents — including the ones the
old code got wrong. Nothing here reaches the network; a test that did would be
a test nobody runs twice.

WHAT IS ACTUALLY BEING PINNED, since most of this is one call deep:

  * the four Suno facts that cost money to get wrong — the per-model character
    ceilings, duration being V5_5-only, style/title being custom-mode-only, and
    CALLBACK_EXCEPTION not meaning the audio failed;
  * that an UNPRICED run reports as unpriced and never lands in the dollar
    totals as $0.00 — it writes a zero-dollar MARKER row that spend.totals
    reports apart as `unaccounted`, so the report names the gap instead of
    silently under-counting the main provider;
  * that a generated take is a CANDIDATE and only a human's keep() puts it
    where the game can load it.
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from bgate_adapters import kie
from bgate_core.store import artifacts
from bgate_core.audio import music
from bgate_core.board import spend
from bgate_ui.app import app

# One request, two takes — what the record-info reference's own example shows.
TRACKS = [
    {"id": "t1", "audioUrl": "https://kie.invalid/one.mp3", "title": "Night Run",
     "streamAudioUrl": "https://kie.invalid/one", "imageUrl": "",
     "modelName": "chirp-v5", "tags": "synth", "duration": 98.4},
    {"id": "t2", "audioUrl": "https://kie.invalid/two.mp3", "title": "Night Run II",
     "streamAudioUrl": "https://kie.invalid/two", "imageUrl": "",
     "modelName": "chirp-v5", "tags": "synth", "duration": 101.2},
]


class FakeKie:
    """kie's two Suno endpoints plus the credit balance, per the reference."""

    def __init__(self, *, status="SUCCESS", tracks=TRACKS, balance=1000.0,
                 charge=24.0, error=""):
        self.status, self.tracks = status, tracks
        self.balance, self.charge, self.error = balance, charge, error
        self.submitted: list[dict] = []
        self.downloads: list[str] = []

    def request(self, path, key, *, payload=None, params=None, method="GET",
                timeout=60.0):
        if path == kie.CREDIT_PATH:
            return {"data": self.balance}
        if path == kie.SUNO_CREATE:
            self.submitted.append(dict(payload or {}))
            self.balance -= self.charge
            return {"taskId": "task-1"}
        if path == kie.SUNO_RECORD:
            return {"taskId": "task-1", "status": self.status,
                    "errorMessage": self.error or None,
                    "response": {"taskId": "task-1", "sunoData": self.tracks}}
        raise AssertionError(f"the test hit an unstubbed kie path: {path}")

    def download(self, url, out_path, *, timeout=300.0, accept="*/*"):
        """EVERY TAKE GETS DIFFERENT BYTES, and that is not cosmetic.

        This wrote the same 2051 bytes for every download, so the two takes of a
        batch were byte-identical and hashed the same. `installed` is measured
        by comparing the installed file's hash against the one the record
        claims, which is the whole mechanism for noticing that take 2 has
        overwritten take 1 — and with identical hashes there is nothing to
        notice, so both takes correctly read as installed and the overwrite test
        could never have passed. Two real takes of one prompt are never the same
        audio.
        """
        from pathlib import Path

        self.downloads.append(str(url))
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        seed = f"{url}|{len(self.downloads)}".encode()
        out.write_bytes(b"ID3" + seed + b"\0" * 2048)
        return out.stat().st_size


@pytest.fixture()
def fake(monkeypatch):
    stub = FakeKie()
    monkeypatch.setenv("KIE_API_KEY", "stub-key-never-used")
    monkeypatch.delenv("BGATE_KIE_USD_PER_CREDIT", raising=False)
    monkeypatch.setattr(kie, "_request", stub.request)
    monkeypatch.setattr(kie, "download", stub.download)
    return stub


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


# ---------------------------------------------------------------------------
# The adapter's contract with Suno
# ---------------------------------------------------------------------------
class TestSunoRequestShape:
    def test_simple_mode_caps_the_prompt_at_500(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_music("x" * 501)
        assert "500" in str(exc.value)

    def test_custom_mode_ceiling_moves_with_the_model(self):
        # V4 is the odd one out at 3,000; everything newer takes 5,000.
        kie.build_music("x" * 3000, model="V4", custom=True)
        with pytest.raises(kie.KieError):
            kie.build_music("x" * 3001, model="V4", custom=True)
        kie.build_music("x" * 5000, model="V5", custom=True)

    def test_style_and_title_are_refused_in_simple_mode(self):
        # Refused rather than dropped: a dropped field is still charged for.
        with pytest.raises(kie.KieError) as exc:
            kie.build_music("a march", style="brass")
        assert "custom" in str(exc.value)

    def test_duration_is_v5_5_only(self):
        assert kie.build_music("hum", model="V5_5", duration=45)["duration"] == 45
        with pytest.raises(kie.KieError) as exc:
            kie.build_music("hum", model="V5", duration=45)
        assert "V5_5" in str(exc.value)

    def test_duration_is_bounded(self):
        with pytest.raises(kie.KieError):
            kie.build_music("hum", model="V5_5", duration=9)
        with pytest.raises(kie.KieError):
            kie.build_music("hum", model="V5_5", duration=361)

    def test_vocal_gender_needs_a_vocalist(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_music("song", vocal_gender="f", instrumental=True)
        assert "instrumental" in str(exc.value)

    def test_weights_are_clamped_not_refused(self):
        payload = kie.build_music("hum", styleWeight=1.9, audioWeight=-1)
        assert payload["styleWeight"] == 1.0 and payload["audioWeight"] == 0.0

    def test_an_unknown_field_is_refused(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_music("hum", tempo=120)
        assert "tempo" in str(exc.value)

    def test_instrumental_defaults_true(self):
        # A game default, not an API one — see build_music.
        assert kie.build_music("hum")["instrumental"] is True


class TestSunoLimitsAreIntrospectable:
    """The UI builds its form from these, so they must not drift from the
    tables build_music enforces."""

    def test_limits_match_the_enforced_tables(self):
        assert kie.music_limits("V4")["custom"]["prompt"] == 3000
        assert kie.music_limits("V5")["custom"]["prompt"] == 5000
        assert kie.music_limits("V5")["simple"]["prompt"] == kie.SUNO_SIMPLE_PROMPT

    def test_duration_is_none_for_models_that_do_not_take_one(self):
        assert kie.music_limits("V5")["duration"] is None
        assert kie.music_limits("V5_5")["duration"] == [10, 360]

    def test_options_covers_every_model(self):
        got = kie.music_options()
        assert set(got["limits"]) == set(kie.SUNO_MODELS)
        assert got["retention_days"] == 14

    def test_an_unknown_model_is_named(self):
        with pytest.raises(kie.KieError):
            kie.music_limits("V9")


class TestSunoStatuses:
    def test_all_eight_documented_statuses_are_known(self):
        assert len(kie.SUNO_STATUSES) == 8
        classified = (set(kie.SUNO_RUNNING) | set(kie.SUNO_DEAD)
                      | {kie.SUNO_DONE, kie.SUNO_CALLBACK_FAILED})
        assert classified == set(kie.SUNO_STATUSES)

    def test_callback_exception_with_audio_is_not_a_failure(self, root, fake):
        # kie could not deliver its webhook. The music rendered and was billed.
        fake.status = "CALLBACK_EXCEPTION"
        record = kie.poll_music("task-1", root=root, timeout=5)
        assert record["callback_failed"] is True
        assert len(kie.music_tracks(record)) == 2

    def test_callback_exception_with_no_audio_is_a_failure(self, root, fake):
        fake.status, fake.tracks = "CALLBACK_EXCEPTION", []
        with pytest.raises(kie.KieError) as exc:
            kie.poll_music("task-1", root=root, timeout=5)
        assert "no audio" in str(exc.value)

    def test_a_sensitive_word_error_says_what_to_do(self, root, fake):
        fake.status = "SENSITIVE_WORD_ERROR"
        with pytest.raises(kie.KieError) as exc:
            kie.poll_music("task-1", root=root, timeout=5)
        assert "reword" in str(exc.value)

    def test_an_unknown_status_stops_rather_than_spins(self, root, fake):
        fake.status = "ASCENDED"
        with pytest.raises(kie.KieError) as exc:
            kie.poll_music("task-1", root=root, timeout=5)
        assert "unknown status" in str(exc.value)


class TestTheUrlIsNeverTheAsset:
    def test_every_track_is_downloaded_inside_the_call(self, root, fake, tmp_path):
        result = kie.generate_music("hum", str(tmp_path / "out"), name="hum",
                                    root=root)
        assert result["ok"] and result["count"] == 2
        assert fake.downloads == [t["audioUrl"] for t in TRACKS]
        for track in result["tracks"]:
            assert (tmp_path / "out").joinpath(
                track["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]).is_file()

    def test_the_expiry_is_stamped_on_the_result(self, root, fake, tmp_path):
        result = kie.generate_music("hum", str(tmp_path / "o"), root=root)
        assert result["retention_days"] == 14
        assert result["expires_at"]        # an ISO date, not "someday"


# ---------------------------------------------------------------------------
# Money — the thing that must never read as free
# ---------------------------------------------------------------------------
class TestUnpricedSaysSo:
    def test_no_rate_configured_means_no_dollars_and_a_named_gap(self, root, fake):
        result = music.generate(root, "a tense loop", name="tense")
        assert result["ok"]
        # The credits ARE known (balance delta); the dollars are not.
        assert result["credits_consumed"] == 24.0
        assert result["credits_source"] == "balance_delta"
        assert result["estimated_usd"] is None
        assert result["accounted"] is False
        assert result["cost_note"]                     # says why, in words
        totals = spend.totals(root)
        # No invented dollars anywhere...
        assert totals["by_kind"].get("audio") in (None, 0)
        assert totals["project_usd"] == 0
        # ...but the charge is NOT silently dropped from the report: with
        # budgets off by default the totals ARE the product, and a kie-heavy
        # project must not read as free.
        assert totals["unaccounted"]["rows"] == 1
        assert totals["unaccounted"]["credits"] == pytest.approx(24.0)

    def test_a_configured_rate_lands_in_the_ledger(self, root, fake, monkeypatch):
        monkeypatch.setenv("BGATE_KIE_USD_PER_CREDIT", "0.002")
        result = music.generate(root, "a calm loop", name="calm")
        assert result["estimated_usd"] == pytest.approx(0.048)
        assert result["accounted"] is True
        assert spend.totals(root)["by_kind"]["audio"] == pytest.approx(0.048)

    def test_an_unreadable_balance_leaves_the_run_unpriced(self, root, fake,
                                                           monkeypatch):
        monkeypatch.setattr(kie, "credit_balance", lambda *a, **k: None)
        result = music.generate(root, "a loop", name="loop")
        assert result["credits_consumed"] is None
        assert result["credits_source"] == "unavailable"
        assert result["estimated_usd"] is None
        # Even with no credit count, the CHARGE is real: it still lands as an
        # unaccounted row, with the credits marked unknown rather than zero.
        un = spend.totals(root)["unaccounted"]
        assert un["rows"] == 1 and un["credits_unknown_rows"] == 1

    def test_a_topup_mid_run_is_not_reported_as_free(self, root, fake):
        # Negative delta = the balance went UP. That is not a price of zero.
        fake.charge = -50.0
        result = music.generate(root, "a loop", name="loop")
        assert result["credits_consumed"] is None
        assert result["credits_source"] == "balance_delta_unusable"

    def test_price_for_is_never_zero(self):
        assert kie.price_for() is None

    def test_a_failed_run_says_the_cost_is_unknown_not_absent(self, root, fake,
                                                               tmp_path):
        # A failed run used to carry none of these keys, so a caller doing
        # `result.get("credits_consumed", 0)` scored a charge that may well have
        # happened as free — the same folding-to-zero estimate_usd refuses to do
        # on the video side.
        fake.status, fake.tracks = "SENSITIVE_WORD_ERROR", []
        got = kie.generate_music("hum", str(tmp_path / "o"), root=root)
        assert got["ok"] is False
        assert got["estimated_usd"] is None
        assert got["credits_consumed"] is None
        assert got["credits_source"] == "unavailable"
        assert got["accounted"] is False


class TestTheTaskIdReachesTheCallerAtSubmit:
    """generate_video has had this hook since it was written; generate_music
    took no callback at all, so its callers could only learn the id from a
    return value — which a killed process never produces."""

    def test_on_submit_fires_before_the_poll(self, root, fake, tmp_path,
                                              monkeypatch):
        # BEFORE, not after: the poll is the minutes-long window in which dying
        # costs money, so an id handed over on the far side of it is an id
        # nobody ever gets.
        order = []
        polled = kie.poll_music

        def watch(*args, **kwargs):
            order.append("poll")
            return polled(*args, **kwargs)

        monkeypatch.setattr(kie, "poll_music", watch)
        kie.generate_music("hum", str(tmp_path / "o"), root=root,
                           on_submit=lambda task_id: order.append(task_id))
        assert order == ["task-1", "poll"]

    def test_a_broken_on_submit_does_not_lose_the_batch(self, root, fake,
                                                         tmp_path):
        # Bookkeeping must never lose the audio it was bookkeeping.
        def boom(_task_id):
            raise RuntimeError("the ledger is on fire")

        got = kie.generate_music("hum", str(tmp_path / "o"), root=root,
                                 on_submit=boom)
        assert got["ok"] and got["count"] == 2


# ---------------------------------------------------------------------------
# A take becomes an asset
# ---------------------------------------------------------------------------
class TestCandidates:
    def test_every_take_is_a_candidate_revision_of_one_logical_name(self, root,
                                                                    fake):
        result = music.generate(root, "night chase", name="Chase Theme")
        assert result["logical_name"] == "chase-theme"
        rows = artifacts.list_revisions(root, logical_name="chase-theme")
        assert [r["revision"] for r in rows] == [2, 1]
        assert {r["status"] for r in rows} == {"candidate"}
        assert {r["producer"] for r in rows} == {music.PRODUCER}

    def test_provenance_records_the_url_and_when_it_dies(self, root, fake):
        music.generate(root, "night chase", name="chase")
        meta = artifacts.list_revisions(root, logical_name="chase")[0]["metadata"]
        assert meta["source_url"].startswith("https://kie.invalid/")
        assert meta["source_url_expires_at"]
        assert meta["task_id"] == "task-1"

    def test_the_gallery_only_offers_this_producer_s_takes(self, root, fake):
        music.generate(root, "night chase", name="chase")
        hand_mixed = root / "audio" / "hand.wav"
        hand_mixed.parent.mkdir(parents=True, exist_ok=True)
        hand_mixed.write_bytes(b"RIFF")
        artifacts.register(root, "hand", hand_mixed, producer="audiolab")
        names = {c["logical_name"] for c in music.candidates(root)}
        assert names == {"chase"}, "a hand-mixed sound must not be discardable here"

    def test_a_candidate_is_playable_through_the_dashboard(self, root, fake):
        music.generate(root, "night chase", name="chase")
        one = music.candidates(root)[0]
        assert one["url"].startswith("/api/audio/file?rel=.bgate_out/audio/")
        assert one["exists"] is True


class TestTheApprovalGateBeingOffMustStillDeliver:
    """THE BUG THAT SHIPPED, and the one worth a class of its own.

    keep() was written on the assumption that a take is a candidate until a
    human keeps it, and that keeping is what installs it. On a project whose
    approval gate is off, ``artifacts.register`` approves the revision INSIDE
    the register call — so there is no candidate, no keep, and nothing was ever
    copied into the engine project. Measured on a live project: gate.mode=none,
    both takes ``approved``, ``reviewed_by=setting:art.auto_approve``, no install
    metadata, ``game/assets/audio/music/`` never created, both .mp3s still in
    .bgate_out. The database said approved and the game had nothing.
    """

    @pytest.fixture()
    def gate_off(self, root):
        from bgate_core.board import gates

        gates.set_mode(root, gates.NONE)
        return root

    def test_a_take_is_installed_even_though_nobody_kept_it(self, gate_off, fake):
        result = music.generate(gate_off, "night chase", name="chase")
        assert result["auto_installed"], "the gate is off and nothing was delivered"
        assert (gate_off / "game/assets/audio/music/chase.mp3").is_file()
        assert result["gate"], "the seat has to be told why nobody was asked"

    def test_only_the_surviving_approved_take_is_installed(self, gate_off, fake):
        # register() supersedes each earlier approval as the next lands, so
        # installing every take would race N copies into one destination.
        music.generate(gate_off, "night chase", name="chase")
        live = [k for k in music.kept(gate_off) if k["installed"]]
        assert len(live) == 1 and live[0]["status"] in ("approved", "integrated")

    def test_a_failed_auto_install_does_not_lose_the_generation(self, gate_off,
                                                                fake, monkeypatch):
        monkeypatch.setattr(music, "_install_file", lambda *a, **k:
                            (_ for _ in ()).throw(music.MusicError("disk full")))
        result = music.generate(gate_off, "night chase", name="chase")
        assert result["ok"] and result["count"] == 2
        assert not result.get("auto_installed")
        # ...and the seat can see the gap and offer the repair button.
        assert all(not k["installed"] for k in music.kept(gate_off))

    def test_install_repairs_a_take_approved_before_this_existed(self, gate_off,
                                                                 fake):
        music.generate(gate_off, "night chase", name="chase")
        art_id = [k for k in music.kept(gate_off) if k["installed"]][0]["artifact_id"]
        (gate_off / "game/assets/audio/music/chase.mp3").unlink()
        assert [k for k in music.kept(gate_off)
                if k["artifact_id"] == art_id][0]["install_missing"]
        got = music.install(gate_off, art_id)
        assert (gate_off / got["install"]["path"]).is_file()
        assert got["artifact"]["installed"] is True

    def test_install_refuses_a_rejected_take(self, root, fake):
        music.generate(root, "night chase", name="chase")
        one = music.candidates(root)[0]
        music.discard(root, one["artifact_id"], note="no")
        with pytest.raises(music.MusicError):
            music.install(root, one["artifact_id"])

    def test_install_does_not_change_review_state(self, root, fake):
        # Installing an older take is not the same act as choosing it — keep()
        # is what re-approves. Confusing the two puts bytes in the game that the
        # approved revision disagrees with.
        music.generate(root, "night chase", name="chase")
        one = music.candidates(root)[0]
        music.install(root, one["artifact_id"])
        assert artifacts.get(root, one["artifact_id"])["status"] == "candidate"


class TestInstalledMeansTheseBytes:
    """`installed` is a claim about the file the game loads, so it is measured
    against the asset registry's hash — not against the file merely existing.
    Every take of a batch installs to the SAME destination, so after take 2
    lands, take 1's record still points at a path that exists and holds someone
    else's audio. Both cards claimed to be in the game; only one was."""

    def test_an_overwritten_take_reads_as_stale_not_installed(self, root, fake):
        (root / "game" / "assets" / "audio").mkdir(parents=True, exist_ok=True)
        music.generate(root, "night chase", name="chase")
        first, second = music.candidates(root)[0], music.candidates(root)[1]
        music.keep(root, first["artifact_id"], actor="human")
        music.keep(root, second["artifact_id"], actor="human")
        by_id = {k["artifact_id"]: k for k in music.kept(root)}
        assert by_id[second["artifact_id"]]["installed"] is True
        loser = by_id[first["artifact_id"]]
        assert loser["installed"] is False and loser["install_stale"] is True

    def test_a_deleted_install_reads_as_missing_not_stale(self, root, fake):
        (root / "game" / "assets" / "audio").mkdir(parents=True, exist_ok=True)
        music.generate(root, "night chase", name="chase")
        one = music.candidates(root)[0]
        got = music.keep(root, one["artifact_id"], actor="human")
        (root / got["install"]["path"]).unlink()
        view = [k for k in music.kept(root)
                if k["artifact_id"] == one["artifact_id"]][0]
        assert view["installed"] is False and view["install_missing"] is True


class TestKeepingIsWhatMakesItReal:
    def test_keep_installs_under_the_engine_project_then_approves(self, root,
                                                                  fake):
        (root / "game" / "assets" / "audio").mkdir(parents=True, exist_ok=True)
        music.generate(root, "night chase", name="chase")
        one = music.candidates(root)[0]

        got = music.keep(root, one["artifact_id"], actor="a-human")

        installed = root / got["install"]["path"]
        assert got["install"]["path"] == "game/assets/audio/music/chase.mp3"
        assert installed.is_file() and installed.stat().st_size > 0
        assert got["artifact"]["status"] in ("approved", "integrated")
        assert got["artifact"]["installed"] is True

    def test_a_failed_copy_blocks_the_approval(self, root, fake, monkeypatch):
        # install-then-approve, and the install is NOT best-effort: an approval
        # the game cannot honour is worse than no approval.
        import shutil as _shutil

        music.generate(root, "night chase", name="chase")
        one = music.candidates(root)[0]
        monkeypatch.setattr(_shutil, "copy2", lambda *a, **k:
                            (_ for _ in ()).throw(OSError("read-only")))
        with pytest.raises(music.MusicError):
            music.keep(root, one["artifact_id"], actor="human")
        assert artifacts.get(root, one["artifact_id"])["status"] == "candidate"

    def test_the_kept_file_is_where_the_audio_lab_looks(self, root, fake):
        # The lab's file walk SKIPS .bgate_out, which is the whole reason keep()
        # copies instead of flipping a status column.
        from bgate_ui.routes import audiolab as lab

        (root / "game" / "assets" / "audio").mkdir(parents=True, exist_ok=True)
        music.generate(root, "night chase", name="chase")
        music.keep(root, music.candidates(root)[0]["artifact_id"], actor="human")
        assert "game/assets/audio" not in " ".join(lab.SKIP_DIRS)
        assert ".bgate_out" in lab.SKIP_DIRS

    def test_an_agent_may_not_keep(self, root, fake):
        music.generate(root, "night chase", name="chase")
        with pytest.raises(PermissionError):
            music.keep(root, music.candidates(root)[0]["artifact_id"],
                       actor="agent:item-7")

    def test_keeping_a_second_take_supersedes_the_first(self, root, fake):
        music.generate(root, "night chase", name="chase")
        takes = music.candidates(root)
        music.keep(root, takes[0]["artifact_id"], actor="human")
        music.keep(root, takes[1]["artifact_id"], actor="human")
        states = {r["revision"]: r["status"]
                  for r in artifacts.list_revisions(root, logical_name="chase")}
        assert sorted(states.values()) == sorted(["superseded",
                                                  states[takes[1]["revision"]]])

    def test_keeping_a_deleted_candidate_says_what_happened(self, root, fake):
        music.generate(root, "night chase", name="chase")
        one = music.candidates(root)[0]
        (root / one["path"]).unlink()
        with pytest.raises(music.MusicError) as exc:
            music.keep(root, one["artifact_id"], actor="human")
        assert "14 days" in str(exc.value)

    def test_keep_refuses_a_non_audio_artifact(self, root):
        image = root / ".bgate_out" / "art" / "hero.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"png")
        art = artifacts.register(root, "hero", image, producer=music.PRODUCER)
        with pytest.raises(music.MusicError):
            music.keep(root, art["id"], actor="human")


class TestDiscard:
    def test_discard_rejects_but_leaves_the_file(self, root, fake):
        music.generate(root, "night chase", name="chase")
        one = music.candidates(root)[0]
        got = music.discard(root, one["artifact_id"], note="drums too early")
        assert got["artifact"]["status"] == "rejected"
        assert got["artifact"]["review_note"] == "drums too early"
        # Unlinking it would make assets.verify() report `missing` forever.
        assert (root / one["path"]).is_file()

    def test_an_agent_may_discard(self, root, fake):
        # Refusing to ship something is a decision a model is allowed to make.
        music.generate(root, "night chase", name="chase")
        got = music.discard(root, music.candidates(root)[0]["artifact_id"],
                            note="off-brief", actor="agent:item-7")
        assert got["artifact"]["status"] == "rejected"


class TestStatusReportsAndRecoverActs:
    def test_status_reports_a_finished_task_without_downloading(self, root, fake):
        got = music.status(root, "task-1")
        assert got["done"] and got["track_count"] == 2 and got["recoverable"]
        assert fake.downloads == []

    def test_a_running_task_is_not_done(self, root, fake):
        fake.status = "FIRST_SUCCESS"
        got = music.status(root, "task-1")
        assert got["running"] and not got["done"] and not got["failed"]
        assert got["stage"], "a status with no words is a spinner"

    def test_it_needs_a_task_id(self, root):
        with pytest.raises(music.MusicError):
            music.status(root, "  ")

    def test_recover_downloads_and_files_an_already_paid_batch(self, root, fake):
        # The door that had to exist: kie's CDN 403'd every download this
        # product made, so batches rendered, were billed, and were thrown away.
        got = music.recover(root, "task-1")
        assert got["ok"] and got["count"] == 2
        assert len(fake.downloads) == 2
        for c in got["candidates"]:
            assert (root / c["path"]).is_file()

    def test_recover_claims_no_cost(self, root, fake):
        # The charge happened at submit time, possibly days ago; a balance delta
        # measured now would be fiction.
        got = music.recover(root, "task-1")
        assert got["credits_consumed"] is None
        assert got["credits_source"] == "not_measurable_after_the_fact"
        assert got["estimated_usd"] is None

    def test_recover_is_idempotent_by_suno_track_id(self, root, fake):
        music.recover(root, "task-1")
        again = music.recover(root, "task-1")
        assert again["ok"] and again["count"] == 0 and again["skipped"] == 2
        assert len(fake.downloads) == 2, "it downloaded the same takes twice"

    def test_recover_lands_beside_an_earlier_attempt_at_the_same_task(self, root,
                                                                      fake):
        music.generate(root, "night chase", name="chase")
        # Same task id, one take already registered: the other one joins it
        # under the same logical name rather than starting a second asset.
        fake.tracks = TRACKS + [{"id": "t3", "audioUrl": "https://kie.invalid/3.mp3",
                                 "title": "Third", "duration": 90.0}]
        got = music.recover(root, "task-1")
        assert got["logical_name"] == "chase" and got["skipped"] == 2
        assert got["count"] == 1

    def test_recover_names_itself_from_the_track_title(self, root, fake):
        # A hex prefix is the last resort — an asset called `0fdd...` is one
        # nobody recognises in the mixer a week later.
        got = music.recover(root, "task-1")
        assert got["logical_name"] == "night-run"

    def test_recover_says_so_when_kie_holds_nothing(self, root, fake):
        fake.status, fake.tracks = "PENDING", []
        got = music.recover(root, "task-1")
        assert got["ok"] is False and "still running" in got["error"]


# ---------------------------------------------------------------------------
# Charged-and-lost: the window between the submit and the download
# ---------------------------------------------------------------------------
class Killed(RuntimeError):
    """The process dying mid-poll, which is the whole point of the ticket.

    Deliberately NOT a KieError: generate_music catches those and hands the task
    id back on the result, which is the path that already worked. What had never
    been survivable is the one where nothing gets returned at all.
    """


def _tickets(root):
    """Every pending ticket on disk, decoded, oldest name first."""
    directory = root / music.PENDING_DIR
    if not directory.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(directory.glob("*.json"))]


def _age_tickets(root, seconds):
    """Backdate every ticket, so a sweep sees an hour-old generation without
    the test sleeping for one."""
    when = (datetime.now(timezone.utc)
            - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    for path in (root / music.PENDING_DIR).glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["updated_at"] = when
        path.write_text(json.dumps(record), encoding="utf-8")


@contextlib.contextmanager
def _killed_at(where):
    """Kill the process at one point in the generation and put it back.

    NOT monkeypatch: the `fake` fixture shares this test's monkeypatch instance,
    so a mid-test ``undo()`` would also unstub kie's HTTP seam and every
    assertion after it would be measuring a failed network call instead of the
    thing under test — which is exactly what it did.
    """
    real = getattr(kie, where)

    def boom(*_a, **_k):
        raise Killed(f"the process died at {where}")

    setattr(kie, where, boom)
    try:
        yield
    finally:
        setattr(kie, where, real)


@contextlib.contextmanager
def _ticket_never_closed():
    """A crash between the download and the ticket being closed."""
    real = music._close_ticket
    music._close_ticket = lambda *_a, **_k: None
    try:
        yield
    finally:
        music._close_ticket = real


class TestTheTaskIdSurvivesTheProcess:
    """THE CHARGE LANDS AT SUBMIT AND THE POLL RUNS FOR MINUTES AFTERWARDS.

    The task id used to reach durable storage only inside _absorb — that is,
    after a download that had already succeeded — so the single window in which
    dying costs money was the only window in which nothing had been written
    down. recover() needs a task id; a killed process took the only copy with
    it.
    """

    def test_the_task_id_is_on_disk_before_the_poll_begins(self, root, fake):
        with _killed_at("poll_music"), pytest.raises(Killed):
            music.generate(root, "night chase", name="chase")

        ticket = _tickets(root)
        assert len(ticket) == 1
        assert ticket[0]["task_id"] == "task-1", \
            "the batch was charged for and the handle died with the process"
        assert ticket[0]["logical_name"] == "chase"

    def test_a_crash_after_submit_is_recoverable(self, root, fake):
        # The proof that the id is worth persisting: a fresh call collects the
        # audio kie is already holding, and pays nothing for it.
        with _killed_at("poll_music"), pytest.raises(Killed):
            music.generate(root, "night chase", name="chase")

        task_id = _tickets(root)[0]["task_id"]
        got = music.recover(root, task_id)
        assert got["ok"] and got["count"] == 2
        assert got["estimated_usd"] is None      # charged at submit, not now
        for candidate in got["candidates"]:
            assert (root / candidate["path"]).is_file()

    def test_the_recovery_lands_under_the_name_the_human_asked_for(self, root,
                                                                    fake):
        # No revision was ever registered, so the artifact scan has nothing to
        # match — before the ticket carried the name, this came back called
        # `night-run` (Suno's title) or twelve hex characters.
        with _killed_at("poll_music"), pytest.raises(Killed):
            music.generate(root, "night chase", name="Chase Theme")

        assert music.recover(root, "task-1")["logical_name"] == "chase-theme"

    def test_collecting_the_batch_closes_the_ticket(self, root, fake):
        with _killed_at("poll_music"), pytest.raises(Killed):
            music.generate(root, "night chase", name="chase")

        music.recover(root, "task-1")
        assert _tickets(root) == [], "a collected batch is owed nothing"

    def test_a_finished_generation_leaves_nothing_pending(self, root, fake):
        music.generate(root, "night chase", name="chase")
        assert _tickets(root) == []

    def test_a_prompt_kie_refused_is_not_reported_as_lost_money(self, root,
                                                                 fake):
        # Nothing was submitted, so nothing is owed. A sweep that cried wolf
        # over every rejected prompt is a sweep nobody reads.
        got = music.generate(root, "x" * 600, name="toolong")
        assert got["ok"] is False and not got.get("task_id")
        assert _tickets(root) == []

    def test_a_cancel_after_the_submit_stays_pending(self, root, fake):
        # kie hands the id back on the result here, but the batch is still paid
        # for and uncollected — which is precisely what the sweep is for.
        seen = []

        def stop(_f, _w, _s):
            seen.append(1)
            if len(seen) > 1:
                raise kie.MusicCancelled("the human pressed cancel")

        got = music.generate(root, "night chase", name="chase",
                             on_progress=stop)
        assert got["ok"] is False and got["task_id"] == "task-1"
        assert _tickets(root)[0]["task_id"] == "task-1"


class TestStuckAndPaidFor:
    """A dead poll loop used to be unrecoverable AND invisible. recover() has
    always been able to collect a batch — what was missing is anything that
    NOTICES, so recovery depended on a human remembering a task id they had
    never been shown."""

    def _stranded(self, root, *, age_s=3600, at_submit=False):
        """One generation charged for and abandoned, aged into the sweep."""
        where = "submit_music" if at_submit else "poll_music"
        with _killed_at(where), pytest.raises(Killed):
            music.generate(root, "night chase", name="chase")
        _age_tickets(root, age_s)

    def test_a_generation_inside_its_window_is_not_swept(self, root, fake,
                                                          monkeypatch):
        """A Suno batch renders in about three minutes. Calling one of those
        stuck is how a human pays twice for work that is running fine."""
        self._stranded(root, age_s=60)
        assert music.stuck_tracks(root, poll=False)["stale"] == 0

    def test_a_crash_mid_download_is_found_and_recovered(self, root, fake,
                                                          monkeypatch):
        """The window the prior review named: charged at submit, dead DURING the
        download — after the poll succeeded, before anything reached disk. The
        ticket was stamped at submit time, so the sweep still has the handle."""
        with _killed_at("download"), pytest.raises(Killed):
            music.generate(root, "night chase", name="chase")
        assert _tickets(root)[0]["task_id"] == "task-1", \
            "the on-submit stamp is what survives a mid-download death"
        _age_tickets(root, 3600)

        got = music.stuck_tracks(root)
        assert got["recoverable"] == 1
        one = got["tracks"][0]
        assert one["state"] == "recoverable" and one["task_id"] == "task-1"
        assert one["logical_name"] == "chase"

        # ...and the handle is worth money: recover() collects without repaying.
        collected = music.recover(root, "task-1")
        assert collected["ok"] and collected["count"] == 2
        assert music.stuck_tracks(root)["stale"] == 0

    def test_a_finished_batch_nobody_collected_is_found(self, root, fake,
                                                         monkeypatch):
        self._stranded(root)
        got = music.stuck_tracks(root)
        assert got["recoverable"] == 1
        one = got["tracks"][0]
        assert one["state"] == "recoverable" and one["task_id"] == "task-1"
        assert one["logical_name"] == "chase"
        assert "pays twice" in one["note"]

    def test_the_cheap_half_asks_kie_nothing(self, root, fake, monkeypatch):
        """It rides on every dashboard refresh, so it must cost no round trip."""
        self._stranded(root)

        def never(*_a, **_k):
            raise AssertionError("poll=False must not reach the provider")

        monkeypatch.setattr(music, "status", never)
        got = music.stuck_tracks(root, poll=False)
        assert got["tracks"][0]["state"] == "unpolled" and got["polled"] is False

    def test_a_job_still_running_is_not_reported_as_money_to_collect(
            self, root, fake, monkeypatch):
        self._stranded(root)
        fake.status, fake.tracks = "FIRST_SUCCESS", []
        got = music.stuck_tracks(root)
        assert got["tracks"][0]["state"] == "running" and got["recoverable"] == 0

    def test_a_dead_generation_is_not_offered_for_recovery(self, root, fake,
                                                            monkeypatch):
        self._stranded(root)
        fake.status, fake.tracks = "SENSITIVE_WORD_ERROR", []
        got = music.stuck_tracks(root)
        assert got["tracks"][0]["state"] == "failed" and got["recoverable"] == 0

    def test_a_provider_that_cannot_be_asked_is_unknown_not_resolved(
            self, root, fake, monkeypatch):
        self._stranded(root)

        def boom(*_a, **_k):
            raise kie.KieError("could not reach kie")

        monkeypatch.setattr(music, "status", boom)
        got = music.stuck_tracks(root)
        assert got["tracks"][0]["state"] == "unknown" and got["recoverable"] == 0

    def test_a_generation_with_no_task_id_is_its_own_category(self, root, fake,
                                                               monkeypatch):
        """The worst one: the submit left and the handle did not come back."""
        self._stranded(root, at_submit=True)
        got = music.stuck_tracks(root, poll=False)
        assert got["tracks"][0]["state"] == "lost"
        assert "no handle" in got["tracks"][0]["note"]

    def test_a_batch_already_filed_is_not_reported_as_money_to_spend_again(
            self, root, fake, monkeypatch):
        # A crash between the download and the ticket being closed leaves one of
        # these behind. The takes ARE registered; reporting them as collectable
        # is how a human pays for audio they already have.
        with _ticket_never_closed():
            music.generate(root, "night chase", name="chase")
        _age_tickets(root, 3600)

        got = music.stuck_tracks(root)
        assert got["tracks"][0]["state"] == "collected"
        assert got["recoverable"] == 0

    def test_recovering_clears_it_from_the_sweep(self, root, fake, monkeypatch):
        self._stranded(root)
        assert music.stuck_tracks(root)["recoverable"] == 1
        music.recover(root, "task-1")
        assert music.stuck_tracks(root)["stale"] == 0

    def test_the_sweep_says_the_deadline_it_is_racing(self, root, fake,
                                                       monkeypatch):
        # kie holds the audio for fourteen days; a recovery offered without its
        # expiry reads as an errand that can wait.
        self._stranded(root)
        assert music.stuck_tracks(root, poll=False)["retention_days"] == 14


class TestProgressIsReported:
    """A minute of unexplained spinner is indistinguishable from a hang, and
    what people do about an apparent hang is fire a second paid generation."""

    def test_every_suno_stage_reaches_the_caller_in_words(self, root, fake,
                                                           monkeypatch):
        seq = [{"status": s} for s in ("PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS")]
        seq.append({"status": "SUCCESS",
                    "response": {"sunoData": [TRACKS[0]]}})
        monkeypatch.setattr(kie, "music_record", lambda t, **kw: seq.pop(0))
        monkeypatch.setattr(kie.time, "sleep", lambda *_: None)
        seen = []
        kie.poll_music("t", root=root, timeout=30, interval=0,
                       on_progress=lambda f, w, s: seen.append((s, f, w)))
        assert [s for s, _f, _w in seen] == [
            "PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS", "SUCCESS"]
        assert all(words for _s, _f, words in seen)
        fractions = [f for _s, f, _w in seen]
        assert fractions == sorted(fractions), "a bar that goes backwards"

    def test_a_repeated_status_is_not_announced_twice(self, root, fake,
                                                       monkeypatch):
        seq = [{"status": "PENDING"}, {"status": "PENDING"},
               {"status": "SUCCESS", "response": {"sunoData": [TRACKS[0]]}}]
        monkeypatch.setattr(kie, "music_record", lambda t, **kw: seq.pop(0))
        monkeypatch.setattr(kie.time, "sleep", lambda *_: None)
        seen = []
        kie.poll_music("t", root=root, timeout=30, interval=0,
                       on_progress=lambda f, w, s: seen.append(s))
        assert seen == ["PENDING", "SUCCESS"]

    def test_cancelling_before_the_submit_costs_nothing(self, root, fake):
        """The first callback fires BEFORE anything is asked of Suno.

        Cancelling there used to escape uncaught, because that one announcement
        sat outside generate_music's try. It now returns like any other cancel,
        and it correctly reports nothing to recover: no task id, no charge.
        """
        def stop_immediately(_f, _w, _s):
            raise kie.MusicCancelled("the human pressed cancel")

        got = kie.generate_music("hum", str(root / "out"), root=root,
                                 on_progress=stop_immediately)
        assert got["ok"] is False and got["cancelled"] is True
        assert got["task_id"] == "" and got["recoverable"] is False

    def test_raising_from_the_callback_cancels_and_keeps_the_task_id(self, root,
                                                                     fake):
        # Cancel AFTER the submit, which is the case where money is at stake:
        # the first stage is the pre-submit announcement and nothing is charged
        # for it, so letting it through is what makes this test about recovery.
        seen = []

        def stop(_f, _w, _s):
            seen.append(1)
            if len(seen) > 1:
                raise kie.MusicCancelled("the human pressed cancel")

        got = kie.generate_music("hum", str(root / "out"), root=root,
                                 on_progress=stop)
        assert got["ok"] is False and got["cancelled"] is True
        # THE MONEY IS RECOVERABLE. Suno was already asked; losing the id here
        # would be losing the batch.
        assert got["task_id"] == "task-1" and got["recoverable"] is True
        assert "recover" in got["recover"]


# ---------------------------------------------------------------------------
# The dashboard surface
# ---------------------------------------------------------------------------
class TestTheRoutesAreMounted:
    def test_the_music_module_imported(self, client):
        data = client.get("/api/routes/status").json()
        assert data["failed"] == []
        assert "music" in data["registered"]

    def test_options_answers_with_the_adapter_s_tables(self, client, fake):
        got = client.get("/api/music/options").json()["data"]
        assert got["available"] is True
        assert got["limits"]["V4"]["custom"]["prompt"] == 3000
        assert got["install_dir"] == "game/assets/audio/music"

    def test_options_answers_without_a_key_too(self, client, monkeypatch):
        monkeypatch.delenv("KIE_API_KEY", raising=False)
        got = client.get("/api/music/options").json()["data"]
        assert got["available"] is False and got["reason"]


class TestGenerateEndpoint:
    def test_an_empty_prompt_is_refused(self, client, fake):
        r = client.post("/api/music/generate", json={"prompt": "  "})
        assert r.status_code == 400

    def test_an_unknown_field_is_refused_not_dropped(self, client, fake):
        r = client.post("/api/music/generate",
                        json={"prompt": "hum", "tempo": 120})
        assert r.status_code == 400 and "tempo" in r.json()["error"]["message"]

    def test_it_answers_a_job_id_by_default(self, client, fake):
        got = client.post("/api/music/generate", json={"prompt": "hum"}).json()
        assert got["data"]["job_id"] and got["data"]["poll"].startswith("/api/jobs/")

    def test_sync_mode_returns_the_batch(self, client, fake):
        got = client.post("/api/music/generate",
                          json={"prompt": "hum", "name": "hum",
                                "async": False}).json()["data"]
        assert got["ok"] and len(got["candidates"]) == 2

    def test_a_suno_refusal_comes_back_as_an_answer_not_a_traceback(self, client,
                                                                    fake):
        got = client.post("/api/music/generate",
                          json={"prompt": "x" * 600, "async": False}).json()["data"]
        assert got["ok"] is False and "500" in got["error"]


class TestKeepAndDiscardEndpoints:
    def _one(self, client, fake):
        client.post("/api/music/generate",
                    json={"prompt": "hum", "name": "hum", "async": False})
        return client.get("/api/music/candidates").json()["data"]["candidates"]

    def test_keep_installs_and_reports_where(self, client, fake, root):
        (root / "game" / "assets" / "audio").mkdir(parents=True, exist_ok=True)
        one = self._one(client, fake)[0]
        got = client.post("/api/music/keep",
                          json={"artifact_id": one["artifact_id"]}).json()["data"]
        assert got["install"]["path"].endswith("hum.mp3")
        assert (root / got["install"]["path"]).is_file()

    def test_discard_records_the_reason(self, client, fake):
        one = self._one(client, fake)[0]
        got = client.post("/api/music/discard",
                          json={"artifact_id": one["artifact_id"],
                                "note": "wrong era"}).json()["data"]
        assert got["artifact"]["status"] == "rejected"

    def test_a_missing_artifact_is_a_404(self, client, fake):
        assert client.post("/api/music/keep",
                           json={"artifact_id": 9999}).status_code == 404

    def test_a_non_integer_id_is_a_400(self, client, fake):
        assert client.post("/api/music/keep",
                           json={"artifact_id": "abc"}).status_code == 400

    def test_the_gallery_separates_pending_from_kept(self, client, fake, root):
        (root / "game" / "assets" / "audio").mkdir(parents=True, exist_ok=True)
        takes = self._one(client, fake)
        client.post("/api/music/keep", json={"artifact_id": takes[0]["artifact_id"]})
        got = client.get("/api/music/candidates").json()["data"]
        assert len(got["candidates"]) == 1 and len(got["kept"]) == 1
        assert got["kept"][0]["install"]["path"].endswith("hum.mp3")


class TestTaskEndpoint:
    def test_it_reports_a_finished_task(self, client, fake):
        got = client.get("/api/music/task/task-1").json()["data"]
        assert got["status"] == "SUCCESS" and got["track_count"] == 2


# ---------------------------------------------------------------------------
# One capability, two doors
# ---------------------------------------------------------------------------
class TestBothDoorsExist:
    """tests/mcp/test_brainstorm_mcp.py::TestParity holds the general rule; this
    names the tools, because a rename that keeps parity happy while losing the
    tool a seat actually calls is still a broken feature."""

    @pytest.mark.parametrize("name", [
        "music_options", "music_generate", "music_status",
        "music_candidates", "music_keep", "music_discard",
    ])
    def test_the_tool_is_registered(self, name):
        import asyncio

        from bgate_mcp import server

        tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert name in tools

    def test_the_music_tools_go_through_the_core_module(self):
        # Not through kie.generate_music directly: that writes files with no
        # provenance row, no ledger entry and nothing the seat can see.
        import inspect

        from bgate_mcp import server

        source = inspect.getsource(server.music_generate)
        assert "_music.generate" in source
        assert "kie.generate_music" not in source
