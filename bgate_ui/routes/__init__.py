"""Auto-registered route modules for the dashboard.

Drop a file in this package that defines a module-level `router` (a FastAPI
APIRouter) and it is included automatically — no edit to app.py. This is what
lets the per-seat workspaces each own their own endpoints file without touching
shared code.
"""
from __future__ import annotations

import importlib
import pkgutil


def register(app) -> list[str]:
    """Import every sibling module and include its `router`. Returns the names
    registered (for a startup log). A module that fails to import is skipped
    loudly rather than taking the whole dashboard down."""
    registered: list[str] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # a broken workspace must not kill the app
            print(f"[routes] skipped {info.name}: {exc}")
            continue
        router = getattr(mod, "router", None)
        if router is not None:
            app.include_router(router)
            registered.append(info.name)
    return registered
