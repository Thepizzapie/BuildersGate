"""Post-production: the half between a folder of clips and a cutscene.

WHY THIS IS A SECOND MODULE. ``bgate_core.cinematic`` ACQUIRES — it plans a shot
list, buys shots, judges them and files them as assets. Everything here happens
after every shot is bought and none of it costs a credit: timing captions,
joining with transitions, laying sound under the picture, checking that the shots
actually cut together, and building the scene the engine plays. Two different
jobs with two different failure modes, and one 2000-line module that did both
would hide the fact that the second half exists at all — which is exactly what
happened before it was written.

WHAT WAS MISSING, STATED PLAINLY, BECAUSE ONE PIECE WAS WORSE THAN MISSING.
cinematic.py's docstring, the cinematic seat's brief and the research note all
said generated audio is off by default because "the audio seat scores the
cutscene over the top". There was no bed, no mix and no mux: every assembled cut
shipped SILENT while three documents described a mechanism that did not exist. A
sentence that reads as a design decision and is actually an unbuilt feature is
worse than an admitted gap, because nobody goes looking for it. The rest was
honestly absent — hard butt-joins with no transitions, dialogue stored per shot
and rendered nowhere, no measurement of whether two shots cut together, and an
.ogv installed into the project with no scene to play it.

THE FIVE THINGS HERE, AND THE ORDER IS THE ORDER THEY DEPEND ON EACH OTHER.
  1. :func:`captions` — WHEN each line is on screen, derived from the shot
     durations and the transitions between them. Nothing stores caption timing,
     because the shot list already answers that question and a second answer
     would disagree the first time a duration changed.
  2. :func:`build_picture` — the join, with fades and dissolves. A sequence whose
     transitions are all cuts still takes the cheap concat path.
  3. :func:`mix_audio` — the bed (and per-shot VO) under the picture, muxed into
     the Ogg the engine plays.
  4. :func:`continuity` — whether the shots MATCH. The art seat has real
     detectors for a frame; a cut had "look at it" and nothing else.
  5. :func:`deliver` — the .tscn, the script, the skip, the finished signal. An
     .ogv sitting in the project is not a cutscene until something plays it.

EVERY MEASUREMENT HERE IS ON THE FILE, NEVER ON THE REQUEST. That is the trap
this repo has already paid for twice (seats.py: "A TOOL REPORTING ITS OWN SUCCESS
IS NOT EVIDENCE"), and it is why continuity extracts real frames with ffmpeg
rather than comparing the prompts that produced them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import assets
from . import ffmpegbin as _ffmpegbin

# Windows: never flash a console window out of an ffmpeg call. Every other
# module in this product that spawns a binary does this (doctor, gitwork,
# playtest, blender, godot) and the cutscene pipeline spawns MORE of them than
# any of them — one per shot for continuity, one per join, one per mix.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# How two shots may be joined. `cut` is the default and is free — it needs no
# filter graph, so a sequence that wants none keeps the fast concat path.
TRANSITIONS = {
    "cut": {"label": "Hard cut", "filter": "",
            "note": "No handle, no cost. The right answer most of the time — a "
                    "dissolve between every shot reads as a slideshow."},
    "fade": {"label": "Fade through black", "filter": "fadeblack",
             "note": "Marks a jump in time or place. Costs its duration from "
                     "BOTH shots, so a 1s fade on a 5s shot spends a fifth of "
                     "the beat on black."},
    "dissolve": {"label": "Cross dissolve", "filter": "dissolve",
                 "note": "Softens a mismatch between two shots, which makes it "
                         "the honest fix for a continuity flag you cannot "
                         "re-generate your way out of."},
    "wipe": {"label": "Wipe left", "filter": "wipeleft",
             "note": "Deliberately artificial. Reads as a stylistic choice "
                     "rather than as coverage, so use it on purpose or not at "
                     "all."},
}
DEFAULT_TRANSITION = "cut"

# Caption timing. A line is on screen for its shot, less a breath at each end so
# two consecutive lines do not touch — subtitles that abut read as one block.
CAPTION_PAD_S = 0.15
# Below this, a caption cannot be read at all and is better left out than
# flashed. Two words at normal reading speed.
CAPTION_MIN_S = 0.8

# Where a delivered cutscene's scene and script are written, relative to the
# engine project. Beside the .ogv rather than in a scenes/ tree, because the
# three files are one asset and a designer moving the cutscene should move all
# of it.
SCENE_DIRNAME = "cinematics"

# Continuity thresholds, in the units the checks actually produce. Deliberately
# loose: this exists to catch a shot that does not belong, not to enforce a
# grade. A detector that fires on everything gets switched off, which is the
# failure mode the art seat's rule 8 names.
LUMA_JUMP = 42.0        # mean brightness delta, 0..255
PALETTE_JUMP = 95.0     # mean nearest-colour distance in RGB space


class CutError(RuntimeError):
    """A post-production step failed in a way the caller should surface."""


# ---------------------------------------------------------------------------
# 1. Captions
# ---------------------------------------------------------------------------

def captions(shots: list) -> list[dict]:
    """When each line of dialogue is on screen, in cut time.

    DERIVED, NEVER STORED. The shot list already says how long every shot runs
    and how it is joined; caption timing is arithmetic on those two facts. A
    stored copy would be a second answer that disagrees the first time somebody
    changes a duration — and the person changing it would have no reason to
    think they had just broken the subtitles.

    A TRANSITION EATS TIME FROM BOTH SHOTS. A one-second dissolve is not one
    second of extra runtime; it is a second in which both shots are on screen at
    once, so the cut is a second SHORTER than the sum of its parts. Timing
    captions off the naive sum drifts later and later through a sequence, which
    is the classic subtitle bug and is invisible until the last line of a long
    scene lands over the fade to black.
    """
    out, clock = [], 0.0
    for index, shot in enumerate(shots):
        overlap = _overlap_before(shots, index)
        clock -= overlap
        start = clock
        duration = float(shot.get("duration") or 0)
        clock += duration
        text = str(shot.get("dialogue") or "").strip()
        if not text:
            continue
        window = duration - (CAPTION_PAD_S * 2)
        if window < CAPTION_MIN_S:
            # Too short to read. Given the whole shot rather than dropped: a
            # line the writer put in the scene should not vanish because the
            # shot is brief, and a caption that cannot be read is still better
            # evidence of a pacing problem than silence.
            begin, end = start, start + duration
        else:
            begin, end = start + CAPTION_PAD_S, start + duration - CAPTION_PAD_S
        out.append({
            "idx": int(shot.get("idx") or index + 1),
            "start": round(max(begin, 0.0), 3),
            "end": round(max(end, begin + 0.1), 3),
            "text": text,
            "short": window < CAPTION_MIN_S,
        })
    return _no_overlap(out)


def _no_overlap(lines: list) -> list[dict]:
    """Stop two captions being on screen at once. The bug a dissolve creates.

    MEASURED, on the first sequence assembled with a transition: shot 1's line
    ran to 4.85s and shot 2's began at 4.15s, because a 1s dissolve pulls the
    incoming shot BACK by a second while the outgoing line still owns its own
    shot's full length. Seven tenths of a second with two subtitles stacked.

    It is worse than it looks, because the player script takes the first line
    whose window contains the clock — so for that stretch the screen shows the
    PREVIOUS line over the NEW shot, which reads as a caption lagging the cut.
    Clamping here rather than in the player keeps the .srt correct too, and the
    .srt is the file a translator opens and a caption tool validates.
    """
    for first, second in zip(lines, lines[1:]):
        limit = second["start"] - CAPTION_PAD_S
        if first["end"] > limit:
            # Never invert the window: a line whose shot is almost entirely
            # eaten by the dissolve keeps a readable minimum and is flagged
            # short, which is a pacing problem to see rather than to hide.
            first["end"] = round(max(limit, first["start"] + 0.1), 3)
            if first["end"] - first["start"] < CAPTION_MIN_S:
                first["short"] = True
    return lines


def _overlap_before(shots: list, index: int) -> float:
    """Seconds shot `index` overlaps the one before it, 0 for a cut."""
    if index == 0:
        return 0.0
    shot = shots[index]
    kind = str(shot.get("transition") or DEFAULT_TRANSITION)
    if kind == "cut" or kind not in TRANSITIONS:
        return 0.0
    # Never longer than either neighbour can afford, or ffmpeg's xfade offset
    # goes negative and the join silently drops a shot.
    return min(float(shot.get("transition_s") or 0.5),
               float(shots[index - 1].get("duration") or 0) - 0.1,
               float(shot.get("duration") or 0) - 0.1)


def runtime_of(shots: list) -> float:
    """The cut's real length: the sum of the shots, less every overlap."""
    total = sum(float(s.get("duration") or 0) for s in shots)
    return round(total - sum(_overlap_before(shots, i)
                             for i in range(len(shots))), 3)


def to_srt(lines: list) -> str:
    """SubRip, which is what a translator's tools open.

    SRT rather than a bespoke format because localisation is the whole reason
    captions are a FILE and not baked pixels: an .srt goes to a translator and
    comes back, and nothing in this product has to understand what came back.
    """
    out = []
    for n, line in enumerate(lines, start=1):
        out.append(str(n))
        out.append(f"{_srt_clock(line['start'])} --> {_srt_clock(line['end'])}")
        out.append(line["text"])
        out.append("")
    return "\n".join(out)


def _srt_clock(seconds: float) -> str:
    whole = int(seconds)
    ms = int(round((seconds - whole) * 1000))
    if ms == 1000:                      # rounding up a whole second
        whole, ms = whole + 1, 0
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d},{ms:03d}"


def write_captions(root: str | os.PathLike[str], out_dir: Path, stem: str,
                   lines: list) -> dict:
    """Write both caption files: .srt for people, .json for the engine.

    TWO FILES, ONE SOURCE, and neither is redundant. The .srt is the artifact a
    translator edits and a localisation pipeline round-trips. The .json is what
    the generated cutscene script reads, because GDScript parses JSON in one
    call and parsing SubRip would be thirty lines of string handling in a script
    this product generates and nobody maintains.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    srt = out_dir / f"{stem}.srt"
    data = out_dir / f"{stem}_captions.json"
    srt.write_text(to_srt(lines), encoding="utf-8")
    data.write_text(json.dumps(
        [{k: v for k, v in line.items() if k in ("start", "end", "text")}
         for line in lines], indent=2), encoding="utf-8")
    return {
        "srt": assets.normalize_path(root, srt),
        "json": assets.normalize_path(root, data),
        "lines": len(lines),
    }


# ---------------------------------------------------------------------------
# 2. The join
# ---------------------------------------------------------------------------

def picture_plan(shots: list, *, fade_in: float = 0.0,
                 fade_out: float = 0.0) -> dict:
    """What joining these shots will take, decided before ffmpeg is touched.

    Answers one question the caller needs and one it does not know to ask: does
    this need a filter graph at all. A sequence of hard cuts with no fades is
    joined by the concat demuxer, which is one decode and one encode; anything
    else needs xfade, which decodes every shot in full. Paying that for a
    sequence that wants none would be a real cost for no effect.
    """
    kinds = [str(s.get("transition") or DEFAULT_TRANSITION) for s in shots[1:]]
    unknown = sorted({k for k in kinds if k not in TRANSITIONS})
    if unknown:
        raise CutError(f"unknown transition(s) {unknown} — known: "
                       f"{sorted(TRANSITIONS)}")
    needs = any(k != "cut" for k in kinds) or fade_in > 0 or fade_out > 0
    return {
        "filtered": needs,
        "transitions": kinds,
        "runtime_s": runtime_of(shots),
        "fade_in": float(fade_in),
        "fade_out": float(fade_out),
        "why": ("cuts only and no fades — joined with the concat demuxer, one "
                "decode and one encode" if not needs else
                "at least one transition or fade, so every shot is decoded and "
                "recomposited through xfade"),
    }


def xfade_graph(shots: list, *, fade_in: float = 0.0,
                fade_out: float = 0.0) -> tuple[str, str]:
    """The filter_complex string joining N shots with their transitions.

    xfade CHAINS PAIRWISE and its `offset` is measured on the OUTPUT built so
    far, not on the input being added — which is the one thing that makes this
    fiddly and the one thing that fails silently. Get the offset wrong and
    ffmpeg still exits 0, having produced a cut with a shot missing or a frozen
    frame where the join should be.
    """
    if not shots:
        raise CutError("nothing to join")
    parts, current, clock = [], "0:v", float(shots[0].get("duration") or 0)
    for index in range(1, len(shots)):
        shot = shots[index]
        kind = str(shot.get("transition") or DEFAULT_TRANSITION)
        overlap = _overlap_before(shots, index)
        label = f"v{index}"
        if kind == "cut" or overlap <= 0:
            # A zero-length xfade is not a cut, it is an error. Concat the pair.
            parts.append(f"[{current}][{index}:v]concat=n=2:v=1:a=0[{label}]")
            clock += float(shot.get("duration") or 0)
        else:
            spec = TRANSITIONS[kind]["filter"]
            offset = max(clock - overlap, 0.0)
            parts.append(
                f"[{current}][{index}:v]xfade=transition={spec}:"
                f"duration={overlap:.3f}:offset={offset:.3f}[{label}]")
            clock += float(shot.get("duration") or 0) - overlap
        current = label

    tail = []
    if fade_in > 0:
        tail.append(f"fade=t=in:st=0:d={float(fade_in):.3f}")
    if fade_out > 0:
        tail.append(
            f"fade=t=out:st={max(clock - float(fade_out), 0.0):.3f}:"
            f"d={float(fade_out):.3f}")
    if tail:
        parts.append(f"[{current}]{','.join(tail)}[vout]")
        current = "vout"
    elif len(shots) == 1:
        # One shot and no fades still needs a named output to map.
        parts.append("[0:v]null[vout]")
        current = "vout"
    return ";".join(parts), current


def build_picture(sources: list, shots: list, out_path: str | os.PathLike[str],
                  *, fade_in: float = 0.0, fade_out: float = 0.0,
                  quality: int = 6, gop: str = "64", ffmpeg: str = "",
                  timeout: float = 3600.0) -> dict:
    """Join the shots into one Theora picture, with whatever transitions they ask.

    TWO PATHS, AND THE CHEAP ONE IS THE DEFAULT. Hard cuts with no fades go
    through the concat demuxer: one decode, one encode, no recompositing. Any
    transition or fade needs xfade, which decodes every shot in full and costs
    real time on a long sequence — so a project that wants none never pays for
    it. :func:`picture_plan` is where that decision is visible before it is made.

    ONE PASS EITHER WAY. Converting each shot to .ogv and joining the results
    would put the sequence through Theora twice — a lossy codec applied to its
    own output — so the .mp4 shots go in and one .ogv comes out.
    """
    exe = ffmpeg or _ffmpeg()
    dst = Path(out_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    plan = picture_plan(shots, fade_in=fade_in, fade_out=fade_out)

    encode = ["-codec:v", "libtheora", "-q:v", str(int(quality)),
              "-g:v", str(gop), "-codec:a", "libvorbis", "-q:a", str(int(quality))]
    listing = None
    if not plan["filtered"]:
        listing = dst.parent / f"{dst.stem}_concat.txt"
        # The demuxer's own quoting rule: single quotes, and an embedded one is
        # escaped by closing, escaping, reopening. Paths with an apostrophe exist.
        #
        # FORWARD SLASHES, WHICH ON WINDOWS IS A CORRECTNESS FIX AND NOT A STYLE
        # ONE. Inside a quoted concat entry the demuxer treats `\` as an ESCAPE
        # character, so a native Windows path is read as escape sequences: a
        # project at C:\Users\nina\new-game feeds it `\n`, and the entry silently
        # becomes a filename with a newline in it that ffmpeg then cannot open.
        # ffmpeg accepts forward slashes on Windows everywhere, so as_posix() is
        # the spelling that is right on both platforms — and Windows is the
        # supported one here, so this is the platform that matters.
        listing.write_text(
            "".join("file '{}'\n".format(Path(p).as_posix().replace("'", r"'\''"))
                    for p in sources), encoding="utf-8")
        cmd = [exe, "-y", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(listing),
               *encode, str(dst)]
    else:
        graph, out_label = xfade_graph(shots, fade_in=fade_in, fade_out=fade_out)
        cmd = [exe, "-y", "-loglevel", "error"]
        for one in sources:
            cmd += ["-i", str(one)]
        # -an: the picture pass drops audio deliberately. xfade does not cross
        # the audio streams, so keeping them would butt-join whatever the models
        # generated underneath a dissolving picture. Sound arrives in mix_audio,
        # from the audio seat, on purpose.
        cmd += ["-filter_complex", graph, "-map", f"[{out_label}]", "-an",
                *encode[:6], str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise CutError(f"ffmpeg did not finish joining within "
                       f"{timeout:.0f}s") from exc
    finally:
        if listing is not None:
            listing.unlink(missing_ok=True)
    if proc.returncode != 0 or not dst.is_file():
        raise CutError("ffmpeg could not assemble the cut: "
                       + ((proc.stderr or "").strip()[-300:]
                          or f"exit {proc.returncode} and no output file"))
    measured = duration_of(dst, ffmpeg=exe)
    out = {"path": str(dst), "bytes": dst.stat().st_size,
           "filtered": plan["filtered"], "transitions": plan["transitions"],
           "planned_s": plan["runtime_s"], "measured_s": measured}
    # THE PLAN IS ARITHMETIC; THE FILE IS THE TRUTH. Caption timing is built
    # from the same arithmetic, so a disagreement here means the subtitles are
    # wrong too — and that is invisible until the last line of a long scene
    # lands over the fade to black.
    if measured and abs(measured - plan["runtime_s"]) > 0.75:
        out["timing_warning"] = (
            f"the cut measures {measured:.1f}s but the shot list adds up to "
            f"{plan['runtime_s']:.1f}s. Caption timing is derived from the shot "
            "list, so it will drift — check the durations against the clips.")
    return out


# ---------------------------------------------------------------------------
# 3. Sound
# ---------------------------------------------------------------------------

def mix_audio(video: str | os.PathLike[str], out_path: str | os.PathLike[str],
              *, bed: str = "", gain_db: float = 0.0,
              fade_in: float = 0.0, fade_out: float = 0.0,
              quality: int = 6, ffmpeg: str = "",
              timeout: float = 1800.0) -> dict:
    """Lay a music bed under a finished picture and mux it into the Ogg.

    THE VIDEO IS COPIED, NOT RE-ENCODED. The picture has already been through
    Theora once; putting it through again to attach an audio stream would be a
    second generation of loss for nothing, and Theora is lossy enough that the
    difference is visible on a gradient. `-c:v copy` with a new Vorbis stream is
    the whole operation.

    THE BED IS TRIMMED TO THE PICTURE, NOT THE OTHER WAY ROUND. `-shortest` on a
    three-minute track under a forty-second cut would be correct; on a
    thirty-second track under a forty-second cut it would silently truncate the
    CUTSCENE to thirty seconds. So the video length is authoritative and the
    audio is padded with silence when it falls short — and the shortfall is
    reported, because a bed that runs out ten seconds early is a thing to fix,
    not to discover in the game.
    """
    exe = ffmpeg or _ffmpeg()
    src, dst = Path(video), Path(out_path)
    if not src.is_file():
        raise CutError(f"nothing on disk at {src}")
    track = Path(bed) if bed else None
    if track is not None and not track.is_file():
        raise CutError(f"audio bed not found: {track}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    picture_s = duration_of(src, ffmpeg=exe)
    bed_s = duration_of(track, ffmpeg=exe) if track else 0.0

    chain = []
    if gain_db:
        chain.append(f"volume={float(gain_db):.2f}dB")
    if fade_in > 0:
        chain.append(f"afade=t=in:st=0:d={float(fade_in):.3f}")
    if fade_out > 0 and picture_s > 0:
        chain.append(
            f"afade=t=out:st={max(picture_s - float(fade_out), 0.0):.3f}:"
            f"d={float(fade_out):.3f}")
    # Pad first, then trim: apad alone runs forever, atrim alone cannot lengthen.
    chain.append("apad")
    chain.append(f"atrim=0:{picture_s:.3f}")

    cmd = [exe, "-y", "-loglevel", "error", "-i", str(src), "-i", str(track),
           "-filter_complex", f"[1:a]{','.join(chain)}[a]",
           "-map", "0:v", "-map", "[a]",
           "-c:v", "copy", "-c:a", "libvorbis", "-q:a", str(int(quality)),
           str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise CutError(f"ffmpeg did not finish mixing within "
                       f"{timeout:.0f}s") from exc
    if proc.returncode != 0 or not dst.is_file():
        raise CutError("ffmpeg could not mix the audio bed: "
                       + ((proc.stderr or "").strip()[-300:]
                          or f"exit {proc.returncode} and no output"))
    short_by = round(picture_s - bed_s, 2) if bed_s else 0.0
    return {
        "path": str(dst), "bytes": dst.stat().st_size,
        "picture_s": picture_s, "bed_s": bed_s,
        "padded_with_silence_s": max(short_by, 0.0),
        "note": (f"the bed is {short_by:.1f}s shorter than the cut and the "
                 "remainder is silence — extend or loop the track"
                 if short_by > 0.5 else ""),
    }


def duration_of(path: Optional[Path], *, ffmpeg: str = "") -> float:
    """Length in seconds, measured with ffprobe. 0.0 when unknowable.

    Measured rather than summed from the shot list, because this is the check on
    that arithmetic: if the file and the plan disagree, the plan is what is
    wrong and the caption timing built from it is wrong too.
    """
    if path is None:
        return 0.0
    import shutil as _shutil

    probe = _shutil.which("ffprobe")
    if not probe:
        return 0.0
    try:
        proc = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        return round(float((proc.stdout or "0").strip() or 0), 3)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _ffmpeg() -> str:

    exe = _ffmpegbin.resolve()
    if not exe:
        raise CutError("ffmpeg not found on PATH")
    return exe


# ---------------------------------------------------------------------------
# 4. Continuity — does this actually cut together
# ---------------------------------------------------------------------------

def continuity(clips: list, *, work_dir: Path,
               ffmpeg: str = "") -> list[dict]:
    """Compare the frames either side of every join. Costs nothing but time.

    WHAT THIS IS AND IS NOT. It is the cut-level twin of the art seat's
    consistency checks, and it inherits their humility: it CANNOT tell you the
    cutscene is good. It measures two things that are objectively comparable
    across a join — overall brightness and the colour palette — and flags a jump.
    Whether the jump is a mistake or the point (a cut from a cellar to a
    snowfield SHOULD be a luma jump) is a human's call, which is why every
    finding says what it measured rather than passing a verdict.

    IT READS THE ACTUAL FRAMES. Comparing the prompts that produced two shots
    would be comparing intentions; the whole reason a cut fails is that the model
    did something other than what was asked. So this extracts the last frame of
    one clip and the first frame of the next with ffmpeg and looks at the pixels.

    The thresholds are deliberately loose. A detector that fires on every join
    gets switched off, which is worse than never having had one — the art seat's
    rule 8, paid for once already.
    """
    from . import chroma

    exe = ffmpeg or _ffmpeg()
    work_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for index in range(1, len(clips)):
        before, after = Path(clips[index - 1]), Path(clips[index])
        tail = work_dir / f"j{index}_out.png"
        head = work_dir / f"j{index}_in.png"
        end = max(duration_of(before, ffmpeg=exe) - 0.1, 0.0)
        if not (_frame(exe, before, end, tail) and _frame(exe, after, 0.0, head)):
            out.append({"join": index, "ok": None,
                        "note": "could not extract the frames either side of "
                                "this join, so nothing was checked"})
            continue
        try:
            luma_a, luma_b = _mean_luma(tail), _mean_luma(head)
            pal_a = chroma.palette_of(tail, colors=8)
            pal_b = chroma.palette_of(head, colors=8)
            palette = sum(chroma.distance_to(c, pal_a) for c in pal_b) / max(len(pal_b), 1)
        except Exception as exc:                                 # noqa: BLE001
            out.append({"join": index, "ok": None,
                        "note": f"frames extracted but not comparable: {exc}"})
            continue
        luma = abs(luma_a - luma_b)
        flags = []
        if luma > LUMA_JUMP:
            flags.append(
                f"brightness jumps {luma:.0f}/255 across this join. If the two "
                "shots are the same place at the same time, one of them is lit "
                "wrong; if they are not, this is fine and a dissolve will sell "
                "it either way.")
        if palette > PALETTE_JUMP:
            flags.append(
                f"the palettes are {palette:.0f} apart, which reads as a colour "
                "grade change mid-scene. A style that was applied to one shot "
                "and not the other looks exactly like this.")
        out.append({
            "join": index, "ok": not flags,
            "between": [before.name, after.name],
            "luma_delta": round(luma, 1),
            "palette_distance": round(palette, 1),
            "flags": flags,
            "frames": [str(tail), str(head)],
        })
    return out


def _frame(exe: str, video: Path, at: float, out_path: Path) -> bool:
    """One frame out of a clip. False rather than raising — a check that cannot
    run must not take the assembly down."""
    cmd = [exe, "-y", "-loglevel", "error", "-ss", f"{max(at, 0.0):.3f}",
           "-i", str(video), "-frames:v", "1", str(out_path)]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                       stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return out_path.is_file()


def _mean_luma(path: Path) -> float:
    """Average perceived brightness, 0..255, on a downscaled copy.

    Rec. 601 weights rather than a plain RGB mean: the eye is roughly six times
    more sensitive to green than to blue, so an unweighted average calls a
    saturated blue frame and a mid-grey one equally bright and the check misses
    the jump a viewer sees.
    """
    from PIL import Image

    with Image.open(path) as im:
        small = im.convert("RGB").resize((64, 36))
        pixels = list(small.getdata())
    if not pixels:
        return 0.0
    return sum(0.299 * r + 0.587 * g + 0.114 * b
               for r, g, b in pixels) / len(pixels)


# ---------------------------------------------------------------------------
# 5. Delivery — the scene the engine actually plays
# ---------------------------------------------------------------------------

CUTSCENE_GD = '''extends CanvasLayer
## Plays one generated cutscene, draws its captions, and gets out of the way.
##
## GENERATED by Builders Gate (bgate_core.cinecut). Safe to edit — nothing
## regenerates this file unless you ask for delivery again, and delivery refuses
## to overwrite a script you have changed.
##
## Usage from gameplay code:
##     var cut := preload("res://{scene_res}").instantiate()
##     add_child(cut)
##     await cut.finished
##
## `finished` fires whether the video ended or the player skipped it, because
## every caller wants the same thing next and branching on which is how a
## skipped cutscene leaves a game stuck on a black screen.

signal finished(skipped: bool)

## Any of these ends the cutscene early. ui_cancel is Escape by default;
## ui_accept is Enter and Space. A cutscene a player cannot skip is the single
## most complained-about thing in a game, so skipping is on by default.
@export var skippable: bool = true
@export var captions_path: String = "res://{captions_res}"

@onready var _video: VideoStreamPlayer = $Video
@onready var _caption: Label = $Caption

var _lines: Array = []
var _done := false


func _ready() -> void:
    _lines = _load_captions()
    _caption.text = ""
    _video.finished.connect(_on_finished)
    _video.play()


func _load_captions() -> Array:
    if captions_path.is_empty() or not FileAccess.file_exists(captions_path):
        return []
    var raw := FileAccess.get_file_as_string(captions_path)
    var parsed: Variant = JSON.parse_string(raw)
    return parsed if parsed is Array else []


func _process(_delta: float) -> void:
    if _done:
        return
    # VideoStreamPlayer reports its own clock, which is the only one that stays
    # correct when the video stalls on a slow disk. A separate timer drifts.
    var t := _video.stream_position
    var text := ""
    for line in _lines:
        if t >= float(line.get("start", 0.0)) and t <= float(line.get("end", 0.0)):
            text = str(line.get("text", ""))
            break
    if text != _caption.text:
        _caption.text = text


func _unhandled_input(event: InputEvent) -> void:
    if not skippable or _done:
        return
    if event.is_action_pressed("ui_cancel") or event.is_action_pressed("ui_accept"):
        get_viewport().set_input_as_handled()
        _finish(true)


func _on_finished() -> void:
    _finish(false)


func _finish(skipped: bool) -> void:
    if _done:
        return
    _done = true
    _video.stop()
    _caption.text = ""
    finished.emit(skipped)
    queue_free()
'''


def cutscene_scene_text(video_res: str, script_res: str,
                        *, node_name: str = "Cutscene") -> str:
    """The .tscn source: a full-screen video with a caption label over it.

    A CanvasLayer ROOT, not a Control, and that is the load-bearing choice. A
    cutscene has to draw over whatever the game is currently rendering — a 2D
    level, a 3D viewport, the HUD — and a CanvasLayer is the only node that
    guarantees it, at a layer above the default. Added as a child of anything,
    it covers the screen; as a Control it would inherit its parent's transform
    and land wherever that happened to be.

    `expand = true` with KEEP_ASPECT_COVERED fills the screen without letterbox
    bars of a colour nobody chose. The caption sits in the lower fifth with an
    outline, because subtitle text over an arbitrary frame is unreadable without
    one and a shadowed box is the convention every player already knows.
    """
    return "\n".join([
        "[gd_scene load_steps=4 format=3]",
        "",
        f'[ext_resource type="VideoStream" path="{video_res}" id="1_video"]',
        f'[ext_resource type="Script" path="{script_res}" id="2_script"]',
        "",
        '[sub_resource type="LabelSettings" id="LabelSettings_caption"]',
        "font_size = 28",
        "outline_size = 6",
        "outline_color = Color(0, 0, 0, 1)",
        "",
        f'[node name="{node_name}" type="CanvasLayer"]',
        "layer = 100",
        'script = ExtResource("2_script")',
        "",
        '[node name="Video" type="VideoStreamPlayer" parent="."]',
        "anchors_preset = 15",
        "anchor_right = 1.0",
        "anchor_bottom = 1.0",
        "grow_horizontal = 2",
        "grow_vertical = 2",
        'stream = ExtResource("1_video")',
        "expand = true",
        "",
        '[node name="Caption" type="Label" parent="."]',
        "anchors_preset = 12",
        "anchor_top = 1.0",
        "anchor_right = 1.0",
        "anchor_bottom = 1.0",
        "offset_top = -160.0",
        "offset_bottom = -48.0",
        "grow_horizontal = 2",
        "grow_vertical = 0",
        "horizontal_alignment = 1",
        "vertical_alignment = 1",
        "autowrap_mode = 3",
        'label_settings = SubResource("LabelSettings_caption")',
        "",
    ])
