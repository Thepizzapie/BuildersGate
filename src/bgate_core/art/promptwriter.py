"""Turn a rough note into an image prompt. One API call, no agent.

This exists because of a design mistake worth naming. The first version of the
"prompt writer" node was an AGENT step: clicking it queued a work item for the
director seat, which needed a Claude Code session dispatched, with a lane hook
and a lifecycle, to rewrite one sentence. The node sat at "queued" looking like
it was running, did nothing, and refused a second click with engine vocabulary.

Reaching for a whole agent to improve a prompt is using the heaviest mechanism
in the codebase because it happened to be the one already wired. A prompt is a
paragraph. It costs a fraction of a cent and about two seconds through the same
chat-completions path the vision judge already uses.

Deliberately NOT here: the art direction. `chroma.generate` appends the bible's
locked constraints to every prompt at generation time, so adding them here would
say everything twice — and a prompt that repeats itself steers worse, not better.
This writes the SUBJECT; the bible governs the LOOK.
"""
from __future__ import annotations

import os
import time
from typing import Any

# Cheap and fast on purpose. This is a rewrite, not a reasoning task.
DEFAULT_MODEL = "gpt-4o-mini"

# Rough, per 1k tokens; a rewrite is a few hundred tokens in and out, so the
# real figure is a small fraction of a cent. Recorded so the ledger is honest
# rather than pretending text is free.
USD_PER_CALL = 0.0002

_SYSTEM = (
    "You rewrite rough notes into prompts for an image model that draws game "
    "assets. Return ONE prompt and nothing else — no preamble, no quotes, no "
    "commentary, no options.\n"
    "Rules:\n"
    "- Keep every concrete noun the author wrote. You are sharpening their "
    "idea, not replacing it.\n"
    "- Describe the SUBJECT: what it is, its pose or action, its framing, and "
    "what must be legible.\n"
    "- Do NOT invent an art style, palette, rendering technique or projection. "
    "Those are set elsewhere and your guesses will fight them.\n"
    "- No text, letters, words, logos or watermarks in the image.\n"
    "- Under 90 words."
)


def available(root: Any = None) -> dict:
    """Can we write a prompt? Presence of the key only — no spend to find out."""
    if root:
        try:
            from ..store import envfile
            envfile.load_project_env(root)
        except Exception:
            pass
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        return {"available": False,
                "reason": "OPENAI_API_KEY not set — put it in the project's "
                          ".env (gitignored, loaded per project)"}
    return {"available": True, "model": _model(root)}


def _model(root: Any = None) -> str:
    """Env, then the stored preference (text.model), then the cheap default —
    the same precedence every other model choice in the registry follows."""
    forced = (os.environ.get("BGATE_PROMPT_MODEL") or "").strip()
    if forced:
        return forced
    if root is not None:
        try:
            from ..store import settings as _settings

            preferred = str(_settings.get(root, "text.model") or "").strip()
            if preferred:
                return preferred
        except Exception:
            pass
    return DEFAULT_MODEL


def expand(text: str, *, subject: str = "", task_kind: str = "",
           root: Any = None, timeout: float = 60.0) -> dict:
    """Rewrite `text` into an image prompt.

    Returns the adapters' shared shape — ``{ok, text, seconds, estimated_usd}``
    or ``{ok: False, error}`` — so a caller does not care whether the thing that
    produced a value was a text model or an image model.
    """
    started = time.monotonic()
    note = (text or "").strip()
    if not note:
        return {"ok": False, "error": "nothing to expand — write a rough note first",
                "seconds": 0.0, "estimated_usd": 0.0}

    ready = available(root)
    if not ready["available"]:
        return {"ok": False, "error": ready["reason"],
                "seconds": 0.0, "estimated_usd": 0.0}

    ask = note
    if subject:
        ask = f"Subject / reference: {subject}\n\nNote: {note}"
    if task_kind:
        ask = f"This is a {task_kind} asset.\n{ask}"

    try:
        from openai import OpenAI

        client = OpenAI(timeout=timeout)
        reply = client.chat.completions.create(
            model=_model(root),
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": ask}],
            temperature=0.7,
        )
        written = (reply.choices[0].message.content or "").strip()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.monotonic() - started, 2),
                "estimated_usd": 0.0}

    if not written:
        return {"ok": False, "error": "the model returned an empty prompt",
                "seconds": round(time.monotonic() - started, 2),
                "estimated_usd": USD_PER_CALL}

    # Models like to wrap a single answer in quotes despite being told not to.
    if len(written) > 1 and written[0] == written[-1] and written[0] in "\"'":
        written = written[1:-1].strip()

    return {"ok": True, "text": written, "model": _model(root),
            "seconds": round(time.monotonic() - started, 2),
            "estimated_usd": USD_PER_CALL}
