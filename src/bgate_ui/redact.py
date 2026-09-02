"""Streamer mode, installed on the dashboard as one middleware.

`bgate serve` is the surface a stream actually points at, so this is where the
filter has to bite. It is one ASGI middleware rather than a call in each of the
~40 route modules, for the reason the token shim is one fetch wrapper and not
200 edits: a per-route filter is a filter with holes in it, and the hole is
always the route somebody added last.

WHAT IT TOUCHES, and nothing else:

  JSON out          scrubbed, keys and values, however deep.
  SSE out           scrubbed per event, because the live log is exactly the
                    thing being read on camera.
  text/plain out    scrubbed.
  JSON bodies IN    RESTORED. The page round-trips paths — it renders one and
                    posts it back — so a scrub with no inverse turns the
                    dashboard's buttons into 404s.

  HTML              LEFT ALONE, deliberately. index.html carries the dashboard
                    token in a <script> tag, `window.BGATE_TOKEN='...'` matches
                    the assignment pattern exactly, and redacting it logs the
                    browser out of its own dashboard. The token is a live
                    secret, and if the stream shows devtools it is exposed —
                    that is a real limitation and it is written down in
                    `status()` rather than papered over.

  images, video,    LEFT ALONE. They are bytes; running a text filter over a
  binary            PNG corrupts it, and a scrubber that breaks the asset
                    previews is a scrubber that gets turned off. A path
                    rendered INTO an image is not something this can reach.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional

from starlette.responses import Response
from starlette.types import ASGIApp

from bgate_core.board import streamer

# Only these are text we own the shape of. text/html is missing on purpose;
# see the module docstring.
_SCRUB_TYPES = ("application/json", "text/event-stream", "text/plain",
                "application/x-ndjson")

# Two bodies must never be rewritten even though they are JSON, because the
# browser compares them against what it sent and a substituted value fails the
# check. Neither carries user data.
_SKIP_PATHS = ("/api/hello", "/api/token")


def _is_scrubbable(content_type: str) -> bool:
    head = (content_type or "").split(";")[0].strip().lower()
    return head in _SCRUB_TYPES


def install(app: ASGIApp, root_fn: Callable[[], object]) -> None:
    """Add the filter to a FastAPI app.

    root_fn is the same accessor the auth guard uses, called per request rather
    than captured — the dashboard learns which project it is serving after
    startup, and a redactor built at import time knows no project path, which
    is the one path most worth hiding.
    """

    @app.middleware("http")
    async def _streamer_filter(request, call_next):  # type: ignore[no-untyped-def]
        roots = _roots(root_fn)
        filt = streamer.active(roots, force=_resolve(roots))
        if filt is None:
            return await call_next(request)

        # INBOUND FIRST. A restored body has to reach the route, so this
        # happens before call_next, not after.
        if request.url.path not in _SKIP_PATHS:
            await _restore_body(request, filt)

        response = await call_next(request)
        if request.url.path in _SKIP_PATHS:
            return response
        if not _is_scrubbable(response.headers.get("content-type", "")):
            return response
        return await _scrub_response(response, filt)


def _roots(root_fn: Callable[[], object]) -> list[str]:
    try:
        root = root_fn()
    except Exception:
        return []
    return [str(root)] if root else []


# The settings registry resolves env > stored > default, so asking it is how the
# panel toggle and BGATE_STREAMER end up meaning the same thing. Cached for a
# beat because this runs on EVERY request and the registry is a file read — but
# only for a beat, because the whole point of a toggle is that it takes effect
# while you are looking at the screen, not after a restart.
_TTL_S = 2.0
_cache: tuple[float, bool] = (0.0, False)


def _resolve(roots: list[str]) -> bool:
    """Is the filter on? Panel switch, env var, or default — in that order.

    FAILS ON, not off. If the registry cannot be read — no project yet, a
    migration mid-flight, a corrupt file — the answer is whatever the env var
    says and otherwise off; but a registry that says ON and then throws on the
    next read keeps the last known ON rather than silently reverting, because
    the cost of the two mistakes is not the same.
    """
    global _cache
    now = time.monotonic()
    if now - _cache[0] < _TTL_S:
        return _cache[1]
    value = streamer.enabled()
    if roots:
        try:
            from bgate_core.store import settings as _settings
            value = bool(_settings.get(roots[0], "privacy.streamer"))
        except Exception:
            value = value or _cache[1]
    _cache = (now, value)
    return value


def invalidate() -> None:
    """Drop the cached answer, for the moment the panel writes the switch.

    Without this a user flips the toggle, sees nothing change for two seconds,
    and flips it back — which is the worst possible interaction for a control
    whose job is to make someone feel safe before going live.
    """
    global _cache
    _cache = (0.0, _cache[1])


async def _restore_body(request, filt: streamer.Redactor) -> None:
    """Put real paths back into an inbound JSON body, in place.

    Starlette caches the body on first read, so replacing the cache is enough
    for the route to see the restored version — there is no re-reading the
    stream twice, and a route that reads `await request.json()` gets the real
    path without knowing this happened.
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    head = (request.headers.get("content-type") or "").split(";")[0].strip()
    if head != "application/json":
        return
    try:
        raw = await request.body()
    except Exception:
        return
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return
    restored = json.dumps(filt.restore(payload)).encode("utf-8")
    if restored == raw:
        return
    # Starlette reads the cached body from _body; the receive channel is
    # replaced too because a middleware further in may re-consume it.
    request._body = restored  # noqa: SLF001
    original = request._receive  # noqa: SLF001
    delivered = False

    async def _receive() -> dict:
        """Hand back the restored body ONCE, then get out of the way.

        Returning `http.request` on every call is the obvious version and it
        breaks the server: BaseHTTPMiddleware calls receive() a second time to
        listen for the client disconnect, and a second `http.request` raises
        `RuntimeError: Unexpected message received`. Every JSON POST and PATCH
        500s — which is to say every mutation in the dashboard, while GET
        traffic stays perfectly healthy and the whole thing looks fine.
        Delegating after the first call lets the real disconnect through.
        """
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": restored,
                    "more_body": False}
        return await original()

    request._receive = _receive  # noqa: SLF001


async def _scrub_response(response: Response, filt: streamer.Redactor
                          ) -> Response:
    """Scrub a response body, streaming or not.

    A streaming response is consumed and re-emitted CHUNK BY CHUNK rather than
    joined: /api/events is an open SSE connection that never ends, and reading
    it to completion first would hang the page on a blank screen forever.
    """
    if hasattr(response, "body_iterator"):
        # Per-event flushing is required for SSE and wasteful for everything
        # else. BaseHTTPMiddleware makes every response a streaming one, so a
        # JSON body was scrubbed and re-emitted at every newline it happened to
        # contain — ~0.23s of pure plumbing on a 1.2MB /api/state, on top of the
        # scrub itself.
        #
        # RESPONSE SIDE ONLY. An earlier attempt also rewrote the inbound
        # `receive` channel to restore redacted paths, and that hung every POST
        # in the dashboard — sign-off buttons included. The two halves are
        # independent; this is the half that was never implicated.
        if "text/event-stream" in (response.headers.get("content-type") or ""):
            return _scrub_stream(response, filt)
        return _scrub_whole(response, filt)

    body = getattr(response, "body", b"")
    if not body:
        return response
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return response  # not text after all; leave the bytes alone
    scrubbed = filt.text(text).encode("utf-8")
    response.body = scrubbed
    # Content-Length changes whenever a substitution is a different length than
    # what it replaced, which is almost always. A stale length truncates the
    # body in the browser and the page fails to parse its own JSON.
    response.headers["content-length"] = str(len(scrubbed))
    return response


def _scrub_whole(response, filt: streamer.Redactor):
    """A finite body: drain it, scrub once, emit ONE chunk.

    Swaps `body_iterator` and returns immediately — the same shape as
    _scrub_stream, for the reason its docstring gives. The draining happens
    inside the replacement generator when Starlette pulls on it, never eagerly
    in the middleware, so the response body is still consumed at the moment the
    machinery above expects it to be.
    """
    original = response.body_iterator

    async def _one():
        parts: list[bytes] = []
        async for chunk in original:
            parts.append(chunk if isinstance(chunk, (bytes, bytearray))
                         else str(chunk).encode("utf-8"))
        body = b"".join(parts)
        try:
            yield filt.text(body.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            yield body          # not text after all; leave the bytes alone

    response.body_iterator = _one()
    # Unknowable until the body has been read, and headers go out first.
    if "content-length" in response.headers:
        del response.headers["content-length"]
    return response


def _scrub_stream(response, filt: streamer.Redactor):
    """Filter a streaming body by REPLACING ITS ITERATOR, in place.

    Not by building a new StreamingResponse around it. That is the obvious
    version and it takes the server down: BaseHTTPMiddleware hands whatever you
    return back to Starlette as `await response(scope, wrapped_receive, send)`,
    and a freshly-constructed StreamingResponse starts its own
    `listen_for_disconnect` on a receive channel whose body has already been
    consumed. The second read comes back `http.request` instead of
    `http.disconnect` and Starlette raises `RuntimeError: Unexpected message
    received` — an unhandled ASGI exception on every filtered response.

    Swapping `body_iterator` leaves the response object BaseHTTPMiddleware
    already built, already wired to the right channel, and changes only the
    bytes flowing through it."""
    original = response.body_iterator

    async def _filtered():
        # A UTF-8 character can straddle a chunk boundary, and an SSE event
        # certainly can. Decoding each chunk in isolation would either raise or
        # silently mangle — carry the remainder and only emit complete lines.
        # Buffer as a LIST of pieces, and only look for a line break in the
        # piece that just arrived.
        #
        # This was `carry += piece` followed by `carry.rpartition("\n")` every
        # chunk, which is fine for SSE — every event ends in a newline, so the
        # buffer drains constantly. A JSON body has NO newline in it at all, so
        # nothing ever drained: each chunk re-copied the whole accumulated
        # string and then rescanned it end to end looking for a break that was
        # never coming. Quadratic in the response size. /api/screenmap on a real
        # project is ~600KB in ~150 chunks and it cost FIFTEEN SECONDS — the
        # scrub itself is 0.22s of that. The dashboard polls this endpoint.
        buf: list[str] = []
        async for chunk in original:
            if isinstance(chunk, (bytes, bytearray)):
                try:
                    piece = chunk.decode("utf-8")
                except UnicodeDecodeError:
                    yield chunk  # binary in a text stream: pass it through
                    continue
            else:
                piece = str(chunk)
            buf.append(piece)
            if "\n" not in piece:
                continue  # no complete line yet; keep buffering, cheaply
            head, sep, tail = "".join(buf).rpartition("\n")
            buf = [tail] if tail else []
            yield filt.text(head + sep).encode("utf-8")
        if buf:
            rest = "".join(buf)
            if rest:
                yield filt.text(rest).encode("utf-8")

    # Length is unknowable for a filtered stream and was never set for SSE;
    # leaving a stale one is how the browser decides the stream ended early and
    # parses a truncated fragment.
    if "content-length" in response.headers:
        del response.headers["content-length"]
    response.body_iterator = _filtered()
    return response


def status(root: Optional[object] = None) -> dict:
    """What the indicator in the UI shows. Answers whether it is on or off.

    Resolves the same way the middleware does. A status endpoint that read only
    the env var would report "off" to a user who had just turned the panel
    switch on — and an indicator that disagrees with the filter is worse than
    no indicator, because it is the one thing being trusted before going live.
    """
    roots = [str(root)] if root else []
    on = _resolve(roots)
    filt = streamer.active(roots, force=on)
    if filt is None:
        return {"on": False, "env_var": streamer.ENV_VAR,
                "note": f"paths, identity and keys are shown in full. Turn this "
                        f"on in Settings > Privacy, or set {streamer.ENV_VAR}=1"}
    return {**filt.status(), "env_var": streamer.ENV_VAR,
            "env_forced": streamer.enabled()}
