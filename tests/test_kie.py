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
        # the 422, and believing anchored generation works.
        with pytest.raises(kie.KieError) as exc:
            kie.build_input("qwen-edit", prompt="x", image_url="C:/art/hero.png")
        assert "public https URL" in str(exc.value)
        assert "Krea" in str(exc.value)

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
        assert got["credits_consumed"] == 40 and got["estimated_usd"] == 0.2

    def test_an_unpriced_success_says_so_instead_of_writing_a_zero(self, root):
        result = kie._account({"ok": True, "estimated_usd": None,
                               "credits_consumed": 30},
                              root, kind="image")
        # A silent ledger row of $0 would under-count a real charge; the result
        # states the gap and keeps the credit count so it stays recoverable.
        assert result["accounted"] is False
        assert kie.USD_PER_CREDIT_ENV in result["cost_note"]
        assert result["credits_consumed"] == 30

    def test_a_priced_success_lands_in_the_ledger_under_its_own_kind(
            self, root, monkeypatch):
        from bgate_core import spend

        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        kie._account({"ok": True, "estimated_usd": kie.cost_usd(200),
                      "model": "seedance-2"},
                     root, kind="video", logical_name="chase")
        totals = spend.totals(root)
        # "video" is its own bucket: kie prices a clip at 100-500 credits
        # against an image's 10-50, so it must not sum into "other".
        assert totals["by_kind"]["video"] == pytest.approx(1.0)
        assert spend.for_logical(root, "chase") == pytest.approx(1.0)

    def test_a_failure_is_never_accounted(self, root):
        result = kie._account({"ok": False, "estimated_usd": 5.0}, root,
                              kind="image")
        assert result["accounted"] is False

    def test_video_is_a_known_spend_kind(self):
        from bgate_core import spend
        assert "video" in spend.KINDS


class TestWiring:
    def test_the_provider_registry_carries_kie_and_what_it_powers(self):
        from bgate_core import providers

        one = providers.by_id("kie")
        assert one.env == "KIE_API_KEY"
        # The three capabilities the user asked for, and NOT model_3d.
        assert set(one.powers) == {"image_2d", "audio", "video"}
        assert "model_3d" not in one.powers
        assert one.key_url.startswith("https://")

    def test_every_power_is_a_declared_capability(self):
        from bgate_core import providers
        for one in providers.PROVIDERS:
            assert set(one.powers) <= set(providers.CAPABILITIES)

    def test_the_doctor_row_goes_green_on_a_kie_only_project(self, monkeypatch):
        # The bug this repeats otherwise: `MISS openai_key` and a non-zero exit
        # for a setup that is completely fine.
        from bgate_core import doctor

        for one in ("OPENAI_API_KEY", "KREA_API_KEY"):
            monkeypatch.delenv(one, raising=False)
        monkeypatch.setenv("KIE_API_KEY", "kie-test-not-a-real-key")
        row = doctor._probe_art_key()
        assert row["available"] is True and "KIE_API_KEY" in row["path"]

    def test_kie_is_a_workflow_provider(self):
        from bgate_core import generate
        assert "kie" in generate.PROVIDERS

    def test_an_unpriced_kie_node_refuses_rather_than_planning_at_zero(self):
        from bgate_core import generate

        with pytest.raises(generate.GenerateRefused) as exc:
            generate.plan({"provider": "kie", "model": "nano-banana"})
        assert kie.USD_PER_CREDIT_ENV in str(exc.value)

    def test_a_rated_kie_node_plans_with_a_real_ceiling(self, monkeypatch):
        from bgate_core import generate

        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        spec = generate.plan({"provider": "kie", "model": "nano-banana"})
        assert spec["provider"] == "kie" and spec["unit_usd"] > 0

    def test_an_unknown_kie_model_is_refused_at_plan_time(self, monkeypatch):
        from bgate_core import generate

        monkeypatch.setenv(kie.USD_PER_CREDIT_ENV, "0.005")
        with pytest.raises(generate.GenerateRefused) as exc:
            generate.plan({"provider": "kie", "model": "seedance-2"})
        # A video model is not an image model, however real its id is.
        assert "no image model" in str(exc.value)

    def test_chroma_refuses_anchored_work_on_kie_before_spending(self, root):
        from bgate_core import chroma

        got = chroma.generate("a hero", str(root / "out.png"), provider="kie",
                              ref_paths=[str(root / "anchor.png")], root=root)
        assert got["ok"] is False
        assert "cannot condition on the pinned refs" in got["error"]
        assert "krea" in got["error"]

    def test_the_mcp_server_exposes_the_two_new_capabilities(self):
        from bgate_mcp import server

        for name in ("kie_status", "kie_music_generate", "kie_video_generate"):
            assert hasattr(server, name), f"{name} is not an MCP tool"
