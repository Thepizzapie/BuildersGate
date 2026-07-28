"""Quality tiers — pick the right model for the job without knowing its name.

The problem this solves: there are ~27 image models across two providers, their
prices span 20x, and the right one depends entirely on what you are making. A
character anchor and a background plate are not the same job, and the model that
nails one is a waste of money (or a failure) on the other.

So callers never name a model. They say WHAT they are making and HOW GOOD it
needs to be:

    tiers.resolve("animation", "draft")    -> ("krea", "krea-2-medium", ...)
    tiers.bump("animation", "draft")       -> "standard"

THE LADDERS ARE MEASURED, NOT GUESSED. Same prompt, same anchor, same 4-frame
duck animation for the Project Manager Paladin:

    z-image        $0.003   4.5s   ONE giant portrait — ignored "4 frames" entirely
    flux-1-dev     $0.007  10.0s   4 frames in a row, but nobody ducks; props hallucinated
    krea-2-medium  $0.030  20.3s   4 frames, real crouch arc, character holds
    krea-2-large   $0.065  31.6s   same, slightly cleaner
    gpt-image-1    $0.042  28.3s   3 frames, one cropped off-canvas, garbled clipboard text

The lesson is the load-bearing one: **the cheap models do not fail on polish,
they fail on capability.** z-image cannot lay out a sheet at any price. So a
plain cheap-to-dear ladder would quietly hand you a portrait when you asked for
an animation. Every tier is therefore constrained by what the task NEEDS, and
`resolve` refuses to return a model that cannot do the job at all.
"""
from __future__ import annotations


from bgate_adapters import krea

# Cheapest first. `bump` walks right, `lower` walks left — the whole point is
# that a user can trade money for quality without learning a model catalogue.
TIERS = ("draft", "standard", "hero")

# What each kind of asset actually demands of a model.
#   style_refs — must condition on a pinned anchor (identity has to hold)
#   sheet      — must lay out several frames in one image
CAPABILITIES: dict[str, dict] = {
    "concept":    {"style_refs": False, "sheet": False},
    "anchor":     {"style_refs": True,  "sheet": False},
    "animation":  {"style_refs": True,  "sheet": True},
    "item":       {"style_refs": True,  "sheet": False},
    "background": {"style_refs": True,  "sheet": False},
    "tile":       {"style_refs": True,  "sheet": False},
    "ui":         {"style_refs": False, "sheet": False},
    # The rest of the pipeline already spoke these (chroma.KEYED_KINDS) while
    # the ladder did not, so the node's "what are you making?" list was missing
    # the thing this tool exists to make: a sprite sheet.
    "sheet":      {"style_refs": True,  "sheet": True},   # several frames, one image
    "gear":       {"style_refs": True,  "sheet": True},   # an aligned layer over a body sheet
    "sprite":     {"style_refs": True,  "sheet": False},  # one frame
    "portrait":   {"style_refs": True,  "sheet": False},
    "prop":       {"style_refs": True,  "sheet": False},
    "icon":       {"style_refs": True,  "sheet": False},
}

# (provider, model) per kind per tier. Two providers on purpose — gpt-image
# stays reachable so its output can be compared, not assumed worse.
LADDERS: dict[str, dict[str, tuple[str, str]]] = {
    # Exploration. Identity does not have to hold yet, so buy volume.
    "concept":    {"draft":    ("krea", "z-image"),
                   "standard": ("krea", "flux-1-dev"),
                   "hero":     ("krea", "krea-2-medium")},
    # The canonical character. This one image governs every later frame, so
    # even "draft" starts at a model that takes references seriously.
    #
    # gpt-image-1 is deliberately NOT the top rung. It is cheaper than
    # krea-2-large ($0.042 vs $0.065), so putting it at "hero" would make the
    # ladder cost less as it climbs — and the only head-to-head evidence we
    # have (the duck sheet) had it lose. It stays reachable by naming the
    # provider explicitly; it is not sold as an upgrade it has not earned.
    "anchor":     {"draft":    ("krea", "krea-2-medium"),
                   "standard": ("krea", "krea-2-large"),
                   "hero":     ("krea", "krea-2-large")},
    # Measured above: medium is the value pick, large is the finish, and
    # nothing cheaper can lay out a sheet at all.
    "animation":  {"draft":    ("krea", "krea-2-medium"),
                   "standard": ("krea", "krea-2-large"),
                   "hero":     ("krea", "krea-2-large")},
    "item":       {"draft":    ("krea", "flux-1-dev"),
                   "standard": ("krea", "krea-2-medium"),
                   "hero":     ("krea", "krea-2-large")},
    "background": {"draft":    ("krea", "flux-1-dev"),
                   "standard": ("krea", "krea-2-medium"),
                   "hero":     ("krea", "krea-2-large")},
    "tile":       {"draft":    ("krea", "flux-1-dev"),
                   "standard": ("krea", "krea-2-medium"),
                   "hero":     ("krea", "krea-2-large")},
    # Prompt-only work where adherence beats style transfer.
    "ui":         {"draft":    ("krea", "z-image"),
                   "standard": ("krea", "imagen-4-fast"),
                   "hero":     ("krea", "imagen-4")},

    # Multi-frame work. Same floor as `animation` and for the same measured
    # reason: nothing cheaper can lay out several frames in one image, so the
    # cheap rungs are not offered rather than offered and disappointing.
    "sheet":      {"draft":    ("krea", "krea-2-medium"),
                   "standard": ("krea", "krea-2-large"),
                   "hero":     ("krea", "krea-2-large")},
    "gear":       {"draft":    ("krea", "krea-2-medium"),
                   "standard": ("krea", "krea-2-large"),
                   "hero":     ("krea", "krea-2-large")},

    # Single frames that must stay on-model — identity matters, layout does not.
    "sprite":     {"draft":    ("krea", "krea-2-medium"),
                   "standard": ("krea", "krea-2-large"),
                   "hero":     ("krea", "krea-2-large")},
    "portrait":   {"draft":    ("krea", "krea-2-medium"),
                   "standard": ("krea", "krea-2-large"),
                   "hero":     ("krea", "krea-2-large")},

    # Objects. Cheap sweeps are genuinely useful here — a prop that misses is
    # thrown away, not re-anchored.
    "prop":       {"draft":    ("krea", "flux-1-dev"),
                   "standard": ("krea", "krea-2-medium"),
                   "hero":     ("krea", "krea-2-large")},
    "icon":       {"draft":    ("krea", "flux-1-dev"),
                   "standard": ("krea", "krea-2-medium"),
                   "hero":     ("krea", "krea-2-large")},
}

DEFAULT_TIER = "standard"

# Models that can lay out several frames in one image. Everything absent is
# assumed unable — proven by the sweep, not inferred from marketing copy.
SHEET_CAPABLE = {"krea-2-medium", "krea-2-large", "gpt-image-1"}


class NoSuchTier(ValueError):
    """The caller asked for a tier or kind that does not exist."""


def kinds() -> list[str]:
    return sorted(LADDERS)


def _check(kind: str, tier: str) -> None:
    if kind not in LADDERS:
        raise NoSuchTier(f"unknown asset kind {kind!r} — known: {kinds()}")
    if tier not in TIERS:
        raise NoSuchTier(f"unknown tier {tier!r} — tiers are {list(TIERS)}")


def resolve(kind: str, tier: str = DEFAULT_TIER) -> dict:
    """The provider and model for this job, plus what it will cost.

    Raises rather than silently downgrading: a model that cannot lay out a
    sheet must never be handed an animation, because the failure is a portrait
    that looks fine in isolation and is useless in the game.
    """
    _check(kind, tier)
    provider, model = LADDERS[kind][tier]
    needs = CAPABILITIES[kind]

    if needs["sheet"] and model not in SHEET_CAPABLE:
        raise NoSuchTier(
            f"{model} cannot lay out a multi-frame sheet, which {kind} needs — "
            f"the ladder for {kind} is misconfigured")
    if needs["style_refs"] and provider == "krea":
        spec = krea.MODELS.get(model) or {}
        if not spec.get("style_refs"):
            raise NoSuchTier(
                f"{model} takes no style references, which {kind} needs to stay "
                "on-model")

    usd = (krea.price_for(model, style_refs=1 if needs["style_refs"] else 0)
           if provider == "krea" else _openai_price(model))
    return {"kind": kind, "tier": tier, "provider": provider, "model": model,
            "usd": usd, "needs": dict(needs),
            "note": (krea.MODELS.get(model) or {}).get("note", "")}


def _openai_price(model: str) -> float:
    from bgate_adapters import imagegen
    return imagegen.price_per_image("medium")


def bump(kind: str, tier: str = DEFAULT_TIER) -> str:
    """One step better. Already at the top? Stay there — the caller asked for
    more quality, not for an error."""
    _check(kind, tier)
    return TIERS[min(TIERS.index(tier) + 1, len(TIERS) - 1)]


def lower(kind: str, tier: str = DEFAULT_TIER) -> str:
    """One step cheaper, floored at the bottom of the ladder."""
    _check(kind, tier)
    return TIERS[max(TIERS.index(tier) - 1, 0)]


def flat_rungs(kind: str) -> list[str]:
    """Tiers that resolve to the same model as the rung below.

    Honest reporting rather than invented variety: for sheet work nothing
    outranks krea-2-large today, so `animation` genuinely tops out early. A UI
    should grey those rungs instead of implying a better option exists.
    """
    _check(kind, TIERS[0])
    out, seen = [], None
    for tier in TIERS:
        model = LADDERS[kind][tier][1]
        if model == seen:
            out.append(tier)
        seen = model
    return out


def ladder(kind: str) -> list[dict]:
    """Every rung for one kind, cheapest first — what a UI renders as a slider."""
    _check(kind, TIERS[0])
    return [resolve(kind, t) for t in TIERS]


def cheapest_capable(kind: str) -> dict:
    """The least you can spend and still get a usable result for this kind."""
    for tier in TIERS:
        try:
            return resolve(kind, tier)
        except NoSuchTier:
            continue
    raise NoSuchTier(f"no tier of {kind!r} resolves to a capable model")


def estimate(kind: str, tier: str = DEFAULT_TIER, count: int = 1) -> float:
    """What `count` generations of this kind cost at this tier."""
    return round(resolve(kind, tier)["usd"] * max(0, int(count)), 4)
