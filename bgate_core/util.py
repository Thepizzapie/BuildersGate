"""Small shared helpers."""
from __future__ import annotations

import re
import unicodedata

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_WORD = re.compile(r"[a-z0-9']+")

# Words that carry no signal when comparing two statements for conflict.
STOPWORDS = frozenset("""
a an and are as at be been being but by can did do does for from had has have he
her his if in into is it its of on or she that the their them they this to was
were will with would you your
""".split())


def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return _SLUG_STRIP.sub("-", norm.lower()).strip("-") or "unnamed"


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def content_words(text: str) -> set[str]:
    return {t for t in tokens(text) if t not in STOPWORDS and len(t) > 2}


def overlap(a: str, b: str) -> float:
    """Jaccard overlap of content words. 0.0 when either side is empty."""
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]
