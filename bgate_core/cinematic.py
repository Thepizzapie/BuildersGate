"""Generated cutscenes, and the four things between a video model and a game.

WHAT WAS MISSING, AND IT IS THE SAME GAP music.py WAS WRITTEN TO CLOSE.
:func:`bgate_adapters.kie.generate_video` could already buy a clip. What it could
not do was hand the result to anything: it wrote an .mp4 into ``.bgate_out/`` and
returned a path carrying the sentence "nothing downstream imports video yet". The
MCP server repeated it, ``providers.CAPABILITIES`` listed ``video`` with the note
that it has no provider, and the money bought a file no surface in this product
could see. This module is the missing half.

IT IS NOT A COPY OF music.py, BECAUSE VIDEO BREAKS THREE OF ITS ASSUMPTIONS.
Music got away with candidate -> human keeps it -> COPY the bytes into the engine
project. Every one of those steps is different here, and each difference cost a
piece of this design:

  1. GODOT CANNOT PLAY WHAT THE MODEL RETURNS. Not "plays it badly" — cannot.
     VideoStreamPlayer supports Ogg Theora (.ogv) and nothing else in core;
     H.264 and H.265 are patent-encumbered and cannot be shipped in the engine,
     and WebM was REMOVED in 4.0. Every video model on the market returns H.264
     in an .mp4. So keeping a shot TRANSCODES it (see :func:`transcode`); a copy
     would put a file in the project that the engine refuses at load with an
     error naming the format, which is the music bug in reverse and worse — the
     database would say shipped and the cutscene would be a black rectangle.
     This is also why ffmpeg is a hard requirement of the keep path and only a
     soft one everywhere else in this product.

  2. ONE GENERATION IS NOT ONE DELIVERABLE. A Suno request returns the track. No
     video model wired here will generate more than 15 seconds (kie MODELS,
     seedance-2: duration 4..15), and the industry practice is tighter than the
     ceiling — shots are written at 5 to 10 seconds because that is where the
     models hold together. A cutscene is therefore a SEQUENCE of paid
     generations that has to be planned, ordered, individually judged,
     individually re-rolled, and concatenated. That is the whole reason
     :func:`plan` exists and music has no equivalent.

  3. THE ANCHOR PROBLEM IS SHARPER AND HAS NO FALLBACK. An off-model sprite is
     one bad frame. An off-model cutscene is the player watching a stranger
     deliver the story beat. Krea can anchor a still on a local pinned ref and
     kie could not anchor anything at all, so the art seat has somewhere to go
     when identity matters; video has nowhere — no other provider here generates
     a frame of it. That is why :func:`bgate_adapters.kie.upload_file` was
     wired: it is what lets an approved character reach a shot.

THE CONTINUITY RULE IS THE ART SEAT'S RULE 2, AND IT IS NOT NEGOTIABLE HERE.
"NEVER CONDITION FRAME N ON FRAME N-1. Chains decay." — seats.py, measured on a
shipped game where a back view turned front-facing by frame 3. The tempting shape
for a sequence is to pull the last frame out of shot N's .mp4 and hand it to shot
N+1 as its first frame, because the field is right there and it looks like exactly
what it is for. It is the same chain, with a worse decay constant: a video model's
final frame is already the most drifted image it produced, it carries the
compression of a lossy intermediate, and eight shots of it is eight generations of
a photocopy. So :func:`keyframes_for` derives every shot's conditioning frames
from the PINNED REFS through the image pipeline, and every shot in a sequence
anchors on the same stills. The last-frame field is for a deliberate two-shot
match cut a human asked for, not for the spine of the sequence.

AUDIO IS OFF BY DEFAULT AND THE AUDIO SEAT OWNS SOUND. Seedance generates its own
audio unless told not to, and for a film that is the feature. For a game it is a
trap: the generated bed is BAKED INTO THE CLIP and cannot be separated later, so
it fights the music the audio seat wrote, it cannot be ducked under dialogue, it
cannot be re-mixed for a localisation pass, and Godot downmixes Theora audio to
stereo regardless of what arrives. A cutscene here is PICTURE; the score and the
voice come from the seat that owns them, over the top, where they stay editable.
Pass ``generate_audio=True`` to override it deliberately.

NO URL IS EVER THE ASSET. kie serves generated files for a limited window and its
uploaded conditioning frames die in three days. Source URLs are recorded in
metadata as PROVENANCE ONLY, stamped with the date they die.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from . import activity, artifacts, assets, cinecut, db
from .util import rows, slugify

# Windows: never flash a console window out of a probe or an encode. The same
# constant five other modules here carry, for the same reason.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Where shots land. Under .bgate_out because a generated shot is scratch until a
# human keeps it — gitignored, and no rejected take reaches the engine project.
CANDIDATE_DIR = Path(".bgate_out") / "cinematic"

# Where a KEPT cutscene is installed. First existing wins, so a project that
# already files cutscenes somewhere does not grow a second tree.
INSTALL_ROOTS = (Path("game") / "assets" / "cinematics", Path("cinematics"))

PRODUCER = "kie_video"

# What the models hand back, and what the engine will take. Two different sets,
# which is the entire reason transcode() exists.
SOURCE_SUFFIXES = {".mp4", ".mov", ".webm"}
ENGINE_SUFFIX = ".ogv"

# THE ENCODE, FROM GODOT'S OWN DOCUMENTATION rather than from a blog or from
# taste. `-q:v 6` and `-q:a 6` are the quality the engine docs recommend as the
# baseline; `-g:v 64` is the keyframe interval, and it is the one that is NOT
# optional — libtheora's default GOP is 12, which the docs call insufficient and
# which inflates a cutscene several times over for nothing. The range they
# sanction is 64..512, larger trading seek time for size, and a cutscene is
# played start to finish so the seek cost is not real.
#
# NOT PARAMETERISED INTO OBLIVION. Quality is a knob because a 1440p source
# genuinely wants 5; the GOP is not, because every value a caller could pass
# other than this one is worse for the only thing this encodes.
THEORA_GOP = "64"
DEFAULT_QUALITY = 6

# The window a shot is written to. The models' ceiling is 15s (kie enforces it
# per model); this is the narrower band the practice actually lives in, and it is
# advisory — plan() warns past it and generates anyway.
SHOT_SECONDS = (5, 10)


class CinematicError(RuntimeError):
    """A cinematic operation failed in a way the caller should surface."""


# ---------------------------------------------------------------------------
# STYLE — "cutscenes in whatever style", and the three levers that deliver it
# ---------------------------------------------------------------------------
#
# WHY THIS IS NOT JUST A STRING ON THE PROMPT. The first cut of this module had
# a `style` column that nothing read: a sequence could be given a look and every
# shot was generated without it. That is worth naming as the failure mode
# because the fix is not "remember to append it" — it is that style has to be
# applied in ONE place, to EVERY shot, automatically, or the sequence drifts
# between beats and the drift is the exact thing a cutscene cannot survive.
#
# THREE LEVERS, IN ASCENDING ORDER OF STRENGTH, and the ordering is the art
# seat's rule 3 rather than an opinion: "THE APPROVED FRAME IS THE STYLE GUIDE,
# NOT YOUR PROSE. 'Detailed pixel art' describes two different drawings."
#
#   1. A PRESET (this table). Vocabulary that is known to move a video model —
#      medium, lighting, lens, grain, motion character. Cheap, coarse, and the
#      right starting point when there is nothing approved yet.
#   2. A STYLE NOTE. The project's own wording, appended to the preset. This is
#      where a game's specific look lives ("faded seaside palette, everything a
#      little sun-bleached") and it is per sequence, not per shot.
#   3. STYLE REFERENCE IMAGES. Actual frames. These beat both of the above and
#      are the only lever that holds a look across eight generations.
#
# THE FOURTH LEVER IS NOT HERE AND SHOULD NOT BE. The art seat can TRAIN a style
# (bgate_core.styles, a Krea LoRA) which moves the look into the model itself.
# No video provider wired here trains anything, so a trained style cannot reach
# a shot — but it CAN generate the keyframes a shot is anchored on, which is the
# honest way a project's trained look reaches its cutscenes today. `style_hint`
# below says so rather than leaving the seat to wonder.
#
# WHAT THE PRESETS DELIBERATELY DO NOT DO is name a studio, a film or a living
# artist. "In the style of <studio>" is the one prompt fragment that is both
# legally fraught and unreliable — models have been tuned away from it — and a
# description of the LOOK is what actually steers. So each entry describes
# medium, light and motion.
STYLES: dict[str, dict] = {
    "live_action": {
        "label": "Live action",
        "prompt": "photoreal live-action cinematography, anamorphic lens, "
                  "natural depth of field, motivated practical lighting, "
                  "subtle film grain, believable weight and inertia in all "
                  "motion",
        "note": "The default a video model is strongest at. Hardest to match to "
                "a stylised game — a photoreal cutscene next to pixel-art "
                "gameplay reads as a different product.",
    },
    "anime": {
        "label": "Anime / cel",
        "prompt": "2D anime cel animation, clean ink linework, flat cel shading "
                  "with hard shadow terminators, painted background art, "
                  "limited-animation timing with held frames and deliberate "
                  "smears",
        "note": "The most reliable stylised look across models. Ask for held "
                "frames explicitly or you get uncanny smooth interpolation.",
    },
    "comic": {
        "label": "Comic / graphic novel",
        "prompt": "inked graphic-novel panel art, heavy black spotting, "
                  "halftone and cross-hatch texture, limited spot-colour "
                  "palette, high-contrast dramatic staging",
        "note": "Hides model artefacts well because the medium is already "
                "high-contrast and textured. Good for prologues and epilogues.",
    },
    "painterly": {
        "label": "Painterly / storybook",
        "prompt": "hand-painted storybook illustration in motion, visible "
                  "brushwork and canvas tooth, soft edges, warm layered "
                  "glazes, gentle parallax rather than literal animation",
        "note": "Forgiving of drift because brushwork is expected to vary. "
                "Pairs well with slow camera moves and badly with fast action.",
    },
    "noir": {
        "label": "Film noir",
        "prompt": "high-contrast black and white film noir, hard single-source "
                  "key light, deep crushed blacks, venetian-blind shadow "
                  "patterns, heavy atmospheric haze, slow deliberate camera",
        "note": "Monochrome removes colour drift between shots entirely, which "
                "makes this the most forgiving style for a long sequence.",
    },
    "pixel": {
        "label": "Pixel art",
        "prompt": "retro pixel art animation, strict limited palette, visible "
                  "square pixels with no anti-aliasing, chunky low-resolution "
                  "sprites, stepped frame-by-frame motion",
        "note": "THE WEAKEST FIT FOR GENERATED VIDEO, and worth saying plainly: "
                "models produce pixel-LOOKING output on a non-integer grid, so "
                "it shimmers next to real pixel art. If the game is pixel art, "
                "an in-engine cutscene is almost always the better answer.",
    },
    "stop_motion": {
        "label": "Stop motion / claymation",
        "prompt": "stop-motion animation with visible handmade materials, clay "
                  "and felt surfaces with fingerprints and seams, shallow "
                  "macro depth of field, slightly stepped frame timing",
        "note": "The stepped timing is what sells it; without it you get smooth "
                "CG that happens to look like clay.",
    },
    "cg_animated": {
        "label": "CG animated feature",
        "prompt": "polished 3D animated feature film look, appealing stylised "
                  "character proportions, soft global illumination, shallow "
                  "depth of field, saturated art-directed colour script",
        "note": "Closest to what a 3D game's own engine renders, so it cuts "
                "into 3D gameplay with the least seam.",
    },
    "watercolor": {
        "label": "Watercolour / ink wash",
        "prompt": "watercolour and ink wash animation, bleeding pigment edges, "
                  "visible paper grain, generous negative space, colour "
                  "pooling at the edges of forms",
        "note": "Very forgiving of frame-to-frame variation — the medium moves "
                "on its own. Poor at fine detail and text.",
    },
    "vhs": {
        "label": "VHS / found footage",
        "prompt": "degraded VHS videotape footage, chromatic bleed, scanlines "
                  "and tracking distortion, blown highlights, timestamp "
                  "overlay, handheld camera drift",
        "note": "The degradation hides model artefacts almost completely, which "
                "makes it the cheapest convincing style available.",
    },
    "silhouette": {
        "label": "Silhouette",
        "prompt": "pure silhouette staging, black featureless foreground forms "
                  "against a luminous graded backdrop, volumetric haze, "
                  "readable poses carrying all the storytelling",
        "note": "No faces means no identity drift, so this is the one style "
                "that survives being unanchored. Excellent for a prologue.",
    },
}

# What the prompt says when a sequence names no style at all. NOT an empty
# string: a model with no stylistic instruction defaults to its own house look,
# which differs per model and per version, so an unstyled sequence is one whose
# appearance nobody chose and nobody can reproduce.
STYLE_FALLBACK = "live_action"


def styles() -> dict:
    """The preset table as data, for a form, a tool description or an agent."""
    return {key: {"key": key, **spec} for key, spec in STYLES.items()}


def resolve_style(style: str = "", note: str = "") -> dict:
    """One sequence's look, as the text every shot of it will carry.

    ``style`` is a preset key OR free prose — "whatever style" has to mean
    whatever style, and refusing an unlisted word would make the preset table a
    cage rather than a shortcut. A key that is not in the table is therefore
    treated as prose, and ``matched`` says which happened so a caller can tell a
    typo ('anmie') from a deliberate description.
    """
    key = str(style or "").strip().lower().replace("-", "_").replace(" ", "_")
    preset = STYLES.get(key)
    parts, matched = [], ""
    if preset:
        matched = key
        parts.append(preset["prompt"])
    elif str(style or "").strip():
        parts.append(str(style).strip())
    if str(note or "").strip():
        parts.append(str(note).strip())
    if not parts:
        matched = STYLE_FALLBACK
        parts.append(STYLES[STYLE_FALLBACK]["prompt"])
    return {"matched": matched, "text": ". ".join(parts),
            "is_preset": bool(preset),
            "label": preset["label"] if preset else "custom",
            "note": preset["note"] if preset else ""}


# ---------------------------------------------------------------------------
# What can be asked for
# ---------------------------------------------------------------------------

def options(root: str | os.PathLike[str]) -> dict:
    """The whole surface as data: models, limits, and what is missing.

    Every number comes from the adapter's own tables or from a live probe. A form
    that retypes "4 to 15 seconds" is a form that lies the day kie changes it.
    """
    from bgate_adapters import kie

    got = dict(kie.available(root))
    encoder = ffmpeg_status()
    return {
        "available": bool(got.get("available")) and bool(encoder["ok"]),
        "provider_available": bool(got.get("available")),
        "reason": got.get("reason", "") or ("" if encoder["ok"] else encoder["reason"]),
        "provider": "kie",
        # IN INTENT TERMS, not each model's own field names — a caller planning a
        # sequence needs to know how many seconds it may ask for, and that is a
        # different question from what this model calls the parameter.
        "models": {name: kie.video_capabilities(name)
                   for name in kie.VIDEO_MODELS},
        "raw_models": {name: spec for name, spec in kie.MODELS.items()
                       if spec["kind"] == "video"},
        "intent": list(kie.VIDEO_INTENT),
        "default_model": kie.DEFAULT_VIDEO_MODEL,
        # THE STYLE SURFACE, served as data for the same reason the model limits
        # are: a form or a brief that retypes the preset list is one that drifts
        # from the table the generation actually uses.
        "styles": styles(),
        "style_fallback": STYLE_FALLBACK,
        "style_levers": [
            "preset — cinematic.STYLES, coarse and cheap",
            "style_note — the project's own wording, appended to the preset",
            "style_refs — actual frames, which beat both and are the only lever "
            "that holds a look across eight generations",
        ],
        "style_hint":
            "No video provider here can be TRAINED on a project's look the way "
            "the art seat trains a Krea style. The way a trained style reaches "
            "a cutscene is by generating this sequence's keyframes through the "
            "art path first, then anchoring every shot on those approved "
            "frames.",
        "install_dir": _install_dir(root, create=False).as_posix(),
        "candidate_dir": CANDIDATE_DIR.as_posix(),
        "shot_seconds": list(SHOT_SECONDS),
        "encoder": encoder,
        "engine_format": {
            "suffix": ENGINE_SUFFIX,
            "codec": "Ogg Theora (video) + Ogg Vorbis (audio)",
            "why": "Godot's VideoStreamPlayer supports Ogg Theora and nothing "
                   "else in core. H.264/H.265 cannot be shipped in the engine "
                   "(software patents) and WebM was removed in 4.0, so the .mp4 "
                   "every model returns is unplayable and keeping a shot "
                   "transcodes it.",
        },
        # Stated because it is the single most expensive thing to learn late.
        "audio_default": False,
        "audio_note": "generated audio is baked into the clip and cannot be "
                      "separated afterwards, so it is off by default and the "
                      "audio seat scores the cutscene over the top.",
    }


def ffmpeg_status() -> dict:
    """Is there an encoder, and can it actually make a Theora file?

    TWO QUESTIONS, NOT ONE, and the second is the one that bites. ffmpeg on PATH
    is necessary and not sufficient: libtheora and libvorbis are OPTIONAL build
    flags, several distributions and at least one popular Windows build ship
    without them, and such a build fails at the encode with "Unknown encoder
    'libtheora'" after the whole clip has been paid for and generated. Asking
    `-encoders` costs milliseconds and moves that discovery to before the spend.
    """
    exe = shutil.which("ffmpeg")
    if not exe:
        return {"ok": False, "ffmpeg": "", "theora": False, "probed": True,
                "reason": "ffmpeg not found on PATH — it is what converts a "
                          "generated .mp4 into the Ogg Theora the engine can "
                          "play, so shots can be generated and none can be kept"}
    try:
        proc = subprocess.run([exe, "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
        listed = (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        # `probed: False` IS THE POINT, and it is not the same fact as
        # theora: False. "This ffmpeg has no libtheora" and "I could not ask
        # this ffmpeg anything" are different states, and a caller that folds
        # them together tells a user with a perfectly good encoder that their
        # build cannot ship cutscenes. Callers that report to a human must say
        # "unknown" here; keep() still refuses, because an encoder that will not
        # run cannot encode either way.
        return {"ok": False, "ffmpeg": exe, "theora": False, "probed": False,
                "reason": f"ffmpeg found at {exe} but would not run: {exc}"}
    theora = "libtheora" in listed
    return {
        "ok": theora,
        "ffmpeg": exe,
        "theora": theora,
        "probed": True,
        "vorbis": "libvorbis" in listed,
        "reason": "" if theora else
                  f"the ffmpeg at {exe} was built without libtheora, so it "
                  "cannot write the only format Godot plays. Install a full "
                  "build (gyan.dev 'full' on Windows, ffmpeg from your "
                  "distribution's multimedia repo on Linux).",
    }


# ---------------------------------------------------------------------------
# The shot list
# ---------------------------------------------------------------------------

def plan(root: str | os.PathLike[str], name: str, shots: list, *,
         logline: str = "", style: str = "", style_note: str = "",
         style_refs: Optional[list] = None, model: str = "",
         aspect_ratio: str = "16:9", resolution: str = "720p",
         audio_track: str = "", audio_gain_db: float = 0.0,
         fade_in: float = 0.0, fade_out: float = 0.0,
         work_item_id: Optional[int] = None) -> dict:
    """Write (or rewrite) a sequence's shot list. Costs nothing, spends nothing.

    ``shots`` is a list of dicts: ``action`` is required, and ``camera``,
    ``dialogue``, ``duration``, ``first_frame``, ``last_frame`` and ``refs`` are
    optional. Order in the list is order in the cut.

    THE PLAN IS SEPARATE FROM THE SPEND ON PURPOSE, and this is the cheapest
    thing in the module for the same reason it is the most important. A sequence
    is the only artifact here that can be reviewed for free: eight shots is eight
    paid generations, an argument about whether shot 3 is needed costs nothing
    before they are bought and costs a generation afterwards. So the director
    reads a shot list, not a folder of clips.

    ``style`` is a preset key (cinematic.STYLES — anime, noir, pixel, vhs, …) OR
    free prose, because "whatever style" has to mean whatever style. ``style_note``
    is the project's own wording appended to it, and ``style_refs`` are frames
    that carry the look, which beat both. All three are applied to EVERY shot,
    automatically, in :func:`prompt_for` — the one place, because a style applied
    per shot by hand is a style that goes missing on shot 6 and a sequence that
    changes look halfway through.

    REPLANNING PRESERVES WHAT WAS ALREADY BOUGHT. A shot whose action text is
    unchanged keeps its artifact, its status and its task id, so re-running plan()
    to fix a typo in shot 7 does not throw away the five shots already generated
    and paid for. A shot whose action CHANGED is reset to planned, because the
    clip on disk is no longer a rendering of what the list now says.

    CHANGING THE STYLE OR THE MODEL RESETS EVERY GENERATED SHOT, and that is not
    over-caution — it is the same rule one level up. A clip bought under the old
    look is not a rendering of the new one, so carrying it forward would leave a
    sequence that is half noir and half anime with nothing saying so, and the
    seam would only be found by watching the assembled cut. The reset is reported
    in ``restyled`` with the count, because it means real money has to be spent
    again and that must never be silent.
    """
    root = str(root)
    stem = slugify(name)
    if not stem:
        raise CinematicError("a sequence needs a name")
    if not shots:
        raise CinematicError("a sequence with no shots is not a plan")

    cleaned, warnings = _clean_shots(shots)

    # The model is validated here, at planning time, because the shot list is
    # written against ITS limits — a 15-second shot is legal on one model and
    # not on another, and discovering that at the first generation means the
    # whole list has to be rewritten after money has already moved.
    chosen = _resolve_model(model)
    look = resolve_style(style, style_note)
    refs_out, missing_refs = _style_refs(root, style_refs or [])
    warnings.extend(_style_warnings(look, refs_out, missing_refs, cleaned))

    with db.tx(root) as conn:
        # WHAT THE OLD SEQUENCE LOOKED LIKE, read BEFORE the upsert overwrites
        # it. A clip bought under a different look or a different model is not a
        # rendering of what this list now says, so it cannot be carried.
        prior = conn.execute(
            "SELECT style, style_note, style_refs_json, model "
            "FROM cine_sequence WHERE name = ?", (stem,)).fetchone()
        restyled = bool(prior) and (
            resolve_style(prior["style"], prior["style_note"])["text"] != look["text"]
            or json.loads(prior["style_refs_json"] or "[]") != refs_out
            or (prior["model"] or "") != chosen)

        conn.execute(
            """
            INSERT INTO cine_sequence (name, logline, style, style_note,
                                       style_refs_json, model, aspect_ratio,
                                       resolution, audio_track, audio_gain_db,
                                       fade_in, fade_out, work_item_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                logline = excluded.logline, style = excluded.style,
                style_note = excluded.style_note,
                style_refs_json = excluded.style_refs_json,
                model = excluded.model,
                aspect_ratio = excluded.aspect_ratio,
                resolution = excluded.resolution,
                audio_track = excluded.audio_track,
                audio_gain_db = excluded.audio_gain_db,
                fade_in = excluded.fade_in, fade_out = excluded.fade_out,
                updated_at = datetime('now')
            """,
            (stem, logline.strip(), style.strip(), style_note.strip(),
             json.dumps(refs_out), chosen, aspect_ratio, resolution,
             audio_track.strip(), float(audio_gain_db), float(fade_in),
             float(fade_out), work_item_id))
        seq_id = int(conn.execute(
            "SELECT id FROM cine_sequence WHERE name = ?", (stem,)).fetchone()[0])

        # What survives a replan, keyed by the text that defines the shot.
        previous = {}
        for row in conn.execute(
                "SELECT * FROM cine_shot WHERE sequence_id = ?", (seq_id,)):
            previous[(row["action"] or "").strip()] = dict(row)
        conn.execute("DELETE FROM cine_shot WHERE sequence_id = ?", (seq_id,))

        kept_rows, dropped = 0, 0
        for index, shot in enumerate(cleaned, start=1):
            carried = previous.get(shot["action"])
            if carried and restyled:
                # The text matches; the look it was rendered in does not.
                if carried["status"] in ("generated", "kept"):
                    dropped += 1
                carried = None
            if carried:
                kept_rows += 1
            conn.execute(
                """
                INSERT INTO cine_shot (sequence_id, idx, slug, action, camera,
                                       dialogue, duration, first_frame,
                                       last_frame, refs_json, transition,
                                       transition_s, vo, artifact_id,
                                       task_id, status, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (seq_id, index, shot["slug"], shot["action"], shot["camera"],
                 shot["dialogue"], shot["duration"], shot["first_frame"],
                 shot["last_frame"], json.dumps(shot["refs"]),
                 shot["transition"], shot["transition_s"], shot["vo"],
                 carried["artifact_id"] if carried else None,
                 carried["task_id"] if carried else "",
                 carried["status"] if carried else "planned",
                 carried["note"] if carried else ""))

    activity.log(root, "cinematic",
                 f"planned {stem}: {len(cleaned)} shot(s), "
                 f"~{sum(s['duration'] for s in cleaned)}s",
                 seat="cinematic", ref=stem)
    out = sequence(root, stem)
    if warnings:
        out["warnings"] = warnings
    if dropped:
        # NEVER SILENT. This means shots that were paid for have to be bought
        # again, which is the one consequence of a re-plan that costs money.
        out["restyled"] = {
            "shots": dropped,
            "note": f"the style or model changed, so {dropped} already-generated "
                    "shot(s) were reset to planned — a clip rendered in the old "
                    "look is not a rendering of the new one. Re-generating them "
                    "costs money again."}
    if kept_rows:
        out["carried"] = {
            "shots": kept_rows,
            "note": f"{kept_rows} shot(s) had unchanged action text and kept "
                    "the clip already generated for them — only changed shots "
                    "were reset to planned"}
    return out


def _clean_shots(shots: list) -> tuple[list[dict], list[str]]:
    """Validate a shot list, and say what is unwise rather than refusing it."""
    from bgate_adapters import kie

    lo, hi = kie.MODELS[kie.DEFAULT_VIDEO_MODEL]["ranges"]["duration"]
    cleaned, warnings, used = [], [], set()
    for index, raw in enumerate(shots, start=1):
        if not isinstance(raw, dict):
            raise CinematicError(f"shot {index} is {type(raw).__name__}, not an "
                                 "object with an 'action'")
        action = str(raw.get("action") or "").strip()
        if not action:
            raise CinematicError(f"shot {index} has no action — a shot with "
                                 "nothing happening in it is a cut, not a shot")
        duration = int(raw.get("duration") or SHOT_SECONDS[0])
        if not lo <= duration <= hi:
            raise CinematicError(
                f"shot {index} asks for {duration}s; "
                f"{kie.DEFAULT_VIDEO_MODEL} generates {lo}..{hi}s. A longer "
                "beat is two shots.")
        if duration > SHOT_SECONDS[1]:
            warnings.append(
                f"shot {index} is {duration}s. The models hold together to "
                f"{hi}s and hold TOGETHER WELL to about {SHOT_SECONDS[1]}s — "
                "past that expect drift in whatever the shot is holding still. "
                "Generated anyway.")
        refs = [str(r) for r in (raw.get("refs") or []) if str(r).strip()]
        transition = str(raw.get("transition")
                         or cinecut.DEFAULT_TRANSITION).strip().lower()
        if transition not in cinecut.TRANSITIONS:
            raise CinematicError(
                f"shot {index} asks for a {transition!r} transition; known: "
                f"{sorted(cinecut.TRANSITIONS)}")
        # A HANDLE LONGER THAN THE SHOT EATS THE SHOT. xfade's offset goes
        # negative and ffmpeg still exits 0, having dropped a beat — so this is
        # clamped here rather than discovered by watching the cut.
        hold = float(raw.get("transition_s") or 0.5)
        if transition != "cut" and hold >= duration:
            raise CinematicError(
                f"shot {index} is {duration}s with a {hold}s {transition} — a "
                "transition cannot be as long as the shot it joins. Shorten the "
                "handle or lengthen the shot.")
        cleaned.append({
            "slug": _unique_slug(raw.get("slug"), index, used),
            "action": action,
            "transition": transition,
            "transition_s": hold if transition != "cut" else 0.0,
            "vo": str(raw.get("vo") or "").strip(),
            "camera": str(raw.get("camera") or "").strip(),
            "dialogue": str(raw.get("dialogue") or "").strip(),
            "duration": duration,
            "first_frame": str(raw.get("first_frame") or "").strip(),
            "last_frame": str(raw.get("last_frame") or "").strip(),
            "refs": refs,
        })
    if not any(s["first_frame"] or s["refs"] for s in cleaned):
        warnings.append(
            "NOT ONE SHOT IS ANCHORED. Every shot is text-only, so the model "
            "invents the cast fresh each time and no two shots will agree on "
            "what anyone looks like. Give each shot a first_frame (a still this "
            "project generated and approved) or refs naming pinned references.")
    return cleaned, warnings


def _resolve_model(model: str = "") -> str:
    """A registered video model name, or a refusal that lists the real ones.

    Validated at PLAN time as well as at generate time, because the shot list is
    written against this model's limits: seconds ranges differ per model, so a
    list that is legal on one is illegal on another, and finding that out at the
    first generation means rewriting the list after money has moved.
    """
    from bgate_adapters import kie

    chosen = str(model or "").strip() or kie.DEFAULT_VIDEO_MODEL
    if chosen not in kie.VIDEO_MODELS:
        raise CinematicError(
            f"{chosen!r} is not a registered video model — known: "
            f"{sorted(kie.VIDEO_MODELS)}. kie serves more than these; a model "
            "whose reference page you have read can be added with "
            "cinematic_register_model without waiting for a release.")
    return chosen


def _style_refs(root: str | os.PathLike[str], given: list) -> tuple[list, list]:
    """Style reference paths, split into the ones on disk and the ones missing.

    Missing ones are REPORTED rather than raised, because plan() must stay free
    and non-refusing: a shot list written before the keyframes exist is the
    normal order of work, and refusing it would force a human to generate art
    before they are allowed to write down what the scene is.
    """
    base = Path(root)
    good, missing = [], []
    for one in given:
        text = str(one or "").strip()
        if not text:
            continue
        if text.startswith(("http://", "https://")) or (base / text).is_file():
            good.append(text)
        else:
            missing.append(text)
    return good, missing


def _style_warnings(look: dict, refs: list, missing: list,
                    shots: list) -> list[str]:
    """Everything true and unwise about this sequence's look, said once.

    These are warnings and not refusals throughout. A style is a judgement call
    and the seat is allowed to make it — what it is not allowed to do is make it
    without being told what this table knows.
    """
    out = []
    if missing:
        out.append(
            f"style reference(s) not on disk and therefore not applied: "
            f"{missing}. The sequence will be generated on prose alone, which "
            "is the weakest of the three levers.")
    if look["note"]:
        out.append(f"{look['label']}: {look['note']}")
    if not look["is_preset"] and look["matched"] == STYLE_FALLBACK:
        out.append(
            "NO STYLE WAS NAMED, so this sequence falls back to "
            f"{STYLES[STYLE_FALLBACK]['label'].lower()}. That is a real choice "
            "being made by default — a model given no stylistic instruction "
            "uses its own house look, which differs per model and per version, "
            "so an unstyled sequence is one whose appearance nobody chose and "
            "nobody can reproduce.")
    if refs:
        # The art seat's rule 4, and it bites harder here: a video model gets
        # ONE reference array, and every frame in it competes.
        identity = sum(len(s.get("refs") or []) for s in shots)
        if identity:
            out.append(
                f"this sequence mixes {len(refs)} style reference(s) with "
                f"{identity} identity reference(s) in one array. A STYLE "
                "REFERENCE AND AN IDENTITY REFERENCE CANNOT SHARE A WEIGHT — at "
                "equal strength the style ref transfers the SUBJECT and the "
                "whole cast comes back as one person. If faces start "
                "converging, drop the style refs and carry the look in prose.")
    return out


def _unique_slug(given: Any, index: int, used: set) -> str:
    """A slug that is unique within the sequence. It names a FILE, so a
    collision is not cosmetic — it is a shot overwriting another shot's clip.

    TWO WAYS TO COLLIDE, AND THE FIRST ONE SHIPPED. ``slugify("")`` returns
    "unnamed", which is TRUTHY, so ``slugify(x) or f"shot{i}"`` gave every
    unnamed shot in a sequence the slug "unnamed" — one logical name, one
    candidate path, and shot 2's generation silently overwriting the clip shot 1
    had just been paid for. Nothing errored; the sequence simply came back with
    every beat looking like the last one generated. Caught by
    tests/test_cinematic.py::test_mismatched_sizes_refuse_rather_than_join_badly,
    which could not see two different sizes because there was only ever one file.

    The second is a human writing the same slug twice — two shots called "wide"
    is an ordinary authoring slip with the identical catastrophic result — so the
    index is appended rather than the duplicate being refused. A shot list is not
    worth rejecting over a name.
    """
    text = str(given or "").strip()
    slug = slugify(text) if text else ""
    if not slug or slug == "unnamed":
        slug = f"shot{index:02d}"
    if slug in used:
        slug = f"{slug}-{index:02d}"
    used.add(slug)
    return slug


def sequence(root: str | os.PathLike[str], name: str) -> dict:
    """One sequence with its shots, in cut order."""
    conn = db.connect(root)
    row = conn.execute("SELECT * FROM cine_sequence WHERE name = ?",
                       (slugify(name),)).fetchone()
    if row is None:
        known = [r["name"] for r in rows(conn.execute(
            "SELECT name FROM cine_sequence ORDER BY id DESC LIMIT 20"))]
        raise CinematicError(f"no sequence named {name!r}"
                             + (f" — known: {known}" if known else
                                " — cinematic_plan writes one"))
    out = dict(row)
    # The look, resolved once and handed to every shot, so a card and a
    # generation cannot disagree about what this sequence is being rendered in.
    out["style_refs"] = json.loads(out.pop("style_refs_json", "") or "[]")
    look = resolve_style(out.get("style", ""), out.get("style_note", ""))
    out["style_resolved"] = look
    out["model"] = out.get("model") or ""
    out["shots"] = [_shot_view(root, dict(s), look["text"])
                    for s in conn.execute(
        "SELECT * FROM cine_shot WHERE sequence_id = ? ORDER BY idx",
        (out["id"],))]
    out["runtime_s"] = sum(s["duration"] for s in out["shots"])
    out["generated"] = sum(1 for s in out["shots"]
                           if s["status"] in ("generated", "kept"))
    out["kept"] = sum(1 for s in out["shots"] if s["status"] == "kept")
    # A sequence whose every shot is cut is not ready — it is empty, and
    # assemble() refuses it. `all()` over an empty selection is True, which
    # would have reported an empty sequence as ready to cut.
    usable = [s for s in out["shots"] if s["status"] != "cut"]
    out["ready_to_assemble"] = bool(usable) and all(
        s["status"] == "kept" for s in usable)
    return out


def sequences(root: str | os.PathLike[str], limit: int = 100) -> list[dict]:
    """Every sequence, newest first, with counts but not full shot lists."""
    conn = db.connect(root)
    out = []
    for row in rows(conn.execute(
            "SELECT * FROM cine_sequence ORDER BY id DESC LIMIT ?", (limit,))):
        counts = conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(status = 'kept') AS kept, SUM(duration) AS secs "
            "FROM cine_shot WHERE sequence_id = ?", (row["id"],)).fetchone()
        row["style_refs"] = json.loads(row.pop("style_refs_json", "") or "[]")
        row["style_label"] = resolve_style(row.get("style", ""),
                                           row.get("style_note", ""))["label"]
        out.append({**row, "shot_count": counts["n"] or 0,
                    "kept": counts["kept"] or 0,
                    "runtime_s": counts["secs"] or 0})
    return out


def _shot_view(root: str | os.PathLike[str], shot: dict,
               style: str = "") -> dict:
    """One shot row, plus whatever is true of the clip it points at.

    ``prompt`` is the FULL text this shot would be generated with, style
    included — not the action alone. A card that shows a prompt shorter than the
    one that gets sent is a card that hides where the look comes from, and the
    review of a shot list is the only cheap review there is.
    """
    shot["refs"] = json.loads(shot.get("refs_json") or "[]")
    shot.pop("refs_json", None)
    shot["prompt"] = prompt_for(shot, style)
    art_id = shot.get("artifact_id")
    if art_id:
        try:
            art = artifacts.get(root, int(art_id))
        except Exception:                                        # noqa: BLE001
            shot["artifact"] = None
            return shot
        install = (art.get("metadata") or {}).get("install") or {}
        shot["artifact"] = {
            "id": art["id"], "logical_name": art["logical_name"],
            "revision": art["revision"], "path": art["path"],
            "status": art["status"],
            "installed": bool(install.get("path")),
            "installed_path": install.get("path", ""),
            "godot_res": install.get("godot_res", ""),
        }
    else:
        shot["artifact"] = None
    return shot


def prompt_for(shot: dict, style: str = "") -> str:
    """The stored fields joined into what the model is actually sent.

    ONE PLACE, so that a re-generation cannot drift from the original by
    assembling the parts in a different order — and so that STYLE cannot be
    forgotten on a shot. Every caller that builds a prompt for a shot comes
    through here; there is deliberately no second path.

    THE ORDER IS LOAD-BEARING, and it is not the order a human would write.
      * CAMERA FIRST. A video model reads the opening of a prompt as the framing
        and the rest as content; a camera instruction buried after two sentences
        of action gets averaged away.
      * ACTION SECOND. The thing that happens.
      * DIALOGUE QUOTED. An unquoted line reads as narration and comes back as a
        character silently doing the thing the line says.
      * STYLE LAST, AND ALWAYS. Trailing style text applies to the whole prompt
        rather than modifying the noun it happens to sit next to — "a cel-shaded
        knight in a ruined hall" styles the knight, and the hall comes back
        photoreal. It also means the style is a constant across every shot of a
        sequence, in the same position, which is what keeps the beats matching.
    """
    parts = []
    if shot.get("camera"):
        parts.append(str(shot["camera"]).strip().rstrip("."))
    parts.append(str(shot.get("action") or "").strip())
    if shot.get("dialogue"):
        parts.append(f'The character says: "{str(shot["dialogue"]).strip()}"')
    if str(style or "").strip():
        parts.append(str(style).strip())
    return ". ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Generating
# ---------------------------------------------------------------------------

def generate_shot(root: str | os.PathLike[str], name: str, idx: int, *,
                  model: str = "", generate_audio: bool = False,
                  timeout: float = 1800.0, on_progress: Any = None,
                  work_item_id: Optional[int] = None) -> dict:
    """Buy one shot of a sequence and register it as a candidate revision.

    THE UNIT IS ONE SHOT, and there is deliberately no generate_all(). A sequence
    is several minutes and several dollars of generation per run; a single call
    that spends the lot has no place to stop when shot 2 comes back wrong, and
    the thing a human must do between shots — LOOK at the clip — is exactly what
    a loop is built to skip. Callers that want the whole sequence iterate and
    judge, which is the pace the work actually goes at.

    THE MODEL AND THE LOOK COME FROM THE SEQUENCE, not from this call. ``model``
    is an override for a deliberate one-shot experiment and it is recorded on the
    revision, but the default is what the sequence was planned with — a cutscene
    generated half on one model or in half a style does not cut together, and the
    seam lands mid-scene where only a full watch would find it.

    The conditioning frames are uploaded to the provider by the adapter and the
    minted URLs are recorded as provenance with the day they die.
    """
    from bgate_adapters import kie

    root = str(root)
    seq = sequence(root, name)
    shot = _shot_at(seq, idx)

    refusal = _budget_refusal(root)
    if refusal:
        return {"ok": False, "error": refusal, "stage": "spend_gate",
                "sequence": seq["name"], "idx": idx}

    # The encoder is checked HERE, before the spend, and not at keep() where it
    # is used. A project with no libtheora can generate every shot of a sequence,
    # pay for all of them, and discover at the first keep that none can reach the
    # game. That discovery belongs before the first dollar.
    encoder = ffmpeg_status()
    if not encoder["ok"]:
        return {"ok": False, "stage": "encoder",
                "error": f"{encoder['reason']} — refusing to generate a shot "
                         "that could not be delivered to the engine afterwards. "
                         "Fix the encoder first; nothing has been charged.",
                "encoder": encoder, "sequence": seq["name"], "idx": idx}

    frames = keyframes_for(root, shot, style_refs=seq.get("style_refs"))
    if frames["missing"]:
        return {"ok": False, "stage": "anchors",
                "error": "conditioning frames named by this shot are not on "
                         f"disk: {frames['missing']}. Nothing has been charged.",
                "sequence": seq["name"], "idx": idx}

    # Intent is checked against THIS model before anything is submitted, so a
    # sequence planned at 12 seconds against a model that caps at 8 is refused
    # here rather than at the provider, after the upload and before the refund
    # that does not exist.
    chosen = str(model or "").strip() or seq["model"] or kie.DEFAULT_VIDEO_MODEL
    wanted = {
        "seconds": int(shot["duration"]),
        "quality": seq["resolution"],
        "shape": seq["aspect_ratio"],
        "first_frame": frames["first"] or None,
        "last_frame": frames["last"] or None,
        "refs": frames["refs"] or None,
        # BOOL, NOT None-WHEN-FALSE. Seedance's `generate_audio` defaults to
        # TRUE upstream, so "off" is a thing that has to be SAID; omitting it
        # would hand back a clip with a baked-in audio bed this module
        # documents as being off by default.
        "audio": bool(generate_audio),
    }
    intent, dropped, refusal = _fit_intent(chosen, wanted)
    if refusal:
        return {"ok": False, "stage": "model", "model": chosen,
                "sequence": seq["name"], "idx": idx, "error": refusal}
    try:
        kie.video_input(chosen, **intent)
    except Exception as exc:                                     # noqa: BLE001
        return {"ok": False, "stage": "model",
                "error": f"{chosen} cannot generate this shot as planned: "
                         f"{exc}. Nothing has been charged.",
                "model": chosen, "sequence": seq["name"], "idx": idx}

    stem = f"{seq['name']}_{shot['slug']}"
    out_dir = Path(root) / CANDIDATE_DIR / seq["name"]
    out_dir.mkdir(parents=True, exist_ok=True)
    revision = 1 + len(artifacts.list_revisions(root, logical_name=stem,
                                                limit=500))
    out_path = out_dir / f"{stem}_r{revision}.mp4"

    _set_shot(root, shot["id"], status="generating")
    if on_progress:
        on_progress(0.05, f"generating shot {idx} of {len(seq['shots'])}", "")

    result = kie.generate_video(
        prompt_for(shot, seq["style_resolved"]["text"]), str(out_path),
        model=chosen, root=root, logical_name=stem,
        work_item_id=work_item_id, timeout=float(timeout), **intent)

    if not result.get("ok"):
        _set_shot(root, shot["id"], status="failed",
                  task_id=str(result.get("task_id") or ""),
                  note=str(result.get("error") or "")[:400])
        return {**result, "sequence": seq["name"], "idx": idx}

    artifact = _register(root, stem, shot, seq, result,
                         work_item_id=work_item_id)
    _set_shot(root, shot["id"], status="generated",
              artifact_id=artifact["id"],
              task_id=str(result.get("task_id") or ""), note="")
    activity.log(root, "cinematic",
                 f"generated {seq['name']} shot {idx} ({shot['duration']}s) "
                 f"-> r{artifact['revision']}",
                 seat="cinematic", ref=str(artifact["id"]))
    if on_progress:
        on_progress(1.0, "shot generated — watch it before keeping it", "")
    out = {"ok": True, "sequence": seq["name"], "idx": idx, "model": chosen,
           "artifact_id": artifact["id"], "path": artifact["path"],
           "revision": artifact["revision"],
           "uploads": result.get("uploads") or [],
           "credits_consumed": result.get("credits_consumed"),
           "seconds": result.get("seconds"),
           "consumes": "WATCH IT. Then cinematic_keep to transcode it into the "
                       "engine project, or re-generate this shot — a shot "
                       "nobody looked at is a shot nobody judged."}
    if dropped:
        # NEVER SILENT. This model has no knob for these, so the clip arrives
        # framed or sharpened in a way nobody chose — which is worth knowing
        # before it is cut against seven shots that were controlled.
        out["unsupported"] = {
            "dropped": dropped,
            "note": f"{chosen} has no parameter for {', '.join(dropped)}, so "
                    "the sequence's setting could not be applied and the model "
                    "used its own. The clip is still usable; check it matches "
                    "the other shots before keeping it."}
    return out


# WHICH INTENTS A MODEL IS ALLOWED TO LACK, and this split is a real decision
# rather than a convenience — getting it wrong in either direction is expensive.
#
# `video_input` refuses any intent a model has no field for, and that rule is
# right at ITS layer: a parameter silently dropped is one you paid for and did
# not get. But this layer sends SEVEN intents on every shot, most of them carried
# from the sequence's defaults rather than asked for, and refusing all seven made
# any model simpler than Seedance unusable. Measured end to end: a second model
# registered, planned and prompted correctly, and every generation died because
# it had no `quality` knob.
#
# So the question is not "did the model have the field" but "does losing it
# change what the caller GETS".
#
#   ADVISORY — the shot is still the shot without it. A model with no quality or
#   shape parameter renders at its own; the picture arrives, framed and sharp in
#   a way nobody chose. Dropped, and REPORTED on the result so it is not silent.
#
#   ESSENTIAL — losing it changes the deliverable, so it refuses BEFORE the
#   spend. `seconds` because the shot list's runtime becomes a lie and the cut
#   will not assemble to the planned length. `first_frame`/`last_frame`/`refs`
#   because an unanchored shot stars a stranger, which is the single most
#   expensive failure in this module. `audio` because a caller who asked for
#   sound and gets silence has a missing deliverable, not a different one.
#
# Note the asymmetry on audio: audio=None (the default — do not generate any) is
# ALREADY TRUE of a model that makes none, so it is simply not sent. Only an
# explicit request refuses.
ADVISORY_INTENT = frozenset({"quality", "shape"})


def _fit_intent(model: str, wanted: dict) -> tuple[dict, list, str]:
    """Trim a shot's intent to what this model can express.

    Returns ``(intent, dropped, refusal)``. A non-empty ``refusal`` means do not
    spend; ``dropped`` names advisory settings this model cannot be told, which
    the caller reports rather than swallows.
    """
    from bgate_adapters import kie

    spec = kie.MODELS.get(model) or {}
    supports = set(spec.get("intent") or {})
    intent, dropped = {}, []
    for name, value in wanted.items():
        if value is None or value == "" or value == []:
            continue
        if name in supports:
            intent[name] = value
            continue
        if name in ADVISORY_INTENT:
            dropped.append(name)
            continue
        if name == "audio":
            # The asymmetry: asking a model to turn OFF audio it cannot make is
            # already satisfied, so it is dropped. Asking it FOR audio it cannot
            # make is a missing deliverable, so it refuses.
            if not value:
                continue
            return {}, dropped, (
                f"{model} cannot generate audio, and this shot asked for it. "
                "Generate the picture here and let the audio seat score it, or "
                "pick a model that makes its own. Nothing has been charged.")
        if name in ("first_frame", "last_frame", "refs"):
            return {}, dropped, (
                f"{model} has no parameter for {name}, so this shot would be "
                "generated with nothing to hold onto — an unanchored shot "
                "invents the cast fresh and will not match the others. Pick a "
                "model that takes reference frames. Nothing has been charged.")
        return {}, dropped, (
            f"{model} has no parameter for {name}, and the shot list is written "
            "against it — generating anyway would give a clip whose length is "
            "not the one the cut was planned around. Nothing has been charged.")
    return intent, dropped, ""


def shot_status(root: str | os.PathLike[str], task_id: str) -> dict:
    """Where a submitted generation got to, at the provider. Costs nothing.

    Reports; :func:`recover_shot` acts. The pair exists here for the same reason
    music has it, and the money at stake is larger: a video job runs for minutes,
    so a dropped connection, a too-short timeout or a killed agent between submit
    and download is not a rare event — and the charge happened at submit.
    """
    from bgate_adapters import kie

    ident = str(task_id or "").strip()
    if not ident:
        raise CinematicError("a task id is needed to check a generation")
    # record(), not poll(): this must LOOK and return, never block. poll() waits
    # for a terminal state and raises on a failed job, which is right for the
    # generation path and wrong for a status call a dashboard hits every few
    # seconds.
    rec = kie.record(ident, root=str(root))
    state = str(rec.get("state") or "").lower()
    urls = kie.result_urls(rec)
    return {
        "ok": True, "task_id": ident, "status": state,
        "done": state == kie.JOB_DONE,
        "running": state in kie.JOB_RUNNING,
        "failed": state in kie.JOB_DEAD,
        "urls": urls,
        "recoverable": bool(urls),
        "note": ("kie is holding a finished clip for this task — recover_shot "
                 "puts it on disk without paying again" if urls else
                 "kie has no finished clip for this task yet"),
    }


def recover_shot(root: str | os.PathLike[str], name: str, idx: int,
                 task_id: str = "", *,
                 work_item_id: Optional[int] = None) -> dict:
    """Download a clip that was already paid for and register it. The repair door.

    THE DOOR THAT HAD TO EXIST, and video needs it more than music did. A
    generation is charged at SUBMIT. Everything after that — the poll loop, the
    download, this process staying alive for the ten minutes it takes — can fail
    while the provider sits on a finished clip that has already been billed. A
    seat whose only option is to press generate again pays twice.

    The task id is read off the shot row when not supplied, which is the whole
    reason it is stored there: an agent that died mid-generation left the id
    behind, and its successor needs no archaeology to find it.

    NO COST IS CLAIMED. The charge happened at submit; a balance delta measured
    now would be meaningless, so credits are reported as unmeasurable rather
    than as zero.
    """
    from bgate_adapters import kie

    root = str(root)
    seq = sequence(root, name)
    shot = _shot_at(seq, idx)
    ident = str(task_id or shot.get("task_id") or "").strip()
    if not ident:
        raise CinematicError(
            f"shot {idx} of {seq['name']} has no task id recorded, so there is "
            "nothing to recover — it was never submitted, or it failed before "
            "the provider accepted it.")

    rec = kie.poll(ident, root=root, timeout=60.0, interval=5.0)
    urls = kie.result_urls(rec)
    if not urls:
        return {"ok": False, "task_id": ident, "sequence": seq["name"],
                "idx": idx,
                "error": f"the provider is holding no finished clip for task "
                         f"{ident}"}

    stem = f"{seq['name']}_{shot['slug']}"
    out_dir = Path(root) / CANDIDATE_DIR / seq["name"]
    out_dir.mkdir(parents=True, exist_ok=True)
    revision = 1 + len(artifacts.list_revisions(root, logical_name=stem,
                                                limit=500))
    out_path = out_dir / f"{stem}_r{revision}_recovered.mp4"
    kie.download(urls[0], out_path, accept="video/*", timeout=600.0)

    artifact = _register(
        root, stem, shot, seq,
        {"path": str(out_path), "model": seq["model"], "task_id": ident,
         "url": urls[0], "uploads": [], "credits_consumed": None,
         "credits_source": "not_measurable_after_the_fact", "accounted": False},
        work_item_id=work_item_id)
    _set_shot(root, shot["id"], status="generated", artifact_id=artifact["id"],
              task_id=ident, note="")
    activity.log(root, "cinematic",
                 f"recovered {seq['name']} shot {idx} from task {ident}",
                 seat="cinematic", ref=str(artifact["id"]))
    return {"ok": True, "recovered": True, "sequence": seq["name"], "idx": idx,
            "task_id": ident, "artifact_id": artifact["id"],
            "path": artifact["path"],
            "note": "this clip was charged when the job was submitted, so no "
                    "cost is recorded against this call."}


def keyframes_for(root: str | os.PathLike[str], shot: dict,
                  style_refs: Optional[list] = None) -> dict:
    """This shot's conditioning frames as things the adapter can send.

    Returns ``{first, last, refs, missing}`` — local paths are handed through as
    paths and the adapter uploads them; anything already a URL passes untouched.

    THE SEQUENCE'S STYLE REFS RIDE IN `refs` ALONGSIDE THE SHOT'S OWN, and they
    go in LAST deliberately. A model reads a reference array in order and weights
    the front of it more heavily; identity is the thing that must not drift, so
    the character frames lead and the look frames follow. This is the mitigation
    for the weight-sharing problem plan() warns about — it does not solve it, and
    nothing at this layer can, which is why the warning exists.

    WHAT THIS DOES NOT DO IS THE POINT. It never reaches into the previous shot's
    video for a frame. See the module docstring: that is the chain the art seat
    banned after measuring it, and a video model's last frame is the worst
    possible link in one.
    """
    base = Path(root)
    missing, out = [], {}
    for field in ("first_frame", "last_frame"):
        value = str(shot.get(field) or "").strip()
        if not value or value.startswith(("http://", "https://")):
            out[field] = value
            continue
        if not (base / value).is_file():
            missing.append(value)
            out[field] = ""
            continue
        out[field] = str(base / value)
    refs = []
    for one in list(shot.get("refs") or []) + list(style_refs or []):
        text = str(one).strip()
        if not text:
            continue
        if text.startswith(("http://", "https://")):
            refs.append(text)
        elif (base / text).is_file():
            refs.append(str(base / text))
        else:
            missing.append(text)
    return {"first": out["first_frame"], "last": out["last_frame"],
            "refs": refs, "missing": missing}


# ---------------------------------------------------------------------------
# Keeping — which for video means transcoding, not copying
# ---------------------------------------------------------------------------

def keep(root: str | os.PathLike[str], artifact_id: int, *, note: str = "",
         quality: int = DEFAULT_QUALITY, install_to_engine: Optional[bool] = None,
         actor: Optional[str] = None) -> dict:
    """Approve a take, and put it in the engine project if the engine loads it.

    WHAT GETS INSTALLED IS KIND-DEPENDENT, and this was wrong in the first
    build. Every kept SHOT was transcoded to Ogg Theora and copied into the game
    — which is a Theora encode per shot and, at 1080p, tens of megabytes each of
    files that NOTHING REFERENCES. The game loads the assembled cut;
    :func:`assemble` builds it from the source .mp4 candidates, not from the
    installed .ogv, so the per-shot transcode was work nobody used and clutter
    nobody asked for. Measured on a three-shot demo: three .ogv intermediates
    beside the one cutscene, invisible until somebody listed the directory.

    So:
      * a CUTSCENE (an assembled cut) is transcoded and installed — that is the
        asset the game plays;
      * a SHOT is approved and stays in .bgate_out, where the gallery previews
        it and assemble reads it.

    ``install_to_engine`` overrides the default in either direction, for the one
    real case it exists for: a single generated clip used on its own, as an
    attract-mode loop or a sting, with no cut around it.

    THE ORDER IS THE POINT when there IS an install, exactly as in music.keep:
    approving first would leave an approval that means nothing when the
    conversion fails — the database saying 'approved' while the game has no
    file. So the transcode happens first and its failure is RAISED.

    :func:`artifacts.review` is what enforces 'only a human may approve'. This
    inherits that gate rather than re-implementing it.
    """
    root = str(root)
    art = artifacts.get(root, int(artifact_id))
    wants = (bool(install_to_engine) if install_to_engine is not None
             else _is_deliverable(art))

    install = {}
    if wants:
        install = _install_file(root, art, quality=quality)
        _stamp(root, int(artifact_id), "install", install)

    reviewed = artifacts.review(
        root, int(artifact_id), "approved",
        note=note or ("kept and transcoded to Ogg Theora for the engine"
                      if wants else
                      "kept as a shot — it stays in .bgate_out until the cut is "
                      "assembled, which is what the game loads"),
        actor=actor)
    _mark_kept(root, int(artifact_id))
    activity.log(root, "cinematic",
                 f"kept {art['logical_name']} r{art['revision']}"
                 + (f" -> {install['path']}" if install else " (shot)"),
                 seat="cinematic", ref=str(artifact_id), actor=actor or "")
    return {"ok": True, "artifact": _view(root, reviewed), "install": install,
            "installed_to_engine": bool(install),
            "note": "" if install else
                    "approved. Shots are not copied into the engine project — "
                    "the cut is what the game loads, and assemble() reads the "
                    "candidates directly. Pass install_to_engine to override."}


def _is_deliverable(art: dict) -> bool:
    """Is this revision the thing the GAME loads, or an intermediate?

    Reads the artifact's own metadata rather than guessing from the filename,
    because `<seq>.ogv` and `<seq>_shot01.mp4` are only distinguishable by
    convention and a convention is what breaks when somebody renames a shot.
    A revision with no kind recorded is treated as deliverable: that is the
    old behaviour, and for anything registered before this distinction existed
    it is the safe direction to be wrong in.
    """
    kind = str((art.get("metadata") or {}).get("kind") or "").strip()
    return kind != "shot"


def install(root: str | os.PathLike[str], artifact_id: int, *,
            quality: int = DEFAULT_QUALITY,
            actor: Optional[str] = None) -> dict:
    """Transcode an already-approved shot into the engine project. The repair verb.

    The same door music.install is, and it exists for the same measured reason: a
    project whose approval gate is OFF has every revision approved inside the
    register call, so there was never a candidate to keep and the keep that
    delivers was unreachable. Idempotent, and it does not change review state.
    """
    root = str(root)
    art = artifacts.get(root, int(artifact_id))
    if art.get("status") == "rejected":
        raise CinematicError(
            f"{art['logical_name']} r{art['revision']} was rejected — "
            "installing it would put a discarded take in the game. keep() it "
            "instead if you have changed your mind.")
    record = _install_file(root, art, quality=quality)
    _stamp(root, int(artifact_id), "install", record)
    _mark_kept(root, int(artifact_id))
    activity.log(root, "cinematic",
                 f"installed {art['logical_name']} r{art['revision']} -> "
                 f"{record['path']}",
                 seat="cinematic", ref=str(artifact_id), actor=actor or "")
    return {"ok": True,
            "artifact": _view(root, artifacts.get(root, int(artifact_id))),
            "install": record}


def discard(root: str | os.PathLike[str], artifact_id: int, *, note: str = "",
            actor: Optional[str] = None) -> dict:
    """Reject a shot. The file stays; the decision is what is recorded."""
    reviewed = artifacts.review(root, int(artifact_id), "rejected",
                                note=note or "discarded from the cinematic "
                                             "shot gallery",
                                actor=actor)
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE cine_shot SET status = 'planned', artifact_id = NULL, "
            "updated_at = datetime('now') WHERE artifact_id = ?",
            (int(artifact_id),))
    return {"ok": True, "artifact": _view(root, reviewed),
            "note": "the clip is left under .bgate_out (gitignored, outside the "
                    "engine project); only the decision was recorded, and the "
                    "shot is back to planned so it can be re-generated"}


def transcode(source: str | os.PathLike[str], destination: str | os.PathLike[str],
              *, quality: int = DEFAULT_QUALITY,
              scale_height: int = 0, timeout: float = 900.0) -> dict:
    """H.264 .mp4 in, Ogg Theora .ogv out. The step that makes a clip playable.

    The flags are Godot's own documented recipe (see THEORA_GOP). ``scale_height``
    downscales when a 1080p source is more than a cutscene needs — the engine docs
    give the same ``scale=-1:720`` for exactly this — and 0 preserves the source.

    RAISES on failure and says what ffmpeg said. A silent best-effort here would
    leave the caller unable to distinguish "converted" from "wrote nothing", which
    is the difference between a cutscene and a black rectangle.
    """
    # THE SOURCE IS CHECKED FIRST, and the order is the diagnostic. Nothing is
    # spent in here, so there is no "fail before the money moves" argument for
    # leading with the encoder — and when the real problem is a missing file,
    # "this ffmpeg has no libtheora" sends the reader to install software they
    # already have. generate_shot still checks the encoder up front, because
    # that one DOES spend.
    src, dst = Path(source), Path(destination)
    if not src.is_file():
        raise CinematicError(f"nothing on disk at {src}")
    encoder = ffmpeg_status()
    if not encoder["ok"]:
        raise CinematicError(encoder["reason"])
    dst.parent.mkdir(parents=True, exist_ok=True)

    cmd = [encoder["ffmpeg"], "-y", "-loglevel", "error", "-i", str(src)]
    if scale_height:
        cmd += ["-vf", f"scale=-1:{int(scale_height)}"]
    cmd += ["-codec:v", "libtheora", "-q:v", str(int(quality)),
            "-g:v", THEORA_GOP]
    # AUDIO IS RE-ENCODED IF PRESENT AND NOT SYNTHESISED IF ABSENT. `-q:a` on a
    # source with no audio stream is harmless; adding a silent track would make
    # every picture-only cutscene carry a Vorbis stream the game never plays.
    cmd += ["-codec:a", "libvorbis", "-q:a", str(int(quality)), str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise CinematicError(
            f"ffmpeg did not finish converting {src.name} within "
            f"{timeout:.0f}s") from exc
    if proc.returncode != 0 or not dst.is_file():
        raise CinematicError(
            f"ffmpeg could not convert {src.name} to Ogg Theora: "
            + ((proc.stderr or "").strip()[-300:] or
               f"exit {proc.returncode} and no output file"))
    return {"path": str(dst), "bytes": dst.stat().st_size,
            "quality": int(quality), "gop": int(THEORA_GOP),
            "scaled_to": int(scale_height) or None}


# ---------------------------------------------------------------------------
# Assembling the cut
# ---------------------------------------------------------------------------

def assemble(root: str | os.PathLike[str], name: str, *,
             quality: int = DEFAULT_QUALITY, actor: Optional[str] = None,
             work_item_id: Optional[int] = None) -> dict:
    """Join a sequence's kept shots, in order, into one .ogv the game loads.

    ONE FILE, NOT A PLAYLIST, and that is a decision rather than a convenience.
    Godot's VideoStreamPlayer plays one stream; chaining N of them at runtime
    means N loads, a visible seam at every cut while the next stream opens, and
    gameplay code that has to know the shot list. A cutscene is one asset.

    A SHOT THAT WAS NEVER KEPT STOPS THIS. Assembling around a missing shot would
    silently ship a cut with a beat missing, and the failure would surface as a
    story that does not make sense rather than as an error. Shots explicitly cut
    (status 'cut') are skipped, because that is a decision somebody recorded.
    """
    root = str(root)
    seq = sequence(root, name)
    usable = [s for s in seq["shots"] if s["status"] != "cut"]
    if not usable:
        raise CinematicError(f"{seq['name']} has no shots that are not cut")
    unkept = [s["idx"] for s in usable if s["status"] != "kept"]
    if unkept:
        raise CinematicError(
            f"shot(s) {unkept} of {seq['name']} have not been kept — assembling "
            "now would ship a cut with those beats missing. Generate and keep "
            "them, or mark them cut if the sequence is meant to be shorter.")

    sources = []
    for shot in usable:
        art = artifacts.get(root, int(shot["artifact_id"]))
        source = Path(root) / art["path"]
        if not source.is_file():
            raise CinematicError(
                f"shot {shot['idx']} of {seq['name']} points at {art['path']}, "
                "which is not on disk — re-generate it before assembling")
        sources.append(source)

    mismatch = _geometry_mismatch(sources)
    if mismatch:
        raise CinematicError(mismatch)

    out_dir = Path(root) / CANDIDATE_DIR / seq["name"]
    out_dir.mkdir(parents=True, exist_ok=True)
    revision = 1 + len(artifacts.list_revisions(root, logical_name=seq["name"],
                                                limit=500))
    out_path = out_dir / f"{seq['name']}_r{revision}{ENGINE_SUFFIX}"

    # 1. THE PICTURE, with whatever transitions the shots ask for. A sequence of
    # hard cuts still takes the cheap concat path — see cinecut.picture_plan.
    picture = cinecut.build_picture(
        sources, usable, out_path, fade_in=float(seq.get("fade_in") or 0),
        fade_out=float(seq.get("fade_out") or 0), quality=quality,
        gop=THEORA_GOP)

    # 2. THE SOUND. Off by default is a real decision (a generated bed is baked
    # in and cannot be separated), but "the audio seat scores it over the top"
    # was documented for months with no path to do it — every cut shipped
    # silent. This is that path: a kept track, or a hand mix, laid under the
    # picture and muxed into the Ogg the engine reads.
    audio: dict = {}
    bed = str(seq.get("audio_track") or "").strip()
    if bed:
        source = Path(root) / bed
        if not source.is_file():
            raise CinematicError(
                f"{seq['name']} names an audio bed at {bed} which is not on "
                "disk. Keep a track in the audio seat first, or clear the bed "
                "— assembling silently without it is how a cutscene ships mute.")
        mixed = out_dir / f"{seq['name']}_r{revision}_scored{ENGINE_SUFFIX}"
        audio = cinecut.mix_audio(
            out_path, mixed, bed=str(source),
            gain_db=float(seq.get("audio_gain_db") or 0),
            fade_in=float(seq.get("fade_in") or 0),
            fade_out=float(seq.get("fade_out") or 0), quality=quality)
        out_path.unlink(missing_ok=True)
        mixed.replace(out_path)
        audio["track"] = bed

    # 3. THE CAPTIONS, timed off the shot list and the transitions between the
    # shots — never stored, because the shot list already answers that question.
    lines = cinecut.captions(usable)
    caption_files = (cinecut.write_captions(root, out_dir, seq["name"], lines)
                     if lines else {})

    artifact = artifacts.register(
        root, seq["name"], out_path, producer=PRODUCER,
        model="ffmpeg/libtheora",
        prompt=seq["logline"] or f"assembled cut of {seq['name']}",
        work_item_id=work_item_id or seq.get("work_item_id"),
        metadata={
            "kind": "cutscene",
            "assembled_from": [{"idx": s["idx"], "slug": s["slug"],
                                "artifact_id": s["artifact_id"],
                                "duration": s["duration"],
                                "transition": s.get("transition", "cut")}
                               for s in usable],
            # MEASURED, not summed. Transitions overlap, so the sum of the shots
            # is longer than the cut, and the arithmetic is what caption timing
            # is built from — recording both is what makes a drift findable.
            "runtime_s": picture.get("measured_s") or picture["planned_s"],
            "planned_runtime_s": picture["planned_s"],
            "transitions": picture["transitions"],
            "filtered": picture["filtered"],
            "aspect_ratio": seq["aspect_ratio"],
            "resolution": seq["resolution"],
            "quality": int(quality),
            "gop": int(THEORA_GOP),
            "audio": audio,
            "captions": caption_files,
            "preview": assets.normalize_path(root, out_path),
        })
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE cine_sequence SET status = 'assembled', "
            "assembled_artifact_id = ?, updated_at = datetime('now') "
            "WHERE id = ?", (artifact["id"], seq["id"]))
    activity.log(root, "cinematic",
                 f"assembled {seq['name']}: {len(usable)} shot(s), "
                 f"{sum(s['duration'] for s in usable)}s -> r{artifact['revision']}",
                 seat="cinematic", ref=str(artifact["id"]), actor=actor or "")
    out = {"ok": True, "sequence": seq["name"],
           "artifact_id": artifact["id"], "revision": artifact["revision"],
           "path": artifacts.get(root, artifact["id"])["path"],
           "shots": len(usable),
           "runtime_s": picture.get("measured_s") or picture["planned_s"],
           "transitions": picture["transitions"],
           "audio": audio or {"note": "NO AUDIO BED — this cut is silent. Pass "
                                      "audio_track on the plan with a kept "
                                      "track from the audio seat."},
           "captions": caption_files or {"lines": 0},
           "consumes": "WATCH THE WHOLE CUT, then cinematic_keep it — the "
                       "individual shots were judged alone and a cut is judged "
                       "as a cut. Keeping installs it under the engine project, "
                       "and cinematic_deliver builds the scene that plays it."}
    for key in ("timing_warning",):
        if picture.get(key):
            out[key] = picture[key]
    if audio.get("note"):
        out["audio_warning"] = audio["note"]
    short = [c for c in lines if c.get("short")]
    if short:
        out["caption_warning"] = (
            f"{len(short)} line(s) are on screen for under "
            f"{cinecut.CAPTION_MIN_S}s and cannot be read at speed — shots "
            f"{[c['idx'] for c in short]} are too short for their dialogue.")
    return out


def check_continuity(root: str | os.PathLike[str], name: str) -> dict:
    """Does this sequence actually cut together? Costs nothing but time.

    THE CHECK THE SEAT WAS TOLD TO DO BY EYE. Its brief says "watch it twice —
    once as a shot, once in the cut", which is correct and unenforceable; the art
    seat has real detectors for a frame and a cut had nothing. This measures the
    two things that are objectively comparable across a join — overall brightness
    and the colour palette — on the ACTUAL FRAMES either side of it.

    It cannot tell you the cutscene is good, and it does not try. A cut from a
    cellar to a snowfield SHOULD jump in brightness; whether a flag is a mistake
    or the point is a human's call, so every finding says what it measured
    rather than passing a verdict.

    Runs on the generated shots, not on the assembled cut, because by the time
    the cut exists a dissolve may already have hidden the join — and the fix for
    a real mismatch is to re-generate a shot, which is a decision to make before
    paying for the assembly.
    """
    root = str(root)
    seq = sequence(root, name)
    usable = [s for s in seq["shots"] if s["status"] != "cut"]
    clips = []
    for shot in usable:
        if not shot.get("artifact_id"):
            continue
        art = artifacts.get(root, int(shot["artifact_id"]))
        source = Path(root) / art["path"]
        if source.is_file():
            clips.append(source)
    if len(clips) < 2:
        return {"ok": True, "sequence": seq["name"], "joins": [],
                "note": "fewer than two generated shots — there is no join to "
                        "check yet"}

    work = Path(root) / CANDIDATE_DIR / seq["name"] / "continuity"
    joins = cinecut.continuity(clips, work_dir=work)
    flagged = [j for j in joins if j.get("flags")]
    return {
        "ok": not flagged,
        "sequence": seq["name"],
        "joins": joins,
        "flagged": len(flagged),
        "note": ("every join is within tolerance on brightness and palette — "
                 "which is not the same as saying the cut is good, only that "
                 "nothing measurable jumps" if not flagged else
                 f"{len(flagged)} join(s) jump. Re-generate the odd shot, or "
                 "soften it with a dissolve — that is what a dissolve is for."),
    }


def deliver(root: str | os.PathLike[str], name: str, *,
            actor: Optional[str] = None, force: bool = False) -> dict:
    """Build the Godot scene that plays this cutscene. The last mile.

    WHY THIS HAS TO EXIST. Keeping a cut installs an .ogv under the engine
    project and prints a res:// path, and that is where this pipeline used to
    stop — which left a designer to hand-author a VideoStreamPlayer, wire a skip
    input, drive the captions, and work out how to hand control back to
    gameplay. Four jobs, each easy to get subtly wrong, on top of an asset that
    already cost real money. music.py gets away with stopping at the file
    because Godot auto-imports audio and an AudioStreamPlayer is one node; a
    cutscene is a scene, a script and a contract with the caller.

    THE CONTRACT IS ONE SIGNAL. `finished(skipped: bool)` fires whether the
    video ended or the player skipped it, because every caller wants the same
    thing next and branching on which is how a skipped cutscene leaves a game
    stuck on a black screen.

    IT REFUSES TO OVERWRITE AN EDITED SCRIPT. The generated .gd is meant to be
    edited — a project will want its own skip input or a letterbox — so a second
    delivery that silently reverted those edits would be this product destroying
    a user's work. `force` is the explicit override, and the scene file is
    rewritten either way because it is derived from paths this module owns.
    """
    root = str(root)
    seq = sequence(root, name)
    art_id = seq.get("assembled_artifact_id")
    if not art_id:
        raise CinematicError(
            f"{seq['name']} has not been assembled yet — cinematic_assemble "
            "joins the kept shots into the file this scene would play.")
    art = artifacts.get(root, int(art_id))
    install = (art.get("metadata") or {}).get("install") or {}
    if not install.get("path"):
        raise CinematicError(
            f"the cut for {seq['name']} has not been kept, so it is not in the "
            "engine project and a scene pointing at it would not load. "
            "cinematic_keep transcodes and installs it first.")

    video_rel = install["path"]
    engine_dir = Path(video_rel).parent
    stem = seq["name"]
    scene_rel = (engine_dir / f"{stem}.tscn").as_posix()
    script_rel = (engine_dir / f"{stem}.gd").as_posix()

    # The captions travel with the cut into the engine project. They live in
    # .bgate_out beside the candidate until now, which is outside the project
    # and unreadable by the game.
    caption_rel = ""
    meta_caps = (art.get("metadata") or {}).get("captions") or {}
    if meta_caps.get("json"):
        source = Path(root) / meta_caps["json"]
        if source.is_file():
            target = Path(root) / engine_dir / f"{stem}_captions.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            caption_rel = assets.normalize_path(root, target)
            # The .srt goes too — it is the file a translator opens, and leaving
            # it in a gitignored scratch directory is how localisation finds out
            # the captions were never versioned.
            srt = Path(root) / meta_caps.get("srt", "")
            if meta_caps.get("srt") and srt.is_file():
                shutil.copy2(srt, Path(root) / engine_dir / f"{stem}.srt")

    video_res = f"res://{_engine_relative(root, video_rel)}"
    script_res = f"res://{_engine_relative(root, script_rel)}"
    captions_res = (f"res://{_engine_relative(root, caption_rel)}"
                    if caption_rel else "")

    script_path = Path(root) / script_rel
    wrote_script = True
    if script_path.is_file() and not force:
        if not _is_generated(script_path):
            wrote_script = False
        else:
            script_path.write_text(_script_text(scene_rel, captions_res),
                                   encoding="utf-8")
    else:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(_script_text(scene_rel, captions_res),
                               encoding="utf-8")

    scene_path = Path(root) / scene_rel
    scene_path.write_text(
        cinecut.cutscene_scene_text(video_res, script_res,
                                    node_name=_node_name(stem)),
        encoding="utf-8")

    for rel in (scene_rel, script_rel):
        try:
            assets.track(root, rel)
        except Exception:                                        # noqa: BLE001
            pass

    activity.log(root, "cinematic",
                 f"delivered {seq['name']} -> {scene_rel}",
                 seat="cinematic", ref=str(art_id), actor=actor or "")
    return {
        "ok": True,
        "sequence": seq["name"],
        "scene": scene_rel,
        "script": script_rel,
        "captions": caption_rel,
        "video": video_rel,
        "scene_res": f"res://{_engine_relative(root, scene_rel)}",
        "script_kept": not wrote_script,
        "usage": (
            "var cut := preload(\"res://"
            f"{_engine_relative(root, scene_rel)}\").instantiate()\n"
            "add_child(cut)\n"
            "await cut.finished"),
        "note": ("the existing script was edited by hand and was NOT "
                 "overwritten — pass force to replace it" if not wrote_script
                 else "the scene and its script were written; edit the .gd "
                      "freely, delivery will not overwrite your changes"),
    }


# The marker that tells a re-delivery whether a human has touched the script.
# A comment rather than a hash sidecar: it survives being copied, it is visible
# to the person deciding whether to edit, and deleting it is a clear way to say
# "this is mine now".
GENERATED_MARK = "# bgate:generated cutscene script"


def _is_generated(path: Path) -> bool:
    try:
        return GENERATED_MARK in path.read_text(encoding="utf-8")[:400]
    except OSError:
        return False


def _script_text(scene_rel: str, captions_res: str) -> str:
    body = cinecut.CUTSCENE_GD.format(
        scene_res=scene_rel, captions_res=captions_res)
    return f"{GENERATED_MARK}\n{body}"


def _node_name(stem: str) -> str:
    """A .tscn node name out of a slug: Godot wants no dots, slashes or colons."""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", stem) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Cutscene"


def _geometry_mismatch(sources: list) -> str:
    """Refuse a concat whose shots are not the same size, and name the odd one.

    The concat demuxer does not scale. Handed a 1080p shot after four 720p ones
    it produces a file whose later frames are garbled or whose stream simply ends
    early, and ffmpeg's exit status is frequently ZERO — so this is a check that
    has to happen here rather than being caught by the return code. A mismatch
    means a sequence's resolution was changed between generations.
    """
    from bgate_adapters import recorder

    seen = {}
    for source in sources:
        probe = recorder.probe_video(str(source))
        if not probe.get("ok"):
            # No ffprobe is not a reason to refuse — the check is a safety net,
            # not a gate, and a machine with ffmpeg but no ffprobe is real.
            return ""
        seen.setdefault((probe.get("width"), probe.get("height")), []).append(
            Path(source).name)
    if len(seen) <= 1:
        return ""
    described = "; ".join(f"{w}x{h}: {', '.join(names)}"
                          for (w, h), names in seen.items())
    return ("these shots are not all the same size, and joining them produces a "
            f"broken file that ffmpeg reports as success — {described}. "
            "Re-generate the odd ones at the sequence's resolution.")


# ---------------------------------------------------------------------------
# Auditioning
# ---------------------------------------------------------------------------

def candidates(root: str | os.PathLike[str], *, logical_name: str = "",
               limit: int = 200) -> list[dict]:
    """Generated clips awaiting a decision, newest first."""
    return [_view(root, row) for row in artifacts.list_revisions(
        root, logical_name=logical_name or None, status="candidate", limit=limit)
        if (row.get("producer") or "") == PRODUCER]


def kept(root: str | os.PathLike[str], *, limit: int = 200) -> list[dict]:
    """Approved clips, with where each was installed — or that it was not."""
    out = []
    for state in ("approved", "integrated", "superseded"):
        for row in artifacts.list_revisions(root, status=state, limit=limit):
            if (row.get("producer") or "") == PRODUCER:
                out.append(_view(root, row))
    out.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _shot_at(seq: dict, idx: int) -> dict:
    for shot in seq["shots"]:
        if int(shot["idx"]) == int(idx):
            return shot
    raise CinematicError(
        f"{seq['name']} has no shot {idx} — it has "
        f"{[s['idx'] for s in seq['shots']]}")


def _set_shot(root: str, shot_id: int, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{k} = ?" for k in fields)
    with db.tx(root) as conn:
        conn.execute(
            f"UPDATE cine_shot SET {columns}, updated_at = datetime('now') "
            "WHERE id = ?", (*fields.values(), int(shot_id)))


def _mark_kept(root: str, artifact_id: int) -> None:
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE cine_shot SET status = 'kept', updated_at = datetime('now') "
            "WHERE artifact_id = ?", (int(artifact_id),))


def _register(root: str, stem: str, shot: dict, seq: dict, result: dict, *,
              work_item_id: Optional[int]) -> dict:
    """One generated clip -> one immutable candidate revision."""
    return artifacts.register(
        root, stem, result["path"], producer=PRODUCER,
        model=str(result.get("model") or ""),
        # THE PROMPT THAT WAS ACTUALLY SENT, style included. Recording the shot
        # text alone would mean a revision whose stored prompt does not
        # reproduce the clip beside it — and the whole reason artifacts carry a
        # prompt is that six months later it is the only record of what was
        # asked for.
        prompt=prompt_for(shot, (seq.get("style_resolved") or {}).get("text", "")),
        refs=list(shot.get("refs") or []), work_item_id=work_item_id,
        metadata={
            "provider": "kie",
            "api": "jobs",
            "kind": "shot",
            "sequence": seq["name"],
            "shot_idx": shot["idx"],
            "shot_slug": shot["slug"],
            "duration_s": shot["duration"],
            "aspect_ratio": seq["aspect_ratio"],
            "resolution": seq["resolution"],
            "camera": shot.get("camera") or "",
            "dialogue": shot.get("dialogue") or "",
            "task_id": result.get("task_id") or "",
            # WHAT IT WAS CONDITIONED ON, as project paths — never as the minted
            # URLs, which are dead in three days and would read as an anchor
            # anybody could re-fetch.
            "first_frame": shot.get("first_frame") or "",
            "last_frame": shot.get("last_frame") or "",
            "uploaded": [{"source": u.get("source", ""),
                          "expires_at": u.get("expires_at", "")}
                         for u in (result.get("uploads") or [])],
            # PROVENANCE, NOT A LOCATION.
            "source_url": result.get("url") or "",
            "source_url_expires_at": result.get("expires_at") or "",
            "credits_consumed": result.get("credits_consumed"),
            "credits_source": result.get("credits_source") or "",
            "estimated_usd": result.get("estimated_usd"),
            "accounted": bool(result.get("accounted")),
            "preview": assets.normalize_path(root, result["path"]),
        })


def _view(root: str | os.PathLike[str], art: dict) -> dict:
    """One revision as a gallery card, saying whether the game can load it.

    ``installed`` MEANS "THIS TAKE IS THE CUTSCENE THE GAME PLAYS", and it is
    measured against the asset registry rather than inferred from the presence of
    a metadata record. Same three ways it can be false that music documents — the
    install never happened (the auto-approve hole), the file was deleted out of
    the engine project, or another take overwrote it — and the third is the one
    that needs the comparison, because every take installs to the destination
    named for the logical asset and the game loads exactly one file.
    """
    meta = art.get("metadata") or {}
    install = meta.get("install") or {}
    target = str(install.get("path") or "")
    exists = bool(target and (Path(root) / target).is_file())
    live = exists
    if exists:
        want = str(install.get("hash") or "")
        if want:
            try:
                live = assets.get(root, target)["hash"] == want
            except Exception:                                    # noqa: BLE001
                # No registry row: fall back to "the file is there". Wrong in
                # the overwrite case, but a missing registry must not turn a
                # real install into a red warning.
                live = True
    return {
        "artifact_id": art["id"],
        "logical_name": art["logical_name"],
        "revision": art["revision"],
        "path": art["path"],
        "status": art["status"],
        "created_at": art.get("created_at"),
        "sequence": meta.get("sequence", ""),
        "shot_idx": meta.get("shot_idx"),
        "duration_s": meta.get("duration_s"),
        "kind": meta.get("kind", "shot"),
        "prompt": art.get("prompt", ""),
        # THE CLAIM THIS MODULE EXISTS TO BE ABLE TO MAKE, and it is checkable.
        "installed": live,
        "install_missing": bool(install) and not exists,
        "install_stale": bool(install) and exists and not live,
        "installed_path": target,
        "godot_res": install.get("godot_res", ""),
        # An .ogv at the destination is the only thing the engine can open, so
        # this is the honest read of "would this play" as distinct from "is this
        # take the one that plays".
        "playable": exists and target.lower().endswith(ENGINE_SUFFIX),
    }


def _install_file(root: str, art: dict, *, quality: int) -> dict:
    """Transcode one clip to where the engine loads it. RAISES on failure."""
    source = Path(root) / art["path"]
    suffix = source.suffix.lower()
    if suffix not in SOURCE_SUFFIXES and suffix != ENGINE_SUFFIX:
        raise CinematicError(
            f"artifact {art['id']} is {suffix or 'extensionless'}, not video — "
            f"only {sorted(SOURCE_SUFFIXES | {ENGINE_SUFFIX})} are installed")
    if not source.is_file():
        raise CinematicError(
            f"nothing on disk at {art['path']} — the clip was removed. kie "
            "serves its own copy for a limited window, so the task id on this "
            "revision may still reach it; past that it has to be regenerated.")

    destination = (Path(root) / _install_dir(root, create=True)
                   / f"{art['logical_name']}{ENGINE_SUFFIX}")
    replaced = destination.is_file()
    if suffix == ENGINE_SUFFIX:
        # Already Theora — an assembled cut. Copying is correct here and
        # re-encoding would be a second generation of loss for nothing.
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            raise CinematicError(
                f"could not install {art['logical_name']} at {destination}: "
                f"{exc}") from exc
        converted = {"path": str(destination), "bytes": destination.stat().st_size,
                     "quality": None, "gop": None, "scaled_to": None}
    else:
        converted = transcode(source, destination, quality=quality)

    rel = assets.normalize_path(root, destination)
    # THE HASH RECORDED IS THE DESTINATION'S, NOT THE SOURCE'S, and that is the
    # one line where this cannot copy music._install_file. Music INSTALLS BY
    # COPYING, so the source revision's hash and the installed file's hash are
    # the same number and either one answers "are these the bytes the game
    # loads". This transcodes: the .ogv is a different file from the .mp4 it came
    # from and shares nothing with it. Storing the source hash here and comparing
    # it to the registry (which is what music does) would compare an .mp4's hash
    # against an .ogv's and report every install as stale; storing it and
    # comparing it to the ARTIFACT's own hash — the first thing tried here — is
    # tautologically true and reported every superseded take as still installed.
    installed_hash = ""
    try:
        installed_hash = str(assets.track(root, rel).get("hash") or "")
    except Exception:                                            # noqa: BLE001
        pass    # the registry is a nicety; the file being in place is not
    return {
        "path": rel,
        "bytes": converted["bytes"],
        "replaced": replaced,
        "transcoded": suffix != ENGINE_SUFFIX,
        "quality": converted["quality"],
        "gop": converted["gop"],
        # WHOSE BYTES ARE AT THE DESTINATION — see music._install_file for why
        # this has to be checkable at all: every take of a shot installs to the
        # same destination, so a record that only says "installed at X" is true
        # of the loser the moment the winner overwrites it.
        "hash": installed_hash,
        "source_hash": str(art.get("hash") or ""),
        "godot_res": f"res://{_engine_relative(root, rel)}",
    }


def _install_dir(root: str | os.PathLike[str], *, create: bool) -> Path:
    """Project-relative directory a kept cutscene is installed into."""
    base = Path(root)
    for candidate in INSTALL_ROOTS:
        if (base / candidate).is_dir():
            return candidate
    chosen = INSTALL_ROOTS[0]
    if create:
        (base / chosen).mkdir(parents=True, exist_ok=True)
    return chosen


def _engine_relative(root: str | os.PathLike[str], rel: str) -> str:
    """The path Godot would use, if the engine project is a subdirectory.

    Advisory, same as music._engine_relative: a res:// string is only right when
    project.godot sits at the root this strips to, and wrong-but-obvious beats
    absent for a string a human can check in a second.
    """
    parts = Path(rel).as_posix().split("/")
    if parts and parts[0] == "game" and (Path(root) / "game" /
                                         "project.godot").is_file():
        return "/".join(parts[1:])
    return "/".join(parts)


def _stamp(root: str, artifact_id: int, key: str, value: Any) -> None:
    """Merge one key into a revision's metadata without disturbing the rest."""
    with db.tx(root) as conn:
        row = conn.execute(
            "SELECT metadata_json FROM artifact_revision WHERE id = ?",
            (int(artifact_id),)).fetchone()
        try:
            meta = json.loads(row["metadata_json"] or "{}") if row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            meta = {}
        meta[key] = value
        conn.execute("UPDATE artifact_revision SET metadata_json = ? WHERE id = ?",
                     (json.dumps(meta), int(artifact_id)))


def _budget_refusal(root: str) -> str:
    """The project budget's answer, or "" to proceed. Never raises.

    Projected at zero because kie publishes no price — this cannot catch "this
    one shot is too expensive", only "this project is already over". A sequence
    is the most expensive thing this product buys in one sitting, which makes the
    ceiling worth asking about before every single shot rather than once per run.
    """
    try:
        from . import spend

        verdict = spend.check(root, projected_usd=0.0)
    except Exception:                                            # noqa: BLE001
        return ""   # no ledger is not a licence to refuse work
    if verdict.get("allowed", True):
        return ""
    return (f"the project budget refuses this shot: "
            f"{verdict.get('reason') or 'ceiling reached'}")
