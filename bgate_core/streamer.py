"""Streamer mode — what the dashboard, the logs and the CLI say on camera.

Builders Gate is a local tool that assumes the only person reading its output is
the person who owns the machine. Every surface reflects that: the dashboard
prints the project's absolute path, agent steps quote the commands they ran,
tracebacks carry file paths, doctor names the interpreter, and an adapter that
fails with a bad key can echo the request it sent. On a private screen that is
all exactly right. Pointed at an audience it is a home directory with a real
name in it, and sometimes a key.

So this is a DISPLAY filter, applied at the boundary where text becomes
something a viewer can read, and nowhere else. Two different jobs, deliberately
not conflated:

  IDENTIFIERS (home, project root, username, host) are SUBSTITUTED, and the
  substitution is reversible by `restore`. They are not secrets, they are
  personally identifying, and the UI round-trips them — a path scrubbed on the
  way out and not restored on the way back in is a broken button, and a broken
  button is how a feature like this gets switched off.

  SECRETS (keys, tokens, passwords, private keys) are DESTROYED. There is no
  reverse map, because a reverse map is the thing itself. If a secret is in a
  string that came out of an adapter, the correct amount of it to keep is none.

WHAT THIS IS NOT: an outbound-egress control or a security boundary. It edits
what is *rendered*. Anything that reads the disk, opens devtools, or looks at
the .env directly still sees everything, because it is all still there. The
dashboard token is a live example and is deliberately left alone — it is handed
to the page in a <script> tag, and redacting it would log the browser out.

FAIL LOUD. `status()` reports what the filter is actually doing, and the
dashboard shows it. A redactor that is quietly off looks exactly like a
redactor that is on and working, right up until it doesn't.
"""
from __future__ import annotations

import os
import re
import socket
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

# The env switch. Read live rather than cached at import, so a session can flip
# it without a restart — a streamer realising mid-stream is the whole use case,
# and "restart the server" is not a thing you can do with an audience watching.
ENV_VAR = "BGATE_STREAMER"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

HOME_TOKEN = "<home>"
PROJECT_TOKEN = "<project>"
USER_TOKEN = "<user>"
HOST_TOKEN = "<host>"
SECRET_TOKEN = "<redacted>"

# Bare-word substitution of the username is the sharpest tool here and the one
# most likely to cut the wrong thing: a username that is also an English word
# would rewrite prose. Short names and common account names are matched only as
# part of a path, never on their own.
_UNSAFE_BARE = {
    "admin", "administrator", "user", "users", "guest", "root", "public",
    "default", "owner", "me", "dev", "test", "build", "runner", "system",
}
_MIN_BARE_LEN = 4


def enabled(default: bool = False) -> bool:
    """Is the filter on right now?

    Unset means off. An unparseable value means ON — the failure mode of
    "BGATE_STREAMER=ture" must be a redacted screen, not a live one.
    """
    raw = os.environ.get(ENV_VAR)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _FALSE:
        return False
    return True


# ---------------------------------------------------------------------------
# Secrets: destroyed, never mapped back
# ---------------------------------------------------------------------------
# Shapes first, because a key in an agent log did not come from this machine's
# environment and there is nothing to compare it against. These are the vendor
# prefixes that are unambiguous enough to match on sight — a false positive here
# costs a viewer some readability and a false negative costs a key, which is not
# a symmetric trade.
_SECRET_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{16,}"),              # Anthropic
    # Hyphen and underscore INSIDE the class: OpenAI's current keys are
    # sk-proj-<blob>, and a class of [A-Za-z0-9] stops at the second hyphen and
    # matches nothing. The tight spelling passed a test written against the old
    # sk-<blob> format and would have printed a live project key on stream.
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}"),                # OpenAI and lookalikes
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                    # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),                    # AWS session key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),            # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),                   # Hugging Face
    # {30,} and no \b terminator, not the {35}\b every blog post publishes:
    # an exact length fails closed the moment a vendor changes a key by one
    # character, and the discriminating part is the AIza prefix, not the count.
    re.compile(r"\bAIza[0-9A-Za-z\-_]{30,}"),               # Google
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),         # Slack
    re.compile(r"\bglpat-[A-Za-z0-9\-_]{16,}"),             # GitLab
    re.compile(r"\bdop_v1_[a-f0-9]{64}\b"),                 # DigitalOcean
    # A JWT is three base64url segments. Session tokens and signed URLs both
    # arrive in this shape and both identify the account that minted them.
    re.compile(r"\beyJ[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}"),
)

# ONE pass, not twelve. Every shape above is flagless, group-free and replaced
# by the same token, so running them as a single ordered alternation is exactly
# equivalent — Python tries alternatives left to right at each position, which
# is the same precedence the sequential loop gave them.
#
# It is not a micro-optimisation. Each `.sub()` walks the WHOLE body, and this
# filter runs on every response: on a 1.4MB /api/state the twelve passes cost
# 0.233s of the 0.650s total. Regex work holds the GIL, so that time does not
# overlap with anything — measured, eight concurrent requests took 8x as long
# as one. That is how 0.65s of CPU became a 16-second page.
# The literal every shape MUST contain to have any chance of matching. All the
# shapes are case-sensitive, so a plain substring test is exact — and a `in`
# test is C-speed substring search, roughly a millisecond over a megabyte,
# against ~20ms for the regex branch it stands in for.
#
# This is the whole optimisation. Merging the shapes into one alternation did
# nothing (0.258s either way) because twelve branches with twelve different
# prefixes cannot be pre-scanned by the engine; it still tries each branch at
# each position. Deciding UP FRONT which branches can fire, and compiling an
# alternation of only those, is what makes it cheap. A /api/screenmap body
# contains none of these prefixes, so its secret pass becomes a handful of
# substring tests instead of a quarter of a second of backtracking.
#
# NECESSARY conditions only: a hint that is present does not mean the shape
# matches, it means we still have to look. That direction is the safe one.
_SHAPE_HINTS: tuple[tuple[str, ...], ...] = (
    ("sk-ant-",),                                   # Anthropic
    ("sk-",),                                       # OpenAI and lookalikes
    ("AKIA",), ("ASIA",),                           # AWS
    ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),       # GitHub
    ("github_pat_",), ("hf_",), ("AIza",),
    ("xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-"),  # Slack
    ("glpat-",), ("dop_v1_",), ("eyJ",),
)
assert len(_SHAPE_HINTS) == len(_SECRET_SHAPES), "a shape lost its hint"


@lru_cache(maxsize=None)
def _shapes_for(live: tuple[int, ...]) -> Optional[re.Pattern[str]]:
    """One alternation of just the shapes whose prefix is actually present."""
    if not live:
        return None
    return re.compile("|".join(f"(?:{_SECRET_SHAPES[i].pattern})" for i in live))


def _secret_pass(text: str) -> str:
    live = tuple(i for i, hints in enumerate(_SHAPE_HINTS)
                 if any(h in text for h in hints))
    pattern = _shapes_for(live)
    return pattern.sub(SECRET_TOKEN, text) if pattern else text

# A PEM block is multi-line and the header alone is not the secret, so this
# takes the whole thing including the footer.
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL)

# Credentials in a URL. The username half goes too: `https://adria:hunter2@...`
# doxes as hard as it leaks.
_URL_CREDS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")

# The named-assignment form, for key shapes nobody has published a prefix for.
# Keeps the NAME — "OPENAI_API_KEY=<redacted>" is a useful line and "<redacted>"
# on its own is not — and takes everything to the end of the value.
_ASSIGNED = re.compile(
    r"(?i)\b(?P<name>[A-Za-z0-9_\-]*"
    r"(?:api[_\-]?key|secret|token|password|passwd|pwd|access[_\-]?key|"
    r"private[_\-]?key|credential)s?)"
    r"(?P<sep>\s*[:=]\s*|\s*[:=]\s*[\"']|\"\s*:\s*\"|'\s*:\s*')"
    r"(?P<value>[^\s\"',;}\]]{6,})")

# The name half is an alternation of these, so the rule cannot fire without one
# of them appearing somewhere. Matched against a lowercased copy because the
# pattern is (?i). The `[_\-]?` spellings collapse to their stems: testing for
# "apikey" would MISS "api_key", so the stem "api" would be needed — but "api"
# alone is far too common to be a useful gate, so both punctuated spellings are
# listed instead and the bare-concatenated form with them.
_ASSIGNED_HINTS: tuple[str, ...] = (
    "apikey", "api_key", "api-key", "secret", "token", "password", "passwd",
    "pwd", "accesskey", "access_key", "access-key",
    "privatekey", "private_key", "private-key", "credential",
)

_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]{12,}")

# Value shapes that are NOT secrets and must survive _ASSIGNED, because the
# dashboard reads them. "token: null" losing its null breaks a JSON consumer;
# "api_key_set: true" is the doctor row that tells you the key is configured
# and is the opposite of a leak.
_NOT_A_SECRET = {"true", "false", "null", "none", "nil", "unset", "", "0", "1",
                 "yes", "no", "set", "missing", "ok", SECRET_TOKEN}


def _redact_assigned(m: re.Match[str]) -> str:
    value = m.group("value")
    if value.strip().lower() in _NOT_A_SECRET:
        return m.group(0)
    # An already-substituted identifier is not a secret either: a line reading
    # `project_token: <project>` should keep saying that.
    if value.startswith("<") and value.endswith(">"):
        return m.group(0)
    return f"{m.group('name')}{m.group('sep')}{SECRET_TOKEN}"


def _kill_secrets(text: str, extra: Iterable[str] = ()) -> str:
    # Literal known values FIRST. If OPENAI_API_KEY is in this process's
    # environment, its value is matched exactly no matter what shape it is or
    # what quoting it arrived in — the shape patterns below are the fallback for
    # secrets this machine has never seen, not the primary defence.
    for literal in extra:
        if literal and len(literal) >= 8:
            text = text.replace(literal, SECRET_TOKEN)
    # Each pass below is gated on a literal it cannot match without. The gate is
    # a substring test costing ~1ms/MB; the pass it guards costs 20-150ms/MB.
    if "-----BEGIN " in text:
        text = _PEM.sub(SECRET_TOKEN, text)
    text = _secret_pass(text)
    if "@" in text and "://" in text:
        text = _URL_CREDS.sub(rf"\g<scheme>{SECRET_TOKEN}@", text)
    lowered = text.lower()          # one pass, shared by the two rules below
    if "bearer " in lowered or "basic " in lowered:
        text = _BEARER.sub(rf"\1 {SECRET_TOKEN}", text)
    if any(h in lowered for h in _ASSIGNED_HINTS):
        text = _ASSIGNED.sub(_redact_assigned, text)
    return text


# ---------------------------------------------------------------------------
# Identity: substituted, and reversible
# ---------------------------------------------------------------------------
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Someone else's home directory, on any of the three platforms. The current
# user's own home is handled by literal replacement (it is known exactly and
# may be a junction or a redirected profile that no pattern predicts); this
# catches the paths that arrive from agent logs, tracebacks and pasted output.
_FOREIGN_HOME = re.compile(
    r"(?i)(?P<prefix>[A-Z]:[\\/]Users[\\/]|/home/|/Users/)(?P<name>[^\\/\s\"']+)")


class Redactor:
    """A filter built once from the environment it is protecting.

    Construction reads the machine: home, username, hostname, the project roots
    in play, and the values of any environment variable whose NAME looks like a
    credential. That last one is why this is a class and not a function — the
    literal values have to be gathered before anything is scrubbed, and doing it
    per-call would re-read the environment on every log line the dashboard
    paints.
    """

    def __init__(self, *, home: Optional[str] = None, user: Optional[str] = None,
                 host: Optional[str] = None, roots: Iterable[str] = (),
                 secrets: Iterable[str] = (), scan_env: bool = True) -> None:
        self.home = str(Path(home).resolve()) if home else _home()
        self.user = user if user is not None else _username()
        self.host = host if host is not None else _hostname()
        # Longest first so a nested root wins over its parent — otherwise
        # C:\games\rpg under C:\games becomes <project>\rpg and the deeper
        # project never gets its own placeholder.
        self.roots = sorted({str(Path(r).resolve()) for r in roots if r},
                            key=len, reverse=True)
        self.secrets = list(secrets)
        if scan_env:
            self.secrets.extend(_env_secrets())

    # -- outbound ---------------------------------------------------------
    def text(self, value: str) -> str:
        """Scrub one string. Secrets die, identity is substituted."""
        if not value:
            return value
        out = _kill_secrets(value, self.secrets)

        # ONE lowercased copy for the gates below, taken here and not refreshed.
        # Every rule in this block is case-insensitive, so the gates need it —
        # and lowering a megabyte costs ~5ms against the 20-40ms each guarded
        # pass costs.
        #
        # Safe to let it go stale: the only thing these substitutions insert is
        # <project>/<home>/<user>/<host>, and none of those contains this
        # machine's real root, home, hostname or username. A rule cannot become
        # matchable because an earlier rule ran.
        low = out.lower()

        # Paths before bare words: the username inside C:\Users\adria\... must
        # be consumed by the path rule, or the bare rule rewrites it first and
        # leaves a half-substituted C:\Users\<user>\Desktop that no longer
        # matches the project root and cannot be restored.
        for i, root in enumerate(self.roots):
            if _path_hint(root) in low:
                out = _replace_path(out, root, _project_token(i, len(self.roots)))
        if self.home and _path_hint(self.home) in low:
            out = _replace_path(out, self.home, HOME_TOKEN)
        if "users" in low or "/home/" in low:
            out = _FOREIGN_HOME.sub(rf"\g<prefix>{USER_TOKEN}", out)

        if "@" in out:
            out = _EMAIL.sub(f"{USER_TOKEN}@{HOST_TOKEN}", out)
        if self.host and self.host.lower() in low:
            out = _bare(self.host).sub(HOST_TOKEN, out)
        if self._bare_user_is_safe() and self.user.lower() in low:
            out = _bare(self.user).sub(USER_TOKEN, out)
        return out

    def scrub(self, value: Any) -> Any:
        """Walk a JSON-shaped value, scrubbing every string in it.

        Dict KEYS are scrubbed too. A dict keyed by absolute path is how the
        doctor report and the project registry are both shaped, and a filter
        that only looked at values would print the whole home directory in the
        one place a viewer is most likely to be reading.
        """
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.scrub(k) if isinstance(k, str) else k: self.scrub(v)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.scrub(v) for v in value)
        return value

    # -- inbound ----------------------------------------------------------
    def restore(self, value: Any) -> Any:
        """Put the real identifiers back, for a request coming the other way.

        Only identifiers. A secret has no restore because it has no map, and a
        `<redacted>` arriving in a request body is a bug upstream, not a value
        to be helpfully rehydrated.
        """
        if isinstance(value, str):
            out = value
            for i, root in enumerate(self.roots):
                out = out.replace(_project_token(i, len(self.roots)), root)
            if self.home:
                out = out.replace(HOME_TOKEN, self.home)
            return out
        if isinstance(value, dict):
            return {self.restore(k) if isinstance(k, str) else k:
                    self.restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.restore(v) for v in value)
        return value

    # -- reporting --------------------------------------------------------
    def status(self) -> dict:
        """What the filter is actually doing, for the indicator in the UI.

        Reports COUNTS and never the values — a status panel that listed the
        secrets it was protecting would be the leak it exists to prevent.
        """
        return {
            "on": True,
            "home": bool(self.home),
            "user": bool(self.user) and self._bare_user_is_safe(),
            "host": bool(self.host),
            "roots": len(self.roots),
            "known_secrets": len([s for s in self.secrets if s]),
            "note": "display filter only — the .env, the DB and devtools are "
                    "unchanged, and the dashboard token is deliberately not "
                    "redacted because the page needs it to authenticate",
        }

    def _bare_user_is_safe(self) -> bool:
        u = (self.user or "").strip()
        return len(u) >= _MIN_BARE_LEN and u.lower() not in _UNSAFE_BARE


def _project_token(index: int, total: int) -> str:
    """<project> when there is one, <project:N> when there are several.

    A dashboard serving one root — the normal case — should read <project> and
    not <project:0>. With more than one in play they have to stay distinct or
    restore() cannot tell them apart.
    """
    return PROJECT_TOKEN if total == 1 else f"<project:{index}>"


def _replace_path(text: str, path: str, token: str) -> str:
    """Substitute one absolute path, in every spelling it shows up in.

    A Windows path reaches a viewer at least four ways: as typed
    (C:\\Users\\x), forward-slashed by pathlib or a URL (C:/Users/x),
    JSON-escaped (C:\\\\Users\\\\x), and percent-encoded in a preview href.
    Handling only the first is how a filter passes its tests and leaks on
    screen. Case-insensitive because Windows paths are, and the same directory
    arrives capitalised differently from argv, from the DB and from a
    traceback.
    """
    if not path:
        return text
    return _path_re(path).sub(lambda m: token, text)


@lru_cache(maxsize=128)
def _path_hint(path: str) -> str:
    """A lowercase literal that EVERY spelling of this path must contain.

    The six variants differ only in their separators, so the longest
    separator-free run — usually the final directory name — survives all of
    them untouched. Testing for it is how we skip the path pass entirely on a
    body that never mentions the project.

    Falls back to "" for a path that is nothing but separators, and "" is in
    every string, so the gate opens rather than closes. Wrong in the safe
    direction: a gate that opens costs time, a gate that closes leaks.
    """
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    return max(parts, key=len).lower() if parts else ""


@lru_cache(maxsize=128)
def _path_re(path: str) -> re.Pattern[str]:
    """The six spellings as ONE alternation, longest first, compiled once.

    This was six `re.sub` calls per path, each rebuilding its pattern from a
    string and each walking the entire body — and it runs once per project root
    plus once for home, on every response. Longest-first ordering is what makes
    the single pass equivalent: Python takes the first alternative that matches
    at a position, so the longest spelling still wins wherever two overlap,
    exactly as the descending-length loop ensured.

    lru_cache because the roots do not change between requests; the old code
    paid re.escape and a pattern-cache lookup on every call.
    """
    variants = {path, path.replace("\\", "/"), path.replace("\\", "\\\\"),
                path.replace("/", "\\"), path.replace("\\", "%5C"),
                path.replace("\\", "/").replace("/", "%2F")}
    ordered = sorted((v for v in variants if v), key=len, reverse=True)
    return re.compile("|".join(re.escape(v) for v in ordered), re.IGNORECASE)


def _bare(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_\-]){re.escape(word)}"
                      rf"(?![A-Za-z0-9_\-])", re.IGNORECASE)


def _home() -> str:
    try:
        return str(Path.home().resolve())
    except (OSError, RuntimeError):
        return ""


def _username() -> str:
    for var in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    home = _home()
    return Path(home).name if home else ""


def _hostname() -> str:
    # A hostname is frequently the owner's name plus a device — "adria-pc" is
    # not meaningfully less identifying than the account itself.
    try:
        return socket.gethostname().split(".")[0].strip()
    except OSError:
        return ""


# Anything whose NAME says credential. Deliberately name-based: this cannot be
# a list of known providers, because the next adapter someone adds will have a
# key nobody added to the list, and a redactor with a per-vendor allowlist fails
# exactly when a new integration is being demonstrated on stream.
_ENV_SECRET_NAME = re.compile(
    r"(?i)(api[_\-]?key|secret|token|password|passwd|credential|access[_\-]?key)")
# ...except the ones that name a FILE or a FLAG rather than carrying a value.
# BGATE_TOKEN_PATH is a path, and blanking it would redact the path instead of
# the token, which is both wrong and confusing.
_ENV_SECRET_SKIP = re.compile(r"(?i)(_path|_file|_dir|_enabled|_set|_name)$")


def _env_secrets() -> list[str]:
    out = []
    for name, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if _ENV_SECRET_NAME.search(name) and not _ENV_SECRET_SKIP.search(name):
            out.append(value)
    # Longest first: a key that contains a shorter key as a substring must be
    # replaced whole, or the tail of it survives on screen.
    return sorted(set(out), key=len, reverse=True)


# ---------------------------------------------------------------------------
# The process-wide filter
# ---------------------------------------------------------------------------
_ACTIVE: Optional[Redactor] = None


def active(roots: Iterable[str] = (), *, force: Optional[bool] = None
           ) -> Optional[Redactor]:
    """The redactor to use right now, or None when the mode is off.

    Cached, but rebuilt when the set of roots changes — the dashboard learns
    which project it is serving after startup, and a redactor built before that
    knows no project path.
    """
    global _ACTIVE
    on = enabled() if force is None else force
    if not on:
        _ACTIVE = None
        return None
    wanted = sorted({str(Path(r).resolve()) for r in roots if r})
    if _ACTIVE is None or _ACTIVE.roots != sorted(wanted, key=len, reverse=True):
        _ACTIVE = Redactor(roots=wanted)
    return _ACTIVE


def scrub(value: Any, roots: Iterable[str] = ()) -> Any:
    """Scrub if the mode is on, pass through untouched if it is not."""
    filt = active(roots)
    return filt.scrub(value) if filt else value


def status(roots: Iterable[str] = ()) -> dict:
    """What to show in the indicator. Always answers, on or off."""
    filt = active(roots)
    if filt is None:
        return {"on": False, "env_var": ENV_VAR,
                "note": f"set {ENV_VAR}=1 to hide paths, identity and keys "
                        f"from the dashboard, the logs and the CLI"}
    return {**filt.status(), "env_var": ENV_VAR}
