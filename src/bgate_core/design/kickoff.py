"""Project kickoff: the brief and the reference images a project starts from.

The way a project actually started, every time, was a human pasting a long
brief and a row of screenshots into the director chat and saying "pin these
in the bible". The create-project card took a 200-character pitch and nothing
else, so the real start happened one screen later, by hand, and only if the
human remembered the ritual. This module is that ritual as code, so the card,
``bgate init`` and ``bgate adopt`` all do it the same way:

  1. the brief is written to ``design/brief.md`` (the director's lane) and
     summarised into a ``reference`` bible section that points at the file;
  2. each image is pinned as a project reference (``refs.pin``) and anchored
     to a second ``reference`` section, so ``bible_read`` shows them where the
     brief is;
  3. a ``next`` note goes on the handoff thread, so the SessionStart block of
     the next top-level session says the kickoff is pending;
  4. ``prompt()`` renders the director's first turn, which the dashboard sends
     the moment the project exists. The CLI cannot start a director, so it
     leaves the note and says so.

Nothing here decides anything about the game. The thesis, the pillars, the
not-building list and the board are the director's to settle from the brief,
exactly as they were when the human pasted it.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from ..art import refs as _refs
from ..board import activity as _activity
from ..board import handoff as _handoff
from ..store.util import slugify
from . import bible as _bible
from . import bible_refs as _bible_refs

MAX_BRIEF = 60_000
MAX_REFS = 24
# What the bible section carries inline. The full text lives in the file; a
# 20 kB brief inside a bible section would be the first thing every seat
# brief's trim ladder cut, and cut to 300 characters at that.
EXCERPT_CHARS = 1_500
BRIEF_PATH = Path("design") / "brief.md"
BRIEF_TITLE = "Project brief"
REFS_TITLE = "Kickoff references"

_DATA_URL = re.compile(
    r"^data:image/(?P<ext>[a-zA-Z0-9.+-]+);base64,(?P<b64>.+)$", re.S)
_EXT_OK = {"png", "jpg", "jpeg", "webp", "gif", "svg+xml", "svg"}
_KINDS = _refs.KINDS


def decode_upload(data: str, ext: str = "png") -> tuple[bytes, str]:
    """A base64 upload (data-URL or raw) to ``(bytes, ext)``. Raises ValueError
    on anything that is not an image this project can pin."""
    raw = (data or "").strip()
    ext = (ext or "png").lower().lstrip(".")
    m = _DATA_URL.match(raw)
    if m:
        ext = m.group("ext").lower()
        raw = m.group("b64")
    if ext not in _EXT_OK:
        raise ValueError(f"unsupported image type {ext!r}")
    if ext == "svg+xml":
        ext = "svg"
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("data is not valid base64")
    if not blob:
        raise ValueError("empty image")
    return blob, ext


def pin_bytes(root, name: str, blob: bytes, ext: str, *, kind: str = "style",
              note: str = "", actor: Optional[str] = None) -> dict:
    """Pin image bytes under ``name``. Stages in a private temp dir, which is
    inside the aegis allowlist, so the pin's own boundary check still runs and
    still refuses anything the staging did not write."""
    if any(sep in name for sep in ("/", "\\")) or ".." in name:
        raise ValueError("name is a ref label, not a path - no separators or '..'")
    staging = Path(tempfile.mkdtemp(prefix="bgate_upload_"))
    tmp = staging / f"{slugify(name) or 'ref'}.{ext}"
    try:
        tmp.write_bytes(blob)
        return _refs.pin(root, name, str(tmp), kind=kind, note=note, actor=actor)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _ref_kind(raw: str) -> str:
    kind = (raw or "").strip().lower()
    return kind if kind in _KINDS else "concept"


def _name_for(entry: dict, index: int) -> str:
    name = str(entry.get("name") or "").strip()
    if not name:
        name = f"kickoff-{index + 1}"
    return name[:80]


def seed(root, brief: str = "", refs: Optional[list] = None, *,
         actor: str = "human") -> dict:
    """Seed a project from its brief and reference images. Idempotent on the
    bible titles; a second call with a new brief rewrites the file and the
    section body rather than adding a second of each.

    ``refs`` entries are ``{name, data, ext, kind, note}`` (``data`` base64 or a
    data-URL) or ``{name, path, kind, note}`` for a file already on disk. A ref
    that fails to pin is reported under ``skipped`` and does not stop the rest.
    """
    root = Path(root)
    brief = (brief or "").strip()
    if len(brief) > MAX_BRIEF:
        raise ValueError(f"the brief is {len(brief)} characters; "
                         f"{MAX_BRIEF} is the cap")
    refs = list(refs or [])
    if len(refs) > MAX_REFS:
        raise ValueError(f"{len(refs)} reference images; {MAX_REFS} is the cap")
    out: dict = {"brief": None, "brief_section": None, "refs_section": None,
                 "pinned": [], "skipped": []}
    if not brief and not refs:
        return out

    if brief:
        path = root / BRIEF_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(brief + "\n", encoding="utf-8")
        out["brief"] = str(BRIEF_PATH).replace(os.sep, "/")
        excerpt = brief[:EXCERPT_CHARS]
        if len(brief) > EXCERPT_CHARS:
            excerpt += " ..."
        body = (f"The full brief, as pasted at project creation, is "
                f"{out['brief']} ({len(brief)} characters). Read that file; "
                f"this section is the pointer and an excerpt.\n\n{excerpt}")
        out["brief_section"] = _upsert(root, BRIEF_TITLE, body)["id"]

    if refs:
        names = []
        for i, entry in enumerate(refs):
            entry = dict(entry or {})
            name = _name_for(entry, i)
            kind = _ref_kind(entry.get("kind"))
            note = str(entry.get("note") or "pinned at project kickoff")[:400]
            try:
                if entry.get("path"):
                    src = Path(str(entry["path"]))
                    blob = src.read_bytes()
                    ext = src.suffix.lower().lstrip(".") or "png"
                    if ext not in _EXT_OK:
                        raise ValueError(f"unsupported image type {ext!r}")
                else:
                    blob, ext = decode_upload(str(entry.get("data") or ""),
                                              str(entry.get("ext") or "png"))
                pinned = pin_bytes(root, name, blob, ext, kind=kind, note=note,
                                   actor=actor)
            except (ValueError, OSError) as exc:
                out["skipped"].append({"name": name, "why": str(exc)})
                continue
            out["pinned"].append({"name": pinned["name"], "kind": kind,
                                  "path": pinned.get("path")})
            names.append((pinned["name"], kind))
        if names:
            listing = "\n".join(f"- {n} ({k})" for n, k in names)
            body = ("Reference images attached when the project was created. "
                    "They are pinned project refs (ref_list) and anchored "
                    "here; `bible_ref_attach` moves one onto the pillar it "
                    "belongs to once the pillars exist.\n\n" + listing)
            section = _upsert(root, REFS_TITLE, body)
            out["refs_section"] = section["id"]
            for n, k in names:
                # bible_ref upserts on (section, ref): re-seeding re-anchors.
                try:
                    _bible_refs.add(root, section["id"], n, kind=k,
                                    note="kickoff")
                except (LookupError, ValueError, FileNotFoundError):
                    pass

    summary = []
    if out["brief"]:
        summary.append(f"brief at {out['brief']}")
    if out["pinned"]:
        summary.append(f"{len(out['pinned'])} reference image(s) pinned")
    if out["skipped"]:
        summary.append(f"{len(out['skipped'])} image(s) refused")
    text = "Project kickoff pending: " + ", ".join(summary) + (
        ". The director's first turn is to read the brief, settle the "
        "mechanical thesis, write the bible and lay out the board.")
    try:
        _handoff.note(root, "next", text, actor=actor)
    except Exception:
        pass
    _activity.log(root, "project", "kickoff seeded: " + ", ".join(summary),
                  seat="director", actor=actor)
    return out


def _upsert(root, title: str, body: str) -> dict:
    for section in _bible.list_sections(root, kind="reference"):
        if section.get("title") == title:
            return _bible.update(root, int(section["id"]), body=body)
    return _bible.add(root, "reference", title, body=body)


def prompt(root, seeded: dict, *, project_name: str = "") -> str:
    """The director's first turn: the words the human used to type."""
    root = Path(root)
    lines = [f"PROJECT KICKOFF{(' - ' + project_name) if project_name else ''}."]
    brief_rel = seeded.get("brief")
    pinned = seeded.get("pinned") or []
    if brief_rel:
        lines.append(
            f"The brief below was pasted when the project was created. It is "
            f"saved at {brief_rel} and summarised in bible section "
            f"#{seeded.get('brief_section')}; the file is the source of truth.")
    if pinned:
        names = ", ".join(p["name"] for p in pinned)
        lines.append(
            f"{len(pinned)} reference image(s) are pinned as project refs and "
            f"anchored to bible section #{seeded.get('refs_section')}: {names}. "
            f"Look at them (ref_list gives the paths) before you write a word "
            f"about the look, and bible_ref_attach each one to the pillar it "
            f"illustrates once the pillars exist.")
    if seeded.get("skipped"):
        bad = "; ".join(f"{s['name']}: {s['why']}" for s in seeded["skipped"])
        lines.append(f"Refused at upload, not pinned: {bad}.")
    lines.append(
        "Do the project start you would do if I had pasted this myself: read "
        "the brief in full; settle the mechanical thesis "
        "(greenlight_thesis_set) and say which option the brief leaves open; "
        "write the pillars, the core loop and the constraints into the bible "
        "(bible_add); record what this project is deliberately not building "
        "(not_building_add); then lay the board out as chains "
        "(queue_add_chain) with an acceptance test in every brief, and tell me "
        "what you dispatched. Ask me only what the brief genuinely leaves open, "
        "in one message, before you spend anything.")
    if brief_rel:
        try:
            text = (root / brief_rel).read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            lines.append("")
            lines.append("--- BRIEF ---")
            lines.append(text)
    return "\n".join(lines)
