"""Project .env loading — secrets live next to the project, never in the repo.

Tiny on purpose (no python-dotenv dependency): KEY=VALUE lines, # comments,
blanks. Existing process env always wins — a var you set in the shell is not
silently overridden by a file. Values never get logged; callers must treat
anything loaded here as radioactive for ledgers and tool results.
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
    """Tests only."""
    _stamps.clear()
