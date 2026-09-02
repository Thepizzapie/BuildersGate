"""Pinned reference anchors — the canonical images art derives from.

The problem this solves: an approved character reference or style anchor is the
single most valuable artifact in a generated-art pipeline, and it was living in
scratch output dirs, found by path guesswork, one cleanup away from gone. A pin
copies the file into ``.bgate/refs/`` (durable, travels with the project),
names it, and surfaces it in every seat brief — so every art agent starts from
the same anchors instead of re-deriving or, worse, re-generating them.

Pins are VERSIONED, like artifact revisions. Re-pinning used to overwrite the
file in place, which quietly rewrote history: every artifact that recorded
"generated against tommy-ref" now pointed at a different image, and no card
could be trusted as evidence. Each pin now lands as ``<slug>.rN<suffix>``, the
ref_pin row points at the newest, and ref_pin_revision keeps every older one
resolvable (by ``name@rN``) and hashed.

resolve() lets image tools accept a pin NAME anywhere they accept a path.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional

from ..board import activity, aegis
from ..store import assets, db
from ..store.util import rows, slugify

REFS_DIRNAME = "refs"
KINDS = ("character", "style", "ui", "concept")

# "tommy-ref@r2" / "tommy-ref@2" — how a caller asks for an older revision.
_AT_REVISION = re.compile(r"^(?P<name>.+?)@r?(?P<revision>\d+)$")


def _refs_dir(root: str | os.PathLike[str]) -> Path:
    return Path(root) / db.DB_DIRNAME / REFS_DIRNAME


# The magic bytes of the four types an image model will accept as a reference.
# A pin taken from a temp file with no extension used to land as
# `<slug>.r1` with no suffix at all, and kie refuses to upload a file whose
# extension is not one of these, so the anchor was pinned, listed, and
# unusable as an anchor, which is the one job it has.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

# What counts as "already named like an image". A pin whose source was itself
# an older pin (`floor-style.r1`) has a suffix, ".r1", and trusting it is how
# an anchor ends up named `<slug>.r2.r1`, still unusable.
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tga")


def _suffix_for(name: str, head: bytes) -> str:
    """The extension the pinned copy should carry.

    TAKES BYTES, NOT A PATH, and that is the point: this used to open the file
    itself, which put a read of a caller-supplied path in a function with no way
    to know which project owns it. The read now happens in pin(), on the line
    after the boundary check, so the check and the open cannot drift apart. It
    also makes this a pure function, which is how the table below is testable
    without a file.

    The source's own suffix wins when it is an image extension. When it is not -
    no suffix at all, or a revision tag like ".r1" - the header decides, because
    everything downstream routes on the extension and kie refuses to upload a
    reference it cannot type.
    """
    suffix = Path(name).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return suffix
    for magic, magic_suffix in _MAGIC:
        if head.startswith(magic):
            return magic_suffix
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ""


def pin(root: str | os.PathLike[str], name: str, src_path: str, *,
        kind: str = "style", note: str = "", actor: Optional[str] = None) -> dict:
    """Pin a reference: copy it into .bgate/refs/ as a new numbered revision.

    Re-pinning an existing name upgrades the anchor — the name stays stable and
    everything referencing it follows — but the previous revision keeps its own
    file, so anything generated against it can still be shown.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    # THE SOURCE IS CHECKED BEFORE ANYTHING OPENS IT, not after. `src_path`
    # arrives from a caller - an agent's tool argument, a dashboard form - and
    # everything below reads it: _suffix_for sniffs its header and the copy
    # takes its bytes. A pin is the one place in this module that reaches
    # OUTSIDE .bgate to fetch a file, so it is the one place the boundary has
    # to be applied, and applying it at the caller instead would leave the
    # other callers to remember.
    #
    # aegis.decide is the same function the PreToolUse hook and the MCP server
    # ask, so a path this refuses here is a path they refuse too.
    # RESOLVED ONCE, AND EVERY LINE BELOW USES THE RESOLVED PATH. The check ran
    # on `Path(src_path)` while the open and the copy used that same unresolved
    # value, so the string that was JUDGED and the string that was OPENED were
    # only equal by assumption: aegis.decide normalises internally before
    # deciding, and anything containing `..` or a symlink was therefore approved
    # in one form and used in another. Resolving here collapses the two into one
    # value, which closes the gap and is also what makes the boundary legible -
    # to a reader, and to the scanner that flagged this line as a
    # caller-controlled path reaching a file open.
    src = Path(src_path).resolve()
    verdict = aegis.decide(str(Path(root).resolve()), str(src), seat="pin")
    if not aegis.is_allowed(verdict):
        raise ValueError(
            f"cannot pin {src_path}: {verdict['reason']}. Copy it into the "
            "project first, then pin the copy.")
    if not src.is_file():
        raise FileNotFoundError(f"no file at {src_path}")
    # READ ONCE, HERE, on the line after the boundary check. _suffix_for used to
    # open the file itself, which put a read of a caller-supplied path in a
    # function with no idea which project owns it; keeping the open next to the
    # check is what stops the two drifting apart.
    try:
        with src.open("rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        raise FileNotFoundError(f"cannot read {src_path}: {exc}") from exc
    slug = slugify(name)
    who = actor if actor is not None else activity.current_actor()

    conn = db.connect(root)
    previous = conn.execute(
        "SELECT * FROM ref_pin WHERE name = ?", (slug,)).fetchone()
    top = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) FROM ref_pin_revision WHERE name = ?",
        (slug,)).fetchone()[0]
    revision = max(int(top or 0),
                   int(previous["revision"] or 1) if previous else 0) + 1

    dest_dir = _refs_dir(root)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{slug}.r{revision}{_suffix_for(src.name, head)}"
    shutil.copy2(src, dest)
    digest = assets.file_hash(dest)

    with db.tx(root) as conn:
        # A pin made before versioning has no revision row; record it now so its
        # history does not start at r2 out of nowhere.
        if previous and not top:
            old = Path(previous["path"])
            conn.execute(
                "INSERT OR IGNORE INTO ref_pin_revision "
                "(name, revision, path, hash, note) VALUES (?, ?, ?, ?, ?)",
                (slug, int(previous["revision"] or 1), previous["path"],
                 assets.file_hash(old) if old.is_file() else "",
                 previous["note"] or ""))
        conn.execute(
            """
            INSERT INTO ref_pin (name, path, kind, note, revision, hash, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (name) DO UPDATE SET
                path = excluded.path, kind = excluded.kind, note = excluded.note,
                revision = excluded.revision, hash = excluded.hash,
                updated_at = excluded.updated_at
            """,
            (slug, str(dest), kind, note, revision, digest),
        )
        conn.execute(
            "INSERT INTO ref_pin_revision (name, revision, path, hash, note, actor) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (slug, revision, str(dest), digest, note, (who or "")[:120]))
    activity.log(root, "ref_pin", f"pinned reference {slug!r} r{revision} ({kind})",
                 ref=str(dest), actor=who)
    return get(root, slug)


def get(root: str | os.PathLike[str], name: str) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM ref_pin WHERE name = ?", (slugify(name),)).fetchone()
    if row is None:
        raise LookupError(f"no pinned reference {name!r}")
    return dict(row)


def history(root: str | os.PathLike[str], name: str) -> list[dict]:
    """Every revision of a pin, newest first. Pre-versioning pins report the one
    revision the ref_pin row itself carries."""
    slug = slugify(name)
    conn = db.connect(root)
    recorded = rows(conn.execute(
        "SELECT * FROM ref_pin_revision WHERE name = ? "
        "ORDER BY revision DESC", (slug,)))
    if recorded:
        return recorded
    current = conn.execute(
        "SELECT * FROM ref_pin WHERE name = ?", (slug,)).fetchone()
    if current is None:
        raise LookupError(f"no pinned reference {name!r}")
    return [{"name": slug, "revision": int(current["revision"] or 1),
             "path": current["path"], "hash": current["hash"] or "",
             "note": current["note"] or "", "actor": "",
             "created_at": current["updated_at"] or ""}]


def get_revision(root: str | os.PathLike[str], name: str, revision: int) -> dict:
    """One historical revision — what an old artifact was actually drawn against."""
    for entry in history(root, name):
        if int(entry["revision"]) == int(revision):
            return entry
    raise LookupError(f"{slugify(name)!r} has no revision r{revision}")


def list_refs(root: str | os.PathLike[str], kind: Optional[str] = None) -> list[dict]:
    conn = db.connect(root)
    if kind:
        return rows(conn.execute(
            "SELECT * FROM ref_pin WHERE kind = ? ORDER BY name", (kind,)))
    return rows(conn.execute("SELECT * FROM ref_pin ORDER BY kind, name"))


def unpin(root: str | os.PathLike[str], name: str) -> dict:
    """Remove a pin (keeps the file — deleting canon art is a human's job)."""
    entry = get(root, name)
    with db.tx(root) as conn:
        conn.execute("DELETE FROM ref_pin WHERE name = ?", (entry["name"],))
    return entry


# ---------------------------------------------------------------------------
# Character profiles — identity as a stored artifact, never re-imagined prose.
# Written by a vision pass LOOKING at the approved reference; injected into
# every generation prompt; the checklist consistency_check judges against.
# ---------------------------------------------------------------------------
def profile_path(root: str | os.PathLike[str], name: str) -> "Path":
    return _refs_dir(root) / f"{slugify(name)}.profile.json"


def profile_set(root: str | os.PathLike[str], name: str, *, traits: str,
                style: str, negative: str) -> dict:
    """Store a character's visual identity next to its pinned reference.

    traits    what the character IS (from LOOKING at the reference)
    style     the rendering style every frame must hold
    negative  what generations must never introduce
    """
    import json

    get(root, name)  # must be a pinned reference
    data = {"name": slugify(name), "traits": traits.strip(),
            "style": style.strip(), "negative": negative.strip()}
    path = profile_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    activity.log(root, "profile", f"visual profile set for {data['name']}",
                 ref=str(path))
    return data


def profile_get(root: str | os.PathLike[str], name: str) -> Optional[dict]:
    import json

    path = profile_path(root, name)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def resolve(root: str | os.PathLike[str], name_or_path: str) -> str:
    """A pin name -> its file path; an existing path passes through untouched.

    ``name@r2`` resolves an older revision, which is how a review screen shows
    what a past artifact was really generated against.

    Missing on both counts raises — silently generating against a nonexistent
    reference produces an unconditioned image that LOOKS like a result.
    """
    at = _AT_REVISION.match(str(name_or_path).strip())
    if at:
        try:
            return get_revision(root, at.group("name"),
                                int(at.group("revision")))["path"]
        except LookupError:
            pass
    try:
        return get(root, name_or_path)["path"]
    except LookupError:
        pass
    if Path(name_or_path).is_file():
        return str(name_or_path)
    try:
        available = [r["name"] for r in list_refs(root)]
    except Exception:
        available = []
    hint = (f" Available pinned refs: {', '.join(available)}." if available
            else " No refs are pinned yet (use ref_pin).")
    raise LookupError(
        f"{name_or_path!r} is neither a pinned reference nor an existing file."
        + hint + " Pass one of those names, a real file path, or ref_pin it first."
    )
