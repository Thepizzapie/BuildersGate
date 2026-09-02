"""The scene before anybody pays for it.

WHAT THIS CLOSES. cinematic.py can plan a shot list and buy the shots; cinecut.py
can cut them together. Both of those start from a point where somebody already
knows what the scene IS — and nothing modelled the part before that. So the only
place to work out a cutscene was the shot list itself, where every wrong idea sits
one click away from a paid generation and a re-think means editing rows that a
provider task id is already attached to. The observable symptom was people
planning cutscenes in chat and then transcribing the result, which means the
reasoning was never in the project and the second draft had nothing to read.

A BOARD IS THE PLACE YOU ARE ALLOWED TO BE WRONG. Reorder it, throw half of it
out, generate six versions of frame 3 — none of it bills against the video
provider, because none of it touches the video provider. The single expensive
verb in this module is :func:`frame_generate`, and that is an IMAGE, which is
roughly two orders of magnitude cheaper than the video shot it exists to stop
you buying blind. Everything else here is free: writing the script costs a
fraction of a cent through the same chat-completions path promptwriter.py uses,
and planning, reordering, attaching and promoting cost nothing at all.

THE COHERENCE ARGUMENT, WHICH IS THE REASON THIS EXISTS RATHER THAN A TEXT FILE.
A cutscene generated shot by shot from prose drifts: the character's jacket
changes colour, the room grows a window, the palette wanders. The fix is not a
better prompt, it is a fixed cast — every frame on a board is conditioned on the
same pinned references (``cast_refs``) and the same style anchors, resolved
through refs.resolve() at generation time. The board is where that cast is
decided once, and :func:`promote` carries it onto the sequence so the shots are
bought under the look the board was approved under.

WHAT PROMOTION IS. :func:`promote` is the boundary between free and paid, and it
is a single call you can point at rather than a gradient. It writes a
cine_sequence via cinematic.plan() with each approved frame's image wired in as
that shot's ``first_frame`` — which is exactly the "anchor on an approved still"
the cinematic seat brief has always demanded and previously had no path to
produce. Before this, anchoring could only be done by hand through the MCP tool
with a path the art seat happened to have made.

DELIBERATELY NOT HERE: video. A board holds stills and prose. The moment
something on a board could cost video money, the free/paid line this module
exists to draw would be inside it instead of at its edge.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from ..board import activity
from ..store import artifacts, db
from . import cinematic
from ..art import refs as _refs
from ..store.util import rows, slugify

# Board images live in the cinematic seat's existing design lane. No new write
# glob: a storyboard is design work about a cutscene, and giving it its own lane
# would mean the seat that plans the cutscene needs two.
BOARD_DIRNAME = "design/cinematics/storyboards"

# Cheap and fast on purpose, same reasoning as promptwriter.DEFAULT_MODEL: this
# is structured writing, not a reasoning task, and it is re-run freely.
SCRIPT_MODEL = "gpt-4o-mini"

# Rough, per script call. A few thousand tokens out is still a fraction of a
# cent; recorded so the ledger is honest rather than pretending text is free.
SCRIPT_USD_PER_CALL = 0.002

FRAME_STATUSES = ("empty", "generating", "drafted", "approved", "cut")
# Mirrors the CHECK constraint on storyboard.status in db.py — kept as the
# Python-side statement of the vocabulary even though the writes go through
# named helpers, so the two cannot drift silently.
BOARD_STATUSES = ("drafting", "boarded", "promoted", "abandoned")
SOURCES = ("none", "generated", "uploaded", "pinned")

# A board frame is a THUMBNAIL, not a deliverable. 1024x576 is 16:9 at a size
# every provider takes and nobody is tempted to ship. Boards are meant to be
# read at a glance, six across.
FRAME_SIZES = {"16:9": "1536x1024", "9:16": "1024x1536", "1:1": "1024x1024",
               "4:3": "1536x1024", "21:9": "1536x1024"}


class StoryboardError(RuntimeError):
    """A refusal a caller should read and act on, not a crash."""


_SCRIPT_SYSTEM = (
    "You are a storyboard writer for a video game cutscene. You return ONE "
    "JSON object and nothing else - no preamble, no code fence, no commentary.\n"
    "Shape:\n"
    '{"logline": "one sentence", "script": "the scene in prose, present tense", '
    '"frames": [{"beat": "what happens, one line", "action": "what is visible '
    'and moving, for an image model", "camera": "shot size and movement", '
    '"dialogue": "spoken line or empty string", "duration": 5}]}\n'
    "Rules:\n"
    "- Use ONLY the characters, places and props the author named. Do not "
    "invent a cast. If the author named nobody, describe roles, not names.\n"
    "- Do NOT invent an art style, palette or rendering technique. Those are "
    "set elsewhere and your guesses will fight them.\n"
    "- `action` is read by an image model: concrete, visible, present tense. "
    "No interiority - 'she realises' is not a picture.\n"
    "- `camera` names the shot: wide, medium, close, over-the-shoulder, and "
    "any movement. One phrase.\n"
    "- `duration` is whole seconds, 2 to 10. No shot runs longer than 10.\n"
    "- Every frame must advance the scene. A frame that restates the previous "
    "frame is a frame the author will pay to generate twice."
)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def boards(root: str | os.PathLike[str], limit: int = 100) -> list[dict]:
    """Every board, newest first, each with a frame count and its status mix."""
    out = []
    for b in rows(db.connect(root).execute(
            "SELECT * FROM story_board ORDER BY updated_at DESC LIMIT ?",
            (int(limit),))):
        counts = rows(db.connect(root).execute(
            "SELECT status, COUNT(*) AS n FROM story_frame WHERE board_id=? "
            "GROUP BY status", (b["id"],)))
        b["frames"] = sum(c["n"] for c in counts)
        b["frame_status"] = {c["status"]: c["n"] for c in counts}
        b["cast_refs"] = _loads(b.get("cast_refs_json"), [])
        b["style_refs"] = _loads(b.get("style_refs_json"), [])
        out.append(b)
    return out


def board(root: str | os.PathLike[str], name: str) -> dict:
    """One board with its frames in order, each decorated for display."""
    row = _board_row(root, name)
    row["cast_refs"] = _loads(row.get("cast_refs_json"), [])
    row["style_refs"] = _loads(row.get("style_refs_json"), [])
    row["script"] = _loads(row.get("script_json"), {})
    row["frames"] = [_frame_view(root, f) for f in rows(db.connect(root).execute(
        "SELECT * FROM story_frame WHERE board_id=? ORDER BY idx",
        (row["id"],)))]
    row["ready"] = _readiness(row["frames"])
    return row


def _frame_view(root: str | os.PathLike[str], frame: dict) -> dict:
    """A frame row plus the two things a caller always asks next: does the
    image actually exist on disk, and what were its refs."""
    view = dict(frame)
    view["refs"] = _loads(frame.get("refs_json"), [])
    rel = (frame.get("image_path") or "").strip()
    view["has_image"] = bool(rel) and (Path(root) / rel).exists()
    if rel and not view["has_image"]:
        # A path that no longer resolves is worth saying out loud rather than
        # rendering as a broken thumbnail — it usually means the file was moved
        # or the board was copied between projects.
        view["missing_image"] = rel
    return view


def _readiness(frames: list) -> dict:
    """Can this board be promoted, and if not, what is in the way.

    Promotion buys video, so the answer has to be specific enough to act on. A
    bare False sends the caller back to read every frame themselves.
    """
    live = [f for f in frames if f.get("status") != "cut"]
    unanchored = [f["idx"] for f in live if not f.get("has_image")]
    unapproved = [f["idx"] for f in live if f.get("status") != "approved"]
    blockers = []
    if not live:
        blockers.append("the board has no live frames")
    if unanchored:
        blockers.append(
            f"{len(unanchored)} frame(s) have no image: {unanchored} - a shot "
            "promoted without one is bought against prose alone")
    if unapproved:
        blockers.append(
            f"{len(unapproved)} frame(s) are not approved: {unapproved}")
    return {"promotable": not blockers, "blockers": blockers,
            "live": len(live), "approved": len(live) - len(unapproved)}


# ---------------------------------------------------------------------------
# planning — free
# ---------------------------------------------------------------------------

def plan(root: str | os.PathLike[str], name: str, frames: Optional[list] = None,
         *, premise: str = "", logline: str = "", style: str = "",
         style_note: str = "", style_refs: Optional[list] = None,
         cast_refs: Optional[list] = None, aspect_ratio: str = "16:9",
         script: Any = None, work_item_id: Optional[int] = None) -> dict:
    """Write (or rewrite) a board. Costs nothing, spends nothing.

    ``frames`` is a list of dicts — ``beat`` or ``action`` is required, and
    ``camera``, ``dialogue``, ``duration``, ``image_path``, ``refs``, ``slug``
    and ``note`` are optional. Order in the list is order in the scene.

    RE-PLANNING PRESERVES WHAT WAS PAID FOR. A frame that already has an image
    keeps it when the board is rewritten at the same index, because the images
    are the only things here that cost money and re-planning is meant to be
    cheap enough to do repeatedly. Pass ``image_path`` explicitly to replace
    one; the frame's status and source ride along with its image.

    Omitting ``frames`` entirely edits the board's own fields and leaves the
    frames alone — that is how you re-cast or re-style a board you have already
    drawn without throwing the drawings away.
    """
    if not str(name or "").strip():
        # Tested BEFORE slugify, which answers "unnamed" for empty input. `if
        # not slugify(name)` never fires, so every unnamed board would collide
        # on one row and each new one would overwrite the last.
        raise StoryboardError("a board needs a name")
    slug = slugify(name)

    conn = db.connect(root)
    existing = rows(conn.execute(
        "SELECT * FROM story_board WHERE name=?", (slug,)))
    prior = existing[0] if existing else None

    if prior and prior["status"] == "promoted" and frames is not None:
        raise StoryboardError(
            f"board {slug!r} was already promoted to a sequence - its frames "
            "are what those shots were approved from. Copy it to a new name "
            "rather than editing the record of a decision that has been acted "
            "on.")

    cast = _clean_refs(root, cast_refs if cast_refs is not None
                       else _loads((prior or {}).get("cast_refs_json"), []))
    looks = _clean_refs(root, style_refs if style_refs is not None
                        else _loads((prior or {}).get("style_refs_json"), []))
    script_blob = _script_blob(script, prior)

    fields = {
        "premise": premise or (prior or {}).get("premise", ""),
        "logline": logline or (prior or {}).get("logline", ""),
        "style": style or (prior or {}).get("style", ""),
        "style_note": style_note or (prior or {}).get("style_note", ""),
        "style_refs_json": json.dumps(looks),
        "cast_refs_json": json.dumps(cast),
        "script_json": json.dumps(script_blob),
        "aspect_ratio": aspect_ratio or (prior or {}).get("aspect_ratio", "16:9"),
    }
    if work_item_id is not None:
        fields["work_item_id"] = work_item_id

    with conn:
        if prior:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE story_board SET {sets}, updated_at=datetime('now') "
                "WHERE id=?", (*fields.values(), prior["id"]))
            board_id = prior["id"]
        else:
            cols = ", ".join(["name", *fields])
            marks = ", ".join("?" * (len(fields) + 1))
            cur = conn.execute(
                f"INSERT INTO story_board ({cols}) VALUES ({marks})",
                (slug, *fields.values()))
            board_id = int(cur.lastrowid)

        written = 0
        if frames is not None:
            kept = _carry_over(conn, board_id)
            conn.execute("DELETE FROM story_frame WHERE board_id=?", (board_id,))
            for i, frame in enumerate(_clean_frames(root, frames), start=1):
                carried = kept.get(i, {})
                image = frame["image_path"] or carried.get("image_path", "")
                source = frame["source"] or (
                    carried.get("source", "none") if image else "none")
                status = frame["status"] or (
                    carried.get("status", "empty") if image else "empty")
                conn.execute(
                    "INSERT INTO story_frame (board_id, idx, slug, beat, action,"
                    " camera, dialogue, duration, image_path, source, refs_json,"
                    " prompt, status, note) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (board_id, i, frame["slug"], frame["beat"], frame["action"],
                     frame["camera"], frame["dialogue"], frame["duration"],
                     image, source, json.dumps(frame["refs"]),
                     frame["prompt"] or carried.get("prompt", ""), status,
                     frame["note"]))
                written += 1

    activity.log(root, "storyboard",
                 f"planned board {slug!r} ({written} frames)"
                 if frames is not None else f"updated board {slug!r}")
    out = board(root, slug)
    out["warnings"] = _plan_warnings(out)
    return out


def _carry_over(conn: Any, board_id: int) -> dict:
    """The paid-for parts of the current frames, keyed by index.

    Only images and what describes them. A re-plan is allowed to rewrite every
    word on a board; it is not allowed to silently drop a generated frame.
    """
    out = {}
    for f in rows(conn.execute(
            "SELECT idx, image_path, source, status, prompt FROM story_frame "
            "WHERE board_id=? AND image_path<>''", (board_id,))):
        out[int(f["idx"])] = f
    return out


def _plan_warnings(out: dict) -> list[str]:
    warn = []
    if not out.get("cast_refs"):
        warn.append(
            "NO CAST REFERENCES PINNED. Every frame will be generated from "
            "prose alone, which is the drift this board exists to prevent - "
            "the character's look will wander between frames. Pin the "
            "characters with ref_pin and pass them as cast_refs.")
    if not (out.get("style") or out.get("style_note") or out.get("style_refs")):
        warn.append(
            "no style set - frames will come back in whatever the model "
            "defaults to, and the sequence promoted from them will inherit it")
    long_shots = [f["idx"] for f in out.get("frames", []) if f["duration"] > 10]
    if long_shots:
        warn.append(
            f"frames {long_shots} run over 10s - no video model generates past "
            "about 15 seconds, so these will have to be split before promotion")
    return warn


# ---------------------------------------------------------------------------
# the script — one cheap text call
# ---------------------------------------------------------------------------

def write_script(root: str | os.PathLike[str], name: str, premise: str, *,
                 frames: int = 6, style: str = "", style_note: str = "",
                 cast_refs: Optional[list] = None, aspect_ratio: str = "16:9",
                 characters: str = "", timeout: float = 90.0,
                 work_item_id: Optional[int] = None) -> dict:
    """Turn a premise into a logline, a script and a beat-per-frame board.

    ONE TEXT CALL, NOT AN AGENT — the same argument promptwriter.py makes. A
    scene breakdown is a paragraph and a list; dispatching a seat with a lane
    hook and a lifecycle to write one is reaching for the heaviest mechanism in
    the codebase because it is the one already wired.

    THE CAST IS INJECTED, NOT INVENTED. Every pinned reference named in
    ``cast_refs`` contributes its stored profile traits (refs.profile_get) to
    the ask, so the script is written about THIS project's characters rather
    than about plausible strangers with the same job titles. That is also why
    the system prompt forbids inventing a cast: a script that introduces a
    character nobody has drawn produces a frame nobody can anchor.

    The result is written to the board immediately. A script that only existed
    in a return value would have to be transcribed to be used, which is the
    workflow this module was written to end.
    """
    text = (premise or "").strip()
    if not text:
        raise StoryboardError("a script needs a premise - one or two sentences "
                              "about what happens in this scene")
    want = max(1, min(int(frames or 6), 24))

    ready = _script_available(root)
    if not ready["available"]:
        return {"ok": False, "error": ready["reason"], "stage": "no_key",
                "estimated_usd": 0.0}

    cast = _clean_refs(root, cast_refs or [])
    ask = _script_ask(root, text, want, cast, characters, style, style_note)

    started = time.monotonic()
    try:
        from openai import OpenAI

        client = OpenAI(timeout=timeout)
        reply = client.chat.completions.create(
            model=os.environ.get("BGATE_SCRIPT_MODEL", SCRIPT_MODEL),
            messages=[{"role": "system", "content": _SCRIPT_SYSTEM},
                      {"role": "user", "content": ask}],
            response_format={"type": "json_object"},
            temperature=0.8,
        )
        raw = (reply.choices[0].message.content or "").strip()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "stage": "model", "seconds": round(time.monotonic() - started, 2),
                "estimated_usd": 0.0}

    try:
        parsed = json.loads(raw)
    except Exception:
        return {"ok": False, "error": "the model did not return usable JSON",
                "stage": "parse", "raw": raw[:2000],
                "seconds": round(time.monotonic() - started, 2),
                "estimated_usd": SCRIPT_USD_PER_CALL}

    beats = _beats_from(parsed, want)
    if not beats:
        return {"ok": False, "error": "the model returned no frames",
                "stage": "parse", "raw": raw[:2000],
                "seconds": round(time.monotonic() - started, 2),
                "estimated_usd": SCRIPT_USD_PER_CALL}

    _record_spend(root, SCRIPT_USD_PER_CALL, "script", name, work_item_id)

    out = plan(root, name, beats, premise=text,
               logline=str(parsed.get("logline") or "").strip(),
               style=style, style_note=style_note, cast_refs=cast,
               aspect_ratio=aspect_ratio,
               script={"prose": str(parsed.get("script") or "").strip(),
                       "premise": text,
                       "model": os.environ.get("BGATE_SCRIPT_MODEL", SCRIPT_MODEL),
                       "written_at": time.strftime("%Y-%m-%d %H:%M:%S")},
               work_item_id=work_item_id)
    out["ok"] = True
    out["seconds"] = round(time.monotonic() - started, 2)
    out["estimated_usd"] = SCRIPT_USD_PER_CALL
    return out


def _script_available(root: Any) -> dict:
    try:
        from ..store import envfile

        envfile.load_project_env(root)
    except Exception:
        pass
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        return {"available": False,
                "reason": "OPENAI_API_KEY not set - put it in the project's "
                          ".env (gitignored, loaded per project). You can still "
                          "write the board by hand with storyboard_plan."}
    return {"available": True}


def _script_ask(root: str | os.PathLike[str], premise: str, want: int,
                cast: list, characters: str, style: str,
                style_note: str) -> str:
    """The user turn. Cast profiles first, because they constrain everything
    after them, and a model reads the top of a prompt hardest."""
    parts = []
    known = _cast_profiles(root, cast)
    if known:
        parts.append("CHARACTERS IN THIS PROJECT (use these, do not invent "
                     "others):\n" + "\n".join(known))
    if characters.strip():
        parts.append(f"The author also says: {characters.strip()}")
    look = cinematic.resolve_style(style, style_note)
    if look.get("text"):
        # Named so the writer does not describe it back at us — the style is
        # applied at image-generation time and saying it twice steers worse.
        parts.append(f"The finished scene will be rendered as: {look['text']}. "
                     "Do not describe the style in your output; write what "
                     "HAPPENS.")
    parts.append(f"PREMISE: {premise}")
    parts.append(f"Break this into exactly {want} frames.")
    return "\n\n".join(parts)


def _cast_profiles(root: str | os.PathLike[str], cast: list) -> list[str]:
    out = []
    for name in cast:
        base = str(name).split("@")[0]
        profile = None
        try:
            profile = _refs.profile_get(root, base)
        except Exception:
            profile = None
        traits = (profile or {}).get("traits", "").strip()
        out.append(f"- {base}: {traits}" if traits else f"- {base}")
    return out


def _beats_from(parsed: Any, want: int) -> list[dict]:
    raw = parsed.get("frames") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return []
    beats = []
    for item in raw[:want]:
        if not isinstance(item, dict):
            continue
        beat = str(item.get("beat") or "").strip()
        action = str(item.get("action") or "").strip()
        if not (beat or action):
            continue
        beats.append({"beat": beat, "action": action or beat,
                      "camera": str(item.get("camera") or "").strip(),
                      "dialogue": str(item.get("dialogue") or "").strip(),
                      "duration": item.get("duration", 5)})
    return beats


# ---------------------------------------------------------------------------
# frames — the one place that spends
# ---------------------------------------------------------------------------

def frame_generate(root: str | os.PathLike[str], name: str, idx: int, *,
                   prompt: str = "", provider: str = "", model: str = "",
                   refs: Optional[list] = None, use_cast: bool = True,
                   ref_strength: float = 0.5, quality: str = "medium",
                   work_item_id: Optional[int] = None) -> dict:
    """Draw one board frame. This is the only verb here that costs money.

    ONE FRAME PER CALL, for the same reason cinematic.generate_shot buys one
    shot per call: a loop that draws the whole board has nowhere to stop when
    frame 2 comes back wrong, and the caller finds out what they bought only
    after they have bought all of it.

    CONDITIONING IS THE POINT. The board's ``cast_refs`` and ``style_refs`` are
    resolved through refs.resolve() and passed as reference images, so frame 6
    is drawn against the same character files as frame 1. ``refs`` adds
    frame-specific references on top; ``use_cast=False`` drops the cast for the
    frames that genuinely have nobody in them (an establishing shot of a room),
    where a character reference is just noise the model has to fight.

    The image lands under ``design/cinematics/storyboards/<board>/`` — the
    cinematic seat's existing design lane, because a storyboard is design work
    about a cutscene, not a shipped asset.
    """
    b = _board_row(root, name)
    frame = _frame_row(root, b["id"], idx)

    from ..art import chroma
    from ..board import spend

    cast = _loads(b["cast_refs_json"], []) if use_cast else []
    looks = _loads(b["style_refs_json"], [])
    extra = _clean_refs(root, refs or [])
    ref_paths, missing = _resolve_all(root, [*extra, *cast, *looks])

    text = (prompt or "").strip()
    if not text:
        # Tested on the SUBJECT, not on the built prompt. _frame_prompt always
        # appends the no-text-in-the-image boilerplate, so the assembled string
        # is never empty and a check on it would happily buy an image of the
        # boilerplate alone.
        if not (frame.get("action") or frame.get("beat") or "").strip():
            raise StoryboardError(
                f"frame {idx} has no action and no prompt - there is nothing "
                "to draw. Write the beat first, or pass prompt=")
        text = _frame_prompt(b, frame)

    try:
        verdict = spend.check(root, projected_usd=_frame_price(quality))
    except Exception:                                            # noqa: BLE001
        verdict = {"allowed": True}   # no ledger is not a licence to refuse work
    if not verdict.get("allowed", True):
        return {"ok": False, "stage": "spend_gate",
                "error": "the project budget refuses this frame: "
                         + (verdict.get("reason") or "ceiling reached"),
                "estimated_usd": 0.0}

    out_rel = _frame_rel(b, frame)
    out_path = Path(root) / out_rel
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Explicit provider means the caller chose; honour it and do not wander off
    # to a different vendor behind their back. Otherwise try each configured
    # provider until one draws, moving on only when the failure was the
    # ACCOUNT's rather than the prompt's - see _is_account_failure.
    chain = [provider] if provider else _providers(root)

    _set_frame(root, frame["id"], status="generating", prompt=text)
    result: dict = {}
    tried: list[dict] = []
    for candidate in chain:
        try:
            result = chroma.generate(
                text, out_path,
                provider=candidate,
                model=model, task_kind="concept",
                keyed=False, transparent=False,
                size=FRAME_SIZES.get(b["aspect_ratio"], "1536x1024"),
                quality=quality, ref_paths=ref_paths,
                ref_strength=ref_strength,
                root=root, logical_name=_logical(b, frame),
                work_item_id=work_item_id)
        except Exception as exc:                                 # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        if result.get("ok", True) and out_path.exists():
            result["provider"] = candidate
            break

        tried.append({"provider": candidate,
                      "error": str(result.get("error", "no image returned"))})
        if not _is_account_failure(result.get("error")):
            break   # the next vendor would refuse this too

    if not result.get("ok", True) or not out_path.exists():
        _set_frame(root, frame["id"], status="empty")
        # EVERY provider is named, with its own reason. One line saying "image
        # generation is unavailable" is what let a dead OpenAI account read as
        # an org-wide outage while Krea sat there working.
        return {"ok": False, "stage": "generate",
                "error": "; ".join(f"{t['provider']}: {t['error']}"
                                   for t in tried) or "no image returned",
                "tried": tried,
                "estimated_usd": result.get("estimated_usd", 0.0)}

    art = artifacts.register(
        root, _logical(b, frame), out_path, producer="storyboard",
        model=result.get("model", ""), prompt=text,
        refs=[*extra, *cast, *looks], work_item_id=work_item_id,
        metadata={"board": b["name"], "frame": idx, "kind": "storyboard_frame"})

    _set_frame(root, frame["id"], image_path=out_rel, source="generated",
               status="drafted", artifact_id=art.get("id"),
               refs_json=json.dumps([*extra, *cast, *looks]))

    activity.log(root, "storyboard",
                 f"drew frame {idx} of board {b['name']!r}")
    return {"ok": True, "board": b["name"], "idx": idx, "path": out_rel,
            "artifact_id": art.get("id"), "prompt": text,
            "refs_used": [*extra, *cast, *looks], "missing_refs": missing,
            # WHICH ACCOUNT PAID, and what it had to walk past to get here. With
            # only the model name in the result, a frame drawn by the second
            # provider after the first refused looked identical to one drawn by
            # the first - so nobody could see that an account had gone dry.
            "provider": result.get("provider", ""),
            "fell_back_from": [t["provider"] for t in tried],
            "model": result.get("model", ""),
            "seconds": result.get("seconds", 0.0),
            "estimated_usd": result.get("estimated_usd", 0.0),
            "frame": _frame_view(root, _frame_row(root, b["id"], idx))}


def frame_attach(root: str | os.PathLike[str], name: str, idx: int, *,
                 image: str = "", ref: str = "",
                 approve: bool = False) -> dict:
    """Put an existing image on a frame — a file the author drew, shot, or
    already pinned.

    THE HUMAN PATH, and it is first-class rather than a fallback. A frame the
    author chose is better evidence for spending video money than a frame the
    model guessed, which is why ``source`` records which one this was: 'uploaded'
    for a path, 'pinned' for a ref name. Approving a paid shot off a generated
    frame while believing a human picked it is the mistake that distinction
    exists to prevent.
    """
    b = _board_row(root, name)
    frame = _frame_row(root, b["id"], idx)

    if bool(image) == bool(ref):
        raise StoryboardError(
            "attach exactly one of image= (a project-relative path) or ref= "
            "(a pinned reference name)")

    if ref:
        try:
            resolved = _refs.resolve(root, ref)
        except Exception as exc:
            raise StoryboardError(f"no pinned reference {ref!r}: {exc}") from exc
        source = "pinned"
    else:
        resolved = cinematic.project_path(root, image, what="frame image")
        source = "uploaded"

    rel = _relative(root, resolved)
    if not (Path(root) / rel).exists():
        raise StoryboardError(f"{rel} does not exist")

    _set_frame(root, frame["id"], image_path=rel, source=source,
               status="approved" if approve else "drafted", artifact_id=None)
    activity.log(root, "storyboard",
                 f"attached {source} image to frame {idx} of board {b['name']!r}")
    return {"ok": True, "board": b["name"], "idx": idx, "path": rel,
            "source": source,
            "frame": _frame_view(root, _frame_row(root, b["id"], idx))}


def frame_set(root: str | os.PathLike[str], name: str, idx: int,
              **fields: Any) -> dict:
    """Edit one frame's text, timing or status without touching the rest."""
    b = _board_row(root, name)
    frame = _frame_row(root, b["id"], idx)

    allowed = {"beat", "action", "camera", "dialogue", "duration", "note",
               "status", "slug"}
    unknown = set(fields) - allowed
    if unknown:
        raise StoryboardError(
            f"cannot set {sorted(unknown)} on a frame - editable fields are "
            f"{sorted(allowed)}. An image is changed with frame_attach or "
            "frame_generate so its source is recorded.")

    clean: dict = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key == "duration":
            clean[key] = _duration(value)
        elif key == "status":
            status = str(value).strip()
            if status not in FRAME_STATUSES:
                raise StoryboardError(
                    f"status {status!r} is not one of {list(FRAME_STATUSES)}")
            if status == "approved" and not (frame.get("image_path") or "").strip():
                raise StoryboardError(
                    f"frame {idx} has no image - approving it would mean the "
                    "shot promoted from it is bought against prose alone. "
                    "Draw it or attach one first.")
            clean[key] = status
        elif key == "slug":
            clean[key] = slugify(str(value))
        else:
            clean[key] = str(value)

    if clean:
        _set_frame(root, frame["id"], **clean)
    return {"ok": True, "board": b["name"], "idx": idx,
            "frame": _frame_view(root, _frame_row(root, b["id"], idx))}


def frame_reorder(root: str | os.PathLike[str], name: str,
                  order: list) -> dict:
    """Re-sequence a board by listing current indices in their new order.

    Every live frame must appear exactly once. A reorder that quietly dropped a
    frame would throw away a generated image, so a partial list is refused
    rather than interpreted.
    """
    b = _board_row(root, name)
    conn = db.connect(root)
    current = rows(conn.execute(
        "SELECT id, idx FROM story_frame WHERE board_id=? ORDER BY idx",
        (b["id"],)))
    have = [int(f["idx"]) for f in current]
    want = [int(x) for x in order]

    if sorted(want) != sorted(have):
        raise StoryboardError(
            f"reorder must list every frame exactly once. Board has {have}, "
            f"you passed {want}.")

    by_idx = {int(f["idx"]): f["id"] for f in current}
    with conn:
        # Two passes through a negative staging range: idx is UNIQUE per board,
        # so writing 2->1 before 1->2 collides with a row that has not moved yet.
        for position, old in enumerate(want, start=1):
            conn.execute("UPDATE story_frame SET idx=? WHERE id=?",
                         (-position, by_idx[old]))
        conn.execute(
            "UPDATE story_frame SET idx=-idx, updated_at=datetime('now') "
            "WHERE board_id=? AND idx<0", (b["id"],))
        conn.execute("UPDATE story_board SET updated_at=datetime('now') "
                     "WHERE id=?", (b["id"],))
    return board(root, b["name"])


def frame_add(root: str | os.PathLike[str], name: str, *, beat: str = "",
              action: str = "", camera: str = "", dialogue: str = "",
              duration: Any = 5, after: Optional[int] = None) -> dict:
    """Insert one frame, at the end by default or after a given index."""
    b = _board_row(root, name)
    conn = db.connect(root)
    have = [int(f["idx"]) for f in rows(conn.execute(
        "SELECT idx FROM story_frame WHERE board_id=? ORDER BY idx", (b["id"],)))]
    at = (len(have) + 1) if after is None else max(1, min(int(after) + 1,
                                                         len(have) + 1))
    with conn:
        for old in sorted([i for i in have if i >= at], reverse=True):
            conn.execute(
                "UPDATE story_frame SET idx=? WHERE board_id=? AND idx=?",
                (old + 1, b["id"], old))
        conn.execute(
            "INSERT INTO story_frame (board_id, idx, slug, beat, action, camera,"
            " dialogue, duration) VALUES (?,?,?,?,?,?,?,?)",
            (b["id"], at, f"frame{at:02d}", str(beat), str(action or beat),
             str(camera), str(dialogue), _duration(duration)))
    return board(root, b["name"])


def frame_cut(root: str | os.PathLike[str], name: str, idx: int) -> dict:
    """Mark a frame cut. It stays on the board and out of the promotion.

    Cut rather than deleted because a frame that was drawn was paid for, and an
    argument about whether the scene needs it is an argument you may lose twice.
    """
    b = _board_row(root, name)
    frame = _frame_row(root, b["id"], idx)
    _set_frame(root, frame["id"], status="cut")
    return board(root, b["name"])


# ---------------------------------------------------------------------------
# the one call
# ---------------------------------------------------------------------------

def auto(root: str | os.PathLike[str], name: str, premise: str = "", *,
         frames: int = 6, style: str = "", style_note: str = "",
         cast_refs: Optional[list] = None, aspect_ratio: str = "16:9",
         quality: str = "low", approve: bool = True, promote_to: str = "",
         model: str = "", work_item_id: Optional[int] = None) -> dict:
    """Premise in, finished storyboard out. No questions asked.

    THIS IS THE DEFAULT DOOR AND THE OTHER VERBS ARE ITS PARTS. Every piece of
    this module could already do its own job, and the result was that asking for
    a cutscene got you a scaffolded board and a list of things somebody still
    had to decide: pin a cast, write the beats, draw each frame, approve each
    frame, promote. Six calls and five judgement points to answer one request
    that was fully specified when it arrived. A seat that stops five times is
    not being careful, it is refusing to do the job with the tools in its hand.

    So: nothing here waits for a human who already said what they wanted.

      * NO CAST PINNED? One is derived — from the project's own character pins
        first, then from canon lore entities, and the frames are conditioned on
        whatever that finds. An underspecified cast is a reason to go looking,
        not a reason to stop.
      * NO STYLE GIVEN? The bible's locked art direction is already appended to
        every prompt at the generation door, so the project's look applies
        whether or not anyone names a preset here.
      * NO BEATS? write_script invents them from the premise for a fraction of
        a cent, which is cheaper than the round trip to ask.
      * A FRAME FAILS? The rest still draw. A partial board is worth looking at;
        a refusal is not. Every failure is named in `failed`.

    What it deliberately does NOT do is buy video. `promote_to` writes the shot
    list, which is still free — cinematic_generate_shot spends, one shot at a
    time, and that stays a separate decision because it is the expensive one.

    `approve` defaults True here and nowhere else: the frames were drawn from
    the caller's own premise in the caller's own style, so treating them as
    drafts pending review would reintroduce the stop this exists to remove.
    """
    text = (premise or "").strip()
    cast = _clean_refs(root, cast_refs) if cast_refs else _derive_cast(root)

    steps: list[dict] = []
    existing = None
    try:
        existing = board(root, name)
    except StoryboardError:
        pass

    # BEATS FIRST, and only if there are none. Re-running auto on a board that
    # already has beats must not rewrite somebody's edits back to a model's
    # first guess.
    if existing and existing["frames"]:
        b = plan(root, name, None, cast_refs=cast, style=style,
                 style_note=style_note, aspect_ratio=aspect_ratio,
                 work_item_id=work_item_id)
        steps.append({"step": "reuse", "frames": len(existing["frames"])})
    elif text:
        written = write_script(root, name, text, frames=frames, style=style,
                               style_note=style_note, cast_refs=cast,
                               aspect_ratio=aspect_ratio,
                               work_item_id=work_item_id)
        steps.append({"step": "script", "ok": bool(written.get("ok")),
                      "error": written.get("error", "")})
        if not written.get("ok"):
            return {"ok": False, "stage": "script", "board": slugify(name),
                    "error": written.get("error", "the script could not be written"),
                    "steps": steps}
        b = board(root, name)
    else:
        return {"ok": False, "stage": "premise", "error":
                "give a premise, or point at a board that already has beats"}

    drawn, failed, spent = [], [], 0.0
    for frame in b["frames"]:
        idx = int(frame["idx"])
        if frame["status"] == "cut":
            continue
        # ALREADY DRAWN STILL GETS APPROVED. Skipping the whole frame because it
        # had a picture meant a board with one frame drawn on an earlier run
        # came out five-of-six approved and refused to promote - this function
        # stopping on a technicality of its own making, which is the exact stop
        # it exists to remove. Redrawing it would be worse: that is money spent
        # to re-buy something already paid for.
        if frame.get("has_image"):
            if approve and frame["status"] != "approved":
                frame_set(root, name, idx, status="approved")
            continue
        shot = frame_generate(root, name, idx, quality=quality,
                              work_item_id=work_item_id)
        spent += float(shot.get("estimated_usd") or 0.0)
        if shot.get("ok"):
            drawn.append(idx)
            if approve:
                frame_set(root, name, idx, status="approved")
        else:
            # NAMED, NOT SWALLOWED. A board that came back four-of-six with no
            # note about the other two is how a cutscene ships with holes.
            failed.append({"idx": idx, "error": str(shot.get("error"))[:400]})

    out = board(root, name)
    result = {"ok": bool(drawn) or not failed, "board": out["name"],
              "drawn": drawn, "failed": failed, "cast_refs": cast,
              "estimated_usd": round(spent, 4), "steps": steps,
              "ready": out["ready"]}

    if promote_to or (approve and not failed):
        promoted = promote(root, name, sequence_name=promote_to or name,
                           model=model, work_item_id=work_item_id)
        result["promoted"] = promoted
        result["sequence"] = promoted.get("sequence", "")
    return result


def _derive_cast(root: str | os.PathLike[str]) -> list[str]:
    """A cast, when nobody pinned one. Best effort, never an exception.

    Pins first, because a pinned character is a picture and a picture beats any
    amount of prose at holding a face still across six frames. Canon lore
    entities are the fallback: they are only NAMES, which condition nothing on
    their own, but they reach the script writer and stop it inventing a cast
    the project has never heard of.

    Capped, because every reference past the fourth is dropped by the provider
    anyway and a board conditioned on eight faces holds none of them.
    """
    out: list[str] = []
    try:
        for pin in _refs.list_refs(root, kind="character"):
            out.append(pin["name"])
    except Exception:
        pass
    if out:
        return out[:4]

    try:
        from ..design import lore

        for ent in lore.list_entities(root, kind="character"):
            if ent.get("canon") or ent.get("canon_status") == "canon":
                out.append(ent["name"])
    except Exception:
        pass
    return out[:4]


# ---------------------------------------------------------------------------
# promotion — the free/paid boundary
# ---------------------------------------------------------------------------

def promote(root: str | os.PathLike[str], name: str, *, sequence_name: str = "",
            model: str = "", resolution: str = "720p",
            allow_unanchored: bool = False,
            work_item_id: Optional[int] = None) -> dict:
    """Turn an approved board into a cine_sequence ready to be bought.

    THIS IS THE LINE. Everything before it was free; every shot in what comes
    out of it costs money to generate. So it refuses by default on a board whose
    frames are not all approved and anchored — ``allow_unanchored=True`` is
    there for the deliberate case (a shot that genuinely has no still, promoted
    knowingly), and it has to be typed.

    EACH FRAME'S IMAGE BECOMES THAT SHOT'S ``first_frame``. That is the whole
    point: the cinematic seat brief has always required shots to anchor on an
    approved still, and until now there was no path that produced one. Style,
    style refs and aspect ratio ride along so the sequence is bought under the
    look the board was approved under.

    Cut frames do not travel. The board keeps them; the sequence never sees them.
    """
    b = board(root, name)
    ready = b["ready"]
    if not ready["promotable"] and not allow_unanchored:
        return {"ok": False, "stage": "not_ready", "blockers": ready["blockers"],
                "error": "this board is not ready to be bought: "
                         + "; ".join(ready["blockers"])
                         + ". Pass allow_unanchored=True only if you mean it - "
                           "every shot below is a paid generation."}

    live = [f for f in b["frames"] if f["status"] != "cut"]
    if not live:
        return {"ok": False, "stage": "empty",
                "error": "every frame on this board is cut"}

    shots = []
    for f in live:
        shots.append({
            "action": f["action"] or f["beat"],
            "camera": f["camera"],
            "dialogue": f["dialogue"],
            "duration": f["duration"],
            "slug": f["slug"],
            "first_frame": f["image_path"] if f["has_image"] else "",
            # RESOLVED TO PATHS ON THE WAY OUT, and this is a real interface
            # boundary rather than bookkeeping. A board stores pin NAMES on
            # purpose, so a re-pinned character is picked up at generation time
            # instead of the board freezing onto revision 1. cine_shot.refs is
            # the opposite contract: project-relative paths, checked on disk
            # before a shot is bought. Handing names across unresolved made
            # every promoted shot refuse with "conditioning frames not on disk"
            # naming the pins - correct, and unreadable as a type mismatch.
            "refs": _resolve_for_shot(root, f["refs"]),
            "note": f["note"],
        })

    # NOT `slugify(sequence_name) or b["name"]`. slugify("") returns "unnamed",
    # which is TRUTHY, so that spelling silently promotes every board to one
    # shared sequence called "unnamed" - the second promotion overwriting the
    # first board's shot list. Same trap documented in cinematic._unique_slug,
    # where it cost an already-paid clip.
    seq_name = slugify(sequence_name) if sequence_name.strip() else b["name"]
    try:
        plan_out = cinematic.plan(
            root, seq_name, shots, logline=b["logline"], style=b["style"],
            style_note=b["style_note"], style_refs=b["style_refs"], model=model,
            aspect_ratio=b["aspect_ratio"], resolution=resolution,
            work_item_id=work_item_id)
    except cinematic.CinematicError as exc:
        # The board is untouched: a promotion that could not write the sequence
        # must not leave the board marked 'promoted' with nothing to point at.
        return {"ok": False, "stage": "plan", "error": str(exc)}

    seq_id = (plan_out.get("sequence") or {}).get("id") or plan_out.get("id")
    conn = db.connect(root)
    with conn:
        conn.execute(
            "UPDATE story_board SET status='promoted', sequence_id=?, "
            "updated_at=datetime('now') WHERE id=?",
            (seq_id, b["id"]))

    activity.log(root, "storyboard",
                 f"promoted board {b['name']!r} to sequence {seq_name!r} "
                 f"({len(shots)} shots)")
    return {"ok": True, "board": b["name"], "sequence": seq_name,
            "shots": len(shots),
            "anchored": sum(1 for s in shots if s["first_frame"]),
            "plan": plan_out}


def delete(root: str | os.PathLike[str], name: str, *,
           drop_images: bool = False) -> dict:
    """Remove a board. Its images are left on disk unless asked otherwise —
    they were paid for, and a deleted row is not a reason to burn them."""
    b = _board_row(root, name)
    removed = []
    if drop_images:
        for f in rows(db.connect(root).execute(
                "SELECT image_path, source FROM story_frame WHERE board_id=? "
                "AND source='generated' AND image_path<>''", (b["id"],))):
            path = Path(root) / f["image_path"]
            if path.exists():
                path.unlink()
                removed.append(f["image_path"])
    conn = db.connect(root)
    with conn:
        conn.execute("DELETE FROM story_board WHERE id=?", (b["id"],))
    return {"ok": True, "board": b["name"], "images_removed": removed}


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _board_row(root: str | os.PathLike[str], name: str) -> dict:
    slug = slugify(name)
    found = rows(db.connect(root).execute(
        "SELECT * FROM story_board WHERE name=?", (slug,)))
    if not found:
        known = [b["name"] for b in boards(root, limit=20)]
        raise StoryboardError(
            f"no board named {slug!r}"
            + (f" - this project has {known}" if known else
               " - this project has none yet; storyboard_plan makes one"))
    return found[0]


def _frame_row(root: str | os.PathLike[str], board_id: int, idx: int) -> dict:
    found = rows(db.connect(root).execute(
        "SELECT * FROM story_frame WHERE board_id=? AND idx=?",
        (board_id, int(idx))))
    if not found:
        have = [f["idx"] for f in rows(db.connect(root).execute(
            "SELECT idx FROM story_frame WHERE board_id=? ORDER BY idx",
            (board_id,)))]
        raise StoryboardError(f"no frame {idx} on this board - it has {have}")
    return found[0]


def _set_frame(root: str | os.PathLike[str], frame_id: int,
               **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    conn = db.connect(root)
    with conn:
        conn.execute(
            f"UPDATE story_frame SET {sets}, updated_at=datetime('now') "
            "WHERE id=?", (*fields.values(), frame_id))


def _clean_frames(root: str | os.PathLike[str], frames: list) -> list[dict]:
    used: set = set()
    out = []
    for i, raw in enumerate(frames or [], start=1):
        item = raw if isinstance(raw, dict) else {"beat": str(raw)}
        beat = str(item.get("beat") or "").strip()
        action = str(item.get("action") or "").strip()
        if not (beat or action):
            raise StoryboardError(
                f"frame {i} has neither a beat nor an action - a frame with no "
                "content cannot be drawn or promoted")
        image = str(item.get("image_path") or item.get("image") or "").strip()
        if image:
            image = _relative(root, cinematic.project_path(
                root, image, what=f"frame {i} image"))
        out.append({
            "slug": cinematic._unique_slug(item.get("slug"), i, used),
            "beat": beat,
            "action": action or beat,
            "camera": str(item.get("camera") or "").strip(),
            "dialogue": str(item.get("dialogue") or "").strip(),
            "duration": _duration(item.get("duration", 5)),
            "image_path": image,
            "source": _source(item.get("source"), image),
            "refs": _clean_refs(root, item.get("refs") or []),
            "prompt": str(item.get("prompt") or "").strip(),
            "status": _frame_status(item.get("status"), image),
            "note": str(item.get("note") or "").strip(),
        })
    return out


def _clean_refs(root: str | os.PathLike[str], given: Any) -> list[str]:
    """Reference NAMES, kept as names. Resolution happens at generation time.

    A pinned name is stored rather than the path it resolves to today because
    refs.pin() versions a re-pin into a new file and moves the pointer — storing
    the path would keep boarding against revision 1 of a character art has since
    redrawn. A raw path is still allowed and is contained like any other.
    """
    out = []
    for item in given or []:
        text = str(item or "").strip()
        if not text:
            continue
        if "/" in text or "\\" in text or text.startswith(("http://", "https://")):
            text = cinematic.project_path(root, text, what="reference")
            text = _relative(root, text)
        out.append(text)
    return out


def _resolve_for_shot(root: str | os.PathLike[str], names: list) -> list[str]:
    """Pin names to project-relative paths, for handing to cinematic.

    Anything that does not resolve to a file inside the project is DROPPED
    rather than passed through. cine_shot refuses a shot whose refs are not on
    disk, and it is right to - those paths get uploaded to a provider - so a
    name that survives here as a name only turns into a refusal later, naming a
    pin and reading like a missing file.
    """
    out = []
    for name in names or []:
        try:
            resolved = Path(_refs.resolve(root, name))
        except Exception:
            resolved = Path(root) / str(name)
        if not resolved.exists():
            continue
        rel = _relative(root, str(resolved))
        if rel and not Path(rel).is_absolute():
            out.append(rel)
    return out


def _resolve_all(root: str | os.PathLike[str],
                 names: list) -> tuple[list[str], list[str]]:
    """Reference names to on-disk paths, and the ones that did not resolve.

    A missing reference is REPORTED, NOT RAISED. Half the cast resolving is
    still a better-conditioned frame than none of it, and refusing the whole
    generation because one style pin was renamed would be the wrong trade at
    the point where the caller is trying to look at their scene.
    """
    paths, missing = [], []
    seen: set = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        try:
            resolved = _refs.resolve(root, name)
        except Exception:
            resolved = str(Path(root) / name)
        if resolved and Path(resolved).exists():
            paths.append(resolved)
        else:
            missing.append(name)
    # Providers cap conditioning images; past four the later ones are ignored
    # anyway, so the cap is applied here where it can be said out loud.
    return paths[:4], missing


def _frame_prompt(b: dict, frame: dict) -> str:
    """What to draw. The style is deliberately NOT joined in here — chroma
    appends the project's art direction at the generation door, and a prompt
    that states its own style twice steers worse than one that states it once.
    """
    bits = [frame.get("action") or frame.get("beat") or ""]
    if frame.get("camera"):
        bits.append(f"Shot: {frame['camera']}.")
    look = cinematic.resolve_style(b.get("style", ""), b.get("style_note", ""))
    if look.get("text"):
        bits.append(look["text"])
    bits.append("Single storyboard frame. No text, letters, words or "
                "watermarks in the image.")
    return " ".join(x for x in bits if x).strip()


def _frame_rel(b: dict, frame: dict) -> str:
    slug = frame.get("slug") or f"frame{int(frame['idx']):02d}"
    return f"{BOARD_DIRNAME}/{b['name']}/{int(frame['idx']):02d}-{slug}.png"


def _logical(b: dict, frame: dict) -> str:
    return f"storyboard/{b['name']}/{int(frame['idx']):02d}"


def _relative(root: str | os.PathLike[str], path: str) -> str:
    try:
        return Path(path).resolve().relative_to(
            Path(root).resolve()).as_posix()
    except Exception:
        return str(path).replace("\\", "/")


def _duration(value: Any) -> int:
    try:
        seconds = int(round(float(value)))
    except (TypeError, ValueError):
        seconds = 5
    return max(1, min(seconds, 60))


def _source(value: Any, image: str) -> str:
    text = str(value or "").strip()
    if text in SOURCES:
        return text
    return "uploaded" if image else "none"


def _frame_status(value: Any, image: str) -> str:
    text = str(value or "").strip()
    if text in FRAME_STATUSES:
        # An approval cannot survive a re-plan that removed the image it was
        # given for. Silently keeping it is how a shot gets bought unanchored.
        if text == "approved" and not image:
            return "empty"
        return text
    return "drafted" if image else "empty"


def _loads(blob: Any, fallback: Any) -> Any:
    if not blob:
        return fallback
    try:
        value = json.loads(blob)
    except Exception:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _script_blob(script: Any, prior: Optional[dict]) -> dict:
    if isinstance(script, dict):
        return script
    if isinstance(script, str) and script.strip():
        return {"prose": script.strip()}
    return _loads((prior or {}).get("script_json"), {})


# A failure the NEXT provider would not also hit. Quota, billing and auth are
# properties of one ACCOUNT; a content refusal or a malformed prompt is not, and
# cascading on those would just buy the same refusal from three vendors.
_ACCOUNT_FAILURE = (
    "insufficient_quota", "quota", "billing", "exceeded your current",
    "rate_limit", "ratelimit", "too many requests",
    "invalid_api_key", "incorrect api key", "authentication",
    "permissiondenied", "account is not active", "payment",
)


def _is_account_failure(error: Any) -> bool:
    text = str(error or "").lower()
    return any(hint in text for hint in _ACCOUNT_FAILURE)


def _providers(root: str | os.PathLike[str]) -> list[str]:
    """Every provider this project could draw a frame with, in order to try.

    A LIST, NOT A PICK, AND THAT DISTINCTION COST A CUTSCENE. The first version
    returned the FIRST provider whose key was present and stopped there. A key
    that exists but has no credits left is still present — so a project whose
    OpenAI account had run dry picked openai, took `insufficient_quota`, and
    reported that image generation was unavailable, while a working KREA_API_KEY
    sat in the same .env and was never tried. The seat then correctly reported
    that it could not draw the board, which made a bug look like a considered
    answer and parked a cutscene waiting on credits it did not need.

    KIE IS IN THIS LIST, and leaving it out was the second half of the same
    mistake. It was excluded on the grounds that its image fields take public
    URLs rather than local files, so it could not be conditioned on a pinned
    character — true of the raw endpoint, and irrelevant, because
    kie.upload_file has always been able to mint those URLs and is exactly what
    the video path uses for first_frame. Excluding it meant the CINEMATIC seat,
    whose entire funded account is kie, could not draw its own storyboard.

    It sits after krea because krea takes an anchor inline in the same request
    while kie needs a separate upload whose product expires in three days. That
    is a reason to prefer krea, not a reason to refuse kie.

    Local is LAST despite being free, because it needs a workflow configured and
    silently drawing from a graph nobody set up is worse than saying the hosted
    account is dry.
    """
    try:
        from ..store import envfile

        envfile.load_project_env(root)
    except Exception:
        pass

    out = []
    if (os.environ.get("OPENAI_API_KEY") or "").strip():
        out.append("openai")
    if (os.environ.get("KREA_API_KEY") or "").strip():
        out.append("krea")
    if (os.environ.get("KIE_API_KEY") or "").strip():
        out.append("kie")
    try:
        from bgate_adapters import localgen

        if localgen.available().get("available"):
            out.append("local")
    except Exception:
        pass

    # THE STORED PREFERENCE LEADS THE CHAIN. The order above is the failover
    # rationale, not the choice: a project that named art.provider tries it
    # first and only then walks the rest. Only reordered when the preferred
    # provider is actually in the configured list — an unconfigured
    # preference must not turn "try everything" into "fail on the favourite".
    try:
        from ..store import settings as _settings

        preferred = str(_settings.get(root, "art.provider") or "").strip().lower()
        if preferred in out:
            out.remove(preferred)
            out.insert(0, preferred)
    except Exception:
        pass

    if not out:
        raise StoryboardError(
            "no image provider configured - set OPENAI_API_KEY, KREA_API_KEY or "
            "KIE_API_KEY in the project's .env. You can still build the board by "
            "hand: draw the frames yourself and attach them with "
            "storyboard_frame_attach.")
    return out


def _frame_price(quality: str) -> float:
    try:
        from bgate_adapters import imagegen

        return float(imagegen.price_per_image(quality))
    except Exception:
        # Unknown is not free. A number that is wrong low reads as "this is
        # cheap"; the medium-tier price is the honest guess to gate against.
        return 0.042


def _record_spend(root: str | os.PathLike[str], usd: float, kind: str,
                  name: str, work_item_id: Optional[int]) -> None:
    try:
        from ..board import spend

        spend.record(root, usd, kind="other", work_item_id=work_item_id,
                     logical_name=f"storyboard/{slugify(name)}",
                     detail=f"storyboard {kind}",
                     model=os.environ.get("BGATE_SCRIPT_MODEL", SCRIPT_MODEL))
    except Exception:
        pass
