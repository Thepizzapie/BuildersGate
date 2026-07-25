"""Auto-registered route modules for the dashboard.

Drop a file in this package that defines a module-level `router` (a FastAPI
APIRouter) and it is included automatically — no edit to app.py. This is what
lets the per-seat workspaces each own their own endpoints file without touching
shared code.

A module that fails to import USED to print a line at startup and vanish: half
the API could be missing while the dashboard looked perfectly healthy (the
shipped wheel once contained no ``routes/`` at all and nothing said so). So the
failure is now a first-class, queryable fact — recorded in FAILURES, served by
``GET /api/routes/status``, and re-raised at startup when BGATE_STRICT_ROUTES=1
(what CI and the packaging test should use).
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import traceback

from fastapi import APIRouter

# Import failures from the last register() call. Module-level so anything in the
# process — an endpoint, a test, a startup check — can see them.
FAILURES: list[dict] = []
REGISTERED: list[str] = []

status_router = APIRouter()


@status_router.get("/api/routes/status")
def routes_status() -> dict:
    """Which route modules loaded, and which did not.

    A UI that renders this can say "the art workspace API failed to load"
    instead of silently drawing an empty panel forever.
    """
    return {
        "ok": not FAILURES,
        "registered": list(REGISTERED),
        "failed": [{k: v for k, v in f.items() if k != "traceback"}
                   for f in FAILURES],
        "detail": FAILURES,
    }


def strict() -> bool:
    return os.environ.get("BGATE_STRICT_ROUTES", "").strip().lower() in {
        "1", "true", "yes", "on"}


def register(app) -> list[str]:
    """Import every sibling module and include its `router`.

    Returns the names registered. A module that fails to import does not take
    the dashboard down (a broken workspace must still leave the rest usable) —
    but it is recorded in FAILURES and surfaced at /api/routes/status, so a
    missing half of the API can never again look like a healthy dashboard.
    """
    FAILURES.clear()
    REGISTERED.clear()
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # a broken workspace must not kill the app
            FAILURES.append({
                "module": info.name,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-2000:],
            })
            print(f"[routes] FAILED {info.name}: {type(exc).__name__}: {exc} "
                  "— its endpoints are MISSING (see /api/routes/status)")
            continue
        router = getattr(mod, "router", None)
        if router is not None:
            app.include_router(router)
            REGISTERED.append(info.name)
    app.include_router(status_router)
    if FAILURES and strict():
        raise RuntimeError(
            "route modules failed to import: "
            + ", ".join(f"{f['module']} ({f['error']})" for f in FAILURES))
    return list(REGISTERED)
