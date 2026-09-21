"""ITEM 13 — a dispatch-time check that an art/audio/cinematic seat has
somewhere to spend before an agent is spawned to spend it.

MEASURED: Retro Diffusion ran dry ($0.11) on EXIT 67's night one; two Codex
art runs stalled on it silently - each one dispatched, discovered the drained
provider itself, and burned a full agent turn reporting what this check now
answers before the process ever starts.

``provider_status`` already reads balances (bgate_core.runtime.gateway /
providers); this module is dispatch's own preflight read of the same data,
scoped to the capabilities one seat actually uses, with a short cache so a
fast-dispatching board does not hammer the balance API on every spawn.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

# Which gateway CAPABILITIES (see bgate_core.runtime.gateway.CAPABILITIES) a
# seat's paid tools actually reach. A seat not listed here spends nothing and
# is never held by this check - tech, gameplay, narrative, qa, director.
SEAT_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "art": ("image", "three_d"),
    "audio": ("music",),
    "cinematic": ("video",),
}

CACHE_S = 60.0
_cache: dict[tuple[str, str], tuple[float, Optional[dict]]] = {}
_lock = threading.Lock()


def _cached(root: str, seat: str, compute):
    key = (str(root), seat)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    result = compute()
    with _lock:
        _cache[key] = (now, result)
    return result


def clear_cache() -> None:
    """Test hook / after a top-up: drop every cached read."""
    with _lock:
        _cache.clear()


def check(root: str, seat: str) -> Optional[dict]:
    """None when the seat can dispatch; a refusal dict when it cannot.

    A seat with no paid capability (SEAT_CAPABILITIES has no entry) always
    returns None - there is nothing to preflight. A seat with NO key
    configured at all ALSO returns None: "no art key yet" is normal, blocks
    nothing per project doctrine (see CLAUDE.md's `art_key` row - "if not,
    everything except art generation still works, do not block on it"), and
    is not the failure this item targets. This refuses ONLY the specific
    case measured on EXIT 67: a provider that WAS keyed and IS drained
    (balance <= 0), with no other route for that capability - dispatching an
    agent there would spend one turn discovering what this already knows.

    Fails OPEN on its own errors, like every explanatory gate in this
    codebase (see server._provider_gate) - the machinery for explaining
    money must never be the thing that blocks work.
    """
    capabilities = SEAT_CAPABILITIES.get(seat, ())
    if not capabilities:
        return None

    def compute():
        try:
            from . import gateway
        except Exception:
            return None  # cannot check; do not block on our own failure

        try:
            rows = {r["id"]: r for r in gateway.status(root)}
        except Exception:
            return None

        picks, drained_providers = {}, []
        for cap in capabilities:
            try:
                picks[cap] = gateway.pick(root, cap)
            except Exception:
                return None
            for pid in gateway.CAPABILITIES.get(cap, ()):
                row = rows.get(pid)
                if row and row.get("keyed") and gateway._drained(row):
                    drained_providers.append((cap, pid, row.get("balance")))
        if any(p.get("provider") for p in picks.values()):
            return None  # at least one capability is routable
        if not drained_providers:
            # Nothing is keyed for this seat at all - not configured, not
            # this item's problem, and not a dispatch blocker.
            return None

        reasons = "; ".join(f"{pid} ({cap}): balance {bal}"
                             for cap, pid, bal in drained_providers)
        return {
            "ok": False, "code": "provider_drained", "seat": seat,
            "capabilities": list(capabilities),
            "drained": [{"provider": pid, "capability": cap, "balance": bal}
                        for cap, pid, bal in drained_providers],
            "error": (
                f"the {seat} seat's provider is drained - {reasons}. This is "
                "an account problem, not a work problem: dispatching an "
                "agent here would spend one turn discovering this itself. "
                "Top up the named provider, or provider_status(fresh=true) "
                "after a top-up to re-probe past this cache."),
        }

    return _cached(root, seat, compute)


def providers_brief(root: str, seat: str) -> list[dict]:
    """Bounded provider facts for one seat's brief: name, usable, balance,
    routes-to. Empty for a seat with no paid capability."""
    capabilities = SEAT_CAPABILITIES.get(seat, ())
    if not capabilities:
        return []

    def compute():
        try:
            from . import gateway
        except Exception:
            return []
        try:
            rows = {r["id"]: r for r in gateway.status(root)}
        except Exception:
            return []
        out = []
        seen = set()
        for cap in capabilities:
            for pid in gateway.CAPABILITIES.get(cap, ()):
                if pid in seen:
                    continue
                seen.add(pid)
                row = rows.get(pid, {})
                drained = bool(row) and gateway._drained(row)
                out.append({
                    "name": pid, "capability": cap,
                    "keyed": bool(row.get("keyed")),
                    "balance": row.get("balance"),
                    "usable": bool(row.get("keyed")) and not drained,
                })
        return out[:12]  # bounded: a brief, not the whole gateway table

    return _cached(root, f"brief:{seat}", compute) or []
