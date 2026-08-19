"""Playable GIF loops for animation review — motion made visible.

Every review surface in this project judges animation on STILLS: the sheet,
the strip, the per-frame consistency composite. A `pop` or an `open_loop` is
obvious in two seconds of playback and invisible in a grid — which is how a
sheet whose every join hitches can arrive at a human as "no outliers, all
pass". This module renders what the .tres will actually play, one GIF per
animation, with the authored per-frame timing.

Pure Pillow on purpose: previews must exist on a machine without Aseprite,
and the frames are already on disk in exactly play order. GIF's binary
transparency is enough here because every conformed frame carries hard alpha.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

#: GIF timing is in centiseconds; anything under 2cs is clamped UP by most
#: renderers (browsers clamp 0/1cs to 10cs) — better to do it ourselves and
#: keep the timing we asked for.
MIN_FRAME_MS = 20


def durations_ms(count: int, spec: Optional[dict], fps: float) -> list[int]:
    """Per-frame milliseconds from an animspec timing entry, same math as the
    .tres and the .aseprite master use: hold * 1000 / fps."""
    spec = spec or {}
    anim_fps = float(spec.get("fps") or fps) or 8.0
    holds = list(spec.get("holds") or [])
    holds += [1.0] * (count - len(holds))
    return [max(MIN_FRAME_MS, round(float(h) * 1000.0 / anim_fps))
            for h in holds[:count]]


def write_gif(frame_paths: Sequence[str], out_path: str, *,
              durations: Sequence[int], loop: bool = True,
              scale: int = 1) -> dict:
    """One animation -> one GIF. Never raises; a preview is a bonus.

    ``durations`` in milliseconds, one per frame. ``loop`` False plays once
    and holds (a death that loops on a review page reads as a defect that is
    not there). ``scale`` nearest-upscales small cells so a 64px sprite is
    reviewable without squinting.
    """
    from PIL import Image

    try:
        frames = []
        for p in frame_paths:
            img = Image.open(p).convert("RGBA")
            if scale > 1:
                img = img.resize((img.width * scale, img.height * scale),
                                 Image.NEAREST)
            frames.append(img)
        if not frames:
            return {"ok": False, "error": "no frames"}
        # RGBA -> P with a reserved transparency index. Pillow's RGBA GIF
        # path handles this, but doing it explicitly keeps every frame on
        # ONE palette — per-frame palettes are how a conformed animation
        # comes back flickering in the preview of all places.
        converted = []
        for img in frames:
            alpha = img.getchannel("A")
            p = img.convert("RGB").quantize(255, dither=Image.Dither.NONE)
            p.info["transparency"] = 255
            mask = alpha.point(lambda a: 255 if a <= 8 else 0)
            p.paste(255, (0, 0), mask)
            converted.append(p)
        durs = [int(d) for d in durations[:len(converted)]]
        durs += [durs[-1] if durs else 100] * (len(converted) - len(durs))
        converted[0].save(
            out_path, save_all=True, append_images=converted[1:],
            duration=durs, loop=0 if loop else 1, transparency=255,
            disposal=2, optimize=False)
        return {"ok": True, "path": str(out_path), "frames": len(converted),
                "duration_ms": sum(durs)}
    except Exception as exc:                                    # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def write_gifs(frames_by_anim: dict[str, list[str]], out_dir: str, name: str, *,
               timing: Optional[dict] = None, fps: float = 8.0,
               no_loop: Sequence[str] = (), scale: int = 1) -> dict[str, str]:
    """One GIF per animation beside the sheet: <name>_<anim>.gif.

    ``frames_by_anim`` maps each animation to its frame PNGs in play order.
    Animations that fail to render are simply absent from the result — the
    sheet shipped, the preview is decoration.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for anim, paths in frames_by_anim.items():
        if not paths:
            continue
        spec = (timing or {}).get(anim) or {}
        loop = spec.get("loop")
        if loop is None:
            loop = anim not in no_loop
        dest = out / f"{name}_{anim}.gif"
        got = write_gif(paths, str(dest),
                        durations=durations_ms(len(paths), spec, fps),
                        loop=bool(loop), scale=scale)
        if got.get("ok"):
            written[anim] = str(dest)
    return written
