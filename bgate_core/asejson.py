"""Aseprite export JSON -> Godot 4 SpriteFrames, built from facts.

Every other SpriteFrames emitter in this project either KNOWS the layout
because it built it (bgate_adapters/sprites.py) or GUESSES it from a grid
(bgate_core/rigmap.py, and sprite_sheet_check exists because that guess needs
checking). This one does neither: Aseprite's ``--data`` JSON states the exact
rect, duration and tag of every frame, so the .tres is a translation, not an
inference. This is what closes the hand-edit loop — an artist fixes frames in
Aseprite, saves, and the re-exported resource is exact by construction.

Timing: Aseprite stores per-frame milliseconds; Godot stores an animation
``speed`` (fps) plus a relative ``duration`` hold per frame. The conversion
divides every duration in an animation by their GCD — so [80, 80, 160] becomes
speed 12.5 with holds [1, 1, 2], the smallest integer holds that reproduce the
authored timing exactly.

Byte-stable on purpose, like rigmap.spriteframes_text: the same JSON must
produce the same .tres so a re-export without edits is a no-op in git.
"""
from __future__ import annotations

import math


class AseJsonError(ValueError):
    """An export JSON this converter refuses, in words worth showing."""


def spriteframes_text(data: dict, sheet_filename: str, res_dir: str) -> str:
    """The .tres text for one exported master.

    ``data`` is the parsed ``--data`` JSON in ``json-array`` format (frames as
    a list; the hash format keys frames by filename and loses their order,
    which is exactly the fact this converter exists to keep).

    Tags become animations. A frame no tag claims is dropped — it exists in
    the master (a scratch frame, a reference) but was never part of any
    animation, and inventing an "untagged" animation for it would ship it.
    A master with no tags at all becomes one looping "default" animation,
    because a single-animation sprite with no tag is the normal way item
    icons and one-shot VFX come through.
    """
    frames = data.get("frames")
    if not isinstance(frames, list) or not frames:
        raise AseJsonError(
            "no frames list — export with --format json-array (the hash "
            "format loses frame order)")
    tags = list((data.get("meta") or {}).get("frameTags") or [])
    if not tags:
        tags = [{"name": "default", "from": 0, "to": len(frames) - 1}]

    # Which frames any animation actually references, in first-use order —
    # one AtlasTexture per referenced frame, untagged frames emitted nowhere.
    used: list[int] = []
    plans: list[dict] = []
    for tag in tags:
        name = str(tag.get("name") or "").strip()
        lo, hi = int(tag.get("from", 0)), int(tag.get("to", -1))
        if not name:
            raise AseJsonError(f"a tag over frames {lo}..{hi} has no name")
        if not (0 <= lo <= hi < len(frames)):
            raise AseJsonError(
                f"tag {name!r} spans frames {lo}..{hi} but the export has "
                f"{len(frames)} frames — the JSON and the master disagree")
        order = list(range(lo, hi + 1))
        if str(tag.get("direction") or "") == "pingpong" and len(order) > 2:
            # Baked into the frame list, same as sprites.py does for animspec
            # ping-pong: Godot has no ping-pong loop mode.
            order = order + order[-2:0:-1]
        for i in order:
            if i not in used:
                used.append(i)
        durations = [max(1, int(frames[i].get("duration") or 100)) for i in order]
        base = math.gcd(*durations) if len(durations) > 1 else durations[0]
        # ``repeat`` is absent on a looping tag and "1" on a play-once one.
        plans.append({
            "name": name,
            "order": order,
            "holds": [d // base for d in durations],
            "fps": round(1000.0 / base, 4),
            "loop": str(tag.get("repeat") or "") != "1",
        })

    res_dir = res_dir.strip("/").replace("\\", "/")
    atlas_of = {frame: i for i, frame in enumerate(used)}
    lines = [
        f'[gd_resource type="SpriteFrames" load_steps={len(used) + 2} format=3]',
        "",
        f'[ext_resource type="Texture2D" path="res://{res_dir}/{sheet_filename}" id="1"]',
        "",
    ]
    for frame in used:
        rect = frames[frame].get("frame") or {}
        try:
            x, y = int(rect["x"]), int(rect["y"])
            w, h = int(rect["w"]), int(rect["h"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AseJsonError(f"frame {frame} has no readable rect") from exc
        lines += [
            f'[sub_resource type="AtlasTexture" id="atlas_{atlas_of[frame]}"]',
            'atlas = ExtResource("1")',
            f"region = Rect2({x}, {y}, {w}, {h})",
            "",
        ]
    blocks = []
    for plan in plans:
        entries = ", ".join(
            '{\n"duration": %s,\n"texture": SubResource("atlas_%d")\n}'
            % (_hold(hold), atlas_of[frame])
            for frame, hold in zip(plan["order"], plan["holds"]))
        blocks.append(
            '{\n"frames": [%s],\n"loop": %s,\n"name": &"%s",\n"speed": %s\n}'
            % (entries, "true" if plan["loop"] else "false",
               plan["name"], _fps(plan["fps"])))
    lines += ["[resource]", "animations = [" + ", ".join(blocks) + "]", ""]
    return "\n".join(lines)


def slice_labels(data: dict) -> tuple[list[dict], list[str]]:
    """Aseprite slices -> rig-sidecar label rows. (labels, skipped_names).

    A slice is how a human marks an anchor IN Aseprite — drag a named rect
    over the hand, key it per frame — and it rides the same export JSON the
    .tres already comes from. Slice-key bounds are CANVAS coordinates (the
    cw×ch sprite), which is exactly the cell-local frame the rig sidecar
    stores, so no grid math happens here at all.

    The slice NAME is the slot, and it must be one of rigmap.KNOWN_SLOTS —
    an unknown name is reported back, never invented into the taxonomy: a
    typo'd "main_hnd" that silently became a new slot would be invisible to
    every reader filtering on the real one.

    The anchor point is the bounds centre; a slice with an explicit pivot
    (set in Aseprite's slice properties) uses that instead, since a pivot is
    the author saying "this exact pixel", offset from the bounds origin.
    """
    from bgate_core import rigmap

    labels: list[dict] = []
    skipped: list[str] = []
    for entry in (data.get("meta") or {}).get("slices") or []:
        raw = str(entry.get("name") or "").strip()
        slot = rigmap.slot_name(raw)
        if slot not in rigmap.KNOWN_SLOTS:
            skipped.append(raw or "(unnamed)")
            continue
        for key in entry.get("keys") or []:
            bounds = key.get("bounds") or {}
            try:
                frame = int(key.get("frame", 0))
                x0, y0 = float(bounds["x"]), float(bounds["y"])
                w, h = float(bounds["w"]), float(bounds["h"])
            except (KeyError, TypeError, ValueError):
                skipped.append(f"{raw}@{key.get('frame')}")
                continue
            pivot = key.get("pivot")
            if isinstance(pivot, dict) and "x" in pivot and "y" in pivot:
                x, y = x0 + float(pivot["x"]), y0 + float(pivot["y"])
            else:
                x, y = x0 + w / 2.0, y0 + h / 2.0
            labels.append({"slot": slot, "frame": frame,
                           "x": round(x, 1), "y": round(y, 1),
                           "source": "slice"})
    return labels, skipped


def _hold(value: int) -> str:
    return f"{max(1, int(value)):.1f}"


def _fps(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if "." in text else f"{text}.0"
