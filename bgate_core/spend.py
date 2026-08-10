"""Money. The one thing in this product that can run away unattended.

Agent sessions and image generation both spend real money with no natural stop.
Before this module ``total_cost_usd`` was parsed off the final result event and
returned in an ephemeral JSON response — never persisted, never summed — so
nothing could answer "worst case, what does tonight cost".

Two halves: a ledger (:func:`record`) that every paying call writes to, and a
budget (:func:`check`) the dispatcher consults *before* spawning. The budget is
a refusal, not a warning.
"""
from __future__ import annotations

import os
from typing import Optional

from bgate_core import db

# "mesh" is image-to-3D. It was landing under "other" alongside genuinely
# uncategorised spend, which is the one bucket nobody reads — and a textured
# generation is ~$0.30, an order of magnitude over an image, so it is exactly
# the line an author wants to find when the month looks wrong.
#
# "video" is here for the same reason, one capability later: kie's own docs put
# video at 100-500 credits against an image's 10-50, so a clip is the single
# most expensive thing this product can buy and it must not sum into "other".
#
# "speech" is Deepgram — the human talking to the brainstorm agent and the agent
# talking back. Apart from "audio" on purpose: "audio" is a sound asset the game
# ships and this is conversation that produces no file, and the two are metered
# on different axes (minutes listened vs characters spoken). Summed together
# neither number answers anything. Migration 0024 widened the CHECK to match.
KINDS = ("agent", "image", "audio", "video", "mesh", "speech", "other")

# WHICH BILL A ROW LANDS ON. The ledger used to sum these together, which is
# how a project total came to match no statement anywhere.
#
#   api           real money. An image generation is invoiced by OpenAI or
#                 Krea whether or not anyone looks at the ledger, so a dollar
#                 here is a dollar.
#   subscription  what the Claude CLI reports as total_cost_usd, which on a
#                 subscription is the API-equivalent price of a run nobody is
#                 charged for. Useful as a size, worthless as a sum against
#                 real spend, and actively harmful in a ceiling: a busy
#                 afternoon of uncharged agent work was refusing image
#                 generation that WOULD have cost money.
API, SUBSCRIPTION = "api", "subscription"
BILLING = (API, SUBSCRIPTION)

# Agent sessions run on the subscription; everything else buys from a vendor.
_BILLING_FOR_KIND = {"agent": SUBSCRIPTION}


def budget(root: str | os.PathLike[str]) -> dict:
    row = db.connect(root).execute("SELECT * FROM spend_budget WHERE id = 1").fetchone()
    return dict(row) if row else {}


def set_budget(root: str | os.PathLike[str], **fields) -> dict:
    """Update the ceilings. Unknown keys are ignored so a UI can PATCH loosely."""
    allowed = {"per_item_usd", "per_day_usd", "per_project_usd",
               "max_runtime_s", "max_concurrent", "enforced"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if sets:
        assignments = ", ".join(f"{k} = ?" for k in sets)
        with db.tx(root) as conn:
            conn.execute(
                f"UPDATE spend_budget SET {assignments}, "
                "updated_at = datetime('now') WHERE id = 1",
                list(sets.values()))
    return budget(root)


def record(root: str | os.PathLike[str], usd: float, *, kind: str = "agent",
           work_item_id: Optional[int] = None, logical_name: str = "",
           detail: str = "", model: str = "", tokens: Optional[dict] = None) -> None:
    """Append a spend event. Never raises — losing the ledger must not lose the
    work that produced it.

    ``tokens`` is the CLI's usage block for an agent run: input, output,
    cache_read, cache_write. It is recorded because on a subscription the
    dollar figure is notional and the TOKENS are what exhaust the rolling usage
    window — a ledger that only knows dollars cannot answer why five hours of
    allowance went in three and a half.

    A run with tokens but no dollars is still worth a row, which is why the
    early return now checks both. Otherwise a run under a plan that reports no
    price would vanish from the ledger entirely.
    """
    tokens = tokens or {}
    counts = {k: int(tokens.get(k) or 0) for k in
              ("input", "output", "cache_read", "cache_write")}
    if (not usd or usd <= 0) and not any(counts.values()):
        return
    if kind not in KINDS:
        kind = "other"
    billing = _BILLING_FOR_KIND.get(kind, API)
    try:
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO spend_event (kind, billing, work_item_id, "
                "logical_name, usd, detail, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, billing, work_item_id, logical_name, max(0.0, float(usd or 0)),
                 detail[:500], model[:80], counts["input"], counts["output"],
                 counts["cache_read"], counts["cache_write"]))
            if work_item_id and usd and usd > 0:
                conn.execute(
                    "UPDATE work_item SET total_cost_usd = total_cost_usd + ? "
                    "WHERE id = ?", (float(usd), work_item_id))
    except Exception:
        pass


def totals(root: str | os.PathLike[str]) -> dict:
    """What has been spent, at the granularities the budget cares about.

    ``project_usd`` and ``today_usd`` are REAL MONEY ONLY — the api-billed
    rows. They used to include agent sessions, whose dollar figure is the
    API-equivalent price of work a subscription already covers, and the sum was
    a number that matched no invoice and refused real purchases on behalf of
    imaginary ones.

    The subscription side is reported alongside rather than dropped: it is the
    honest way to size a night's agent work, and its tokens are the only thing
    that tracks the limit that actually bites.
    """
    conn = db.connect(root)
    row = conn.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN billing = 'api' THEN usd END), 0)     AS project_usd,
          COALESCE(SUM(CASE WHEN billing = 'api'
                             AND created_at >= date('now')
                            THEN usd END), 0)                          AS today_usd,
          COUNT(*)                                                     AS events
        FROM spend_event
    """).fetchone()
    out = dict(row)
    by_kind = conn.execute(
        "SELECT kind, COALESCE(SUM(usd), 0) AS usd FROM spend_event GROUP BY kind"
    ).fetchall()
    out["by_kind"] = {r["kind"]: round(r["usd"], 4) for r in by_kind}
    out["project_usd"] = round(out["project_usd"], 4)
    out["today_usd"] = round(out["today_usd"], 4)
    out["subscription"] = subscription_totals(root)
    out["budget"] = budget(root)
    return out


def subscription_totals(root: str | os.PathLike[str]) -> dict:
    """Agent-session usage: notional dollars, and the tokens that are not.

    Reported apart from :func:`totals` because these two numbers answer
    different questions. ``usd`` sizes a run against what the API would have
    charged. ``*_tokens`` is what the subscription's rolling window actually
    meters, and cache_read dominates it — a long agent run re-sends its whole
    context every turn, so turn 90 is billed for the 89 before it. A night that
    moved 1.19 billion input-side tokens showed up here as a few hundred
    notional dollars and as an exhausted allowance.
    """
    row = db.connect(root).execute("""
        SELECT
          COALESCE(SUM(usd), 0)                                  AS usd,
          COALESCE(SUM(CASE WHEN created_at >= date('now')
                            THEN usd END), 0)                    AS today_usd,
          COALESCE(SUM(input_tokens), 0)                         AS input_tokens,
          COALESCE(SUM(output_tokens), 0)                        AS output_tokens,
          COALESCE(SUM(cache_read_tokens), 0)                    AS cache_read_tokens,
          COALESCE(SUM(cache_write_tokens), 0)                   AS cache_write_tokens,
          COALESCE(SUM(CASE WHEN created_at >= date('now') THEN
                        input_tokens + cache_read_tokens
                        + cache_write_tokens END), 0)            AS today_input_tokens,
          COUNT(*)                                               AS runs
        FROM spend_event WHERE billing = 'subscription'
    """).fetchone()
    out = {k: row[k] for k in row.keys()}
    out["usd"] = round(out["usd"], 4)
    out["today_usd"] = round(out["today_usd"], 4)
    # The number to watch. Output is a rounding error next to it: the same night
    # was 1,192,624,529 input-side against 77,234 out.
    out["input_side_tokens"] = (out["input_tokens"] + out["cache_read_tokens"]
                                + out["cache_write_tokens"])
    return out


def for_logical(root: str | os.PathLike[str], logical_name: str) -> float:
    """Total spent on one logical asset — the number the art lab header shows."""
    row = db.connect(root).execute(
        "SELECT COALESCE(SUM(usd), 0) AS usd FROM spend_event WHERE logical_name = ?",
        (logical_name,)).fetchone()
    return round(row["usd"], 4) if row else 0.0


def check(root: str | os.PathLike[str], *, projected_usd: float = 0.0) -> dict:
    """Would spending ``projected_usd`` now breach a ceiling?

    Returns ``{allowed, reason, ...}``. Callers refuse on ``allowed=False`` —
    this is the gate that makes a 20-item overnight fan-out bounded.

    THE DAY AND PROJECT CEILINGS NOW MEASURE REAL MONEY ONLY (see
    :func:`totals`). Agent sessions are bounded by per-item cost, by
    dispatch.max_turns and by max_concurrent, all of which act on the run
    itself; charging them against a dollar ceiling meant an evening of
    subscription work could lock out an image generation that actually costs
    something, while doing nothing to slow the agents down.
    """
    b = budget(root)
    if not b or not b.get("enforced"):
        return {"allowed": True, "reason": "", "enforced": False}
    t = totals(root)
    day, project = t["today_usd"], t["project_usd"]

    if b["per_day_usd"] and day + projected_usd > b["per_day_usd"]:
        return {"allowed": False, "enforced": True,
                "reason": f"daily budget reached — ${day:.2f} spent today of "
                          f"${b['per_day_usd']:.2f}",
                "scope": "day", "spent": day, "ceiling": b["per_day_usd"]}
    if b["per_project_usd"] and project + projected_usd > b["per_project_usd"]:
        return {"allowed": False, "enforced": True,
                "reason": f"project budget reached — ${project:.2f} spent of "
                          f"${b['per_project_usd']:.2f}",
                "scope": "project", "spent": project,
                "ceiling": b["per_project_usd"]}
    return {"allowed": True, "reason": "", "enforced": True,
            "today_usd": day, "project_usd": project}


def item_ceiling(root: str | os.PathLike[str], item: dict) -> float:
    """The per-run cost ceiling for one item: its own override, else the default."""
    override = (item or {}).get("max_cost_usd")
    if override:
        return float(override)
    return float(budget(root).get("per_item_usd") or 0)


def runtime_ceiling(root: str | os.PathLike[str], item: dict) -> int:
    override = (item or {}).get("max_runtime_s")
    if override:
        return int(override)
    return int(budget(root).get("max_runtime_s") or 0)
