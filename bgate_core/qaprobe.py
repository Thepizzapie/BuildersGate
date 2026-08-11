"""What the QA probe drives, DECLARED per project instead of assumed.

The headless probe used to be a 2D fighting game with the serial numbers filed
off. It only accepted a scene containing child nodes literally named ``Player``
and ``Opponent``; it sampled ``player_hp`` / ``opponent_hp`` /
``player_stamina`` whether or not the game had such a thing; and it took the
tick authority by calling ``sim_tick()`` on the scene root. Every project that
was not that fighter got "no scene with both a Player and an Opponent node was
found" and a bot that could not fail, because it never sampled anything.

A CONTRACT says what the probe drives:

* ``scene``   — the .tscn to instance.
* ``actors``  — the nodes worth watching, each with a stable key, addressed by
  node path or by ``find_child`` name.
* ``samples`` — which numeric property comes off which actor, and what the
  sample key is called (that key is what an expectation and a stored baseline
  address, so it is part of the contract, not an implementation detail).
* ``derived`` — values computed from other sample keys. Today that is one kind,
  ``abs_diff``, which is where the fighter's ``distance`` comes from.
* ``tick``    — how the sim advances: a named method on a controller node, or
  plain engine frames when the game has no such method.

It lives in the per-project workspace doc store (seat ``qa``, key
``probe_contract``) beside the bot roster, because that is where every other
per-project seat setting already lives — one table, no schema churn.

DERIVATION IS THE DEFAULT. Nobody should have to author JSON to get a first
probe, so when no contract is stored one is derived from the real scene files
(via scenewire.outline, the same reader scene_outline uses) and persisted so it
can then be edited. Derivation is deliberately loud about how it chose: the
contract carries ``why`` and ``alternatives``, and any reason it could not
decide lands in ``issues`` rather than in a green run that sampled nothing.

THE FIGHTING SHAPE IS PINNED. A scene carrying both ``Player`` and ``Opponent``
derives to exactly the six sample keys the old hardcoded probe emitted. Those
keys are recorded in every stored baseline and in every saved expectation on
disk, so deriving "something reasonable" for that shape would quietly invalidate
history that took real runs to build.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from . import scenewire, workspace

SEAT = "qa"
KEY = "probe_contract"

# The legacy probe looked here first and only then at the project's main scene.
# Kept in that order, because a game whose gameplay lives in scenes/main.tscn
# while its declared main scene is the title screen is common, and reversing
# the two would silently repoint an existing project's probe at a menu.
LEGACY_SCENE = "res://scenes/main.tscn"

# What a node has to be before it is worth watching. Roles come from
# scenewire.role_for, which already folds path, script name and type together.
ACTOR_ROLES = {"character", "enemy"}
ACTOR_TYPES = ("CharacterBody2D", "CharacterBody3D", "RigidBody2D",
               "RigidBody3D", "CharacterBody", "AnimatableBody2D")

# Methods a project might use to advance one deterministic step. The probe takes
# the tick authority only when one of these exists; otherwise it lets the engine
# process normally, which is the honest answer for a game that has no fixed step.
TICK_METHODS = ("sim_tick", "tick", "step")

MAX_ACTORS = 4
MAX_PROPS_PER_ACTOR = 4

# Runtime state worth graphing, strongest first. A game's interesting number is
# almost always one of these and almost never the fortieth @export knob.
_VITALS = ("hp", "health", "stamina", "energy", "shield", "armor", "ammo",
           "mana", "lives", "score", "fuel", "oxygen", "charge", "morale",
           "money", "credits", "hunger", "heat", "speed", "combo")

# Names that read as tuning, not state. A fleshed-out character script carries
# dozens of these and none of them move while the game runs, so sampling them
# would fill the table with constants and bury the three numbers that change.
_TUNING = ("damage", "cost", "scale", "chance", "duration", "threshold",
           "window", "telegraph", "recover", "regen", "drain", "velocity",
           "gravity", "reach", "seed", "cooldown", "fatigue", "offset",
           "radius", "margin", "delay", "_time", "_ticks")

# `var hp: float`, `var hp := 100.0`, `@export var max_hp := 150.0`, and the one
# that matters most in practice: `var hp := MAX_HP`, where nothing on the line
# says the type. Declaring health against a constant is normal GDScript, and a
# stricter reading of the line derived a probe that could not see the health bar
# in a real project it was tested against.
_VAR_LINE = re.compile(
    r"^(?P<export>@export\s+)?var\s+(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*"
    r"(?P<rest>.*)$", re.MULTILINE)
_NUMERIC_DECL = re.compile(r"^:\s*(int|float)\b|^:?=\s*-?\d")
_TYPED_DECL = re.compile(r"^:\s*[A-Za-z]")

# Scratch scenes agents leave behind. A project a few months into an agent
# pipeline can carry eighty .tscn files with a third of them one-shot screenshot
# rigs from finished work items; probing one of those would be a verdict about a
# screenshot.
_SCRATCH_RE = re.compile(r"^(_|item\d|qa\d)", re.IGNORECASE)


# --- reading the project ----------------------------------------------------

def main_scene(game_dir: Path) -> str:
    """``application/run/main_scene`` from project.godot, or ''."""
    try:
        text = (game_dir / "project.godot").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r'^run/main_scene\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else ""


def _disk(game_dir: Path, res_path: str) -> Optional[Path]:
    if not res_path.startswith("res://"):
        return None
    return game_dir / res_path[len("res://"):]


def _outline(game_dir: Path, res_path: str) -> list[dict]:
    """The scene's nodes, or [] for anything unreadable.

    Reuses scenewire.outline rather than parsing .tscn a second time — a second
    parser is a second set of bugs, and this one already resolves scripts.
    """
    path = _disk(game_dir, res_path)
    if path is None or not path.is_file():
        return []
    try:
        return scenewire.outline(path.read_text(encoding="utf-8",
                                                errors="replace"))
    except Exception:
        return []


def _script_text(game_dir: Path, res_path: str) -> str:
    path = _disk(game_dir, res_path or "")
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --- deriving ---------------------------------------------------------------

def _key_for(name: str, taken: set) -> str:
    """A node name as a sample-key prefix: Player -> player, EnemyA -> enemy_a."""
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    key = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").lower() or "actor"
    base, n = key, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    taken.add(key)
    return key


def _actors_in(nodes: list[dict]) -> list[dict]:
    """The nodes worth watching, shallowest first, capped."""
    found = [n for n in nodes
             if n.get("role") in ACTOR_ROLES or n.get("type") in ACTOR_TYPES]
    found.sort(key=lambda n: (n.get("path", "").count("/"), n.get("path", "")))
    return found[:MAX_ACTORS]


def _numeric_props(game_dir: Path, node: dict) -> list[str]:
    """Numeric runtime state the actor's script declares.

    Plain ``var`` beats ``@export``: a boxer's hp and stamina are plain vars and
    its forty @export knobs are tuning that never moves during a match. Only
    when a script has no plain numeric state at all do the exports get a look,
    because a sample table of constants still beats an empty one.
    """
    text = _script_text(game_dir, node.get("script", ""))
    if not text:
        return []
    plain, exported = [], []
    for m in _VAR_LINE.finditer(text):
        name, rest = m.group("name"), m.group("rest").strip()
        if name.startswith("_"):
            continue                      # private by convention
        if any(t in name for t in _TUNING) or name.startswith("max_"):
            continue
        numeric = bool(_NUMERIC_DECL.match(rest))
        if not numeric:
            # Nothing on the line says the type. A name from the vitals list is
            # taken at its word; anything else with a declared class is not a
            # number, and anything else at all is a coin flip not worth taking.
            numeric = name in _VITALS and not _TYPED_DECL.match(rest)
        if not numeric:
            continue
        (exported if m.group("export") else plain).append(name)
    pool = plain or exported
    seen, out = set(), []
    for name in pool:
        if name not in seen:
            seen.add(name)
            out.append(name)
    out.sort(key=lambda n: (_VITALS.index(n) if n in _VITALS else len(_VITALS), n))
    return out[:MAX_PROPS_PER_ACTOR]


def _position_props(node: dict) -> list[str]:
    """Where the actor is. Nothing else survives being wrong as cheaply."""
    kind = node.get("type", "")
    if "3D" in kind:
        return ["position.x", "position.y", "position.z"]
    if "2D" in kind or node.get("instance"):
        return ["position.x", "position.y"]
    return []


def _tick_for(game_dir: Path, nodes: list[dict]) -> dict:
    """How the sim advances. A named method if the scene root has one, else the
    engine's own frames — saying "frames" out loud beats calling a method that
    is not there and reporting a match that never advanced."""
    root_node = next((n for n in nodes if n.get("path") == "."), None)
    text = _script_text(game_dir, (root_node or {}).get("script", ""))
    for name in TICK_METHODS:
        if re.search(rf"^func\s+{name}\s*\(", text, re.MULTILINE):
            return {"mode": "method", "node": "", "method": name}
    return {"mode": "frames", "node": "", "method": ""}


def _legacy_fight_contract(scene: str, nodes: list[dict],
                           tick: dict) -> dict:
    """The exact shape the hardcoded probe emitted, for the game it was written
    for. Pinned rather than re-derived because these six key names are what
    every stored baseline and every saved expectation on that project already
    address; deriving something merely reasonable would silently orphan them."""
    return {
        "scene": scene,
        "actors": [{"key": "player", "path": "", "find": "Player"},
                   {"key": "opponent", "path": "", "find": "Opponent"}],
        "samples": [
            {"key": "player_x", "actor": "player",
             "property": "position.x", "round": 0.01},
            {"key": "opponent_x", "actor": "opponent",
             "property": "position.x", "round": 0.01},
            {"key": "player_hp", "actor": "player", "property": "hp"},
            {"key": "opponent_hp", "actor": "opponent", "property": "hp"},
            {"key": "player_stamina", "actor": "player", "property": "stamina"},
        ],
        "derived": [{"key": "distance", "kind": "abs_diff",
                     "of": ["player_x", "opponent_x"], "round": 0.01}],
        "tick": tick,
        "shape": "fight",
    }


def _contract_from(game_dir: Path, scene: str, nodes: list[dict]) -> dict:
    """One scene's outline into a contract."""
    tick = _tick_for(game_dir, nodes)
    names = {n.get("name") for n in nodes}
    if "Player" in names and "Opponent" in names:
        return _legacy_fight_contract(scene, nodes, tick)

    actors, samples, taken = [], [], set()
    for node in _actors_in(nodes):
        key = _key_for(node.get("name", ""), taken)
        actors.append({"key": key, "path": node.get("path", ""),
                       "find": node.get("name", "")})
        for prop in _position_props(node):
            samples.append({"key": f"{key}_{prop.split('.')[-1]}", "actor": key,
                            "property": prop, "round": 0.01})
        for prop in _numeric_props(game_dir, node):
            samples.append({"key": f"{key}_{prop}", "actor": key,
                            "property": prop})
    derived = []
    if len(actors) == 2 and all(any(s["key"] == f"{a['key']}_x" for s in samples)
                                for a in actors):
        derived.append({"key": "distance", "kind": "abs_diff", "round": 0.01,
                        "of": [f"{actors[0]['key']}_x", f"{actors[1]['key']}_x"]})
    return {"scene": scene, "actors": actors, "samples": samples,
            "derived": derived, "tick": tick, "shape": "generic"}


def _sweep(game_dir: Path, limit: int = 250) -> list[str]:
    """Every non-scratch scene under scenes/, so a project whose main scene is a
    title screen still gets a probe pointed at something that moves."""
    scenes = game_dir / "scenes"
    if not scenes.is_dir():
        return []
    out = []
    for path in sorted(scenes.glob("*.tscn"))[:limit]:
        if _SCRATCH_RE.match(path.stem):
            continue
        out.append("res://scenes/" + path.name)
    return out


def derive(game_dir) -> dict:
    """Read the project and answer what the probe should drive.

    Preference order, and it says which one it used in ``why``: the legacy
    scenes/main.tscn, then the project's declared main scene, then the scene
    under scenes/ with the most actors in it. A tie goes to the shorter name,
    which is reliably the real one rather than the variant.
    """
    game_dir = Path(game_dir)
    issues: list[str] = []
    declared = main_scene(game_dir)

    preferred = [p for p in (LEGACY_SCENE, declared) if p]
    for scene in preferred:
        nodes = _outline(game_dir, scene)
        if not nodes:
            continue
        contract = _contract_from(game_dir, scene, nodes)
        if contract["actors"]:
            contract["why"] = (
                f"{scene} is the project's main scene and has "
                f"{len(contract['actors'])} actor node(s) in it"
                if scene == declared else
                f"{scene} is the conventional gameplay scene and has "
                f"{len(contract['actors'])} actor node(s) in it")
            contract["alternatives"] = []
            contract["issues"] = issues
            contract["source"] = "derived"
            return contract

    if declared:
        issues.append(
            f"the project's main scene ({declared}) has no actor node in it - "
            "no character, enemy or physics body - so the probe cannot watch it")
    else:
        issues.append(
            "project.godot declares no application/run/main_scene, so there is "
            "no scene to fall back on")

    scored = []
    for scene in _sweep(game_dir):
        if scene in preferred:
            continue
        nodes = _outline(game_dir, scene)
        if not nodes:
            continue
        contract = _contract_from(game_dir, scene, nodes)
        if contract["actors"] and contract["samples"]:
            scored.append((-len(contract["actors"]), len(scene), scene, contract))
    scored.sort(key=lambda row: row[:3])

    if not scored:
        return {"scene": "", "actors": [], "samples": [], "derived": [],
                "tick": {"mode": "frames", "node": "", "method": ""},
                "shape": "none", "source": "none", "why": "",
                "alternatives": [],
                "issues": issues + [
                    "no scene under game/scenes has an actor node with a numeric "
                    "property on it, so there is nothing to sample. Declare the "
                    "probe contract by hand: scene, actors (key + node name), "
                    "and at least one sample (key, actor, property)"]}

    _n, _len, scene, contract = scored[0]
    contract["source"] = "derived"
    contract["why"] = (
        f"no actor node in the main scene, so the probe fell back to {scene} - "
        f"the scene under game/scenes with the most actors ({len(contract['actors'])})")
    contract["alternatives"] = [row[2] for row in scored[1:6]]
    contract["issues"] = issues
    return contract


# --- validating -------------------------------------------------------------

def normalise(raw) -> tuple[dict, list[str]]:
    """Clean a hand-edited contract and say what is wrong with it.

    Returns the usable contract and the list of complaints. A broken contract is
    NOT swapped for a derived one behind the human's back: they wrote something,
    and a probe that quietly drives a different scene than the one on screen is
    the whole failure this module exists to end.
    """
    issues: list[str] = []
    raw = raw if isinstance(raw, dict) else {}

    scene = str(raw.get("scene", "") or "").strip()
    if scene and not scene.startswith("res://"):
        scene = "res://" + scene.lstrip("/")
    if not scene:
        issues.append("no scene declared - the probe has nothing to instance")

    actors, keys = [], set()
    for i, a in enumerate(raw.get("actors") or []):
        if not isinstance(a, dict):
            issues.append(f"actors[{i}] is not an object")
            continue
        key = str(a.get("key", "") or "").strip()
        path = str(a.get("path", "") or "").strip()
        find = str(a.get("find", "") or "").strip()
        if not key:
            issues.append(f"actors[{i}] has no key")
            continue
        if not path and not find:
            issues.append(
                f"actor '{key}' has neither a path nor a find name, so the "
                "probe cannot locate it in the scene")
            continue
        if key in keys:
            issues.append(f"actor key '{key}' appears twice")
            continue
        keys.add(key)
        actors.append({"key": key, "path": path, "find": find})
    if not actors:
        issues.append("no actors declared - the probe would sample nothing")

    samples, sample_keys = [], set()
    for i, s in enumerate(raw.get("samples") or []):
        if not isinstance(s, dict):
            issues.append(f"samples[{i}] is not an object")
            continue
        key = str(s.get("key", "") or "").strip()
        actor = str(s.get("actor", "") or "").strip()
        prop = str(s.get("property", "") or "").strip()
        if not key or not prop:
            issues.append(f"samples[{i}] needs both a key and a property")
            continue
        if actor not in keys:
            issues.append(
                f"sample '{key}' reads off actor '{actor}', which is not declared")
            continue
        if key in sample_keys:
            issues.append(f"sample key '{key}' appears twice")
            continue
        try:
            rounding = float(s.get("round", 0) or 0)
        except (TypeError, ValueError):
            rounding = 0.0
        sample_keys.add(key)
        entry = {"key": key, "actor": actor, "property": prop}
        if rounding > 0:
            entry["round"] = rounding
        samples.append(entry)

    derived = []
    for i, d in enumerate(raw.get("derived") or []):
        if not isinstance(d, dict):
            issues.append(f"derived[{i}] is not an object")
            continue
        key = str(d.get("key", "") or "").strip()
        kind = str(d.get("kind", "") or "").strip() or "abs_diff"
        of = [str(x) for x in (d.get("of") or []) if str(x).strip()]
        if kind != "abs_diff":
            issues.append(f"derived '{key}' kind '{kind}' is not one of: abs_diff")
            continue
        if not key or len(of) != 2:
            issues.append(f"derived '{key}' needs a key and two sample keys in 'of'")
            continue
        unknown = [x for x in of if x not in sample_keys]
        if unknown:
            issues.append(
                f"derived '{key}' reads sample key(s) {', '.join(unknown)} that "
                "nothing samples")
            continue
        try:
            rounding = float(d.get("round", 0.01) or 0)
        except (TypeError, ValueError):
            rounding = 0.01
        sample_keys.add(key)
        derived.append({"key": key, "kind": kind, "of": of, "round": rounding})

    tick = raw.get("tick") if isinstance(raw.get("tick"), dict) else {}
    mode = str(tick.get("mode", "") or "").strip().lower()
    method = str(tick.get("method", "") or "").strip()
    if mode == "method" and not method:
        issues.append("tick.mode is 'method' but no method is named - "
                      "falling back to engine frames")
        mode = "frames"
    if mode not in ("method", "frames"):
        mode = "method" if method else "frames"
    tick = {"mode": mode, "node": str(tick.get("node", "") or "").strip(),
            "method": method if mode == "method" else ""}

    if not samples:
        issues.append("no samples declared - a probe that samples nothing "
                      "cannot pass, fail or regress")

    # Provenance survives the round trip. A derived contract is persisted the
    # first time anything reads it, so without carrying this the very next read
    # would report a machine guess as something a human declared — and "somebody
    # chose this scene on purpose" is exactly the wrong thing to believe about a
    # fallback pick.
    source = str(raw.get("source", "") or "declared")
    if source not in ("derived", "declared"):
        source = "declared"

    return ({"scene": scene, "actors": actors, "samples": samples,
             "derived": derived, "tick": tick, "source": source,
             "shape": str(raw.get("shape", "") or "custom")}, issues)


def sample_keys(contract: dict) -> list[str]:
    """Every key a run of this contract can produce, in sample-table order."""
    keys = [s["key"] for s in contract.get("samples") or []]
    keys += [d["key"] for d in contract.get("derived") or []]
    return keys


def fingerprint(contract: dict) -> str:
    """What a baseline was measured under. Two runs whose fingerprints differ
    were not measuring the same thing, and the diff has to say so."""
    tick = contract.get("tick") or {}
    return "|".join([
        str(contract.get("scene", "")),
        ",".join(sorted(sample_keys(contract))),
        str(tick.get("mode", "")) + ":" + str(tick.get("method", "")),
    ])


# --- storage ----------------------------------------------------------------

def stored(root) -> dict:
    doc = workspace.get(root, SEAT, KEY)
    return doc if isinstance(doc, dict) else {}


# What actually gets stored. `issues`, `source` and `sample_keys` are recomputed
# on every read, and writing them down would let a complaint outlive its cause.
STORED_FIELDS = ("scene", "actors", "samples", "derived", "tick", "shape",
                 "source", "why", "alternatives")


def save(root, contract: dict) -> dict:
    """Persist the contract, honouring the workspace store's lost-update check.

    The ``_version`` a read stamped in rides back through, so two tabs editing
    the contract get a 409 rather than one of them silently winning.
    """
    body = {k: contract[k] for k in STORED_FIELDS if k in contract}
    return workspace.set(root, SEAT, KEY, body,
                         if_version=contract.get(workspace.VERSION_KEY))


def load(root, game_dir, *, persist: bool = True) -> dict:
    """The contract this project's probe runs under.

    A stored contract wins, broken or not (see normalise). Otherwise one is
    derived and persisted, so the next visit to the seat shows an editable
    starting point instead of re-guessing.
    """
    doc = stored(root)
    if doc.get("scene") or doc.get("actors") or doc.get("samples"):
        contract, issues = normalise(doc)
        contract["issues"] = issues
        contract["why"] = str(doc.get("why", "") or "")
        contract["alternatives"] = list(doc.get("alternatives") or [])
        contract[workspace.VERSION_KEY] = doc.get(workspace.VERSION_KEY, "")
        return contract

    contract = derive(game_dir)
    if persist and contract.get("actors") and contract.get("samples"):
        try:
            contract[workspace.VERSION_KEY] = save(root, contract).get(
                workspace.VERSION_KEY, "")
        except Exception:
            # A read-only or locked db must not stop the run; the contract is
            # still correct in memory, it just has to be re-derived next time.
            contract.setdefault("issues", []).append(
                "the derived contract could not be saved, so it will be "
                "re-derived on every run until it is")
    return contract
