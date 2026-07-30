"""Trained styles — the project's own look, in a model instead of a prompt.

WHAT THIS IS FOR, in the art seat's own words: "A style reference and an identity
reference cannot share a weight. At equal strength the style ref transfers the
SUBJECT and the whole cast comes back as one person." One reference slot, two
jobs. Every anchored generation in this tool has been paying that tax — the
project's look and the character's identity competing for the same array, so
holding one costs you the other.

A trained style moves the LOOK into the model. The slot is then free to carry
identity alone, which is the thing prompts and references are worst at.

THE DATASET IS THE PINNED REFERENCES, and that is the point rather than a
convenience. Those images are already the ones a human approved: `ref_pin` is the
gate the art seat passes to say "this is on-model". Training on anything else
would mean maintaining a second, unreviewed idea of what the project looks like —
and the failure mode of a LoRA is that its drift is baked in rather than visible
in a payload, so what goes in has to be what was already blessed.

WHAT LIVES HERE AND WHAT DOES NOT. This module owns the RECORD: which style was
trained, from which anchors, when, and which one is active. The training call
itself is `bgate_adapters.krea` (upload -> /styles/train -> poll), and the
decision to spend money on one is a human's — see `bgate_ui/routes/art_style.py`.
Nothing in here starts a training run.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from . import activity, refs as _refs, workspace as _ws

SEAT = "art"
KEY = "styles"

# Which trained style a generation should reach for. One per project: a second
# active style is two answers to "what does this game look like", and the whole
# reason to train one is that there is a single answer.
ACTIVE = "active"

# What the record carries. Sources are kept because a LoRA is otherwise
# unfalsifiable six months later: without the list you cannot tell whether the
# thing generating your art was trained on the anchors you are looking at.
FIELDS = ("style_id", "name", "trigger_word", "model", "kind", "images",
          "sources", "trained_at", "trained_by", "strength", "note")


def _doc(root: str | os.PathLike[str]) -> dict:
    try:
        return _ws.get(root, SEAT, KEY, {}) or {}
    except Exception:
        return {}


def record(root: str | os.PathLike[str], style: dict, *,
           make_active: bool = True) -> dict:
    """Remember a style that finished training. Returns the stored record.

    Called after the adapter reports a style_id, never before: a record for a
    run that failed would be a style the generator reaches for and the API does
    not have, which fails every image rather than none.
    """
    style_id = str(style.get("style_id") or "").strip()
    if not style_id:
        raise ValueError("a trained style needs its style_id")
    entry = {k: style.get(k) for k in FIELDS if k in style}
    entry["style_id"] = style_id
    entry.setdefault("name", style_id)
    entry.setdefault("strength", 0.85)
    entry.setdefault("trained_at", time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.gmtime()))
    entry.setdefault("trained_by", activity.current_actor())

    doc = _doc(root)
    trained = {str(s.get("style_id")): s for s in (doc.get("trained") or [])}
    trained[style_id] = entry
    doc["trained"] = sorted(trained.values(),
                            key=lambda s: str(s.get("trained_at") or ""),
                            reverse=True)
    if make_active:
        doc[ACTIVE] = style_id
    _ws.set(root, SEAT, KEY, doc)
    activity.log(root, "style",
                 f"trained style {entry['name']} ({style_id}) from "
                 f"{entry.get('images') or '?'} anchors", seat=SEAT)
    _announce(root, entry, make_active)
    return entry


def _announce(root, entry: dict, made_active: bool) -> None:
    """Put it on the bus. A trained style changes what every future image looks
    like, which is worth a line in the drawer — and guarded, because a project
    from before migration 0016 has nowhere to put one."""
    try:
        from . import events as _events

        _events.emit(root, "style.trained", ref=str(entry["style_id"]),
                     payload={"style_id": entry["style_id"],
                              "name": entry.get("name") or "",
                              "images": entry.get("images") or 0,
                              "active": bool(made_active)})
    except Exception:
        pass


def trained(root: str | os.PathLike[str]) -> list[dict]:
    """Every style this project has trained, newest first."""
    return list(_doc(root).get("trained") or [])


def active(root: str | os.PathLike[str]) -> Optional[dict]:
    """The style generations should use, or None.

    None is a normal answer, not an error: a project that has not trained one
    generates the way it always did.
    """
    doc = _doc(root)
    wanted = str(doc.get(ACTIVE) or "").strip()
    if not wanted:
        return None
    for entry in doc.get("trained") or []:
        if str(entry.get("style_id")) == wanted:
            return dict(entry)
    return None


def set_active(root: str | os.PathLike[str], style_id: str) -> Optional[dict]:
    """Point at a different trained style, or at none with an empty string."""
    doc = _doc(root)
    wanted = str(style_id or "").strip()
    if wanted and not any(str(s.get("style_id")) == wanted
                          for s in doc.get("trained") or []):
        raise LookupError(f"no trained style {wanted!r} on this project")
    doc[ACTIVE] = wanted
    _ws.set(root, SEAT, KEY, doc)
    activity.log(root, "style",
                 f"active style -> {wanted or '(none)'}", seat=SEAT)
    return active(root)


def forget(root: str | os.PathLike[str], style_id: str) -> bool:
    """Drop a record. The style still exists on Krea's side — this is the
    project forgetting it, not a delete, and saying so matters because a user
    who expects the second is owed the API call this does not make."""
    doc = _doc(root)
    before = doc.get("trained") or []
    after = [s for s in before if str(s.get("style_id")) != str(style_id)]
    if len(after) == len(before):
        return False
    doc["trained"] = after
    if str(doc.get(ACTIVE) or "") == str(style_id):
        doc[ACTIVE] = ""
    _ws.set(root, SEAT, KEY, doc)
    return True


# ---------------------------------------------------------------------------
# The dataset: what the art seat already approved
# ---------------------------------------------------------------------------
SOURCES = ("pins", "assets", "both")

# Where shipped art lives, relative to the project. The game's own tree, not the
# tool's: .bgate/refs is the pin store and .bgate_out is scratch, and training on
# either means training on the tool instead of the game.
ASSET_DIRS = ("game/assets",)
ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
# A cap, because an assets tree is thousands of files and every one of them gets
# opened to read its size. The floor rejects most pixel art anyway; what this
# stops is a scan that takes a minute to tell you so.
MAX_SCAN = 4000


def _from_assets(root) -> list[dict]:
    """Shipped art as training candidates.

    THE PINS ARE NOT ALWAYS THE DATASET. `ref_pin` holds the anchors a human
    approved as on-model, which is the right default and a small set — six
    images on a real project. But a game that has been generating for weeks has
    hundreds of finished, in-game, on-model pieces in `game/assets/`, and
    refusing to train on them because nobody re-pinned them is refusing the
    project's own output.

    Everything here still goes through the same floor and the same human
    confirmation; what changes is only which shelf is offered.
    """
    base = Path(root)
    out: list[dict] = []
    for rel_dir in ASSET_DIRS:
        folder = base / rel_dir
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if len(out) >= MAX_SCAN:
                break
            if not path.is_file() or path.suffix.lower() not in ASSET_SUFFIXES:
                continue
            rel = path.relative_to(base).as_posix()
            out.append({
                # The name is the path under the assets root, because two files
                # called "idle.png" in different character folders are not the
                # same anchor and a bare stem would read as a duplicate.
                "name": path.relative_to(folder).as_posix(),
                "path": str(path), "kind": "asset", "rel": rel})
    return out


def dataset(root: str | os.PathLike[str],
            names: Optional[list] = None, source: str = "pins") -> dict:
    """The pinned references this project would train on, judged before spending.

    Returns ``{ok, usable, rejected, warnings, reason, candidates}`` — the
    adapter's verdict, plus the pin names behind it so a UI can say WHICH anchor
    was too small rather than which path.

    Judged here rather than at the API because a training run is 5-15 minutes at
    an unpublished price, and "one of your anchors is 297px wide" is worth
    knowing on this side of the network. Measured on a real board: 6 of 27 pins
    cleared the 1024 floor.
    """
    from bgate_adapters import krea

    source = str(source or "pins").strip().lower()
    if source not in SOURCES:
        source = "pins"
    wanted = {str(n).strip() for n in (names or []) if str(n).strip()}
    candidates: list[dict] = []
    if source in ("pins", "both"):
        for pin in _refs.list_refs(root) or []:
            path = str(pin.get("path") or "")
            if path:
                candidates.append({"name": str(pin.get("name") or ""),
                                   "path": path,
                                   "kind": str(pin.get("kind") or "")})
    if source in ("assets", "both"):
        seen = {c["path"] for c in candidates}
        candidates += [c for c in _from_assets(root) if c["path"] not in seen]
    if wanted:
        candidates = [c for c in candidates if c["name"] in wanted]

    verdict = krea.check_training_set([c["path"] for c in candidates])
    by_path = {c["path"]: c for c in candidates}
    # Names, not paths: the human pinned "concept-battle", and telling them
    # ".bgate/refs/concept-battle.png is 1672x941" makes them do the lookup.
    verdict["rejected"] = [{**r, **_shown(root, by_path.get(r.get("path")))}
                           for r in verdict.get("rejected") or []]
    verdict["usable_names"] = [
        (by_path.get(p) or {}).get("name", "") for p in verdict.get("usable") or []]
    # The anchors themselves, so a panel can SHOW the six images that will
    # define the style rather than print the number six. This is an art
    # feature; a count is the least useful way to describe a dataset of
    # pictures, and "which of my anchors is in this" is the question actually
    # being asked.
    verdict["anchors"] = [_shown(root, by_path.get(p))
                          for p in verdict.get("usable") or []]
    verdict["candidates"] = len(candidates)
    verdict["source"] = source
    return verdict


def _shown(root, entry: Optional[dict]) -> dict:
    """One anchor as a panel needs it: its name, kind, and a servable path.

    ``rel`` is project-relative because /api/preview refuses anything else — it
    resolves against the root and rejects an escape, so an absolute path from
    the pin table is a 403 rather than a thumbnail.
    """
    if not entry:
        return {"name": "", "kind": "", "rel": ""}
    rel = ""
    try:
        rel = str(Path(entry["path"]).resolve().relative_to(
            Path(root).resolve())).replace("\\", "/")
    except Exception:
        rel = ""
    return {"name": entry.get("name", ""), "kind": entry.get("kind", ""),
            "rel": rel, "path": entry.get("path", "")}


def describe(root: str | os.PathLike[str]) -> dict:
    """Everything a panel needs: the styles, the active one, and the dataset."""
    from . import settings as _settings

    try:
        mode = _settings.get(root, "art.style_source")
    except Exception:
        mode = "refs"
    try:
        strength = float(_settings.get(root, "art.lora_strength"))
    except Exception:
        strength = 0.85
    try:
        source = _settings.get(root, "art.style_dataset")
    except Exception:
        source = "pins"
    return {"trained": trained(root), "active": active(root),
            "mode": mode, "strength": strength, "source": source,
            "sources": list(SOURCES),
            "dataset": dataset(root, source=source)}


def for_generation(root: Any) -> list[dict]:
    """The `styles` array a Krea generation should send, or [].

    THE TOGGLE IS READ HERE, at the one place that answers "does this image use
    the trained look", so the art seat, a workflow node and the sprite pipeline
    cannot disagree about it. Empty list = generate the way this project always
    did, which is what every caller does when nothing is trained or the setting
    says references.

    Never raises: an unreadable setting must not fail an image.
    """
    if not root:
        return []
    try:
        from . import settings as _settings
        from bgate_adapters import krea

        if str(_settings.get(root, "art.style_source") or "refs") != "lora":
            return []
        entry = active(root)
        if not entry:
            return []
        strength = entry.get("strength")
        if strength is None:
            strength = _settings.get(root, "art.lora_strength")
        return [krea.style(str(entry["style_id"]), float(strength))]
    except Exception:
        return []
