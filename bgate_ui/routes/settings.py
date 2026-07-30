"""One settings surface: read every switch, write any of them, one validator.

Four features each added their switch in a different mechanism — a column on
``spend_budget``, a workspace doc, an env var read inline, a module constant — so
nothing listed them and nothing said which one the environment was overriding.
``bgate_core.settings`` is the registry that describes them where they already
live; this router is the only thing between it and a browser.

THE UI RENDERS FROM THE DESCRIPTION, NOT FROM A TEMPLATE PER SWITCH. ``GET`` hands
back ``describe()`` verbatim: groups, then fields carrying kind, choices, range,
value, default, source and help. Adding a toggle is one registry entry and zero
edits here, which is the whole scalability claim of the plan — a payload shaped
per setting would have put the fifth switch back to costing three edits in three
files that then disagree.

VALIDATION IS BORROWED, NEVER RE-DERIVED. Every value goes through
``settings.coerce`` / ``settings.set``, so a range is enforced once. A bad key or
an out-of-range value is a 400 carrying the reason the registry gave; nothing
here can turn one into a 500, because a settings panel that answers a typo with a
traceback is a panel people stop opening.

A PATCH IS VALIDATED WHOLE, THEN WRITTEN. Half-applying a five-field save leaves
the board in a state the human never asked for and the panel cannot describe, so
every value is checked before the first write lands. Writes are still one store
at a time — three stores with no shared transaction is the cost of not moving the
storage — so a failure mid-write reports exactly which keys did land.

WRITING IS A HUMAN ACTION. These are the ceilings and gates that bound agents:
budget, concurrency, who signs off, whether failures reopen themselves. An agent
that can PATCH this can widen the leash it is on, which is the same reason
``/api/spend/budget`` has always required a human.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from bgate_core import settings as _settings
from bgate_core import workspace as _ws
from bgate_ui import api
from bgate_ui.deps import root

# The event bus is optional here on purpose. A settings panel is most needed
# exactly when something else is broken — including a project whose migration to
# the event table has not run — and a router that will not import takes its
# endpoints off the dashboard entirely (see routes/__init__.py).
try:  # pragma: no cover - import guard, not logic
    from bgate_core import events as _events
except Exception:  # noqa: BLE001 - any import failure means "no bus", not "no panel"
    _events = None  # type: ignore[assignment]

router = APIRouter()


def apply_changes(project, changes: dict) -> list[dict]:
    """Validate every change, then store them; report what each one did.

    Shared with the ``/api/spend/budget`` alias so that one SQL row has exactly
    one validation path. ``changes`` is ``{registry key: value}``. Returns a row
    per key: ``{key, previous, stored, value, source, env_override}`` — ``stored``
    is what went to the store and ``value`` is what is now EFFECTIVE, which differ
    whenever an env var is winning, and a UI that conflated them would show a save
    as having taken effect when a shell profile is still overriding it.

    Raises :class:`api.ApiError`: 400 for an unknown key or a value outside the
    declared range, 409 when another tab moved the same doc first, 503 when a
    store refused the write. The whole payload is validated before anything is
    written; without that a rejected fifth field leaves the first four applied.
    """
    if not changes:
        raise api.bad_request(
            "nothing to change — send {key: value} using the keys from "
            "GET /api/settings", keys=list(_settings.keys()))

    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, value in changes.items():
        try:
            clean[key] = _settings.coerce(key, value)
        except _settings.SettingError as exc:
            errors[key] = str(exc)
        except ValueError as exc:  # a validator that raised plain ValueError
            errors[key] = str(exc)
    if errors:
        first = next(iter(errors.values()))
        # The legal keys ride along: "unknown setting 'gate_mode'" is only
        # actionable next to the list that has 'gate.mode' in it, and a UI that
        # has to fetch GET /api/settings to interpret a 400 will not bother.
        raise api.bad_request(first, errors=errors, invalid=list(errors),
                              keys=list(_settings.keys()))

    # Read the before-values while nothing has moved, so every row can say what
    # it changed from — the difference between "saved" and "saved, and it was
    # already that" is what stops a human re-clicking a switch that never took.
    previous: dict[str, Any] = {}
    for key in clean:
        try:
            previous[key] = _settings.get(project, key)
        except Exception:
            previous[key] = None

    applied: list[dict] = []
    for key, value in clean.items():
        try:
            result = _settings.set(project, key, value)
        except _settings.SettingError as exc:
            raise api.bad_request(str(exc), key=key, applied=applied)
        except _ws.StaleWrite as exc:
            # Another tab saved this doc first. A conflict, not a bad request: the
            # value was fine, the precondition was not — and merging instead would
            # erase whatever the other save changed.
            raise api.conflict(str(exc), key=key, applied=applied)
        except Exception as exc:
            # A store that would not take the write. 503 rather than 500 because
            # nothing about the request was wrong and retrying is the right move.
            raise api.unavailable(
                f"could not store {key}: {type(exc).__name__}: {exc}",
                key=key, applied=applied)
        applied.append({
            "key": key,
            "previous": previous.get(key),
            "stored": result.get("stored"),
            "value": result.get("value"),
            "source": result.get("source"),
            "env_override": result.get("env_override") or "",
        })
        _announce(project, key, previous.get(key), result)
    return applied


def _announce(project, key: str, previous: Any, result: dict) -> None:
    """Put a settings change on the event bus when the vocabulary has a kind for it.

    Only ``gate.mode`` does, deliberately — the bus vocabulary is small so that a
    notification checkbox can enumerate it, and "somebody moved a slider" is not
    something a subscriber acts on. Who signs off IS: a run that starts under a
    different gate than the one the human thinks is active is the confusion the
    whole gate surface exists to prevent.

    Best-effort in both directions: no bus module, no event table, or a locked
    database must never turn a successful save into a failed request.
    """
    if _events is None or key != "gate.mode":
        return
    value = result.get("value")
    if value == previous:
        return
    try:
        _events.emit(project, "gate.mode", ref=key, payload={
            "key": key,
            "mode": value,
            "previous": previous,
            "source": result.get("source", ""),
            "env_override": result.get("env_override") or "",
        })
    except Exception:
        pass


@router.get("/api/settings")
def settings_view() -> dict:
    """Every switch, grouped, with value, default, range and which layer won.

    ``source`` is ``default | stored | env`` and ``locked`` is true when the
    environment owns the value — without those two a panel showing "agent" while
    ``BGATE_QA_GATE=0`` forces "none" is the most expensive lie a settings surface
    can tell, and somebody debugs the gate for an hour before finding their shell
    profile.
    """
    return api.ok(_settings.describe(root()))


@router.patch("/api/settings")
def settings_patch(request: Request, payload: dict) -> dict:
    """Save one or more settings and return the whole description again.

    Returning ``describe()`` rather than an acknowledgement means a UI never has
    to guess what took effect: a save that an env var is overriding, or that
    another field's range clamped, comes back stated. ``applied`` beside it says
    what each key did, so a panel can flag exactly the row that was overridden.
    """
    api.require_human(api.current_actor(request), "change settings")
    project = root()
    changes = dict(payload or {})
    # Flat {key: value}, no envelope: the keys are the registry's own dotted
    # names, so a wrapper object would only add a level for a client to get wrong.
    if any(not isinstance(key, str) for key in changes):
        raise api.bad_request("send a flat {key: value} object")
    applied = apply_changes(project, changes)
    return api.ok(_settings.describe(project), applied=applied)


@router.get("/api/settings/{key}")
def setting_view(key: str) -> dict:
    """One setting, for a panel that wants to re-read a single row after a save
    rather than the whole table. 404 on an unknown key, because a UI silently
    rendering nothing for a typo'd key is how a control ends up wired to a
    setting that does not exist.

    A plain ``{key}`` rather than ``{key:path}``: registry keys are dotted and
    never contain a slash, and the greedy converter would swallow any future
    ``/api/settings/<something>`` route declared in another module."""
    try:
        _settings.setting(key)
    except _settings.SettingError as exc:
        raise api.not_found(str(exc), key=key)
    described = _settings.describe(root())
    for group in described.get("groups", []):
        for field in group.get("fields", []):
            if field.get("key") == key:
                return api.ok(field)
    raise api.not_found(f"unknown setting '{key}'", key=key)
