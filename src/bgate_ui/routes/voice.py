"""Talking to an agent: a mic relay and a speech endpoint. The key stays here.

THE PROBLEM THIS MODULE IS SHAPED BY. Deepgram's realtime STT is a websocket
that the CLIENT normally opens holding the API key. Doing that from this
dashboard would put DEEPGRAM_API_KEY into page JavaScript, where it is readable
by the devtools console, by any script the page ever loads, and by anything that
can scrape the DOM — the same class of exposure that put a key in a commit once
already and is why bgate_core/runtime/providers.py has no getter.

TWO HONEST OPTIONS. THE RELAY WON.

  RELAY (this module).       The browser streams PCM to a FastAPI websocket on
                             loopback; this process holds the key and relays
                             both ways. The key never leaves the machine, in any
                             form, for any duration.
  SHORT-LIVED TOKEN.         Deepgram documents POST /v1/auth/grant, which mints
                             a JWT with `usage:write` and a ttl_seconds of 1-3600
                             (default 30). The browser would connect straight to
                             Deepgram with it.

The grant is a real, well-designed feature and the reason it lost is not that it
is insecure — it is two facts about THIS product:

1. THE LATENCY ARGUMENT DOES NOT APPLY HERE. A relay costs one extra hop, and
   for a normal web app that hop crosses the internet. This dashboard binds to
   127.0.0.1 and the browser is on the same machine, so the hop is loopback:
   sub-millisecond, against a network leg to Deepgram measured in tens of
   milliseconds. The headline advantage of the token is worth approximately
   nothing at this address.
2. THE LEDGER GOES BLIND WITHOUT IT. bgate_core/board/spend.py is a budget that
   REFUSES, not a report — dispatch consults it before spawning. If the browser
   talks to Deepgram directly, this process never learns how many minutes were
   transcribed and the spend row cannot be written at all. Metered spend that
   the budget cannot see is the failure mode spend.py's own docstring was
   written about. The relay counts the bytes it forwards, so the row is exact.

Against that, `usage:write` is account-wide for its TTL rather than scoped to
one stream, and a socket opened with a 30-second token stays open long after it
expires. Short, but not zero, and bought nothing here.

SO: no endpoint in this file returns, embeds or logs a key, and no key appears in
a URL. deepgram.auth_header() is the only thing that reads one and it is called
inside the two functions that talk to Deepgram.

WEBSOCKETS ARE NOT COVERED BY api.install_guard. That middleware is registered
with @app.middleware("http") and Starlette never runs it for a websocket scope —
so the loopback-Host check, the origin check and the dashboard-token check that
protect every other mutating endpoint DO NOT protect /api/voice/listen. They are
re-done here, by hand, in _accept(). Getting this wrong would mean any page in
the browser could open the user's microphone relay and bill their Deepgram
account, which is worse than the endpoints the guard was written for.

THE TOKEN ARRIVES AS THE FIRST MESSAGE, NOT AS A QUERY PARAMETER. The browser
WebSocket API cannot set request headers, so the two choices are ?token=... and a
handshake frame. A query string lands in uvicorn's access log and in anything
that ever proxies this, which is the rule routes/providers.py already states for
API keys and applies just as well to the credential that guards them.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from typing import Optional

from fastapi import APIRouter, WebSocket
from fastapi.responses import Response

from bgate_adapters import deepgram as _dg
from bgate_core.board import spend
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

# How long the browser has to send its handshake before we hang up. Generous for
# a loopback client and short enough that a socket opened by something that is
# not our page does not sit there holding a task.
HANDSHAKE_TIMEOUT_S = 5.0

# A hard ceiling on one dialogue turn, in bytes of PCM. At 32 KB/s this is ten
# minutes. It exists because the browser end is a `while(listening)` loop and a
# tab left open with a hot mic is an unbounded bill against an account the user
# is not watching — the same reasoning as spend.check, applied where the meter
# actually runs.
MAX_STREAM_BYTES = _dg.BYTES_PER_SECOND * 600

# Deepgram closes an idle socket. Its documented KeepAlive is cheap and does not
# reset the transcript, so the relay sends one whenever the human has been quiet.
KEEPALIVE_EVERY_S = 5.0


# ---------------------------------------------------------------------------
# Status — the endpoint the UI paints its mic control from
# ---------------------------------------------------------------------------

@router.get("/api/voice/status")
def voice_status() -> dict:
    """Can this machine do voice, and if not, exactly what is missing?

    Always 200, never a key, and the reason is a sentence the mic button can put
    in a tooltip. A 503 here would be the truthful status code and the wrong
    product decision: the brainstorm chat's whole no-key story is that typing
    keeps working, and a panel that has to catch an exception to find that out
    tends to render an empty box instead.
    """
    verdict = _dg.available()
    return api.ok({
        "available": bool(verdict["available"]),
        "reason": verdict.get("reason", ""),
        "key": bool(verdict.get("key")),
        "websockets": bool(verdict.get("websockets")),
        "listen_model": _dg.DEFAULT_LISTEN_MODEL,
        "speak_model": _dg.DEFAULT_SPEAK_MODEL,
        "speak_models": list(_dg.SPEAK_MODELS),
        "max_speak_chars": _dg.MAX_SPEAK_CHARS,
        # The audio contract, served rather than duplicated in JavaScript: the
        # browser resamples to exactly this or Deepgram transcribes chipmunks.
        "audio": {"encoding": _dg.ENCODING, "sample_rate": _dg.SAMPLE_RATE,
                  "channels": _dg.CHANNELS},
        "usd_per_minute": _dg.USD_PER_MINUTE.get(_dg.DEFAULT_LISTEN_MODEL),
        "usd_per_1k_chars": _dg.USD_PER_1K_CHARS.get(_dg.DEFAULT_SPEAK_MODEL),
    })


# ---------------------------------------------------------------------------
# TTS — a plain POST, because it is a plain request/response
# ---------------------------------------------------------------------------

@router.post("/api/voice/speak")
def voice_speak(payload: dict) -> Response:
    """Synthesise speech and hand back the audio bytes.

    Answers ``audio/wav`` rather than JSON-with-base64: the browser feeds this
    straight to an <audio> element, and base64 would inflate every reply by a
    third for a round trip that is already the slowest part of the conversation.

    NOT the api.ok envelope on success, for that reason — and deliberately still
    the envelope on failure, so the one thing a caller has to branch on is the
    content type. 503 when voice cannot run at all, 502 when Deepgram refused
    this particular call: the difference between a settings link and a retry.
    """
    body = payload if isinstance(payload, dict) else {}
    text = str(body.get("text") or "")
    if not text.strip():
        raise api.bad_request('send {"text": "..."} — the words to speak')

    # can_speak, not available: TTS is one HTTPS POST and needs the key only.
    # available() also demands the `websockets` package, which nothing on this
    # path imports, so a clean install refused working text-to-speech and told
    # the user to install something it does not use. The listen relay below
    # still asks available(), because that one genuinely needs a socket.
    verdict = _dg.can_speak()
    if not verdict["available"]:
        raise api.unavailable(verdict["reason"], provider="deepgram")

    result = _dg.speak(text, model=str(body.get("model")
                                       or _dg.DEFAULT_SPEAK_MODEL))
    if not result.get("ok"):
        raise api.ApiError(502, str(result.get("error") or "speech failed"),
                           code="speech_failed")

    _record(kind="speech", usd=result.get("usd"),
            model=result.get("model", ""),
            detail=f"deepgram tts {result.get('chars', 0)} chars")
    return Response(
        content=result["audio"],
        media_type=result.get("media_type", "audio/wav"),
        # The cost of the call, on the response, so the UI can show it without a
        # second request. Header rather than body because the body is audio.
        headers={
            "X-Voice-Chars": str(result.get("chars", 0)),
            "X-Voice-Usd": ("" if result.get("usd") is None
                            else f"{result['usd']:.6f}"),
            "X-Voice-Model": str(result.get("model", "")),
            "Cache-Control": "no-store",
        })


# ---------------------------------------------------------------------------
# STT — the relay
# ---------------------------------------------------------------------------

async def _accept(socket: WebSocket) -> Optional[dict]:
    """Do by hand every check api.install_guard does for HTTP, then handshake.

    Returns the handshake dict, or None if the socket was refused and closed.
    The refusal is always sent as a JSON frame BEFORE the close: a websocket
    close code is nearly unreadable from page JavaScript (the browser collapses
    most of them to 1006 with an empty reason), so a socket that closes silently
    gives the mic button nothing to say.
    """
    host = (socket.headers.get("host") or "").strip().lower()
    if host and host.rsplit(":", 1)[0].strip("[]") not in api._LOOPBACK_HOSTS:
        await socket.close(code=1008)
        return None
    origin = socket.headers.get("origin")
    if origin and host and not origin.endswith(f"//{host}"):
        await socket.close(code=1008)
        return None

    await socket.accept()

    try:
        raw = await asyncio.wait_for(socket.receive_text(), HANDSHAKE_TIMEOUT_S)
        hello = json.loads(raw)
        if not isinstance(hello, dict):
            raise ValueError("handshake must be an object")
    except Exception:
        await _say(socket, {"type": "error",
                            "reason": "expected a JSON handshake frame first"})
        await socket.close(code=1008)
        return None

    if not api._auth_disabled():
        try:
            expected = api.ensure_token(root())
        except Exception:
            expected = ""
        if expected and not secrets.compare_digest(
                str(hello.get("token") or ""), expected):
            await _say(socket, {"type": "error",
                                "reason": "stale dashboard token — reload the page"})
            await socket.close(code=1008)
            return None
    return hello


async def _say(socket: WebSocket, message: dict) -> None:
    """Send one JSON frame, and never let a dead socket raise into the relay."""
    with contextlib.suppress(Exception):
        await socket.send_text(json.dumps(message))


@router.websocket("/api/voice/listen")
async def voice_listen(socket: WebSocket) -> None:
    """Relay the browser's microphone to Deepgram and the transcript back.

    Wire protocol, browser side:

        ->  {"token": "...", "model": "nova-3"}   handshake, first frame
        ->  <binary>                              linear16 16kHz mono PCM
        ->  {"type": "Finalize"}                  flush; I stopped talking
        ->  {"type": "CloseStream"}               done, settle up

        <-  {"type": "ready", ...}                Deepgram is connected
        <-  {"type": "Results", text, final, speech_final, ...}
        <-  {"type": "UtteranceEnd", ...}
        <-  {"type": "closed", seconds, usd}      the bill for this turn
        <-  {"type": "error", reason}             anything that went wrong

    ``final`` vs ``speech_final`` is the distinction the UI turns on — see the
    deepgram adapter's docstring. Nothing here decides what becomes a chat
    message; the relay reports and the workspace chooses.
    """
    hello = await _accept(socket)
    if hello is None:
        return

    verdict = _dg.available()
    if not verdict["available"]:
        # The degradation path, and the one this machine can actually exercise.
        # A 200-shaped refusal rather than a dropped connection, for the same
        # reason the brainstorm chat answers 200 with reply:null when there is
        # no OPENAI_API_KEY: the human's other affordances still work and they
        # need to be told which one did not.
        await _say(socket, {"type": "unavailable",
                            "reason": verdict["reason"]})
        await socket.close(code=1000)
        return

    model = str(hello.get("model") or _dg.DEFAULT_LISTEN_MODEL)
    try:
        upstream = await _dg.open_listen_socket(model=model)
    except _dg.DeepgramError as exc:
        await _say(socket, {"type": "unavailable", "reason": str(exc)})
        await socket.close(code=1000)
        return
    except Exception as exc:  # noqa: BLE001 - a refused connect is a sentence
        await _say(socket, {"type": "error",
                            "reason": f"could not reach Deepgram: "
                                      f"{type(exc).__name__}: {exc}"})
        await socket.close(code=1011)
        return

    await _say(socket, {"type": "ready", "model": model,
                        "sample_rate": _dg.SAMPLE_RATE,
                        "encoding": _dg.ENCODING})

    sent = {"bytes": 0}
    try:
        # Two pumps, and the first to finish ends the turn. Anything else leaves
        # a half-open relay: a browser that navigated away with Deepgram still
        # billing, or a Deepgram socket that errored with the mic light still on.
        up = asyncio.create_task(_pump_up(socket, upstream, sent))
        down = asyncio.create_task(_pump_down(socket, upstream))
        done, pending = await asyncio.wait({up, down},
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            with contextlib.suppress(Exception):
                task.result()
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        cost = _dg.stream_cost(sent["bytes"], model=model)
        _record(kind="speech", usd=cost["usd"], model=model,
                detail=f"deepgram stt {cost['seconds']:.1f}s")
        await _say(socket, {"type": "closed", **cost})
        with contextlib.suppress(Exception):
            await socket.close(code=1000)


async def _pump_up(socket: WebSocket, upstream, sent: dict) -> None:
    """Browser -> Deepgram. Audio as binary, control frames as text.

    BACKPRESSURE IS THE `await upstream.send(...)`. websockets' send only
    returns once the frame is handed to the transport, so a slow upstream stops
    this coroutine, which stops us reading the browser socket, which fills the
    browser's own send buffer and is visible to the page as bufferedAmount. The
    alternative — spawning a send task per chunk — would queue microphone audio
    in memory without limit while pretending everything was fine.
    """
    last_audio = time.monotonic()
    while True:
        event = await socket.receive()
        kind = event.get("type")
        if kind == "websocket.disconnect":
            with contextlib.suppress(Exception):
                await upstream.send(_dg.CLOSE_STREAM)
            return

        chunk = event.get("bytes")
        if chunk:
            sent["bytes"] += len(chunk)
            if sent["bytes"] > MAX_STREAM_BYTES:
                await _say(socket, {
                    "type": "error",
                    "reason": f"this turn hit the "
                              f"{MAX_STREAM_BYTES // _dg.BYTES_PER_SECOND // 60}"
                              f"-minute cap — release the mic and start again"})
                with contextlib.suppress(Exception):
                    await upstream.send(_dg.CLOSE_STREAM)
                return
            await upstream.send(chunk)
            last_audio = time.monotonic()
            continue

        text = event.get("text")
        if text:
            # Only the three control messages Deepgram documents get through.
            # Forwarding arbitrary client JSON would let the page reconfigure a
            # stream the server is paying for.
            try:
                want = str((json.loads(text) or {}).get("type") or "")
            except Exception:
                continue
            if want == "CloseStream":
                with contextlib.suppress(Exception):
                    await upstream.send(_dg.CLOSE_STREAM)
                return
            if want == "Finalize":
                await upstream.send(_dg.FINALIZE)
            elif want == "KeepAlive":
                await upstream.send(_dg.KEEP_ALIVE)
            continue

        # A quiet mic still has to hold the socket open — Deepgram drops an idle
        # connection and the human would find out by their next sentence
        # vanishing.
        if time.monotonic() - last_audio > KEEPALIVE_EVERY_S:
            await upstream.send(_dg.KEEP_ALIVE)
            last_audio = time.monotonic()


async def _pump_down(socket: WebSocket, upstream) -> None:
    """Deepgram -> browser, normalised through deepgram.read_transcript.

    The raw message rides along under ``raw`` for Metadata and anything Deepgram
    adds later, but the four fields the UI acts on are lifted to the top level
    so the page never reaches into channel.alternatives[0] itself.
    """
    async for message in upstream:
        if isinstance(message, (bytes, bytearray)):
            continue  # the listen socket is JSON-only; ignore, do not crash
        try:
            parsed = json.loads(message)
        except Exception:
            continue
        shaped = _dg.read_transcript(parsed)
        if shaped["type"] == "Metadata":
            await _say(socket, {"type": "Metadata",
                                "duration": parsed.get("duration")})
            continue
        # An interim with no words is Deepgram breathing, not a transcript.
        if not shaped["text"] and shaped["type"] == "Results":
            continue
        await _say(socket, shaped)


def _record(*, kind: str, usd, model: str, detail: str) -> None:
    """One ledger row. Best effort, exactly like spend.record itself.

    ``usd`` is None for a model this adapter has never been told the price of.
    It is passed through as 0.0 and spend.record's own zero-guard then drops the
    row — deliberately, and the detail string says so, because the alternative
    is inventing a number and every budget check in this product reads a number
    as permission to spend it (krea.TRAIN_USD set that precedent). A zero-second
    stream is dropped by the same guard, which is correct: nobody spoke.
    """
    with contextlib.suppress(Exception):
        spend.record(root(), float(usd or 0.0), kind=kind, model=model,
                     detail=detail if usd is not None
                     else f"{detail} (unpriced model — no ledger row)")
