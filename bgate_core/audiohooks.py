"""Which sounds the GAME asks for, and which file answers.

A directory listing tells you what audio exists. It cannot tell you the thing
that decides whether anyone hears it, which is whether a line of game code ever
asks for it. Those are different failures with different fixes: an ORPHAN file
is wasted work, an UNBOUND event is silence at the moment the game meant to make
a noise, and only the second one is a bug in the build.

So the hook table is built from the CALL SITES, not from the disk. This walks the
project's own scripts and scenes for the two ways Godot asks for a sound:

  * BY NAME, through a front door — ``Audio.sfx("melee_hit")``,
    ``.play_music("title")``. The convention is universal enough to scan for and
    the resolution is mechanical: the file whose stem is ``<family>_<name>`` or
    ``<name>``, in the project's audio directories.
  * BY PATH — a ``res://…wav`` literal in a script, a scene or a resource. That
    binding cannot fail to resolve at the name level, but the file it names can
    be missing, which is the same silence by another route.

WHAT IS NOT INFERRED. A call whose argument is a variable (``Audio.sfx(pick())``)
is a real call site with an unknowable name; it is reported as a dynamic call
with its location, and NOT expanded into guessed events. A project with no front
door produces no rows and says so — an empty table here means the scan found
nothing, and the seat's empty state names the scan rather than pretending the
game is silent.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional

#: What a sound file can be.
AUDIO_SUFFIXES = frozenset({".wav", ".ogg", ".mp3"})

#: Text the scan reads. .gd is where the calls are; .tscn/.tres carry direct
#: stream bindings that never appear in any script.
SOURCE_SUFFIXES = frozenset({".gd", ".tscn", ".tres", ".cs"})

#: Directories that are never the game. `.godot` is the import cache (it holds a
#: copy of every res:// path and would double every row); the QA scripts under
#: game/scripts/qa are harnesses that reference sounds to prove the autoload
#: compiles, not gameplay asking for them.
SKIP_DIRS = frozenset({".godot", ".git", ".bgate", ".bgate_out", "__pycache__",
                       "node_modules", "export", "build", ".import"})

#: Methods that PLAY something, mapped to the stem family their name implies.
#: Deliberately not `play` on its own: `AnimationPlayer.play("walk")` and
#: `$Overlay.play("resolved")` are the same three letters and neither is a
#: sound, and a table full of animation names is worse than an empty one.
PLAY_METHODS: dict[str, str] = {
    "sfx": "sfx",
    "play_sfx": "sfx",
    "play_sound": "sfx",
    "sound": "sfx",
    "stinger": "stinger",
    "play_stinger": "stinger",
    "music": "music",
    "play_music": "music",
    "ambience": "ambience",
    "play_ambience": "ambience",
    "voice": "voice",
    "play_voice": "voice",
}

_METHOD_ALT = "|".join(sorted(PLAY_METHODS, key=len, reverse=True))

# `Audio.sfx("melee_hit")` — a receiver, a play method, a string literal.
_CALL_LITERAL = re.compile(
    r"\b(?P<recv>[A-Za-z_][\w.$/]*)\.(?P<method>" + _METHOD_ALT + r")\s*\(\s*"
    r"(?P<q>[\"'])(?P<name>[^\"']*)(?P=q)")
# The same call with anything else inside the parens — a variable, a ternary, a
# dictionary lookup. Known to happen, unknowable in name.
_CALL_DYNAMIC = re.compile(
    r"\b(?P<recv>[A-Za-z_][\w.$/]*)\.(?P<method>" + _METHOD_ALT + r")\s*\(\s*"
    r"(?P<arg>[^\"')][^)]*)?\)")
# A direct resource path, wherever it appears.
_RES_PATH = re.compile(r"res://(?P<rel>[^\"'\s)]+\.(?:wav|ogg|mp3))")

#: Where sound assets live. Same two places the audio listing walks.
def audio_dirs(root: str | os.PathLike[str]) -> list[Path]:
    r = Path(root)
    candidates = [r / "game" / "assets" / "audio", r / "audio",
                  r / "assets" / "audio"]
    return [d for d in candidates if d.is_dir()]


def _rel(root: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return p.as_posix()


def sound_index(root: str | os.PathLike[str]) -> dict[str, str]:
    """``{stem: project-relative path}`` for every sound in the project.

    Lower-cased stems, because a call site spells the name and a filename spells
    the file and the two disagree about case more often than they disagree about
    anything else. First writer wins so a deterministic walk gives a
    deterministic table.
    """
    r = Path(root)
    out: dict[str, str] = {}
    for d in audio_dirs(r):
        for p in sorted(d.rglob("*")):
            try:
                if not p.is_file() or p.suffix.lower() not in AUDIO_SUFFIXES:
                    continue
            except OSError:
                continue
            out.setdefault(p.stem.lower(), _rel(r, p))
    return out


def _sources(root: Path, *, skip: Iterable[str] = ()) -> Iterable[Path]:
    skip_names = SKIP_DIRS | {s.lower() for s in skip}
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = sorted(cur.iterdir())
        except OSError:
            continue
        for p in entries:
            try:
                if p.is_dir():
                    if p.name.lower() not in skip_names:
                        stack.append(p)
                elif p.suffix.lower() in SOURCE_SUFFIXES:
                    yield p
            except OSError:
                continue


def _resolve(family: str, name: str, index: dict[str, str]) -> Optional[str]:
    """The file that answers ``family.name``, or None.

    ``<family>_<name>`` first because that is the convention a front door
    implements (``sfx("melee_hit")`` loads ``sfx_melee_hit``); the bare name
    second, for projects whose front door does not prefix.
    """
    for stem in (f"{family}_{name}".lower(), name.lower()):
        hit = index.get(stem)
        if hit:
            return hit
    return None


def scan(root: str | os.PathLike[str], *,
         max_bytes: int = 2_000_000) -> dict:
    """Every audio hook this project's own code asks for.

    Returns ``{events, dynamic, unresolved_paths, scanned_files, sound_count}``.
    An event is ``{event, family, name, file, state, sites:[{file,line}], n}``
    where ``state`` is "wired" (a file answers it) or "unbound" (nothing does).

    Never raises: an unreadable file is skipped, and a project with no scripts
    yields empty lists rather than an error. A workspace poll must not be able
    to take the dashboard down.
    """
    r = Path(root)
    index = sound_index(r)
    events: dict[str, dict] = {}
    dynamic: list[dict] = []
    unresolved: list[dict] = []
    scanned = 0

    for src in _sources(r):
        try:
            if src.stat().st_size > max_bytes:
                continue
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        srel = _rel(r, src)
        # Line numbers matter more than they look: "unbound" is a claim about
        # somebody's code and the seat has to be able to send them to it.
        starts = [0]
        for ch in text:
            starts.append(starts[-1] + (1 if ch == "\n" else 0))

        def line_of(pos: int) -> int:
            return text.count("\n", 0, pos) + 1

        for m in _CALL_LITERAL.finditer(text):
            family = PLAY_METHODS[m.group("method")]
            name = m.group("name").strip()
            if not name:
                # `music("")` is this project's spelling of "stop the music".
                continue
            key = f"{family}.{name}"
            row = events.setdefault(key, {
                "event": key, "family": family, "name": name,
                "file": _resolve(family, name, index), "sites": [], "n": 0,
            })
            row["n"] += 1
            if len(row["sites"]) < 6:
                row["sites"].append({"file": srel, "line": line_of(m.start())})

        for m in _CALL_DYNAMIC.finditer(text):
            arg = (m.group("arg") or "").strip()
            if not arg:
                continue
            if len(dynamic) < 40:
                dynamic.append({
                    "expr": f"{m.group('recv')}.{m.group('method')}({arg})",
                    "file": srel, "line": line_of(m.start()),
                })

        for m in _RES_PATH.finditer(text):
            rel = m.group("rel")
            target = r / "game" / rel
            if not target.is_file():
                target = r / rel
            key = f"res.{rel}"
            row = events.setdefault(key, {
                "event": key, "family": "res", "name": rel,
                "file": _rel(r, target) if target.is_file() else None,
                "sites": [], "n": 0,
            })
            row["n"] += 1
            if len(row["sites"]) < 6:
                row["sites"].append({"file": srel, "line": line_of(m.start())})
            if not target.is_file():
                unresolved.append({"path": rel, "file": srel,
                                   "line": line_of(m.start())})

    rows = []
    for row in events.values():
        row["state"] = "wired" if row["file"] else "unbound"
        rows.append(row)
    # Unbound first — the table exists to show them — then by how often the game
    # asks, because a call site hit every frame matters more than a one-off.
    rows.sort(key=lambda e: (e["state"] != "unbound", -e["n"], e["event"]))

    return {
        "events": rows,
        "dynamic": dynamic,
        "unresolved_paths": unresolved,
        "scanned_files": scanned,
        "sound_count": len(index),
    }


def orphans(root: str | os.PathLike[str], events: list[dict]) -> list[str]:
    """Sound files no event points at. Wasted work, not a broken build.

    WALKS THE DIRECTORIES, NOT THE STEM INDEX. ``sound_index`` is keyed by stem
    with first-writer-wins, because that is what a call site spelling a NAME
    needs to resolve. Deriving orphans from it inherits that collapse: a file
    whose stem was already claimed by another file is not in the index, so it
    could never appear here, so it was reported as bound. On the project this
    was written against that hid ``music_combat.wav`` (4.0 MB) behind
    ``music_combat.ogg`` and ``music_title.wav`` (2.6 MB) behind its own .ogg —
    6.6 MB of genuinely unreferenced audio counted as in use, which is the exact
    opposite of what this function exists to report.

    A shadowed duplicate is in fact the MOST orphaned kind of file: nothing can
    ever resolve to it by name, so unless an event names its full path it is
    unreachable by construction.
    """
    used = {str(e.get("file") or "").lower() for e in events}
    r = Path(root)
    found: set[str] = set()
    for d in audio_dirs(r):
        for p in sorted(d.rglob("*")):
            try:
                if not p.is_file() or p.suffix.lower() not in AUDIO_SUFFIXES:
                    continue
                rel = p.relative_to(r).as_posix()
            except (OSError, ValueError):
                continue
            if rel.lower() not in used:
                found.add(rel)
    return sorted(found)


# ── in-game listening ───────────────────────────────────────────────────────
#
# EVERYTHING ABOVE THIS LINE IS STATIC. The hook table proves a cue is wired,
# loudness proves it is normalised, the duplicate check proves it is not the
# same file twice. None of it proves it SOUNDS RIGHT, and all three can pass on
# a cue that is wrong for the moment it fires, mixed under the music, or three
# frames late. Night Shift's audio passed every file metric it had; the first
# time anybody heard the cues in context was after release.
#
# So the last audio gate is a person (or a QA seat) listening to a GAMEPLAY
# CAPTURE with the cues firing, and saying which ones they heard. Stored per
# capture, so re-recording after a mix change starts the coverage over — which
# is correct, and was not what a file-metric pass did.

LISTEN_SEAT = "audio"
LISTEN_KEY = "in_game_listen"

#: What a gameplay capture can be. A .wav of the bus is accepted: the point is
#: that the cues were heard IN CONTEXT, not that there was a picture.
CAPTURE_SUFFIXES = frozenset({".mp4", ".mkv", ".webm", ".mov", ".ogv",
                              ".wav", ".ogg", ".mp3"})


def _listen_doc(root: str | os.PathLike[str]) -> dict:
    from bgate_core import workspace as _ws

    try:
        got = _ws.get(root, LISTEN_SEAT, LISTEN_KEY, {}) or {}
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def listen_record(root: str | os.PathLike[str], *, capture: str,
                  cues: list[str], verdict: str, notes: str,
                  by: str = "") -> dict:
    """Record an in-game listening pass over a gameplay capture.

    ``capture`` must exist and must be a recording — the check is the point,
    because "I listened to it" with nothing behind it is what a file-metric
    pass already was.
    """
    import time

    from bgate_core import activity as _activity, workspace as _ws

    capture = str(capture or "").strip()
    path = Path(capture)
    if not path.is_absolute():
        path = Path(root) / capture
    if not path.is_file():
        raise ValueError(
            f"{capture!r} is not a file under this project — an in-game "
            "listening pass is a pass over a RECORDING, and without one this "
            "is the file-metric review again")
    if path.suffix.lower() not in CAPTURE_SUFFIXES:
        raise ValueError(
            f"{capture} is not a capture ({', '.join(sorted(CAPTURE_SUFFIXES))})")
    verdict = str(verdict or "").strip().lower()
    if verdict not in ("pass", "fail"):
        raise ValueError("verdict is 'pass' or 'fail'")
    notes = " ".join(str(notes or "").split())[:2000]
    if len(notes) < 20:
        raise ValueError(
            "say what you heard. A cue can be perfectly normalised and still "
            "be wrong for the moment it fires, and that sentence is the only "
            "place that ever gets written down.")
    heard = sorted({str(c).strip() for c in (cues or []) if str(c).strip()})
    if not heard:
        raise ValueError(
            "name the cues you heard firing — a listening pass that covers no "
            "cue covers nothing")
    doc = _listen_doc(root)
    passes = doc.get("passes")
    doc["passes"] = passes if isinstance(passes, dict) else {}
    row = {"capture": capture, "cues": heard, "verdict": verdict,
           "notes": notes, "by": by or _activity.current_actor(),
           "at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
    doc["passes"][capture] = row
    _ws.set(root, LISTEN_SEAT, LISTEN_KEY,
            {k: v for k, v in doc.items() if k != _ws.VERSION_KEY})
    _activity.log(root, "audio",
                  f"in-game listen {verdict}: {len(heard)} cue(s) over "
                  f"{capture}", seat=LISTEN_SEAT, ref=capture)
    return row


def listened(root: str | os.PathLike[str]) -> set[str]:
    """Every cue covered by a PASSING listening pass."""
    out: set[str] = set()
    for row in (_listen_doc(root).get("passes") or {}).values():
        if isinstance(row, dict) and row.get("verdict") == "pass":
            out.update(str(c) for c in row.get("cues") or [])
    return out


def in_game_unreviewed(root: str | os.PathLike[str]) -> list[str]:
    """Wired cues nobody has heard in context. The release gate's question.

    Only WIRED events count. An unbound event is a different bug with a
    different fix (it is silence, not a bad mix) and the hook table already
    reports it; making the listening gate refuse on it would put one failure in
    two places and let fixing either one look like fixing both.
    """
    try:
        found = scan(root)
    except Exception as exc:                                      # noqa: BLE001
        return [f"the audio hook scan could not run ({type(exc).__name__}: "
                f"{exc}), so no cue can be shown as heard"]
    wired = [e for e in found.get("events") or []
             if str(e.get("state")) == "wired"]
    if not wired:
        return []
    heard = listened(root)
    missing = sorted({str(e.get("event")) for e in wired} - heard)
    if not missing:
        return []
    shown = ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "")
    return [f"{len(missing)} of {len(wired)} wired cue(s) have never been "
            f"heard in a gameplay capture ({shown}) — peaks, RMS and wiring "
            "all pass on a cue that sounds wrong in context. "
            "audio_listen_record(capture=..., cues=[...])"]
