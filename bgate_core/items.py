"""The item-art pipeline — item-as-object generation, class-templated.

Two kinds of gear art have OPPOSITE economics, and this module owns the cheap
one. An item-as-object (an inventory icon, a potion, a thrown axe, an arrow) is
a single transparent image with no animation coupling: one prompt template per
class, a parameter grid, N variants. The expensive kind — gear WORN on an
animated fighter, which must move correctly through every attack frame — is the
equip/layer system (templates/2d gear_rig.gd) fed by a separate rig pipeline.

The whole point is that variants are cheap and classes are expensive. A class
carries an INVARIANT style clause (framing, light, scale, background) so an
entire inventory reads as one set; a variant only swaps the item's material /
element / rarity. That split is what lets Codex mint "a plethora of gear" from a
tiny, typed contract instead of freehand prompting — build_prompt is the
contract, plan_variants is the batch.

This module is pure: taxonomy + prompt/plan/manifest building, no network and no
disk. The MCP tools (item_generate / item_variants) do the I/O — they call the
gpt-image adapter, archive, track, and write the manifest that the equip system
and gameplay read back.
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# The class taxonomy — the slot contract, made concrete
# ---------------------------------------------------------------------------
# `slot` is the equip target the equip/layer system understands. Worn classes
# (worn=True) map to a fighter gear layer of the same name; the rest are objects
# that live in inventory or fly as projectiles, never parented to the rig.
ITEM_CLASSES: dict[str, dict] = {
    "main_hand": {
        "label": "Main-hand weapon",
        "slot": "main_hand", "worn": True,
        "subject": "a one-handed melee weapon held in the right hand",
        "framing": "shown at a slight 3/4 diagonal, blade/head up and to the "
                   "right, grip toward lower-left, as if ready to be gripped",
    },
    "off_hand": {
        "label": "Off-hand (weapon or shield)",
        "slot": "off_hand", "worn": True,
        "subject": "an off-hand item — a shield, buckler, dagger, or focus",
        "framing": "shown face-on (shields) or at a slight diagonal (blades), "
                   "sized to pair with a main-hand weapon of the same set",
    },
    "head": {
        "label": "Head gear",
        "slot": "head", "worn": True,
        "subject": "a piece of headgear — helm, hood, circlet, or mask",
        "framing": "shown in 3/4 front view as it would sit on a head, hollow "
                   "underside implied, no head or face inside",
    },
    "body": {
        "label": "Body gear",
        "slot": "body", "worn": True,
        "subject": "a torso armor piece — cuirass, robe, tunic, or harness",
        "framing": "shown front-on as a flat-lay garment, symmetric, no body "
                   "or mannequin inside",
    },
    "feet": {
        "label": "Foot gear",
        "slot": "feet", "worn": True,
        "subject": "a pair of foot armor — boots, greaves, or sandals",
        "framing": "shown as a matched pair at a slight 3/4 angle, side by side",
    },
    "consumable": {
        "label": "Consumable",
        "slot": "inventory", "worn": False,
        "subject": "a usable consumable — potion, elixir, food, or scroll",
        "framing": "shown upright and centered, single object, label/cork/seal "
                   "legible",
    },
    "throwable": {
        "label": "Throwable",
        "slot": "projectile", "worn": False,
        "subject": "a throwable weapon — bomb, throwing knife, axe, or flask",
        "framing": "shown at a diagonal mid-tumble reading, compact silhouette "
                   "that stays legible when small and spinning",
    },
    "ranged": {
        "label": "Ranged gear",
        "slot": "main_hand", "worn": True,
        "subject": "a ranged weapon — bow, crossbow, sling, or wand",
        "framing": "shown side-on in profile, string/limb detail readable, "
                   "held region toward the lower-middle",
    },
}

SLOTS = tuple(dict.fromkeys(c["slot"] for c in ITEM_CLASSES.values()))

# The INVARIANT clause every item in every class shares. This is the consistency
# rail: hold framing, light, scale, and background constant so a class of
# variants reads as one crafted set instead of a bag of unrelated pictures.
STYLE = (
    "single video-game item icon, one object only, centered, orthographic, "
    "clean readable silhouette, soft key light from the upper-left with a subtle "
    "rim light, painterly but crisp, consistent scale filling most of the frame "
    "with a small margin, fully transparent background, no ground shadow, no "
    "text, no numbers, no border, no frame, no watermark, no drop reflection"
)

# Rarity drives the strongest read in gear art — ornamentation and energy, not
# just a color swap — so tier gets a real prompt fragment, not a bare adjective.
TIERS: dict[str, str] = {
    "common": "plain and functional, minimal ornamentation, muted worn materials",
    "uncommon": "lightly adorned, clean condition, a single accent color",
    "rare": "finely crafted with engraved detail and a faint magical sheen",
    "epic": "ornate, inlaid with gems and glowing runes, radiating colored energy",
    "legendary": "masterwork artifact, intricate filigree, strong elemental aura "
                 "and particulate glow, unmistakably powerful",
}

# Element woven into the material read (a fire sword glows at the edge; an ice
# one frosts). Kept open — unknown values pass through as free descriptors.
ELEMENTS: dict[str, str] = {
    "fire": "wreathed in embers and heat-glow along its edges",
    "ice": "rimed with frost and pale blue crystal",
    "poison": "slick with dripping green venom and sickly haze",
    "lightning": "arcing with crackling electric filaments",
    "holy": "haloed in warm golden light",
    "shadow": "leaking dark violet smoke and void",
}

# The rollup the equip UI reads in ONE shot instead of globbing loose
# per-item manifests. Shape: {"items": {name: manifest}}.
INDEX_REL = ".bgate_out/items/_index.json"

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Filesystem- and Godot-safe lowercase slug."""
    return _SLUG.sub("_", text.strip().lower()).strip("_") or "item"


def _class(item_class: str) -> dict:
    try:
        return ITEM_CLASSES[item_class]
    except KeyError:
        raise ValueError(
            f"unknown item class {item_class!r}; classes are "
            f"{', '.join(ITEM_CLASSES)}") from None


def build_prompt(item_class: str, descriptor: str, *, material: str = "",
                 element: str = "", tier: str = "", extra: str = "",
                 style_clause: str = "") -> str:
    """Assemble ONE item prompt: invariant style + class framing + variant params.

    descriptor is the item's identity ("curved saber", "round wooden shield").
    material / element / tier are the variant axes; unknown element/tier values
    are folded in verbatim so the vocab never blocks a valid idea.

    style_clause is the cross-leg rail: a character/project rendering style
    (a visual profile's `style`) appended AFTER the invariant, so gear worn on
    a fighter reads as the same set as the body. Empty means no rail — prompts
    are byte-identical to before the param existed.
    """
    cls = _class(item_class)
    desc = descriptor.strip()
    if not desc:
        raise ValueError("descriptor is required — name the item, e.g. 'curved saber'")

    parts: list[str] = [f"{cls['subject']}: {desc}"]
    if material.strip():
        parts.append(f"made of {material.strip()}")
    if element.strip():
        parts.append(ELEMENTS.get(element.strip().lower(), element.strip()))
    if tier.strip():
        parts.append(TIERS.get(tier.strip().lower(), tier.strip()))
    if extra.strip():
        parts.append(extra.strip())
    parts.append(cls["framing"])
    parts.append(STYLE)
    if style_clause.strip():
        parts.append(style_clause.strip())
    return ", ".join(parts)


def variant_name(base_name: str, *, material: str = "", element: str = "",
                 tier: str = "") -> str:
    """Deterministic slug for a variant: base + the axes that vary it."""
    bits = [base_name] + [b for b in (tier, element, material) if b.strip()]
    return slugify("_".join(bits))


def plan_variants(item_class: str, base_name: str, descriptor: str, *,
                  materials: Optional[list[str]] = None,
                  elements: Optional[list[str]] = None,
                  tiers: Optional[list[str]] = None,
                  extra: str = "", style_clause: str = "") -> list[dict]:
    """Expand a parameter grid into a flat list of variant specs.

    The cartesian product of the axes you pass. An empty axis contributes a
    single "" value (that axis simply isn't varied), so plan_variants with no
    axes yields exactly one item. Each spec is everything the generator needs and
    nothing it has to recompute: {name, item_class, slot, worn, descriptor,
    prompt, params}.
    """
    _class(item_class)  # validate early
    mats = [m for m in (materials or []) if m.strip()] or [""]
    els = [e for e in (elements or []) if e.strip()] or [""]
    trs = [t for t in (tiers or []) if t.strip()] or [""]

    cls = ITEM_CLASSES[item_class]
    specs: list[dict] = []
    seen: set[str] = set()
    for tier in trs:
        for element in els:
            for material in mats:
                name = variant_name(base_name, material=material,
                                     element=element, tier=tier)
                if name in seen:  # identical slug from overlapping axes — skip
                    continue
                seen.add(name)
                specs.append({
                    "name": name,
                    "item_class": item_class,
                    "slot": cls["slot"],
                    "worn": cls["worn"],
                    "descriptor": descriptor,
                    "params": {"material": material, "element": element,
                               "tier": tier},
                    "prompt": build_prompt(item_class, descriptor,
                                           material=material, element=element,
                                           tier=tier, extra=extra,
                                           style_clause=style_clause),
                })
    return specs


def rel_art_path(item_class: str, name: str) -> str:
    """Where a class's variants land under .bgate_out/art — one dir per class so
    a set stays together for review. Returned repo-relative, forward slashes."""
    _class(item_class)
    return f".bgate_out/art/items/{item_class}/{slugify(name)}.png"


def rel_manifest_path(name: str) -> str:
    """The JSON record the equip system and gameplay read back."""
    return f".bgate_out/items/{slugify(name)}.json"


def estimate_cost(count: int, quality: str = "medium") -> float:
    """Estimated $ spend for `count` images at `quality` — what the tools show
    a human BEFORE a batch buys anything. Rounded to cents for display."""
    from bgate_adapters.imagegen import price_per_image
    return round(max(0, count) * price_per_image(quality), 2)


def split_existing(specs: list[dict], exists) -> tuple[list[dict], list[dict]]:
    """Partition a plan into (to_mint, skipped) so a re-run never re-buys.

    `exists` is a predicate over a repo-relative manifest path — the caller
    supplies disk truth, keeping this module pure. The manifest is written
    LAST in the mint sequence, so its existence implies the art landed too.
    """
    to_mint: list[dict] = []
    skipped: list[dict] = []
    for spec in specs:
        target = skipped if exists(rel_manifest_path(spec["name"])) else to_mint
        target.append(spec)
    return to_mint, skipped


def update_index(index: dict, man: dict) -> dict:
    """Upsert one item manifest into the rollup index (pure — caller does the
    I/O). Keyed by name, so re-minting a variant replaces its entry instead of
    appending a duplicate."""
    index.setdefault("items", {})[man["name"]] = man
    return index


def manifest(spec: dict, sprite_rel: str) -> dict:
    """The bridge record: enough for gameplay to slot the item and the equip
    system to find its art, decoupled from the prompt that made it."""
    return {
        "name": spec["name"],
        "item_class": spec["item_class"],
        "slot": spec["slot"],
        "worn": spec["worn"],
        "descriptor": spec["descriptor"],
        "params": spec.get("params", {}),
        "sprite": sprite_rel,
    }
