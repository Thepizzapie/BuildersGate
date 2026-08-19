"""Project .env loading — secrets live next to the project, never in the repo.

Tiny on purpose (no python-dotenv dependency): KEY=VALUE lines, # comments,
blanks. A var the SHELL set always wins — it is never silently overridden by a
file. Vars this module itself put into os.environ are a different matter: it
remembers which file supplied each one (see ``_owned``), so in a long-lived
process that serves several projects — the MCP server with a project_dir per
call, the dashboard after a project switch — a later project's .env re-points
the vars the earlier project's file loaded instead of the first project's keys
winning forever. Values never get logged; callers must treat anything loaded
here as radioactive for ledgers and tool results.

It WRITES now as well as reads, because the dashboard grew a panel for setting
art-provider keys and a second .env parser would be a second thing to get wrong
about quoting. The writer is read-modify-write and atomic — see
:func:`write_var` for why both halves are non-negotiable.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

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

# name -> (source, value): which .env (by its resolved path) supplied the value
# THIS MODULE put into os.environ, and the value it put there. This is what
# makes project-scoped keys project-scoped in one process serving several
# projects: without it, load's never-overwrite rule means whichever project
# loads FIRST owns every shared name (OPENAI_API_KEY, ...) for the life of the
# process. Only vars this module set (or adopted, see load_project_env) appear
# here; a var the shell exported is never owned and therefore never re-pointed
# or evicted. The value rides along so an external change (a test's
# monkeypatch, a caller assigning os.environ directly) is detected and
# ownership is relinquished rather than the external value being stomped.
_owned: dict[str, tuple[str, str]] = {}

# The names the SHELL owned when this process started. Used only to gate
# ADOPTION: a var that appears later with exactly the value a layer's file
# holds was almost certainly assigned by our own code (providers._reapply
# writes os.environ directly after a key save), and adopting it lets a project
# switch re-point it — but a name the shell exported at startup is never
# adopted, whatever its value, because the shell always wins.
_shell_names = frozenset(os.environ)


def global_dir() -> Path:
    """The user-scoped Builders Gate directory that holds the machine-wide .env.

    Same directory the project registry and the active-project pointer already
    live in (``~/.bgate``, or ``BGATE_HOME``), and for the same reason: it is
    the one place on the machine that belongs to this product rather than to any
    one game. Its own docstring promises nothing game-shaped ever lands there,
    which a credential satisfies — a key belongs to the person, not the project.

    Imported lazily so this module keeps its "no bgate imports" shape; the
    fallback matters on the path where ``project`` cannot be imported at all,
    because a key store that silently moves is worse than one that is missing.
    """
    try:
        from bgate_core import project

        return project.user_dir()
    except Exception:
        override = os.environ.get("BGATE_HOME")
        return Path(override).expanduser() if override else Path.home() / ".bgate"


def global_path() -> Path:
    """``~/.bgate/.env`` — the machine-wide key file."""
    return global_dir() / ".env"


def load_env(root: Optional[str | os.PathLike[str]] = None) -> dict[str, list[str]]:
    """Load the project's .env and then the machine-wide one. Keys, never values.

    THE PRECEDENCE, and it is the only thing here that is load-bearing:

        shell environment  >  THIS project's .env  >  ~/.bgate/.env

    Most specific first, which is the rule every layer of this product already
    follows. A shell export beats both files — that was already true and is why
    the status panel has a ``shadowed`` state. The project beats the global
    because standing in a project is a statement about which credentials you
    mean, exactly as ``require_root`` treats standing in a project as beating
    the remembered active one.

    "THIS project" is not "whichever project loaded first". In one process
    serving several projects, a var an earlier project's .env supplied is
    evicted here (see :func:`_evict_stale`) before this project's layers load,
    so the answer for project B is B's key, then the global fallback — never
    A's leftovers. Vars the shell set are untouched by all of this.

    Returns ``{"project": [...], "global": [...]}`` so a caller can say which
    layer supplied what. ``root=None`` loads the global layer alone, which is
    the whole point of there being one: a tool that needs a key and not a game
    should not have to invent a project to hold it. (With no project named,
    nothing is evicted — there is no project to re-point to.)
    """
    loaded = {"project": [], "global": []}
    if root:
        _evict_stale(_source_key(root))
        loaded["project"] = load_project_env(root)
    loaded["global"] = load_project_env(global_dir())
    return loaded


def _source_key(root: str | os.PathLike[str]) -> str:
    """The identity of <root>/.env as an ownership source."""
    path = Path(root) / ".env"
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _global_key() -> str:
    try:
        return _source_key(global_dir())
    except Exception:
        return ""


def _evict_stale(current: str) -> None:
    """Unload every var owned by a project OTHER than ``current``.

    The cross-project bleed fix. A var is only popped from os.environ when it
    still holds exactly the value this module set — an external change means
    someone else owns it now, and their value stands (ownership is dropped
    either way). When anything was popped the stamps are cleared, so the
    current project's file and the global one are re-read in full and refill
    what the eviction uncovered; a set-once stamp would otherwise report
    "already loaded" for a file whose vars just left the environment.

    Never raises: a failure to tidy must not take a tool call down with it.
    """
    try:
        keep = {current, _global_key()}
        stale = [name for name, (src, _v) in _owned.items() if src not in keep]
        popped = False
        for name in stale:
            _src, value = _owned.pop(name)
            if os.environ.get(name) == value:
                os.environ.pop(name, None)
                popped = True
        if popped:
            _stamps.clear()
    except Exception:
        pass


def load_project_env(root: str | os.PathLike[str]) -> list[str]:
    """Load <root>/.env into os.environ, re-reading it whenever the file has
    changed since the last load. Returns loaded KEYS only — never values.

    ``root`` is any directory holding a ``.env``; :func:`global_dir` is passed
    here too, which is what makes the machine-wide store the same code path as a
    project's rather than a second parser to keep in step.
    """
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

    global_key = _global_key()
    project_layer = key != global_key

    loaded = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if not name or not value:
            continue
        current = os.environ.get(name)
        owner = _owned.get(name)
        if owner is not None and current != owner[1]:
            # Something outside this module changed or removed it since we set
            # it (a shell-level assignment, a test's monkeypatch). Whoever did
            # that wins from here on; we only note that it is no longer ours.
            _owned.pop(name, None)
            owner = None
        if current is None:
            os.environ[name] = value
            _owned[name] = (key, value)
            loaded.append(name)
        elif owner is None:
            if current == value and name not in _shell_names:
                # Exactly the value this file holds, set by our own process
                # (providers._reapply assigns os.environ directly after a key
                # save): adopt it, so a later project switch can re-point it.
                # A name the shell exported at startup is never adopted.
                _owned[name] = (key, value)
            # else: the shell set it; a file never overrides the shell.
        elif owner[0] == key:
            # Our own earlier load from THIS file — refresh, so a rotated key
            # actually rotates instead of the first-ever value sticking.
            if current != value:
                os.environ[name] = value
                loaded.append(name)
            _owned[name] = (key, value)
        elif project_layer:
            # A project's .env outranks every other FILE layer: a stale value
            # another project's .env loaded, or the global fallback.
            os.environ[name] = value
            _owned[name] = (key, value)
            loaded.append(name)
        # else: the global layer never overrides a project-owned value.
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
