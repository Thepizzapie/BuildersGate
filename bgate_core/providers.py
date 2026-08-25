"""Every art-generation provider, declared once, in data.

WHY THIS EXISTS. OpenAI and Krea were each wired in by hand, separately, in five
places — ``imagegen.available``, ``krea.available``, ``imageto3d``'s backend
table, ``doctor._probe_openai_key`` and ``_pick_provider`` in the MCP server —
and every one of them learned about Krea late and on its own. Two of those
mistakes are on the record: ``image_status`` probed OPENAI_API_KEY alone and
told a project holding a working Krea key that painted art was unavailable, and
``bgate doctor`` printed ``MISS openai_key`` and exited 1 for a setup that was
fine (documented as a known bug in CLAUDE.md, fixed by the probe now reading
this file). Both are one bug: the provider list lived in prose, not in data.

ADDING A THIRD PROVIDER IS ONE ENTRY IN :data:`PROVIDERS`. Nothing downstream —
the doctor row, the HTTP endpoints, the dashboard panel, the .env writer — names
a provider. If adding one ever needs a second edit, that second place is the bug.

SECRETS DO NOT LEAVE THIS MODULE. :func:`status` reports presence, a last-4
fingerprint, and which layer supplied the value. There is deliberately NO
function here that returns a key: this project has already committed a key once,
which is why ``.env`` is gitignored, and a getter is exactly the thing that ends
up interpolated into a log line two refactors later. Adapters read their own env
var directly, as they always have.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from bgate_core import envfile

# What a provider can produce. The dashboard groups cards by these, and the
# vocabulary is fixed here so two providers cannot describe the same capability
# with two different words. `video` was listed here with no provider behind it —
# an empty column being a truthful answer where a missing column is a silent one
# — and kie fills it now, through bgate_core.cinematic. Note that a video key is
# only half of what a cutscene needs: the other half is an ffmpeg built with
# libtheora, because Godot plays Ogg Theora and nothing else, and no credential
# table can report on that. cinematic.ffmpeg_status() is where that is asked.
CAPABILITIES: dict[str, str] = {
    "image_2d": "2D images",
    # ANIMATION IS NOT `image_2d`. A provider that paints a still is not a
    # provider that can turn one into a cycle — Retro Diffusion animates an
    # existing sheet and mints nothing, kie is the reverse — and a project
    # that folded them together would report it can animate because it holds
    # an image key, which is how a character ships as eleven stiff stills.
    "animation_2d": "2D animation (cycles from an existing sheet)",
    "model_3d": "3D models",
    "audio": "music and sound",
    "video": "video",
    "text": "writing (prompts, brainstorm)",
    # SPEECH IS NOT `audio`, AND KEEPING THEM APART IS LOAD-BEARING. `audio`
    # means "generates a sound asset the game ships" — Suno writing a track,
    # which is art generation and is what ART_CAPABILITIES below gates on.
    # Speech is the human talking to the dashboard and the dashboard answering;
    # it produces nothing the project keeps. Folded together, a Deepgram-only
    # setup would report that this product can generate audio for the game,
    # which is false, and `bgate doctor` would go green on art with no art
    # provider configured at all.
    "speech": "speech in and out (talking to an agent)",
    # CHAT IS NOT `text`. `text` means "a model writes prose for us"; chat means
    # "we read what an audience typed". Folded together, a project with only a
    # Twitch token would report that it can write prompts and brainstorm, which
    # is false, and the first thing to notice would be a generation failing.
    "chat": "live-stream chat (community feedback)",
}

# WHICH CAPABILITIES ARE ART GENERATION. doctor's `art_key` row asks "is ANY art
# provider configured", and before this existed it asked "is ANY provider
# configured" — which was the same question only for as long as every provider
# generated art. The first one that does not (deepgram) would have turned that
# row green for a project that cannot make a single image.
ART_CAPABILITIES = frozenset({"image_2d", "animation_2d", "model_3d",
                              "audio", "video"})


class ProviderError(ValueError):
    """A refusal with a sentence worth showing the human."""


@dataclass(frozen=True)
class Provider:
    """One credential and everything the product needs to know about it.

    ``probe`` returns an adapter's own ``available()`` dict — ``{available,
    reason, ...}``. It is BORROWED, never re-derived: the adapter already knows
    that a key without the ``openai`` package installed is still unusable, and a
    second opinion here would disagree with the tool that actually runs.
    """

    id: str
    label: str
    env: str
    powers: tuple[str, ...]
    key_url: str
    help: str
    probe: Callable[[Any], dict] = field(repr=False, default=lambda root: {})


def _probe_openai(root: Any = None) -> dict:
    # Imported inside the probe, same rule doctor follows: this module is on the
    # dashboard's import path and an adapter import belongs behind the thing
    # that needs it.
    from bgate_adapters import imagegen
    return dict(imagegen.available())


def _probe_krea(root: Any = None) -> dict:
    from bgate_adapters import krea
    return dict(krea.available(root))


def _probe_kie(root: Any = None) -> dict:
    from bgate_adapters import kie
    return dict(kie.available(root))


def _probe_retrodiffusion(root: Any = None) -> dict:
    from bgate_adapters import retrodiffusion
    return dict(retrodiffusion.available(root))


def _probe_deepgram(root: Any = None) -> dict:
    from bgate_adapters import deepgram
    return dict(deepgram.available(root))


def _probe_twitch(root: Any = None) -> dict:
    """Whether chat can be read AT ALL, which is not whether a key is set.

    The only entry in this table whose availability does not depend on its own
    env var. Twitch chat is readable anonymously — measured, see
    ``bgate_core.chatlink`` — so what makes this provider usable is the CHANNEL,
    and the token buys one extra capability (posting) rather than access. A row
    that went red because a token was missing would be telling a fully working
    setup that it is broken, which is the exact bug this whole module was
    written to stop repeating.
    """
    from bgate_core import chatlink

    config, why = chatlink.config(root, "twitch")
    if config is None:
        return {"available": False, "reason": why}
    return {"available": True, "reason": "",
            "anonymous": config.anonymous}


def art_providers() -> tuple["Provider", ...]:
    """Only the providers that can generate something the game ships.

    The list ``doctor._probe_art_key`` counts. See ART_CAPABILITIES for why this
    is not simply PROVIDERS any more.
    """
    return tuple(one for one in PROVIDERS
                 if ART_CAPABILITIES.intersection(one.powers))


# ORDER IS THE AUTO-SELECT ORDER, and it matches _pick_provider in the MCP
# server on purpose: openai first, then krea, then kie. A panel that lists them
# in a different order than the code picks them teaches the wrong default.
#
# kie is LAST among the image providers, and THE REASON THIS COMMENT USED TO
# GIVE IS NO LONGER TRUE. It said kie was the only provider here that cannot
# condition on a local pinned anchor, because its reference documents every
# image field as a URI and says nothing about base64. That was correct when it
# was written and it is not correct now: `kie.upload_file` posts base64 to the
# file-upload host and hands back an https URL the generation endpoint accepts,
# `kie.reference_slots` reads each model's own reference field and cap, and
# nano-banana-2 takes fourteen reference images — which makes kie one of the
# better choices for a consistent cast, not a provider that quietly downgrades
# the work.
#
# THE ORDER IS LEFT ALONE ANYWAY, and deliberately, because it is now resting on
# a different and much duller argument: openai is the key most setups have, and
# changing which provider a project bills by default is not a thing to do inside
# a comment fix. What is fixed here is the false statement, which was the part
# that would have sent the next reader to build an upload path that already
# exists. Its music and video are unaffected — nothing else in this product can
# do either at all.
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="openai",
        label="OpenAI",
        env="OPENAI_API_KEY",
        powers=("image_2d", "text"),
        key_url="https://platform.openai.com/api-keys",
        help="gpt-image for painted art, and the small model behind prompt "
             "writing and brainstorm. Billed per image (~$0.02-0.19).",
        probe=_probe_openai,
    ),
    Provider(
        id="krea",
        label="Krea",
        env="KREA_API_KEY",
        powers=("image_2d", "model_3d"),
        key_url="https://www.krea.ai/settings/api-tokens",
        help="krea-2 for painted art with native style references, and "
             "TRELLIS / Hunyuan3D / Tripo for image-to-3D meshes.",
        probe=_probe_krea,
    ),
    Provider(
        id="kie",
        label="kie.ai",
        env="KIE_API_KEY",
        powers=("image_2d", "audio", "video"),
        key_url="https://kie.ai/api-key",
        help="One key for Nano Banana / FLUX.2 / Qwen images, Suno music and "
             "Seedance video — the only provider here that generates audio or "
             "video at all. No 3D. Prices are not published: kie bills in "
             "credits and reports what a job consumed after it runs, so set "
             "BGATE_KIE_USD_PER_CREDIT to your account's rate for dollar "
             "ledger rows.",
        probe=_probe_kie,
    ),
    # After the general image providers because it is not one: RD generates
    # ANIMATION (and pixel-art stills, unused here) — the capability nothing
    # above it has. The sprite contract's animation_generate names it
    # directly rather than walking the auto-select order.
    Provider(
        id="retrodiffusion",
        label="Retro Diffusion",
        env="RETRO_DIFFUSION_API_KEY",
        powers=("animation_2d",),
        key_url="https://www.retrodiffusion.ai/app/devtools",
        help="Purpose-trained pixel animation: walk/idle/attack cycles FROM "
             "one of your own character frames ($0.14/cycle), plus free "
             "pixel-grid repair. The model that knows what a walk cycle is.",
        probe=_probe_retrodiffusion,
    ),
    # LAST, AND NOT IN THE AUTO-SELECT ORDER AT ALL, because it competes with
    # nothing above it: it is the only entry here that generates no art, so
    # there is no capability for it to be picked ahead of or behind.
    Provider(
        id="deepgram",
        label="Deepgram",
        env="DEEPGRAM_API_KEY",
        powers=("speech",),
        key_url="https://console.deepgram.com/",
        help="Nova-3 realtime speech-to-text and Aura-2 text-to-speech, so you "
             "can talk to the brainstorm agent and hear it answer. Billed by "
             "the minute listened ($0.0048/min) and per character spoken "
             "($0.030/1k). Generates no art and no game audio.",
        probe=_probe_deepgram,
    ),
    # LAST, AND OPTIONAL IN A WAY NOTHING ELSE HERE IS. Every other row is a key
    # without which its capability does not exist. This one is a key without
    # which the capability still works: chat reads anonymously with only
    # TWITCH_CHANNEL set, and the token adds the ability to POST — announcing a
    # feedback session in chat, and nothing else. It is registered here anyway
    # so that credential handling has exactly ONE implementation: written
    # through the human-only endpoint, into the gitignored .env, reported as a
    # last-4 and never returned. A second credential surface with its own rules
    # is how the rule gets weaker.
    #
    # Generates no art, so it is not in ART_CAPABILITIES and `bgate doctor`'s
    # art row does not go green because somebody connected a chat account.
    Provider(
        id="twitch",
        label="Twitch chat",
        env="TWITCH_OAUTH_TOKEN",
        powers=("chat",),
        key_url="https://dev.twitch.tv/docs/authentication/",
        help="Reads your stream's chat so viewers can leave feedback on the "
             "game. OPTIONAL: set TWITCH_CHANNEL alone and chat is read "
             "anonymously with no account at all. A token with the chat:read "
             "and chat:edit scopes only adds the ability to announce a feedback "
             "session in chat. Free; no per-message cost.",
        probe=_probe_twitch,
    ),
)


def by_id(provider_id: str) -> Provider:
    """One provider, or a refusal that names the legal ids — an error that does
    not list them costs a round trip to find out what was meant."""
    wanted = (provider_id or "").strip().lower()
    for one in PROVIDERS:
        if one.id == wanted:
            return one
    raise ProviderError(
        f"unknown provider '{provider_id}' — known: "
        + ", ".join(p.id for p in PROVIDERS))


def provider_for(task_kind: str = "", *, asked: str = "",
                 root: str | os.PathLike | None = None) -> str:
    """Which image provider this KIND of work goes to. Not merely a default.

    2D CHARACTER WORK GOES TO KREA WHENEVER KREA IS CONFIGURED, and it is a
    routing rule rather than a preference because the alternative was measured
    and it is not close. On a 16-frame NE/SE walk sheet generated from one
    pinned character, the same prompt and reference through every
    reference-capable model on both providers:

      nano-banana-2 (krea)  eight frames a row, correct back-view and
                            front-view rows, clean key
      krea-2-large          FAILED the alpha audit at 14% hollow interior —
                            the key colour landed inside the figure and was
                            cut out of it — six near-identical frames a row
      gpt-image (openai)    refuses sheet prompts outright at this layer

    The general auto-select order (openai, then krea, then kie) is right for a
    plate, a concept pass or a prop, and wrong for a character: it hands sprite
    work to whichever key happens to be set first, which is how the best model
    for the job sat configured and unused. So identity work is ROUTED, and
    everything else keeps the historical order.

    An explicit `asked` always wins, including when its key is missing — the
    caller gets that provider's own error naming the key to set, rather than a
    silent substitution that bills them for a model they did not choose.
    """
    if (asked or "").strip():
        return asked.strip().lower()

    # THE STORED PREFERENCE, between the explicit ask and the routing rules.
    # There was no such thing anywhere: a person with a paid, preferred
    # service watched work go to whichever key probed first, per tool. A
    # named preference behaves exactly like an explicit ask — honoured even
    # with its key missing, so the failure names THAT provider's key rather
    # than silently billing a service nobody chose. `auto` (the default) is
    # the routing below, unchanged.
    if root is not None:
        try:
            from bgate_core import settings as _settings

            preferred = str(_settings.get(root, "art.provider") or "").strip().lower()
        except Exception:
            preferred = ""
        if preferred and preferred != "auto":
            return preferred

    from bgate_adapters import krea

    if str(task_kind or "").strip().lower() in krea.CHARACTER_KINDS:
        # KIE FIRST FOR IDENTITY WORK WHEN IT IS CONFIGURED, and this is the
        # same routing rule as before rather than a reversal of it. Read the
        # measurement above carefully: what won was NANO-BANANA-2, and the
        # provider it was reached through is incidental. That model is now
        # registered on kie directly (bgate_adapters.kie.MODELS, verified off
        # /market/google/nanobanana2) with FOURTEEN reference slots against the
        # eight its Pro tier takes, so a cast that has to stay consistent gets
        # more anchors here than anywhere else in the product.
        #
        # THE OLD OBJECTION TO ROUTING ANYTHING TO KIE IS DEAD. It was that
        # kie's image fields take public URLs, so an anchored generation would
        # silently become prompt-only. `kie.upload_file` posts the local anchor
        # as base64 and hands back a URL the generation endpoint accepts, and
        # `kie.reference_slots` reads each model's own field and cap, so a
        # pinned ref travels. See the note beside PROVIDERS.
        #
        # krea remains the fallback rather than being removed: it serves the
        # same model, a project may hold only a krea key, and character work
        # must never fall through to the general order - that is what handed
        # sprite sheets to gpt-image, which refuses them outright.
        live = _routable(("kie", "krea"), root)
        if live:
            return live[0]
    # THE GATEWAY'S DOCTRINE ORDER, NOT WHICHEVER KEY PROBES FIRST. This read
    # `openai, krea, kie` by key presence alone, which is two bugs in one
    # line: it contradicted gateway.CAPABILITIES["image"] (kie > krea >
    # openai, the stated house rule), and it could not see a DRAINED account,
    # so a 429/402 was routed to on every call while two funded providers sat
    # idle. The gateway already knew both facts and was only ever consulted
    # AFTER the failure - this is the same answer, one call earlier.
    live = _routable(_gateway_order("image"), root)
    if live:
        return live[0]
    # None configured: the historical default, so the error a caller sees is the
    # familiar "OPENAI_API_KEY not set" rather than a surprise about a provider
    # they never mentioned.
    return "openai"


def _gateway_order(capability: str) -> tuple[str, ...]:
    try:
        from bgate_core import gateway as _gateway

        return _gateway.CAPABILITIES.get(capability) or ()
    except Exception:
        return ("kie", "krea", "openai")


def _routable(order: tuple[str, ...],
              root: str | os.PathLike | None = None) -> list[str]:
    """``order``, minus what has no key and minus what is provably drained.

    Balance is UNKNOWN for most providers and unknown is routable - the call
    itself is the probe. Only a balance that reads 0 skips a provider, which
    is the gateway's own semantics rather than a second opinion about them.

    A gateway that cannot answer (no network, a probe that raised) must not
    take art generation down with it, so key presence is the fallback and the
    order still holds.
    """
    rows: dict[str, dict] = {}
    drained: set[str] = set()
    try:
        from bgate_core import gateway as _gateway

        rows = {r["id"]: r for r in _gateway.status(root)}
        drained = {p for p, r in rows.items() if _gateway._drained(r)}
    except Exception:
        rows = {}
    live = []
    for one in order:
        if one in drained:
            continue
        row = rows.get(one)
        keyed = (bool(row.get("keyed")) if row is not None
                 else bool((os.environ.get(_env_of(one)) or "").strip()))
        if keyed:
            live.append(one)
    return live


def _env_of(provider_id: str) -> str:
    try:
        return by_id(provider_id).env
    except ProviderError:
        return ""


def ids() -> tuple[str, ...]:
    return tuple(p.id for p in PROVIDERS)


def env_vars() -> tuple[str, ...]:
    return tuple(p.env for p in PROVIDERS)


def _fingerprint(value: str) -> str:
    """The last four characters, and only when there is enough key that four
    cannot be most of it. Enough to answer "is this the key I think it is"
    against the provider's own dashboard, which is the only question a human
    actually asks of a key they cannot see."""
    value = (value or "").strip()
    return value[-4:] if len(value) >= 12 else ""


def _one_status(one: Provider, root: Optional[Path], from_file: dict,
                from_global: Optional[dict] = None) -> dict:
    from_global = from_global or {}
    live = (os.environ.get(one.env) or "").strip()
    stored = (from_file.get(one.env) or "").strip()
    shared = (from_global.get(one.env) or "").strip()

    # WHICH LAYER WON, stated. load_env deliberately lets a shell variable beat
    # both files and the project file beat the machine-wide one, so a panel
    # reading os.environ alone would report a key the user just saved as being
    # in force while a stale `set OPENAI_API_KEY=` in their shell profile is the
    # value actually being sent — the same class of lie the settings panel's
    # `source` column exists to stop.
    #
    # The project layer is checked BEFORE the global one because that is the
    # precedence: with the same key in both files the project's is the one in
    # force, and reporting "global" there would send someone to edit the file
    # that is not being read.
    if live and stored and live == stored:
        source = "env_file"
    elif live and shared and live == shared:
        source = "global_file"
    elif live:
        source = "environment"
    elif stored or shared:
        # The loader could not apply the file's value, which means the name is
        # present in os.environ as an EMPTY string — a shell that exported it
        # blank. Nothing generates, and nothing else on screen would say why.
        source = "shadowed"
    else:
        source = "unset"

    row = {
        "id": one.id,
        "label": one.label,
        "env": one.env,
        "powers": list(one.powers),
        "power_labels": [CAPABILITIES.get(p, p) for p in one.powers],
        "key_url": one.key_url,
        "help": one.help,
        "configured": bool(live),
        "in_env_file": bool(stored),
        "in_global_file": bool(shared),
        # Where a WRITE would land to change what is in force. Not the same
        # question as `source`: a key inherited from the global file is answered
        # "global" here, so a panel offering "change this" edits the file the
        # value is actually coming from rather than shadowing it with a project
        # copy the user then has to remember exists.
        "scope": "project" if stored else ("global" if shared else ""),
        "source": source,
        "last4": _fingerprint(live or stored or shared),
    }

    # The adapter's own verdict. A key can be set and the provider still
    # unusable (openai installed? krea reachable?), and the adapter is the only
    # thing that knows — so it answers, and a probe that explodes reports itself
    # as unavailable rather than taking the panel down.
    try:
        verdict = one.probe(root)
    except Exception as exc:  # noqa: BLE001 - a broken adapter is a red row
        verdict = {"available": False,
                   "reason": f"{type(exc).__name__}: {exc}"}
    row["available"] = bool(verdict.get("available"))
    row["reason"] = "" if row["available"] else str(verdict.get("reason") or "")
    if source == "shadowed":
        row["reason"] = (f"{one.env} is set to an empty value in this shell, "
                         f"which beats the .env — unset it and restart, or "
                         f"set the key in the shell instead")
    return row


def status(root: Optional[str | os.PathLike[str]] = None) -> list[dict]:
    """Every provider: what it powers, whether it is usable, and why not.

    ``root=None`` is a supported, meaningful call and not a degraded one: the
    machine-wide layer is read either way, so "what can this machine generate"
    has an answer with no project in sight.

    NEVER RETURNS A KEY. ``last4`` is the whole fingerprint; there is no field
    here, and no flag, that widens to the value.
    """
    try:
        envfile.load_env(root)
    except Exception:
        pass  # a panel that will not render because .env is odd helps nobody
    from_file = envfile.file_vars(root) if root else {}
    from_global = envfile.file_vars(envfile.global_dir())
    base = Path(root) if root else None
    return [_one_status(one, base, from_file, from_global) for one in PROVIDERS]


def status_for(root: Optional[str | os.PathLike[str]], provider_id: str) -> dict:
    one = by_id(provider_id)
    from_file = envfile.file_vars(root) if root else {}
    from_global = envfile.file_vars(envfile.global_dir())
    return _one_status(one, Path(root) if root else None, from_file, from_global)


def configured(root: Optional[str | os.PathLike[str]] = None) -> list[str]:
    """The ids that have a key right now, in auto-select order."""
    return [row["id"] for row in status(root) if row["configured"]]


def usable(root: Optional[str | os.PathLike[str]] = None) -> list[str]:
    """The ids whose ADAPTER says it can run, which is not the same list.

    ``configured`` answers "is the variable set". This answers the question a
    human is actually asking of a health check: could a generation start right
    now. An openai key with no ``openai`` package, a krea key the adapter cannot
    reach, a var exported empty - all configured, none usable. The doctor's
    art row counted the first list and printed "4 of 4 providers" for a machine
    with one usable option, which is the disagreement this pair exists to end.
    """
    return [row["id"] for row in status(root) if row["available"]]


def routing(root: Optional[str | os.PathLike[str]] = None) -> dict:
    """WHAT WOULD ACTUALLY HAPPEN, per capability, from the router itself.

    THE FAILURE THIS CLOSES, measured across three benchmark games: `bgate
    doctor` reported ``art_key  4 of 4 providers`` while the gateway that
    routes generation reported openai unkeyed, krea unkeyed, and one live
    option with no alternatives. Two surfaces answering one question from two
    interpretations of the environment, and the human-facing one was the
    optimistic wrong answer.

    So there is one function, and it DELEGATES: the per-capability order and
    the pick come from :mod:`bgate_core.gateway` (which owns doctrine order,
    keying and drained balances) and the character-work override comes from
    :func:`provider_for` (which owns the routing rule and the stored
    ``art.provider`` preference). Nothing here re-derives either. A caller that
    wants "is this machine able to make art" reads ``families`` and gets the
    same verdict the next generation call will act on.

    Cheap enough for a status panel: gateway.status caches its balance probes
    for two minutes and every other input is offline.
    """
    from bgate_core import gateway as _gateway

    try:
        rows = {r["id"]: r for r in _gateway.status(root)}
    except Exception as exc:  # noqa: BLE001 - a status panel must still render
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}",
                "families": {}, "character_work": ""}

    families: dict[str, dict] = {}
    for capability, order in _gateway.CAPABILITIES.items():
        picked = _gateway.pick(root, capability)
        unavailable = []
        for one in order:
            row = rows.get(one) or {}
            if not row.get("keyed"):
                unavailable.append({"id": one,
                                    "why": str(row.get("reason") or "no key")})
            elif _gateway._drained(row):
                unavailable.append({"id": one, "why": "keyed but drained "
                                                      "(balance reads 0)"})
        families[capability] = {
            "order": list(order),
            "provider": picked.get("provider"),
            "alternatives": list(picked.get("alternatives") or []),
            "why": picked.get("why", ""),
            "unavailable": unavailable,
        }

    # The one route that does NOT come off the capability table: identity work
    # is routed by provider_for (kie, then krea), and an `art.provider`
    # preference overrides everything. A panel that showed only the table would
    # name the provider a sprite sheet will not go to.
    try:
        character = provider_for("character", root=root)
    except Exception:
        character = ""
    return {"ok": True, "reason": "", "families": families,
            "character_work": character,
            "note": "families come from bgate_core.gateway - the same table "
                    "and the same pick the next generation call uses. "
                    "character_work is provider_for('character'), which "
                    "overrides the image order for sprite/identity jobs."}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

# Every pattern that would make git ignore a plain `.env` at the project root.
# Checked textually rather than by shelling out to `git check-ignore`: this runs
# inside a request handler, and a subprocess per keystroke-adjacent call is a
# cost with no upside when the answer only has to be right about the four
# spellings anyone actually writes.
_IGNORE_RULES = {".env", ".env*", ".env.*", "*.env", "/.env"}


def env_is_ignored(root: str | os.PathLike[str]) -> bool:
    """Would committing this project leave the .env out of it?"""
    base = Path(root)
    if not (base / ".git").exists():
        return True  # not a repo: nothing to leak it into
    try:
        text = (base / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.strip() in _IGNORE_RULES for line in text.splitlines())


def _protect(root: str | os.PathLike[str]) -> str:
    """Make sure .env is gitignored BEFORE a key goes into it.

    This is the failure this whole feature is one careless step away from
    repeating: following the documented setup once put an API key in a commit,
    which is why `bgate init` and `bgate adopt` stamp the ignore rules now. A
    project adopted before that change still has an unprotected .env, and a
    dashboard button that writes a key into it would re-create the incident with
    one click instead of a paragraph of instructions.

    Returns "" when nothing was needed, else what was done to .gitignore. Never
    silent — the caller reports it, because editing someone's .gitignore behind
    their back is its own kind of surprise.
    """
    if env_is_ignored(root):
        return ""
    try:
        from bgate_core import adopt
        return str(adopt.stamp_gitignore(root).get("action") or "")
    except Exception:
        return ""


# Where a key can be stored. "project" is the .env beside the game; "global" is
# ~/.bgate/.env, which every project on the machine inherits and which is the
# only one that exists when there is no project at all.
SCOPES = ("project", "global")


def _shell_owns(one: Provider, root: Optional[str | os.PathLike[str]]) -> bool:
    """Is the live value a SHELL export rather than something a file supplied?

    Asked before a write, because the answer decides whether the write is
    allowed to change what is in force. A shell export beats every file — that
    rule predates the global store and is what the ``shadowed`` status exists to
    surface — so a save that silently overwrote it would make the panel agree
    with itself and disagree with the process.

    Inferred rather than tracked: if the live value matches neither file, no
    file put it there.
    """
    live = (os.environ.get(one.env) or "").strip()
    if not live:
        return False
    known = {(envfile.file_vars(root).get(one.env) or "").strip() if root else "",
             (envfile.file_vars(envfile.global_dir()).get(one.env) or "").strip()}
    return live not in known


def _reapply(one: Provider, root: Optional[str | os.PathLike[str]],
             shell_owned: bool) -> None:
    """Make ``os.environ`` agree with the files, by the documented precedence.

    THE STEP THAT ONLY MATTERS ONCE THERE ARE TWO LAYERS, in both directions:
    writing a global key must NOT stamp over a project key that outranks it, and
    clearing a project key must UNCOVER the global one rather than leaving the
    provider unset until a restart. Assigning the value that was just written —
    which is what a single-store version could get away with — gets the first of
    those backwards and the second one silently wrong.
    """
    if shell_owned:
        return
    envfile.reset_cache()
    value = ""
    if root:
        value = (envfile.file_vars(root).get(one.env) or "").strip()
    if not value:
        value = (envfile.file_vars(envfile.global_dir()).get(one.env) or "").strip()
    if value:
        os.environ[one.env] = value
    else:
        os.environ.pop(one.env, None)


def _store(root: Optional[str | os.PathLike[str]], scope: str) -> Path:
    """The directory whose .env this write targets, or a refusal.

    A scope typo must not fall back to the other store: "I set my key and it did
    not take" and "I set my key into the wrong file" look identical from the
    outside, and only one of them is recoverable by trying again.
    """
    scope = (scope or "project").strip().lower()
    if scope not in SCOPES:
        raise ProviderError(
            f"unknown scope '{scope}' — 'project' (the .env beside this game) "
            "or 'global' (~/.bgate/.env, shared by every project on this "
            "machine)")
    if scope == "global":
        return envfile.global_dir()
    if not root:
        raise ProviderError(
            "there is no project here to store a key in. Use scope='global' to "
            "put it in ~/.bgate/.env, which every project inherits and which "
            "works with no project at all.")
    return Path(root)


def set_key(root: Optional[str | os.PathLike[str]], provider_id: str,
            value: str, *, actor: str = "", scope: str = "project") -> dict:
    """Store one provider's key and make it live NOW.

    ``scope='project'`` writes the .env beside the game; ``scope='global'``
    writes ``~/.bgate/.env``, which every project on this machine inherits and
    which is readable with no project at all. Project beats global when both
    hold the same key — see :func:`envfile.load_env`.

    Three steps, and all three are load-bearing:

    1. the .env is gitignored first (see :func:`_protect`);
    2. :func:`envfile.write_var` preserves the rest of the file, atomically;
    3. the running process is updated in place.

    Step 1 is a no-op for the global store and correctly so: ``~/.bgate`` is not
    a repository, so there is nothing there to leak a key into. That is not an
    exemption from the rule but the rule's own answer — :func:`env_is_ignored`
    already returns True for a directory with no ``.git``.

    Step 3 is the one that looks redundant and is not. ``load_project_env``
    refuses to overwrite a name already in ``os.environ`` — the shell must win —
    so after the very first save of a key the file is never again allowed to
    update the live value. Without the explicit assignment the user sets a key,
    nothing starts working, and they reasonably conclude the panel is broken.

    Returns the provider's status row (no key in it) plus what the write did.
    """
    one = by_id(provider_id)
    target = _store(root, scope)
    shell_owned = _shell_owns(one, root)
    protected = _protect(target)
    try:
        action = envfile.write_var(target, one.env, value)
    except envfile.EnvWriteError as exc:
        raise ProviderError(str(exc)) from None

    _reapply(one, root, shell_owned)

    # The ledger records THAT a key moved and who moved it, never the key. An
    # audit trail for credentials is worth having; an audit trail that contains
    # the credential is the leak with a timestamp on it.
    #
    # A GLOBAL write has no project ledger to land in, and that is a real gap
    # rather than a hidden one: the row is written to the project you are
    # standing in when there is one, so the machine-wide store is the one place
    # a key change is not audited. Said out loud in the docs rather than papered
    # over with a second log file nothing else reads.
    if root:
        _note(root, f"{one.label} API key set ({scope})", ref=one.env,
              actor=actor)

    row = status_for(root, one.id)
    row["write"] = action
    row["scope_written"] = "global" if scope == "global" else "project"
    row["gitignore"] = protected
    return row


def clear_key(root: Optional[str | os.PathLike[str]], provider_id: str, *,
              actor: str = "", scope: str = "project") -> dict:
    """Remove one provider's key from one store and from this process.

    Both halves, for the mirror of the reason ``set_key`` writes ``os.environ``:
    deleting the line alone leaves the value live until a restart, so the panel
    would say "not set" while generations kept billing the key it just cleared.

    AND THEN THE LAYERS ARE RE-READ, which is the part that only matters once
    there are two of them. Clearing a project key over a global one must UNCOVER
    the global one, not leave the provider unset until a restart — otherwise
    "remove this project's override" reads as "break generation here", and the
    fix (restart the server) is not a thing anyone would guess.
    """
    one = by_id(provider_id)
    target = _store(root, scope)
    shell_owned = _shell_owns(one, root)
    removed = envfile.remove_var(target, one.env)
    _reapply(one, root, shell_owned)
    if removed and root:
        _note(root, f"{one.label} API key cleared ({scope})", ref=one.env,
              actor=actor)
    row = status_for(root, one.id)
    row["write"] = "removed" if removed else "absent"
    row["scope_written"] = "global" if scope == "global" else "project"
    return row


def _note(root: str | os.PathLike[str], summary: str, *, ref: str,
          actor: str) -> None:
    """Best effort. A project whose activity table will not take the row must
    still get its key saved — the write already landed by the time we are here."""
    try:
        from bgate_core import activity
        activity.log(root, "settings", summary, ref=ref, actor=actor or "")
    except Exception:
        pass
