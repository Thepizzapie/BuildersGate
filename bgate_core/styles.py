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
def dataset(root: str | os.PathLike[str],
            names: Optional[list] = None) -> dict:
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

    wanted = {str(n).strip() for n in (names or []) if str(n).strip()}
    candidates: list[dict] = []
    for pin in _refs.list_refs(root) or []:
        name = str(pin.get("name") or "")
        if wanted and name not in wanted:
            continue
        path = str(pin.get("path") or "")
        if not path:
            continue
        candidates.append({"name": name, "path": path,
                           "kind": str(pin.get("kind") or "")})

    verdict = krea.check_training_set([c["path"] for c in candidates])
    by_path = {c["path"]: c["name"] for c in candidates}
    # Names, not paths: the human pinned "concept-battle", and telling them
    # ".bgate/refs/concept-battle.png is 1672x941" makes them do the lookup.
    verdict["rejected"] = [{**r, "name": by_path.get(r.get("path"), "")}
                           for r in verdict.get("rejected") or []]
    verdict["usable_names"] = [by_path.get(p, "") for p in verdict.get("usable") or []]
    verdict["candidates"] = len(candidates)
    return verdict


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
    return {"trained": trained(root), "active": active(root),
            "mode": mode, "strength": strength,
            "dataset": dataset(root)}


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
