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
import uuid
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

# Agent sessions run on the subscription; everything else buys from a vendor.
#
# UNCONDITIONAL, AND THAT IS THE DECISION RATHER THAN AN OVERSIGHT (2026-08-11).
# It means agent runs never count against per_day_usd / per_project_usd, which
# reads like a hole in the ceiling and has been reported as one. It is not:
# every agent this harness spawns today runs on a subscription CLI, so its
# dollar figure is the API-EQUIVALENT price of work already paid for, and
# summing it into a budget meant an afternoon of uncharged agent work refused
# an image generation that would have cost real money.
#
# WHAT WOULD CHANGE IT: a direct-API or local-model runner, which is wanted and
# not built. When one exists, billing becomes a property of the RUNNER rather
# than of the kind — the row is already shaped for that (spend_event.billing is
# per row, not per kind) and `totals` already reports agent runs separately, so
# the change is here and in the runner table, not in the schema.
#
# Until then this line is deliberate. Do not "fix" it.
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
           detail: str = "", model: str = "", tokens: Optional[dict] = None,
           seat: str = "") -> None:
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
    # WHO SPENT IT, taken from the environment when the caller did not say.
    # Every paid tool runs inside a seat's MCP server process (BGATE_SEAT is
    # set by dispatch), so the attribution is available for free at the one
    # place that writes the row — which is why nearly every call site can stay
    # unchanged and still start answering "which seat is expensive".
    seat = (seat or os.environ.get("BGATE_SEAT", "") or "").strip()[:32]
    try:
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO spend_event (kind, billing, work_item_id, "
                "logical_name, usd, detail, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, seat) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, billing, work_item_id, logical_name, max(0.0, float(usd or 0)),
                 detail[:500], model[:80], counts["input"], counts["output"],
                 counts["cache_read"], counts["cache_write"], seat))
            if work_item_id and usd and usd > 0:
                conn.execute(
                    "UPDATE work_item SET total_cost_usd = total_cost_usd + ? "
                    "WHERE id = ?", (float(usd), work_item_id))
    except Exception:
        pass


def record_unpriced(root: str | os.PathLike[str], credits, *, kind: str = "other",
                    work_item_id: Optional[int] = None, logical_name: str = "",
                    detail: str = "", model: str = "", seat: str = "") -> None:
    """A REAL charge whose dollar figure cannot be known. Never raises.

    kie bills in credits and publishes no credit-to-dollar rate; unless the
    human sets BGATE_KIE_USD_PER_CREDIT there is no honest USD number to
    record. Before this, such a call wrote NOTHING — the provider's biggest
    spender left no ledger row, and with budgets off by default the report is
    the whole product, so the totals read low exactly when kie was the main
    provider.

    The row lands with ``usd = 0`` and a machine-readable ``detail`` prefix
    (:data:`_UNPRICED_PREFIX` + the credit count, or ``?`` when even the
    credits are unknown). Zero is safe here BECAUSE the marker exists:
    :func:`totals` reports these rows apart as ``unaccounted`` rather than
    letting them read as free, which is the failure a bare $0.00 row causes.
    No dollar figure is invented — the rate is the human's to declare.
    """
    if kind not in KINDS:
        kind = "other"
    try:
        amount = float(credits)
        if amount < 0:
            raise ValueError
        stamp = f"{amount:g}"
    except (TypeError, ValueError):
        stamp = "?"
    text = _UNPRICED_PREFIX + stamp + (f" — {detail}" if detail else "")
    seat = (seat or os.environ.get("BGATE_SEAT", "") or "").strip()[:32]
    try:
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO spend_event (kind, billing, work_item_id, "
                "logical_name, usd, detail, model, seat) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (kind, API, work_item_id, logical_name, text[:500],
                 model[:80], seat))
    except Exception:
        pass


# The marker an unpriced row carries in `detail`. The credits ride in the
# detail column because they are provenance, not money — a numeric column for
# a unit only one provider uses would be schema spent on a footnote.
_UNPRICED_PREFIX = "unpriced credits="


def _unaccounted(conn) -> dict:
    """The rows :func:`record_unpriced` wrote, summarised for :func:`totals`."""
    rows = conn.execute(
        "SELECT detail, (created_at >= date('now')) AS today FROM spend_event "
        "WHERE billing = 'api' AND usd <= 0 AND detail LIKE ?",
        (_UNPRICED_PREFIX + "%",)).fetchall()
    credits, unknown, today_rows = 0.0, 0, 0
    for row in rows:
        stamp = str(row["detail"])[len(_UNPRICED_PREFIX):].split(" ", 1)[0]
        try:
            credits += float(stamp)
        except ValueError:
            unknown += 1
        if row["today"]:
            today_rows += 1
    return {
        "rows": len(rows),
        "credits": round(credits, 4),
        "credits_unknown_rows": unknown,
        "today_rows": today_rows,
        "note": (f"{len(rows)} kie call(s) were charged in credits with no "
                 "dollar rate configured — they are NOT in project_usd or "
                 "today_usd. Set BGATE_KIE_USD_PER_CREDIT to your account's "
                 "rate to price future calls." if rows else ""),
    }


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

    ``unaccounted`` is the third bucket and it is REAL MONEY WITH NO NUMBER:
    kie calls charged in credits under no configured dollar rate (see
    :func:`record_unpriced`). They are deliberately not summed into
    ``project_usd``/``today_usd`` — inventing a rate would be worse than the
    gap — but with budgets off by default the report IS the product, so a
    total that silently omits them reads low exactly when kie is the main
    provider. Every consumer that prints a total should say
    "+ N unpriced kie rows" when ``unaccounted.rows`` is nonzero.
    """
    conn = db.connect(root)
    # THE WINDOWS USED TO BE LIFETIME AND TODAY, AND NOTHING ELSE — so anyone
    # coming back after a break asked "what did last month cost" and got a
    # today figure of $0.00, which reads as "cheap" rather than "not measured".
    # 7 and 30 days are the two windows a person actually asks about.
    row = conn.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN billing = 'api' THEN usd END), 0)     AS project_usd,
          COALESCE(SUM(CASE WHEN billing = 'api'
                             AND created_at >= date('now')
                            THEN usd END), 0)                          AS today_usd,
          COALESCE(SUM(CASE WHEN billing = 'api'
                             AND created_at >= date('now', '-7 day')
                            THEN usd END), 0)                          AS week_usd,
          COALESCE(SUM(CASE WHEN billing = 'api'
                             AND created_at >= date('now', '-30 day')
                            THEN usd END), 0)                          AS month_usd,
          COUNT(*)                                                     AS events
        FROM spend_event
    """).fetchone()
    out = dict(row)
    out["week_usd"] = round(out["week_usd"], 4)
    out["month_usd"] = round(out["month_usd"], 4)
    # AGENT RUNS ARE NOT IN ANY OF THE ABOVE, and that surprised people: kind
    # 'agent' is billed 'subscription' unconditionally, so the ceilings and the
    # headline dollar figure both exclude the single biggest thing a night of
    # fan-out consumes. Reported alongside rather than folded in — a plan user's
    # runs genuinely are not dollars — but never again invisible.
    agent = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) AS usd, COUNT(*) AS runs "
        "FROM spend_event WHERE kind = 'agent'").fetchone()
    out["agent_runs"] = {
        "runs": int(agent["runs"] or 0),
        "usd": round(float(agent["usd"] or 0), 4),
        "note": "agent sessions bill as subscription, so they are NOT counted "
                "in project_usd/today_usd or against per_day_usd — the token "
                "figures below are what actually runs out on a plan",
    }
    by_kind = conn.execute(
        "SELECT kind, COALESCE(SUM(usd), 0) AS usd FROM spend_event GROUP BY kind"
    ).fetchall()
    out["by_kind"] = {r["kind"]: round(r["usd"], 4) for r in by_kind}
    # BY SEAT — the cut that decides where a budget is actually reduced. Rows
    # with no seat (a human's own session) are grouped under '' rather than
    # dropped: an unattributed total that silently vanishes is how a by-seat
    # view ends up not summing to the project total.
    by_seat = conn.execute(
        "SELECT seat, COALESCE(SUM(usd), 0) AS usd, COUNT(*) AS events "
        "FROM spend_event GROUP BY seat ORDER BY usd DESC").fetchall()
    out["by_seat"] = {(r["seat"] or "(unattributed)"):
                      {"usd": round(r["usd"], 4), "events": int(r["events"])}
                      for r in by_seat}
    out["project_usd"] = round(out["project_usd"], 4)
    out["today_usd"] = round(out["today_usd"], 4)
    out["unaccounted"] = _unaccounted(conn)
    out["subscription"] = subscription_totals(root)
    out["budget"] = budget(root)
    # ONE FIGURE EVERY CONSUMER CAN PRINT, so nobody has to remember to add the
    # footnote. `board_digest` reported "11 kie call(s) were charged in credits
    # with no dollar rate configured — they are NOT in project_usd or
    # today_usd", and 84 credits were invisible to every spend figure and to
    # every budget ceiling. A total that silently excludes a channel is worse
    # than no total: it reads as complete.
    #
    # No rate is invented. The combined figure is a STRING because that is the
    # honest shape of "$4.12 + 84 credits" — a number would have to pretend the
    # credits were dollars or pretend they were nothing, and both are lies that
    # have already been told here.
    out["spend_line"] = spend_line(out["project_usd"], out["unaccounted"])
    out["today_spend_line"] = spend_line(
        out["today_usd"], out["unaccounted"], today=True)
    out["complete"] = not out["unaccounted"]["rows"]
    return out


def spend_line(usd: float, unaccounted: dict, today: bool = False) -> str:
    """"$4.12 + 84 unpriced credits (11 calls)" — the total, whole.

    THE ONE FORMATTER. Every surface that prints money uses it, which is what
    makes the omission impossible rather than merely documented: the doctrine
    "every consumer should say + N unpriced kie rows" was written down and then
    honoured by nobody, because it lived in a comment rather than in a function
    anybody had to call.
    """
    rows = int((unaccounted or {}).get("today_rows" if today else "rows") or 0)
    if not rows:
        return f"${float(usd):.2f}"
    credits = float((unaccounted or {}).get("credits") or 0)
    unknown = int((unaccounted or {}).get("credits_unknown_rows") or 0)
    tail = (f"{credits:g} unpriced credits" if credits else "unpriced credits")
    if unknown:
        tail += f", {unknown} of unknown size"
    return (f"${float(usd):.2f} + {tail} ({rows} call"
            + ("s" if rows != 1 else "") + " with no dollar rate configured; "
            "set BGATE_KIE_USD_PER_CREDIT to price them)")


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

    # THE CHANNEL THE CEILING CANNOT SEE. kie bills in credits, and without a
    # declared rate there is no honest dollar figure — so 84 credits across 11
    # calls were invisible to `day` and `project` above, and therefore to both
    # ceilings. A budget that silently ignores a spend channel is not a budget.
    #
    # No rate is invented here either. What changes is that the omission
    # travels WITH the verdict, so a caller that prints "under budget" prints
    # the qualification too, and a refusal explains what it did and did not
    # count.
    unpriced = t["unaccounted"]
    blind = int(unpriced.get("rows") or 0)
    caveat = ("" if not blind else
              f"NOT COUNTED: {blind} call(s) billed in credits "
              f"({unpriced.get('credits', 0):g}) with no dollar rate "
              "configured, so this ceiling cannot see them. Set "
              "BGATE_KIE_USD_PER_CREDIT to bring them inside it.")

    if b["per_day_usd"] and day + projected_usd > b["per_day_usd"]:
        return {"allowed": False, "enforced": True,
                "reason": f"daily budget reached — {spend_line(day, unpriced, today=True)}"
                          f" spent today of ${b['per_day_usd']:.2f}",
                "scope": "day", "spent": day, "ceiling": b["per_day_usd"],
                "unpriced_rows": blind, "blind_spot": caveat}
    if b["per_project_usd"] and project + projected_usd > b["per_project_usd"]:
        return {"allowed": False, "enforced": True,
                "reason": f"project budget reached — {spend_line(project, unpriced)}"
                          f" spent of ${b['per_project_usd']:.2f}",
                "scope": "project", "spent": project,
                "ceiling": b["per_project_usd"],
                "unpriced_rows": blind, "blind_spot": caveat}
    return {"allowed": True, "reason": "", "enforced": True,
            "today_usd": day, "project_usd": project,
            "spend_line": spend_line(project, unpriced),
            "today_spend_line": spend_line(day, unpriced, today=True),
            "unpriced_rows": blind, "blind_spot": caveat}


# How long a hold survives without being released. Generous on purpose: a
# single kie video job legitimately runs for minutes, and a hold that expired
# under its own call would stop counting exactly when the money is most likely
# to be spent. Short enough that a process killed by the runtime ceiling does
# not hold the budget hostage for the rest of the session.
HOLD_TTL_S = 1800


def _reap_holds(conn) -> None:
    """Drop holds whose holder died. SETTLED rows are not holds any more - they
    are what a credit-billed call actually consumed of the run's ceiling, and
    reaping those would hand the money back to a run that already spent it."""
    conn.execute("DELETE FROM spend_hold WHERE settled = 0 "
                 "AND expires_at <= datetime('now')")


def held_usd(root: str | os.PathLike[str], *,
             work_item_id: Optional[int] = None) -> float:
    """Money reserved by calls that have not finished paying yet."""
    try:
        with db.tx(root) as write:
            _reap_holds(write)
    except Exception:
        pass
    sql = "SELECT COALESCE(SUM(usd), 0) AS usd FROM spend_hold"
    args: tuple = ()
    if work_item_id is not None:
        sql += " WHERE work_item_id = ?"
        args = (int(work_item_id),)
    try:
        row = db.connect(root).execute(sql, args).fetchone()
    except Exception:
        return 0.0
    return round(float(row["usd"] or 0), 4)


def spent_on_item(root: str | os.PathLike[str], work_item_id: int) -> float:
    """REAL money this work item has already been charged, holds excluded.

    api-billed rows only, for the same reason :func:`totals` draws that line:
    an agent session's dollar figure is the API-equivalent price of work a
    subscription already covers, and counting it here would refuse an image
    generation on behalf of tokens nobody was invoiced for.

    ``work_item.total_cost_usd`` is deliberately NOT the source: it sums every
    kind including the agent session, which is precisely the number that must
    not gate a purchase.
    """
    try:
        row = db.connect(root).execute(
            "SELECT COALESCE(SUM(usd), 0) AS usd FROM spend_event "
            "WHERE billing = 'api' AND work_item_id = ?",
            (int(work_item_id),)).fetchone()
    except Exception:
        return 0.0
    return round(float(row["usd"] or 0), 4)


def reserve(root: str | os.PathLike[str], usd: float, *,
            work_item_id: Optional[int] = None, what: str = "",
            run_ceiling_usd: float = 0.0, seat: str = "") -> dict:
    """Take money out of the budget BEFORE the provider is called.

    ``{"ok": True, "token": ...}`` or ``{"ok": False, "reason": ..., ...}``.

    THIS IS WHAT MAKES A CEILING A CEILING. The spend gate used to ask "is THIS
    call under the ceiling", which every one of eighteen forty-cent calls
    answers yes to against a five dollar cap - measured, three benchmark games,
    $6.40 / $9.49 / $5.16 against a $5 max_cost_usd, while the runtime ceiling
    stopped work every time. The question here is "does this call fit in what
    is LEFT", and the arithmetic is:

        already recorded for this item  +  every live hold  +  this estimate

    against the run ceiling, and the same sum project-wide against the day and
    project budgets. The check and the insert share ONE transaction, so two
    processes racing cannot both be told there is room: the second reads the
    first one's hold.

    FAIL-OPEN ON ITS OWN FAULTS, like every other explanatory gate in this
    product - a project whose migration has not run, or whose DB is locked,
    gets an unenforced pass rather than a stopped pipeline. The failure mode of
    a broken budget must be an unrecorded purchase, never a studio that cannot
    generate.
    """
    projected = round(max(0.0, float(usd or 0.0)), 4)
    ceiling = round(max(0.0, float(run_ceiling_usd or 0.0)), 4)
    seat = (seat or os.environ.get("BGATE_SEAT", "") or "").strip()[:32]
    token = "%d-%s" % (os.getpid(), uuid.uuid4().hex[:16])
    try:
        with db.tx(root) as conn:
            _reap_holds(conn)
            if ceiling and work_item_id:
                spent = float(conn.execute(
                    "SELECT COALESCE(SUM(usd), 0) FROM spend_event "
                    "WHERE billing = 'api' AND work_item_id = ?",
                    (int(work_item_id),)).fetchone()[0] or 0)
                # LIVE holds and SETTLED estimates both count. A settled row
                # is a call that really happened and really consumed provider
                # credit while reporting no dollars - see the spend_hold
                # migration. Counting it is the only thing that makes a dollar
                # ceiling bind a credit-billed provider without inventing a
                # rate for it.
                pending = float(conn.execute(
                    "SELECT COALESCE(SUM(usd), 0) FROM spend_hold "
                    "WHERE work_item_id = ?",
                    (int(work_item_id),)).fetchone()[0] or 0)
                if spent + pending + projected > ceiling:
                    return {
                        "ok": False, "scope": "run", "spent": round(spent, 4),
                        "held": round(pending, 4), "estimated_usd": projected,
                        "ceiling": ceiling,
                        "reason": "the $%.2f ceiling for this run is spent: "
                                  "$%.2f already charged%s, and %s is estimated "
                                  "at $%.2f. Raise max_cost_usd on the item if "
                                  "the work genuinely needs it - a human "
                                  "decides that, not a retry"
                                  % (ceiling, spent,
                                     (", $%.2f in flight" % pending) if pending
                                     else "", what or "this call", projected),
                    }
            budget_row = budget(root)
            if budget_row and budget_row.get("enforced"):
                pending_all = float(conn.execute(
                    "SELECT COALESCE(SUM(usd), 0) FROM spend_hold"
                ).fetchone()[0] or 0)
                sums = conn.execute(
                    "SELECT COALESCE(SUM(usd), 0) AS project_usd, "
                    "COALESCE(SUM(CASE WHEN created_at >= date('now') "
                    "THEN usd END), 0) AS today_usd "
                    "FROM spend_event WHERE billing = 'api'").fetchone()
                day = float(sums["today_usd"] or 0) + pending_all
                project = float(sums["project_usd"] or 0) + pending_all
                per_day = float(budget_row.get("per_day_usd") or 0)
                per_project = float(budget_row.get("per_project_usd") or 0)
                if per_day and day + projected > per_day:
                    return {"ok": False, "scope": "day", "spent": round(day, 4),
                            "held": round(pending_all, 4), "ceiling": per_day,
                            "estimated_usd": projected,
                            "reason": "daily budget reached - $%.2f of $%.2f "
                                      "committed today" % (day, per_day)}
                if per_project and project + projected > per_project:
                    return {"ok": False, "scope": "project",
                            "spent": round(project, 4),
                            "held": round(pending_all, 4),
                            "ceiling": per_project,
                            "estimated_usd": projected,
                            "reason": "project budget reached - $%.2f of $%.2f "
                                      "committed" % (project, per_project)}
            baseline, baseline_rows = 0.0, 0
            if work_item_id:
                mark = conn.execute(
                    "SELECT COALESCE(SUM(usd), 0) AS usd, COUNT(*) AS rows "
                    "FROM spend_event WHERE billing = 'api' "
                    "AND work_item_id = ?", (int(work_item_id),)).fetchone()
                baseline = float(mark["usd"] or 0)
                baseline_rows = int(mark["rows"] or 0)
            conn.execute(
                "INSERT INTO spend_hold (token, work_item_id, usd, what, seat, "
                "baseline_usd, baseline_rows, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, "
                "datetime('now', '+" + str(int(HOLD_TTL_S)) + " seconds'))",
                (token, work_item_id, projected, str(what or "")[:200], seat,
                 baseline, baseline_rows))
    except Exception:
        return {"ok": True, "token": "", "enforced": False}
    return {"ok": True, "token": token, "enforced": True,
            "estimated_usd": projected}


def release(root: str | os.PathLike[str], token: str) -> bool:
    """Close out a hold once its call has finished. Never raises.

    THREE OUTCOMES, and which one applies is MEASURED against the ledger rather
    than guessed, by comparing it to the baseline taken when the hold opened:

      a dollar figure landed   the provider priced the call, that number is in
                               the ledger, and it is the truth - the hold is
                               dropped so nothing double-counts.
      a row landed with no
      dollars                  the call happened and consumed provider credit
                               while reporting no price (kie bills in credits
                               and this product refuses to invent a rate). The
                               hold SETTLES at the estimate the gate already
                               used, so the run ceiling moves by what the call
                               plausibly cost instead of by zero.
      nothing landed           the call failed before billing. The hold is
                               dropped: a run must not lose budget to work that
                               never happened.

    A hold whose process dies before reaching here expires on its own; see
    migration 0042.
    """
    if not token:
        return False
    try:
        with db.tx(root) as conn:
            row = conn.execute(
                "SELECT work_item_id, baseline_usd, baseline_rows "
                "FROM spend_hold WHERE token = ?", (token,)).fetchone()
            if row is None:
                return False
            item = row["work_item_id"]
            settle = False
            if item:
                now = conn.execute(
                    "SELECT COALESCE(SUM(usd), 0) AS usd, COUNT(*) AS rows "
                    "FROM spend_event WHERE billing = 'api' "
                    "AND work_item_id = ?", (int(item),)).fetchone()
                priced = float(now["usd"] or 0) > float(
                    row["baseline_usd"] or 0) + 1e-9
                charged = int(now["rows"] or 0) > int(row["baseline_rows"] or 0)
                settle = charged and not priced
            if settle:
                conn.execute(
                    "UPDATE spend_hold SET settled = 1 WHERE token = ?",
                    (token,))
            else:
                conn.execute("DELETE FROM spend_hold WHERE token = ?", (token,))
        return True
    except Exception:
        return False


def item_ceiling(root: str | os.PathLike[str], item: dict) -> float:
    """The per-run cost ceiling for one item. 0 means uncapped.

    An item's OWN max_cost_usd always acts - a human (or the brief) set that
    number on that item deliberately. The budget row's per_item_usd default
    acts only when the budget is ENFORCED: with enforcement off (the
    default), the numbers are reports, and a default ceiling that kept
    killing runs nobody asked it to bound was exactly the "budget gate on by
    default" complaint that flipped budget.enforced off.
    """
    override = (item or {}).get("max_cost_usd")
    if override:
        return float(override)
    b = budget(root)
    if not b.get("enforced"):
        return 0.0
    return float(b.get("per_item_usd") or 0)


def runtime_ceiling(root: str | os.PathLike[str], item: dict) -> int:
    override = (item or {}).get("max_runtime_s")
    if override:
        return int(override)
    return int(budget(root).get("max_runtime_s") or 0)
