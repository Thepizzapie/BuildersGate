"""Deepgram — hosted speech-to-text and text-to-speech, for talking to an agent.

WHY A SECOND SPEECH BACKEND. ``bgate_adapters/transcribe.py`` already turns audio
into words, and it stays exactly where it is: faster-whisper in a subprocess,
batch, over a finished playtest wav. It cannot do the thing this module exists
for. A live dialogue needs PARTIAL results while the human is still talking, and
a local model that loads for ~10s and then transcribes a file has no interim to
give. So these are two backends for two shapes of the same problem, not a
replacement — PLAYTEST TRANSCRIPTION IS UNCHANGED and nothing here is wired into
it. If that ever changes it should be a deliberate, argued switch, because it
moves a currently-offline, currently-free path onto a metered network service.

THE TWO SURFACES, read off Deepgram's own reference (sources at the bottom).

  STREAMING STT   wss://api.deepgram.com/v1/listen
                  Authorization: Token <key>
                  Query: model, encoding, sample_rate, channels,
                  interim_results, punctuate, smart_format, endpointing,
                  utterance_end_ms, vad_events.
                  Client sends RAW BINARY audio frames — no wrapper, no
                  base64 — and JSON control messages {"type": "KeepAlive"},
                  {"type": "Finalize"}, {"type": "CloseStream"}.
                  Server sends JSON: Results / UtteranceEnd / SpeechStarted /
                  Metadata. A Results carries
                  ``channel.alternatives[0].transcript`` plus ``is_final`` and
                  ``speech_final``, and the difference between those two is the
                  whole reason this is usable for dialogue:

                    is_final=false   an INTERIM guess. It will be revised.
                                     Paint it, never send it.
                    is_final=true    this SEGMENT is settled, but the sentence
                                     may continue.
                    speech_final=true  endpointing heard the human stop. THIS
                                     is the boundary that means "their turn is
                                     over" — the one to send as a chat message.

  TTS             POST https://api.deepgram.com/v1/speak
                  Authorization: Token <key>, body {"text": "..."}
                  Query: model, encoding, container. Answers audio bytes, with
                  the character count in the ``dg-char-count`` response header —
                  which is what makes the ledger row exact rather than a guess
                  at our own strlen. Aura-1 and Aura-2 cap input at 2000
                  characters per request, so :func:`speak` refuses past that
                  instead of sending a request that comes back 400.

THE KEY NEVER REACHES THE BROWSER, AND THIS MODULE IS WHY THAT IS CHEAP. Both
calls above are made from the server. See ``bgate_ui/routes/voice.py`` for the
relay and for the argument against the alternative (Deepgram's
``POST /v1/auth/grant``, which mints a short-lived JWT a browser could hold).
Nothing here returns, logs or formats a key, and nothing here puts one in a URL:
the credential travels in a header on both surfaces.

PRICES ARE PUBLISHED, so unlike kie there is a real number to record. Nova-3
streaming is $0.0048/min monolingual and $0.0058/min multilingual; Aura-2 is
$0.030 per 1000 characters. Those are pay-as-you-go list rates and a negotiated
or committed-use account pays less, so a ledger row from here is an UPPER BOUND
and says so. A model this module has never been told the price of resolves to
None, never 0.0 — the precedent krea.TRAIN_USD set, because every budget check in
this product reads a number as permission to spend it.

WEBSOCKETS IS NOT A PINNED DEPENDENCY. It arrives only as an EXTRA of things we
do pin — ``uvicorn[standard]``, ``mcp[ws]``, ``openai[realtime]`` — so a clean
`pip install -e .` has no websocket implementation at all, in either direction:
no client to reach Deepgram with and no server-side protocol for the browser's
own socket to uvicorn. It is therefore declared as the `voice` extra rather than
assumed, and :func:`available` reports its absence as its own sentence. Checked
before writing a hand-rolled RFC 6455 client, which is the other way this could
have gone and is 200 lines of framing nobody should own here.

Everything except the websocket leg is stdlib.

SOURCES, read 2026-08-09:
  https://developers.deepgram.com/reference/speech-to-text/listen-streaming
  https://developers.deepgram.com/docs/text-to-speech
  https://developers.deepgram.com/guides/fundamentals/token-based-authentication
  https://deepgram.com/pricing
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

ENV = "DEEPGRAM_API_KEY"
KEY_URL = "https://console.deepgram.com/"

HOST = "api.deepgram.com"
LISTEN_URL = f"wss://{HOST}/v1/listen"
SPEAK_URL = f"https://{HOST}/v1/speak"
# The road not taken, named so nobody has to go looking for it: this is what
# would hand a browser its own short-lived credential. routes/voice.py explains
# why the relay won instead.
GRANT_URL = f"https://{HOST}/v1/auth/grant"

# ---------------------------------------------------------------------------
# Models and money
# ---------------------------------------------------------------------------

# Only models whose id was read off Deepgram's own reference are here. A guessed
# id is a round trip that comes back 400 with the human already talking.
LISTEN_MODELS = ("nova-3", "nova-3-general", "nova-2", "nova", "enhanced", "base")
DEFAULT_LISTEN_MODEL = "nova-3"

SPEAK_MODELS = ("aura-2-thalia-en", "aura-2-andromeda-en", "aura-2-apollo-en",
                "aura-2-arcas-en", "aura-2-asteria-en", "aura-2-orion-en",
                "aura-asteria-en")
DEFAULT_SPEAK_MODEL = "aura-2-thalia-en"

# Pay-as-you-go list rates. An UPPER BOUND on what a call costs — see the module
# docstring. A model absent from these maps is priced None and the caller says
# "no dollar figure recorded" rather than logging a free-looking 0.0.
USD_PER_MINUTE: dict[str, Optional[float]] = {
    "nova-3": 0.0048, "nova-3-general": 0.0048,
    "nova-2": 0.0043, "nova": 0.0043, "enhanced": 0.0145, "base": 0.0125,
}
USD_PER_1K_CHARS: dict[str, Optional[float]] = {
    m: (0.030 if m.startswith("aura-2-") else 0.015) for m in SPEAK_MODELS
}

# Aura-1 and Aura-2 both document this ceiling. Refused here rather than at
# Deepgram so the caller gets a sentence instead of a 400 body.
MAX_SPEAK_CHARS = 2000

# ---------------------------------------------------------------------------
# Audio format. Fixed rather than negotiated, and the browser is held to it.
# ---------------------------------------------------------------------------
# linear16 at 16 kHz mono, because it is the one encoding with no container to
# get wrong. MediaRecorder's webm/opus was the obvious alternative and is a trap
# for a RELAY: its chunks after the first are not standalone, so anything that
# reframes, drops or reorders them produces audio Deepgram cannot decode and a
# transcript that is silently empty rather than an error. Raw PCM has no such
# state — every frame stands alone, and a dropped one costs exactly its own
# milliseconds.
ENCODING = "linear16"
SAMPLE_RATE = 16000
CHANNELS = 1
# Two bytes a sample, which is how bytes-on-the-wire become billable seconds.
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * 2

# Milliseconds of silence that end an utterance. Deepgram's default endpointing
# is 10ms, which is tuned for command-and-control and cuts a thinking human off
# mid-sentence; utterance_end_ms is the slower, word-timing-based signal and
# Deepgram documents 1000 as the sane floor for it.
ENDPOINTING_MS = 400
UTTERANCE_END_MS = 1000


class DeepgramError(RuntimeError):
    """A Deepgram call failed in a way the caller should surface, not retry."""


# ---------------------------------------------------------------------------
# Can this run?
# ---------------------------------------------------------------------------

def _key() -> str:
    return (os.environ.get(ENV) or "").strip()


def have_websockets() -> bool:
    """Is there a websocket implementation in this interpreter at all?

    Import-free: ``websockets`` pulls in asyncio machinery and this is called
    from the providers panel, the doctor and every voice status poll.
    """
    import importlib.util
    return importlib.util.find_spec("websockets") is not None


def available(root: Any = None) -> dict:
    """Can we do voice, and if not, WHICH of the two reasons is it?

    Both are reported, separately, because they need different actions from the
    human — one is a key to paste into the settings panel and the other is a pip
    install — and a single "voice unavailable" would send them to the wrong one.
    ``root`` is accepted and ignored, matching the probe signature the provider
    registry calls with.
    """
    missing = []
    if not _key():
        missing.append(
            f"{ENV} is not set — paste a key from {KEY_URL} into the project's "
            ".env through the settings panel")
    if not have_websockets():
        missing.append(
            "the `websockets` package is not installed — pip install "
            "'builders-gate[voice]'. It is not a pinned dependency: it arrives "
            "only as an extra of uvicorn/mcp/openai, so a clean install has no "
            "websocket support in either direction")
    if missing:
        return {"available": False, "reason": " · ".join(missing),
                "key": bool(_key()), "websockets": have_websockets()}
    return {"available": True, "reason": "", "key": True, "websockets": True,
            "listen_model": DEFAULT_LISTEN_MODEL,
            "speak_model": DEFAULT_SPEAK_MODEL}


def can_speak() -> dict:
    """Can we do TEXT TO SPEECH, which is a different question from ``available``.

    ``speak`` is one synchronous HTTPS POST through urllib. It does not open a
    socket, it does not import ``websockets``, and it works perfectly on a
    machine that has never heard of that package. Only the realtime LISTEN relay
    needs it, in both directions: a client to reach Deepgram and a server
    implementation before FastAPI can accept the browser's connection.

    Gating /api/voice/speak on ``available()`` therefore refused a feature that
    was working. On a clean ``pip install -e .`` — which resolves no websocket
    support at all, as pyproject's `voice` extra explains — the whole of TTS
    answered 503 with a message telling the user to install a package it does
    not use.
    """
    if not _key():
        return {"available": False, "key": False,
                "reason": f"{ENV} is not set — paste a key from {KEY_URL} into "
                          "the project's .env through the settings panel"}
    return {"available": True, "key": True, "reason": "",
            "speak_model": DEFAULT_SPEAK_MODEL}


def _require() -> str:
    """The KEY, and only the key.

    This asked ``available()``, which is a stricter question: it also requires
    the `websockets` package. Since ``auth_header`` goes through here, that made
    every authenticated call depend on websockets — including ``speak``, which
    is one synchronous urllib POST that never opens a socket. On a clean install
    (pyproject's `voice` extra exists precisely because nothing pins websockets)
    text-to-speech refused itself with a message telling the user to install a
    package it does not use.

    The socket requirement belongs to the listen relay and is enforced there,
    where a missing implementation is a real blocker rather than a guess.
    """
    verdict = can_speak()
    if not verdict["available"]:
        raise DeepgramError(verdict["reason"])
    return _key()


# ---------------------------------------------------------------------------
# Streaming STT — the parameters, and the arithmetic that prices a stream
# ---------------------------------------------------------------------------

def listen_params(*, model: str = DEFAULT_LISTEN_MODEL,
                  language: str = "en-US",
                  interim_results: bool = True) -> dict[str, str]:
    """The query Deepgram's realtime socket is opened with.

    Split out from the connect call so a test can assert the documented request
    shape without a socket, and so the one place that knows the audio format is
    the one place that declares it.
    """
    if model not in LISTEN_MODELS:
        raise DeepgramError(
            f"unknown listen model {model!r} — known: {', '.join(LISTEN_MODELS)}")
    return {
        "model": model,
        "language": language,
        "encoding": ENCODING,
        "sample_rate": str(SAMPLE_RATE),
        "channels": str(CHANNELS),
        # Interims are what make the UI show words as they are spoken. They are
        # never sent as a message — see the module docstring on speech_final.
        "interim_results": "true" if interim_results else "false",
        "punctuate": "true",
        "smart_format": "true",
        "endpointing": str(ENDPOINTING_MS),
        "utterance_end_ms": str(UTTERANCE_END_MS),
        # Without vad_events there is no SpeechStarted, and the mic indicator
        # has nothing to go on but the human's own faith that it is listening.
        "vad_events": "true",
    }


def listen_url(**kwargs) -> str:
    return LISTEN_URL + "?" + urllib.parse.urlencode(listen_params(**kwargs))


def auth_header() -> dict[str, str]:
    """The one header both surfaces authenticate with.

    A function rather than a value so there is no module-level constant holding
    a key, and so the caller cannot accidentally log the dict it did not build.
    """
    return {"Authorization": f"Token {_require()}"}


def stream_cost(audio_bytes: int, *, model: str = DEFAULT_LISTEN_MODEL) -> dict:
    """What a stream of ``audio_bytes`` PCM cost, from the bytes we relayed.

    Counted from bytes rather than from wall-clock time on purpose: a socket
    held open while nobody speaks bills nothing, and a session that idles for
    ten minutes must not appear in the ledger as ten minutes of transcription.
    """
    seconds = max(0.0, audio_bytes) / BYTES_PER_SECOND
    rate = USD_PER_MINUTE.get(model)
    return {
        "seconds": round(seconds, 2),
        "bytes": int(audio_bytes),
        "model": model,
        "usd": None if rate is None else round(seconds / 60.0 * rate, 6),
        "rate_usd_per_min": rate,
    }


async def open_listen_socket(**kwargs):
    """Connect to Deepgram's realtime socket. Returns the open connection.

    The import is inside the function, not at module scope: this module is on
    the providers panel's import path and ``websockets`` is an optional extra,
    so importing it up top would make a missing extra a 500 on a page that has
    nothing to do with voice — instead of the sentence :func:`available` already
    has ready.
    """
    import websockets
    return await websockets.connect(
        listen_url(**kwargs),
        additional_headers=auth_header(),
        # Deepgram sends nothing on an idle socket and websockets' default ping
        # keepalive would close a stream during a long pause. Deepgram's own
        # KeepAlive control message is the documented way to hold it open, and
        # the relay sends those.
        ping_interval=None,
        max_size=None,
    )


CLOSE_STREAM = json.dumps({"type": "CloseStream"})
KEEP_ALIVE = json.dumps({"type": "KeepAlive"})
FINALIZE = json.dumps({"type": "Finalize"})


def read_transcript(message: dict) -> dict:
    """Normalise one server message into the four fields a caller acts on.

    Everything that has to know Deepgram's response shape is here, so the relay
    and the browser both work against ``{type, text, final, speech_final}``
    rather than each independently reaching four levels down for
    ``channel.alternatives[0].transcript``. A message with no alternatives —
    Metadata, SpeechStarted, an empty interim — yields an empty ``text``, never
    a KeyError.
    """
    kind = str(message.get("type") or "")
    alts = (((message.get("channel") or {}).get("alternatives")) or [{}])
    first = alts[0] if isinstance(alts[0], dict) else {}
    return {
        "type": kind,
        "text": str(first.get("transcript") or ""),
        "confidence": first.get("confidence"),
        "final": bool(message.get("is_final")),
        # UtteranceEnd is its own message type, and it is the backstop for a
        # speaker whose trailing word never triggered speech_final.
        "speech_final": bool(message.get("speech_final")) or kind == "UtteranceEnd",
    }


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

def speak(text: str, *, model: str = DEFAULT_SPEAK_MODEL,
          container: str = "wav", timeout: int = 30) -> dict:
    """Turn ``text`` into audio bytes. Synchronous, stdlib, one round trip.

    Returns ``{ok, audio, media_type, chars, usd, model, request_id}``. ``chars``
    is Deepgram's OWN count from the ``dg-char-count`` response header where it
    sent one, because that is the number being billed — our len() differs from
    it on anything Deepgram normalises before synthesis, and a ledger that
    disagrees with the invoice is the thing spend.py exists to stop.

    Never raises for an ordinary refusal: a caller mid-conversation needs a
    reason it can render next to the reply, not an exception that loses it.
    """
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "nothing to speak"}
    if len(text) > MAX_SPEAK_CHARS:
        return {"ok": False, "error":
                f"Deepgram caps one /v1/speak request at {MAX_SPEAK_CHARS} "
                f"characters and this is {len(text)} — split it"}
    if model not in SPEAK_MODELS:
        return {"ok": False, "error":
                f"unknown speak model {model!r} — known: {', '.join(SPEAK_MODELS)}"}
    try:
        headers = auth_header()
    except DeepgramError as exc:
        return {"ok": False, "error": str(exc)}

    # The model rides in the QUERY (Deepgram's documented shape) and the text in
    # the BODY. The key is in neither — it is the header above, which is the
    # whole reason this is a POST from the server and not a URL a browser holds.
    query = urllib.parse.urlencode({
        "model": model, "encoding": "linear16", "container": container})
    request = urllib.request.Request(
        f"{SPEAK_URL}?{query}",
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            audio = response.read()
            head = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        return {"ok": False, "status": exc.code,
                "error": _speak_help(exc.code, body)}
    except Exception as exc:  # noqa: BLE001 - a dead network is a sentence, not a trace
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        chars = int(head.get("dg-char-count") or len(text))
    except ValueError:
        chars = len(text)
    rate = USD_PER_1K_CHARS.get(model)
    return {
        "ok": True,
        "audio": audio,
        "media_type": "audio/wav" if container == "wav" else "audio/mpeg",
        "chars": chars,
        "model": head.get("dg-model-name") or model,
        "request_id": head.get("dg-request-id") or "",
        "usd": None if rate is None else round(chars / 1000.0 * rate, 6),
    }


def _speak_help(status: int, body: str) -> str:
    """What a human should DO about each failure. The bare status is useless to
    somebody who just wanted their agent to talk."""
    if status == 401:
        return (f"Deepgram rejected the API key — check {ENV} in the project's "
                f".env; keys come from {KEY_URL}")
    if status == 402 or status == 403:
        return ("Deepgram refused this request — the account is out of credit "
                "or the key lacks usage:write. Check the console at " + KEY_URL)
    if status == 429:
        return "Deepgram rate-limited this request — retry in a moment"
    if status >= 500:
        return f"Deepgram had an internal error ({status}) — retry"
    return f"Deepgram refused the request ({status}): {body}"


def doctor_row() -> dict:
    """The shape ``bgate_core.runtime.doctor`` wants from an adapter it probes."""
    verdict = available()
    return {"name": "deepgram", "available": bool(verdict["available"]),
            "detail": verdict["reason"] or "streaming STT and TTS ready",
            "optional": True}
