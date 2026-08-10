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
ART_CAPABILITIES = frozenset({"image_2d", "model_3d", "audio", "video"})


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
# kie is LAST among the image providers deliberately, and not because it is
# worse: it is the only one here that cannot condition on a local pinned anchor
# (its reference documents every image field as a URI and says nothing about
# base64), so auto-selecting it would quietly turn anchored character work into
# unanchored prompt-only work. It is the right choice when a human names it, and
# the wrong default. Its music and video are unaffected — nothing else in this
# product can do either at all.
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


def _one_status(one: Provider, root: Optional[Path], from_file: dict) -> dict:
    live = (os.environ.get(one.env) or "").strip()
    stored = (from_file.get(one.env) or "").strip()

    # WHICH LAYER WON, stated. load_project_env deliberately lets a shell
    # variable beat the file, so a panel reading os.environ alone would report a
    # key the user just saved as being in force while a stale `set
    # OPENAI_API_KEY=` in their shell profile is the value actually being sent —
    # the same class of lie the settings panel's `source` column exists to stop.
    if live and stored and live == stored:
        source = "env_file"
    elif live:
        source = "environment"
    elif stored:
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
        "source": source,
        "last4": _fingerprint(live or stored),
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

    NEVER RETURNS A KEY. ``last4`` is the whole fingerprint; there is no field
    here, and no flag, that widens to the value.
    """
    if root:
        try:
            envfile.load_project_env(root)
        except Exception:
            pass  # a panel that will not render because .env is odd helps nobody
    from_file = envfile.file_vars(root) if root else {}
    base = Path(root) if root else None
    return [_one_status(one, base, from_file) for one in PROVIDERS]


def status_for(root: Optional[str | os.PathLike[str]], provider_id: str) -> dict:
    one = by_id(provider_id)
    from_file = envfile.file_vars(root) if root else {}
    return _one_status(one, Path(root) if root else None, from_file)


def configured(root: Optional[str | os.PathLike[str]] = None) -> list[str]:
    """The ids that have a key right now, in auto-select order."""
    return [row["id"] for row in status(root) if row["configured"]]


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


def set_key(root: str | os.PathLike[str], provider_id: str, value: str, *,
            actor: str = "") -> dict:
    """Store one provider's key in the project's .env and make it live NOW.

    Three steps, and all three are load-bearing:

    1. the .env is gitignored first (see :func:`_protect`);
    2. :func:`envfile.write_var` preserves the rest of the file, atomically;
    3. the running process is updated in place.

    Step 3 is the one that looks redundant and is not. ``load_project_env``
    refuses to overwrite a name already in ``os.environ`` — the shell must win —
    so after the very first save of a key the file is never again allowed to
    update the live value. Without the explicit assignment the user sets a key,
    nothing starts working, and they reasonably conclude the panel is broken.

    Returns the provider's status row (no key in it) plus what the write did.
    """
    one = by_id(provider_id)
    protected = _protect(root)
    try:
        action = envfile.write_var(root, one.env, value)
    except envfile.EnvWriteError as exc:
        raise ProviderError(str(exc)) from None

    envfile.reset_cache()
    os.environ[one.env] = (value or "").strip()

    # The ledger records THAT a key moved and who moved it, never the key. An
    # audit trail for credentials is worth having; an audit trail that contains
    # the credential is the leak with a timestamp on it.
    _note(root, f"{one.label} API key set", ref=one.env, actor=actor)

    row = status_for(root, one.id)
    row["write"] = action
    row["gitignore"] = protected
    return row


def clear_key(root: str | os.PathLike[str], provider_id: str, *,
              actor: str = "") -> dict:
    """Remove one provider's key from the .env and from this process.

    Both halves, for the mirror of the reason ``set_key`` writes ``os.environ``:
    deleting the line alone leaves the value live until a restart, so the panel
    would say "not set" while generations kept billing the key it just cleared.
    """
    one = by_id(provider_id)
    removed = envfile.remove_var(root, one.env)
    envfile.reset_cache()
    os.environ.pop(one.env, None)
    if removed:
        _note(root, f"{one.label} API key cleared", ref=one.env, actor=actor)
    row = status_for(root, one.id)
    row["write"] = "removed" if removed else "absent"
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
