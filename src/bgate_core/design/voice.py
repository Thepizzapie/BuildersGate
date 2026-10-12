"""Corny-copy check: player-facing text should sound real, not written.

WHY THIS EXISTS. Vibe Coder (2026-10-12) shipped lines like "I have rotated my
confidence, not the key", "It felt like it should, and I committed to the
bit", "Here is the same diff, again, but warmer" and "Prod is watching." The
human's verdict: "your wording and phrasing is so corny in games". Every line
was a punchline. Agents write writer's-room zingers by default: self-aware,
winking, twist-at-the-end, every sentence a joke.

THE RULE (also in every writing seat's brief, see RULES): write the thing the
game depicts, the way it really talks. An AI assistant says "You're absolutely
right! I've updated the test to match the current behavior." A PM writes "can
we get this out today?". A status bar says "Prod health 64%". The comedy comes
from the SITUATION the player is in (they know the test now always passes),
never from a character announcing the joke. One line in twenty may be a gag;
the rest is deadpan and accurate.

HOW IT CHECKS. Two layers, both cheap:
- lexical tells (no key needed): twist constructions ("X, not Y"), meta
  ("the bit", "joke"), dramatic taglines ("...is watching"), stacked
  exclamation marks, accusatory tags ("Root cause: you"), sitcom openers.
- a decision model (Jev, when a key is set) asked one question per line: is
  it trying to be funny (a quip, pun, wink, twist, punchline)? Measured on
  Vibe Coder's lines it separates cleanly: the zingers scored 0.71-0.87, the
  sincere lines 0.03-0.09 ("You're absolutely right! Here is the updated
  change." 0.05). A second question, "would the real thing say this?", was
  tried and dropped: it scored everything 0.4-0.66 and separated nothing.
A scene passes with at most GAG_BUDGET of its lines flagged.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

#: Given to every seat that writes player-facing text.
RULES = (
    "VOICE: write what the depicted thing really says, the way it really says "
    "it, played straight. Real apps are terse and functional; real people at "
    "work are brief and a little tired; real AI assistants are sincere, "
    "upbeat and confidently wrong. The joke is the SITUATION the player sees "
    "(the diff deletes the test while the assistant says the tests pass), "
    "never a line that announces it. NO: zingers, twist endings ('X, not Y'), "
    "puns, winks at the audience, characters who know they are funny, "
    "taglines ('Prod is watching.'), accusatory tags ('Root cause: you'), "
    "every-line-a-joke. At most one line in twenty is an outright gag. If a "
    "line would get a rimshot, cut the rimshot. Run copy_check on new text.")

TRYING_FLAG = 0.6
GAG_BUDGET = 0.05

_TELLS: list[tuple[str, re.Pattern]] = [
    ("twist construction ('X, not Y')",
     re.compile(r"\b\w+[^,.!?]{0,30},\s+not\s+(?:the|my|your|a)\s+\w+", re.I)),
    ("meta / self-aware ('the bit', 'joke', 'in my defense')",
     re.compile(r"\b(the bit|a bit|joke|punchline|in my defen[cs]e|plot twist|"
                r"spoiler|narrator|fourth wall)\b", re.I)),
    ("dramatic tagline ('... is watching', 'what could go wrong')",
     re.compile(r"\b(is|are) watching\b|what could (possibly )?go wrong|"
                r"famous last words|you had one job|this is fine", re.I)),
    ("accusatory tag ('Root cause: you')",
     re.compile(r":\s*you\b\.?$|\byou did this\b", re.I)),
    ("stacked exclamation", re.compile(r"!.*!|!!")),
    ("sitcom escalation ('but warmer', 'with more confidence', 'very proud of us')",
     re.compile(r"\b(but|with) (warmer|more confidence|extra|feeling)\b|"
                r"proud of us|rotated my|i committed\b|"
                r"by (changing|doing|fixing) nothing", re.I)),
]


def lint(line: str) -> list[str]:
    """Lexical tells in one line - the reasons, empty when none."""
    text = str(line or "").strip()
    return [name for name, pat in _TELLS if pat.search(text)]


def check(lines: Iterable[str], *, context: str = "", root: Any = None,
          use_model: bool = True) -> dict:
    """Flag corny lines. ``context`` names what is speaking/where (e.g. 'an AI
    coding assistant's chat replies in a game parodying AI coding tools').

    Returns {ok, flagged: [{line, tells, trying, real}], checked, model}."""
    items = [str(x).strip() for x in lines if str(x or "").strip()]
    out: list[dict] = []
    model_used = False
    jev = None
    if use_model:
        try:
            from bgate_adapters import jev as _jev
            if _jev.available(root)["available"]:
                jev = _jev
        except Exception:                                        # noqa: BLE001
            jev = None
    question = {"trying": {"type": "noul", "instructions": (
        "Is this line trying to be funny - a quip, pun, wink, twist ending or "
        "punchline - rather than just saying something"
        + (f" the way {context} would" if context else "") + "?")}}
    for line in items:
        tells = lint(line)
        trying = None
        if jev is not None:
            try:
                got = jev.decide(f'Line of game text: "{line}"', question,
                                 root=root, timeout=10.0)
                trying = (got["answers"].get("trying") or {}).get("noul")
                model_used = True
            except Exception:                                    # noqa: BLE001
                pass
        if tells or (trying is not None and trying >= TRYING_FLAG):
            out.append({"line": line, "tells": tells,
                        "trying": None if trying is None else round(trying, 2)})
    share = len(out) / len(items) if items else 0.0
    return {"ok": share <= GAG_BUDGET, "checked": len(items),
            "flagged_share": round(share, 3), "flagged": out,
            "model": model_used, "rules": RULES}


# ── finding the words in a project ─────────────────────────────────────────
#: JSON keys whose values are code or ids, never text a player reads.
_CODE_KEYS = {"lines", "hunks", "header", "path", "files", "id", "line_text",
              "file", "type", "kind", "scene", "key", "url", "src", "href"}
# Double-, single- and back-quoted literals on one line; escapes allowed.
_STRING = re.compile(r'"((?:[^"\\\n]|\\.){8,240})"'
                     r"|'((?:[^'\\\n]|\\.){8,240})'"
                     r"|`([^`\n${}]{8,240})`")
_CODEISH = re.compile(r"[{}<>=;]|^[\w\-./:#%]+$|\b(px|rgba?|var|function|"
                      r"return|const|import)\b|\[nt]")


_FRAGMENT = re.compile(r"\w\(|\)\s*[,|.]|\?\?|\|\||&&|\]\s*,|:\s*$|^\W")


def _looks_like_prose(s: str) -> bool:
    s = s.strip()
    return (" " in s and len(s) >= 8 and not _CODEISH.search(s)
            and not _FRAGMENT.search(s)
            and sum(c.isalpha() for c in s) >= 0.6 * len(s.replace(" ", "")))


def scan(root: Any, paths: Iterable[str] = ()) -> list[str]:
    """Player-facing lines in a project: string literals that read as prose in
    its scripts, and text values in its JSON content. ``paths`` (globs,
    relative to the root) override the defaults."""
    import json
    from pathlib import Path

    base = Path(root)
    globs = list(paths) or ["src/**/*.ts", "src/**/*.tsx", "content/**/*.json",
                            "scripts/**/*.gd", "dialogue/**/*.json",
                            "scenes/**/*.tscn"]
    seen: list[str] = []

    def add(s: str) -> None:
        s = s.strip()
        if _looks_like_prose(s) and s not in seen:
            seen.append(s)

    def walk(v: Any, key: str = "") -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k not in _CODE_KEYS:
                    walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str):
            for part in v.split("\n\n"):
                add(part)

    for pattern in globs:
        for f in sorted(base.glob(pattern)):
            rel = f.as_posix()
            if "/bgate/" in rel or ".test." in rel or "/addons/" in rel:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f.suffix == ".json":
                try:
                    walk(json.loads(text))
                except ValueError:
                    pass
                continue
            for m in _STRING.finditer(text):
                add(next(g for g in m.groups() if g is not None))
    return seen
