"""What the game is actually bound to, read from its own input map.

The play panel used to tell every project's player "J/K punch · U/I kick ·
S block · L duck" — controls one game once had and the shipped template has
never implemented. The audit filed that under advertised-but-absent, which is
the failure mode where a tool teaches you something false about your own work.

The cure is to stop asserting and start reading: `project.godot`'s ``[input]``
section is the authority on what the keyboard does, so the hint is derived from
it or it is not shown at all.

This is a text parse, not a Godot call. Rendering a controls hint must not
depend on the engine being installed, and `project.godot` is a stable ini-ish
format we already read elsewhere.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# Godot 4 reserves the top bit for non-printable keys (KEY_SPECIAL = 1 << 22).
# Only the ones a game is plausibly bound to are named; anything else falls
# through to a plain "key <n>", which is honest rather than wrong.
_SPECIAL = {
    4194305: "Esc", 4194306: "Tab", 4194308: "Backspace", 4194309: "Enter",
    4194310: "Enter", 4194311: "Insert", 4194312: "Delete", 4194313: "Pause",
    4194317: "Home", 4194318: "End", 4194319: "←", 4194320: "↑",
    4194321: "→", 4194322: "↓", 4194323: "PgUp", 4194324: "PgDn",
    4194325: "Shift", 4194326: "Ctrl", 4194327: "Meta", 4194328: "Alt",
    4194329: "CapsLock",
}
_SPECIAL.update({4194332 + i: f"F{i + 1}" for i in range(12)})

# Godot's own actions. A project's map always carries them; a player does not
# need to be told the UI arrows work.
_BUILTIN_PREFIXES = ("ui_",)

_SECTION_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$")
_ACTION_RE = re.compile(r"^(?P<action>[A-Za-z_][\w/]*)\s*=\s*\{")
_PHYSICAL_RE = re.compile(r'"physical_keycode"\s*:\s*(\d+)')
_KEYCODE_RE = re.compile(r'"keycode"\s*:\s*(\d+)')
_BUTTON_RE = re.compile(r'"button_index"\s*:\s*(\d+)')


def key_name(code: int) -> str:
    """A keycode as something a person can press."""
    if code in _SPECIAL:
        return _SPECIAL[code]
    if code == 32:
        return "Space"
    if 33 <= code <= 126:
        return chr(code).upper()
    return f"key {code}"


def _input_section(text: str) -> list[str]:
    """The raw lines of ``[input]``. The values span multiple lines (Godot
    pretty-prints each event object), so this keeps everything until the next
    section header rather than parsing line-by-line."""
    lines, collecting, out = text.splitlines(), False, []
    for line in lines:
        header = _SECTION_RE.match(line)
        if header:
            collecting = header.group("name") == "input"
            continue
        if collecting:
            out.append(line)
    return out


def parse_input_map(text: str, *, include_builtin: bool = False) -> list[dict]:
    """Every bound action in a ``project.godot``, as ``{action, keys, buttons}``.

    Actions are returned in declaration order — that is the order the project's
    author chose, and it reads better than alphabetical.
    """
    actions: list[dict] = []
    current: Optional[dict] = None

    for line in _input_section(text):
        match = _ACTION_RE.match(line)
        if match:
            name = match.group("action")
            if not include_builtin and name.startswith(_BUILTIN_PREFIXES):
                current = None
                continue
            current = {"action": name, "keys": [], "buttons": []}
            actions.append(current)
        if current is None:
            continue
        # physical_keycode is what the player's finger actually does on a
        # non-QWERTY layout; keycode is the fallback when it is unset (0).
        for raw in _PHYSICAL_RE.findall(line) + _KEYCODE_RE.findall(line):
            code = int(raw)
            if code:
                name = key_name(code)
                if name not in current["keys"]:
                    current["keys"].append(name)
        for raw in _BUTTON_RE.findall(line):
            button = f"pad {raw}"
            if button not in current["buttons"]:
                current["buttons"].append(button)

    return actions


def project_godot(root: str | os.PathLike[str]) -> Optional[Path]:
    """The game's project.godot, wherever the scaffold put it."""
    base = Path(root)
    for candidate in (base / "game" / "project.godot", base / "project.godot"):
        if candidate.is_file():
            return candidate
    return None


def for_project(root: str | os.PathLike[str]) -> list[dict]:
    """The project's controls, or an empty list.

    Empty is a real answer and the UI must render it as one ("controls come from
    this project's input map") — inventing a plausible default is how the old
    hardcoded hint happened.
    """
    path = project_godot(root)
    if path is None:
        return []
    try:
        return parse_input_map(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
