"""Deepgram voice: the key never leaves, the request is the documented one.

FOUR THINGS ARE WORTH ASSERTING HERE and the rest is Deepgram's problem.

1. NO KEY REACHES A RESPONSE. This is the whole design constraint — the browser
   never gets DEEPGRAM_API_KEY, in a body, a header, a URL or an error. Asserted
   by setting a distinctive key and searching every byte the endpoints emit,
   including the failure paths, which is where a key normally escapes (an
   exception that stringifies a Request object, a URL echoed in a 502).
2. THE ADAPTER BUILDS THE DOCUMENTED REQUEST. Model ids, encoding, sample rate
   and the `Authorization: Token` header shape, checked against what was read
   off Deepgram's reference — because the failure mode of getting these wrong is
   a socket that opens fine and transcribes nothing, or chipmunks.
3. SPEND IS ACCOUNTED, in the 'speech' bucket and not in 'audio'.
4. NO KEY DEGRADES GRACEFULLY. Status is 200 with a reason, /speak is a 503 with
   a sentence, and the sentence names the variable to set.

NOTHING HERE TOUCHES THE NETWORK. There is no fixture that would let it: the key
is fake, `speak` is exercised against a stubbed urlopen, and the relay's
Deepgram leg is never opened. A test that needed a real key would be a test
nobody runs.
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from bgate_adapters import deepgram
from bgate_core.store import db
from bgate_core.runtime import providers
from bgate_core.board import spend

# Distinctive enough that a substring search cannot false-negative, and shaped
# nothing like a real Deepgram key so it can never be mistaken for one.
FAKE = "dg-THIS-IS-NOT-A-REAL-KEY-8b41f0e2"


@pytest.fixture()
def keyed(monkeypatch):
    monkeypatch.setenv(deepgram.ENV, FAKE)
    return FAKE


@pytest.fixture()
def keyless(monkeypatch):
    """The state this machine is actually in, and the one the UI must survive."""
    monkeypatch.delenv(deepgram.ENV, raising=False)


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    from bgate_ui.app import app
    return TestClient(app)


class _Response:
    """Just enough of http.client.HTTPResponse for urlopen's context manager."""

    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def captured(monkeypatch):
    """Stub urlopen and hand back the Request object it was given."""
    seen: dict = {}

    def fake(request, timeout=None):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response(b"RIFF----WAVEfake", {
            "dg-char-count": "42",
            "dg-request-id": "req-1234",
            "dg-model-name": "aura-2-thalia-en",
            "Content-Type": "audio/wav",
        })

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return seen


# ---------------------------------------------------------------------------
# 1. The key does not leave
# ---------------------------------------------------------------------------
class TestTheKeyNeverReachesTheBrowser:
    def test_status_says_it_has_a_key_without_showing_it(self, client, keyed):
        res = client.get("/api/voice/status")
        assert res.status_code == 200
        assert FAKE not in res.text
        assert res.json()["data"]["key"] is True

    def test_the_speak_response_carries_audio_and_no_key(self, client, keyed,
                                                         captured):
        res = client.post("/api/voice/speak", json={"text": "hello there"})
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("audio/")
        assert FAKE.encode() not in res.content
        for value in res.headers.values():
            assert FAKE not in value

    def test_a_refusal_does_not_leak_the_key_either(self, client, keyed,
                                                    monkeypatch):
        """The path a key usually escapes by: an error that stringifies too
        much. Deepgram's 401 body is echoed to the human on purpose, so this
        asserts the echo cannot carry the credential that was rejected."""
        import urllib.error

        def boom(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {},
                io.BytesIO(b'{"err_msg":"Invalid credentials"}'))

        monkeypatch.setattr("urllib.request.urlopen", boom)
        res = client.post("/api/voice/speak", json={"text": "hello"})
        assert res.status_code == 502
        assert FAKE not in res.text
        # And it still says what to DO about a 401.
        assert deepgram.ENV in res.json()["error"]["message"]

    def test_the_key_is_in_a_header_and_never_in_the_url(self, keyed, captured):
        deepgram.speak("hello")
        request = captured["request"]
        assert FAKE not in request.full_url
        assert request.get_header("Authorization") == f"Token {FAKE}"
        assert deepgram.listen_url() and FAKE not in deepgram.listen_url()

    def test_the_provider_row_shows_a_fingerprint_and_not_the_key(self, root,
                                                                   keyed):
        row = providers.status_for(root, "deepgram")
        assert row["configured"] is True
        assert row["last4"] == FAKE[-4:]
        assert FAKE not in json.dumps(row)


# ---------------------------------------------------------------------------
# 2. The documented request
# ---------------------------------------------------------------------------
class TestTheAdapterBuildsTheDocumentedRequest:
    def test_the_listen_query_matches_the_streaming_reference(self, keyed):
        params = deepgram.listen_params()
        assert params["model"] == "nova-3"
        assert params["encoding"] == "linear16"
        assert params["sample_rate"] == "16000"
        assert params["channels"] == "1"
        # Interims are what make the UI show words while somebody is talking,
        # and utterance_end_ms is the backstop that ends a turn.
        assert params["interim_results"] == "true"
        assert params["vad_events"] == "true"
        assert int(params["utterance_end_ms"]) >= 1000
        assert deepgram.listen_url().startswith("wss://api.deepgram.com/v1/listen?")

    def test_an_unknown_listen_model_is_refused_before_a_round_trip(self):
        with pytest.raises(deepgram.DeepgramError) as exc:
            deepgram.listen_params(model="nova-9")
        assert "nova-3" in str(exc.value)

    def test_speak_posts_json_text_with_the_model_in_the_query(self, keyed,
                                                              captured):
        got = deepgram.speak("hello there", model="aura-2-thalia-en")
        request = captured["request"]
        assert request.get_method() == "POST"
        assert request.full_url.startswith("https://api.deepgram.com/v1/speak?")
        assert "model=aura-2-thalia-en" in request.full_url
        assert json.loads(request.data.decode()) == {"text": "hello there"}
        assert got["ok"] is True and got["media_type"] == "audio/wav"

    def test_the_billed_character_count_comes_from_deepgram_not_from_len(
            self, keyed, captured):
        """dg-char-count is what the invoice uses. Our own len() differs on
        anything Deepgram normalises, and a ledger that disagrees with the bill
        is the thing spend.py exists to stop."""
        got = deepgram.speak("hello")
        assert got["chars"] == 42
        assert got["usd"] == pytest.approx(42 / 1000 * 0.030)

    def test_a_reply_over_the_documented_cap_is_refused_locally(self, keyed):
        got = deepgram.speak("x" * (deepgram.MAX_SPEAK_CHARS + 1))
        assert got["ok"] is False
        assert str(deepgram.MAX_SPEAK_CHARS) in got["error"]

    def test_a_stream_is_priced_from_bytes_relayed_not_wall_clock(self):
        """A socket held open while nobody speaks bills nothing."""
        assert deepgram.stream_cost(0)["usd"] == 0
        one_minute = deepgram.BYTES_PER_SECOND * 60
        assert deepgram.stream_cost(one_minute)["seconds"] == 60
        assert deepgram.stream_cost(one_minute)["usd"] == pytest.approx(0.0048)

    def test_an_unpriced_model_is_none_and_never_zero(self, monkeypatch):
        """The krea.TRAIN_USD precedent: every budget check reads a number as
        permission to spend it, so a made-up 0.0 is worse than no answer."""
        monkeypatch.setitem(deepgram.USD_PER_MINUTE, "base", None)
        assert deepgram.stream_cost(deepgram.BYTES_PER_SECOND * 60,
                                    model="base")["usd"] is None

    def test_results_are_normalised_so_the_ui_never_walks_the_shape(self):
        shaped = deepgram.read_transcript({
            "type": "Results", "is_final": True, "speech_final": True,
            "channel": {"alternatives": [{"transcript": "the hub has weather",
                                          "confidence": 0.98}]}})
        assert shaped["text"] == "the hub has weather"
        assert shaped["final"] is True and shaped["speech_final"] is True
        # A message with no alternatives must not be a KeyError mid-sentence.
        assert deepgram.read_transcript({"type": "Metadata"})["text"] == ""
        # UtteranceEnd is the backstop for a trailing word that never ended.
        assert deepgram.read_transcript(
            {"type": "UtteranceEnd"})["speech_final"] is True


# ---------------------------------------------------------------------------
# 3. Spend
# ---------------------------------------------------------------------------
class TestSpendIsAccounted:
    def test_speaking_writes_a_speech_row_not_an_audio_one(self, client, keyed,
                                                           captured, root):
        client.post("/api/voice/speak", json={"text": "hello there"})
        rows = [dict(r) for r in db.connect(root).execute(
            "SELECT kind, usd, model, detail FROM spend_event")]
        assert len(rows) == 1
        # 'audio' means a sound asset the game ships. This is conversation.
        assert rows[0]["kind"] == "speech"
        assert rows[0]["usd"] == pytest.approx(42 / 1000 * 0.030)
        assert "42 chars" in rows[0]["detail"]

    def test_speech_is_a_kind_the_ledger_will_accept(self, root):
        """Migration 0024 widened the CHECK. Without it spend.record's own
        blanket except would swallow every IntegrityError and the rows would
        vanish silently — which is exactly what happened to 'mesh' before 0023."""
        assert "speech" in spend.KINDS
        spend.record(root, 0.0048, kind="speech", detail="deepgram stt 60.0s")
        assert spend.totals(root)["by_kind"].get("speech") == pytest.approx(0.0048)

    def test_a_zero_length_turn_writes_nothing(self, root):
        spend.record(root, 0.0, kind="speech", detail="deepgram stt 0.0s")
        assert spend.totals(root)["by_kind"].get("speech") is None


# ---------------------------------------------------------------------------
# 4. No key: the state this machine is in
# ---------------------------------------------------------------------------
class TestItDegradesWithoutAKey:
    def test_status_is_200_and_names_the_variable_to_set(self, client, keyless):
        res = client.get("/api/voice/status")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["available"] is False and data["key"] is False
        assert deepgram.ENV in data["reason"]
        # The UI paints its mic control from this and must not have to guess.
        assert data["audio"]["sample_rate"] == 16000

    def test_speak_is_503_with_a_sentence_rather_than_a_crash(self, client,
                                                              keyless):
        res = client.post("/api/voice/speak", json={"text": "hello"})
        assert res.status_code == 503
        assert deepgram.ENV in res.json()["error"]["message"]

    def test_the_two_reasons_are_reported_separately(self, keyless,
                                                     monkeypatch):
        """A missing key and a missing `websockets` extra need different actions
        from the human — a single 'voice unavailable' sends them to the wrong
        one."""
        monkeypatch.setattr(deepgram, "have_websockets", lambda: False)
        reason = deepgram.available()["reason"]
        assert deepgram.ENV in reason and "websockets" in reason

    def test_an_empty_request_is_a_400_not_a_500(self, client, keyed):
        assert client.post("/api/voice/speak", json={}).status_code == 400

    def test_the_doctor_art_row_stays_red_for_a_deepgram_only_project(
            self, root, keyed, monkeypatch):
        """The bug this addition would otherwise introduce. Deepgram generates
        no art, so counting it in the art_key row would print green for a
        project that cannot produce one image — the same lie the openai-only
        probe was replaced for, pointing the other way."""
        from bgate_core.runtime import doctor

        for one in providers.PROVIDERS:
            if one.id != "deepgram":
                monkeypatch.delenv(one.env, raising=False)
        row = doctor._probe_art_key()
        assert row["available"] is False
        assert deepgram.ENV not in row["reason"]

    def test_speech_is_its_own_capability_and_deepgram_claims_only_it(self):
        one = providers.by_id("deepgram")
        assert one.env == "DEEPGRAM_API_KEY"
        assert set(one.powers) == {"speech"}
        assert set(one.powers) <= set(providers.CAPABILITIES)
        assert not providers.ART_CAPABILITIES.intersection(one.powers)
        assert one not in providers.art_providers()
