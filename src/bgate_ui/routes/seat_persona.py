"""Editing a seat's PERSONALITY: how it behaves, and how it looks on the floor.

TWO KINDS OF FIELD LIVE HERE AND ONLY ONE OF THEM IS REAL. `cast`, `surface`,
`vibe`, `name` and `lines` are the studio view - what the room looks like and
what the character says while standing in it. `style` is different in kind: it
is appended to the DISPATCH PROMPT, so a seat spawned with it set actually
behaves differently, and it is the one field on this endpoint that costs tokens
and can change an outcome.

THE ONLY DOOR THE DASHBOARD HAS ONTO seat_config, and that is deliberate rather
than incidental. The rest of that table is PERMISSIONS - `write_globs` is the
lane a seat may write in and `enabled` is whether its QA runs at all - and the
MCP tool refuses both when an agent asks, on the grounds that a seat which can
widen its own lanes has no lanes. None of that reasoning applies here: the worst
a wrong persona does is give a room the wrong carpet, so this endpoint carries
no permission check and is safe to expose to a form.

`mission` is deliberately NOT editable here either, even though it is the same
table and carries no permissions. It is the brief the agent holding the seat is
actually given, so editing it from a panel about floor decoration would be a
text box that quietly changes what an agent does. That belongs in the seat's own
workspace, next to the work, not in a personality picker.

MERGED, NEVER REPLACED. seats.configure merges a persona key by key, so a form
that only sends `surface` keeps the cast and the vibe word - which is what lets
this be three independent controls rather than one all-or-nothing save.
"""
from __future__ import annotations

from fastapi import APIRouter, Body

from bgate_core.board import seats
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

# WHAT A PERSONA MAY SAY, ENFORCED HERE AS WELL AS IN THE UI. A dropdown is not
# a validator: this endpoint is reachable by anything that can reach the
# dashboard, and an unknown surface would fall through the renderer to its
# default with nothing anywhere saying why the carpet did not change.
SURFACES = ("carpet", "tile", "wood", "vinyl", "concrete")

# The cast sets that have art. `generic` is the fallback the renderer already
# uses for a seat nobody drew, and it is offered explicitly because "look like
# nobody in particular" is a legitimate choice.
CASTS = ("director", "narrative", "gameplay", "tech", "art", "audio", "qa",
         "cinematic", "generic")

# Long enough to name a room, short enough to fit under a nameplate. The floor
# draws this at about a third of a cell; a sentence would be drawn over the
# room next door.
VIBE_MAX = 14

# WHAT THE SEAT GOES BY. A project may call its narrative seat "Dave", and the
# floor's nameplate should say Dave. Capped at something that fits a nameplate
# drawn a third of a cell high next to a room eight cells wide.
NAME_MAX = 18

# A SEAT'S OWN BANTER, WHICH IS THE PART THAT IS ACTUALLY A PERSONALITY.
# Everything else here is what a room looks like; this is what the person in it
# SAYS. When this seat is the one talking in the lounge, its own lines are used
# instead of the shared pool, so two projects' art seats can be different people
# rather than differently-carpeted rooms.
#
# THE SAME TWO RULES THE SHARED POOL IS HELD TO, and they are enforced here
# because a form is exactly where they get broken: short enough to fit in a
# bubble drawn over a character's head, and no em dashes, which are the loudest
# tell that a line was machine written rather than typed by a person.
LINE_MAX = 38
LINES_MAX = 24

# HOW THE SEAT CARRIES ITSELF, AND THE ONLY FIELD HERE THAT REACHES A REAL
# AGENT. Everything else on a persona is decoration; this is appended to the
# dispatch prompt, so a seat spawned with it set actually behaves differently.
#
# CAPPED BECAUSE IT IS BILLED. It rides on every dispatch for that seat, so a
# page of character notes is a page of tokens on every item forever. A couple of
# sentences is a personality; an essay is a second mission competing with the
# first, which is exactly what the prompt tells the agent to ignore.
STYLE_MAX = 600


@router.get("/api/seats/persona")
def list_personas() -> dict:
    """Every seat's current look, plus what the picker may offer.

    The choices ship WITH the values so the form cannot drift from the server:
    a dropdown built from a list typed into the frontend is a second copy of
    this tuple, and the first time a cast is added it is wrong.
    """
    table = seats.roles_for(root())
    return api.ok({
        "seats": [{"role": role, "title": cfg.get("title") or role,
                   "persona": cfg.get("persona") or {}}
                  for role, cfg in table.items()],
        "choices": {"cast": list(CASTS), "surface": list(SURFACES),
                    "vibe_max": VIBE_MAX, "name_max": NAME_MAX,
                    "line_max": LINE_MAX, "lines_max": LINES_MAX,
                    "style_max": STYLE_MAX},
    })


@router.post("/api/seats/{role}/persona")
def set_persona(role: str, body: dict = Body(default={})) -> dict:
    """Change one seat's look. Only the keys present in the body are touched."""
    r = root()
    table = seats.roles_for(r)
    if role not in table:
        raise api.not_found(f"no seat {role!r} in this project", role=role)

    persona: dict = {}

    if "cast" in body:
        cast = str(body.get("cast") or "").strip()
        if cast and cast not in CASTS:
            raise api.ApiError(422, "no character art by that name",
                               detail={"cast": cast, "known": list(CASTS)})
        persona["cast"] = cast or None

    if "surface" in body:
        surface = str(body.get("surface") or "").strip()
        if surface and surface not in SURFACES:
            raise api.ApiError(422, "not a floor surface",
                               detail={"surface": surface,
                                       "known": list(SURFACES)})
        persona["surface"] = surface or None

    if "vibe" in body:
        # STRIPPED AND CAPPED, not refused. Somebody typing a sentence into a
        # one-word field has not made an error worth an error message; they
        # have made a word that is too long, and the honest fix is to keep the
        # part that fits rather than reject the whole edit.
        vibe = " ".join(str(body.get("vibe") or "").split())[:VIBE_MAX]
        persona["vibe"] = vibe or None

    if "name" in body:
        name = " ".join(str(body.get("name") or "").split())[:NAME_MAX]
        persona["name"] = name or None

    if "style" in body:
        style = " ".join(str(body.get("style") or "").split())[:STYLE_MAX]
        persona["style"] = style or None

    if "lines" in body:
        raw = body.get("lines")
        if isinstance(raw, str):
            raw = raw.splitlines()
        if not isinstance(raw, list):
            raise api.ApiError(422, "lines must be a list or newline-separated text",
                               detail={"got": type(raw).__name__})
        lines: list[str] = []
        for one in raw[:LINES_MAX]:
            text = " ".join(str(one).split())
            if not text:
                continue
            # DASHES ARE REPLACED, NOT REFUSED. Somebody pasting a line with an
            # em dash in it has not made an error worth losing their edit over;
            # they have used the one piece of punctuation this floor's voice
            # does not use, and a comma says the same thing.
            text = text.replace("—", ",").replace("–", ",")
            lines.append(text[:LINE_MAX])
        persona["lines"] = lines or None

    if not persona:
        raise api.ApiError(422, "nothing to change", detail={
            "fix": "send at least one of name, style, cast, surface, "
                   "vibe or lines"})

    # None means "back to the code default", which is why the empty string is
    # mapped to it above rather than being stored as an empty value that would
    # draw an empty nameplate.
    merged = seats.configure(r, role, persona=persona)
    return api.ok({"role": role, "persona": merged.get("persona") or {}})
