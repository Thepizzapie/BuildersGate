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
            current = {"action": name, "keys": [], "buttons": [], "keycodes": []}
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
                if code not in current["keycodes"]:
                    current["keycodes"].append(code)
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
        web = controls_json(root)
        if web is None:
            return []
        try:
            return parse_controls_json(web.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return []
    try:
        return parse_input_map(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# A web game's input map: controls.json, DOM codes per action
# ---------------------------------------------------------------------------
#
# A vite game has no project.godot; its bindings are TypeScript tables nobody
# should parse. It ships `controls.json` instead ({"actions": [{"action":
# "move_left", "keys": ["KeyA", "ArrowLeft"]}, ...]}) with the DOM
# KeyboardEvent.code values its own listener reads, in the order the pad
# should show them. Measured (LAST CALL on the handheld): with no map the
# companion drew its default arrows-and-Space pad against a WASD game and not
# one button did anything.

_DOM_NAMED: dict[str, tuple[str, int]] = {
    "Space": (" ", 32), "Escape": ("Escape", 27), "Enter": ("Enter", 13), "Tab": ("Tab", 9),
    "Backspace": ("Backspace", 8), "ShiftLeft": ("Shift", 16), "ShiftRight": ("Shift", 16),
    "ControlLeft": ("Control", 17), "ControlRight": ("Control", 17), "AltLeft": ("Alt", 18),
    "AltRight": ("Alt", 18), "ArrowLeft": ("ArrowLeft", 37), "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39), "ArrowDown": ("ArrowDown", 40),
}
_DOM_NAMED_LABELS = {"Space": "Space", "Escape": "Esc", "Enter": "Enter", "Tab": "Tab",
                     "Backspace": "Backspace", "ShiftLeft": "Shift", "ShiftRight": "Shift",
                     "ControlLeft": "Ctrl", "ControlRight": "Ctrl", "AltLeft": "Alt", "AltRight": "Alt",
                     "ArrowLeft": "\u2190", "ArrowUp": "\u2191", "ArrowRight": "\u2192", "ArrowDown": "\u2193"}


def dom_from_code(code: str) -> Optional[dict]:
    """A DOM KeyboardEvent.code (what a web game binds) as the pad's key fields."""
    if code in _DOM_NAMED:
        key, kc = _DOM_NAMED[code]
        return {"label": _DOM_NAMED_LABELS[code], "code": code, "key": key, "keyCode": kc}
    m = re.fullmatch(r"Key([A-Z])", code)
    if m:
        ch = m.group(1)
        return {"label": ch, "code": code, "key": ch.lower(), "keyCode": ord(ch)}
    m = re.fullmatch(r"Digit([0-9])", code)
    if m:
        ch = m.group(1)
        return {"label": ch, "code": code, "key": ch, "keyCode": ord(ch)}
    by_code = {v: k for k, v in _DOM_PUNCT.items()}  # defined further down the module
    if code in by_code:
        n = by_code[code]
        return {"label": chr(n), "code": code, "key": chr(n), "keyCode": n}
    return None


def controls_json(root: str | os.PathLike[str]) -> Optional[Path]:
    """A web game's controls.json, wherever the project keeps it."""
    base = Path(root)
    for candidate in (base / "controls.json", base / "design" / "controls.json",
                      base / "game" / "controls.json"):
        if candidate.is_file():
            return candidate
    return None


def parse_controls_json(text: str) -> list[dict]:
    """Every action in a controls.json as ``{action, keys, buttons, codes}``,
    declaration order kept; ``keys`` are labels like parse_input_map's."""
    import json as _json
    doc = _json.loads(text)
    rows = doc.get("actions") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise ValueError('controls.json: expected {"actions": [...]}')
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("action"):
            continue
        codes = [c for c in (row.get("keys") or []) if isinstance(c, str) and dom_from_code(c)]
        out.append({"action": str(row["action"]), "keys": [dom_from_code(c)["label"] for c in codes],
                    "buttons": [str(b) for b in (row.get("buttons") or [])], "codes": codes})
    return out


# ---------------------------------------------------------------------------
# A touch layout for the phone, from the same map
# ---------------------------------------------------------------------------
#
# The companion app plays the Web export in a web view, where a finger is a
# mouse and a keyboard does not exist. It draws a d-pad and a few buttons and
# dispatches the KeyboardEvents the engine's own listener reads, so the game
# needs no change and no project needs a hand-written mapping: the roles come
# from the action NAMES (ui_left, move_left, walk_left, left ... are all the
# left arrow of a pad) and the keys from the map, translated to the DOM's
# `code` / `key` / `keyCode` triplet Godot's web input reads.

_DOM_SPECIAL: dict[int, tuple[str, str, int]] = {
    4194305: ("Escape", "Escape", 27), 4194306: ("Tab", "Tab", 9),
    4194308: ("Backspace", "Backspace", 8), 4194309: ("Enter", "Enter", 13),
    4194310: ("NumpadEnter", "Enter", 13), 4194311: ("Insert", "Insert", 45),
    4194312: ("Delete", "Delete", 46), 4194317: ("Home", "Home", 36),
    4194318: ("End", "End", 35), 4194319: ("ArrowLeft", "ArrowLeft", 37),
    4194320: ("ArrowUp", "ArrowUp", 38), 4194321: ("ArrowRight", "ArrowRight", 39),
    4194322: ("ArrowDown", "ArrowDown", 40), 4194323: ("PageUp", "PageUp", 33),
    4194324: ("PageDown", "PageDown", 34), 4194325: ("ShiftLeft", "Shift", 16),
    4194326: ("ControlLeft", "Control", 17), 4194327: ("MetaLeft", "Meta", 91),
    4194328: ("AltLeft", "Alt", 18), 4194329: ("CapsLock", "CapsLock", 20),
}
_DOM_SPECIAL.update({4194332 + i: (f"F{i + 1}", f"F{i + 1}", 112 + i) for i in range(12)})
_DOM_PUNCT = {44: "Comma", 46: "Period", 47: "Slash", 59: "Semicolon", 39: "Quote",
              91: "BracketLeft", 93: "BracketRight", 92: "Backslash", 45: "Minus",
              61: "Equal", 96: "Backquote"}


def dom_key(code: int) -> Optional[dict]:
    """A Godot keycode as the DOM event fields the web build reads."""
    if code in _DOM_SPECIAL:
        dom, key, kc = _DOM_SPECIAL[code]
        return {"label": key_name(code), "code": dom, "key": key, "keyCode": kc}
    if code == 32:
        return {"label": "Space", "code": "Space", "key": " ", "keyCode": 32}
    if 65 <= code <= 90:
        ch = chr(code)
        return {"label": ch, "code": f"Key{ch}", "key": ch.lower(), "keyCode": code}
    if 48 <= code <= 57:
        ch = chr(code)
        return {"label": ch, "code": f"Digit{ch}", "key": ch, "keyCode": code}
    if code in _DOM_PUNCT:
        return {"label": chr(code), "code": _DOM_PUNCT[code], "key": chr(code), "keyCode": code}
    return None


# Action-name → pad role. First match wins; the ui_ builtins are the engine's
# own and every 2D scaffold binds them, so a project with nothing custom still
# gets a working pad.
_ROLE_RES: list[tuple[str, re.Pattern]] = [
    ("left", re.compile(r"(^|_)(left|west)$")),
    ("right", re.compile(r"(^|_)(right|east)$")),
    ("up", re.compile(r"(^|_)(up|north|forward)$")),
    ("down", re.compile(r"(^|_)(down|south|back|backward)$")),
    ("accept", re.compile(r"(^|_)(accept|confirm|ok|select|interact|use|talk)$")),
    ("cancel", re.compile(r"(^|_)(cancel|back|escape)$")),
    ("menu", re.compile(r"(^|_)(menu|pause|start|inventory)$")),
]
_DPAD = ("left", "right", "up", "down")
_BUTTON_LABELS = {"accept": "OK", "cancel": "Back", "menu": "Menu"}
_MAX_BUTTONS = 6


def _role(action: str) -> str:
    name = action.lower()
    for role, rx in _ROLE_RES:
        if rx.search(name):
            return role
    return ""


def _button_label(action: str, role: str) -> str:
    if role in _BUTTON_LABELS:
        return _BUTTON_LABELS[role]
    bare = re.sub(r"^(ui_|action_|btn_|input_)", "", action)
    words = bare.replace("_", " ").split()
    full = " ".join(words).title()
    # A cap fits on a button; the first word is a better fit than a cut.
    return (full if len(full) <= 8 else words[0].title()[:8]) or action[:8]


def pad_layout(actions: list[dict]) -> dict:
    """A d-pad and up to six buttons for the phone, from parse_input_map(
    include_builtin=True). Every entry carries the DOM key to dispatch.

    ``dpad`` is a role → key dict (missing directions are simply absent, a
    menu-only game gets no pad). ``buttons`` are in a fixed order - accept,
    cancel, menu, then the project's own actions in declaration order - so
    the thumb learns one layout across projects.
    """
    return _layout([(a["action"], [k for k in (dom_key(c) for c in a.get("keycodes", [])) if k])
                    for a in actions])


def pad_layout_web(actions: list[dict]) -> dict:
    """The same pad from parse_controls_json's rows (DOM codes, not keycodes)."""
    return _layout([(a["action"], [k for k in (dom_from_code(c) for c in a.get("codes", [])) if k])
                    for a in actions])


def _layout(entries: list[tuple[str, list[dict]]]) -> dict:
    dpad: dict[str, dict] = {}
    buttons: list[dict] = []
    seen_codes: set[str] = set()
    seen_roles: set[str] = set()
    for action, keys in entries:
        if not keys:
            continue
        role = _role(action)
        if role in _DPAD:
            if role not in dpad:
                dpad[role] = {**keys[0], "action": action}
            continue
        # One button per distinct key: ui_accept and "interact" both on Enter
        # would be two buttons that do the same thing.
        key = keys[0]
        if key["code"] in seen_codes or role in seen_roles:
            continue
        seen_codes.add(key["code"])
        if role in _BUTTON_LABELS:
            seen_roles.add(role)      # one OK, one Back, one Menu
        buttons.append({**key, "action": action, "role": role or "action",
                        "label": _button_label(action, role)})
    order = {"accept": 0, "cancel": 1, "menu": 2}
    buttons.sort(key=lambda b: order.get(b["role"], 3))
    return {"dpad": dpad, "buttons": buttons[:_MAX_BUTTONS],
            "dropped": [b["action"] for b in buttons[_MAX_BUTTONS:]]}


def pad_for_project(root: str | os.PathLike[str]) -> dict:
    path = project_godot(root)
    if path is None:
        web = controls_json(root)
        if web is None:
            return {"dpad": {}, "buttons": [], "dropped": []}
        try:
            return pad_layout_web(parse_controls_json(web.read_text(encoding="utf-8", errors="replace")))
        except (OSError, ValueError):
            return {"dpad": {}, "buttons": [], "dropped": []}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"dpad": {}, "buttons": [], "dropped": []}
    return pad_layout(parse_input_map(text, include_builtin=True))
