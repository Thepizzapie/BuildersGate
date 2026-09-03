"""Who signs off before an agent's work counts as done.

THREE MODES, AND THE MIDDLE ONE IS WHAT SHIPPED BEFORE THIS FILE EXISTED.

    none      an agent's own word closes its item. Fastest, and the one that
              let a HUD ship with black boxes in it.
    agent     the QA seat reviews every maker-seat deliverable and reopens it
              with a nitpick list on FAIL (bgate_ui.agents.qa_gate). Verification
              without a human in the loop.
    builders  the HUMAN approves. A finished item parks in 'review' and the
              chain behind it does not advance until somebody says yes.

The distinction that matters is not "strict vs loose", it is WHO IS AWAY. A
studio that wants to leave the board running overnight wants `agent`; a studio
that wants to watch each thing land wants `builders`; a studio iterating on
something it will look at anyway in ten seconds wants `none`. Forcing one of
those on the other two is how a gate ends up switched off wholesale, which is
the outcome every mode here exists to avoid.

WHY NOT BOTH (agent THEN human)? Because a queue of things waiting on a human is
only useful if the human actually drains it, and stacking two gates doubles the
latency on every item to buy a review the human is already doing by reading the
QA verdict. Deliberately not built; if it is ever wanted it is a fourth mode,
not a flag on this one.

PERSISTED PER PROJECT (workspace doc ``director/gate``), like auto-deploy, and
read fresh on every check so flipping it mid-run takes effect on the next item
rather than the next restart.

THE VALUE NOW COMES THROUGH ``bgate_core.store.settings`` (key ``gate.mode``), which
describes that same doc field — the storage did not move. What that buys is one
precedence rule and one validator: before it, the env kill switch was applied
here and nowhere else, so a settings panel listing "agent" while
``BGATE_QA_GATE=0`` forced "none" was a lie only this module could have caught.
Every public function below keeps its shape, including ``state()['env_override']``.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from . import activity
from ..store import settings as _settings, workspace as _ws

SEAT = "director"
KEY = "gate"

NONE = "none"
AGENT = "agent"
BUILDERS = "builders"
MODES = (NONE, AGENT, BUILDERS)

# The registry key these constants describe. Kept as module constants because
# every caller in the tree already imports MODES/DEFAULT from here.
SETTING = "gate.mode"

# What the board did before the setting existed. A silent change of behaviour on
# upgrade is worse than a setting nobody flips.
DEFAULT = AGENT

LABELS = {
    NONE: "no gate — an agent's own word closes its item",
    AGENT: "agent gate — the QA seat verifies every deliverable",
    BUILDERS: "builder's gate — you approve before anything counts as done",
}

# The pre-existing kill switch. It predates this module, it is in the docs, and
# somebody's shell profile has it in already — so it keeps working, and it means
# exactly what it always meant: no automatic review of any kind.
ENV_OFF = "BGATE_QA_GATE"


def _env_forces_none() -> bool:
    return os.environ.get(ENV_OFF, "1").strip().lower() in ("0", "false", "off")


def mode(root: str | os.PathLike[str]) -> str:
    """The active mode, honouring the legacy env kill switch.

    Reads through the settings registry so the env precedence is applied in one
    place. Falls back to the doc, then to DEFAULT: this is consulted on every
    completion, and an unreadable registry must degrade to the old behaviour
    rather than leave the board unable to decide whether anything is gated.
    """
    try:
        got = str(_settings.get(root, SETTING) or "").strip()
        if _settings.source(root, SETTING) == _settings.SOURCE_DEFAULT:
            # Nothing set it explicitly: the enforcement profile decides.
            from . import enforcement
            return enforcement.ladder("gate", root)
        return got if got in MODES else DEFAULT
    except Exception:
        pass
    if _env_forces_none():
        return NONE
    try:
        got = str(_ws.get(root, SEAT, KEY, {}).get("mode") or "").strip()
    except Exception:
        return DEFAULT
    return got if got in MODES else DEFAULT


def set_mode(root: str | os.PathLike[str], value: str, by: str = "") -> dict:
    """Store the mode and log it. Raises ValueError on an unknown mode.

    Validation is delegated to the registry so the panel, the API and this
    setter cannot disagree about what is a legal mode; the local MODES check
    stays as the fallback for a registry that will not import.
    """
    try:
        value = _settings.coerce(SETTING, value)
    except _settings.SettingError as exc:
        raise ValueError(str(exc)) from None
    except Exception:
        pass
    if value not in MODES:
        raise ValueError(f"gate mode must be one of {MODES}")
    was = mode(root)
    doc = {
        "mode": value,
        "since": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "by": by or activity.current_actor(),
    }
    _ws.set(root, SEAT, KEY, doc)
    activity.log(root, "gate", f"approval gate -> {value}: {LABELS[value]}",
                 seat=SEAT)
    after = state(root)
    _announce(root, was, after)
    return after


def _announce(root, was: str, after: dict) -> None:
    """Put the change on the event bus. Guarded, and skipped when nothing moved.

    THE CONSOLE'S INLINE CONTROL COMES THROUGH HERE and the Settings panel comes
    through ``settings.set``, so without this emit one of the two writers rang the
    bell and the other did not — for the same switch, with no way to tell from the
    drawer which one somebody used. Who signs off is worth an event because a run
    that starts under a different gate than the human believes is active is the
    confusion this whole surface exists to prevent.

    Best-effort in every direction: no bus module, no ``event`` table (a project
    from before migration 0016) and a locked database must never turn flipping the
    gate into a failed request.
    """
    if str(after.get("mode") or "") == str(was or ""):
        return
    try:
        from ..store import events as _events

        _events.emit(root, "gate.mode", ref=SETTING, payload={
            "key": SETTING, "mode": after.get("mode"), "previous": was,
            "source": after.get("source", ""),
            "env_override": after.get("env_override") or "",
        })
    except Exception:
        pass


def state(root: str | os.PathLike[str]) -> dict:
    """The mode plus why it is what it is — the env override is invisible
    otherwise, and a setting the UI shows as 'agent' while an env var forces
    'none' is the most expensive kind of lie a settings panel can tell.

    ``source`` is added for the generalised panel ("default" | "stored" | "env");
    ``env_override`` keeps its exact old wording because the console's gate
    control tests for it and renders it verbatim.
    """
    try:
        doc = _ws.get(root, SEAT, KEY, {})
    except Exception:
        doc = {}
    stored = str(doc.get("mode") or "").strip()
    forced = _env_forces_none()
    try:
        source = _settings.source(root, SETTING)
    except Exception:
        source = _settings.SOURCE_ENV if forced else (
            _settings.SOURCE_STORED if stored in MODES else _settings.SOURCE_DEFAULT)
    return {
        "mode": mode(root),
        "stored": stored if stored in MODES else DEFAULT,
        "modes": list(MODES),
        "labels": dict(LABELS),
        "since": doc.get("since") or "",
        "by": doc.get("by") or "",
        "source": source,
        "setting": SETTING,
        "env_override": f"{ENV_OFF}=0 forces no gate" if forced else "",
    }


def holds_for_human(root: str | os.PathLike[str]) -> bool:
    """Does a finished item wait in 'review' instead of going straight to done?"""
    return mode(root) == BUILDERS


def wants_qa_agent(root: str | os.PathLike[str]) -> bool:
    """Should a completed maker-seat item spawn the auto-QA reviewer?"""
    return mode(root) == AGENT


def describe(root: str | os.PathLike[str], seat: Optional[str] = None) -> str:
    """One line for an agent's brief, so the working seat knows what closing
    its item actually means before it claims done."""
    active = mode(root)
    if active == BUILDERS:
        return ("APPROVAL GATE: builder's gate. Reporting done parks this item "
                "in 'review' for the human — it is NOT closed, and anything "
                "chained behind it will not start until they approve.")
    if active == AGENT:
        return ("APPROVAL GATE: agent gate. Reporting done spawns a QA agent "
                "that will verify the claim against the real artefact and "
                "reopen this item with a nitpick list if it does not hold.")
    return ("APPROVAL GATE: none. Reporting done closes this item with nobody "
            "checking it — so the evidence in your result note is the only "
            "record that it worked.")
