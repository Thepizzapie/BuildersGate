"""Art-provider credentials: read the status, set a key, clear a key.

WHY NOT IN THE SETTINGS REGISTRY. ``/api/settings`` hands back
``settings.describe()`` VERBATIM, including every field's ``value`` — that is
the whole design, and it is what lets settingsview.js render a new switch with
no JavaScript change. A secret in that registry would need a value-suppressing
exception threaded through ``describe``, ``effective``, ``/api/settings/{key}``
AND ``doctor.settings_report``, which prints every value to stdout. Four places
where the next person to add a field gets the default wrong once and the key is
in a terminal scrollback. So keys are a sibling surface with its own rule: this
router has no code path that can emit a key, because it never reads one.

WHAT GOES OVER THE WIRE. Out: presence, a last-4 fingerprint, which layer
supplied the value, and the adapter's own reason it cannot run. In: the key, in
a POST body, once. Never a query string, never a path segment — those land in
logs, in shell history, and in the browser's own address bar.

THE BODY IS AN UNTYPED dict ON PURPOSE. A pydantic model would be tidier and it
would also leak: FastAPI's 422 handler serialises the validation errors, and
pydantic v2 puts the OFFENDING INPUT in each error's ``ctx``/``input``. A key
that fails a model constraint would come straight back out in the error body.
Validating by hand keeps the value out of every response shape there is.

WRITING IS HUMAN-ONLY. Same rule and same mechanism as ``PATCH /api/settings``
and ``/api/spend/budget``: an agent that can write credentials can hand itself a
provider the human never paid for, and a dispatched agent's session is exactly
the caller ``api.current_actor`` exists to recognise.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from bgate_core import providers as _providers
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


def _payload(rows: list[dict]) -> dict:
    return {
        "providers": rows,
        "capabilities": _providers.CAPABILITIES,
        "configured": [row["id"] for row in rows if row.get("configured")],
        # The panel says out loud whether the file it writes into is safe to
        # write into. A key in a committed .env is the incident this project
        # already had, so the fact rides along with every read rather than being
        # something the UI has to think to ask for.
        "env_gitignored": _providers.env_is_ignored(root()),
    }


@router.get("/api/providers")
def providers_view() -> dict:
    """Every provider, what it powers, and whether it can run.

    Deliberately a GET with no key material in the response — a reader of this
    endpoint learns which providers are configured and nothing else. That is the
    same thing ``bgate doctor`` prints, so it exposes no new fact.
    """
    return api.ok(_payload(_providers.status(root())))


@router.post("/api/providers/{provider_id}/key")
def provider_set_key(provider_id: str, request: Request, payload: dict) -> dict:
    """Store one provider's key in the project's .env.

    The response is the provider's status row — presence, fingerprint, and the
    adapter's fresh verdict — so a panel repaints from what is now TRUE rather
    than from what it just sent. That difference is not cosmetic here: a key can
    save correctly and the provider still be unavailable (missing package, a
    shell variable shadowing the file), and a UI that painted "ready" from its
    own optimism would send the user off to debug a generator instead.
    """
    api.require_human(api.current_actor(request), "set an API key")
    body = payload if isinstance(payload, dict) else {}
    value = body.get("key")
    if not isinstance(value, str):
        raise api.bad_request("send {\"key\": \"...\"} — the key as a string")
    try:
        row = _providers.set_key(root(), provider_id, value,
                                 actor=api.current_actor(request))
    except _providers.ProviderError as exc:
        # Never echo `value` back in the message, not even truncated. The
        # registry's refusals are written to describe the SHAPE of the problem
        # ("that value contains whitespace") for exactly this reason.
        raise api.bad_request(str(exc), provider=provider_id)
    except OSError as exc:
        raise api.unavailable(
            f"could not write the project's .env: {type(exc).__name__}: {exc}",
            provider=provider_id)
    return api.ok(_payload(_providers.status(root())), applied=row)


@router.delete("/api/providers/{provider_id}/key")
def provider_clear_key(provider_id: str, request: Request) -> dict:
    """Forget one provider's key — out of the .env and out of this process."""
    api.require_human(api.current_actor(request), "clear an API key")
    try:
        row = _providers.clear_key(root(), provider_id,
                                   actor=api.current_actor(request))
    except _providers.ProviderError as exc:
        raise api.bad_request(str(exc), provider=provider_id)
    except OSError as exc:
        raise api.unavailable(
            f"could not write the project's .env: {type(exc).__name__}: {exc}",
            provider=provider_id)
    return api.ok(_payload(_providers.status(root())), applied=row)
