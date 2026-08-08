"""Every switch in one table — described, not moved.

Four features each added a switch in a different mechanism: a column on
``spend_budget``, a workspace doc, an env var read inline where it was needed,
and a module constant. Nothing listed them, nothing said which one the
environment was overriding, and adding the fifth meant editing a route, a
payload shape and a template. That pile is what this module deletes.

WHAT THIS IS NOT: a new store. Every :class:`Setting` carries a ``store``
locator pointing at where its value ALREADY lives, so upgrading changes no data
and no other module's reads. ``gate.mode`` is still the ``director/gate``
workspace doc; ``dispatch.max_concurrent`` is still the ``spend_budget`` row.
Move the storage and every existing reader silently starts reading a stale
value — the one failure mode a settings refactor cannot recover from.

The three stores, and why there are three rather than one:

    workspace   an existing per-seat JSON doc with its own established shape
                (``director/gate``, ``director/autopilot``). Versioned writes,
                so two tabs cannot erase each other.
    budget      a column on the single ``spend_budget`` row, because
                ``spend.check`` reads it on every dispatch and must keep
                reading exactly that.
    registry    the shared ``director/settings`` doc, for switches whose only
                previous home was a module constant. The registry default IS
                that constant's old value, so a project with no doc behaves
                identically to one that never upgraded.

PRECEDENCE IS env > stored > default, and :func:`describe` always reports which
one won. A panel that shows "agent" while ``BGATE_QA_GATE=0`` forces "none" is
the most expensive lie a settings surface can tell — somebody debugs the gate
for an hour before finding the shell profile.

TWO KINDS OF ENV OVERRIDE, because the vars that already exist are not one kind:

    env=NAME              the var SUPPLIES the value (BGATE_GIT_ISOLATION=1).
    env_coerce=(NAME, f)  the var FORCES a value (BGATE_QA_GATE=0 forces
                          gate.mode to "none").

Modelling the second as the first is a type mismatch: ``BGATE_QA_GATE`` is a
boolean kill switch and ``gate.mode`` is one of three strings, so a boolean
cannot be *read as* the value. Coercion wins over supply when both are set,
because a kill switch that loses to a preference is not a kill switch.

VALIDATION LIVES IN ONE PLACE (:func:`coerce`). Every writer — the settings
endpoint, the budget alias, the console's inline controls — goes through it, so
a range only has to be right once.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from . import db, workspace as _ws

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
BOOL, ENUM, INT, FLOAT, STRING, LIST = "bool", "enum", "int", "float", "string", "list"
KINDS = (BOOL, ENUM, INT, FLOAT, STRING, LIST)

# Where a value came from. The UI greys out a field whose source is "env".
SOURCE_DEFAULT, SOURCE_STORED, SOURCE_ENV = "default", "stored", "env"

# Scope, for the sentence a row prints next to itself. "machine" means the
# switch describes this checkout/host rather than the game, so copying the
# project elsewhere must not be expected to carry it.
PROJECT, MACHINE = "project", "machine"

# The doc that holds switches whose previous home was a module constant. One
# doc, not one per key: workspace docs are whole-document read-modify-writes,
# so twelve docs would mean twelve version preconditions for one Settings save.
REGISTRY_SEAT = "director"
REGISTRY_KEY = "settings"

# Group order is the display order, in both the panel and `bgate doctor`.
GROUPS = ("Dispatch", "Gates", "Art", "Follow-up", "Notifications",
          "Budget", "Console", "Privacy")

# The event vocabulary a notification can be asked for. Kept here rather than
# imported from events.py so that a settings panel still renders when the event
# table is mid-migration — a settings surface that needs the rest of the system
# healthy is useless exactly when it is needed.
# It must stay in step with events.KINDS: a kind that is emitted but absent here
# has no checkbox, so it can never be added to notify.kinds and the coerce()
# below REFUSES it — which is how chain.filed was emitted by queue.add_chain and
# unselectable in the panel at the same time.
EVENT_KINDS = ("item.done", "item.review", "item.failed", "item.approved",
               "item.rejected", "item.aging", "chain.filed", "chain.advanced",
               "chain.stalled", "gate.mode", "settings.guard", "style.trained",
               "budget.refused",
               "director.question", "agent.spawned", "agent.exited",
               "file.edited")

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


class SettingError(ValueError):
    """A rejected value or an unknown key.

    Subclasses ValueError so the existing routes, which already translate a
    ValueError into a 400, keep working without knowing this module exists.
    """


def _falsey(raw: str) -> bool:
    return str(raw or "").strip().lower() in _FALSE


@dataclass(frozen=True)
class Setting:
    """One switch: what it is, where it lives, and who may override it.

    Without this record the same switch is described three times — in the
    reader, in the route's validation, and in the template that renders it —
    and the three drift. The UI renders FROM this, so a new toggle is one entry
    here and no UI change.
    """

    key: str
    group: str
    kind: str
    default: Any
    help: str
    # ("workspace", seat, doc_key, field) | ("budget", column) | ("registry", field)
    store: tuple
    choices: tuple = ()
    scope: str = PROJECT
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    # The var SUPPLIES the value.
    env: str = ""
    # (NAME, fn) — the var FORCES a value. fn(raw) returns the forced value, or
    # None for "this var has no opinion here".
    env_coerce: Optional[tuple] = None
    # One line for the row when the env var is doing something non-obvious.
    env_note: str = ""
    # This switch WIDENS a safety guard rather than tuning behaviour, so turning
    # it on costs the studio a protection it had. `dispatch.allow_dirty` is the
    # case that forced the flag: it used to need an env var, and describing it
    # here made it one click in a browser — an agent still cannot flip it (the
    # PATCH demands a human actor), but a human can now do it by accident, and
    # "an agent's edits are indistinguishable from mine in the diff" is not a
    # thing to discover afterwards. The UI confirms these; the audit records
    # them either way.
    guard: bool = False
    # A MACHINE MAY NOT CHANGE THIS ONE AT ALL. Not a widened guard — a
    # constraint the harness advertises as ENFORCED, which is worth nothing if
    # the thing being constrained can switch it off.
    #
    # MEASURED over one overnight run: gate.mode was found reverted from "agent"
    # to "none" on four separate occasions with no human action, budget.enforced
    # was found off, and dispatch.max_concurrent went from the 4 a human set to
    # 9 and then 11. Three items reached done with no reviewer ever spawned,
    # including a rigged character whose bind weights nobody checked — the exact
    # failure class the gate exists to catch. `seat_configure` already refuses a
    # machine the write lanes, on the reasoning that "a lane change that comes
    # from a machine is not a lane system, it is a suggestion"; an agent turning
    # off its own reviewer is the same act, and these switches were the half of
    # that policy nobody had written down.
    #
    # DECLARED HERE, not enforced per route, because the hole was precisely that
    # one write path did not know it was a policy boundary.
    human_only: bool = False

    def env_vars(self) -> list[str]:
        """Every var that can take this setting away from the stored value."""
        names = []
        if self.env_coerce:
            names.append(self.env_coerce[0])
        if self.env:
            names.append(self.env)
        return names


# ---------------------------------------------------------------------------
# The registry. Defaults for anything that used to be a module constant MUST be
# that constant's current value: an upgrade that changes behaviour because a
# setting appeared is worse than the setting not existing.
# ---------------------------------------------------------------------------
SETTINGS: tuple[Setting, ...] = (
    # -- Dispatch -----------------------------------------------------------
    Setting(
        key="autopilot.on", group="Dispatch", kind=BOOL, default=False,
        store=("workspace", "director", "autopilot", "on"),
        env_coerce=("BGATE_AUTODEPLOY", lambda raw: False if _falsey(raw) else None),
        env_note="BGATE_AUTODEPLOY=0 stops the loop from starting at all, so the "
                 "stored switch cannot take effect until the server restarts",
        help="Dispatch queued work automatically as slots free up, instead of "
             "waiting for somebody to press deploy on each item."),
    Setting(
        key="dispatch.allow_dirty", group="Dispatch", kind=BOOL, default=False,
        store=("registry", "dispatch.allow_dirty"), scope=MACHINE,
        env="BGATE_ALLOW_DIRTY", guard=True, human_only=True,
        help="Let an agent be dispatched on top of uncommitted changes. Off, "
             "because the resulting diff cannot tell the agent's edits from "
             "yours — which is what makes a revert safe."),
    Setting(
        key="dispatch.isolation", group="Dispatch", kind=BOOL, default=False,
        store=("registry", "dispatch.isolation"), scope=MACHINE,
        env="BGATE_GIT_ISOLATION",
        env_note="the var is BGATE_GIT_ISOLATION, which is what gitwork.py has "
                 "always read",
        help="Run each agent in a private git worktree. Off: a worktree moves "
             "the agent's cwd, and base_commit + diff + revert already work "
             "without that surprise."),
    Setting(
        key="dispatch.max_concurrent", group="Dispatch", kind=INT, default=4,
        minimum=1, maximum=32, store=("budget", "max_concurrent"),
        human_only=True,
        help="How many agents may run at once. The dispatcher refuses past "
             "this, which is what stops a fan-out from eating the machine. "
             "Machine-writable would make it self-service: observed going from "
             "the 4 a human set to 9 and then 11 inside one run."),

    # -- Gates --------------------------------------------------------------
    Setting(
        key="gate.mode", group="Gates", kind=ENUM, default="agent",
        choices=("none", "agent", "builders"),
        store=("workspace", "director", "gate", "mode"),
        # NOT env="BGATE_QA_GATE": that var is a boolean kill switch and cannot
        # supply one of three modes.
        env_coerce=("BGATE_QA_GATE", lambda raw: "none" if _falsey(raw) else None),
        env_note="BGATE_QA_GATE=0 forces no gate — the legacy kill switch, which "
                 "keeps meaning exactly what it always meant",
        human_only=True,
        help="Who signs off before an agent's work counts as done: nobody, the "
             "QA seat, or you. An agent cannot change this: switching off your "
             "own reviewer is the same act as granting yourself the repo."),
    Setting(
        key="qa.max_rounds", group="Gates", kind=INT, default=3,
        minimum=1, maximum=10, store=("registry", "qa.max_rounds"),
        human_only=True,
        help="Rounds of automatic QA an item may go through before a human is "
             "asked to arbitrate. Past that the disagreement is about taste, "
             "and another agent will not settle it — it is a money pump."),
    Setting(
        key="qa.gated_seats", group="Gates", kind=LIST,
        default=("art", "gameplay", "audio", "narrative"),
        choices=("art", "gameplay", "audio", "narrative", "tech"),
        store=("registry", "qa.gated_seats"), human_only=True,
        help="Which maker seats get an automatic QA reviewer when their work "
             "is completed. Was a hardcoded tuple in the gate, so a studio that "
             "wanted QA on art alone had to edit harness source — which changed "
             "it for every project on the machine and needed a restart. "
             "director and qa are never gated: that is recursion, not review."),
    Setting(
        key="signoff.hours", group="Gates", kind=INT, default=8,
        minimum=1, maximum=168, store=("registry", "signoff.hours"),
        help="How long a finished item keeps asking for sign-off in the "
             "console before it stops being surfaced there."),

    # -- Art ----------------------------------------------------------------
    Setting(
        key="art.style_source", group="Art", kind=ENUM, default="refs",
        choices=("refs", "lora"),
        store=("workspace", "art", "styles", "mode"),
        help="Where a generation gets this project's LOOK. `refs` sends the "
             "pinned anchors as style references, which is how it has always "
             "worked. `lora` uses the style trained from those same anchors, "
             "which frees the reference slot to carry IDENTITY instead — the "
             "job it competes with today. Needs a trained style; without one "
             "this falls back to refs rather than generating unanchored."),
    Setting(
        key="art.style_dataset", group="Art", kind=ENUM, default="pins",
        choices=("pins", "assets", "both"),
        store=("workspace", "art", "styles", "source"),
        help="Which shelf a training run draws from. `pins` is the anchors a "
             "human approved through ref_pin — the right default and a small "
             "set. `assets` is the game's own shipped art, which on a project "
             "that has been generating for weeks is hundreds of finished, "
             "in-game pieces nobody re-pinned. Everything still passes the "
             "1024px floor and your confirmation either way."),
    Setting(
        key="art.lora_strength", group="Art", kind=FLOAT, default=0.85,
        minimum=0.0, maximum=1.0, store=("registry", "art.lora_strength"),
        help="How hard the trained style pulls, 0-1. Krea recommends 0.8-0.9; "
             "1.0 is where a style stops being a style and becomes a stamp. A "
             "style's own record can override this per style."),
    Setting(
        key="art.runner", group="Art", kind=ENUM, default="claude",
        choices=("claude", "codex"),
        store=("registry", "art.runner"), scope=MACHINE,
        env="BGATE_ART_RUNNER",
        help="Which CLI the art seat's agents run on. `codex` is here for one "
             "reason: it generates images itself, which is what art.image_"
             "backend switches between. It cannot be steered mid-run and it "
             "reports tokens rather than dollars, so the per-run cost ceiling "
             "does not apply to it — those runs are marked cost-not-tracked "
             "wherever they are shown. Every other seat stays on claude."),
    Setting(
        key="art.image_backend", group="Art", kind=ENUM, default="bgate",
        choices=("bgate", "native"),
        store=("registry", "art.image_backend"),
        env="BGATE_IMAGE_BACKEND",
        help="Who makes the PIXELS, and nothing else. `bgate` is image_generate "
             "and the pipeline behind it: pinned references, the trained style, "
             "consistency_check, the artifact ledger, BGATE_IMAGE_MODEL. "
             "`native` lets a runner that has its own image tool use it — "
             "faster, and outside all of that. Either way the agent still "
             "reads refs, holds locks, checks consistency and registers what it "
             "made; only the generation call changes. On a runner with no image "
             "tool of its own this falls back to `bgate` rather than failing."),
    Setting(
        key="art.auto_approve", group="Art", kind=BOOL, default=False,
        store=("registry", "art.auto_approve"),
        env="BGATE_ART_AUTO_APPROVE",
        help="Let an agent promote its own generated artifact to canon, instead "
             "of every candidate waiting on a human approve/reject. OFF by "
             "default, and the default is the considered position: approval is "
             "the one decision in this pipeline a model may not make, and the "
             "art-QA router exists precisely so the art seat cannot approve its "
             "own drift. Turn it on when the review queue is the bottleneck and "
             "you accept that unreviewed generations reach the build — a "
             "turnaround at four angles per asset registers four candidates, "
             "and that volume is usually the real complaint. Rejection stays "
             "available to agents either way; this only unblocks approval."),

    # -- Follow-up ----------------------------------------------------------
    Setting(
        key="followup.director_debrief", group="Follow-up", kind=BOOL,
        default=False, store=("registry", "followup.director_debrief"),
        help="On completion, file a debrief item for the director, which may "
             "dispatch the follow-up, ask you one question, or close it out. "
             "Off, and it stays off on upgrade: this spends money on every "
             "completed item."),
    Setting(
        key="followup.max_per_hour", group="Follow-up", kind=INT, default=4,
        minimum=1, maximum=60, store=("registry", "followup.max_per_hour"),
        help="Ceiling on debriefs per hour. A busy board finishing twenty "
             "items must not buy twenty director agents."),
    Setting(
        key="followup.max_age_min", group="Follow-up", kind=INT, default=30,
        minimum=1, maximum=1440, store=("registry", "followup.max_age_min"),
        help="Skip the debrief when the completion is older than this. A "
             "subscriber that was down for eight hours must not wake up and "
             "fire eight hours of debriefs at a board that has moved on."),
    Setting(
        key="followup.auto_reopen_failures", group="Follow-up", kind=BOOL,
        default=False, store=("registry", "followup.auto_reopen_failures"),
        help="Reopen a failed item with the failure text instead of leaving it "
             "for a human, up to qa.max_rounds attempts. Off: a failure that "
             "retries itself unattended is how one broken brief spends a "
             "night's budget."),

    # -- Notifications ------------------------------------------------------
    Setting(
        key="notify.in_app", group="Notifications", kind=BOOL, default=True,
        store=("registry", "notify.in_app"),
        help="Light the header bell and fill the drawer. On by default because "
             "it costs nothing and leaves nothing — but it only tells you "
             "things while the dashboard is open."),
    Setting(
        key="notify.kinds", group="Notifications", kind=LIST,
        default=("item.done", "item.failed", "item.review", "chain.stalled",
                 "director.question", "budget.refused"),
        choices=EVENT_KINDS, store=("registry", "notify.kinds"),
        help="Which events are worth telling you about. The rest are still "
             "recorded and still readable in the drawer, they just do not "
             "ring — a bell that rings for everything gets muted."),
    Setting(
        key="notify.webhook", group="Notifications", kind=STRING, default="",
        store=("registry", "notify.webhook"), scope=MACHINE,
        help="POST notifications to this https URL. Empty, and deliberately: "
             "this is the only path that sends anything off the machine, which "
             "breaks the promise the rest of the tool makes. https only, no "
             "private or link-local addresses, one attempt."),
    Setting(
        key="notify.stall_hours", group="Notifications", kind=FLOAT, default=2.0,
        minimum=0.25, maximum=168.0, store=("registry", "notify.stall_hours"),
        help="How long a chain's head may sit in review or blocked before it "
             "is called stalled. The bus is transition-driven, so without this "
             "the quiet failure — nothing happening — emits nothing."),
    Setting(
        key="notify.question_stale_h", group="Notifications", kind=FLOAT,
        default=12.0, minimum=0.25, maximum=168.0,
        store=("registry", "notify.question_stale_h"),
        help="How long an unanswered director question waits before one "
             "reminder. One, not a repeat of the question: a ping that "
             "re-asks is the thing people mute."),
    Setting(
        key="notify.quiet_hours", group="Notifications", kind=STRING, default="",
        store=("registry", "notify.quiet_hours"), scope=MACHINE,
        help="A window like 23:00-07:00 in which nothing is delivered; events "
             "still accumulate and collapse into one notice afterwards. Empty "
             "means always deliver."),

    # -- Budget (the spend_budget row; described here, not copied) ----------
    Setting(
        key="budget.enforced", group="Budget", kind=BOOL, default=True,
        store=("budget", "enforced"), human_only=True,
        help="Refuse a dispatch that would breach a ceiling. Off turns every "
             "number below into a report rather than a limit."),
    Setting(
        key="budget.per_item_usd", group="Budget", kind=FLOAT, default=5.0,
        minimum=0.0, maximum=10000.0, store=("budget", "per_item_usd"),
        human_only=True,
        help="Ceiling for one agent run, in USD. Also the figure the "
             "dispatcher projects against the daily budget before spawning."),
    Setting(
        key="budget.per_day_usd", group="Budget", kind=FLOAT, default=25.0,
        minimum=0.0, maximum=100000.0, store=("budget", "per_day_usd"),
        human_only=True,
        help="Ceiling for today, in USD. 0 means no daily ceiling."),
    Setting(
        key="budget.per_project_usd", group="Budget", kind=FLOAT, default=250.0,
        minimum=0.0, maximum=1000000.0, store=("budget", "per_project_usd"),
        human_only=True,
        help="Lifetime ceiling for this project, in USD. 0 means none."),
    Setting(
        key="budget.max_runtime_s", group="Budget", kind=INT, default=1800,
        minimum=30, maximum=86400, store=("budget", "max_runtime_s"),
        human_only=True,
        help="Wall clock an agent gets before it is killed. The backstop for a "
             "run that is spending without progressing."),

    # -- Console (client-side; delivered in the page bootstrap) -------------
    Setting(
        key="console.poll_live_ms", group="Console", kind=INT, default=3000,
        minimum=500, maximum=60000, store=("registry", "console.poll_live_ms"),
        help="How often the console refreshes while an agent is running. "
             "Lower feels live and costs the dashboard more requests."),
    Setting(
        key="console.poll_idle_ms", group="Console", kind=INT, default=12000,
        minimum=1000, maximum=300000, store=("registry", "console.poll_idle_ms"),
        help="How often the console refreshes when nothing is running."),
    Setting(
        key="graph.phase_cap", group="Console", kind=INT, default=6,
        minimum=1, maximum=50, store=("registry", "graph.phase_cap"),
        help="How many phase rows the graph draws per item before it stops. A "
             "long-running agent otherwise paints a node taller than the "
             "canvas."),

    # -- Privacy ------------------------------------------------------------
    # MACHINE scope, not PROJECT. Whose home directory is on screen is a fact
    # about the person streaming, not about the game — and someone who turns
    # this on for one project and then opens another has not changed their mind
    # about being on camera.
    Setting(
        key="privacy.streamer", group="Privacy", kind=BOOL, default=False,
        store=("registry", "privacy.streamer"), scope=MACHINE,
        env="BGATE_STREAMER",
        env_note="BGATE_STREAMER in the environment wins over this switch, so a "
                 "shell that exports it keeps the filter on no matter what the "
                 "panel says — which is the safe direction for this one",
        help="Hide absolute paths, your username, hostname, email and any API "
             "key from the dashboard, the logs and the CLI. For streaming, "
             "screen-sharing and screenshots. It is a DISPLAY filter: the .env, "
             "the database and devtools are unchanged, and the dashboard's own "
             "auth token is deliberately left alone because the page needs it."),
)

BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}

# The subset the browser needs. It rides in the index page's bootstrap next to
# the cache-busting module srcs rather than costing a second fetch on load, and
# every consumer keeps its hardcoded fallback for a bootstrap that is missing.
CLIENT_KEYS = ("console.poll_live_ms", "console.poll_idle_ms", "graph.phase_cap",
               "notify.in_app")


def keys() -> tuple[str, ...]:
    """Every registered key, in registry order."""
    return tuple(s.key for s in SETTINGS)


def setting(key: str) -> Setting:
    """The record for one key. Raises :class:`SettingError` on an unknown key —
    a typo'd key must fail loudly at the writer rather than silently store a
    value nothing will ever read."""
    try:
        return BY_KEY[key]
    except KeyError:
        raise SettingError(f"unknown setting '{key}'") from None


# ---------------------------------------------------------------------------
# Validation — the one place a value is checked
# ---------------------------------------------------------------------------
def _as_bool(raw: Any, key: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    text = str(raw or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise SettingError(f"{key} must be a true/false value, got {raw!r}")


def _as_number(raw: Any, s: Setting) -> Any:
    try:
        value = int(raw) if s.kind == INT else float(raw)
    except (TypeError, ValueError):
        raise SettingError(f"{s.key} must be a number, got {raw!r}") from None
    if s.minimum is not None and value < s.minimum:
        raise SettingError(f"{s.key} must be at least {s.minimum}, got {value}")
    if s.maximum is not None and value > s.maximum:
        raise SettingError(f"{s.key} must be at most {s.maximum}, got {value}")
    return value


def _as_list(raw: Any, s: Setting) -> list[str]:
    if isinstance(raw, str):
        # A comma-separated string is what an env var and a text input both
        # produce; accepting only a JSON array here means the env override for
        # a list setting is unusable.
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(part).strip() for part in raw]
    else:
        raise SettingError(f"{s.key} must be a list, got {raw!r}")
    items = [part for part in items if part]
    if s.choices:
        unknown = [part for part in items if part not in s.choices]
        if unknown:
            raise SettingError(
                f"{s.key}: unknown value(s) {', '.join(unknown)} — allowed: "
                f"{', '.join(s.choices)}")
    # Order-preserving dedupe: a list that quietly contains the same kind twice
    # notifies twice. dict.fromkeys rather than a set literal because this
    # module defines its own `set` (below) and shadows the builtin.
    return list(dict.fromkeys(items))


def _as_string(raw: Any, s: Setting) -> str:
    text = "" if raw is None else str(raw).strip()
    if s.key == "notify.webhook" and text:
        # Checked here as well as at delivery: a settings panel that accepts an
        # http:// or a 127.0.0.1 URL and then silently never posts to it is the
        # kind of "configured but dead" the whole plan is about. The delivery
        # side still re-checks, because a value can arrive by other paths.
        if not text.lower().startswith("https://"):
            raise SettingError(
                "notify.webhook must be an https:// URL — a plaintext webhook "
                "puts what your agents are doing on the wire")
    if s.key == "notify.quiet_hours" and text:
        if not _valid_window(text):
            raise SettingError(
                "notify.quiet_hours must look like 23:00-07:00 (24h, or empty "
                "for no quiet window)")
    return text


def _valid_window(text: str) -> bool:
    parts = text.split("-")
    if len(parts) != 2:
        return False
    for part in parts:
        bits = part.strip().split(":")
        if len(bits) != 2 or not all(bit.isdigit() for bit in bits):
            return False
        hour, minute = int(bits[0]), int(bits[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return False
    return True


def coerce(key: str, value: Any) -> Any:
    """Validate and normalise one value against its declared kind and range.

    Every writer goes through here — the settings endpoint, the budget alias,
    the console's inline gate control. Without one place, a range is enforced
    by whichever route happened to remember it and a value that is impossible
    through the UI arrives through the API.
    """
    s = setting(key)
    if s.kind == BOOL:
        return _as_bool(value, key)
    if s.kind == ENUM:
        text = str(value or "").strip()
        if text not in s.choices:
            raise SettingError(
                f"{key} must be one of {', '.join(s.choices)}, got {value!r}")
        return text
    if s.kind in (INT, FLOAT):
        return _as_number(value, s)
    if s.kind == LIST:
        return _as_list(value, s)
    return _as_string(value, s)


# ---------------------------------------------------------------------------
# Reading the stores it describes
# ---------------------------------------------------------------------------
def _registry_doc(root) -> dict:
    try:
        return _ws.get(root, REGISTRY_SEAT, REGISTRY_KEY, {})
    except Exception:
        # A store that will not read must not take the panel down: every field
        # then reports its default, which is what the code is using anyway.
        return {}


def _stored(root, s: Setting) -> tuple[bool, Any]:
    """(present, raw) from this setting's declared home. Never raises."""
    kind = s.store[0]
    try:
        if kind == "workspace":
            _, seat, doc_key, field_name = s.store
            doc = _ws.get(root, seat, doc_key, {})
            if field_name not in doc:
                return False, None
            return True, doc[field_name]
        if kind == "registry":
            doc = _registry_doc(root)
            field_name = s.store[1]
            if field_name not in doc:
                return False, None
            return True, doc[field_name]
        if kind == "budget":
            row = db.connect(root).execute(
                "SELECT * FROM spend_budget WHERE id = 1").fetchone()
            column = s.store[1]
            if row is None or column not in row.keys():
                return False, None
            return True, row[column]
    except Exception:
        return False, None
    return False, None


def _from_env(s: Setting) -> tuple[bool, Any, str]:
    """(present, value, var). Coercion beats supply — a kill switch that loses
    to a preference is not a kill switch."""
    if s.env_coerce:
        name, forcer = s.env_coerce
        raw = os.environ.get(name)
        if raw is not None:
            try:
                forced = forcer(raw)
            except Exception:
                forced = None
            if forced is not None:
                try:
                    return True, coerce(s.key, forced), name
                except SettingError:
                    pass
    if s.env:
        raw = os.environ.get(s.env)
        if raw is not None and str(raw).strip() != "":
            try:
                return True, coerce(s.key, raw), s.env
            except SettingError:
                # A typo in a shell profile must not brick the board. The stored
                # value is used and the row says the var was ignored.
                return False, None, ""
    return False, None, ""


def _resolve(root, s: Setting) -> tuple[Any, str, str]:
    """(value, source, env_var) for one setting. Never raises."""
    present, value, var = _from_env(s)
    if present:
        return value, SOURCE_ENV, var
    present, raw = _stored(root, s)
    if present:
        try:
            return coerce(s.key, raw), SOURCE_STORED, ""
        except SettingError:
            # A stored value that no longer validates (a range tightened, a
            # choice removed) reads as the default rather than propagating a
            # crash into every caller of get().
            pass
    default = list(s.default) if s.kind == LIST else s.default
    return default, SOURCE_DEFAULT, ""


def get(root: str | os.PathLike[str], key: str) -> Any:
    """The effective value of one setting: env, else stored, else default.

    This is what callers use instead of reading a doc field or a module
    constant directly, so an env override stops being invisible to half the
    code and visible to the other half.
    """
    return _resolve(root, setting(key))[0]


def source(root: str | os.PathLike[str], key: str) -> str:
    """Which layer won: "default" | "stored" | "env"."""
    return _resolve(root, setting(key))[1]


def effective(root: str | os.PathLike[str]) -> dict:
    """``{key: {value, source}}`` for every setting.

    One call, so doctor, the bootstrap and a bug report all quote the same
    numbers. Without it, "what is this board actually configured to do" is
    answered by reading five modules.
    """
    out: dict[str, dict] = {}
    for s in SETTINGS:
        value, src, var = _resolve(root, s)
        row = {"value": value, "source": src}
        if var:
            row["env"] = var
        out[s.key] = row
    return out


def client(root: str | os.PathLike[str]) -> dict:
    """The browser's subset, keyed by the short names the JS uses.

    Rides in the index page bootstrap. Every consumer keeps its hardcoded
    fallback, because a page served by an older build (or by a dashboard whose
    DB is unreadable) must still poll at some rate rather than not at all.
    """
    values = {}
    for key in CLIENT_KEYS:
        try:
            values[key.split(".", 1)[1]] = get(root, key)
        except Exception:
            continue
    return values


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def _write(root, s: Setting, value: Any) -> None:
    kind = s.store[0]
    if kind == "workspace":
        _, seat, doc_key, field_name = s.store
        doc = _ws.get(root, seat, doc_key, {})
        doc[field_name] = value
        # since/by are what both existing docs (director/gate, director/autopilot)
        # already carry, and the console renders them. Stamping them here keeps
        # a save through the registry indistinguishable from a save through the
        # specialised setter.
        doc.setdefault("since", "")
        doc["since"] = _now()
        doc["by"] = _actor()
        _ws.set(root, seat, doc_key, doc)
        return
    if kind == "registry":
        doc = _registry_doc(root)
        doc[s.store[1]] = value
        doc["updated_at"] = _now()
        doc["by"] = _actor()
        _ws.set(root, REGISTRY_SEAT, REGISTRY_KEY, doc)
        return
    if kind == "budget":
        column = s.store[1]
        stored = int(value) if s.kind == BOOL else value
        with db.tx(root) as conn:
            # Column names come from this module's own table, never from the
            # caller, so the interpolation is not a user-supplied identifier.
            conn.execute(
                f"UPDATE spend_budget SET {column} = ?, "
                "updated_at = datetime('now') WHERE id = 1", (stored,))
        return
    raise SettingError(f"{s.key} has no writable store ({s.store!r})")


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _actor() -> str:
    try:
        from . import activity
        return activity.current_actor()
    except Exception:
        return ""


def set(root: str | os.PathLike[str], key: str, value: Any, *,
        actor: str = "") -> dict:
    """Validate and store one setting, then report what is now effective.

    An env override does NOT refuse the write — the stored value is what takes
    effect the moment the var goes away, and refusing would mean a project's
    real configuration could not be recorded while a shell profile happened to
    disagree. The returned ``source`` and ``env_override`` say plainly that the
    save is not what the board is currently using, which is the honest answer.

    Returns ``{ok, key, stored, value, source, env_override}``.
    """
    s = setting(key)
    if s.human_only:
        # FAIL CLOSED, AND BEFORE VALIDATION. The refusal is about who is
        # asking, not about the value, so a machine must not be able to learn
        # anything from the shape of the error either.
        from . import activity as _activity

        who = actor or _activity.current_actor()
        if _activity.is_machine(who):
            raise SettingError(
                f"{key} is HUMAN-ONLY and this call is a machine's "
                f"({who}). " + (s.help.split(".")[0].strip() or key)
                + " is a constraint on agents; an agent that can change it is "
                  "not constrained. Ask the human at the dashboard, or file it "
                  "with ask_human. Every setting carrying human_only in "
                  "bgate_core.settings refuses the same way.")
    clean = coerce(key, value)
    was, _src_before, _var_before = _resolve(root, s)
    _write(root, s, list(clean) if isinstance(clean, list) else clean)
    live, src, var = _resolve(root, s)
    note = ""
    if src == SOURCE_ENV:
        note = s.env_note or f"{var} is overriding this"
    if s.guard and live != was:
        _audit_guard(root, s, was, live)
    return {"ok": True, "key": key, "stored": clean, "value": live,
            "source": src, "env_override": note}


def _audit_guard(root, s: Setting, was: Any, now: Any) -> None:
    """Record a change to a switch that WIDENS a safety guard.

    `dispatch.allow_dirty` used to need an environment variable; describing it in
    the registry made it one click, and one click that turns off "an agent may
    not write on top of your uncommitted work" is a click worth being able to
    find afterwards. Recorded whether or not the panel's confirmation was read,
    and on the bus as well as the ledger so it lands in the drawer beside the
    dispatches it changes the meaning of.

    Best-effort in both directions: an audit that fails must not stop the human
    from changing their own setting.
    """
    opened = bool(now) and not bool(was)
    line = (f"{s.key}: {was!r} -> {now!r}"
            + (" — a dispatch guard is now OFF" if opened else ""))
    try:
        from . import activity as _activity

        _activity.log(root, "settings", line, seat="director")
    except Exception:
        pass
    try:
        from . import events as _events

        _events.emit(root, "gate.mode" if s.key.startswith("gate.") else "settings.guard",
                     ref=s.key, payload={"key": s.key, "was": was, "now": now,
                                         "opened": opened, "help": s.help})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Description — the UI renders from this, not from a template per switch
# ---------------------------------------------------------------------------
def _field(root, s: Setting) -> dict:
    value, src, var = _resolve(root, s)
    stored_present, stored_raw = _stored(root, s)
    return {
        "key": s.key,
        "group": s.group,
        "kind": s.kind,
        "choices": list(s.choices),
        "value": value,
        "human_only": s.human_only,
        "default": list(s.default) if s.kind == LIST else s.default,
        "stored": stored_raw if stored_present else None,
        "source": src,
        "scope": s.scope,
        "help": s.help,
        "min": s.minimum,
        "max": s.maximum,
        "env_vars": s.env_vars(),
        "env": var,
        # The panel greys out a field the environment owns instead of offering a
        # control whose effect is invisible. It stays writable through the API
        # on purpose — see set().
        "locked": src == SOURCE_ENV,
        "env_override": (s.env_note or f"{var} is overriding this") if src == SOURCE_ENV else "",
        # Turning this one ON gives up a protection. The panel asks first; the
        # audit records it whether or not anybody read the dialog.
        "guard": bool(s.guard),
    }


def describe(root: str | os.PathLike[str]) -> dict:
    """Every setting, grouped, with value/default/source/help/kind/choices.

    This is the whole API surface a settings UI needs: adding a switch is one
    registry entry, and no route, payload or template changes. Without it, each
    new switch costs three edits in three files that then disagree.
    """
    groups: dict[str, list[dict]] = {name: [] for name in GROUPS}
    for s in SETTINGS:
        groups.setdefault(s.group, []).append(_field(root, s))
    return {
        "precedence": "env > project stored > default",
        "groups": [{"name": name, "fields": fields}
                   for name, fields in groups.items() if fields],
    }
