"""The HTTP contract every route module shares.

The QA audit's loudest cross-cutting finding: two mutually exclusive error
conventions coexisted (FastAPI's ``{detail}`` at 4xx and ``200 {ok: false,
error}``), so the frontend gave up and wrapped every fetch in
``.catch(() => ({}))`` — which does not even fire on a 500, because a 500 body
is still valid JSON. Every failure in the product rendered as a blank panel.

One envelope, always::

    {"ok": true,  "data": <payload>}
    {"ok": false, "error": {"code": "not_found", "message": "...", "detail": {...}}}

``code`` is machine-readable and stable; ``message`` is a sentence a human can
act on. Handlers raise :class:`ApiError` (or any HTTPException — it is coerced)
and never hand-roll an error body.

Also here because every router needs it and nobody should re-derive it:
pagination, the actor identity that makes reviews accountable, and the
same-origin + bearer-token guard on the mutating surface.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from fastapi import Query, Request
from fastapi.responses import JSONResponse

from bgate_core import activity as _activity

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

# Stable codes. The UI switches on these; keep them and add rather than rename.
CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "too_large",
    415: "unsupported_media",
    422: "unprocessable",
    423: "locked",
    429: "rate_limited",
    500: "internal",
    503: "unavailable",
    504: "timeout",
}


class ApiError(Exception):
    """An error with a machine-readable code and a message worth showing.

    ``detail`` carries structured context the UI can render — the conflicting
    lock's owner, the budget that was exceeded, the field that failed.
    """

    def __init__(self, status: int, message: str, *,
                 code: Optional[str] = None,
                 detail: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or CODES.get(status, "error")
        self.detail = detail or {}


# Shorthands. These read better at the call site than ApiError(404, ...).
def bad_request(msg: str, **detail: Any) -> ApiError:
    return ApiError(400, msg, detail=detail or None)


def not_found(msg: str, **detail: Any) -> ApiError:
    return ApiError(404, msg, detail=detail or None)


def conflict(msg: str, **detail: Any) -> ApiError:
    return ApiError(409, msg, detail=detail or None)


def forbidden(msg: str, **detail: Any) -> ApiError:
    return ApiError(403, msg, detail=detail or None)


def locked(msg: str, **detail: Any) -> ApiError:
    """423 — a seat holds this path. Distinct from 403 on purpose: the caller is
    allowed to do this, just not right now, and the UI offers `force` for it."""
    return ApiError(423, msg, detail=detail or None)


def unavailable(msg: str, **detail: Any) -> ApiError:
    return ApiError(503, msg, detail=detail or None)


def ok(data: Any = None, **extra: Any) -> dict:
    """Success envelope. ``extra`` lands beside ``data`` (page metadata, etc)."""
    body: dict = {"ok": True, "data": data}
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# Turning an exception into something safe to put in a response
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. Every route in this product reports a failure as
# f"{type(exc).__name__}: {exc}", including the app-wide handler below, and an
# exception message is not a curated string: an OSError carries the ABSOLUTE
# path that failed (so, the home directory and the account name), a provider
# error carries whatever that vendor put in its response body, and a stray
# ValueError from a library carries whatever it was holding. CodeQL flags the
# whole family as information exposure and it is right that the class is real,
# even though this server binds loopback and sits behind a bearer token.
#
# THE MACHINERY ALREADY EXISTED AND WAS ONLY HALF-WIRED. bgate_core.streamer
# knows how to strip exactly this — API keys by value and by vendor shape, the
# home directory, the account name, the hostname — and bgate_ui.redact installs
# it over every JSON response. But only in STREAMER MODE, which is off by
# default. A key in an error payload is a leak whether or not somebody is
# streaming, so this runs always and the middleware stays what it is: the
# stronger, opt-in filter for when a camera is pointed at the screen.
#
# WHAT IT DOES NOT DO is swallow the sentence. The exception TYPE is kept
# because it is diagnostic and carries nothing personal, and our own error
# messages (CinematicError, KieError, MusicError) are hand-written and
# repo-relative, so they pass through untouched. What changes is only the
# accidental content of an unexpected exception, which is the part nobody wrote
# and nobody checked.
_REDACTOR_TTL_S = 30.0
_redactor_cache: tuple[float, Any] = (0.0, None)


def _redactor() -> Any:
    """A scrubber for this machine, rebuilt occasionally.

    Cached because building one scans the environment for secrets and this sits
    on an error path that a retry loop can hit hard; rebuilt on a timer because
    a key set through the settings panel must start being redacted without a
    restart. Never raises: a failure to build a redactor must not turn a handled
    error into an unhandled one, so the caller falls back to the raw string.
    """
    global _redactor_cache
    import time as _time

    now = _time.monotonic()
    if now - _redactor_cache[0] < _REDACTOR_TTL_S and _redactor_cache[1]:
        return _redactor_cache[1]
    try:
        from bgate_core import streamer as _streamer

        built = _streamer.Redactor(scan_env=True)
    except Exception:                                            # noqa: BLE001
        built = None
    _redactor_cache = (now, built)
    return built


# WHOSE MESSAGE IS IT. Our own exception types carry messages a person in this
# repo WROTE, reviewed, and kept repo-relative on purpose — "nothing on disk at
# game/assets/cinematics/intro.ogv" is the whole value of the error and there is
# nothing in it to leak. A foreign exception's message is not ours: an OSError
# names the absolute path that failed, a provider error carries that vendor's
# response body, and a library's ValueError carries whatever it was holding.
#
# Scrubbing helps and is not sufficient, because the scrubber can only remove
# what it can RECOGNISE — this machine's identity and secret-shaped strings. It
# cannot know that some third party's error text is quoting a filename from
# somewhere else. So a foreign message does not go out at all; it is logged
# where an operator can read it and replaced with its type, which is the part
# that is diagnostic and carries nothing.
_OURS = ("bgate_core", "bgate_adapters", "bgate_ui", "bgate_mcp", "builtins")


def _is_ours(exc: BaseException) -> bool:
    # OSError IS THE EXCEPTION TO THE EXCEPTION, and it is not a nicety: its
    # whole contract is to name the FILE that failed, so `strerror: filename` is
    # an absolute path by construction — which is the single thing this function
    # exists to keep out of a response. Caught here rather than trusted to the
    # scrubber, which substitutes the home directory it recognises and leaves
    # the rest of the tree standing ("/home/<user>/private/notes.txt" still
    # says `private/notes.txt`).
    if isinstance(exc, OSError):
        return False
    module = (type(exc).__module__ or "").split(".")[0]
    # `builtins` otherwise counts as ours, for ValueError/KeyError raised BY our
    # own code with our own message — the common case for a deliberate refusal.
    # The cost is passing a builtin raised by a library; those messages are
    # short and rarely carry paths, and excluding them would blank out most of
    # the useful refusals in the product.
    return module in _OURS


def safe_error(exc: BaseException) -> str:
    """What a route may put in a response body for this exception.

    Ours: the message, scrubbed of secrets and this machine's identity.
    Anything else: the type, with the detail logged rather than served.
    """
    filt = _redactor()

    def scrub(text: str) -> str:
        if filt is None:
            return ""
        try:
            return filt.text(text)
        except Exception:                                        # noqa: BLE001
            return ""

    if _is_ours(exc):
        scrubbed = scrub(f"{type(exc).__name__}: {exc}")
        # An empty scrub means no scrubber; the type alone is thin but safe.
        return scrubbed or type(exc).__name__

    # FOREIGN, AND DELIBERATELY NOT LOGGED HERE. An earlier version of this
    # printed the scrubbed message so an operator could still diagnose it, and
    # that traded one finding for a worse one: writing a possibly-secret-bearing
    # string to stdout is clear-text logging of sensitive information, which is
    # a HIGH, where the thing it was helping with is a MEDIUM. The exception is
    # not swallowed — it still propagates to whatever raised it and to the
    # server's own stderr through normal traceback handling; this function's job
    # is only to decide what goes in the RESPONSE.
    return (f"{type(exc).__name__} — unexpected failure. The message is not "
            "shown here because it came from outside this project and may name "
            "paths or values that are not ours to repeat.")


def error_body(status: int, message: str, *, code: Optional[str] = None,
               detail: Optional[dict] = None) -> dict:
    return {
        "ok": False,
        "error": {
            "code": code or CODES.get(status, "error"),
            "message": message,
            "detail": detail or {},
        },
    }


def install_error_handlers(app) -> None:
    """Coerce every failure — ApiError, HTTPException, and the unexpected — into
    the one envelope. Without the bare-Exception handler a stray KeyError still
    escapes as an HTML traceback the UI cannot parse."""
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status,
            content=error_body(exc.status, exc.message, code=exc.code,
                               detail=exc.detail),
        )

    # Starlette's HTTPException, not FastAPI's: an unmatched route raises the
    # base class, so registering only the subclass leaves every 404 and 405 in
    # the old {detail} shape — the exact inconsistency this module exists to end.
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        # Legacy raises across app.py pass through here and come out shaped
        # like everything else, so the frontend needs exactly one code path.
        detail = exc.detail
        message = detail if isinstance(detail, str) else "request failed"
        extra = None if isinstance(detail, str) else {"detail": detail}
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.status_code, message, detail=extra),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{loc or 'request'}: {first.get('msg', 'invalid')}"
        return JSONResponse(
            status_code=422,
            content=error_body(422, message, detail={"errors": _jsonable(errors)}),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        # THE WIDEST EXPOSURE IN THE PRODUCT, because it catches what nobody
        # anticipated — and an unanticipated exception is exactly the one whose
        # message was never read by a human. See safe_error.
        return JSONResponse(
            status_code=500,
            content=error_body(500, safe_error(exc)),
        )


def _jsonable(errors: Sequence[dict]) -> list[dict]:
    """Pydantic v2 stuffs the offending exception object into ``ctx``, which is
    not JSON-serialisable — stringify anything that is not a primitive."""
    out = []
    for err in errors:
        out.append({k: (v if isinstance(v, (str, int, float, bool, type(None), list))
                        else str(v))
                    for k, v in err.items()})
    return out


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


class Page:
    """Validated limit/offset. Every list endpoint takes one.

    Unbounded list endpoints were silently truncating in the UI with no count
    and no 'load more', so a project past a few hundred rows quietly stopped
    showing its own data.
    """

    def __init__(self, limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                 offset: int = Query(0, ge=0)) -> None:
        self.limit = min(int(limit), MAX_LIMIT)
        self.offset = max(0, int(offset))

    def slice(self, rows: Sequence) -> list:
        return list(rows[self.offset:self.offset + self.limit])

    def envelope(self, items: Iterable, total: int) -> dict:
        items = list(items)
        nxt = self.offset + len(items)
        return ok(items, page={
            "limit": self.limit,
            "offset": self.offset,
            "total": int(total),
            "next_offset": nxt if nxt < total else None,
        })

    def apply(self, rows: Sequence) -> dict:
        """Slice an already-materialised list and wrap it. For endpoints backed
        by a COUNT query, prefer ``envelope`` with the real total."""
        return self.envelope(self.slice(rows), len(rows))


# ---------------------------------------------------------------------------
# Actor identity
# ---------------------------------------------------------------------------

# Re-exported, not re-declared. The prefix, the identity fallback and the
# human/agent predicate below all live in bgate_core.activity, which is the layer
# the MCP server, the hook and the CLI also go through — a second copy here is a
# second thing to keep in step, and "only a human may approve" is not a rule that
# survives two definitions of "human".
AGENT_PREFIX = _activity.AGENT_PREFIX


def current_actor(request: Optional[Request] = None) -> str:
    """Who is responsible for this call.

    An agent's spawned session carries BGATE_ACTOR=agent:item-<id> in its env;
    anything else is a human at the dashboard. This is what makes 'approved'
    mean something — see :func:`is_human`.
    """
    env = os.environ.get("BGATE_ACTOR", "").strip()
    if env:
        return env

    # Fail closed: infer an agent from the environment dispatch actually sets.
    #
    # BGATE_ACTOR is the explicit stamp, but it is one line in one spawn path,
    # and this gate is only worth having if forgetting that line cannot silently
    # disable it. It was in fact forgotten — a dispatched agent resolved to the
    # machine's human identity and could approve its own art, which is the exact
    # thing the human-only rule exists to prevent. BGATE_WORK_ITEM/BGATE_SEAT are
    # set by every spawn because the hook needs them, so they are the honest
    # signal that nobody can forget without breaking enforcement outright.
    #
    # A human who exports BGATE_SEAT in their own shell is read as an agent and
    # loses the ability to approve. That is the safe direction to be wrong in.
    item = os.environ.get("BGATE_WORK_ITEM", "").strip()
    if item:
        return f"{AGENT_PREFIX}item-{item}"
    seat = os.environ.get("BGATE_SEAT", "").strip()
    if seat:
        return f"{AGENT_PREFIX}seat-{seat}"

    if request is not None:
        header = (request.headers.get("x-bgate-actor") or "").strip()
        if header and not header.startswith(AGENT_PREFIX):
            return header[:120]
    return local_identity()


local_identity = _activity.local_identity
is_human = _activity.is_human


def require_human(actor: str, action: str = "approve") -> None:
    if not is_human(actor):
        raise forbidden(
            f"{action} requires a human — {actor or 'an unidentified caller'} is an agent",
            actor=actor, action=action)


# ---------------------------------------------------------------------------
# Auth: same-origin + a per-run bearer token
# ---------------------------------------------------------------------------

TOKEN_FILENAME = "ui-token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# The only names this dashboard will answer to. Anything else in the Host header
# means the client did not type "localhost" -- it resolved some other name to
# this machine, which is the shape of a DNS rebinding attack. Kept as a set of
# HOSTNAMES, port stripped by the caller, so a user who runs on a non-default
# port does not have to be enumerated here.
#
# "testserver" is Starlette's in-process TestClient default. It is on the list
# deliberately and it is not a hole: an attacker's page has to reach this
# process through a browser, which means resolving a name through DNS, and
# "testserver" is not a registrable public name -- nobody can make a browser
# send it. Leaving it off instead would have meant the suite could only run with
# the gate disabled, and a security control the tests never exercise is one that
# breaks silently.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "testserver"}
# Everything the browser needs before it can present a token.
_OPEN_PATHS = ("/static/", "/play/", "/api/preview", "/favicon")


def token_path(root: Path) -> Path:
    return Path(root) / ".bgate" / TOKEN_FILENAME


def ensure_token(root: Path) -> str:
    """Read (or mint) this project's dashboard token.

    Written 0600 into .bgate/ — the same directory the DB lives in, which is
    already gitignored, so the token never travels with the game repo.
    """
    path = token_path(root)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best effort; Windows ACLs do not map cleanly
    return token


def _auth_disabled() -> bool:
    return os.environ.get("BGATE_NO_AUTH", "").strip().lower() in {
        "1", "true", "yes", "on"}


def dispatch_enabled() -> bool:
    """A viewer-only deployment must not be able to spawn agents."""
    return os.environ.get("BGATE_ALLOW_DISPATCH", "1").strip().lower() not in {
        "0", "false", "no", "off"}


def install_guard(app, root_fn) -> None:
    """Reject cross-origin and unauthenticated mutations.

    The dashboard binds to 127.0.0.1, which is not a security boundary: any page
    in the browser can POST to localhost. Two cheap gates close it — the request
    must be same-origin, and it must carry the token only something with read
    access to .bgate/ could know.

    Opt out with BGATE_NO_AUTH=1 for a scripted/CI run.
    """

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        # THE HOST GATE COMES FIRST, AND IT IS NOT OPTIONAL -- not even under
        # BGATE_NO_AUTH, because the whole point is that it closes a hole the
        # other two checks cannot see.
        #
        # DNS REBINDING. Every other gate here reasons about ORIGIN RELATIVE TO
        # HOST: `sec-fetch-site: same-origin` and `origin == host` both compare
        # the request against whatever Host it happens to carry. An attacker
        # page on evil.com:7788 that rebinds its own DNS to 127.0.0.1 satisfies
        # both -- the browser genuinely believes it is same-origin, so it will
        # also let the page READ the response. From there it fetches `/`, which
        # is a safe method and therefore exempt, scrapes window.BGATE_TOKEN out
        # of the HTML, and owns the entire mutating surface. That surface
        # includes POST /api/godot/run, which executes arbitrary GDScript, which
        # is OS.execute(), which is a shell as the desktop user.
        #
        # Binding to 127.0.0.1 does not help: the browser is on the machine. The
        # fix is to check the name the client ASKED FOR, which a rebinding
        # attack cannot forge without giving up the same-origin illusion it
        # depends on.
        host = (request.headers.get("host") or "").strip().lower()
        if host and host.rsplit(":", 1)[0].strip("[]") not in _LOOPBACK_HOSTS:
            return JSONResponse(status_code=403, content=error_body(
                403, "request Host is not loopback", code="bad_host"))

        # Read the opt-out per request, not once at install time: the app is
        # imported when a test module is first collected, which is before any
        # fixture has had a chance to set the env var. Latching it here made the
        # guard un-disableable from a fixture and 401'd unrelated tests.
        if _auth_disabled() or request.method in _SAFE_METHODS:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in _OPEN_PATHS):
            return await call_next(request)

        site = request.headers.get("sec-fetch-site")
        if site and site not in {"same-origin", "none"}:
            return JSONResponse(status_code=403, content=error_body(
                403, "cross-origin mutation refused", code="cross_origin"))

        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            if host and not origin.endswith(f"//{host}"):
                return JSONResponse(status_code=403, content=error_body(
                    403, f"origin {origin} is not this dashboard",
                    code="cross_origin"))

        try:
            expected = ensure_token(root_fn())
        except Exception:
            expected = ""  # no project yet: first-run must still be reachable
        if expected:
            presented = (request.headers.get("x-bgate-token")
                         or (request.headers.get("authorization", "")
                             .removeprefix("Bearer ").strip()))
            if not secrets.compare_digest(presented or "", expected):
                return JSONResponse(status_code=401, content=error_body(
                    401, "missing or stale dashboard token — reload the page",
                    code="unauthorized"))

        return await call_next(request)
