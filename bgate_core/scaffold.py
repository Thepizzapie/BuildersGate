"""Stamp out a Godot project wired for playtesting.

Templates are real, runnable slices — not empty shells. Each ships a player whose
"feel" tunables (gravity, fall_multiplier, coyote_time) are exported AND emitted
as telemetry, so the very first playtest already produces the join that makes
"the jump feels floaty" actionable.

The shared/ tree (the BGate autoload) is overlaid onto every template, so there
is one copy of the telemetry code rather than one per dimension.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from .util import slugify

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
KINDS = ("2d", "3d")

_NAME_TOKEN = "__PROJECT_NAME__"


def list_templates() -> list[dict]:
    out = []
    for kind in KINDS:
        path = TEMPLATES_DIR / kind
        out.append({
            "kind": kind,
            "available": path.is_dir(),
            "path": str(path),
            "description": {
                "2d": "Side-on platformer slice: player, ground, ledge, jump/land "
                      "telemetry, feel tunables exported.",
                "3d": "First-person slice: capsule player, ground, block, jump/land "
                      "telemetry, feel tunables exported.",
            }[kind],
        })
    return out


def new_project(dest: str | os.PathLike[str], name: str, kind: str = "2d",
                force: bool = False) -> dict:
    """Create a Godot project at dest from the given template.

    Refuses to write into a non-empty directory unless force — a scaffolder that
    quietly overwrites someone's work is a data-loss bug wearing a feature's hat.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")

    template = TEMPLATES_DIR / kind
    shared = TEMPLATES_DIR / "shared"
    if not template.is_dir():
        raise FileNotFoundError(f"template not found: {template}")

    target = Path(dest)
    if target.exists() and any(target.iterdir()) and not force:
        raise FileExistsError(
            f"{target} is not empty — pass force=True to scaffold into it anyway"
        )
    target.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for source in (template, shared):
        if not source.is_dir():
            continue
        for item in source.rglob("*"):
            if item.is_dir():
                continue
            rel = item.relative_to(source)
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if item.suffix in (".godot", ".tscn", ".gd", ".cfg", ".svg"):
                text = item.read_text(encoding="utf-8").replace(_NAME_TOKEN, name)
                out.write_text(text, encoding="utf-8")
            else:
                shutil.copy2(item, out)
            written.append(str(rel).replace("\\", "/"))

    return {
        "ok": True,
        "path": str(target),
        "kind": kind,
        "name": name,
        "slug": slugify(name),
        "files": sorted(written),
        "next": [
            "godot_check_project to import and validate it",
            "playtest_start, then launch the game with BGATE_TELEMETRY set",
            "BGateTelemetry.emit_event(kind, data) from your own code",
        ],
    }
