"""The provider gateway - which paid providers are LIVE, per capability.

THE FAILURE THIS EXISTS TO END, observed repeatedly: an agent's first
generation call hits the one provider it thought of, that provider answers
402 (no credit) or has no key at all, and the agent concludes "no art today"
and hand-rolls the asset - placeholder PNGs, procedural stand-ins, a 270-line
workaround - while a second provider sat fully keyed and funded the whole
time. One dead key must read as a ROUTING event, not a capability outage.

So this module gives one answer, from any process:

  status(root)            every hosted provider: keyed, reachable, and - where
                          the provider exposes it - the remaining balance.

WHAT IT DELIBERATELY IS NOT. Not a key store (keys stay in .env; there is no
set_api_key tool, same reasoning as bgate_core.store.settings). It reads; the
one thing it writes is a short-lived in-process cache, because balance probes cost a network round
trip and a billing failure can trigger several in one tick.

BALANCE SEMANTICS: None is UNKNOWN, never zero. Only kie and Retro Diffusion
expose a balance endpoint; openai exposes none and krea's API balance shows
only through a 402 at call time. A provider with a key and an unknown
balance is ROUTABLE - the call itself is the probe - while a provider whose
balance reads 0 is named as drained and skipped.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

# Which providers can serve which job. ORDER IS THE DOCTRINE, not a fallback
# chain of equals: sprites/stills are minted with kie (nano-banana-2), motion
# is Retro Diffusion, and that division is a hard project rule stated in the
# art seat's house rules - this table must never quietly reorder it.
CAPABILITIES: dict[str, tuple[str, ...]] = {
    "image": ("kie", "krea", "openai"),
    "animate": ("retrodiffusion",),
    "three_d": ("krea", "stability", "tripo", "meshy", "rodin"),
    "music": ("kie",),
    "video": ("kie",),
}

PROVIDERS = ("openai", "krea", "kie", "retrodiffusion",
             "stability", "tripo", "meshy", "rodin")

# WHAT EACH CAPABILITY SERVES, IN THE WORDS AN AGENT LOOKS FOR. The table
# above names capabilities by their first job ("music") and an agent reads
# it as the whole job. Measured (Turf War, 2026-10-16): the audio seat read
# provider_status, saw kie under image / music / video and no "sound" line,
# concluded its seat had no sound-effects route, and gave up - while
# sfx_prompt, which gates on "music", sat in its own tool list and had
# already produced two clips. The listing sent it wrong; the listing says
# the second job now.
CAPABILITY_SERVES: dict[str, str] = {
    "image": "2D images: stills, sprites, sheets, UI, props, storyboards",
    "animate": "2D animation cycles from an existing sheet",
    "three_d": "3D models",
    "music": "music AND sound effects (Suno via kie: music_generate, "
             "sfx_prompt)",
    "video": "video clips",
}

# THE TOOLS THAT WALK THROUGH EACH GATE. Kept here, beside the table they
# route on, and pinned by a test that reads every _provider_gate call in the
# MCP modules: a tool that gates on a capability and is not listed under it
# fails that test, so this cannot drift into the kind of half-truth that
# sent the audio seat wrong.
CAPABILITY_TOOLS: dict[str, tuple[str, ...]] = {
    "image": ("image_generate", "image_edit", "image_sprites",
              "image_talkhead", "item_generate", "item_variants",
              "tileset_generate", "vfx_animate", "ui_concept",
              "cutout_kit_generate", "cutout_part_rerun",
              "character_generate", "prop_generate", "storyboard_auto",
              "storyboard_frame_generate"),
    "animate": ("animation_generate",),
    # 3D generation routes through provider_for in bgate_core.runtime.providers,
    # not through _provider_gate; nothing to list here yet.
    "three_d": (),
    "music": ("music_generate", "sfx_prompt"),
    "video": ("kie_video_generate", "cinematic_generate_shot"),
}

# Balance probes are network calls; a burst of billing failures must not turn
# into a burst of probes. Per (root, provider), seconds.
CACHE_S = 120.0
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_lock = threading.Lock()


def _probe(provider: str, root) -> dict:
    """One provider's row: {id, keyed, reason, balance, balance_unit}.

    Never raises - a gateway that throws while explaining a failure eats the
    original error. ``keyed`` is offline truth (the adapter's available());
    ``balance`` is None wherever the provider will not say.
    """
    row: dict[str, Any] = {"id": provider, "keyed": False, "reason": "",
                           "balance": None, "balance_unit": ""}
    try:
        if provider == "openai":
            from bgate_adapters import imagegen
            got = imagegen.available()
            row["keyed"] = bool(got.get("available"))
            row["reason"] = str(got.get("reason") or "")
            # No balance API. The key is the whole offline answer.
        elif provider == "krea":
            from bgate_adapters import krea
            got = krea.available(root)
            row["keyed"] = bool(got.get("available"))
            row["reason"] = str(got.get("reason") or "")
            # The API balance surfaces only as a 402 at call time; billed
            # separately from the workspace plan, which is the trap the
            # reason string carries when that 402 arrives.
        elif provider == "kie":
            from bgate_adapters import kie
            got = kie.available(root)
            row["keyed"] = bool(got.get("available"))
            row["reason"] = str(got.get("reason") or "")
            if row["keyed"]:
                row["balance"] = kie.credit_balance(root)
                row["balance_unit"] = "credits"
        elif provider == "retrodiffusion":
            from bgate_adapters import retrodiffusion as rd
            got = rd.available(root)
            row["keyed"] = bool(got.get("available"))
            row["reason"] = str(got.get("reason") or "")
            if row["keyed"]:
                try:
                    bal = rd.balance(root)
                    # PREFER THE USD BALANCE, NOT `credits`. RD's
                    # /inferences/credits returns BOTH, and only `balance` is
                    # the spendable figure its billing charges against -
                    # `credits` kept reporting 50.0 on an account that was
                    # refusing every call with {'code': 'request_failed',
                    # 'message': 'Not enough balance.'}. Preferring `credits`
                    # made this surface state, with fresh=True, that a dry
                    # account was funded; four animation jobs were refused
                    # across two characters while it did. A balance nobody can
                    # spend is not a balance.
                    value = bal.get("balance")
                    if value is None:
                        value, row["balance_unit"] = bal.get("credits"), "credits"
                    else:
                        row["balance_unit"] = "usd"
                    row["balance"] = float(value) if value is not None else None
                except Exception:
                    row["balance"] = None
        else:
            # The rest are declared in providers.PROVIDERS with a probe that
            # borrows their adapter's available(); none exposes a balance.
            from . import providers as _providers
            got = _providers.by_id(provider).probe(root)
            row["keyed"] = bool(got.get("available"))
            row["reason"] = str(got.get("reason") or "")
    except Exception as exc:
        # TYPE NAME ONLY in the row, deliberately. The rows this builds go out
        # over HTTP (/api/providers/balances), and an exception's MESSAGE can
        # carry filesystem paths or a provider response body - CodeQL flagged
        # the flow, and it is right on hygiene even for a loopback-only
        # surface. The adapters' own `reason` strings above are crafted
        # sentences and stay; only the unexpected-failure catch is anonymised,
        # with the full detail kept in the server's own log.
        logging.getLogger(__name__).warning(
            "provider probe %s failed: %s: %s", provider,
            type(exc).__name__, exc)
        row["reason"] = (f"the {provider} probe failed "
                         f"({type(exc).__name__}) - the server log has the "
                         "detail")
    return row


def status(root=None, *, fresh: bool = False) -> list[dict]:
    """Every hosted provider's row, cached CACHE_S per (root, provider).

    ``fresh=True`` bypasses the cache - the right call after the human says
    they just topped an account up.
    """
    key_root = str(root or "")
    now = time.monotonic()
    rows = []
    for provider in PROVIDERS:
        cache_key = (key_root, provider)
        with _lock:
            hit = _cache.get(cache_key)
        if hit and not fresh and now - hit[0] < CACHE_S:
            rows.append(dict(hit[1]))
            continue
        row = _probe(provider, root)
        with _lock:
            _cache[cache_key] = (now, dict(row))
        rows.append(row)
    return rows


def _drained(row: dict) -> bool:
    balance = row.get("balance")
    return balance is not None and float(balance) <= 0


def pick(root, capability: str) -> dict:
    """The provider a ``capability`` job should route to right now.

    {"provider": id or None, "why": sentence, "alternatives": [ids]} -
    doctrine order, keyed, not provably drained. None means genuinely nothing
    is routable, and ``why`` says so per provider so the caller can file THE
    blocker rather than A blocker.
    """
    order = CAPABILITIES.get(capability, ())
    if not order:
        return {"provider": None, "alternatives": [],
                "why": f"unknown capability {capability!r}; known: "
                       f"{sorted(CAPABILITIES)}"}
    rows = {r["id"]: r for r in status(root)}
    routable = [p for p in order
                if rows.get(p, {}).get("keyed") and not _drained(rows[p])]
    if routable:
        chosen = routable[0]
        note = _balance_phrase(rows[chosen])
        return {"provider": chosen, "alternatives": routable[1:],
                "why": f"{chosen} is keyed{note}"}
    reasons = "; ".join(
        f"{p}: " + ("drained (balance 0)" if p in rows and _drained(rows[p])
                    else str(rows.get(p, {}).get("reason") or "no key"))
        for p in order)
    return {"provider": None, "alternatives": [],
            "why": f"no {capability} provider is routable - {reasons}"}


def _balance_phrase(row: dict) -> str:
    balance = row.get("balance")
    if balance is None:
        return " (balance unknown - the call itself is the probe)"
    return f" with {balance:g} {row.get('balance_unit') or 'credits'} left"


# Error text that means "the account, not the request" - the shapes the
# adapters actually raise (kie 402 sentence, krea 402 sentence, RD/openai
# billing phrasing). Deliberately narrow: a 422 must not read as a money
# problem or the redirect trains agents to provider-hop around real bugs.
_BILLING_SIGNS = ("no credit", "insufficient credit", "out of credit",
                  "insufficient_quota", "exceeded your current quota",
                  "billing", "402", "payment required", "top the account up",
                  "top it up", "balance")


def is_billing_error(error) -> bool:
    """Is this failure about the ACCOUNT rather than the request?

    A :class:`bgate_adapters._http.ProviderError` answers for itself - the
    flag is set where the 402 or the balance sentence was read - and the
    substring match is only the fallback for a bare string or a foreign
    exception (the openai SDK's, say) that carries no flag.
    """
    from bgate_adapters._http import ProviderError

    if isinstance(error, ProviderError):
        return bool(error.billing)
    lowered = str(error or "").lower()
    return any(sign in lowered for sign in _BILLING_SIGNS)


def billing_note(root=None) -> str:
    """The capability-agnostic redirect appended to every billing-shaped tool
    failure (bgate_mcp.server._fail). It cannot know which capability the
    failed call served, so it reports the whole board and restates the
    division of labour; the agent holds the context to pick the lane.
    """
    parts = []
    for row in status(root):
        if not row.get("keyed"):
            parts.append(f"{row['id']}: no key")
        elif _drained(row):
            parts.append(f"{row['id']}: keyed but DRAINED (balance 0)")
        else:
            parts.append(f"{row['id']}: keyed{_balance_phrase(row)}")
    live = [r for r in status(root) if r.get("keyed") and not _drained(r)]
    tail = ("Re-run the SAME pipeline tool routed at a live provider where "
            "the craft allows it (sprites/stills: kie > krea > openai; "
            "motion: retrodiffusion only; music/video: kie only). Do NOT "
            "hand-roll a substitute asset because one account is empty."
            if live else
            "Nothing is funded. Do not improvise a substitute asset: name "
            "the drained accounts in your result note and file the top-up "
            "as the blocker - a human decides which account gets money.")
    return ("ONE ACCOUNT BEING EMPTY IS A ROUTING EVENT, NOT AN OUTAGE. "
            "The board right now - " + "; ".join(parts) + ". " + tail +
            " provider_status(fresh=true) re-probes after a top-up.")
