"""One enforcement setting, composed from four ladders.

Four dials grew up separately - the seatless director's lane check
(BGATE_DIRECTOR_MODE), a seated worker's lane (BGATE_LANES), the project
boundary (BGATE_AEGIS) and the approval gate (gate.mode / BGATE_QA_GATE) -
and the question "how hard is this board enforcing right now" needed four
answers from four modules. This module names three PROFILES and each ladder
reads its default from the selected one.

PRECEDENCE: an explicit ladder setting beats the profile. BGATE_LANES=block
under the ``relaxed`` profile means block; the profile only supplies what
nothing else set. The profile itself comes from BGATE_ENFORCEMENT, else the
stored ``enforcement.profile`` setting, else ``standard`` - which is exactly
the set of defaults every ladder shipped with, so an untouched board behaves
as it always did.
"""
from __future__ import annotations

import os
from typing import Optional

ENV = "BGATE_ENFORCEMENT"
SETTING = "enforcement.profile"
DEFAULT_PROFILE = "standard"

LADDERS = ("director", "lanes", "aegis", "gate")

PROFILES: dict[str, dict[str, str]] = {
    "relaxed": {"director": "off", "lanes": "collide",
                "aegis": "warn", "gate": "none"},
    "standard": {"director": "collide", "lanes": "warn",
                 "aegis": "block", "gate": "agent"},
    "strict": {"director": "block", "lanes": "block",
               "aegis": "block", "gate": "builders"},
}

# The explicit override each ladder honours ahead of the profile.
LADDER_ENV = {"director": "BGATE_DIRECTOR_MODE", "lanes": "BGATE_LANES",
              "aegis": "BGATE_AEGIS", "gate": "BGATE_QA_GATE / gate.mode"}


def _root(root) -> Optional[str]:
    """The project whose stored profile applies: the one given, else the one
    dispatch pinned (BGATE_ROOT). None means only the env can choose."""
    if root:
        return str(root)
    pinned = os.environ.get("BGATE_ROOT", "").strip()
    return pinned or None


def profile(root=None) -> str:
    """The active profile name. Never raises."""
    return _profile_with_source(root)[0]


def _profile_with_source(root=None) -> tuple[str, str]:
    chosen = os.environ.get(ENV, "").strip().lower()
    if chosen in PROFILES:
        return chosen, "env"
    where = _root(root)
    if where:
        try:
            from ..store import settings as _settings
            got = str(_settings.get(where, SETTING) or "").strip().lower()
            if got in PROFILES:
                return got, _settings.source(where, SETTING)
        except Exception:
            pass
    return DEFAULT_PROFILE, "default"


def ladder(name: str, root=None) -> str:
    """The profile's value for one ladder - the DEFAULT a ladder falls back to
    when nothing set it explicitly."""
    return PROFILES[profile(root)][name]


def resolved(root=None) -> dict[str, dict[str, str]]:
    """Every ladder's effective mode and where it came from.

    Reads the ladders themselves, so an explicit override is reported as
    such rather than as the profile's opinion.
    """
    from . import aegis as _aegis, gates as _gates, seats as _seats

    active = profile(root)
    out: dict[str, dict[str, str]] = {}

    def _put(name: str, mode: str, explicit: bool) -> None:
        out[name] = {"mode": mode,
                     "source": LADDER_ENV[name] if explicit else
                     f"profile {active!r}"}

    try:
        from bgate_cli import hook as _hook
        director = _hook.director_mode(root)
        explicit = os.environ.get("BGATE_DIRECTOR_MODE", "").strip().lower() \
            in _hook.DIRECTOR_MODES
    except Exception:
        director, explicit = PROFILES[active]["director"], False
    _put("director", director, explicit)
    _put("lanes", _seats.lane_mode(root),
         os.environ.get("BGATE_LANES", "").strip().lower() in _seats.LANE_MODES)
    _put("aegis", _aegis.mode(root),
         os.environ.get("BGATE_AEGIS", "").strip().lower() in _aegis.MODES)
    where = _root(root)
    gate_mode, gate_explicit = PROFILES[active]["gate"], False
    if where:
        try:
            gate_mode = _gates.mode(where)
            from ..store import settings as _settings
            gate_explicit = _settings.source(where, _gates.SETTING) != \
                _settings.SOURCE_DEFAULT
        except Exception:
            pass
    _put("gate", gate_mode, gate_explicit)
    return out


_SENTENCES = {
    "director": {
        "off": "a seatless session is not checked at all",
        "collide": "a seatless session is refused only a file another live "
                   "run holds",
        "warn": "a seatless session is told about out-of-lane writes, which "
                "still land",
        "block": "a seatless session is refused out-of-lane writes like any "
                 "seat"},
    "lanes": {
        "collide": "a seated worker's lane is waived; collisions still block",
        "warn": "a seated worker's out-of-lane writes land and the human is "
                "told",
        "block": "a seated worker is refused out-of-lane writes"},
    "aegis": {
        "off": "the project boundary is not checked",
        "warn": "a seated worker crossing the project boundary is allowed and "
                "reported",
        "block": "a seated worker is refused anything outside its project"},
    "gate": {
        "none": "an agent's own word closes its item",
        "agent": "the QA seat verifies every deliverable before it counts",
        "builders": "you approve every deliverable before it counts"},
}


def describe(root=None) -> str:
    """The composed policy: one sentence per ladder, the profile first."""
    active, src = _profile_with_source(root)
    lines = [f"Enforcement profile {active!r} ({src}; {ENV} or "
             f"{SETTING} selects one of {', '.join(PROFILES)})."]
    for name, row in resolved(root).items():
        what = _SENTENCES[name].get(row["mode"], row["mode"])
        lines.append(f"{name} = {row['mode']} ({row['source']}): {what}.")
    return "\n".join(lines)
