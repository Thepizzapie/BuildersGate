"""The one HTTP layer every hosted adapter calls through.

WHY. krea, kie, retrodiffusion, imageto3d, localgen and deepgram each carried a
private ``urlopen`` wrapper, and they disagreed about the things that matter:
kie retried a 429 and honoured Retry-After, nobody else did; kie refused to
retry a 500 on a submit because that is how one job becomes two paid ones,
and the others had no retry at all; each one decided on its own what a 402
meant. A fix to one never reached the rest. This module holds the transport
rules once, and the adapters keep only their vocabulary: which header carries
the key, what each status means for THAT provider.

RETRY RULES, stated once. A 429 is always retryable - the request was refused,
so nothing was created or charged. A 5xx or a dead socket is retried only on
an idempotent call (GET/HEAD) or when the caller passes ``retry_submit=True``
and thereby says the submit is safe to repeat. Retry-After outranks the local
schedule and is capped, because an unchecked header can park an agent.

BILLING IS FLAGGED AT THE SOURCE. ``ProviderError.billing`` is set here from
the status (402) and from the balance phrases the providers actually send, so
the gateway asks the exception rather than grepping its message.
"""
from __future__ import annotations

import collections
import json as _json
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# The balance phrases seen in the wild: kie's 402 sentence, RD's
# "Not enough balance.", openai's insufficient_quota, krea's 402. Narrow on
# purpose - a 422 must never read as a money problem or agents learn to
# provider-hop around real bugs.
BILLING_SIGNS = ("no credit", "insufficient credit", "out of credit",
                 "insufficient_quota", "exceeded your current quota",
                 "not enough balance", "insufficient balance",
                 "insufficient funds", "payment required")

RETRY_AFTER_CAP = 120.0
DEFAULT_BACKOFF = (1.0, 2.0, 4.0)

# Patched by tests that exercise the retry path; nothing else should.
def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class ProviderError(Exception):
    """A provider refused, or could not be reached.

    ``billing`` is the flag the gateway routes on; ``status`` is 0 for a
    transport failure; ``body`` is the first 400 bytes of what came back.
    Every adapter's own error class subclasses this and keeps its name, so
    ``except KreaError`` still works and ``except ProviderError`` now does too.
    """

    def __init__(self, message: str = "", *, provider: str = "",
                 status: int = 0, body: str = "", billing: bool = False):
        super().__init__(message)
        self.provider = provider
        self.status = int(status or 0)
        self.body = body or ""
        self.billing = bool(billing)


class PollUnknown(ProviderError):
    """A job status the adapter does not recognise. :func:`poll` treats it as
    still-running unless told it is fatal."""


def is_billing(status: int, body: str = "") -> bool:
    if int(status or 0) == 402:
        return True
    lowered = (body or "").lower()
    return any(sign in lowered for sign in BILLING_SIGNS)


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)
    url: str = ""

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self, *, provider: str = "") -> Any:
        raw = self.text() or "{}"
        try:
            return _json.loads(raw)
        except ValueError as exc:
            raise ProviderError(
                f"{provider or 'the provider'} returned a non-JSON response: "
                f"{raw[:200]}", provider=provider, status=self.status,
                body=raw[:400]) from exc


class RateGate:
    """A sliding-window request limiter, per process.

    kie's: "20 new requests per 10 seconds", and sliding rather than bucketed
    because 20 at 9.9s plus 20 at 10.1s is forty inside one real window. The
    wait is computed from the OLDEST entry so a caller sleeps exactly until a
    slot frees, and slept outside the lock so it never holds the gate shut.
    """

    def __init__(self, window_s: float, max_calls: int):
        self.window_s = float(window_s)
        self.max_calls = int(max_calls)
        self._lock = threading.Lock()
        self._window: "collections.deque[float]" = collections.deque()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._window and now - self._window[0] >= self.window_s:
                    self._window.popleft()
                if len(self._window) < self.max_calls:
                    self._window.append(now)
                    return
                pause = self.window_s - (now - self._window[0])
            _sleep(max(pause, 0.01))


def _retry_after(headers: Any, attempt: int, backoff: tuple) -> float:
    raw = ""
    try:
        raw = (headers or {}).get("Retry-After", "") or ""
    except Exception:                                            # noqa: BLE001
        raw = ""
    try:
        asked = float(str(raw).strip())
        if asked > 0:
            return min(asked, RETRY_AFTER_CAP)
    except (TypeError, ValueError):
        pass   # an HTTP-date Retry-After: the schedule is a fine answer
    return backoff[min(attempt, len(backoff) - 1)]


def _read_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:400]
    except Exception:                                            # noqa: BLE001
        return ""


def request(method: str, url: str, *, headers: Optional[dict] = None,
            json: Any = None, data: Optional[bytes] = None,
            timeout: float = 60.0, retries: int = 3,
            retry_submit: bool = False, provider: str = "",
            gate: Optional[RateGate] = None,
            backoff: tuple = DEFAULT_BACKOFF) -> Response:
    """One HTTP call with the house retry rules. Raises :class:`ProviderError`.

    ``json`` is encoded as the body with an application/json content type
    unless ``headers`` already names one; ``data`` sends raw bytes as given.
    ``retries`` is the total attempt count. ``provider`` names the adapter in
    errors and on the exception.
    """
    method = (method or "GET").upper()
    hdrs = dict(headers or {})
    body = data
    if json is not None:
        body = _json.dumps(json).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    hdrs.setdefault("Accept", "application/json")
    idempotent = method in ("GET", "HEAD", "OPTIONS") or retry_submit
    attempts = max(1, int(retries))
    what = f"{method} {url}"
    for attempt in range(attempts):
        if gate is not None:
            gate.wait()
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                head = {}
                try:
                    head = {k.lower(): v for k, v in resp.headers.items()}
                except Exception:                                # noqa: BLE001
                    head = {}
                return Response(int(getattr(resp, "status", 200) or 200),
                                raw, head, url)
        except urllib.error.HTTPError as exc:
            detail = _read_body(exc)
            code = int(exc.code)
            last = attempt >= attempts - 1
            retryable = code == 429 or (code >= 500 and idempotent)
            if retryable and not last:
                wait = _retry_after(getattr(exc, "headers", None), attempt,
                                    backoff)
                # Out loud: a call twenty seconds slower than usual is
                # indistinguishable from a hung one to whoever is watching.
                print(f"{provider or 'provider'} HTTP {code} on {what}; "
                      f"waiting {wait:.0f}s (attempt {attempt + 1} of "
                      f"{attempts})", file=sys.stderr)
                _sleep(wait)
                continue
            raise ProviderError(
                f"{provider or 'provider'} HTTP {code} on {what}"
                + (f": {detail}" if detail else ""),
                provider=provider, status=code, body=detail,
                billing=is_billing(code, detail)) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None) or exc
            if idempotent and attempt < attempts - 1:
                _sleep(backoff[min(attempt, len(backoff) - 1)])
                continue
            raise ProviderError(
                f"could not reach {provider or 'the provider'} ({reason})",
                provider=provider, status=0, body=str(reason)) from exc
    raise ProviderError(f"{provider} gave up on {what}", provider=provider)


def poll(fn: Callable[[], Any], *, first: float = 2.0, max_wait: float = 300.0,
         factor: float = 1.25, ceiling: float = 10.0,
         unknown_is_fatal: bool = False, provider: str = "",
         label: str = "job") -> Any:
    """Call ``fn`` until it answers, backing off from ``first`` to ``ceiling``.

    ``fn`` returns None while the job runs and the finished payload when it
    is done; it raises for a dead job. A :class:`PollUnknown` it raises is
    swallowed as "still running" unless ``unknown_is_fatal``. Bounded on
    purpose: a job that never finishes must fail the caller rather than hold
    a seat's agent forever.
    """
    deadline = time.monotonic() + max(1.0, float(max_wait))
    wait = max(0.0, float(first))
    last_unknown: Optional[PollUnknown] = None
    while True:
        try:
            got = fn()
        except PollUnknown as exc:
            if unknown_is_fatal:
                raise
            last_unknown = exc
            got = None
        if got is not None:
            return got
        if time.monotonic() >= deadline:
            break
        _sleep(min(wait, max(0.0, deadline - time.monotonic())))
        wait = min(float(ceiling), max(wait, 0.1) * float(factor))
    tail = f" (last status {last_unknown})" if last_unknown else ""
    raise ProviderError(f"{provider or 'the provider'} {label} did not finish "
                        f"within {max_wait:.0f}s{tail}", provider=provider)


def download(url: str, path: str | Path, *, timeout: float = 300.0,
             headers: Optional[dict] = None, provider: str = "",
             retries: int = 3) -> int:
    """Fetch a finished file to disk. Returns bytes written; empty is an error."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    hdrs = {"Accept": "*/*", **(headers or {})}
    try:
        got = request("GET", str(url), headers=hdrs, timeout=timeout,
                      retries=retries, provider=provider)
    except ProviderError as exc:
        raise ProviderError(f"could not download the finished file: {exc}",
                            provider=provider, status=exc.status,
                            body=exc.body, billing=exc.billing) from exc
    if not got.body:
        raise ProviderError(f"{provider or 'the provider'} returned an empty "
                            "file", provider=provider, status=got.status)
    out.write_bytes(got.body)
    return len(got.body)
