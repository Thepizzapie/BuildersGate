"""The live chat connection, and the ONE place a message is routed.

Sits beside the server like auto-deploy, the follow-up router and the steer pump
— see :mod:`bgate_ui.pump` for the shape those three share. This one does not
use :class:`~bgate_ui.pump.Pump` and the difference is worth stating: those loops
POLL, so "sleep, do a tick, swallow anything that raised" is exactly right. This
one holds a socket open for hours and blocks on it. A poll loop around a blocking
read is a poll loop in name only, and the state a reconnect needs — which attempt
we are on, why the last one died, whether the human asked for it to stop — has
nowhere to live in a stateless tick function.

WHAT IT OWNS

    the socket        one per project, opened on request, reconnected with
                      backoff, never on the request thread
    the state         off / not_configured / connecting / connected /
                      reconnecting / error, each with the reason visible
    the ring buffer   the last few hundred messages, in memory, for the live
                      view. Chat is high-volume and disposable; what a session
                      captures is what persists.
    the routing       and this is the important one — see below

THE ROUTING RULE LIVES IN THE CORE, NOT HERE. :func:`chatfeedback.owner` decides
whether an open feedback session, a live recording, or nobody is capturing, and
this module does what it says. That split is deliberate: the rule is a product
decision that has to be explainable in one sentence in the UI, and a rule
implemented inside a daemon thread is a rule nobody can query. This module's job
is to make sure the answer is asked for ONCE PER MESSAGE and acted on exactly
once, so a message cannot land in two stores.

NOTHING HERE CAN QUEUE WORK. No import of the queue, none of the brainstorm
machinery beyond what ``chatfeedback.stop`` reaches for on its own, and no
dispatch. The furthest a viewer's sentence gets on this thread is a row in a
table a human has to read.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Optional

from bgate_core import chatfeedback, chatlink, feedback, playtest, streamer

# Values of the env kill switch that mean "do not connect at all".
_OFF = ("0", "false", "off", "no")
ENV_VAR = "BGATE_CHAT"

# How long a reader thread waits for its own stop flag before giving up on a
# graceful exit. The socket read timeout is 1s, so this only matters if a read
# has wedged inside the TLS layer.
_JOIN_TIMEOUT = 3.0

# A connection that has failed this many times IN A ROW without ever reaching
# 'connected' stops retrying and reports why. Retrying a misconfiguration
# forever is how a dashboard shows a spinner for an hour instead of an answer.
MAX_COLD_FAILURES = 6


def disabled() -> bool:
    return os.environ.get(ENV_VAR, "1").strip().lower() in _OFF


class ChatLink:
    """One project's live chat connection.

    Every public method is safe to call from a request thread; everything that
    can block happens on the reader thread. The lock covers the small mutable
    bits (state, buffer, counters) and is never held across a socket call — a
    status poll that blocks behind a TLS handshake is a dashboard that hangs
    whenever chat is slow.
    """

    def __init__(self, root: str) -> None:
        self.root = str(root)
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._transport: Optional[chatlink.Transport] = None
        self._buffer: deque = deque(maxlen=chatlink.LIVE_BUFFER)
        self._seq = 0
        self._state = "off"
        self._reason = ""
        self._platform = chatlink.DEFAULT_PLATFORM
        self._channel = ""
        self._anonymous = True
        self._attempt = 0
        self._cold = 0
        self._connected_at = 0.0
        self._last_message_at = 0.0
        self._captured = 0
        self._noted = 0
        # (playtest session id, viewer) -> (notes so far, last note at). In
        # memory rather than counted from the database on every message: this is
        # the check that runs BEFORE the expensive write, so paying for a query
        # to decide whether to skip a query is the wrong way round. A dashboard
        # restart resets it, which is the correct failure — it re-grants an
        # allowance rather than silencing somebody who never spent theirs.
        self._note_rate: dict[tuple, tuple[int, float]] = {}

    # -- state ---------------------------------------------------------------

    def _set(self, state: str, reason: str = "") -> None:
        with self._lock:
            self._state = state
            self._reason = reason

    def status(self) -> dict:
        """Everything the panel renders, in one lock-free-ish read.

        ``reason`` is populated for every state that is not ``connected``, and
        that is the point of the whole shape: "reconnecting" with no reason is
        a spinner, and "reconnecting — the chat server closed the connection,
        next try in 4s" is a thing a human can act on or ignore deliberately.
        """
        with self._lock:
            live = self._state == "connected"
            out = {
                "state": self._state,
                "state_label": chatlink.STATES.get(self._state, self._state),
                "reason": self._reason,
                "platform": self._platform,
                "channel": self._channel,
                "anonymous": self._anonymous,
                "can_post": live and not self._anonymous,
                "attempt": self._attempt,
                "seq": self._seq,
                "connected_for": (round(time.time() - self._connected_at)
                                  if live and self._connected_at else 0),
                "last_message_ago": (round(time.time() - self._last_message_at)
                                     if self._last_message_at else None),
                "captured": self._captured,
                "noted": self._noted,
                "running": bool(self._thread and self._thread.is_alive()),
            }
        return out

    def messages(self, since: int = 0, limit: int = 100) -> dict:
        """The live view's feed, read forward from a sequence cursor.

        A cursor rather than a diff, for the reason ``routes/critique`` gives:
        a consumer that has to compare payloads to work out whether anything is
        new gets it wrong the first time a field's wording changes. The buffer
        is bounded, so a client that has been away longer than
        ``chatlink.LIVE_BUFFER`` messages is told plainly that it missed some
        rather than being handed a silent gap.
        """
        with self._lock:
            items = [m for m in self._buffer if m["seq"] > int(since or 0)]
            oldest = self._buffer[0]["seq"] if self._buffer else 0
            seq = self._seq
        missed = bool(since and oldest and oldest > int(since) + 1)
        return {"messages": items[-max(1, int(limit)):], "seq": seq,
                "missed": missed}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> dict:
        """Connect, or say why not. Idempotent and non-blocking.

        Returns the status immediately: the handshake happens on the reader
        thread, so a channel that is slow to answer does not hold the button
        press. The state moves to ``connecting`` before this returns, which is
        what stops a double-click from spawning two sockets.
        """
        if disabled():
            self._set("error", f"{ENV_VAR} is set to off in this environment, so "
                               "the chat connection will not start")
            return self.status()
        config, why = chatlink.config(self.root, self._platform)
        if config is None:
            self._set("not_configured", why)
            return self.status()
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._channel = config.channel
            self._anonymous = config.anonymous
            self._stop = threading.Event()
            self._attempt = 0
            self._cold = 0
            self._state = "connecting"
            self._reason = ""
            self._thread = threading.Thread(
                target=self._run, args=(config,), daemon=True,
                name=f"bgate-chat-{config.platform.id}")
            self._thread.start()
        return self.status()

    def stop(self) -> dict:
        """Disconnect. The captured feedback is untouched — this is the socket."""
        self._stop.set()
        transport = self._transport
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_JOIN_TIMEOUT)
        with self._lock:
            self._thread = None
            self._transport = None
            self._state = "off"
            self._reason = "disconnected"
        return self.status()

    def say(self, text: str) -> bool:
        """Post to chat. False when anonymous or not connected.

        The ONLY thing this product ever posts is its own session announcements
        (see ``chatfeedback.announcement``). Chat is never echoed back to chat,
        and no model output is ever posted — a model reply going out over the
        dev's own channel is a thing they would have to be asked about first,
        and nothing here asks.
        """
        transport = self._transport
        if transport is None or self._state != "connected":
            return False
        try:
            return bool(transport.send(text))
        except Exception:
            return False

    # -- the reader thread ---------------------------------------------------

    def _run(self, config: chatlink.ChatConfig) -> None:
        while not self._stop.is_set():
            try:
                self._session(config)
            except chatlink.TwitchAuthError as exc:
                # A bad token is not a transient failure and backoff cannot
                # repair it. Stop, and say the sentence the service said.
                self._set("error", str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - every drop lands here
                reason = f"{type(exc).__name__}: {exc}"[:300]
                if self._stop.is_set():
                    break
                with self._lock:
                    self._attempt += 1
                    if not self._connected_at:
                        self._cold += 1
                    attempt = self._attempt
                    cold = self._cold
                if cold >= MAX_COLD_FAILURES:
                    self._set("error",
                              f"gave up after {cold} attempts that never "
                              f"connected — {reason}. Check the channel name "
                              "and try again.")
                    return
                wait = chatlink.backoff_for(attempt - 1)
                self._set("reconnecting",
                          f"{reason} — retrying in {wait:.0f}s "
                          f"(attempt {attempt})")
                # Waiting on the stop event rather than sleeping: a disconnect
                # pressed during a 30-second backoff must take effect now.
                if self._stop.wait(wait):
                    break
        self._set("off", "disconnected")

    def _session(self, config: chatlink.ChatConfig) -> None:
        """One connection, from open until it drops. Raises to trigger backoff."""
        transport = config.platform.connect(config)
        self._transport = transport
        self._set("connecting", "")
        transport.open()
        try:
            while not self._stop.is_set():
                for message in transport.read():
                    self._dispatch(message)
        finally:
            self._transport = None
            try:
                transport.close()
            except Exception:
                pass

    def _dispatch(self, message: chatlink.ChatMessage) -> None:
        """One message off the wire. Control lines first, then routing."""
        control = message.msg_id
        if control == chatlink.TwitchIRC.CONTROL_READY:
            with self._lock:
                self._attempt = 0
                self._cold = 0
                self._connected_at = time.time()
            self._set("connected", "")
            return
        if control == chatlink.TwitchIRC.CONTROL_RECONNECT:
            raise OSError("the chat server asked us to reconnect (maintenance)")
        if control == chatlink.TwitchIRC.CONTROL_CLEARMSG:
            self._retract(msg_id=message.user_id)
            return
        if control == chatlink.TwitchIRC.CONTROL_CLEARCHAT:
            self._retract(user_id=message.user_id)
            return

        with self._lock:
            self._seq += 1
            seq = self._seq
            self._last_message_at = time.time()
            row = {**message.as_dict(), "seq": seq, "captured": ""}
            self._buffer.append(row)

        # ONE decision, ONE destination. `owner` is asked per message rather
        # than cached, because both a recording and a feedback session can start
        # or stop between two messages and a cached answer is how a message
        # lands in the store that just closed.
        try:
            where = chatfeedback.owner(self.root)
        except Exception:
            return
        if where["owner"] == chatfeedback.OWNER_FEEDBACK:
            self._to_feedback(where, message, row)
        elif where["owner"] == chatfeedback.OWNER_PLAYTEST:
            self._to_playtest(where, message, row)
        # OWNER_NONE: it stays in the ring buffer and nowhere else. That is not
        # a dropped message, it is chat.

    def _to_feedback(self, where: dict, message: chatlink.ChatMessage,
                     row: dict) -> None:
        try:
            session = chatfeedback.get(self.root,
                                       int(where["feedback_session_id"]))
            item = chatfeedback.capture(self.root, session, message)
        except Exception:
            return
        if item is None:
            return
        with self._lock:
            self._captured += 1
            row["captured"] = "feedback"
            row["kind"] = item["kind"]
            row["seat"] = item["seat"]

    def _to_playtest(self, where: dict, message: chatlink.ChatMessage,
                     row: dict) -> None:
        """A note on the recording, from somebody who is not in the room.

        Same caps as a feedback session's, applied here too rather than only
        there: the rate limit is a property of "how much of this is one person"
        and it must not evaporate because the destination changed. They are
        counted against the recording, which is the window that exists.
        """
        session_id = int(where["playtest_session_id"])
        if not self._note_allowed(session_id, message):
            return
        try:
            playtest.add_note(self.root, session_id, message.text,
                              ts=message.at or None,
                              source=playtest.CHAT, author=message.author)
        except Exception:
            return
        with self._lock:
            self._noted += 1
            row["captured"] = "playtest"

    # A note is heavier than a feedback item — it writes two rows, links assets
    # and later pulls a video frame — so the filter in front of it is the same
    # one and applied before any of that happens.
    def _note_allowed(self, session_id: int,
                      message: chatlink.ChatMessage) -> bool:
        text = message.text
        marked = any(text.lower().lstrip().startswith(m)
                     for m in chatfeedback.MARKERS)
        if not marked and feedback.is_noise(text):
            return False
        key = (session_id, message.user_id or message.author)
        now = float(message.at or time.time())
        with self._lock:
            seen = self._note_rate
            count, last = seen.get(key, (0, 0.0))
            if count >= chatlink.MAX_PER_AUTHOR:
                return False
            if now - last < chatlink.AUTHOR_COOLDOWN_S:
                return False
            total = sum(c for (sid, _u), (c, _t) in seen.items()
                        if sid == session_id)
            if total >= chatlink.MAX_SESSION_ITEMS:
                return False
            seen[key] = (count + 1, now)
        return True

    def _retract(self, *, msg_id: str = "", user_id: str = "") -> None:
        """A moderator removed it — take it out of whatever captured it.

        Both stores, unconditionally, because the message may have been captured
        under a different owner than the one active now: a viewer banned at
        11:04 for something they said at 10:58 must lose the 10:58 remark too.
        """
        try:
            chatfeedback.retract(self.root, msg_id=msg_id, user_id=user_id)
        except Exception:
            pass
        with self._lock:
            for row in self._buffer:
                if ((msg_id and row.get("msg_id") == msg_id)
                        or (user_id and row.get("user_id") == user_id)):
                    row["text"] = "[removed by a moderator]"
                    row["captured"] = ""


# ---------------------------------------------------------------------------
# One link per project. Same reasoning as bgate_ui.pump's per-root latch: the
# active project can change under a long-lived server, and a single global would
# keep a socket open on the project the user has already left.
# ---------------------------------------------------------------------------

_LINKS: dict[str, ChatLink] = {}
_LINKS_LOCK = threading.Lock()


def link(root: str | os.PathLike[str]) -> ChatLink:
    key = str(root)
    with _LINKS_LOCK:
        got = _LINKS.get(key)
        if got is None:
            got = _LINKS[key] = ChatLink(key)
        return got


def reset(root: Optional[str | os.PathLike[str]] = None) -> None:
    """Forget the link(s). Tests use this; nothing else should."""
    with _LINKS_LOCK:
        keys = [str(root)] if root is not None else list(_LINKS)
        for key in keys:
            got = _LINKS.pop(key, None)
            if got is not None:
                try:
                    got.stop()
                except Exception:
                    pass


def redaction_advice(root: str | os.PathLike[str]) -> dict:
    """SHOULD THE PATH FILTER BE ON? A suggestion, never an action.

    If chat is CONNECTED the dev is almost certainly live, and if they are live
    the dashboard is on camera with their home directory, username and any key
    they have set visible in it. ``bgate_core.streamer`` exists for exactly that
    and it is off by default.

    The connection is checked rather than assumed, because the sentence names it
    as the reason: "chat is connected, so you are probably live" printed on a
    dashboard where chat is not connected is a warning that is simply false, and
    a warning that is false the first time somebody reads it is a warning they
    will not read the second time.

    Surfacing the relationship is obviously right. TURNING IT ON IS NOT OURS TO
    DO: it is a display filter with real costs — paths stop being clickable,
    copy-pasted output stops matching what is on disk — and a product that
    silently changes how the whole dashboard renders because a socket opened is
    a product doing something the human did not ask for. So this returns a
    sentence and a switch to press.
    """
    on = streamer.enabled()
    live = link(root).status().get("state") == "connected"
    return {
        "redaction_on": on,
        "connected": live,
        "advise": live and not on,
        "message": ("" if on or not live else
                    "Chat is connected, so you are probably live — the path "
                    "filter is off, which means your home directory, username "
                    "and hostname are on screen. Privacy → streamer mode turns "
                    "it on."),
    }
