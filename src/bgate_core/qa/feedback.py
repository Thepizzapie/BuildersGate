"""Turning spoken playtest notes into classified, routed feedback.

Deterministic and lexical, like canon.py and for the same reason: it runs on
every segment with no model call, and it is honest about being a first pass. It
sorts and routes; it does not comprehend. Items land as 'new' and a human
promotes them — an offhand "huh, weird" must never become a ticket by itself.

Order matters in KIND_RULES: "I don't like the jump" is a fix, not a like, so
negation is checked before praise.
"""
from __future__ import annotations

import re

from ..store.util import tokens

# --- kind classification ---------------------------------------------------
# (kind, weight, pattern). Highest total weight wins; ties fall back to earlier.
KIND_RULES: list[tuple[str, int, str]] = [
    # Negated praise first — it is a complaint wearing a compliment's words.
    ("fix", 5, r"\b(?:don'?t|do not|doesn'?t|not really|didn'?t)\s+(?:like|love|enjoy)\b"),
    ("fix", 5, r"\bnot\s+(?:great|good|fun|satisfying|nice)\b"),

    ("fix", 4, r"\b(?:bugs?|broken|glitch(?:es|y)?|crash(?:es|ing)?|softlock|stuck|freez(?:e|ing)|frozen)\b"),
    # Speech-to-text does not preserve your word choice: "floaty" comes back as
    # "floating", "janky" as "janked". Match the stem, not the adjective you
    # happened to imagine. Observed on real audio.
    ("fix", 3, r"\b(?:jank\w*|float(?:y|ing|ey)|clunk\w*|sluggish|unresponsive|lag(?:gy|ging)?|chopp\w*|stiff|mushy|drift\w*)\b"),
    ("fix", 3, r"\b(?:annoying|frustrating|confusing|awkward|painful)\b"),
    ("fix", 3, r"\b(?:doesn'?t|does not|won'?t)\s+work\b"),
    ("fix", 2, r"\b(?:weird|wrong|off|bad|hate|ugly|terrible)\b"),
    ("fix", 2, r"\bshouldn'?t\b|\bshould not\b"),

    ("add", 4, r"\b(?:we should add|needs? (?:to have|a|an|more)|there should be)\b"),
    ("add", 3, r"\b(?:missing|lacking|no (?:sound|music|feedback|indicator))\b"),
    ("add", 3, r"\bwhat if (?:we|there|it|you)\b"),
    ("add", 2, r"\b(?:add|include|introduce)\b"),

    ("change", 4, r"\b(?:too (?:slow|fast|hard|easy|big|small|loud|quiet|long|short|much|many))\b"),
    ("change", 3, r"\b(?:tweak|adjust|rebalance|retune|instead of|rather than)\b"),
    ("change", 2, r"\b(?:change|make it|dial (?:it )?(?:up|down|back))\b"),
    ("change", 2, r"\b(?:more|less|faster|slower|bigger|smaller|higher|lower)\b"),

    ("like", 4, r"\b(?:i (?:really )?(?:like|love)|feels? (?:really )?(?:good|great|nice))\b"),
    ("like", 3, r"\b(?:satisfying|juicy|snappy|smooth|slick|perfect|awesome)\b"),
    ("like", 2, r"\b(?:nice|good|great|cool|fun|yes+)\b"),

    ("question", 3, r"^(?:why|how come|what does|is (?:that|it|this) supposed)\b"),
    ("question", 2, r"\?$"),
]

# --- seat routing ----------------------------------------------------------
# Plurals and inflections matter: "\benemy\b" silently misses "the ENEMIES are
# too fast" — a whole class of feedback lost to a word boundary. Match stems.
SEAT_RULES: list[tuple[str, str]] = [
    ("tech", r"\b(?:fps|framerates?|lag(?:gy|ging)?|stutter\w*|performance|crash\w*|load(?:s|ing)?|memory)\b"),
    ("audio", r"\b(?:sounds?|music|sfx|audio|loud|quiet|volume|tracks?|noise|song)\b"),
    ("art", r"\b(?:sprites?|models?|textures?|animation\w*|colou?rs?|lighting|shaders?|visuals?|looks?|art|ugly|readable)\b"),
    ("narrative", r"\b(?:story|dialogue|lore|quests?|characters?|writing|text|voice|lines?|npc)\b"),
    ("gameplay", r"\b(?:jump\w*|move(?:s|ment)?|controls?|feels?|physics|enem(?:y|ies)|damage|hits?|combat|balance|difficulty|speed|input|dash\w*|attack\w*)\b"),
    ("qa", r"\b(?:bugs?|broken|glitch(?:es|y)?|repro|softlock|stuck)\b"),
]

SEATS = ("director", "narrative", "gameplay", "tech", "art", "audio", "cinematic", "qa",
         "unassigned")
KINDS = ("like", "fix", "add", "change", "question", "note")

# Segments that carry no signal — filler, plus the canned phrases whisper
# hallucinates into silence (it was trained on subtitles, so quiet stretches come
# back as "Thanks for watching!"). Matched after punctuation is stripped.
#
# THE OUTER QUANTIFIER IS POSSESSIVE (`*+`) AND HAS TO STAY THAT WAY. With a
# plain `*`, a run of one letter can be partitioned between repetitions in
# exponentially many ways — `mm+` inside `(?:…)*` is the textbook shape — and
# every one of them is tried before the match is refused. Measured on this
# pattern: 32 m's took 0.5s, 36 took 3.6s, 41 took 26 SECONDS, doubling every
# two characters. is_noise() runs on every transcript segment, and a stretch of
# someone humming is exactly what whisper hands back, so the input that triggers
# it is ordinary use rather than an attack. Possessive: 4000 m's in 0.0000s.
#
# It costs nothing that anyone asks. Diffing both patterns over ~5,700 generated
# cases turned up four disagreements and no others — `uhhello`, `uhhelloo`,
# `uhhhello`, `uhhhelloo`, where `uh+` eats the h of hello and possessive will
# not give it back — and is_noise() still answers True for all four, because a
# string under three words is noise on the word count alone and never reaches
# this pattern's verdict.
_NOISE = re.compile(
    r"^(?:o?k(?:ay)?|um+|uh+|hmm+|mm+|yeah|yep|nope|right|so|and|but|well|"
    r"you|thanks? (?:for )?watching|thanks? (?:so much )?for watching|"
    r"subscribe|like and subscribe|bye+|hello+)*+$", re.I)

_PUNCT = re.compile(r"[^\w\s']+")

_MIN_WORDS = 3


def is_noise(text: str) -> bool:
    """Filler, or whisper's classic silence hallucinations."""
    stripped = _PUNCT.sub(" ", text).strip()
    stripped = re.sub(r"\s+", " ", stripped)
    if not stripped or _NOISE.match(stripped):
        return True
    return len(tokens(stripped)) < _MIN_WORDS


def classify(text: str) -> tuple[str, dict[str, int]]:
    """Best-guess kind for one utterance, plus the raw scores behind it."""
    low = text.lower().strip()
    scores: dict[str, int] = {}
    for kind, weight, pattern in KIND_RULES:
        if re.search(pattern, low):
            scores[kind] = scores.get(kind, 0) + weight
    if not scores:
        return "note", {}
    best = max(scores.items(), key=lambda kv: kv[1])[1]
    for kind, _w, _p in KIND_RULES:  # rule order breaks ties
        if scores.get(kind) == best:
            return kind, scores
    return "note", scores


def route(text: str) -> str:
    """Which seat owns this. First matching rule wins — order is priority."""
    low = text.lower()
    for seat, pattern in SEAT_RULES:
        if re.search(pattern, low):
            return seat
    return "unassigned"


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


# Seconds of silence that end one spoken thought. A playtest comment is a
# continuous 5-15s ramble about ONE thing; then a pause, then the next thing.
# Merging segments separated by less than this keeps one issue as ONE item
# instead of shredding it across sentences (which routed the same complaint to
# three different seats). Larger = fewer, chunkier items.
# 1.0s: a middle ground found from real playtest audio. Smaller shreds one
# thought across sentences; larger fuses distinct topics the user spoke
# back-to-back. No time threshold separates topics said with no pause between
# them — that final semantic grouping is the director's job (it reads the whole
# transcript for meaning). This just stops the worst over-fragmentation.
THOUGHT_GAP = 1.0


def group_thoughts(segments: list[dict], max_gap: float = THOUGHT_GAP) -> list[dict]:
    """Merge temporally-adjacent transcript segments into coherent thoughts.

    The inverse of sentence-splitting: instead of breaking a comment apart, we
    stitch consecutive segments back together while the speaker keeps talking,
    and only break when they pause (a gap > max_gap). One thought carries the
    full spoken context, so classify/route see the whole complaint at once.
    """
    thoughts: list[dict] = []
    cur: dict | None = None
    for seg in sorted(segments, key=lambda s: s.get("t_start", 0.0)):
        text = seg.get("text", "").strip()
        if not text:
            continue
        if cur is not None and seg["t_start"] - cur["t_end"] <= max_gap:
            cur["text"] = (cur["text"] + " " + text).strip()
            cur["t_end"] = seg["t_end"]
            cur["segment_ids"].append(seg.get("id"))
        else:
            if cur is not None:
                thoughts.append(cur)
            cur = {"text": text, "t_start": seg["t_start"], "t_end": seg["t_end"],
                   "segment_ids": [seg.get("id")]}
    if cur is not None:
        thoughts.append(cur)
    return thoughts


def extract(segments: list[dict], max_gap: float = THOUGHT_GAP) -> list[dict]:
    """Transcript segments -> candidate feedback items, ONE PER SPOKEN THOUGHT.

    Groups temporally-adjacent segments (see group_thoughts), drops pure-filler
    thoughts, then classifies and routes each on its FULL text — so a facing
    complaint spread over three sentences stays one gameplay item instead of
    three items across three seats. Everything survives as 'new'; nothing here
    decides what becomes work.
    """
    items = []
    for thought in group_thoughts(segments, max_gap):
        text = thought["text"].strip()
        if is_noise(text):
            continue
        kind, scores = classify(text)
        items.append({
            "segment_id": thought["segment_ids"][0],
            "segment_ids": thought["segment_ids"],
            "t": thought["t_start"],
            "t_end": thought["t_end"],
            "kind": kind,
            "text": text,
            "seat": route(text),
            "scores": scores,
        })
    return items


# --- decision-model triage ---------------------------------------------------
# THE LEXICAL PASS ABOVE IS A FIRST PASS AND SAYS SO. "It's like making my ears
# bleed" carries no KIND_RULES word at all and lands as a 'note'; "the music is
# fine but the guy keeps shooting me" routes to audio on its first noun. A
# decision model (Jev, bgate_adapters.jev) answers exactly these two questions -
# which kind, which seat - with a probability per option, for about a
# hundredth of a cent a thought. It REFINES, never replaces: no key, a failed
# call, or a pick below TRIAGE_MIN_CONFIDENCE keeps the lexical answer, and
# the item records which classifier spoke.
TRIAGE_KINDS = {
    "fix": "something is broken, wrong, unpleasant or hurts to play",
    "change": "it works but a value or behaviour should be tuned",
    "add": "something is missing and should be added",
    "like": "praise: keep this as it is",
    "question": "the player is confused or asking how something works",
    "note": "an observation with nothing to act on",
}
TRIAGE_SEATS = {
    "gameplay": "controls, movement, combat, enemies, difficulty, game feel",
    "level": "layout, pacing, platforms, pits, where things are placed",
    "art": "how it looks: sprites, colours, readability, animation",
    "audio": "sound effects, music, volume, mix",
    "tech": "performance, crashes, loading, frame rate, bugs in the engine",
    "narrative": "story, dialogue, text, characters' words",
    "unassigned": "none of the above",
}
TRIAGE_MIN_CONFIDENCE = 0.5
#: One call per spoken thought; a session past this many thoughts keeps the
#: lexical answer for the rest rather than spending without a ceiling.
TRIAGE_MAX_ITEMS = 200


def triage(items: list[dict], root=None, *, workers: int = 6) -> list[dict]:
    """Refine extract()'s kind and seat with the decision model, in place.

    Each item gains ``classifier`` ('jev' or 'lexical') and, when Jev spoke,
    ``triage`` {kind, seat, confidence, probabilities}. Returns ``items``."""
    try:
        from bgate_adapters import jev
        if not items or not jev.available(root)["available"]:
            for it in items:
                it.setdefault("classifier", "lexical")
            return items
    except Exception:                                            # noqa: BLE001
        return items

    questions = {
        "kind": {"type": "choice", "criteria": TRIAGE_KINDS,
                 "instructions": "A playtester said this out loud while "
                                 "playing a video game. What kind of feedback "
                                 "is it?"},
        "seat": {"type": "choice", "criteria": TRIAGE_SEATS,
                 "instructions": "Which game-dev discipline owns the fix?"},
    }

    def one(it: dict) -> None:
        it["classifier"] = "lexical"
        try:
            got = jev.decide(f'Playtester said: "{it["text"]}"', questions,
                             root=root, timeout=10.0)
        except Exception:                                        # noqa: BLE001
            return
        kind = got["answers"].get("kind") or {}
        seat = got["answers"].get("seat") or {}
        it["triage"] = {"kind": kind.get("choice"), "seat": seat.get("choice"),
                        "kind_confidence": kind.get("confidence"),
                        "seat_confidence": seat.get("confidence"),
                        "usd": got.get("usd")}
        if (kind.get("confidence") or 0) >= TRIAGE_MIN_CONFIDENCE \
                and kind.get("choice") in TRIAGE_KINDS:
            it["kind"] = kind["choice"]
            it["classifier"] = "jev"
        if (seat.get("confidence") or 0) >= TRIAGE_MIN_CONFIDENCE \
                and seat.get("choice") in TRIAGE_SEATS:
            it["seat"] = seat["choice"]
            it["classifier"] = "jev"

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, items[:TRIAGE_MAX_ITEMS]))
    for it in items[TRIAGE_MAX_ITEMS:]:
        it["classifier"] = "lexical"
    return items
