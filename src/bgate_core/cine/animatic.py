"""The animatic: the edit, timed, before a single frame of it is bought.

THE STAGE THAT WAS MISSING, AND WHAT IT COST. ``cinematic.plan()`` writes a shot
list and ``cinematic.generate_shot()`` buys a clip, and between those two calls
there was NOTHING. So the first time any human saw the EDIT — the order, the
rhythm, whether shot 4 is the same picture as shot 3, whether the scene runs
ninety seconds when the brief said thirty — was after every second of it had been
paid for. At that point the only cheap change left is to delete shots, which is
how a cutscene ends up being whatever the budget could afford rather than
whatever was planned.

WHY PREVIS IS THE ANSWER, IN THE INDUSTRY'S OWN ARITHMETIC. Hand animation runs
at roughly one finished second per animator-hour, which is why nobody in film or
games animates an unproven edit: you cut the storyboard panels together against a
scratch track first, and you fix the scene THERE, where a change is a drag of a
handle. Generated video costs MORE per second than that hand animation did, and
had no such stage at all. This module is that stage.

IT CALLS NO MODEL AND SPENDS NOTHING. That is not a footnote, it is the whole
design constraint: every panel here is a PNG that already exists (or a slate card
drawn locally), every join is ffmpeg, and the only resource consumed is a few
seconds of CPU. An animatic you hesitate over the price of is an animatic nobody
builds, and then the edit goes unproven again.

WHERE IT READS FROM, AND WHY THE SEQUENCE IS THE PRIMARY DOOR. Two things in this
codebase hold panels and timing:

  * a ``story_board`` — frames with images, beats, dialogue and durations. Free,
    and the place the scene is actually argued about.
  * a ``cine_sequence`` — shots with ``duration``, ``transition``,
    ``transition_s`` and (when promoted from a board) a ``first_frame`` still.

The SEQUENCE is the default source, for two reasons that are not preference. It
is the row that is one call away from spending, so an animatic built from it is a
preview of exactly what will be bought — a board can be edited after the
promotion and then the reel is describing a scene nobody is buying. And it is the
only one of the two that carries TRANSITIONS: the story_frame table has no such
column, so an animatic built off a board would have to invent the joins, and the
invented timing would disagree with the shot list's own arithmetic the moment
anyone looked at both. cinecut's :func:`~bgate_core.cine.cinecut.runtime_of`,
:func:`~bgate_core.cine.cinecut.captions` and :func:`~bgate_core.cine.cinecut.xfade_graph`
already read that shape, so the animatic and the finished cut are timed by the
same code and cannot drift apart.

A board is still accepted, because the honest case exists: an un-promoted board
is where somebody is deciding the scene, and refusing to show them their own
timing until they have crossed the paid boundary would put the previs stage on
the wrong side of the line it exists to guard. Board panels are normalised into
the same shot shape with every join a hard cut, and the report says so.

A MISSING FRAME IS RENDERED, NOT SKIPPED. A beat with no image gets a slate card
naming it and holds its full duration. Dropping it would produce a reel that is
shorter than the scene and reads as complete, which is the one outcome that makes
previs actively harmful — a gap in the edit is INFORMATION, and it is exactly the
information the person watching needs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from . import cinecut
from ..store.util import slugify
from ..runtime import ffmpegbin as _ffmpegbin

# Windows: never flash a console window out of an ffmpeg call. Same reasoning as
# cinecut._NO_WINDOW, and this module spawns one process per panel.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Where a reel lands. Under design/, beside the storyboards it is built from,
# because an animatic is design work about a cutscene and not a shipped asset —
# nothing here is ever installed into the engine project.
ANIMATIC_DIRNAME = "design/cinematics/animatics"

# Scratch: panels and per-panel segments. .bgate is gitignored by both init and
# adopt, so the intermediate PNGs never reach a repository.
WORK_DIRNAME = ".bgate/animatic"

# THE OUTPUT IS H.264 IN AN MP4, DELIBERATELY, AND IT IS THE ONE PLACE THIS
# PIPELINE DOES NOT WRITE OGG THEORA.
#   1. The finished cut is .ogv because Godot's VideoStreamPlayer plays Theora
#      and nothing else without an add-on. An animatic is never played by the
#      engine — it is watched by a person, in the dashboard or a media player —
#      so that constraint simply does not apply here.
#   2. The Windows ffmpeg most users have (Gyan.FFmpeg) ships a libtheora that
#      produces CORRUPT Ogg: the encoder is present, exits 0, and the file
#      decodes a handful of frames before falling over. Writing the previs
#      artifact in that format would mean the tool whose entire job is to show
#      you the edit before you pay is the tool most likely to hand back
#      something you cannot watch.
#   3. H.264/yuv420p in MP4 plays in every browser, which is where the dashboard
#      shows it, and libx264 is in every build worth having.
# mpeg4 is the fallback for a build without libx264 — worse looking, universally
# present, and a slightly soft previs beats no previs.
CONTAINER = "mp4"
PREFERRED_ENCODER = "libx264"
FALLBACK_ENCODER = "mpeg4"

# A still held for seconds does not need a high frame rate — every frame in a
# panel is identical. 12 is enough for xfade to have something to dissolve
# through and keeps the encode near-instant.
FPS = 12

# A shot with no duration set. Matches storyboard's own default so a board and
# its promoted sequence cannot disagree about the length of the same beat.
DEFAULT_DURATION_S = 5.0

# Panel geometry per aspect ratio. Every dimension is EVEN, which yuv420p
# requires — an odd height makes libx264 refuse with a message about chroma
# subsampling that says nothing about the actual mistake.
PANEL_SIZES = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (960, 960),
               "4:3": (960, 720), "21:9": (1260, 540)}

# The average-shot-length window worth flagging against. Modern feature films
# sit around 4-6 seconds and it is the single most useful number for judging
# whether an edit reads: much under and the scene is a montage the viewer cannot
# follow, much over and it is a slideshow. Advisory, never a gate — a deliberate
# slow scene is a real choice and this is not the place to argue with it.
ASL_FAST_S = 4.0
ASL_SLOW_S = 6.0


class AnimaticError(RuntimeError):
    """A refusal the caller should read and act on, not a crash."""


# ---------------------------------------------------------------------------
# 1. Where the panels come from
# ---------------------------------------------------------------------------

def resolve(root: str | os.PathLike[str], name: str,
            source: str = "auto") -> dict:
    """The shot list to cut, from a sequence or a board, in one shape.

    ``source`` is "auto" (sequence first, then board), "sequence" or "board".
    Auto prefers the sequence because that is the row money is spent against —
    see the module docstring. When both exist and they have diverged, the
    sequence is what the next generation will read, so it is what you want to be
    looking at.
    """
    want = str(source or "auto").strip().lower()
    if want not in ("auto", "sequence", "board"):
        raise AnimaticError(
            f"source={want!r} is not one of 'auto', 'sequence' or 'board'")

    tried = []
    if want in ("auto", "sequence"):
        try:
            return _from_sequence(root, name)
        except Exception as exc:                                  # noqa: BLE001
            if want == "sequence":
                raise AnimaticError(str(exc)) from exc
            tried.append(f"sequence: {exc}")
    if want in ("auto", "board"):
        try:
            return _from_board(root, name)
        except Exception as exc:                                  # noqa: BLE001
            if want == "board":
                raise AnimaticError(str(exc)) from exc
            tried.append(f"board: {exc}")
    raise AnimaticError(
        f"nothing named {name!r} to cut — " + "; ".join(tried)
        + ". An animatic is built from a planned sequence or a storyboard; "
          "cinematic_plan or storyboard_plan writes one.")


def _from_sequence(root: str | os.PathLike[str], name: str) -> dict:
    from . import cinematic

    seq = cinematic.sequence(root, name)
    shots = []
    for shot in seq.get("shots", []):
        if str(shot.get("status") or "") == "cut":
            continue
        shots.append(_normalise(root, shot, still=shot.get("first_frame") or "",
                                label=shot.get("action") or ""))
    return {"source": "sequence", "name": seq.get("name") or slugify(name),
            "logline": seq.get("logline") or "",
            "aspect_ratio": seq.get("aspect_ratio") or "16:9",
            "shots": shots}


def _from_board(root: str | os.PathLike[str], name: str) -> dict:
    from . import storyboard

    b = storyboard.board(root, name)
    shots = []
    for frame in b.get("frames", []):
        if str(frame.get("status") or "") == "cut":
            continue
        # A board has no transition column, so every join here is a hard cut and
        # the report says so. Guessing dissolves would put timing in the reel
        # that the shot list does not agree with.
        # The stored path is passed through even when the board already knows
        # the file is gone. _normalise re-checks disk, and keeping the path lets
        # the slate say WHICH image is missing — "design/.../03-lights.png is
        # not there" is actionable in a way that "no image" is not, and a board
        # copied between projects produces a whole page of exactly that.
        shots.append(_normalise(
            root, frame, still=frame.get("image_path") or "",
            label=frame.get("beat") or frame.get("action") or ""))
    return {"source": "board", "name": b.get("name") or slugify(name),
            "logline": b.get("logline") or "",
            "aspect_ratio": b.get("aspect_ratio") or "16:9",
            "shots": shots}


def _normalise(root: str | os.PathLike[str], row: dict, *, still: str,
               label: str) -> dict:
    """One row from either table into the shape cinecut's timing code reads."""
    raw = row.get("duration")
    duration, defaulted = _duration(raw)
    kind = str(row.get("transition") or cinecut.DEFAULT_TRANSITION).strip()
    unknown = kind not in cinecut.TRANSITIONS
    if unknown:
        # NORMALISED, NOT REFUSED. An animatic that will not render because one
        # row holds a transition name nobody recognises is an animatic that does
        # not get built, and the whole point is that this stage is never worth
        # skipping. The substitution is reported.
        kind = cinecut.DEFAULT_TRANSITION

    path = ""
    rel = str(still or "").strip()
    if rel:
        candidate = Path(root) / rel
        if candidate.is_file():
            path = str(candidate)
    return {
        "idx": int(row.get("idx") or 0),
        "slug": str(row.get("slug") or ""),
        "label": str(label or "").strip(),
        "camera": str(row.get("camera") or "").strip(),
        "dialogue": str(row.get("dialogue") or "").strip(),
        "duration": duration,
        "duration_defaulted": defaulted,
        "transition": kind,
        "transition_s": float(row.get("transition_s") or 0.5),
        "still": path,
        "still_rel": rel,
        "unknown_transition": str(row.get("transition") or "") if unknown else "",
    }


def _duration(value: Any) -> tuple[float, bool]:
    """Seconds, and whether the default had to be used.

    A shot with no duration is not an error and must not be dropped: it is a
    beat somebody wrote and forgot to time, and the reel is where they find out.
    It gets the same default the storyboard uses and is FLAGGED, because an
    untimed shot silently taking five seconds is how a scene budgeted at thirty
    seconds turns out to be fifty.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DURATION_S, True
    if seconds <= 0:
        return DEFAULT_DURATION_S, True
    # Clamped low only. A previs of a 40s shot is a legitimate thing to want to
    # look at, even though no video model will generate one.
    return max(round(seconds, 3), 0.2), False


# ---------------------------------------------------------------------------
# 2. Panels — including the ones that do not exist yet
# ---------------------------------------------------------------------------

def _font(size: int):
    from PIL import ImageFont

    for candidate in ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf",
                      "C:/Windows/Fonts/arial.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:                                          # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:                                              # noqa: BLE001
        return ImageFont.load_default()


def panel(shot: dict, size: tuple, out_path: Path, *,
          burn_captions: bool = True) -> dict:
    """Draw one panel: the still if there is one, a slate card if there is not.

    THE CAPTION IS BURNED INTO THE PIXELS, and that is a different decision from
    the one the finished cut makes. cinecut ships captions as a sidecar .srt
    plus a .json the Godot player reads, because captions in a shipped cutscene
    have to be translatable and a translator cannot edit pixels. An animatic is
    never shipped and never translated; it is watched once, often by somebody who
    will not have a player that loads sidecars, and a silent timed reel with no
    text is close to unreadable. So the reel carries the words, and
    :func:`build` ALSO writes the sidecar off the same arithmetic — which is
    what makes a timing disagreement between the previs and the finished cut
    visible at the point it is still free to fix.

    FITTED, NEVER CROPPED. A storyboard frame's framing IS the information being
    reviewed; filling the panel by cropping would change the shot size of every
    panel whose aspect does not match, and shot size is one of the things the
    reel exists to let somebody judge.
    """
    from PIL import Image, ImageDraw

    width, height = size
    canvas = Image.new("RGB", (width, height), (12, 12, 14))
    draw = ImageDraw.Draw(canvas)
    placeholder = not shot.get("still")

    if not placeholder:
        try:
            with Image.open(shot["still"]) as im:
                art = im.convert("RGB")
                art.thumbnail((width, height), Image.LANCZOS)
                canvas.paste(art, ((width - art.width) // 2,
                                   (height - art.height) // 2))
        except Exception as exc:                                   # noqa: BLE001
            # A path that is on disk but is not an image is the same problem as
            # no image at all, and saying which it was matters — "the file is
            # there and unreadable" and "nobody drew this yet" have different
            # fixes.
            placeholder = True
            shot = {**shot, "slate_reason": f"unreadable image: {exc}"}

    if placeholder:
        _slate(draw, shot, width, height)

    _burn_slug(draw, shot, width, height)
    if burn_captions and shot.get("dialogue"):
        _burn_caption(draw, shot["dialogue"], width, height)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return {"path": str(out_path), "placeholder": placeholder}


def _slate(draw, shot: dict, width: int, height: int) -> None:
    """The card that stands in for a beat nobody has drawn.

    Loud on purpose. A tasteful grey card reads as an artistic choice at a
    glance, and the entire value of rendering the gap instead of skipping it is
    that a viewer counts the holes in the scene without being told to.
    """
    draw.rectangle([0, 0, width - 1, height - 1], fill=(28, 20, 22),
                   outline=(150, 60, 60), width=6)
    title = _font(max(int(height * 0.055), 14))
    body = _font(max(int(height * 0.038), 12))
    small = _font(max(int(height * 0.030), 11))

    head = "NO FRAME YET"
    draw.text((width // 2, int(height * 0.24)), head, font=title,
              fill=(220, 120, 120), anchor="mm")

    label = shot.get("label") or f"shot {shot.get('idx') or '?'}"
    wrapped = textwrap.wrap(label, width=max(int(width / (height * 0.024)), 24))
    y = int(height * 0.40)
    for line in wrapped[:4]:
        draw.text((width // 2, y), line, font=body, fill=(228, 224, 220),
                  anchor="mm")
        y += int(height * 0.055)

    reason = shot.get("slate_reason") or (
        f"missing: {shot['still_rel']}" if shot.get("still_rel") else
        "this beat has no still — it is held at full length so the gap in the "
        "edit is visible rather than silently shortening the scene")
    for line in textwrap.wrap(reason, width=max(int(width / (height * 0.019)), 30))[:3]:
        draw.text((width // 2, y), line, font=small, fill=(150, 140, 138),
                  anchor="mm")
        y += int(height * 0.042)


def _burn_slug(draw, shot: dict, width: int, height: int) -> None:
    """Index, camera and duration in the corner. Standard previs practice — the
    number on screen is how a note like "shot 7 is dead" gets written down."""
    font = _font(max(int(height * 0.030), 11))
    bits = [f"{int(shot.get('idx') or 0):02d}"]
    if shot.get("camera"):
        bits.append(str(shot["camera"])[:40])
    bits.append(f"{float(shot.get('duration') or 0):.1f}s")
    if shot.get("transition") and shot["transition"] != "cut":
        bits.append(f"→{shot['transition']}")
    text = "   ".join(bits)
    pad = max(int(height * 0.012), 6)
    box = draw.textbbox((0, 0), text, font=font)
    draw.rectangle([0, 0, box[2] + pad * 2, box[3] + pad * 2], fill=(0, 0, 0))
    draw.text((pad, pad), text, font=font, fill=(235, 230, 225))


def _burn_caption(draw, text: str, width: int, height: int) -> None:
    font = _font(max(int(height * 0.040), 13))
    lines = textwrap.wrap(f"\u201c{text}\u201d",
                          width=max(int(width / (height * 0.026)), 28))[:3]
    line_h = int(height * 0.055)
    top = height - line_h * len(lines) - int(height * 0.05)
    draw.rectangle([0, top - int(height * 0.02), width, height], fill=(0, 0, 0))
    y = top
    for line in lines:
        draw.text((width // 2, y + line_h // 2), line, font=font,
                  fill=(245, 242, 238), anchor="mm")
        y += line_h


# ---------------------------------------------------------------------------
# 3. The build
# ---------------------------------------------------------------------------

def build(root: str | os.PathLike[str], name: str, *, source: str = "auto",
          fps: int = FPS, burn_captions: bool = True, out_path: str = "",
          ffmpeg: str = "", keep_panels: bool = False,
          timeout: float = 900.0) -> dict:
    """Cut the panels together at their planned timings. Costs nothing.

    Returns the report the edit is judged on: total runtime measured off the
    FILE, per-shot runtime, how many panels were slates, and the average shot
    length — see :func:`report_notes` for why that last number is the one to
    read first.
    """
    # WHAT THE CALLER GOT WRONG IS CHECKED BEFORE WHAT THE MACHINE IS MISSING.
    #
    # These two guards were the other way round, so a board with every row cut
    # was told to install ffmpeg. Both refusals are true at once, and the order
    # decides which one the human reads: the toolchain is a fact about the
    # machine and takes a minute to fix, while an empty board is a fact about
    # the work and is the thing they actually did. Answering with the wrong one
    # sends somebody off to install software they did not need, and they come
    # back to the same refusal.
    #
    # Found by CI, on a runner with no ffmpeg on it, which is exactly the
    # machine that cannot tell the two apart.
    resolved = resolve(root, name, source=source)
    shots = resolved["shots"]
    if not shots:
        raise AnimaticError(
            f"{resolved['source']} {resolved['name']!r} has no live shots to "
            "cut. Every row on it is marked cut, or it was never populated.")

    exe = _ffmpeg(ffmpeg)

    size = PANEL_SIZES.get(resolved["aspect_ratio"], PANEL_SIZES["16:9"])
    slug = slugify(resolved["name"])
    work = Path(root) / WORK_DIRNAME / slug
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    out_rel = (out_path or f"{ANIMATIC_DIRNAME}/{slug}.{CONTAINER}").replace(
        "\\", "/")
    dst = Path(root) / out_rel
    dst.parent.mkdir(parents=True, exist_ok=True)

    encoder = _encoder(exe)

    panels, segments, per_shot = [], [], []
    for position, shot in enumerate(shots, start=1):
        png = work / f"panel{position:03d}.png"
        drawn = panel(shot, size, png, burn_captions=burn_captions)
        panels.append({"idx": shot["idx"], "path": drawn["path"],
                       "placeholder": drawn["placeholder"]})
        seg = work / f"seg{position:03d}.{CONTAINER}"
        _segment(exe, png, seg, shot["duration"], fps, size, encoder,
                 timeout=timeout)
        segments.append(seg)
        per_shot.append({
            "idx": shot["idx"], "slug": shot["slug"],
            "label": shot["label"][:120],
            "duration_s": shot["duration"],
            "duration_defaulted": shot["duration_defaulted"],
            "transition": shot["transition"],
            "placeholder": drawn["placeholder"],
            "dialogue": bool(shot["dialogue"]),
        })

    plan = cinecut.picture_plan(shots)
    _join(exe, segments, shots, dst, encoder, filtered=plan["filtered"],
          fps=fps, timeout=timeout)

    measured = cinecut.duration_of(dst, ffmpeg=exe)
    lines = cinecut.captions(shots)
    # The sidecar, off the SAME arithmetic the finished cut will use. Two files
    # that agree today and are generated by one function cannot quietly disagree
    # tomorrow.
    caption_files = cinecut.write_captions(root, dst.parent, slug, lines)

    placeholders = [p["idx"] for p in panels if p["placeholder"]]
    runtime = plan["runtime_s"]
    average = round(runtime / max(len(shots), 1), 2)

    out = {
        "ok": True,
        "name": resolved["name"],
        "source": resolved["source"],
        "path": out_rel,
        "bytes": dst.stat().st_size if dst.is_file() else 0,
        "container": CONTAINER,
        "codec": encoder,
        "fps": int(fps),
        "size": f"{size[0]}x{size[1]}",
        "panels": len(panels),
        "placeholders": len(placeholders),
        "placeholder_idx": placeholders,
        "runtime_s": runtime,
        "measured_s": measured,
        "average_shot_s": average,
        "shots": per_shot,
        "captions": caption_files,
        "panel_files": [p["path"] for p in panels],
        "transitions": plan["transitions"],
        # Stated in the payload rather than left to be inferred. Every other
        # verb in the cutscene pipeline returns an estimated_usd and a caller
        # that has to work out which ones are free will eventually guess wrong.
        "estimated_usd": 0.0,
    }
    out["warnings"] = report_notes(out, shots)
    try:
        from ..board import activity

        activity.log(root, "cinematic", summary(out))
    except Exception:                                              # noqa: BLE001
        pass                # a missing log is not a reason to lose the reel
    if not keep_panels:
        # The panels stay until the segments are gone, because they are what a
        # re-render would otherwise redraw; but they are scratch, not artifacts.
        for seg in segments:
            seg.unlink(missing_ok=True)
    return out


def report_notes(out: dict, shots: list) -> list[str]:
    """What the reel just showed you, said out loud.

    THE AVERAGE SHOT LENGTH IS FIRST BECAUSE IT IS THE MOST USEFUL NUMBER HERE.
    It is one figure that says whether an edit reads: modern features sit around
    4-6 seconds, and a sequence averaging 1.8s is a montage nobody can follow
    while one averaging 12s is a slideshow. It is also the number nobody
    calculates by hand, so it is the one previs is best placed to hand over.

    Every note is advisory. This module measures; it does not have opinions
    strong enough to block a scene somebody meant.
    """
    notes = []
    average = float(out.get("average_shot_s") or 0)
    if out["panels"] == 1:
        notes.append(
            "one panel, so this is a held frame rather than an edit — there is "
            "no rhythm here to judge, only the length.")
    elif average < ASL_FAST_S:
        notes.append(
            f"average shot length {average:.1f}s. Modern films sit at "
            f"{ASL_FAST_S:.0f}-{ASL_SLOW_S:.0f}s; under that the cut reads as a "
            "montage and the viewer stops registering individual shots. Fine if "
            "that is the intent, expensive if it is not — every one of these is "
            "a shot you pay full price for and nobody looks at.")
    elif average > ASL_SLOW_S:
        notes.append(
            f"average shot length {average:.1f}s, above the "
            f"{ASL_FAST_S:.0f}-{ASL_SLOW_S:.0f}s a modern audience is used to. "
            "Long holds need something moving in them; a generated shot that "
            "holds still for nine seconds reads as a freeze.")

    if out["placeholders"]:
        notes.append(
            f"{out['placeholders']} panel(s) are slates, not pictures: shots "
            f"{out['placeholder_idx']}. They are held at full length, so the "
            "runtime is honest — but that much of this edit has not actually "
            "been seen by anyone.")

    untimed = [s["idx"] for s in out["shots"] if s["duration_defaulted"]]
    if untimed:
        notes.append(
            f"shots {untimed} had no duration and were held for "
            f"{DEFAULT_DURATION_S:.0f}s each. Time them before promoting: an "
            "untimed shot taking the default is how a scene budgeted for 30s "
            "turns out to be 50.")

    swapped = [s["idx"] for s in shots if s.get("unknown_transition")]
    if swapped:
        notes.append(
            f"shots {swapped} name a transition this build does not know, so "
            "they were cut hard for the previs. Fix them before assembly — "
            "cinecut refuses an unknown transition rather than guessing.")

    measured = float(out.get("measured_s") or 0)
    if measured and abs(measured - float(out["runtime_s"])) > 0.5:
        notes.append(
            f"the reel measures {measured:.1f}s but the shot list adds up to "
            f"{out['runtime_s']:.1f}s. Caption timing is derived from the shot "
            "list, so whichever is wrong, the subtitles inherit it.")

    repeats = []
    for before, after in zip(shots, shots[1:]):
        one = (before.get("label") or "").strip().lower()
        two = (after.get("label") or "").strip().lower()
        if one and one == two:
            repeats.append(after["idx"])
    if repeats:
        notes.append(
            f"shots {repeats} describe the same beat as the shot before them. "
            "Two shots of one picture is a shot you pay for twice and an edit "
            "that stalls; this is the cheapest moment there will ever be to "
            "merge or re-write them.")
    return notes


# ---------------------------------------------------------------------------
# 4. ffmpeg
# ---------------------------------------------------------------------------

def _ffmpeg(given: str = "") -> str:
    exe = _ffmpegbin.resolve(given)
    if not exe:
        raise AnimaticError(
            "ffmpeg is not on PATH, and an animatic is nothing BUT ffmpeg over "
            "PNGs. There is no model to fall back to and nothing to spend "
            "instead. Install one, or point BGATE_FFMPEG at a binary you "
            "already have, and check the ffmpeg row in `bgate doctor`. "
            "DO NOT install Gyan.FFmpeg on Windows, which an earlier version of "
            "this message recommended: that build's libtheora encodes without "
            "error and produces files the decoder cannot read, and it shipped a "
            "whole cutscene of green rectangles before anything noticed. See "
            "bgate_core.runtime.ffmpegbin. Nothing has been generated, charged or "
            "written.")
    return str(exe)


def _encoder(exe: str) -> str:
    """libx264 if this build has it, mpeg4 if not. Never Theora — see CONTAINER."""
    try:
        proc = subprocess.run([exe, "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
        if PREFERRED_ENCODER in (proc.stdout or ""):
            return PREFERRED_ENCODER
    except (OSError, subprocess.SubprocessError):
        pass
    return FALLBACK_ENCODER


def _encode_args(encoder: str) -> list[str]:
    if encoder == PREFERRED_ENCODER:
        # crf 20 on a still image is visually lossless and tiny; veryfast
        # because nothing here has detail a slower preset would preserve.
        return ["-c:v", PREFERRED_ENCODER, "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", FALLBACK_ENCODER, "-q:v", "3", "-pix_fmt", "yuv420p"]


def _segment(exe: str, png: Path, dst: Path, seconds: float, fps: int,
             size: tuple, encoder: str, *, timeout: float) -> None:
    """One panel held for its duration, as a clip.

    Per-panel clips rather than one long filter graph over N images, because the
    join then reuses cinecut's own xfade arithmetic unchanged — the same code
    that times the finished cut. A separate image2 pipeline would be a second
    implementation of shot timing, and the two would disagree eventually.
    """
    # THE FRAME RATE IS SET ON THE INPUT, NOT ONLY THE OUTPUT, and that is a
    # correctness fix rather than tidiness. A looped PNG is read at ffmpeg's
    # default 25fps, so `-t 2` gives 50 input frames which resample to 26 at
    # 12fps — 2.167s, not 2s. Two extra frames per panel is invisible on one
    # shot and compounds: a nine-panel reel measured 1.5s longer than its own
    # shot list, which reads as the timing arithmetic being wrong when in fact
    # the encode was.
    cmd = [exe, "-y", "-loglevel", "error",
           "-framerate", str(int(fps)),
           "-loop", "1", "-t", f"{max(seconds, 0.05):.3f}", "-i", str(png),
           # Explicit even-dimension scale even though the PNG is already the
           # right size: an out-of-tree panel or a future non-even PANEL_SIZES
           # entry would otherwise fail deep inside libx264 with a message about
           # chroma that names nothing recognisable.
           "-vf", f"scale={size[0]}:{size[1]}",
           *_encode_args(encoder), str(dst)]
    _run(cmd, timeout, f"could not render panel {png.name}")


def _join(exe: str, segments: list, shots: list, dst: Path, encoder: str, *,
          filtered: bool, fps: int, timeout: float) -> None:
    """Concat for an all-cuts reel, xfade when a transition asks for it.

    The cheap path is the default for the same reason cinecut's is: a sequence
    of hard cuts needs no filter graph, and the segments were all encoded
    identically, so the demuxer can stream-copy them into one file.
    """
    if not filtered:
        listing = dst.parent / f"{dst.stem}_concat.txt"
        # Forward slashes and the demuxer's single-quote escape, for the reason
        # cinecut.build_picture documents at length: inside a quoted concat entry
        # a backslash is an ESCAPE, so a native Windows path silently becomes a
        # filename containing control characters.
        listing.write_text(
            "".join("file '{}'\n".format(Path(p).as_posix().replace("'", r"'\''"))
                    for p in segments), encoding="utf-8")
        cmd = [exe, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", str(listing), "-c", "copy", "-movflags", "+faststart",
               str(dst)]
        try:
            _run(cmd, timeout, "could not join the panels")
        finally:
            listing.unlink(missing_ok=True)
        return

    graph, label = cinecut.xfade_graph(shots)
    cmd = [exe, "-y", "-loglevel", "error"]
    for seg in segments:
        cmd += ["-i", str(seg)]
    cmd += ["-filter_complex", graph, "-map", f"[{label}]", "-an",
            "-r", str(int(fps)), *_encode_args(encoder),
            "-movflags", "+faststart", str(dst)]
    _run(cmd, timeout, "could not join the panels with their transitions")


def _run(cmd: list, timeout: float, what: str) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise AnimaticError(f"{what}: ffmpeg did not finish within "
                            f"{timeout:.0f}s") from exc
    except OSError as exc:
        raise AnimaticError(f"{what}: {exc}") from exc
    if proc.returncode != 0:
        raise AnimaticError(
            f"{what}: " + ((proc.stderr or "").strip()[-300:]
                           or f"ffmpeg exited {proc.returncode}"))


def summary(out: dict) -> str:
    """One line for a log or an activity row."""
    return (f"animatic {out['name']}: {out['panels']} panels, "
            f"{out['runtime_s']:.1f}s, avg {out['average_shot_s']:.1f}s, "
            f"{out['placeholders']} slate(s)")


__all__ = ["AnimaticError", "build", "resolve", "panel", "report_notes",
           "summary", "ANIMATIC_DIRNAME", "PANEL_SIZES", "FPS",
           "DEFAULT_DURATION_S"]
