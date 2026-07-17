"""Project .env loading — secrets live next to the project, never in the repo.

Tiny on purpose (no python-dotenv dependency): KEY=VALUE lines, # comments,
blanks. Existing process env always wins — a var you set in the shell is not
silently overridden by a file. Values never get logged; callers must treat
anything loaded here as radioactive for ledgers and tool results.
"""
from __future__ import annotations

import os
from pathlib import Path

_loaded: set[str] = set()


def load_project_env(root: str | os.PathLike[str]) -> list[str]:
    """Load <root>/.env into os.environ (once per root). Returns loaded KEYS
    only — never values."""
    path = Path(root) / ".env"
    key = str(path.resolve())
    if key in _loaded:
        return []
    _loaded.add(key)
    if not path.is_file():
        return []

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
    _loaded.clear()
