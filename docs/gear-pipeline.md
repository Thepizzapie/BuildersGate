# Gear pipelines — how to make diverse combat gear

Two kinds of gear art have **opposite economics**. Keep them separate.

| | Item-as-object | Gear-as-worn |
|---|---|---|
| what | inventory icons, consumables, throwables, ranged, projectiles | armor/weapons drawn **on the animated fighter** |
| cost | one transparent image, no animation | must move correctly through every frame |
| pipeline | **1 — item-art** (this doc) | equip/layer system + a rig pipeline (later) |

The governing rule: **variants are cheap, classes are expensive.** A pipeline
mints skins of a class (flame saber vs ice saber) for near-free. What costs
animation labor is the number of weapon *classes* (unarmed, blade, bow…), because
each swings differently. Push diversity into variant-space; keep the class list
deliberate.

## Pipeline 1 — item-art (Codex drives this today)

Pure taxonomy + builders: `bgate_core/items.py`. I/O via MCP tools. The class
carries an **invariant style clause** (framing, light, scale, transparent bg) so a
whole class reads as one set; a variant only swaps material / element / tier.

**Contract (call in this order):**

1. `item_classes()` → the taxonomy: classes, their equip `slot`, the variant axes.
2. `item_generate(item_class, name, descriptor, material?, element?, tier?)` → one
   item. Transparent PNG under `.bgate_out/art/items/<class>/`, archived, tracked,
   with a manifest at `.bgate_out/items/<name>.json`.
3. `item_variants(item_class, base_name, descriptor, materials?, elements?, tiers?, limit=12)`
   → the batch engine. Cartesian product of the axes; each combo is one on-set
   icon. `limit` caps spend — a grid over the cap is **reported and refused** so
   you confirm before paying.

Classes: `main_hand · off_hand · head · body · feet` (worn) and
`consumable · throwable · ranged` (objects). Each maps to an equip `slot` so the
manifest lines up with pipeline 2.

Every image costs real money (~$0.02–0.19). Generate, then **look** before importing.

## Pipeline 2 — the 2D equip/layer system (built)

`templates/2d/scenes/fighter.tscn` + `templates/2d/scripts/gear_rig.gd`.

A fighter is a **body sprite (`Base`) plus one `AnimatedSprite2D` layer per slot**
(`Feet · BodyGear · Head · OffHand · MainHand`). The body is the single animation
clock; `gear_rig.gd` mirrors its `animation` and `frame` onto every equipped layer
each tick. So the body's animations are authored **once** and any gear that exists
as a layer keyed to the same animation names rides them for free — this is the
2D escape from the combinatorial trap.

**API:**

```gdscript
rig.set_base(body_frames, &"idle")     # the clock
rig.play(&"punch")                     # layers follow automatically
rig.equip("main_hand", sword_frames)   # rides the current animation immediately
rig.equip("head", helm_frames, Vector2(0,-4))  # optional per-slot offset
rig.unequip("off_hand")
rig.set_facing(false)                  # flips the whole rig; gear mirrors too
```

Two art conventions both work through the same slots:

- **aligned sheet** — a full-character-canvas `SpriteFrames` with only the gear
  drawn (the sprite pipeline's native output). Offset 0; overlays the body 1:1.
- **anchored icon** — a small item icon at a slot anchor so a static weapon sits
  in the hand. Use `item_to_spriteframes(sprite, name)` to wrap a pipeline-1 PNG
  into a 1-frame `SpriteFrames`, then `equip(slot, frames, offset)`. This is the
  honest v1 for worn weapons before the per-frame rig pipeline exists.

A layer that lacks the body's current animation hides its visual for that anim and
reappears when a defined animation plays — it is never frozen on a stale frame.

## Not built yet (pipeline 3)

Per-frame worn gear that deforms with the body across a swing needs either a
Blender attach-point rig or paperdoll layers drawn per frame. Out of scope until
the equip system above is carrying real combat.
