"""What can actually be generated HERE, by provider and modality.

One question, answered in one place: which providers are usable on this
project right now (a key is set, or the thing is installed), and which model
ids each one serves per modality — image, video, music, speech, text, 3d.
The Settings panel's model pickers read this, filtered, so a person swapping
models chooses from things that will actually run rather than typing ids from
a docs page.

THE ADAPTER TABLES ARE THE SOURCE, NOT A DOCS SCRAPE. krea.MODELS,
kie.MODELS/SUNO_MODELS, deepgram.USD_PER_1K_CHARS are each the list their
adapter can genuinely route (request shape verified, priced where known). The
providers serve far more — kie alone markets dozens of video models behind
per-family APIs with different request shapes — and an id offered here that
the adapter cannot drive is a dropdown entry that fails at generation time.
New ids join by joining the adapter's table (or, for video,
cinematic_register_model, which probes the id against the live API first).

Anthropic appears for the AGENT model settings (dispatch.model,
console.model, brainstorm.model): those name Claude CLI models, and the CLI's
own short aliases are the values the settings take.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

# The Claude Code CLI's model aliases — what every *agent* model setting
# takes. 'fable' is the Claude 5 flagship alias; the full ids also work but
# the aliases survive model-version bumps.
AGENT_MODELS = ("sonnet", "opus", "haiku", "fable")

# Text models for API-side text calls (promptwriter). The default first.
OPENAI_TEXT_MODELS = ("gpt-4o-mini", "gpt-5.6-luna", "gpt-5.6-terra",
                      "gpt-5.6-sol")

def _local_available() -> bool:
    try:
        from bgate_adapters import localgen

        return bool(localgen.available().get("available"))
    except Exception:
        return False


def _claude_cli_present() -> bool:
    if shutil.which("claude"):
        return True
    fallback = Path.home() / ".local" / "bin" / (
        "claude.exe" if os.name == "nt" else "claude")
    return fallback.exists()


def configured(root=None) -> dict[str, bool]:
    """Which providers are usable right now. Keys are presence-checked only —
    never validated with a paid call from here."""
    from . import providers

    keyed = {row["id"]: bool(row["configured"])
             for row in providers.status(root)}
    return {
        "openai": keyed.get("openai", False),
        "krea": keyed.get("krea", False),
        "kie": keyed.get("kie", False),
        "deepgram": keyed.get("deepgram", False),
        "local": _local_available(),
        "anthropic": _claude_cli_present(),
    }


def catalog(root=None) -> dict:
    """{provider: {modality: [model ids]}} for CONFIGURED providers only.

    "Only show those for which a key is set" is the contract: an unconfigured
    provider contributes nothing, so a picker built from this cannot offer a
    model that fails on its first call with a missing-key error.
    """
    live = configured(root)
    out: dict[str, dict[str, list[str]]] = {}
    if live["openai"]:
        out["openai"] = {"image": ["gpt-image-1"],
                         "text": list(OPENAI_TEXT_MODELS)}
    if live["krea"]:
        try:
            from bgate_adapters import krea

            out["krea"] = {"image": sorted(krea.MODELS),
                           "3d": sorted(getattr(krea, "MODELS_3D", {}) or ())}
        except Exception:
            out["krea"] = {"image": [], "3d": []}
    if live["kie"]:
        try:
            from bgate_adapters import kie

            out["kie"] = {"image": sorted(kie.IMAGE_MODELS),
                          "video": sorted(kie.VIDEO_MODELS),
                          "music": list(kie.SUNO_MODELS)}
        except Exception:
            out["kie"] = {"image": [], "video": [], "music": []}
    if live["deepgram"]:
        try:
            from bgate_adapters import deepgram

            out["deepgram"] = {"speech": sorted(deepgram.USD_PER_1K_CHARS)}
        except Exception:
            out["deepgram"] = {"speech": []}
    if live["local"]:
        model = (os.environ.get("BGATE_LOCAL_IMAGE_MODEL") or "").strip()
        out["local"] = {"image": [model] if model else []}
    if live["anthropic"]:
        out["anthropic"] = {"agent": list(AGENT_MODELS)}
    return out


def options(root, kind: str) -> list[str]:
    """Dropdown values for ONE settings field, from the configured catalog.

    ``kind`` names the field's need, not a provider — the union of every
    configured provider's list for that modality, so switching a provider
    never strands the picker. Unknown kinds answer [] and the field renders
    as the free-text input it always was.
    """
    live = catalog(root)

    def gather(modality: str) -> list[str]:
        seen: list[str] = []
        for lists in live.values():
            for m in lists.get(modality, ()):
                if m and m not in seen:
                    seen.append(m)
        return seen

    if kind == "image-providers":
        # 'auto' is the routing rules; the rest are only offered configured.
        names = [p for p in ("openai", "krea", "kie", "local") if p in live]
        return ["auto"] + names
    if kind == "image-models":
        return gather("image")
    if kind == "video-models":
        return gather("video")
    if kind == "music-models":
        return gather("music")
    if kind == "speech-models":
        return gather("speech")
    if kind == "text-models":
        return gather("text")
    if kind == "agent-models":
        return list(AGENT_MODELS)
    return []
