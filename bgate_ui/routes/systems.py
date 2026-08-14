"""Systems — the rules the game is actually made of, over HTTP.

The gameplay seat's SYSTEMS tab has been a placeholder saying "causal_specs and
causal_chains are MCP tools with no route". That was true and it is the wrong
answer: the seat that owns the core loop cannot read the loop's own resolution
ladders without an agent to fetch them for it.

WHAT A SYSTEM IS HERE. `.bgate/causal_specs.json` holds one spec per system: the
event kinds that open an attempt, the gates in RESOLUTION ORDER, and the terminal
that means success. `bgate_core/causal.py` folds a session's telemetry into
chains against that ladder — so "why do so many attacks fail?" is answered by a
gate name and a count instead of by reading JSONL.

`order_verified` IS THE LOAD-BEARING FLAG AND IT IS SURFACED, NOT SMOOTHED. Every
passed gate in a chain is an INFERENCE from gate order, and order lives in the
game's source, not its telemetry — `causal_infer_spec` cannot recover it. A spec
nobody confirmed produces chains whose failure counts are sound and whose passes
are guesses. This route reports that per system and per summary rather than
letting an unverified ladder render exactly like a verified one; that is the
difference between a diagnosis and a plausible story.

READ ONLY. Writing a spec means asserting an order against source, which is
`causal_infer_spec` plus a human — not a dashboard GET.
"""
from __future__ import annotations

import json

from fastapi import APIRouter

from bgate_core import causal, db, playtest
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


def _events(conn, session_id: int) -> list[dict]:
    """The ingested telemetry for one session, in causal.py's shape.

    Read from `playtest_event` rather than re-parsing the JSONL: ingestion has
    already put every event on the SESSION's clock, which the raw file has not
    (the game may have been running an hour before anyone hit record).
    """
    out = []
    for row in conn.execute(
            "SELECT t, kind, data FROM playtest_event WHERE session_id = ? ORDER BY t",
            (session_id,)):
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        out.append({"t": row["t"], "kind": row["kind"],
                    "data": data if isinstance(data, dict) else {}})
    return out


@router.get("/api/systems")
def systems_index(session_id: int | None = None) -> dict:
    """Every declared system, with the latest session run through it.

    With no spec file the answer is an empty list plus the telemetry contract's
    own next step — an honest "nothing is declared", not an error. That state is
    the normal start of a project and it is where most projects will sit.
    """
    r = root()
    specs = causal.load_specs(r)
    conn = db.connect(r)

    row = conn.execute(
        "SELECT id, name, started_at FROM playtest_session WHERE status = 'ready' "
        + ("AND id = ? " if session_id else "")
        + "ORDER BY started_at DESC LIMIT 1",
        (session_id,) if session_id else ()).fetchone()
    session = dict(row) if row else None
    events = _events(conn, session["id"]) if session else []

    out = []
    for name in sorted(specs):
        spec = specs[name]
        described = causal.describe_spec(spec)
        chains = causal.build_chains(events, spec) if events else []
        summary = causal.summarize(chains)
        # The gate that fails most is the tuning question this whole module
        # exists to answer, so it is lifted out rather than left in a dict the
        # UI has to sort itself.
        gates = summary.get("by_failed_gate") or {}
        described["measured"] = {
            "session": session["name"] if session else None,
            "attempts": summary.get("total", 0),
            "success_rate": summary.get("success_rate"),
            "by_outcome": summary.get("by_outcome", {}),
            "by_failed_gate": gates,
            "worst_gate": next(iter(gates), None),
            # Repeated on the measurement because THIS is where it changes what
            # a reader may conclude: unverified passes are assumptions.
            "order_verified": spec.order_verified,
            "warning": summary.get("warning"),
        }
        out.append(described)

    return api.ok({
        "systems": out,
        "session": session,
        "events": len(events),
        # When there are no specs, say what would make one — the seat should not
        # have to go read the MCP tool list to find out.
        "next": None if out else
                playtest.telemetry_contract().get("for_causal_chains"),
    })
