"""Project .env loading — secrets live next to the project, never in the repo.

Tiny on purpose (no python-dotenv dependency): KEY=VALUE lines, # comments,
blanks. Existing process env always wins — a var you set in the shell is not
silently overridden by a file. Values never get logged; callers must treat
anything loaded here as radioactive for ledgers and tool results.

It WRITES now as well as reads, because the dashboard grew a panel for setting
art-provider keys and a second .env parser would be a second thing to get wrong
about quoting. The writer is read-modify-write and atomic — see
:func:`write_var` for why both halves are non-negotiable.
"""
from __future__ import annotations

import os
from pathlib import Path

# path -> the (mtime_ns, size) we last loaded. Keyed on the STAMP, not just
# "seen it", because the miss this fixes is the common one: the server says
# OPENAI_API_KEY is missing, the user pastes it into .env, and a set-once cache
# means only a RESTART is ever going to see it — with nothing on screen saying
# so. A stat() per call is cheap enough for the hot path (every tool call goes
# through here) and buys us noticing the edit on the next call instead.
#
# Size rides along with mtime because a fast edit can land inside one filesystem
# timestamp tick; a key being added always changes the length.
_stamps: dict[str, tuple[int, int]] = {}


def load_project_env(root: str | os.PathLike[str]) -> list[str]:
    """Load <root>/.env into os.environ, re-reading it whenever the file has
    changed since the last load. Returns loaded KEYS only — never values."""
    path = Path(root) / ".env"
    key = str(path.resolve())
    try:
        stat = path.stat()
    except OSError:  # missing, or a directory we cannot stat
        _stamps.pop(key, None)
        return []
    if not path.is_file():
        return []
    stamp = (stat.st_mtime_ns, stat.st_size)
    if _stamps.get(key) == stamp:
        return []
    _stamps[key] = stamp

    loaded = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if not name or not value:
            continue
        if name not in os.environ:  # shell wins over file
            os.environ[name] = value
            loaded.append(name)
    return loaded


def reset_cache() -> None:
    """Forget every stamp, so the next :func:`load_project_env` re-reads.

    Not tests-only any more. :mod:`bgate_core.providers` calls it after writing
    a key, and it is doing real work there: the stamp is (mtime_ns, size), and
    REPLACING a key with another of the SAME LENGTH inside one filesystem
    timestamp tick changes neither — rotating a key would then look like a
    no-op until something else touched the file.
    """
    _stamps.clear()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

# Nothing legitimate is longer than this, and an unbounded write is how a
# paste of an entire buffer ends up as one .env line.
MAX_VALUE = 4096


class EnvWriteError(ValueError):
    """A value that must not be written, with the reason to show the human."""


def _path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / ".env"


def _assignment(line: str, name: str) -> bool:
    """Is this line the assignment for `name`? Mirrors the loader exactly.

    Comments are not assignments even when they contain one — a commented-out
    `#OPENAI_API_KEY=...` is a note the user left themselves, and overwriting it
    would both lose the note and leave the real assignment untouched.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return False
    return stripped.partition("=")[0].strip() == name


def file_vars(root: str | os.PathLike[str]) -> dict[str, str]:
    """Every KEY=VALUE in <root>/.env, read from the FILE, ignoring os.environ.

    Callers use this to tell "the .env supplies this" apart from "a shell
    variable is shadowing the .env" — the loader deliberately lets the shell win,
    so a panel that only looked at os.environ would report a saved key as in
    force while a stale shell export was the value actually being sent.

    Returns values. Treat them exactly as radioactive as anything else here.
    """
    out: dict[str, str] = {}
    try:
        text = _path(root).read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip().strip('"').strip("'")
    return out


def _read_raw(path: Path) -> str:
    """The file with its line endings INTACT.

    ``Path.read_text`` translates CRLF to LF on the way in, so a writer that
    used it could never see that the file was CRLF and would silently convert
    the whole thing — a one-line change arriving as a whole-file diff, on
    Windows, which is the supported platform.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return handle.read()


def _newline(text: str) -> str:
    """Keep the file's own line endings. A .env written by Notepad is CRLF."""
    return "\r\n" if "\r\n" in text else "\n"


def _atomic_write(path: Path, text: str) -> None:
    """Temp file in the same directory, fsync, then replace.

    A crash between truncate and write on the real file leaves the user with no
    keys at all — which is the one outcome worse than the write failing, because
    they did not lose anything they typed, they lost the ones already working.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        # newline="" so the caller's chosen line endings survive verbatim.
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)  # best effort; Windows ACLs do not map cleanly
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass  # the common case: replace() already moved it


def write_var(root: str | os.PathLike[str], name: str, value: str, *,
              allow_spaces: bool = False) -> str:
    """Set one variable in <root>/.env. Returns 'created' | 'updated' | 'added'.

    READ-MODIFY-WRITE, LINE BY LINE. Every other line — comments, blanks,
    unrelated variables, their order — comes back byte for byte. A writer that
    re-serialises the whole file from a parsed dict silently eats the comments
    people put above their keys explaining which account they came from.

    The value is written raw, unquoted, because the loader strips one layer of
    quotes and a key containing no whitespace needs none. Whitespace is refused
    outright rather than quoted: every provider token this ships with is one
    opaque word, so a space in the paste means the paste is wrong, and quoting
    it would store the mistake instead of reporting it.

    ``allow_spaces`` IS FOR FILESYSTEM PATHS, AND FOR NOTHING ELSE. The local
    generation surface stores things like ``BGATE_COMFY_T2I_WORKFLOW`` here, and
    on the supported platform a real path is routinely
    ``C:\\Users\\me\\My Documents\\wf.json``. Refusing that would push the whole
    local-runtime config into a second store, which is how a project ends up
    with two answers to "where does configuration live". The value is then
    written INSIDE double quotes, which is exactly the one layer
    :func:`load_project_env` and :func:`file_vars` strip back off, so the
    round-trip is byte-identical. A value containing a double quote, a newline
    or a NUL is still refused outright — quoting cannot rescue those and a
    silently mangled path is worse than a refusal with a sentence on it.
    """
    name = (name or "").strip()
    if not name or not name.replace("_", "").isalnum():
        raise EnvWriteError(f"'{name}' is not a usable variable name")
    value = (value or "").strip()
    if not value:
        raise EnvWriteError("no value — send the key, or clear it instead")
    if len(value) > MAX_VALUE:
        raise EnvWriteError(f"that is {len(value)} characters; the limit is "
                            f"{MAX_VALUE} — is it the whole key and nothing else?")
    quote = False
    if allow_spaces:
        if any(ch in value for ch in ('"', "\r", "\n", "\x00")):
            raise EnvWriteError(
                "that value contains a quote or a line break — a .env line "
                "cannot hold either; paste the path only")
        quote = any(ch.isspace() for ch in value)
    elif any(ch.isspace() for ch in value) or "\x00" in value:
        # Never quote the value back at them: see the docstring.
        raise EnvWriteError("that value contains whitespace — paste the key "
                            "only, with nothing before or after it")
    if quote:
        value = f'"{value}"'

    path = _path(root)
    try:
        existing = _read_raw(path)
    except OSError:
        existing = ""
    line = f"{name}={value}"
    if not existing:
        _atomic_write(path, line + "\n")
        return "created"

    eol = _newline(existing)
    lines = existing.splitlines()
    for index, one in enumerate(lines):
        if _assignment(one, name):
            if one == line:
                return "updated"  # already exactly this; do not churn mtime
            lines[index] = line
            _atomic_write(path, eol.join(lines) + eol)
            return "updated"
    lines.append(line)
    _atomic_write(path, eol.join(lines) + eol)
    return "added"


def remove_var(root: str | os.PathLike[str], name: str) -> bool:
    """Drop every assignment of `name` from <root>/.env. True if one was there.

    EVERY assignment, not the first: the loader takes the last one it reads, so
    removing only the first would leave a shadowed duplicate still in force and
    the panel reporting a key it just cleared.
    """
    name = (name or "").strip()
    path = _path(root)
    try:
        existing = _read_raw(path)
    except OSError:
        return False
    lines = existing.splitlines()
    kept = [one for one in lines if not _assignment(one, name)]
    if len(kept) == len(lines):
        return False
    eol = _newline(existing)
    _atomic_write(path, (eol.join(kept) + eol) if kept else "")
    return True
