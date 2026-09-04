"""Project identity — the single row every other table hangs off."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from . import db
from .util import slugify

ENGINES = ("godot", "none")
DIMENSIONS = ("2d", "3d", "2d+3d")

# The ceilings a new project is born under. Declared once, read by the seed
# below and mirrored by the settings registry, so the panel's "default" and
# the row a fresh project actually carries never disagree.


def user_dir() -> Path:
    """The user-scoped Builders Gate directory (``~/.bgate`` unless overridden).

    BGATE_HOME exists so a test — or a user with two unrelated fleets — can
    point the registry and the active-project pointer somewhere else. It is NOT
    a project root; nothing game-shaped ever lands here.
    """
    override = os.environ.get("BGATE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".bgate"


# Which project `bgate serve` / an MCP session should assume when nothing else
# says. Separate file from the registry on purpose: the registry is a fact
# (these projects exist), the pointer is a preference (this is the one I mean),
# and blowing away the preference must never cost you the list.
ACTIVE_FILENAME = "active.json"


def _registry_path() -> Path:
    """Machine-wide registry of every project ever init'ed/selected, so a session
    whose cwd is NOWHERE NEAR the project (e.g. an MCP server spawned from a
    different repo) can still find and select it by name. Best-effort JSON —
    losing it only costs rediscovery, never data.

    Resolved through :func:`user_dir` rather than ``Path.home()`` so BGATE_HOME
    moves the registry with everything else — a module-level constant that
    baked in the home directory used to sit here and silently ignored it.
    """
    return user_dir() / "projects.json"


def _active_path() -> Path:
    return user_dir() / ACTIVE_FILENAME


def _read_registry() -> dict[str, str]:
    try:
        return {k: v for k, v in json.loads(
            _registry_path().read_text(encoding="utf-8")).items()
            if (Path(v) / db.DB_DIRNAME / db.DB_FILENAME).exists()}
    except Exception:
        return {}


def set_active(root: str | os.PathLike[str]) -> Path:
    """Remember ``root`` as the project to assume when nothing else says.

    Refuses a directory that is not a project — a pointer at nothing is worse
    than no pointer, because it makes every later tool call fail somewhere far
    from the mistake.
    """
    resolved = Path(root).expanduser().resolve()
    if not (resolved / db.DB_DIRNAME / db.DB_FILENAME).exists():
        raise LookupError(
            f"{resolved} is not a Builders Gate project — no "
            f"{db.DB_DIRNAME}/{db.DB_FILENAME}. Run `bgate adopt` (existing "
            "game) or `bgate init` (new one) there first.")
    path = _active_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"root": str(resolved)}, indent=2),
                    encoding="utf-8")
    try:
        register(resolved)
    except Exception:
        pass
    return resolved


def active_root() -> Optional[Path]:
    """The remembered project, or None. Never raises — a corrupt or stale
    pointer degrades to 'no preference', which is the pre-existing behaviour."""
    try:
        data = json.loads(_active_path().read_text(encoding="utf-8"))
        root = Path(data["root"])
    except Exception:
        return None
    return root if (root / db.DB_DIRNAME / db.DB_FILENAME).exists() else None


def clear_active() -> bool:
    """Forget the remembered project. True if there was one."""
    try:
        _active_path().unlink()
        return True
    except OSError:
        return False


def register(root: str | os.PathLike[str], name: str = "") -> None:
    """Record root in the machine-wide registry (best-effort, never raises)."""
    try:
        resolved = str(Path(root).resolve())
        if not name:
            try:
                name = get(resolved)["slug"]
            except Exception:
                name = Path(resolved).name
        reg = _read_registry()
        reg[name] = resolved
        path = _registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    except Exception:
        pass


def known_projects() -> dict[str, str]:
    """{name: root} for every registered project THAT STILL EXISTS ON DISK.

    The docstring said this before the code did: it returned the raw registry,
    dead entries and all. A registry entry is a breadcrumb, not a promise — the
    folder can be deleted, renamed or on a drive that is not plugged in — and
    the project switcher offers this list as things you can open, so a stale
    row is a menu item that fails when clicked.

    Filtered on READ rather than pruned on write, deliberately. An unplugged
    external drive is not a reason to forget a project; it is a reason not to
    offer it right now.

    ONE ENTRY PER FOLDER. The keys here are names and are unique by
    construction; the VALUES are not, and two names pointing at one folder is
    ordinary — renaming a project registers the new name without retiring the
    old one, so the same root arrives twice under two labels. Every caller
    turns this into a list of choices keyed by root, and a repeated root is a
    repeated key.

    That is not a cosmetic duplicate. The dashboard's project picker is a
    Mantine Select, which THROWS on a duplicate value rather than rendering it
    twice, and an exception thrown while rendering unmounts the React tree that
    contains it -- so the packaged app opened a window, painted nothing, and
    sat there black with the server behind it answering every request
    perfectly. One doubly-registered folder took out the whole interface.

    Comparison is normcase+normpath, so C:\\Games\\X and c:/games/x are one
    folder, which is what Windows thinks too. First registration wins: it is
    the stable choice, and the alternative (last wins) makes the menu reorder
    itself when an unrelated project is added.
    """
    seen: set[str] = set()
    out: dict[str, str] = {}
    for name, root in _read_registry().items():
        if not root or not Path(root).is_dir():
            continue
        key = os.path.normcase(os.path.normpath(root))
        if key in seen:
            continue
        seen.add(key)
        out[name] = root
    return out


def init(root: str | os.PathLike[str], name: str, pitch: str = "",
         engine: str = "godot", dimension: str = "2d") -> dict:
    """Create ``<root>/.bgate/game.db``. Idempotent: re-init updates metadata."""
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension must be one of {DIMENSIONS}, got {dimension!r}")

    Path(root).mkdir(parents=True, exist_ok=True)
    with db.tx(root) as conn:
        conn.execute(
            """
            INSERT INTO project (id, name, slug, pitch, engine, dimension)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name,
                slug = excluded.slug,
                pitch = excluded.pitch,
                engine = excluded.engine,
                dimension = excluded.dimension,
                updated_at = datetime('now')
            """,
            (name, slugify(name), pitch, engine, dimension),
        )
    register(root, slugify(name))
    _seed_doctrine(root)
    return get(root)


# THE ONE RULE THAT WORKED, SHIPPED AS A DEFAULT INSTEAD OF BEING REDISCOVERED.
#
# In the first of three benchmark games every sound effect shipped TWICE: two
# seats independently wired the same four SFX, both implementations valid, and
# the QA gate passed the duplicated build because it verified each stream was
# non-null and playing - true, twice. The two later games opened with a short
# ownership paragraph in the bible and it did not recur; in both, a producer
# seat that found a mismatched or unwired asset FILED it against the consumer
# seat instead of silently editing that seat's integration code.
#
# It is a bible SECTION and not only prompt text on purpose, and the two are not
# duplicates: seats.OWNERSHIP_RULE is the board-wide default that reaches every
# dispatched agent, and it says the bible wins. This is where a project states
# ITS answer - who owns the audio wire in THIS game - by editing one section
# somebody can read, diff and disagree with. A constraint nobody can find is a
# constraint nobody applies.
_OWNERSHIP_SECTION = (
    "Producing an artifact does not grant ownership of its INTEGRATION. The "
    "producer creates the asset; the declared consumer owns the wire that puts "
    "it in the game. Every cross-seat integration has exactly ONE owner, and a "
    "second valid implementation of the same wire is a defect - it passes every "
    "existence check twice.\n"
    "\n"
    "Default owners, until this project says otherwise:\n"
    "- audio file -> the seat that owns the gameplay EVENT wires it\n"
    "- art asset -> the seat that owns the scene/resource consuming it\n"
    "- animation -> the seat that owns the state machine\n"
    "- simulation value -> the seat that owns the UI reading it\n"
    "- death / despawn -> the seat that owns occupancy and state cleanup\n"
    "- ability -> the seat that owns the VFX it triggers\n"
    "- narrative content -> the seat that owns the gameplay trigger\n"
    "\n"
    "A producer that finds the consumer side wrong routes the work "
    "(queue_add(<owning seat>, ..., depends_on=<its own item>)) rather than "
    "fixing it in another seat's file. Edit this section to name real owners "
    "for this game; the seats read it."
)


def _seed_doctrine(root: str | os.PathLike[str]) -> None:
    """Put the ownership constraint in a new project's bible, once.

    Idempotent by TITLE, so re-running init (which is documented as safe) does
    not stack copies, and a project that deleted the section on purpose does not
    get it handed back on the next re-init... it does, and that is the accepted
    trade: re-init is rare and a duplicate title is not, so the check is cheap
    and the failure mode is one section a human deletes again.

    Never raises. A project that cannot be created because a default paragraph
    would not insert is a worse product than one with no paragraph.
    """
    try:
        from ..design import bible

        existing = {str(row.get("title") or "").strip().lower()
                    for row in bible.list_sections(root, "constraint")}
        if "integration ownership" in existing:
            return
        bible.add(root, "constraint", "Integration ownership",
                  _OWNERSHIP_SECTION, rank=0)
    except Exception:
        pass


def get(root: str | os.PathLike[str]) -> dict:
    conn = db.connect(root)
    row = conn.execute("SELECT * FROM project WHERE id = 1").fetchone()
    if row is None:
        raise LookupError(f"no Builders Gate project at {root} — run init first")
    return dict(row)


def set_dimension(root: str | os.PathLike[str], dimension: str) -> dict:
    """Correct the project's 2d/3d record after the game changed shape.

    A PROJECT CHANGES DIMENSION AND THE RECORD DID NOT FOLLOW. ``init`` writes it
    and ``adopt`` detects it, and after that there was no way to change it: a 2D
    prototype that grew a 3D scene kept reporting ``dimension: "2d"`` in
    project_status forever, with no tool on any surface that could fix it. It
    reads as cosmetic and is not — the field steers scaffolding templates and the
    wording of seat briefs, so a wrong value quietly aims the whole board at the
    wrong kind of game.

    Re-running ``init`` would have done it, and that is exactly why this exists
    instead: init also rewrites name, pitch and engine from its own defaults, so
    the available workaround was to overwrite four fields to correct one. The
    2d+3d value is there for the common real case — a 3D game with a 2D HUD, or a
    prototype mid-port — and is not a compromise between the other two.
    """
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension must be one of {DIMENSIONS}, "
                         f"got {dimension!r}")
    was = get(root).get("dimension") or ""
    with db.tx(root) as conn:
        conn.execute("UPDATE project SET dimension = ?, "
                     "updated_at = datetime('now') WHERE id = 1", (dimension,))
    if was != dimension:
        try:
            from ..board import activity

            activity.log(root, "project",
                         f"dimension {was or '(unset)'} -> {dimension}",
                         seat="director")
        except Exception:
            pass            # an unlogged correction is still a correction
    return get(root)


def game_dir(root: str | os.PathLike[str]) -> Optional[Path]:
    """Where this project's Godot project.godot actually lives, or None.

    Two entrypoints disagree about the layout: godot_scaffold (MCP) writes into
    ``<root>/game``, while ``bgate init`` and the dashboard's new-project route
    write straight into ``<root>``. Everything downstream that hardcoded one of
    the two silently did nothing for projects made the other way — the web
    export was unreachable for every CLI-created project for exactly that
    reason. Ask here instead of guessing.
    """
    base = Path(root)
    for candidate in (base / "game", base):
        if (candidate / "project.godot").is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# The scratch project — somewhere for work that belongs to no game
# ---------------------------------------------------------------------------
# The names a caller can use to ASK for it, rather than falling into it. Passing
# `project_dir="scratch"` is how you say "this one is not about any of my games"
# without leaving the directory you happen to be standing in.
SCRATCH_ALIASES = frozenset({"scratch", "global", "-"})
SCRATCH_DIRNAME = "scratch"
SCRATCH_LABEL = "Scratch"


def scratch_root(create: bool = True) -> Path:
    """``~/.bgate/scratch`` — the drop point for generations with no game.

    WHY A REAL PROJECT AND NOT A LOOSE DIRECTORY. Everything downstream of a
    generation needs a root that is a project: the artifact registry, the spend
    ledger, the activity log, ``.bgate_out``. A bare output folder would mean a
    second, thinner code path for "generations that are not part of anything",
    and the first thing anyone would ask of one is what it cost — which is a
    question only the ledger can answer. So the scratch drop point is an
    ordinary project that happens to live in the user-scoped directory, and
    every tool that writes into it does so through the code it already uses.

    It carries no game. ``init`` creates the database and nothing else — no
    Godot project, no scaffold — because this is a place to put an image, not a
    place to build. A tool that needs an engine will say so in its own words.

    Created on demand rather than at install time: a user who never generates
    outside a project never gets a directory they did not ask for.
    """
    root = user_dir() / SCRATCH_DIRNAME
    if not create:
        return root
    if not (root / db.DB_DIRNAME / db.DB_FILENAME).exists():
        init(root, SCRATCH_LABEL,
             pitch="Generations that do not belong to any one game. Created "
                   "automatically the first time a tool needed somewhere to "
                   "put its output.")
    return root


def is_scratch(root: Optional[str | os.PathLike[str]]) -> bool:
    """Is this the scratch project? Asked by anything that should SAY so."""
    if not root:
        return False
    try:
        return Path(root).resolve() == scratch_root(create=False).resolve()
    except Exception:
        return False


def resolve_alias(token: str) -> Optional[Path]:
    """``"scratch"`` / ``"global"`` -> the scratch root. None for anything else.

    Kept here so every surface spells the alias the same way; a second place
    that understood "scratch" would be a second place to forget it.
    """
    return (scratch_root() if str(token or "").strip().lower() in SCRATCH_ALIASES
            else None)


def require_root(start: Optional[str | os.PathLike[str]] = None, *,
                 scratch: bool = False) -> Path:
    """Find the enclosing project or explain how to make one.

    ``scratch=True`` puts the scratch project at the BOTTOM of the chain instead
    of raising — for callers whose work has somewhere to go even when no game
    does. It is deliberately last, below the remembered active project, so it
    only ever catches someone who has no project at all: anyone who has run
    `bgate init`, `bgate adopt` or `bgate use` keeps landing in their own work,
    and a mistyped directory keeps failing loudly instead of quietly filling a
    folder nobody looks in.
    """
    root = db.resolve_root(start)
    if root is not None:
        return root

    # LAST resort, deliberately below the cwd walk-up: `bgate use` is a
    # preference, and standing inside a project is a statement of intent that
    # must keep beating it. This slot used to be a hard failure, which is why
    # the only way to work from outside a project tree was exporting BGATE_ROOT
    # by hand.
    remembered = active_root()
    if remembered is not None:
        return remembered

    if scratch:
        return scratch_root()

    known = known_projects()
    hint = (f" Known projects (`bgate projects`; select with `bgate use <name>`): "
            f"{known}" if known else " Run `bgate init` or `bgate adopt` first.")
    raise LookupError(
        f"no .bgate project found at or above {Path(start or os.getcwd()).resolve()}."
        + hint
    )
