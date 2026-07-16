"""canon_check — the gate every narrative write passes through.

Scope, stated plainly: these are DETERMINISTIC LEXICAL CHECKS, not comprehension.
They reliably catch the cheap, common failures — a retired entity walking back
on stage, a made-up proper noun, "the siege lasted three years" against a locked
fact that says seven, a flat polarity flip. They will not catch a subtle thematic
contradiction, and they are not meant to.

The design intent is a cheap filter that runs on every write with no model call,
returning the canon an agent should have read plus anything that smells wrong.
An LLM adjudication layer can consume this output later; it cannot replace it,
because a model asked to check its own output for canon drift is the fox.

Verdicts:
  ok       — nothing to look at
  review   — soft flags; a human or Director seat should glance
  conflict — hard flags; the write should not land as canon
"""
from __future__ import annotations

import os
import re
from typing import Iterable

from . import lore
from .util import content_words, overlap, tokens

# Negation markers. A polarity mismatch between an established fact and new text
# that otherwise says the same thing is the single most common canon break.
NEG_MARKERS = frozenset("""
not never no none cannot cant isnt arent wasnt werent doesnt dont didnt wont
without refuses refused denies denied lacks lacked fails failed neither nor
""".split())

_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_PROPER = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*\b")

# Sentence-initial words and common prose openers that look proper but aren't.
_NOT_PROPER = frozenset("""
The A An And But Or So If When While After Before Then There Here This That These
Those It He She They We You I His Her Its Their Our Your My One Two Three Now
Later Once Every Each Some Any All No Not Yet Still Also However Because Since
""".split())

# Overlap needed before two statements are considered "about the same thing".
_POLARITY_THRESHOLD = 0.45
_NUMERIC_THRESHOLD = 0.35

_WORD_NUMBERS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12", "twenty": "20", "thirty": "30", "fifty": "50",
    "hundred": "100", "thousand": "1000",
}


def has_negation(text: str) -> bool:
    return bool(NEG_MARKERS & set(tokens(text.replace("'", ""))))


def numbers_in(text: str) -> set[str]:
    """Digits plus spelled-out numerals, normalized to a comparable form."""
    found = set(_NUMBER.findall(text.lower()))
    for word, digit in _WORD_NUMBERS.items():
        if word in tokens(text):
            found.add(digit)
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in found}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s.strip()]


def proper_nouns(text: str) -> set[str]:
    """Capitalized phrases that aren't sentence-openers. Rough by design."""
    out: set[str] = set()
    for sentence in sentences(text):
        words = sentence.split()
        for match in _PROPER.finditer(sentence):
            phrase = match.group(0)
            head = phrase.split()[0]
            # Skip a capitalized first word — could just be the sentence starting.
            if words and phrase.startswith(words[0]) and head in _NOT_PROPER:
                continue
            if head in _NOT_PROPER:
                continue
            out.add(phrase)
    return out


def _mentioned(root, text: str, entities: Iterable[str] | None) -> list[dict]:
    """Entities named in the text — by slug/name match, plus any forced in."""
    lowered = text.lower()
    hits: dict[str, dict] = {}
    for entity in lore.list_entities(root):
        name = entity["name"].lower()
        if name in lowered or entity["slug"] in lowered:
            hits[entity["slug"]] = entity
    for ref in entities or []:
        try:
            entity = lore.get_entity(root, ref)
            hits[entity["slug"]] = entity
        except LookupError:
            continue
    return list(hits.values())


def check(root: str | os.PathLike[str], text: str,
          entities: Iterable[str] | None = None) -> dict:
    """Check ``text`` against canon. See module docstring for what this catches."""
    mentions = _mentioned(root, text, entities)
    flags: list[dict] = []

    # --- Hard: retired entities must not appear in new content -------------
    for entity in mentions:
        if entity["status"] == "retired":
            flags.append({
                "level": "conflict",
                "code": "retired_entity",
                "entity": entity["slug"],
                "message": f"{entity['name']} is retired from canon but appears here.",
            })
        elif entity["status"] == "draft":
            flags.append({
                "level": "review",
                "code": "draft_entity",
                "entity": entity["slug"],
                "message": f"{entity['name']} is still draft — not settled canon.",
            })

    # --- Statement-level conflicts against the facts of mentioned entities --
    relevant: list[dict] = []
    for entity in mentions:
        relevant.extend(lore.facts_of(root, entity["id"]))

    text_sentences = sentences(text)
    for fact in relevant:
        statement = fact["statement"]
        for sentence in text_sentences:
            sim = overlap(statement, sentence)

            if sim >= _POLARITY_THRESHOLD and has_negation(statement) != has_negation(sentence):
                flags.append({
                    "level": "conflict" if fact["locked"] else "review",
                    "code": "polarity_conflict",
                    "fact_id": fact["id"],
                    "canon": statement,
                    "text": sentence,
                    "message": "Says the opposite of an established fact.",
                })
                continue

            if sim >= _NUMERIC_THRESHOLD:
                fact_nums, text_nums = numbers_in(statement), numbers_in(sentence)
                if fact_nums and text_nums and not (fact_nums & text_nums):
                    flags.append({
                        "level": "conflict" if fact["locked"] else "review",
                        "code": "numeric_conflict",
                        "fact_id": fact["id"],
                        "canon": statement,
                        "text": sentence,
                        "message": f"Numbers disagree: canon {sorted(fact_nums)} "
                                   f"vs text {sorted(text_nums)}.",
                    })

    # --- Soft: proper nouns with no entity behind them ----------------------
    known = {e["name"].lower() for e in lore.list_entities(root)}
    known |= {w for name in known for w in name.split()}
    unknown = sorted(
        p for p in proper_nouns(text)
        if p.lower() not in known and not (content_words(p) & known)
    )
    for name in unknown:
        flags.append({
            "level": "review",
            "code": "unknown_entity",
            "name": name,
            "message": f"{name!r} has no entity in the lore graph — "
                       "invention, or a name that drifted?",
        })

    hard = [f for f in flags if f["level"] == "conflict"]
    return {
        "verdict": "conflict" if hard else ("review" if flags else "ok"),
        "mentions": [{"slug": e["slug"], "name": e["name"], "status": e["status"]}
                     for e in mentions],
        "canon": [{"id": f["id"], "statement": f["statement"], "locked": bool(f["locked"])}
                  for f in relevant],
        "flags": flags,
    }
