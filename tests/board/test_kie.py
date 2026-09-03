"""kie.ai adapter — request shapes, poll semantics, error shapes, spend.

NOT ONE TEST HERE NEEDS A KEY OR TOUCHES THE NETWORK, and that is the point
rather than a convenience. The things most worth checking about this adapter are
the ones that would otherwise be discovered by paying for a wrong request: a
model id that is not what kie's reference says, an aspect ratio that produces a
422, a poll loop that stops at Suno's FIRST_SUCCESS and hands back a third of
what was billed. Every one of those is decided before a byte leaves the process,
so every one of them is testable with the socket unplugged.

`_request` is the only seam that would reach the network, and it is stubbed. Two
tests assert the KEY IS ABSENT from the assertion surface for the same reason
the adapter never logs it — a test that prints a payload is a place a real key
can end up in CI output.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from bgate_adapters import kie


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    """No credentials, ever, unless a test asks for a fake one."""
    monkeypatch.delenv("KIE_API_KEY", raising=False)
    monkeypatch.delenv(kie.USD_PER_CREDIT_ENV, raising=False)


@pytest.fixture()
def keyed(monkeypatch):
    monkeypatch.setenv("KIE_API_KEY", "kie-test-not-a-real-key")


class TestAvailable:
    def test_no_key_reports_the_variable_the_url_and_where_it_goes(self):
        got = kie.available()
        assert got["available"] is False
        # The UI prints this string verbatim, so it has to be actionable on its
        # own: which variable, which file, and where a key comes from.
        assert "KIE_API_KEY" in got["reason"]
        assert ".env" in got["reason"]
        assert kie.KEY_URL in got["reason"]

    def test_a_key_reports_all_three_capabilities_and_no_price(self, keyed):
        got = kie.available()
        assert got["available"] is True
        assert "nano-banana" in got["image_models"]
        assert "seedance-2" in got["video_models"]
        assert "V5" in got["music_models"]
        # Unknown, never zero — a budget gate reads 0.0 as free.
        assert got["usd_per_credit"] is None
        assert "credits" in got["price_note"]

    def test_availability_never_returns_the_key(self, keyed):
        assert "not-a-real-key" not in json.dumps(kie.available())

    def test_the_doctor_row_carries_the_reason_when_absent(self):
        row = kie.doctor_row()
        assert row["name"] == "kie" and row["optional"] is True
        assert row["available"] is False and "KIE_API_KEY" in row["detail"]


class TestRequestConstruction:
    """What goes on the wire matches docs.kie.ai, field for field."""

    def test_the_model_ids_are_the_documented_strings(self):
        assert kie.MODELS["nano-banana"]["model"] == "google/nano-banana"
        assert kie.MODELS["flux-2-pro-edit"]["model"] == "flux-2/pro-image-to-image"
        assert kie.MODELS["qwen-edit"]["model"] == "qwen/image-to-image"
        assert kie.MODELS["seedance-2"]["model"] == "bytedance/seedance-2"

    def test_submit_posts_createtask_with_model_and_input_nested(
            self, keyed, monkeypatch):
        seen = {}

        def fake(path, key, *, payload=None, params=None, method="GET",
                 timeout=60.0):
            seen.update(path=path, payload=payload, method=method)
            return {"taskId": "task_google_1"}

        monkeypatch.setattr(kie, "_request", fake)
        got = kie.submit("nano-banana", prompt="a lantern")

        assert seen["method"] == "POST"
        assert seen["path"] == "/api/v1/jobs/createTask"
        # The two-level shape is the whole contract: a flat body is a 422.
        assert seen["payload"] == {"model": "google/nano-banana",
                                   "input": {"prompt": "a lantern"}}
        assert got == {"task_id": "task_google_1", "model": "nano-banana",
                       "kind": "image", "kie_model": "google/nano-banana"}

    def test_a_callback_url_rides_at_the_top_level_not_inside_input(
            self, keyed, monkeypatch):
        seen = {}
        monkeypatch.setattr(kie, "_request",
                            lambda p, k, **kw: (seen.update(kw), {"taskId": "t"})[1])
        kie.submit("nano-banana", prompt="x", callback="https://example.test/cb")
        assert seen["payload"]["callBackUrl"] == "https://example.test/cb"
        assert "callBackUrl" not in seen["payload"]["input"]

    def test_an_unsupported_field_is_refused_not_dropped(self):
        # Dropping it would bill for a generation that ignored the setting.
        with pytest.raises(kie.KieError) as exc:
            kie.build_input("nano-banana", seed=7)
        assert "does not take seed" in str(exc.value)

    def test_a_missing_required_field_is_named(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_input("qwen-edit", prompt="x")
        assert "image_url" in str(exc.value)

    def test_an_enum_outside_the_documented_set_lists_the_set(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_input("seedance-2", prompt="a chase", resolution="8k")
        assert "480p" in str(exc.value) and "4k" in str(exc.value)

    def test_a_numeric_range_is_enforced_from_the_reference(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_input("seedance-2", prompt="a chase", duration=30)
        assert "4..15" in str(exc.value)
        assert kie.build_input("seedance-2", prompt="a chase",
                               duration=15)["duration"] == 15

    def test_a_string_range_is_measured_as_a_length(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_input("seedance-2", prompt="ab")   # min is 3 characters
        assert "characters" in str(exc.value)

    def test_an_array_cap_is_enforced(self):
        urls = [f"https://example.test/{i}.png" for i in range(10)]
        with pytest.raises(kie.KieError) as exc:
            kie.build_input("seedance-2", prompt="a chase",
                            reference_image_urls=urls)
        assert "at most 9" in str(exc.value)

    def test_a_local_path_in_a_url_field_is_refused_with_the_reason(self):
        # The failure this prevents: encoding a data URI on a guess, paying for
        # the 422, and believing anchored generation works. build_input still
        # refuses, because validation must not perform network calls — but the
        # refusal now names the ROUTE (upload_file) instead of saying anchored
        # generation through kie does not exist, which stopped being true when
        # the file-upload API was wired.
        with pytest.raises(kie.KieError) as exc:
            kie.build_input("qwen-edit", prompt="x", image_url="C:/art/hero.png")
        assert "must be a URL" in str(exc.value)
        assert "upload_file" in str(exc.value)

    def test_a_hosted_url_passes(self):
        got = kie.build_input("qwen-edit", prompt="x",
                              image_url="https://example.test/hero.png")
        assert got["image_url"] == "https://example.test/hero.png"

    def test_wxh_becomes_the_nearest_aspect_this_model_accepts(self):
        wide = kie.MODELS["nano-banana"]["enums"]["aspect_ratio"]
        assert kie.aspect_for("1920x1080", wide) == "16:9"
        assert kie.aspect_for("1024x1024", wide) == "1:1"
        # flux-2 has no 5:4, so 1280x1024 must not resolve to one.
        flux = kie.MODELS["flux-2-pro-edit"]["enums"]["aspect_ratio"]
        assert kie.aspect_for("1280x1024", flux) in flux

    def test_shape_is_checked_before_the_key_is(self):
        # With no key set, a bad enum must still report the ENUM. Checking the
        # key first would answer every schema mistake with "KIE_API_KEY not set".
        with pytest.raises(kie.KieError) as exc:
            kie.submit("seedance-2", prompt="a chase", resolution="8k")
        assert "480p" in str(exc.value)

    def test_a_good_shape_with_no_key_reports_the_key(self):
        with pytest.raises(kie.KieError) as exc:
            kie.submit("nano-banana", prompt="a lantern")
        assert "KIE_API_KEY" in str(exc.value)


class TestErrorShapes:
    """kie answers HTTP 200 and puts the verdict in the body."""

    def test_a_business_code_in_a_200_body_still_raises(self):
        with pytest.raises(kie.KieError) as exc:
            kie._envelope({"code": 402, "msg": "Insufficient Credits",
                           "data": None}, what="POST /x")
        text = str(exc.value)
        assert "402" in text and "Insufficient Credits" in text
        # The advice, not just the number — 402 is the one nobody guesses.
        assert "top the account up" in text

    def test_code_200_unwraps_to_data(self):
        assert kie._envelope({"code": 200, "msg": "success",
                              "data": {"taskId": "t1"}},
                             what="x") == {"taskId": "t1"}

    def test_an_unknown_code_still_names_itself(self):
        with pytest.raises(kie.KieError) as exc:
            kie._envelope({"code": 599, "msg": "who knows"}, what="GET /y")
        assert "599" in str(exc.value)

    def test_every_documented_code_carries_advice(self):
        for code in (401, 402, 422, 429, 501):
            assert kie._CODE_HELP[code]


class TestPolling:
    def _records(self, monkeypatch, states):
        queue = list(states)
        monkeypatch.setattr(kie, "record",
                            lambda task_id, **kw: queue.pop(0))
        monkeypatch.setattr(kie.time, "sleep", lambda *_: None)

    def test_it_waits_through_the_running_states_then_returns_success(
            self, keyed, monkeypatch):
        self._records(monkeypatch, [
            {"state": "waiting"}, {"state": "queuing"}, {"state": "generating"},
            {"state": "success", "taskId": "t", "creditsConsumed": 30},
        ])
        got = kie.poll("t", timeout=30.0, interval=0.0)
        assert got["state"] == "success" and got["creditsConsumed"] == 30

    def test_fail_raises_with_failmsg_not_a_silent_empty_result(
            self, keyed, monkeypatch):
        self._records(monkeypatch, [
            {"state": "fail", "failCode": "501", "failMsg": "content blocked"}])
        with pytest.raises(kie.KieError) as exc:
            kie.poll("t", timeout=30.0, interval=0.0)
        assert "content blocked" in str(exc.value)

    def test_an_unknown_state_stops_rather_than_spinning(self, keyed, monkeypatch):
        self._records(monkeypatch, [{"state": "thinking"}])
        with pytest.raises(kie.KieError) as exc:
            kie.poll("t", timeout=30.0, interval=0.0)
        assert "unknown state" in str(exc.value)

    def test_it_gives_up_bounded_rather_than_holding_a_seat_forever(
            self, keyed, monkeypatch):
        monkeypatch.setattr(kie, "record", lambda task_id, **kw: {"state": "queuing"})
        monkeypatch.setattr(kie.time, "sleep", lambda *_: None)
        # deadline, one pass through the loop, then past it.
        clock = iter([0.0, 1.0] + [1000.0] * 20)
        monkeypatch.setattr(kie.time, "monotonic", lambda: next(clock))
        with pytest.raises(kie.KieError) as exc:
            kie.poll("t", timeout=5.0, interval=0.0)
        assert "did not finish" in str(exc.value) and "queuing" in str(exc.value)


class TestResultParsing:
    """resultJson is a STRING. Reading it as an object is a TypeError."""

    def test_result_urls_json_loads_the_string(self):
        rec = {"resultJson": '{"resultUrls":["https://example.test/a.jpg"]}'}
        assert kie.result_urls(rec) == ["https://example.test/a.jpg"]

    def test_an_already_parsed_object_also_works(self):
        assert kie.result_urls(
            {"resultJson": {"resultUrls": ["https://example.test/b.png"]}}
        ) == ["https://example.test/b.png"]

    def test_a_resultobject_of_urls_is_found(self):
        rec = {"resultJson":
               '{"resultObject":{"mask_urls":["https://example.test/m1"]}}'}
        assert kie.result_urls(rec) == ["https://example.test/m1"]

    def test_a_resultobject_with_no_files_returns_empty_not_a_guess(self):
        assert kie.result_urls(
            {"resultJson": '{"resultObject":{"subject_status":1}}'}) == []

    def test_missing_or_broken_resultjson_returns_empty(self):
        assert kie.result_urls({}) == []
        assert kie.result_urls({"resultJson": ""}) == []
        assert kie.result_urls({"resultJson": "not json"}) == []


class TestSuno:
    """A different API under the same key — different everything else."""

    def test_it_posts_to_the_suno_endpoint_not_createtask(self, keyed, monkeypatch):
        seen = {}
        monkeypatch.setattr(kie, "_request",
                            lambda p, k, **kw: (seen.update(path=p, **kw),
                                                {"taskId": "5c79"})[1])
        kie.submit_music("a slow marimba loop")
        assert seen["path"] == "/api/v1/generate"
        assert seen["payload"]["model"] == kie.DEFAULT_SUNO_MODEL

    def test_the_body_is_flat_with_the_five_required_fields(self):
        body = kie.build_music("a slow marimba loop")
        # callBackUrl IS ONE OF THEM, whatever the quickstart's example shows.
        # Measured against the live API: without it, POST /api/v1/generate
        # answers HTTP 200 with {"code":422,"msg":"Please enter callBackUrl."}
        # and nothing generates. It is always sent now — see SUNO_CALLBACK_NOTE.
        assert set(body) == {"prompt", "customMode", "instrumental", "model",
                             "callBackUrl"}
        assert body["callBackUrl"]
        # Instrumental by default: a vocalist over the dialogue is the wrong
        # asset almost every time.
        assert body["instrumental"] is True and body["customMode"] is False

    def test_the_callback_url_is_env_overridable_with_a_loopback_default(
            self, monkeypatch):
        monkeypatch.delenv(kie.SUNO_CALLBACK_ENV, raising=False)
        assert kie.callback_url().startswith("http://127.0.0.1")
        monkeypatch.setenv(kie.SUNO_CALLBACK_ENV, "https://relay.example/cb")
        assert kie.build_music("hum")["callBackUrl"] == "https://relay.example/cb"
        # An explicit argument still wins over the environment.
        assert kie.callback_url("https://other.example/x") == "https://other.example/x"

    def test_simple_mode_caps_the_prompt_at_500(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_music("x" * 501)
        assert "500" in str(exc.value) and "custom=True" in str(exc.value)

    def test_custom_mode_limits_move_with_the_model(self):
        # V4 allows 3,000; everything newer allows 5,000.
        kie.build_music("x" * 4000, custom=True, model="V5")
        with pytest.raises(kie.KieError):
            kie.build_music("x" * 4000, custom=True, model="V4")

    def test_style_and_title_are_refused_in_simple_mode_not_dropped(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_music("a loop", style="chiptune")
        assert "custom mode" in str(exc.value)

    def test_duration_is_v5_5_only(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_music("a loop", model="V5", duration=60)
        assert "V5_5" in str(exc.value)
        assert kie.build_music("a loop", model="V5_5",
                               duration=60)["duration"] == 60

    def test_an_unknown_model_lists_the_known_ones(self):
        with pytest.raises(kie.KieError) as exc:
            kie.build_music("a loop", model="V9")
        assert "V5_5" in str(exc.value)

    def test_weights_are_clamped_and_rounded(self):
        body = kie.build_music("a loop", styleWeight=1.7, weirdnessConstraint=0.456)
        assert body["styleWeight"] == 1.0
        assert body["weirdnessConstraint"] == 0.46

    def test_first_success_is_progress_not_completion(self, keyed, monkeypatch):
        # Stopping here returns a fraction of a request billed in full.
        queue = [{"status": "PENDING"}, {"status": "TEXT_SUCCESS"},
                 {"status": "FIRST_SUCCESS"},
                 {"status": "SUCCESS", "response": {"sunoData": [
                     {"id": "a", "audioUrl": "https://example.test/a.mp3"},
                     {"id": "b", "audioUrl": "https://example.test/b.mp3"}]}}]
        monkeypatch.setattr(kie, "music_record", lambda t, **kw: queue.pop(0))
        monkeypatch.setattr(kie.time, "sleep", lambda *_: None)
        done = kie.poll_music("5c79", timeout=30.0, interval=0.0)
        assert len(kie.music_tracks(done)) == 2

    def test_a_content_filter_rejection_says_what_to_do(self, keyed, monkeypatch):
        monkeypatch.setattr(kie, "music_record",
                            lambda t, **kw: {"status": "SENSITIVE_WORD_ERROR"})
        monkeypatch.setattr(kie.time, "sleep", lambda *_: None)
        with pytest.raises(kie.KieError) as exc:
            kie.poll_music("5c79", timeout=30.0, interval=0.0)
        assert "reword" in str(exc.value)

    def test_tracks_come_from_nested_json_not_a_string(self):
        # The market API's string-encoded resultJson has no counterpart here.
        rec = {"response": {"sunoData": [
            {"id": "e231", "audioUrl": "https://example.test/x.mp3",
             "title": "Peaceful Piano", "duration": 198.44}]}}
        got = kie.music_tracks(rec)
        # model_name and tags are normalised too, and absent in this record, so
        # they come back empty rather than missing: a caller that reads
        # track["tags"] should not have to know which fields Suno happened to
        # send for one take.
        assert got == [{"id": "e231", "audio_url": "https://example.test/x.mp3",
                        "title": "Peaceful Piano", "duration": 198.44,
                        "model_name": "", "tags": "", "image_url": ""}]

    def test_a_record_with_no_tracks_is_empty_not_a_crash(self):
        assert kie.music_tracks({}) == []
        assert kie.music_tracks({"response": {"sunoData": None}}) == []


class TestSpend:
    def test_no_rate_configured_means_no_price_never_zero(self):
        assert kie.usd_per_credit() is None
        assert kie.cost_usd(30) is None
        assert kie.price_for("nano-banana") is None

    def test_a_declared_rate_costs_the_credits_the_job_actually_used(
            self, monkeypatch):
        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        assert kie.usd_per_credit() == 0.005
        assert kie.cost_usd(30) == 0.15

    def test_a_junk_or_negative_rate_falls_back_to_unknown(self, monkeypatch):
        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "free")
        assert kie.usd_per_credit() is None
        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "-1")
        assert kie.usd_per_credit() is None

    def test_finish_carries_the_credits_and_the_derived_dollars(self, monkeypatch):
        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        got = kie._finish({"taskId": "t", "creditsConsumed": 40},
                          model="nano-banana", kind="image")
        assert got["credits_consumed"] == 40 and got["usd"] == 0.2

    def test_an_unpriced_success_says_so_instead_of_writing_a_dollar(self, root):
        result = kie._account({"ok": True, "usd": None,
                               "credits_consumed": 30},
                              root, kind="image")
        # No dollar figure is invented; the result states the gap and keeps the
        # credit count, and a zero-dollar MARKER row lands so totals() can say
        # how much real spend is missing a price.
        assert result["accounted"] is False
        assert kie.USD_PER_CREDIT_ENV in result["cost_note"]
        assert result["credits_consumed"] == 30
        assert result["unpriced_recorded"] is True

    def test_unpriced_rows_are_surfaced_in_totals_not_dropped(self, root):
        # THE REPORT IS THE PRODUCT (budgets are off by default), so a kie-heavy
        # project must not read as cheap just because kie bills in credits.
        from bgate_core.board import spend

        kie._account({"ok": True, "usd": None, "credits_consumed": 30,
                      "model": "seedance-2"},
                     root, kind="video", logical_name="chase")
        kie._account({"ok": True, "usd": None, "credits_consumed": 12},
                     root, kind="image", logical_name="hero")
        totals = spend.totals(root)
        # No invented rate: the dollar totals stay honest at zero...
        assert totals["project_usd"] == 0 and totals["today_usd"] == 0
        # ...and the gap is named instead of silent.
        un = totals["unaccounted"]
        assert un["rows"] == 2 and un["today_rows"] == 2
        assert un["credits"] == pytest.approx(42)
        assert un["credits_unknown_rows"] == 0
        assert "BGATE_KIE_USD_PER_CREDIT" in un["note"]

    def test_unknown_credits_still_count_as_an_unaccounted_row(self, root):
        # Suno's balance-delta path can fail to measure even the credits; the
        # ROW still counts — a charge with no number at all is still a charge.
        from bgate_core.board import spend

        kie._account({"ok": True, "usd": None,
                      "credits_consumed": None},
                     root, kind="audio", logical_name="loop")
        un = spend.totals(root)["unaccounted"]
        assert un["rows"] == 1 and un["credits"] == 0
        assert un["credits_unknown_rows"] == 1

    def test_a_priced_success_writes_no_unaccounted_row(self, root, monkeypatch):
        from bgate_core.board import spend

        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        kie._account({"ok": True, "usd": kie.cost_usd(200),
                      "credits_consumed": 200},
                     root, kind="video", logical_name="chase")
        un = spend.totals(root)["unaccounted"]
        assert un["rows"] == 0 and un["note"] == ""

    def test_an_unpriced_failure_writes_no_row(self, root):
        # A failed call may not have been charged; claiming spend for it would
        # be inventing in the other direction.
        from bgate_core.board import spend

        kie._account({"ok": False, "usd": None,
                      "credits_consumed": 30}, root, kind="image")
        assert spend.totals(root)["unaccounted"]["rows"] == 0

    def test_a_priced_success_lands_in_the_ledger_under_its_own_kind(
            self, root, monkeypatch):
        from bgate_core.board import spend

        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        kie._account({"ok": True, "usd": kie.cost_usd(200),
                      "model": "seedance-2"},
                     root, kind="video", logical_name="chase")
        totals = spend.totals(root)
        # "video" is its own bucket: kie prices a clip at 100-500 credits
        # against an image's 10-50, so it must not sum into "other".
        assert totals["by_kind"]["video"] == pytest.approx(1.0)
        assert spend.for_logical(root, "chase") == pytest.approx(1.0)

    def test_a_failure_is_never_accounted(self, root):
        result = kie._account({"ok": False, "usd": 5.0}, root,
                              kind="image")
        assert result["accounted"] is False

    def test_video_is_a_known_spend_kind(self):
        from bgate_core.board import spend
        assert "video" in spend.KINDS


class TestWiring:
    def test_the_provider_registry_carries_kie_and_what_it_powers(self):
        from bgate_core.runtime import providers

        one = providers.by_id("kie")
        assert one.env == "KIE_API_KEY"
        # The three capabilities the user asked for, and NOT model_3d.
        assert set(one.powers) == {"image_2d", "audio", "video"}
        assert "model_3d" not in one.powers
        assert one.key_url.startswith("https://")

    def test_every_power_is_a_declared_capability(self):
        from bgate_core.runtime import providers
        for one in providers.PROVIDERS:
            assert set(one.powers) <= set(providers.CAPABILITIES)

    def test_the_doctor_row_goes_green_on_a_kie_only_project(self, monkeypatch):
        # The bug this repeats otherwise: `MISS openai_key` and a non-zero exit
        # for a setup that is completely fine.
        from bgate_core.runtime import doctor

        for one in ("OPENAI_API_KEY", "KREA_API_KEY"):
            monkeypatch.delenv(one, raising=False)
        monkeypatch.setenv("KIE_API_KEY", "kie-test-not-a-real-key")
        row = doctor._probe_art_key()
        assert row["available"] is True and "KIE_API_KEY" in row["path"]

    def test_kie_is_a_workflow_provider(self):
        from bgate_core.board import generate
        assert "kie" in generate.PROVIDERS

    def test_an_unpriced_kie_node_refuses_rather_than_planning_at_zero(self):
        from bgate_core.board import generate

        with pytest.raises(generate.GenerateRefused) as exc:
            generate.plan({"provider": "kie", "model": "nano-banana"})
        assert kie.USD_PER_CREDIT_ENV in str(exc.value)

    def test_a_rated_kie_node_plans_with_a_real_ceiling(self, monkeypatch):
        from bgate_core.board import generate

        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        spec = generate.plan({"provider": "kie", "model": "nano-banana"})
        assert spec["provider"] == "kie" and spec["unit_usd"] > 0

    def test_an_unknown_kie_model_is_refused_at_plan_time(self, monkeypatch):
        from bgate_core.board import generate

        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        with pytest.raises(generate.GenerateRefused) as exc:
            generate.plan({"provider": "kie", "model": "seedance-2"})
        # A video model is not an image model, however real its id is.
        assert "no image model" in str(exc.value)

    def test_chroma_uploads_anchors_for_kie_rather_than_refusing_them(
            self, root, monkeypatch):
        """kie's image fields are URLs and a pinned ref is a local file, and
        this used to be refused outright on exactly that basis. upload_file has
        always bridged that gap — it is what the video path uses for
        first_frame — so the refusal left a project whose only funded account is
        kie unable to draw an anchored frame at all."""
        from bgate_core.art import chroma

        anchor = root / "anchor.png"
        anchor.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
        seen = {}

        monkeypatch.setattr(kie, "upload_file",
                            lambda p, **kw: {"url": "https://kie.test/a.png"})

        def _gen(prompt, out_path, **kw):
            seen.update(model=kw.get("model"), urls=kw.get("image_urls"))
            pathlib.Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
            return {"ok": True, "model": kw.get("model"), "usd": 0.0}

        monkeypatch.setattr(kie, "generate_image", _gen)

        got = chroma.generate("a hero", str(root / "out.png"), provider="kie",
                              ref_paths=[str(anchor)], root=root)
        assert got["ok"] is True, got
        assert seen["urls"] == ["https://kie.test/a.png"]
        # The default image model declares no references, so asking for anchored
        # work on it would refuse every time and look like kie not supporting
        # anchors at all. An unnamed model is upgraded to one that can hold them.
        assert kie.image_ref_cap(seen["model"]) >= 1

    def test_an_explicit_kie_model_that_takes_no_refs_is_still_refused(
            self, root, monkeypatch):
        """Overriding a model the caller NAMED would be buying something other
        than what they asked for. The refusal still lands before any spend."""
        from bgate_core.art import chroma

        anchor = root / "anchor.png"
        anchor.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
        monkeypatch.setattr(kie, "generate_image", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not reach the provider")))

        got = chroma.generate("a hero", str(root / "out.png"), provider="kie",
                              model="nano-banana",
                              ref_paths=[str(anchor)], root=root)
        assert got["ok"] is False
        assert "takes no reference images" in got["error"]

    def test_a_failed_anchor_upload_refuses_rather_than_buying_unanchored(
            self, root, monkeypatch):
        """The original refusal existed to stop a paid frame that looks nothing
        like the character. That reasoning still holds at the upload."""
        from bgate_core.art import chroma

        anchor = root / "anchor.png"
        anchor.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
        monkeypatch.setattr(kie, "upload_file", lambda p, **kw: (_ for _ in ()).throw(
            kie.KieError("upload rejected")))
        monkeypatch.setattr(kie, "generate_image", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not reach the provider")))

        got = chroma.generate("a hero", str(root / "out.png"), provider="kie",
                              ref_paths=[str(anchor)], root=root)
        assert got["ok"] is False
        assert "could not upload the anchor" in got["error"]

    def test_the_mcp_server_exposes_the_two_new_capabilities(self):
        from bgate_mcp import server

        for name in ("kie_status", "music_generate", "kie_video_generate"):
            assert hasattr(server, name), f"{name} is not an MCP tool"


class TestTheUploadSurface:
    """The call that turned "anchored generation through kie is unavailable"
    from a fact into a missing four lines.

    It matters far more for video than for images: an anchored still has Krea to
    fall back on, and an anchored SHOT has nothing — no other provider wired here
    generates a frame of video at all.
    """

    def _fake(self, seen):
        def _request(path, key, *, payload=None, params=None,
                     method="GET", timeout=60.0):
            seen.update(path=path, payload=payload, method=method)
            return {"fileName": "hero.png", "fileSize": 7,
                    "mimeType": "image/png",
                    "downloadUrl": "https://kie.test/f/hero.png"}
        return _request

    def _png(self, tmp_path):
        p = tmp_path / "hero.png"
        p.write_bytes(b"\x89PNG123")
        return p

    def test_it_posts_raw_base64_to_the_file_host(self, tmp_path, monkeypatch):
        """NOT api.kie.ai, and NOT a data URI. A `data:` prefix would be decoded
        a second time by the server and land as a corrupt image that still
        uploads with a 200."""
        import base64

        seen = {}
        monkeypatch.setattr(kie, "_request", self._fake(seen))
        monkeypatch.setattr(kie, "api_key", lambda root=None: "k")

        got = kie.upload_file(self._png(tmp_path))
        assert seen["path"] == kie.UPLOAD_BASE64
        assert seen["method"] == "POST"
        assert not seen["payload"]["base64Data"].startswith("data:")
        assert base64.b64decode(seen["payload"]["base64Data"]) == b"\x89PNG123"
        assert got["url"] == "https://kie.test/f/hero.png"

    def test_the_minted_url_is_stamped_with_the_day_it_dies(self, tmp_path,
                                                            monkeypatch):
        """Three days, not fourteen. Nothing may cache one of these, so a caller
        that stores it has to be able to tell it is dead without calling it."""
        monkeypatch.setattr(kie, "_request", self._fake({}))
        monkeypatch.setattr(kie, "api_key", lambda root=None: "k")

        got = kie.upload_file(self._png(tmp_path))
        assert got["ttl_days"] == kie.UPLOAD_TTL_DAYS == 3
        assert len(got["expires_at"]) == 10   # ISO date

    def test_a_type_no_model_accepts_is_refused_before_the_upload(self,
                                                                  tmp_path,
                                                                  monkeypatch):
        """A file kie's uploader takes but no generation model accepts would
        fail at generation instead, after the upload had already succeeded."""
        monkeypatch.setattr(kie, "api_key", lambda root=None: "k")
        bad = tmp_path / "hero.bmp"
        bad.write_bytes(b"BM")
        with pytest.raises(kie.KieError, match="not one of"):
            kie.upload_file(bad)

    def test_an_upload_with_no_url_back_is_not_reported_as_success(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(kie, "_request",
                            lambda *a, **k: {"fileName": "hero.png"})
        monkeypatch.setattr(kie, "api_key", lambda root=None: "k")
        with pytest.raises(kie.KieError, match="no downloadUrl"):
            kie.upload_file(self._png(tmp_path))

    def test_a_local_frame_is_uploaded_and_a_url_is_passed_through(
            self, tmp_path, monkeypatch):
        """The whole point: a pinned anchor on disk reaches a video model. An
        already-hosted URL is NOT re-uploaded — callers reuse one frame across
        the shots of a sequence, and minting duplicates would be pure waste."""
        monkeypatch.setattr(kie, "_request", self._fake({}))
        monkeypatch.setattr(kie, "api_key", lambda root=None: "k")

        uploads = []
        assert kie._as_url(str(self._png(tmp_path)), None,
                           uploads) == "https://kie.test/f/hero.png"
        assert kie._as_url("https://x.test/a.png", None,
                           uploads) == "https://x.test/a.png"
        assert kie._as_url("", None, uploads) == ""
        assert len(uploads) == 1

    def test_build_input_still_refuses_a_local_path(self, tmp_path):
        """Validation must not perform network calls — an upload hidden inside a
        shape check is a validation that costs a round trip and leaks a file. So
        build_input refuses and names the route; generate_video is what resolves
        anchors before submitting."""
        with pytest.raises(kie.KieError) as exc:
            kie.build_input("seedance-2", prompt="a long enough prompt",
                            first_frame_url="C:/art/hero.png")
        assert "upload_file" in str(exc.value)


class TestNothingIsWrittenOverSomethingPaidFor:
    def test_a_second_batch_does_not_land_on_the_first_one(self, tmp_path,
                                                           monkeypatch):
        """Takes are named by their INDEX in the batch, so a second generation
        under the same logical name used to write `<stem>_1.mp3` again — the
        first batch's bytes gone, and the revisions registered against them now
        describing audio nobody generated."""
        monkeypatch.setattr(kie, "download",
                            lambda url, path, **kw: pathlib.Path(path).write_bytes(b"x"))
        tracks = [{"audio_url": "https://kie.test/a.mp3"}]
        first = kie.download_tracks(tracks, tmp_path, stem="theme")
        second = kie.download_tracks(tracks, tmp_path, stem="theme")
        assert first[0]["path"] != second[0]["path"]
        assert pathlib.Path(first[0]["path"]).is_file()


@pytest.fixture()
def clean_models(monkeypatch):
    """Registration mutates module-level MODELS, so put it back afterwards.

    A leftover entry is not a leaked fixture — it is a video model the next test
    can plan a sequence against.
    """
    monkeypatch.delenv(kie.VIDEO_CREDITS_ENV, raising=False)
    before = dict(kie.MODELS)
    yield
    kie.MODELS.clear()
    kie.MODELS.update(before)
    kie._refresh_model_kinds()


class TestTheForwardEstimate:
    """The number a budget gate needs, which is the one kie does not publish.

    Everything else here measures cost after the fact — creditsConsumed arrives
    on the finished record, which is the truth and is useless to a gate that
    runs before the spend. So these are mostly about what happens when there is
    NO number: it stays None, it says so, and it never reaches spend.check
    wearing a zero.
    """

    def test_an_unknown_model_is_priced_at_nothing_rather_than_at_zero(self):
        got = kie.estimate_usd("veo-9", 5)
        assert got["credits"] is None and got["usd"] is None
        assert got["known"] is False and got["credits_known"] is False
        assert "UNKNOWN" in got["basis"]

    def test_a_registered_model_with_no_rate_is_unknown_not_free(
            self, clean_models):
        """A model somebody added at runtime has no published price, and a zero
        here reads as permission to a spend ceiling."""
        kie.register_video_model("kling-like", {
            "model": "kling/v3-text-to-video",
            "intent": {"seconds": "duration"}})
        got = kie.estimate_credits("kling-like", 6)
        assert got["known"] is False and got["credits"] is None
        assert kie.VIDEO_CREDITS_ENV in got["basis"]

    def test_the_estimate_moves_with_the_length_and_stays_in_the_band(self):
        """An upper bound spread across the model's own duration range, so the
        longest shot it will generate quotes at the top of kie's band."""
        short = kie.estimate_credits("seedance-2", 4)["credits"]
        longest = kie.estimate_credits("seedance-2", 15)["credits"]
        assert 100 <= short < longest <= 500

    def test_dollars_need_the_credit_rate_and_the_account_rate_separately(
            self, monkeypatch):
        """Two independent unknowns. Folded together a caller cannot tell which
        is missing — and one of them is a thing they can fix."""
        unpriced = kie.estimate_usd("seedance-2", 5)
        assert unpriced["credits_known"] is True
        assert unpriced["known"] is False and unpriced["usd"] is None
        assert kie.USD_PER_CREDIT_ENV in unpriced["basis"]

        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        priced = kie.estimate_usd("seedance-2", 5)
        assert priced["known"] is True
        assert priced["usd"] == pytest.approx(priced["credits"] * 0.005)

    def test_the_environment_beats_the_built_in_band(self, monkeypatch):
        """A user with a month of invoices knows more than this table does."""
        monkeypatch.setenv(kie.VIDEO_CREDITS_ENV,
                           json.dumps({"seedance-2": {"per_second": 10,
                                                      "per_call": 5}}))
        got = kie.estimate_credits("seedance-2", 6)
        assert got["credits"] == 65
        assert kie.VIDEO_CREDITS_ENV in got["rate"]["origin"]

    def test_a_junk_override_is_ignored_rather_than_believed(self, monkeypatch):
        monkeypatch.setenv(kie.VIDEO_CREDITS_ENV, "not json at all")
        assert kie.estimate_credits("seedance-2", 6)["known"] is True
        monkeypatch.setenv(kie.VIDEO_CREDITS_ENV,
                           json.dumps({"seedance-2": {"per_second": -3}}))
        # A negative rate is not a rate. It falls through to the table rather
        # than quoting a shot at less than nothing.
        assert kie.estimate_credits("seedance-2", 6)["rate"]["per_second"] > 0

    def test_a_zero_rate_is_never_a_rate(self, monkeypatch):
        """The one number this module refuses to produce."""
        monkeypatch.setenv(kie.VIDEO_CREDITS_ENV,
                           json.dumps({"seedance-2": {"per_second": 0,
                                                      "per_call": 0,
                                                      "minimum": 0}}))
        assert kie.estimate_credits("seedance-2", 6)["credits"] > 0

    def test_the_estimate_says_it_is_one(self):
        got = kie.estimate_usd("seedance-2", 5)
        assert "ESTIMATE" in got["note"]
        assert any("not modelled" in c for c in got["caveats"])


class TestRegistrationValidation:
    """A typo in a model id passes registration and fails as a PAID 404 — after
    the conditioning frames have been uploaded. kie serves no catalogue endpoint
    this adapter can read, so none of this can prove an id EXISTS; what it can
    do is refuse the ones that could not possibly."""

    def _spec(self, **over):
        return {"model": "vendor/model-v1",
                "intent": {"seconds": "duration"}, **over}

    def test_an_id_with_whitespace_is_refused(self, clean_models):
        with pytest.raises(kie.KieError, match="whitespace"):
            kie.register_video_model("x", self._spec(model="vendor/model \n"))

    def test_a_url_is_not_a_model_id(self, clean_models):
        with pytest.raises(kie.KieError, match="URL, not a model id"):
            kie.register_video_model(
                "x", self._spec(model="https://docs.kie.ai/market/kling"))

    def test_a_malformed_id_is_refused(self, clean_models):
        with pytest.raises(kie.KieError, match="not shaped like"):
            kie.register_video_model("x", self._spec(model="two words"))

    def test_a_bare_string_enum_is_refused_not_split_into_letters(
            self, clean_models):
        """tuple("720p") is ('7','2','0','p') — an enum that refuses every real
        value and lists letters when it does."""
        with pytest.raises(kie.KieError, match="not a list"):
            kie.register_video_model("x", self._spec(
                intent={"seconds": "duration", "quality": "resolution"},
                enums={"resolution": "720p"}))

    def test_a_backwards_range_is_refused(self, clean_models):
        with pytest.raises(kie.KieError, match="low bound is above"):
            kie.register_video_model("x", self._spec(
                ranges={"duration": (15, 4)}))

    def test_a_cap_of_zero_is_refused(self, clean_models):
        with pytest.raises(kie.KieError, match="refuse every request"):
            kie.register_video_model("x", self._spec(
                intent={"seconds": "duration", "refs": "image_urls"},
                caps={"image_urls": 0}))

    def test_a_limit_on_a_field_the_model_does_not_take_is_refused(
            self, clean_models):
        with pytest.raises(kie.KieError, match="not in .supports."):
            kie.register_video_model("x", self._spec(
                ranges={"n_frames": (1, 10)}))

    def test_two_intents_on_one_field_are_refused(self, clean_models):
        """The second overwrites the first in video_input's output, so one of
        the two settings is billed for and does not apply."""
        with pytest.raises(kie.KieError, match="both map to"):
            kie.register_video_model("x", self._spec(
                intent={"seconds": "duration", "quality": "duration"}))

    def test_a_scale_of_zero_is_refused(self, clean_models):
        with pytest.raises(kie.KieError, match="multiplier"):
            kie.register_video_model("x", self._spec(
                intent_scale={"seconds": 0}))

    def test_intent_values_for_an_intent_the_model_lacks_are_refused(
            self, clean_models):
        with pytest.raises(kie.KieError, match="no intent entry"):
            kie.register_video_model("x", self._spec(
                intent_values={"shape": {"16:9": "landscape"}}))

    def test_a_built_in_cannot_be_registered_over(self, clean_models):
        """Every sequence already planned against that name would silently
        start buying from an unverified entry."""
        with pytest.raises(kie.KieError, match="built-in"):
            kie.register_video_model("seedance-2", self._spec())

    def test_two_names_for_one_id_that_disagree_are_refused(self, clean_models):
        kie.register_video_model("a", self._spec(ranges={"duration": (4, 15)}))
        with pytest.raises(kie.KieError, match="describe it differently"):
            kie.register_video_model("b", self._spec(ranges={"duration": (1, 5)}))

    def test_re_registering_the_same_model_is_allowed(self, clean_models):
        kie.register_video_model("a", self._spec())
        assert kie.register_video_model("a", self._spec())["model"] == "a"

    def test_a_registered_model_says_its_id_was_never_confirmed(
            self, clean_models):
        got = kie.register_video_model("a", self._spec())
        assert got["verified"] is False
        assert "never been confirmed" in got["verified_note"]
        assert kie.video_capabilities("seedance-2")["verified"] is True


class TestProbingAnId:
    """The only free way to ask kie whether an id exists, and its caveat.

    There is no catalogue endpoint. What discriminates is the business code on a
    deliberately malformed createTask: a bad id answers 404, a good one answers
    422 because it got as far as validating the input. That is inference from
    kie's own error table rather than a documented contract, which is why the
    probe is opt-in and why the branch where kie ACCEPTS the empty input says
    loudly that a job may have been created.
    """

    def test_no_key_is_unknown_rather_than_a_verdict(self):
        got = kie.probe_model_id("seedance-2")
        assert got["exists"] is None and got["checked"] is False
        assert "KIE_API_KEY" in got["reason"]

    def test_a_404_means_the_id_is_wrong(self, keyed, monkeypatch,
                                         clean_models):
        kie.register_video_model("typo", {"model": "vendor/mdoel-v1",
                                          "intent": {"seconds": "duration"}})

        def boom(*a, **k):
            raise kie.KieError("kie refused POST /api/v1/jobs/createTask "
                               "(code 404)")

        monkeypatch.setattr(kie, "_request", boom)
        got = kie.probe_model_id("typo")
        assert got["exists"] is False
        assert kie.video_capabilities("typo")["verified"] is False

    def test_a_422_proves_the_model_resolved_and_nothing_was_charged(
            self, keyed, monkeypatch, clean_models):
        kie.register_video_model("real", {"model": "vendor/model-v1",
                                          "intent": {"seconds": "duration"}})

        def refuse(*a, **k):
            raise kie.KieError("kie refused POST /api/v1/jobs/createTask "
                               "(code 422): Please enter prompt.")

        monkeypatch.setattr(kie, "_request", refuse)
        got = kie.probe_model_id("real")
        assert got["exists"] is True and got["task_id"] == ""
        assert "charged" in got["reason"]
        assert kie.video_capabilities("real")["verified"] is True

    def test_an_accepted_empty_input_is_reported_as_possibly_billed(
            self, keyed, monkeypatch, clean_models):
        """The branch the opt-in exists for. A task id that came back is money
        that may be moving, and losing it loses the only handle on it."""
        kie.register_video_model("loose", {"model": "vendor/model-v1",
                                           "intent": {"seconds": "duration"}})
        monkeypatch.setattr(kie, "_request",
                            lambda *a, **k: {"taskId": "task-77"})
        got = kie.probe_model_id("loose")
        assert got["exists"] is True and got["task_id"] == "task-77"
        assert "may be charged" in got["reason"]
