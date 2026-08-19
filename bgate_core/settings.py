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
# "Community" arrived with the streamer chat settings and was never added here,
# so every one of those entries declared a group the registry did not admit.
GROUPS = ("Dispatch", "Gates", "Art", "Generators", "Modules", "Follow-up",
          "Notifications", "Budget", "Console", "Privacy", "Community")

# The event vocabulary a notification can be asked for. Kept here rather than
# imported from events.py so that a settings panel still renders when the event
# table is mid-migration — a settings surface that needs the rest of the system
# healthy is useless exactly when it is needed.
# It must stay in step with events.KINDS: a kind that is emitted but absent here
# has no checkbox, so it can never be added to notify.kinds and the coerce()
# below REFUSES it — which is how chain.filed was emitted by queue.add_chain and
# unselectable in the panel at the same time.
EVENT_KINDS = ("item.done", "item.review", "item.failed", "item.stopped",
               "item.approved", "item.rejected", "item.aging",
               "artifact.candidate", "artifact.reviewed",
               "chain.filed", "chain.advanced",
               "chain.stalled", "gate.mode", "settings.guard", "style.trained",
               "budget.refused",
               "director.question", "agent.spawned", "agent.exited",
               "file.edited")


# ── how a human reads this panel ────────────────────────────────────────────
# A settings key is an IDENTIFIER — dispatch.allow_dirty, notify.question_stale_h
# — and identifiers are for the code that reads them. The panel was rendering
# them as the heading of every row, so the Settings screen read as a config file
# with checkboxes: a person who had not written this app could not tell what
# `follow_up.max_age_min` governed without reading three lines of help text.
#
# The key still shows, small and mono, underneath — it is what you search for,
# what an env override is named after, and what a bug report should quote. It is
# simply no longer the title.
#
# EVERY SETTING MUST APPEAR HERE. tests/test_settings_labels.py fails on a key
# with no label, because the failure mode otherwise is a new switch silently
# reverting to showing its identifier and nobody noticing for a release.
LABELS: dict[str, str] = {
    # Dispatch
    "autopilot.on": "Start queued work automatically",
    "dispatch.allow_dirty": "Let agents work on top of your unsaved changes",
    "dispatch.auto_commit": "Commit each finished task's own files",
    "dispatch.isolation": "Give each agent its own private copy of the repo",
    "dispatch.max_concurrent": "How many agents may work at once",
    "dispatch.model": "Model every seat uses",
    "dispatch.model_art": "Model the art seat uses",
    "dispatch.max_turns": "Stop an agent after this many turns",
    # Gates
    "gate.mode": "Who signs off finished work",
    "qa.max_rounds": "How many times work may bounce back for fixes",
    "qa.gated_seats": "Seats whose work gets checked automatically",
    "signoff.hours": "How long finished work waits for your sign-off",
    # Art
    "art.style_source": "Where the art style comes from",
    "art.style_dataset": "Reference images the style is trained on",
    "art.lora_strength": "How strongly the trained style is applied",
    "art.runner": "Which tool generates images",
    "art.image_backend": "Image provider",
    "art.auto_approve": "Accept generated art without review",
    # Generators
    "art.provider": "Preferred image provider",
    "art.model": "Preferred image model",
    "cinematic.model": "Preferred video model",
    "music.model": "Preferred music model",
    "voice.model": "Preferred speech voice",
    "text.model": "Model for prompt-writing calls",
    # Modules
    "modules.disabled": "Features switched off for this project",
    # Follow-up
    "followup.director_debrief": "Ask the director to review finished work",
    "followup.max_per_hour": "Most reviews to raise in an hour",
    "followup.max_age_min": "Skip reviewing work older than this",
    "followup.auto_reopen_failures": "Reopen failed tasks automatically",
    "followup.max_auto_retries": "Most automatic retries per task",
    "followup.escalate_failures": "Raise a director item when work fails",
    "followup.escalation_to_session": "Hand failure escalations to the director session",
    # Notifications
    "notify.in_app": "Show notifications in the app",
    "notify.kinds": "What to be notified about",
    "notify.webhook": "Send notifications to a webhook",
    "notify.stall_hours": "Warn when work has not moved for this long",
    "notify.question_stale_h": "Warn about unanswered questions after",
    "notify.quiet_hours": "Hours to stay silent",
    # Budget
    "budget.enforced": "Enforce spending limits",
    "budget.per_item_usd": "Limit per task",
    "budget.per_day_usd": "Limit per day",
    "budget.per_project_usd": "Limit for the whole project",
    "budget.max_runtime_s": "Stop an agent after this long",
    # Console
    "console.poll_live_ms": "Refresh rate while work is running",
    "console.poll_idle_ms": "Refresh rate when nothing is running",
    "console.model": "Model the director console session runs on",
    "console.max_usd": "Spending limit for one console session",
    "graph.phase_cap": "Most steps to show per agent on the graph",
    "brainstorm.runner": "Which assistant the brainstorm room uses",
    "brainstorm.model": "Model the brainstorm room uses",
    "brainstorm.max_usd": "Spending limit for one brainstorm",
    # Privacy
    "privacy.streamer": "Streamer mode - hide anything private on screen",
    # Community
    "chat.capture": "What viewer chat to keep during a stream",
    "chat.playtest_notes": "Let viewers leave notes on a playtest",
}

#: One glyph per group, for the category list. Tabler names — see shell/Ti.tsx.
GROUP_ICONS: dict[str, str] = {
    "Dispatch": "send",
    "Gates": "shield-check",
    "Art": "palette",
    "Follow-up": "rotate-clockwise",
    "Notifications": "bell",
    "Budget": "coin",
    "Console": "terminal-2",
    "Modules": "puzzle",
    "Privacy": "eye-off",
    "Community": "users",
    "Generators": "sparkles",
}


def label_for(key: str) -> str:
    """The human name for a key, falling back to a readable form of the key."""
    hit = LABELS.get(key)
    if hit:
        return hit
    tail = key.split(".")[-1].replace("_", " ")
    return tail[:1].upper() + tail[1:]


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
        key="autopilot.on", group="Dispatch", kind=BOOL, default=True,
        store=("workspace", "director", "autopilot", "on"),
        env_coerce=("BGATE_AUTODEPLOY", lambda raw: False if _falsey(raw) else None),
        env_note="BGATE_AUTODEPLOY=0 stops the loop from starting at all, so the "
                 "stored switch cannot take effect until the server restarts",
        help="Dispatch queued work automatically as slots free up, instead of "
             "waiting for somebody to press deploy on each item. ON by "
             "default since 2026-08-19: shipped off, a filed chain looked "
             "exactly like a running one and sat still until somebody found "
             "the toggle - 'my chains never auto-deploy' was this default. "
             "Turn it off for a board you want to hand-dispatch."),
    Setting(
        key="dispatch.allow_dirty", group="Dispatch", kind=BOOL, default=False,
        store=("registry", "dispatch.allow_dirty"), scope=MACHINE,
        env="BGATE_ALLOW_DIRTY", guard=True, human_only=True,
        help="Let an agent be dispatched on top of uncommitted changes. Off, "
             "because the resulting diff cannot tell the agent's edits from "
             "yours — which is what makes a revert safe."),
    Setting(
        key="dispatch.auto_commit", group="Dispatch", kind=BOOL, default=True,
        store=("registry", "dispatch.auto_commit"), scope=MACHINE,
        env="BGATE_AUTO_COMMIT", human_only=True,
        help="Commit each finished run's OWN files, so the next dispatch is "
             "not refused by the dirty tree the last one made. On, because "
             "with it off nothing ever commits and the board deadlocks after "
             "the first item that writes anything — measured: an overnight "
             "queue of thirty finished three. It commits only the paths that "
             "run touched, so your own uncommitted work is never swept in "
             "(and still, correctly, blocks the next dispatch)."),
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
    Setting(
        key="dispatch.model", group="Dispatch", kind=STRING, default="sonnet",
        store=("registry", "dispatch.model"), scope=MACHINE,
        env="BGATE_MODEL", human_only=True,
        help="The model every seat runs on unless overridden below. Nothing "
             "used to pass --model at all, so every agent silently inherited "
             "whatever the CLI defaulted to — a night of 82 runs went out on "
             "opus-5[1m] and moved 1.19 BILLION input-side tokens in eight "
             "hours. A seat that edits GDScript does not need the biggest "
             "model; naming one here is what stops the default deciding."),
    Setting(
        key="dispatch.model_art", group="Dispatch", kind=STRING, default="opus",
        store=("registry", "dispatch.model_art"), scope=MACHINE,
        env="BGATE_MODEL_ART", human_only=True,
        help="The art seat's model, because art is the one seat whose output "
             "is judged on taste rather than on whether it parses. Blank "
             "falls back to dispatch.model."),
    Setting(
        key="dispatch.max_turns", group="Dispatch", kind=INT, default=120,
        minimum=0, maximum=1000, store=("registry", "dispatch.max_turns"),
        scope=MACHINE, env="BGATE_MAX_TURNS", human_only=True,
        help="Hard ceiling on assistant turns per run; 0 disables it. There "
             "was no ceiling: one item took 395 turns and another 393, and "
             "because every turn re-sends the whole context the last hundred "
             "cost more than the first hundred. The cost ceiling only trips at "
             "a result boundary, which a grinding agent may not reach."),

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
        default=("art", "gameplay", "audio", "narrative", "tech", "cinematic"),
        choices=("art", "gameplay", "audio", "narrative", "tech", "cinematic"),
        store=("registry", "qa.gated_seats"), human_only=True,
        help="Which maker seats get an automatic QA reviewer when their work "
             "is completed. Was a hardcoded tuple in the gate, so a studio that "
             "wanted QA on art alone had to edit harness source — which changed "
             "it for every project on the machine and needed a restart. Every "
             "maker seat is on by default now: the old four left tech and "
             "cinematic completions closing on the agent's word alone, which "
             "made quality structurally uneven by seat. director and qa are "
             "never gated: that is recursion, not review."),
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

    # -- Generators: the preferred provider and models -----------------------
    # There was NO stored preference anywhere: every choice was key-presence
    # probing plus hardcoded per-tool defaults, so a person with a paid,
    # preferred service watched the harness route work to whichever key
    # happened to probe first. These are the single write point; every picker
    # consults them before probing.
    Setting(
        key="art.provider", group="Generators", kind=ENUM, default="auto",
        choices=("auto", "openai", "krea", "kie", "local"),
        store=("registry", "art.provider"), scope=MACHINE,
        env="BGATE_ART_PROVIDER", human_only=True,
        help="Which image provider generation goes to. `auto` keeps the "
             "routing rules (identity work to the reference-strongest "
             "configured provider, everything else by key-presence order). A "
             "named provider is honoured the way an explicit ask is: even "
             "with its key missing, you get THAT provider's error naming the "
             "key to set — never a silent substitution billed to a service "
             "you did not choose."),
    Setting(
        key="art.model", group="Generators", kind=STRING, default="",
        store=("registry", "art.model"), scope=MACHINE,
        env="BGATE_ART_MODEL", human_only=True,
        help="The image model, when a generation names none itself. Must be a "
             "model of the provider actually in use (gpt-image-1, "
             "krea-2-large, nano-banana-2, …); blank takes that provider's "
             "own default. A stale value after switching providers fails with "
             "the provider's unknown-model error rather than being silently "
             "dropped."),
    Setting(
        key="cinematic.model", group="Generators", kind=STRING, default="",
        store=("registry", "cinematic.model"), scope=MACHINE,
        human_only=True,
        help="The video model a cinematic sequence plans onto when the plan "
             "names none. Blank is the adapter default (seedance-2). "
             "Validated against the registered video models at plan time, "
             "same as an explicit choice."),
    Setting(
        key="music.model", group="Generators", kind=STRING, default="",
        store=("registry", "music.model"), scope=MACHINE, human_only=True,
        help="The music model when a request names none. Blank is the "
             "adapter default (V5)."),
    Setting(
        key="voice.model", group="Generators", kind=STRING, default="",
        store=("registry", "voice.model"), scope=MACHINE, human_only=True,
        help="The speech voice/model when a line names none. Blank is the "
             "adapter default (aura-2-thalia-en)."),
    Setting(
        key="text.model", group="Generators", kind=STRING, default="",
        store=("registry", "text.model"), scope=MACHINE, human_only=True,
        help="The model promptwriter uses for prompt-polish calls. Blank is "
             "the historical default (gpt-4o-mini)."),

    # -- Modules -------------------------------------------------------------
    Setting(
        key="modules.disabled", group="Modules", kind=LIST, default=(),
        choices=("floor", "brainstorm", "music", "cinematic", "voice",
                 "playtest", "three_d"),
        store=("registry", "modules.disabled"), human_only=True,
        help="Optional features this project has switched off — chosen on the "
             "first-run card and changeable here. A disabled module's MCP "
             "tools are not registered (agents stop paying context for tools "
             "they will never call — new sessions only, a running server "
             "keeps its registry), its panes leave the dashboard, and doctor "
             "stops grading its dependencies. The core — board, seats, "
             "canon, image generation, Godot — has no switch: a module "
             "nobody can ship without would be a checkbox that only exists "
             "to be mis-unchecked."),

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
        default=True, store=("registry", "followup.auto_reopen_failures"),
        help="Reopen a failed item with the failure text instead of leaving it "
             "for a human, up to followup.max_auto_retries automatic rounds. "
             "ON by default: shipped off, the retry rail existed and never "
             "fired, so ONE failure stopped a whole chain until a human "
             "noticed — the exact dead-end this feature was built to remove. "
             "The runaway-spend risk lives in the CAP, which is 1 with a hard "
             "ceiling of 2, not in this switch. Whether this is on or off, "
             "the failure still reaches the director — see "
             "followup.escalate_failures."),
    Setting(
        key="followup.max_auto_retries", group="Follow-up", kind=INT, default=1,
        minimum=0, maximum=2, store=("registry", "followup.max_auto_retries"),
        help="How many times the harness may re-dispatch ONE failed item on "
             "its own before handing it to the director instead. One, and the "
             "ceiling of two is not a suggestion: an item that fails for a "
             "structural reason — a missing key, a credit block, an asset that "
             "does not exist, a lane the seat cannot write to — fails "
             "identically every round, so every retry past the first is money "
             "spent rediscovering the same blocker. 0 disables automatic "
             "retries entirely and escalates on the first failure. The count "
             "is stored on the item, so a dashboard restart does not hand it a "
             "fresh budget."),
    Setting(
        key="followup.escalate_failures", group="Follow-up", kind=BOOL,
        default=True, store=("registry", "followup.escalate_failures"),
        help="When a failure is not going to be retried automatically, file "
             "ONE item for the director naming the failing item, its seat and "
             "what it said, so a person or the director decides what happens "
             "next. On by default because it costs nothing — the escalation is "
             "queued and never dispatched to an agent — and because the "
             "alternative is what this replaced: a red marker on the board "
             "that nothing acts on until somebody happens to look."),
    Setting(
        key="followup.escalation_to_session", group="Follow-up", kind=BOOL,
        default=True, store=("registry", "followup.escalation_to_session"),
        human_only=True,
        help="Hand a filed failure escalation straight to the console's "
             "director session, which investigates the failure and acts — "
             "reopen with a corrected brief, file the fix as new work, or "
             "explain what needs your decision. This is what makes an "
             "escalation a decision that gets MADE rather than a card that "
             "waits: shipped as held-only, every failure dead-ended until a "
             "human opened the dashboard. Off returns to that — the "
             "escalation is filed and held for you. Spend is bounded by "
             "console.max_usd; no worker agent is bought either way."),

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
        key="notify.stall_hours", group="Notifications", kind=FLOAT, default=0.5,
        minimum=0.25, maximum=168.0, store=("registry", "notify.stall_hours"),
        help="How long a chain's head may sit in review or blocked before it "
             "is called stalled. The bus is transition-driven, so without this "
             "the quiet failure — nothing happening — emits nothing. Half an "
             "hour by default: at the old two hours, a chain parked behind a "
             "dead predecessor was invisible for a whole working session."),
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
    Setting(
        key="console.model", group="Console", kind=STRING, default="opus",
        store=("registry", "console.model"), scope=MACHINE,
        env="BGATE_CONSOLE_MODEL", human_only=True,
        help="The model the director console session runs on. This session is "
             "the human's own counterpart — it investigates, arbitrates and "
             "delegates — so it defaults to a stronger model than the seats it "
             "dispatches. Named rather than inherited, for the same reason "
             "dispatch.model is."),
    Setting(
        key="console.max_usd", group="Console", kind=FLOAT, default=15.0,
        minimum=0.0, maximum=10000.0, store=("registry", "console.max_usd"),
        human_only=True,
        help="Ceiling for one console session's conversation, in USD. The "
             "session outlives any single process (it resumes across dashboard "
             "restarts), so this bounds the CONVERSATION; clearing the console "
             "starts a fresh one. 0 means no ceiling."),
    Setting(
        key="brainstorm.runner", group="Console", kind=STRING, default="claude",
        store=("registry", "brainstorm.runner"), scope=MACHINE,
        env="BGATE_BRAINSTORM_RUNNER", human_only=True,
        # NOT an ENUM. The list of runners that can hold a read-only
        # conversation lives in bgate_ui.runners, which this module may not
        # import, and a choices tuple copied out of it is a second list that
        # goes stale the day somebody adds a local model. An unknown name falls
        # back to the default and the room says which runner it ended up on.
        help="Which CLI the brainstorm room's thinking partner runs on. It is "
             "spawned with the built-in tool set EMPTY and one two-tool MCP "
             "server registered — read and draw on this session's own pads, "
             "nothing else — so it can talk, it can join your diagram, and it "
             "cannot reach the queue, the repo or a generator. A runner that "
             "has not declared that read-only mode is refused rather than "
             "started with the dispatch flags. `claude` is the only one that "
             "has, so far; codex and local models are one table entry each."),
    Setting(
        key="brainstorm.model", group="Console", kind=STRING, default="sonnet",
        store=("registry", "brainstorm.model"), scope=MACHINE,
        env="BGATE_BRAINSTORM_MODEL", human_only=True,
        help="The model a brainstorm turn runs on. Named rather than inherited "
             "for the same reason dispatch.model is: an unset --model means "
             "whatever the CLI defaults to that day, which is how a night of "
             "work went out on the largest model nobody chose. Blank inherits "
             "the CLI default and accepts that."),
    Setting(
        key="brainstorm.max_usd", group="Console", kind=FLOAT, default=2.0,
        minimum=0.0, maximum=100.0, store=("registry", "brainstorm.max_usd"),
        scope=MACHINE, human_only=True,
        help="What one brainstorm conversation may spend before it stops "
             "answering. This is the CHEAP room — that is the whole reason it "
             "exists next to the board — and the partner is now a real CLI "
             "session rather than a fraction-of-a-cent API call: one trivial "
             "measured turn was $0.06. Passed to the CLI's own --max-budget-usd "
             "and also tracked across the conversation, so respawning cannot "
             "launder a per-process ceiling into no ceiling. 0 removes it."),

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

    # -- Community ----------------------------------------------------------
    # Live-stream chat. The CREDENTIALS are not here and must not be: a channel
    # name and a token are env-bound, in the project's gitignored .env, because
    # this registry is served verbatim by /api/settings and printed whole by
    # `bgate doctor` (see routes/providers.py for that argument in full). What
    # IS here is behaviour — how much of chat to keep, and whether viewers may
    # write on a recording — which is exactly what a switch should be.
    # `chat.autoconnect` used to live here. It rendered as a toggle and NOTHING
    # read it — chatpump only ever consulted the BGATE_CHAT kill switch — so the
    # switch moved and the behaviour did not. An inert control is worse than a
    # missing one.
    Setting(
        key="chat.capture", group="Community", kind=ENUM, default="all",
        choices=("all", "marked"),
        store=("registry", "chat.capture"),
        help="What counts as feedback during a session. 'all' keeps everything "
             "that survives the filler filter, because the honest reaction is "
             "the unmarked one nobody typed a command for — one person can "
             "still only contribute a capped number of lines. 'marked' keeps "
             "only messages starting !fb, which is the right choice for a big "
             "channel or a raid."),
    Setting(
        key="chat.playtest_notes", group="Community", kind=BOOL, default=True,
        store=("registry", "chat.playtest_notes"),
        help="Let viewers leave notes on a playtest while it records, on the "
             "same clock as your own typed notes and attributed to them. They "
             "land as candidates only — a note from chat is never "
             "auto-promoted, and nothing reaches an agent without you "
             "confirming a plan."),
)

BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}

# The subset the browser needs. It rides in the index page's bootstrap next to
# the cache-busting module srcs rather than costing a second fetch on load, and
# every consumer keeps its hardcoded fallback for a bootstrap that is missing.
CLIENT_KEYS = ("modules.disabled",
               "console.poll_live_ms", "console.poll_idle_ms", "graph.phase_cap",
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
            value = get(root, key)
        except Exception:
            continue
        if isinstance(value, (list, tuple)):
            # Structured values keep their FULL key: the short-name scheme
            # exists for the poll-rate numbers the JS has always read, and
            # "disabled" floating free of its module context is a collision
            # waiting for the next list setting.
            values[key] = list(value)
        else:
            values[key.split(".", 1)[1]] = value
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
        # NOT _registry_doc. That helper swallows a failed read into {} so a
        # PANEL can render defaults, and this is a read-modify-REPLACE: writing
        # that {} back would silently reset every other registry-stored setting
        # to its default because one transient "database is locked" happened at
        # read time. A save that cannot read what it is about to rewrite must
        # fail loudly instead — the caller retries; nothing is lost.
        doc = _ws.get(root, REGISTRY_SEAT, REGISTRY_KEY, {})
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
# Fields whose CHOICES are live facts about this machine, not registry
# constants: which providers hold a key, which model ids their adapters can
# actually route. Served by bgate_core.modelcatalog, filtered to configured
# providers, so the panel's pickers cannot offer a model that fails on its
# first call with a missing-key error. The static declaration stays as the
# fallback for a catalog that will not import.
_DYNAMIC_CHOICES: dict[str, str] = {
    "art.provider": "image-providers",
    "art.model": "image-models",
    "cinematic.model": "video-models",
    "music.model": "music-models",
    "voice.model": "speech-models",
    "text.model": "text-models",
    "dispatch.model": "agent-models",
    "dispatch.model_art": "agent-models",
    "console.model": "agent-models",
    "brainstorm.model": "agent-models",
}


def _choices_for(root, s: Setting) -> list:
    want = _DYNAMIC_CHOICES.get(s.key)
    if not want:
        return list(s.choices)
    try:
        from bgate_core import modelcatalog

        live = modelcatalog.options(root, want)
    except Exception:
        live = []
    if not live:
        live = list(s.choices)
    # The stored value survives in the list even when its provider's key was
    # removed — a picker that hides the current value reads as data loss.
    current = str(_resolve(root, s)[0] or "").strip()
    if current and current not in live:
        live = [current] + live
    if not live:
        return list(s.choices)
    # An ENUM must keep every legal value reachable; the catalog only orders
    # and augments it. A blank entry on the STRING pickers is "provider
    # default", which set() accepts because "" is these settings' default.
    if s.kind == ENUM:
        live += [c for c in s.choices if c not in live]
    return live


def _field(root, s: Setting) -> dict:
    value, src, var = _resolve(root, s)
    stored_present, stored_raw = _stored(root, s)
    return {
        "key": s.key,
        "group": s.group,
        "kind": s.kind,
        "choices": _choices_for(root, s),
        "value": value,
        "human_only": s.human_only,
        "default": list(s.default) if s.kind == LIST else s.default,
        "stored": stored_raw if stored_present else None,
        "source": src,
        "scope": s.scope,
        # THE HUMAN NAME, and the reason the panel stopped titling every row
        # with an identifier. `key` is still here and still shown, because it is
        # what you search for and what an env override is named after.
        "label": label_for(s.key),
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
        "groups": [{"name": name, "icon": GROUP_ICONS.get(name, "adjustments"),
                    "fields": fields}
                   for name, fields in groups.items() if fields],
    }
