"""The cutscene pipeline's money, which is the only part of it that is expensive.

Everything here is about a number arriving somewhere it decides a spend, and the
four ways that used to go wrong. None of these tests reaches the provider — the
whole point of each is that it is decided BEFORE anything is bought:

  * an unknown price must stay unknown all the way to spend.check. A zero there
    is not "unpriced", it is "free", and it is the only value a budget gate
    reads as permission;
  * a shot left mid-generation may be a finished clip the account has already
    been billed for, sitting at the provider with nobody collecting it;
  * a generated clip must never land on a path that already holds one, because
    the file it would destroy was paid for and nothing errors when it goes;
  * the task id has to survive the process that started the generation, because
    it is the only handle on the money.
"""
from __future__ import annotations

import pytest

from bgate_adapters import kie
from bgate_core.cine import cinematic
from bgate_core.store import db
from bgate_core.board import spend

pytestmark = pytest.mark.usefixtures("root")


@pytest.fixture(autouse=True)
def _encoder(monkeypatch):
    """These tests are about money, not about ffmpeg. The encoder gate sits in
    front of the spend and would otherwise skip the suite on a build without
    libtheora — which is a real machine state and a different test's subject."""
    monkeypatch.setattr(cinematic, "ffmpeg_status",
                        lambda: {"ok": True, "ffmpeg": "ffmpeg", "theora": True,
                                 "probed": True, "reason": ""})


@pytest.fixture()
def clean_models():
    """MODELS is module-level and registration mutates it for the process."""
    before = dict(kie.MODELS)
    yield
    kie.MODELS.clear()
    kie.MODELS.update(before)
    kie._refresh_model_kinds()


def _shots(n=1, **over):
    return [{"action": f"beat {i}", "duration": 5, **over}
            for i in range(1, n + 1)]


def _stub_video(monkeypatch, **over):
    """The provider, replaced by something that writes a file and remembers."""
    calls = []

    def fake(prompt, out_path, **kw):
        calls.append({"prompt": prompt, "path": str(out_path), **kw})
        from pathlib import Path

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"not really an mp4")
        if kw.get("on_submit"):
            kw["on_submit"]("task-1")
        return {"ok": True, "model": "bytedance/seedance-2",
                "path": str(out_path), "task_id": "task-1",
                "url": "https://kie.test/v/1.mp4", "uploads": [],
                "credits_consumed": 120, **over}

    monkeypatch.setattr(kie, "generate_video", fake)
    return calls


def _spy_on_the_gate(monkeypatch):
    """Capture what reaches spend.check, which is where the number decides."""
    seen = {}

    def check(root, *, projected_usd=0.0):
        seen["projected_usd"] = projected_usd
        return {"allowed": True, "reason": "", "enforced": True}

    monkeypatch.setattr(spend, "check", check)
    return seen


class TestWhatReachesTheSpendGate:
    """_budget_refusal passed projected_usd=0.0 unconditionally, so the gate
    could only ever catch "this project is already over" — never "this one shot
    is expensive", which for a fifteen-second clip is the larger number."""

    def test_a_known_estimate_is_actually_projected(self, root, monkeypatch):
        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        seen = _spy_on_the_gate(monkeypatch)
        _stub_video(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))

        cinematic.generate_shot(root, "seq", 1)
        assert seen["projected_usd"] > 0

    def test_an_unknown_price_does_not_become_a_dollar_figure(
            self, root, monkeypatch):
        """The failure this whole thing guards: an invented number in front of a
        ceiling is worse than no number, because it is spent against."""
        monkeypatch.delenv(kie.USD_PER_CREDIT_ENV, raising=False)
        seen = _spy_on_the_gate(monkeypatch)
        _stub_video(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))

        cinematic.generate_shot(root, "seq", 1)
        assert seen["projected_usd"] == 0.0

    def test_a_refusal_says_the_estimate_was_unavailable_and_why(
            self, root, monkeypatch):
        """A gate that passes on an unknown price must not read as "free"."""
        monkeypatch.delenv(kie.USD_PER_CREDIT_ENV, raising=False)
        monkeypatch.setattr(spend, "check", lambda *a, **k: {
            "allowed": False, "reason": "daily budget reached"})
        cinematic.plan(root, "seq", _shots(1))

        out = cinematic.generate_shot(root, "seq", 1)
        assert out["ok"] is False and out["stage"] == "spend_gate"
        assert "could not be estimated" in out["error"]
        assert out["estimate"]["usd"] is None

    def test_a_refusal_names_the_figure_when_there_is_one(self, root,
                                                          monkeypatch):
        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        monkeypatch.setattr(spend, "check", lambda *a, **k: {
            "allowed": False, "reason": "daily budget reached"})
        cinematic.plan(root, "seq", _shots(1))

        out = cinematic.generate_shot(root, "seq", 1)
        assert "projected at about $" in out["error"]

    def test_a_real_ceiling_refuses_one_expensive_shot(self, root, monkeypatch):
        """End to end through the real ledger: the thing that was impossible
        before, because a shot was always projected at nothing."""
        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.05")
        spend.set_budget(root, per_project_usd=1.0, enforced=1)
        cinematic.plan(root, "seq", _shots(1, duration=15))

        out = cinematic.generate_shot(root, "seq", 1)
        assert out["ok"] is False and out["stage"] == "spend_gate"
        assert "project budget" in out["error"]


class TestTheSequenceEstimate:
    """A sequence is the most expensive thing this product buys in one sitting
    and it was bought one shot at a time, so the total only ever appeared on an
    invoice. plan() is free and is where a shot gets argued out of the list."""

    def test_a_plan_carries_what_the_list_will_cost(self, root, monkeypatch):
        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        seq = cinematic.plan(root, "seq", _shots(3))
        estimate = seq["estimate"]
        assert estimate["known"] is True
        assert estimate["shots"] == 3
        assert estimate["usd"] > 0

    def test_an_unpriced_shot_is_left_out_of_the_total_and_named(
            self, root, monkeypatch, clean_models):
        """A partial sum presented as a total is the same lie as a zero."""
        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        kie.register_video_model("unpriced", {
            "model": "vendor/unpriced-v1", "intent": {"seconds": "duration"}})
        seq = cinematic.plan(root, "seq", _shots(2), model="unpriced")

        estimate = seq["estimate"]
        assert estimate["known"] is False
        assert estimate["usd"] is None
        assert estimate["unknown_shots"] == [1, 2]
        assert "NOT in the total" in estimate["basis"]

    def test_the_total_is_never_zero_for_a_sequence_nobody_can_price(
            self, root, monkeypatch, clean_models):
        kie.register_video_model("unpriced", {
            "model": "vendor/unpriced-v1", "intent": {"seconds": "duration"}})
        got = cinematic.plan(root, "seq", _shots(2),
                             model="unpriced")["estimate"]
        assert got["usd"] is None and got["credits"] is None

    def test_it_says_out_loud_that_it_is_an_estimate(self, root):
        assert "ESTIMATE" in cinematic.plan(
            root, "seq", _shots(1))["estimate"]["note"]

    def test_a_cut_shot_is_not_billed_for(self, root, monkeypatch):
        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        cinematic.plan(root, "seq", _shots(2))
        shot = cinematic.sequence(root, "seq")["shots"][0]
        with db.tx(root) as conn:
            conn.execute("UPDATE cine_shot SET status = 'cut' WHERE id = ?",
                         (shot["id"],))
        assert cinematic.estimate_sequence(root, "seq")["shots"] == 1


class TestStuckAndPaidFor:
    """A generation is charged at SUBMIT and polled for minutes afterwards.
    Anything that kills the process in that window leaves the row at
    'generating' while the provider holds a finished, billed clip — and until
    this sweep existed, the only thing that noticed was a human remembering."""

    def _generating(self, root, *, task_id="task-1", age_s=3600):
        cinematic.plan(root, "seq", _shots(1))
        shot = cinematic.sequence(root, "seq")["shots"][0]
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE cine_shot SET status = 'generating', task_id = ?, "
                "updated_at = datetime('now', ?) WHERE id = ?",
                (task_id, f"-{age_s} seconds", shot["id"]))
        return shot

    def test_a_shot_still_inside_its_window_is_not_swept(self, root):
        """A video job runs five to fifteen minutes. Calling one of those stuck
        is how a human cancels work that is running fine."""
        self._generating(root, age_s=60)
        assert cinematic.stuck_shots(root, poll=False)["stale"] == 0

    def test_a_finished_clip_nobody_collected_is_found(self, root, monkeypatch):
        self._generating(root)
        monkeypatch.setattr(cinematic, "shot_status", lambda *a, **k: {
            "ok": True, "status": "success", "done": True, "running": False,
            "failed": False, "urls": ["https://kie.test/v/1.mp4"],
            "recoverable": True})

        got = cinematic.stuck_shots(root)
        assert got["recoverable"] == 1
        assert got["shots"][0]["state"] == "recoverable"
        assert got["shots"][0]["task_id"] == "task-1"
        assert "pays twice" in got["shots"][0]["note"]

    def test_a_job_still_running_is_not_reported_as_money_to_collect(
            self, root, monkeypatch):
        self._generating(root)
        monkeypatch.setattr(cinematic, "shot_status", lambda *a, **k: {
            "ok": True, "status": "generating", "done": False, "running": True,
            "failed": False, "urls": [], "recoverable": False})
        got = cinematic.stuck_shots(root)
        assert got["shots"][0]["state"] == "running"
        assert got["recoverable"] == 0

    def test_a_provider_that_cannot_be_asked_is_unknown_not_resolved(
            self, root, monkeypatch):
        self._generating(root)

        def boom(*a, **k):
            raise kie.KieError("could not reach kie")

        monkeypatch.setattr(cinematic, "shot_status", boom)
        got = cinematic.stuck_shots(root)
        assert got["shots"][0]["state"] == "unknown"
        assert got["recoverable"] == 0

    def test_a_generating_shot_with_no_task_id_is_its_own_category(self, root):
        """The worst one: the charge happened and the handle did not survive."""
        self._generating(root, task_id="")
        got = cinematic.stuck_shots(root, poll=False)
        assert got["shots"][0]["state"] == "lost"
        assert "no handle" in got["shots"][0]["note"]

    def test_the_cheap_half_asks_the_provider_nothing(self, root, monkeypatch):
        """It rides on every dashboard refresh, so it must cost no round trip."""
        self._generating(root)

        def never(*a, **k):
            raise AssertionError("poll=False must not reach the provider")

        monkeypatch.setattr(cinematic, "shot_status", never)
        got = cinematic.stuck_shots(root, poll=False)
        assert got["shots"][0]["state"] == "unpolled"
        assert got["polled"] is False

    def test_the_task_id_is_written_at_submit_not_at_return(self, root,
                                                            monkeypatch):
        """The id is the only handle on the money, and the charge happens at
        submit. A process that dies during the poll used to leave a row at
        'generating' with an empty task_id — a paid clip nothing can collect."""
        def dies_after_submitting(prompt, out_path, **kw):
            kw["on_submit"]("task-early")
            return {"ok": False, "error": "the connection died mid-poll"}

        monkeypatch.setattr(kie, "generate_video", dies_after_submitting)
        cinematic.plan(root, "seq", _shots(1))
        cinematic.generate_shot(root, "seq", 1)

        assert cinematic.sequence(root, "seq")["shots"][0]["task_id"] == \
            "task-early"


class TestNothingOverwritesSomethingPaidFor:
    """slugify("") returns the truthy "unnamed", so every unnamed shot in a
    sequence once shared a slug, a logical name and a candidate path — and shot
    2's generation silently overwrote the clip shot 1 had just been paid for.
    _unique_slug closes the way that collision was reached; these close the
    consequence, which holds however two paths come to be the same."""

    def test_a_candidate_already_on_disk_refuses_before_the_spend(
            self, root, monkeypatch):
        calls = _stub_video(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))
        doomed = (root / ".bgate_out" / "cinematic" / "seq" /
                  "seq_shot01_r1.mp4")
        doomed.parent.mkdir(parents=True, exist_ok=True)
        doomed.write_bytes(b"a clip somebody paid for")

        out = cinematic.generate_shot(root, "seq", 1)
        assert out["ok"] is False and out["stage"] == "collision"
        assert "already been paid for" in out["error"]
        assert not calls, "the provider must not be called"
        assert doomed.read_bytes() == b"a clip somebody paid for"

    def test_overwriting_takes_saying_so(self, root, monkeypatch):
        _stub_video(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))
        doomed = (root / ".bgate_out" / "cinematic" / "seq" /
                  "seq_shot01_r1.mp4")
        doomed.parent.mkdir(parents=True, exist_ok=True)
        doomed.write_bytes(b"scrap")

        assert cinematic.generate_shot(root, "seq", 1,
                                       overwrite=True)["ok"] is True

    def test_a_recovery_refuses_the_same_collision(self, root, monkeypatch):
        cinematic.plan(root, "seq", _shots(1))
        shot = cinematic.sequence(root, "seq")["shots"][0]
        with db.tx(root) as conn:
            conn.execute("UPDATE cine_shot SET task_id = 'task-1' WHERE id = ?",
                         (shot["id"],))
        monkeypatch.setattr(kie, "poll", lambda *a, **k: {"state": "success"})
        monkeypatch.setattr(kie, "result_urls",
                            lambda rec: ["https://kie.test/v/1.mp4"])
        landing = (root / ".bgate_out" / "cinematic" / "seq" /
                   "seq_shot01_r1_recovered.mp4")
        landing.parent.mkdir(parents=True, exist_ok=True)
        landing.write_bytes(b"an earlier recovery")

        def never(*a, **k):
            raise AssertionError("nothing may be downloaded over a paid clip")

        monkeypatch.setattr(kie, "download", never)
        with pytest.raises(cinematic.CinematicError, match="already been paid"):
            cinematic.recover_shot(root, "seq", 1)

    def test_every_shot_of_a_sequence_gets_its_own_path(self, root):
        """Including the case the index suffix alone does not cover: two shots
        called "x" plus one already called "x-03"."""
        seq = cinematic.plan(root, "seq", [
            {"action": "a", "slug": "x-03"}, {"action": "b", "slug": "x"},
            {"action": "c", "slug": "x"}])
        slugs = [s["slug"] for s in seq["shots"]]
        assert len(set(slugs)) == 3, slugs

    def test_two_shots_cannot_share_an_index(self, root):
        """The database constraint that IS doing work here — slug uniqueness has
        no index behind it, (sequence_id, idx) does."""
        import sqlite3

        cinematic.plan(root, "seq", _shots(2))
        seq = cinematic.sequence(root, "seq")
        with pytest.raises(sqlite3.IntegrityError):
            with db.tx(root) as conn:
                conn.execute(
                    "INSERT INTO cine_shot (sequence_id, idx, slug, action) "
                    "VALUES (?, 1, 'clash', 'a duplicate beat')",
                    (seq["id"],))


class TestMutuallyExclusiveIntent:
    """Some settings are legal alone and refused together.

    Seedance takes an anchor frame OR reference images, never both, and says so
    only in a 422: "The reference image and the first and last frames are
    mutually exclusive". Field-by-field validation cannot see that — every value
    is individually fine — so a storyboard-promoted shot carrying a still AND a
    pinned cast built a payload that was refused as a whole, AFTER both anchors
    had been uploaded to the provider.
    """

    def test_the_anchor_frame_wins_and_the_refs_are_dropped(self):
        intent, dropped, refusal = cinematic._fit_intent("seedance-2", {
            "seconds": 5, "first_frame": "a.png", "refs": ["x.png", "y.png"]})
        assert not refusal
        assert intent["first_frame"] == "a.png"
        assert "refs" not in intent
        # Reported, never silent: losing the cast references changes the clip.
        assert "refs" in dropped

    def test_references_alone_are_left_alone(self):
        """The rule is a conflict resolver, not a ban on references."""
        intent, dropped, _ = cinematic._fit_intent(
            "seedance-2", {"seconds": 5, "refs": ["x.png"]})
        assert intent["refs"] == ["x.png"]
        assert "refs" not in dropped

    def test_a_model_declaring_no_exclusivity_keeps_both(self):
        from bgate_adapters import kie

        assert not (kie.MODELS["qwen-edit"].get("exclusive") or ())
