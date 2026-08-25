"""What the PLAYER sees when they press play — captured, and ASSERTED ABOUT.

THE FAILURE THIS IS WRITTEN AGAINST. A 3D benchmark game shipped with
``application/run/main_scene`` still pointing at the scaffold's demo room while
every named-scene test in the project passed. Each test named the scene it
tested; none of them named the one the game boots into, because naming it was
never anybody's step. The build was green, the export worked, and the first
frame a player would ever have seen was a grey box with a capsule in it.

Named-scene evidence cannot substitute for this, and the reason is structural
rather than accidental: every gate that names its own scene is a gate that
cannot notice the scene it was not told about. So the default-scene capture
takes NO scene argument. It launches exactly what ``godot <project>`` launches.

CAPTURING EVIDENCE IS NOT EXAMINING IT — the second half, and the more
expensive one. The same benchmark shipped a character with two tails for a full
day WITH TURNAROUND RENDERS AND A CONTACT SHEET ALREADY ON DISK. Nobody looked
at the rear. Every defect that reached the player was found by a human looking
at a frame, and several of those frames already existed. A gate that records
"beauty.png exists, 412 KB" has recorded the one thing that was never in doubt.

So a proof here is a capture PLUS an explicit assertion about what is IN the
image, made by whoever looked, and stored with the frame it was made against.
An assertion is prose — no static check can read a picture for you — but it is
prose that is DATED, ATTRIBUTED and BOUND TO A FILE DIGEST. Change the frame
and the assertion stops applying to it, which is the property that makes it
worth anything at all.

FOUR CARDINAL VIEWS FOR A CHARACTER, for the same reason: the forked tail was
invisible from every angle anybody had rendered. ``views`` records which of
front/back/left/right an assertion covers, and :func:`character_gaps` names the
ones nobody has spoken about.

SCAFFOLD DETECTION IS A CLAIM ABOUT THE SCENE FILE, not about the picture. The
templates this harness ships stamp a marker node name and a known path; a main
scene that still resolves to one of those is the scaffold, whatever the frame
looks like. Stated rather than inferred, because an inferred version of this
check would be a pixel heuristic that fails on any game whose art is grey.

STORAGE. The workspace doc store (seat ``qa``, key ``scene_proof``), beside
every other per-project seat setting. One doc, keyed by scene path.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import activity, workspace as _ws

SEAT = "qa"
DOC_KEY = "scene_proof"

#: The four views a character has to be seen from before anybody may say what
#: it looks like. Named here because "a turnaround" meant three views in one
#: run and six in another, and the missing one was the back.
CARDINALS = ("front", "back", "left", "right")

#: Scene paths and root-node names the scaffolds ship with. A main scene that
#: is still one of these has never been pointed at the game.
SCAFFOLD_SCENES = (
    "res://scenes/main.tscn",
    "res://scenes/demo.tscn",
    "res://demo.tscn",
    "res://main.tscn",
)
SCAFFOLD_MARKERS = ("BGateDemo", "ScaffoldDemo", "TemplateRoot")

#: How long an assertion is trusted before it is worth restating. Not an
#: expiry — the digest binding is the real guard — but a gate reports an
#: assertion older than this alongside the frame's age so a reader can tell a
#: fresh look from an inherited one.
STALE_DAYS = 30

MAX_TEXT = 2000


class NoDefaultScene(ValueError):
    """The project declares no main scene, so there is nothing to boot."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _doc(root: str | os.PathLike[str]) -> dict:
    try:
        got = _ws.get(root, SEAT, DOC_KEY, {}) or {}
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def _save(root: str | os.PathLike[str], doc: dict) -> dict:
    clean = {k: v for k, v in doc.items() if k != _ws.VERSION_KEY}
    _ws.set(root, SEAT, DOC_KEY, clean)
    return clean


def digest(path: str | os.PathLike[str]) -> str:
    """SHA-256 of a captured frame, short. '' when it cannot be read.

    THE BINDING. An assertion about an image is worth exactly as much as the
    guarantee that the image has not changed since; without this the sentence
    "the cat has one tail" outlives the model it was true of.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()[:16]


# ── what the project actually boots ─────────────────────────────────────────

_MAIN_SCENE_RE = re.compile(r'^run/main_scene\s*=\s*"([^"]*)"', re.MULTILINE)


def declared_scene(game_dir: str | os.PathLike[str]) -> str:
    """``application/run/main_scene`` as project.godot declares it, or ''."""
    try:
        text = (Path(game_dir) / "project.godot").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = _MAIN_SCENE_RE.search(text)
    return found.group(1) if found else ""


def _scene_file(game_dir: Path, res_path: str) -> Optional[Path]:
    if not res_path.startswith("res://"):
        return None
    return game_dir / res_path[len("res://"):]


def is_scaffold(game_dir: str | os.PathLike[str], res_path: str) -> dict:
    """Is this scene the template's demo rather than the game? {scaffold, why}.

    A claim about the FILE. The alternative — deciding from the picture —
    would be a pixel heuristic, and a pixel heuristic that fires on grey would
    fail every graybox in the world.
    """
    if not res_path:
        return {"scaffold": False, "why": ""}
    if res_path.lower() in SCAFFOLD_SCENES:
        # The path alone is not proof — a real game may well own res://main.tscn
        # — so this only counts when the file also carries a template marker.
        pass
    path = _scene_file(Path(game_dir), res_path)
    if path is None or not path.is_file():
        return {"scaffold": False, "why": ""}
    try:
        body = path.read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return {"scaffold": False, "why": ""}
    for marker in SCAFFOLD_MARKERS:
        if marker in body:
            return {"scaffold": True,
                    "why": f"{res_path} still carries the scaffold marker "
                           f"{marker!r} — this is the template's demo room, "
                           "not the game"}
    return {"scaffold": False, "why": ""}


def default_scene_state(root: str | os.PathLike[str]) -> dict:
    """Everything knowable about the boot scene WITHOUT running the engine.

    Returns ``{ok, scene, exists, scaffold, why, game_dir}``. Cheap; the
    engine capture below is what costs, and this is what decides whether
    spending it is even worth doing.
    """
    from . import project as _project

    base = _project.game_dir(root)
    if base is None:
        return {"ok": False, "scene": "", "exists": False, "scaffold": False,
                "game_dir": "",
                "why": "no project.godot was found, so this project has no "
                       "runtime to boot"}
    base = Path(base)
    scene = declared_scene(base)
    if not scene:
        return {"ok": False, "scene": "", "exists": False, "scaffold": False,
                "game_dir": str(base),
                "why": "project.godot declares no application/run/main_scene — "
                       "pressing play opens the project manager, not the game"}
    path = _scene_file(base, scene)
    exists = bool(path and path.is_file())
    scaffold = is_scaffold(base, scene)
    why = ""
    if not exists:
        why = (f"run/main_scene points at {scene}, which is not on disk — the "
               "game cannot boot at all")
    elif scaffold["scaffold"]:
        why = scaffold["why"]
    return {"ok": exists and not scaffold["scaffold"], "scene": scene,
            "exists": exists, "scaffold": bool(scaffold["scaffold"]),
            "game_dir": str(base), "why": why}


# ── the capture, and what may be said about it ──────────────────────────────

def capture_default(root: str | os.PathLike[str], *, at: float = 1.5,
                    timeout: int = 120, out_dir: str = "") -> dict:
    """Launch EXACTLY the project default and capture the frame.

    No ``scene`` argument, on purpose and permanently. The whole value of this
    call is that it cannot be pointed somewhere more convenient.
    """
    from bgate_adapters import godot as _godot

    state = default_scene_state(root)
    if not state["game_dir"]:
        return {"ok": False, "error": state["why"], **state}
    if not state["scene"]:
        return {"ok": False, "error": state["why"], **state}
    if not state["exists"]:
        return {"ok": False, "error": state["why"], **state}

    out = Path(out_dir) if out_dir else (
        Path(root) / ".bgate" / "evidence" / "default-scene")
    got = _godot.evidence(state["game_dir"], str(out), at=at, timeout=timeout)
    got.setdefault("scene", state["scene"])
    got["declared_scene"] = state["scene"]
    got["scaffold"] = state["scaffold"]
    if state["scaffold"]:
        got["ok"] = False
        got["error"] = state["why"]
    if got.get("beauty"):
        got["beauty_digest"] = digest(got["beauty"])
    record_capture(root, state["scene"], got)
    return got


def record_capture(root: str | os.PathLike[str], scene: str,
                   capture: dict) -> dict:
    """Remember that a frame was taken, and of what. Never raises."""
    doc = _doc(root)
    scenes = doc.get("scenes")
    doc["scenes"] = scenes if isinstance(scenes, dict) else {}
    row = doc["scenes"].get(scene)
    row = row if isinstance(row, dict) else {}
    row["last_capture"] = {
        "at": _now(),
        "ok": bool(capture.get("ok")),
        "beauty": str(capture.get("beauty") or ""),
        "digest": str(capture.get("beauty_digest")
                      or digest(capture.get("beauty") or "")),
        "entities": int((capture.get("counts") or {}).get("entities", 0)),
        "ui": int((capture.get("counts") or {}).get("ui", 0)),
        "error": str(capture.get("error") or "")[:400],
        "scaffold": bool(capture.get("scaffold")),
    }
    doc["scenes"][scene] = row
    try:
        _save(root, doc)
    except Exception:                                             # noqa: BLE001
        pass
    return row["last_capture"]


def assert_content(root: str | os.PathLike[str], scene: str, *, frame: str,
                   says: str, by: str = "", views: Optional[list] = None,
                   subject: str = "") -> dict:
    """Record what somebody SAW in a frame. The half nobody was doing.

    ``says`` is prose and has to be: no static check reads a picture. What
    makes it evidence rather than a wish is everything around it — the file, its
    digest at the moment of the claim, who claimed it, and when. If the frame is
    regenerated the digest moves and :func:`assertions_for` reports the claim as
    stale rather than carrying it forward.

    Refuses a claim that names no frame, an unreadable frame, or a sentence
    short enough to be a shrug. "looks fine" is the assertion that let a
    two-tailed cat ship.
    """
    says = " ".join(str(says or "").split())
    if len(says) < 15:
        raise ValueError(
            "an assertion about an image has to say what is IN it — name the "
            "thing you checked and what you saw. 'looks fine' is the sentence "
            "a two-tailed character shipped under.")
    if len(says) > MAX_TEXT:
        says = says[:MAX_TEXT]
    frame_path = Path(frame)
    if not frame_path.is_file():
        raise FileNotFoundError(
            f"{frame} is not a file — an assertion has to be bound to the "
            "frame it is about, or it outlives what it was true of")
    bad = [v for v in (views or []) if v not in CARDINALS]
    if bad:
        raise ValueError(f"views are {CARDINALS}; got {bad!r}")
    row = {
        "at": _now(),
        "frame": str(frame_path),
        "digest": digest(frame_path),
        "says": says,
        "by": str(by or activity.current_actor() or "")[:120],
        "views": list(views or []),
        "subject": str(subject or "")[:200],
    }
    doc = _doc(root)
    scenes = doc.get("scenes")
    doc["scenes"] = scenes if isinstance(scenes, dict) else {}
    held = doc["scenes"].get(scene)
    held = held if isinstance(held, dict) else {}
    claims = held.get("assertions")
    held["assertions"] = (claims if isinstance(claims, list) else [])[-40:]
    held["assertions"].append(row)
    doc["scenes"][scene] = held
    _save(root, doc)
    activity.log(root, "evidence",
                 f"asserted about {scene}: {says[:120]}", seat=SEAT, ref=scene)
    return row


def assertions_for(root: str | os.PathLike[str], scene: str) -> list[dict]:
    """Every claim made about this scene, newest first, marked live or stale.

    An assertion is STALE when the frame it names is gone or its bytes have
    moved. That is not a failure of the person who made it; it is the whole
    point of binding it to a digest.
    """
    held = (_doc(root).get("scenes") or {}).get(scene)
    rows = (held or {}).get("assertions") if isinstance(held, dict) else []
    out: list[dict] = []
    for row in reversed(rows if isinstance(rows, list) else []):
        current = digest(row.get("frame") or "")
        out.append({**row, "live": bool(current) and current == row.get("digest"),
                    "why_stale": ("" if current == row.get("digest") else
                                  "the frame this was said about has changed "
                                  "or is gone, so the claim no longer applies "
                                  "to anything")})
    return out


def character_gaps(root: str | os.PathLike[str], subject: str,
                   scene: str = "") -> list[str]:
    """Which cardinal views nobody has spoken about for one character.

    THE TWO-TAILED CAT CHECK. Front, left and right were rendered and looked
    at; the back was rendered and never opened. A character is not reviewed
    until somebody has said something about all four.
    """
    seen: set[str] = set()
    scenes = (_doc(root).get("scenes") or {})
    keys = [scene] if scene else list(scenes)
    for key in keys:
        for row in assertions_for(root, key):
            if not row.get("live"):
                continue
            if subject and str(row.get("subject") or "") != subject:
                continue
            seen.update(row.get("views") or [])
    return [v for v in CARDINALS if v not in seen]


# ── the release gate's question ─────────────────────────────────────────────

def unproven(root: str | os.PathLike[str]) -> list[dict]:
    """What the DEFAULT SCENE still owes, as findings with provenance.

    Rows carry the tool that would clear them and the measurement behind them,
    because a gate row with neither is how a false blocker becomes permanent.
    """
    from . import findings as _findings

    out: list[dict] = []
    state = default_scene_state(root)
    if not state["game_dir"]:
        return out                       # not a Godot project; not our question

    if not state["scene"] or not state["exists"] or state["scaffold"]:
        out.append(_findings.make(
            gate="default_scene", key="run/main_scene",
            kind=_findings.BLOCKING,
            claim=state["why"] or "the project's default scene is not the game",
            tool="sceneproof.default_scene_state",
            inputs={"project": state["game_dir"]},
            measured={"run/main_scene": state["scene"],
                      "exists": state["exists"],
                      "scaffold": state["scaffold"]},
            clears_by=("point application/run/main_scene at the scene the "
                       "player is meant to boot into, then "
                       "godot_evidence() with NO scene argument")))
        return out

    scene = state["scene"]
    held = (_doc(root).get("scenes") or {}).get(scene) or {}
    capture = held.get("last_capture") if isinstance(held, dict) else None
    if not isinstance(capture, dict) or not capture.get("ok"):
        out.append(_findings.make(
            gate="default_scene", key=f"capture:{scene}",
            kind=_findings.BLOCKING,
            claim=(f"nobody has captured {scene} — the scene the game actually "
                   "boots into. Every named-scene test in this project names a "
                   "scene somebody chose; this one is the scene the player gets"),
            tool="sceneproof.capture_default",
            inputs={"scene": scene},
            measured={"captured": bool(capture),
                      "error": (capture or {}).get("error", "")},
            clears_by="godot_evidence() with no scene argument"))
        return out

    live = [a for a in assertions_for(root, scene) if a.get("live")]
    if not live:
        out.append(_findings.make(
            gate="default_scene", key=f"assertion:{scene}",
            kind=_findings.JUDGEMENT,
            claim=(f"{scene} has been captured but nobody has said what is IN "
                   "the frame. Capturing evidence is not examining it — a "
                   "character shipped with two tails while its turnaround "
                   "renders sat on disk unopened"),
            tool="sceneproof.assert_content",
            inputs={"scene": scene, "frame": capture.get("beauty", "")},
            measured={"frame": capture.get("beauty", ""),
                      "digest": capture.get("digest", ""),
                      "entities": capture.get("entities", 0)},
            clears_by=("a person (or the QA seat) opens "
                       f"{capture.get('beauty') or 'the captured frame'} and "
                       "records evidence_assert(scene, frame, says=...) naming "
                       "what they saw")))
    return out


def state(root: str | os.PathLike[str]) -> dict:
    got = default_scene_state(root)
    scene = got.get("scene") or ""
    return {**got,
            "assertions": assertions_for(root, scene) if scene else [],
            "cardinals": list(CARDINALS),
            "unproven": [f["claim"] for f in unproven(root)]}
