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
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from . import activity, artifacts, assets, db
from .util import rows, slugify

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
        "provider": "kie/seedance",
        "models": {name: spec for name, spec in kie.MODELS.items()
                   if spec["kind"] == "video"},
        "default_model": kie.DEFAULT_VIDEO_MODEL,
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
        return {"ok": False, "ffmpeg": "", "theora": False,
                "reason": "ffmpeg not found on PATH — it is what converts a "
                          "generated .mp4 into the Ogg Theora the engine can "
                          "play, so shots can be generated and none can be kept"}
    try:
        proc = subprocess.run([exe, "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
        listed = (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "ffmpeg": exe, "theora": False,
                "reason": f"ffmpeg found at {exe} but would not run: {exc}"}
    theora = "libtheora" in listed
    return {
        "ok": theora,
        "ffmpeg": exe,
        "theora": theora,
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
         logline: str = "", style: str = "", aspect_ratio: str = "16:9",
         resolution: str = "720p", work_item_id: Optional[int] = None) -> dict:
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

    REPLANNING PRESERVES WHAT WAS ALREADY BOUGHT. A shot whose action text is
    unchanged keeps its artifact, its status and its task id, so re-running plan()
    to fix a typo in shot 7 does not throw away the five shots already generated
    and paid for. A shot whose action CHANGED is reset to planned, because the
    clip on disk is no longer a rendering of what the list now says.
    """
    root = str(root)
    stem = slugify(name)
    if not stem:
        raise CinematicError("a sequence needs a name")
    if not shots:
        raise CinematicError("a sequence with no shots is not a plan")

    cleaned, warnings = _clean_shots(shots)

    with db.tx(root) as conn:
        conn.execute(
            """
            INSERT INTO cine_sequence (name, logline, style, aspect_ratio,
                                       resolution, work_item_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                logline = excluded.logline, style = excluded.style,
                aspect_ratio = excluded.aspect_ratio,
                resolution = excluded.resolution,
                updated_at = datetime('now')
            """,
            (stem, logline.strip(), style.strip(), aspect_ratio, resolution,
             work_item_id))
        seq_id = int(conn.execute(
            "SELECT id FROM cine_sequence WHERE name = ?", (stem,)).fetchone()[0])

        # What survives a replan, keyed by the text that defines the shot.
        previous = {}
        for row in conn.execute(
                "SELECT * FROM cine_shot WHERE sequence_id = ?", (seq_id,)):
            previous[(row["action"] or "").strip()] = dict(row)
        conn.execute("DELETE FROM cine_shot WHERE sequence_id = ?", (seq_id,))

        kept_rows = 0
        for index, shot in enumerate(cleaned, start=1):
            carried = previous.get(shot["action"])
            if carried:
                kept_rows += 1
            conn.execute(
                """
                INSERT INTO cine_shot (sequence_id, idx, slug, action, camera,
                                       dialogue, duration, first_frame,
                                       last_frame, refs_json, artifact_id,
                                       task_id, status, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (seq_id, index, shot["slug"], shot["action"], shot["camera"],
                 shot["dialogue"], shot["duration"], shot["first_frame"],
                 shot["last_frame"], json.dumps(shot["refs"]),
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
        cleaned.append({
            "slug": _unique_slug(raw.get("slug"), index, used),
            "action": action,
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
    out["shots"] = [_shot_view(root, dict(s)) for s in conn.execute(
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
        out.append({**row, "shot_count": counts["n"] or 0,
                    "kept": counts["kept"] or 0,
                    "runtime_s": counts["secs"] or 0})
    return out


def _shot_view(root: str | os.PathLike[str], shot: dict) -> dict:
    """One shot row, plus whatever is true of the clip it points at."""
    shot["refs"] = json.loads(shot.get("refs_json") or "[]")
    shot.pop("refs_json", None)
    shot["prompt"] = prompt_for(shot)
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


def prompt_for(shot: dict) -> str:
    """The three stored fields joined into what the model is actually sent.

    ONE PLACE, so that a re-generation cannot drift from the original by
    assembling the parts in a different order. Camera leads because a video model
    reads the opening of a prompt as the framing and the rest as the content, and
    a camera instruction buried after two sentences of action gets averaged away.
    """
    parts = []
    if shot.get("camera"):
        parts.append(str(shot["camera"]).strip().rstrip("."))
    parts.append(str(shot.get("action") or "").strip())
    if shot.get("dialogue"):
        # Quoted rather than described: an unquoted line reads as narration and
        # comes back as a character silently doing the thing the line says.
        parts.append(f'The character says: "{str(shot["dialogue"]).strip()}"')
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

    frames = keyframes_for(root, shot)
    if frames["missing"]:
        return {"ok": False, "stage": "anchors",
                "error": "conditioning frames named by this shot are not on "
                         f"disk: {frames['missing']}. Nothing has been charged.",
                "sequence": seq["name"], "idx": idx}

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
        prompt_for(shot), str(out_path),
        model=model or kie.DEFAULT_VIDEO_MODEL,
        duration=int(shot["duration"]),
        resolution=seq["resolution"], aspect_ratio=seq["aspect_ratio"],
        first_frame_url=frames["first"], last_frame_url=frames["last"],
        reference_image_urls=frames["refs"],
        generate_audio=bool(generate_audio),
        root=root, logical_name=stem, work_item_id=work_item_id,
        timeout=float(timeout))

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
    return {"ok": True, "sequence": seq["name"], "idx": idx,
            "artifact_id": artifact["id"], "path": artifact["path"],
            "revision": artifact["revision"],
            "uploads": result.get("uploads") or [],
            "credits_consumed": result.get("credits_consumed"),
            "seconds": result.get("seconds"),
            "consumes": "WATCH IT. Then cinematic_keep to transcode it into the "
                        "engine project, or re-generate this shot — a shot "
                        "nobody looked at is a shot nobody judged."}


def keyframes_for(root: str | os.PathLike[str], shot: dict) -> dict:
    """This shot's conditioning frames as things the adapter can send.

    Returns ``{first, last, refs, missing}`` — local paths are handed through as
    paths and the adapter uploads them; anything already a URL passes untouched.

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
    for one in (shot.get("refs") or []):
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
         quality: int = DEFAULT_QUALITY,
         actor: Optional[str] = None) -> dict:
    """Transcode a shot into the engine project, then approve the revision.

    THE ORDER IS THE POINT, exactly as it is in music.keep: approving first would
    leave an approval that means nothing when the conversion fails — the database
    saying 'approved' while the game has no file. So the transcode happens first
    and its failure is RAISED. The recoverable direction to fail in is a file in
    the project whose revision is still a candidate.

    :func:`artifacts.review` is what enforces 'only a human may approve'. This
    inherits that gate rather than re-implementing it.
    """
    root = str(root)
    art = artifacts.get(root, int(artifact_id))
    install = _install_file(root, art, quality=quality)
    _stamp(root, int(artifact_id), "install", install)

    reviewed = artifacts.review(
        root, int(artifact_id), "approved",
        note=note or "kept from the cinematic shot gallery and transcoded to "
                     "Ogg Theora for the engine",
        actor=actor)
    _mark_kept(root, int(artifact_id))
    activity.log(root, "cinematic",
                 f"kept {art['logical_name']} r{art['revision']} -> "
                 f"{install['path']}",
                 seat="cinematic", ref=str(artifact_id), actor=actor or "")
    return {"ok": True, "artifact": _view(root, reviewed), "install": install}


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
    encoder = ffmpeg_status()
    if not encoder["ok"]:
        raise CinematicError(encoder["reason"])
    src, dst = Path(source), Path(destination)
    if not src.is_file():
        raise CinematicError(f"nothing on disk at {src}")
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
                              timeout=timeout, stdin=subprocess.DEVNULL)
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
    _concat(sources, out_path, quality=quality)

    artifact = artifacts.register(
        root, seq["name"], out_path, producer=PRODUCER,
        model="ffmpeg/libtheora",
        prompt=seq["logline"] or f"assembled cut of {seq['name']}",
        work_item_id=work_item_id or seq.get("work_item_id"),
        metadata={
            "kind": "cutscene",
            "assembled_from": [{"idx": s["idx"], "slug": s["slug"],
                                "artifact_id": s["artifact_id"],
                                "duration": s["duration"]} for s in usable],
            "runtime_s": sum(s["duration"] for s in usable),
            "aspect_ratio": seq["aspect_ratio"],
            "resolution": seq["resolution"],
            "quality": int(quality),
            "gop": int(THEORA_GOP),
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
    return {"ok": True, "sequence": seq["name"],
            "artifact_id": artifact["id"], "revision": artifact["revision"],
            "path": artifacts.get(root, artifact["id"])["path"],
            "shots": len(usable),
            "runtime_s": sum(s["duration"] for s in usable),
            "consumes": "WATCH THE WHOLE CUT, then cinematic_keep it — the "
                        "individual shots were judged alone and a cut is judged "
                        "as a cut. Keeping installs it under the engine project."}


def _concat(sources: list, out_path: Path, *, quality: int) -> None:
    """Join clips through ffmpeg's concat demuxer, encoding Theora in one pass.

    ONE PASS, NOT N TRANSCODES AND A JOIN. Converting each shot to .ogv and then
    concatenating the results would put the sequence through Theora twice — the
    generation loss of a lossy codec applied to its own output — and the demuxer
    is fussier about joining Ogg streams than about reading the .mp4s we already
    have. So the .mp4 shots go in and one .ogv comes out.

    -safe 0 because the list holds absolute paths, which is what a project root
    on Windows produces and what the demuxer refuses by default.
    """
    encoder = ffmpeg_status()
    if not encoder["ok"]:
        raise CinematicError(encoder["reason"])
    listing = out_path.parent / f"{out_path.stem}_concat.txt"
    # The demuxer's own quoting rule: single quotes, and an embedded one is
    # escaped by closing, escaping, reopening. Paths with an apostrophe exist.
    listing.write_text(
        "".join("file '{}'\n".format(str(p).replace("'", r"'\''"))
                for p in sources),
        encoding="utf-8")
    cmd = [encoder["ffmpeg"], "-y", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(listing),
           "-codec:v", "libtheora", "-q:v", str(int(quality)),
           "-g:v", THEORA_GOP,
           "-codec:a", "libvorbis", "-q:a", str(int(quality)),
           str(out_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                              stdin=subprocess.DEVNULL)
    finally:
        listing.unlink(missing_ok=True)
    if proc.returncode != 0 or not out_path.is_file():
        raise CinematicError(
            "ffmpeg could not assemble the cut: "
            + ((proc.stderr or "").strip()[-300:] or
               f"exit {proc.returncode} and no output file"))


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
        model=str(result.get("model") or ""), prompt=prompt_for(shot),
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
