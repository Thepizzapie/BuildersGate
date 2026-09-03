"""The generated map of the MCP surface — what exists, grouped by craft.

THE FAILURE THIS EXISTS TO END: 231 tools arrive as a flat alphabetical list
with no shape, and the only map was `seat_brief().workflow` — hand-written
prose, opt-in, and free to drift from the registry the moment a tool is added
or renamed. An agent that cannot find `sprite_sheet_check` does not call it;
it generates the sheet again, or hand-rolls the check, or asks the human.

So the index is DERIVED, never authored. It is built from the live registry of
the process that serves it, which means it cannot list a tool this session
does not have and cannot omit one it does — the two ways a written list goes
wrong. A seat with `music` switched off sees no music_ row, because there is
no music_ tool to see.

WHAT IT DELIBERATELY IS NOT. Not documentation: one line per tool, the first
sentence only, no parameters. The schemas are already in context and repeating
them is the cost this is trying to cut. It answers "what is there and roughly
where", and the tool's own schema answers everything after that.
"""
from __future__ import annotations

from ..store import modules as _modules

# Craft -> the sentence that says when to look here. The craft NAMES are
# machine-readable groupings; these are what makes the grouping legible to
# someone deciding where to look, and they are the one authored thing in this
# module because no registry can derive intent.
CRAFT_BLURBS: dict[str, str] = {
    "image": "2D art: generate, slice, clean, check sheets, pin palettes",
    "three_d": "3D: model, rig, bake sprites, deliver into the engine",
    "level": "levels and tilesets: plan a layout, build a scene",
    "music": "music generation, audition, keep/discard",
    "sfx": "sound effects",
    "voice": "text-to-speech and speech-to-text",
    "cinematic": "storyboards, shot planning, video generation, assembly",
    "playtest": "run the game, capture evidence, diagnose why an action failed",
    "dialogue": "dialogue lines and trees",
    "quest": "quests and their steps",
    "verdicts": "the QA calls: pass, fail, and which candidate won",
    "brainstorm": "read-only thinking rooms and deploying a plan to the board",
}

SPINE_BLURB = ("the shared spine every seat has: the board, seats, canon, "
               "assets, scenes, the engine, the project itself")

# One line per tool means one SENTENCE per tool. A docstring's first sentence
# is written to be exactly this; anything longer is the schema's job.
_LINE_CAP = 96
# Below this a line has not said anything, so it borrows the next sentence.
_MIN_LINE = 32


def headline(description: str) -> str:
    """The first sentence of a tool's docstring, flattened and capped."""
    text = " ".join(str(description or "").split())
    if not text:
        return ""
    # A sentence ends at ". " — not at "e.g." or a decimal, which is why this
    # splits on a period FOLLOWED BY A SPACE rather than getting clever about
    # abbreviations. Sentences are then taken until there is enough to be
    # useful: "Free - spends nothing." is a true first sentence and a useless
    # index line, so a short one borrows the next.
    head = ""
    for piece in text.split(". "):
        head = piece if not head else f"{head}. {piece}"
        if len(head) >= _MIN_LINE:
            break
    # A dash clause is the other shape docstrings open with, and everything
    # after it is elaboration.
    for end in (" - ", " — "):
        before, sep, _ = head.partition(end)
        if sep and len(before) >= _MIN_LINE:
            head = before
            break
    head = head.rstrip(".")
    if len(head) > _LINE_CAP:
        head = head[:_LINE_CAP - 3].rsplit(" ", 1)[0] + "..."
    return head


def groups(tools) -> dict[str, list[tuple[str, str]]]:
    """``{group: [(name, headline), ...]}`` for ``tools``, an iterable of
    ``(name, description)``.

    A tool held by several crafts is listed under EACH of them. That is not
    duplication to trim: the art seat and the tech seat both reach for
    `godot_deliver_asset`, and a map that filed it under one of them sends the
    other looking in the wrong place — the exact failure this replaces.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for name, description in tools:
        line = headline(description)
        owners = _modules.crafts_owning(name)
        for group in sorted(owners) or ["spine"]:
            out.setdefault(group, []).append((name, line))
    for rows in out.values():
        rows.sort()
    return out


def matches(tools, task: str) -> list[tuple[str, str]]:
    """Tools whose name or first sentence contains every word of ``task``.

    Every word, not any: "sprite sheet" must not return every tool that says
    "sprite". A search that answers with forty rows has not answered.
    """
    words = [w for w in str(task or "").lower().split() if w]
    if not words:
        return []
    found = []
    for name, description in tools:
        hay = (name + " " + str(description or "")).lower()
        if all(w in hay for w in words):
            found.append((name, headline(description)))
    return sorted(found)


def render(tools, *, task: str = "", seat: str = "",
           hidden: dict[str, list[str]] | None = None) -> str:
    """The index as text: grouped, one line per tool, no schemas."""
    if task:
        rows = matches(tools, task)
        if not rows:
            return (f"No tool's name or first line matches {task!r}. "
                    "Call tool_index() with no arguments for the whole map.")
        body = "\n".join(f"  {n:<28} {h}" for n, h in rows)
        return f"Tools matching {task!r}:\n{body}"

    by_group = groups(tools)
    lines = []
    # DISTINCT names: a tool held by two crafts is listed twice on purpose
    # (see `groups`), but counting it twice would misreport the surface.
    total = len({n for rows in by_group.values() for n, _ in rows})
    who = f"the {seat.upper()} seat" if seat else "this session"
    lines.append(f"THE {total} TOOLS {who} CAN CALL, by craft. One line each - "
                 "the tool's own schema has the parameters.")
    for group in sorted(by_group):
        if group == "spine":
            continue
        blurb = CRAFT_BLURBS.get(group, "")
        lines.append(f"\n{group.upper()}" + (f" - {blurb}" if blurb else ""))
        for name, head in by_group[group]:
            lines.append(f"  {name:<28} {head}")
    if by_group.get("spine"):
        lines.append(f"\nSPINE - {SPINE_BLURB}")
        for name, head in by_group["spine"]:
            lines.append(f"  {name:<28} {head}")
    lines.extend(_hidden_lines(hidden, full=True))
    return "\n".join(lines)

def _hidden_lines(hidden, *, full: bool) -> list[str]:
    """The crafts this seat could `tool_unlock`, so the map shows what exists
    beyond the schemas in context rather than implying the surface ends here."""
    if not hidden:
        return []
    out = ["\nNOT LOADED for this seat - tool_unlock(craft) registers them, or "
           "file the work with the seat that holds the craft:"]
    for craft in sorted(hidden):
        names = hidden[craft]
        blurb = CRAFT_BLURBS.get(craft, "")
        if full:
            out.append(f"  {craft} ({len(names)} tools" + (f": {blurb}" if blurb else "")
                       + "): " + ", ".join(names))
        else:
            out.append(f"  {craft}: {len(names)} tools" + (f" - {blurb}" if blurb else ""))
    return out



def compact(tools, *, seat: str = "",
            hidden: dict[str, list[str]] | None = None) -> str:
    """NAMES ONLY, grouped — the version that rides in the instructions block.

    The full `render` is ~230 lines and belongs in a tool result the agent asks
    for. This is what it costs to make the agent KNOW the map exists and
    roughly what is in it, which is the whole discoverability problem: a flat
    alphabetical tool list gives no hint that `sprite_sheet_check` is free and
    goes before `image_sprites`.
    """
    by_group = groups(tools)
    lines = ["THE TOOL SURFACE, by craft. `tool_index()` gives one line per "
             "tool, `tool_index(task=\"...\")` searches it."]
    for group in sorted(by_group):
        if group == "spine":
            continue
        names = ", ".join(n for n, _ in by_group[group])
        lines.append(f"  {group}: {names}")
    if by_group.get("spine"):
        names = ", ".join(n for n, _ in by_group["spine"])
        lines.append(f"  spine ({SPINE_BLURB}): {names}")
    lines.extend(_hidden_lines(hidden, full=False))
    return "\n".join(lines)
