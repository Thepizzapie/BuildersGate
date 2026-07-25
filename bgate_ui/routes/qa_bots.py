"""QA bot playtest endpoints — drive the real game headless and report.

The reliable QA method for this project is a RUNTIME PROBE: a headless GDScript
that instances the fight scene, drives it one deterministic sim tick at a time
while pressing real Input actions on a scripted schedule, samples the two
fighters' positions / hp / stamina every few ticks, and prints one JSON line.
That is the ground truth a bot match reports on — not static reasoning about the
code, but what the running engine actually did with the inputs.

Auto-registers via routes/__init__.py (module-level ``router``). Everything that
can fail (godot discovery, the run, JSON parsing, project.godot reading) is
wrapped so a broken game or a missing engine returns a structured error rather
than a 500.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from fastapi import APIRouter, HTTPException

from bgate_adapters import godot as _godot
from bgate_ui.deps import root

router = APIRouter()

# Built-in Godot input actions we never surface as game moves.
_BUILTIN_ACTIONS = re.compile(r"^(ui_|spatial_editor|editor_)")

# Hard cap so a runaway bot can't outlive the run_script timeout.
_MAX_TICKS = 3000
_DEFAULT_TICKS = 240
_SAMPLE_EVERY = 10


def _game_dir():
    """The Godot project dir for the active project: <root>/game."""
    return root() / "game"


# --- the probe -------------------------------------------------------------
# extends SceneTree so it owns the main loop headless. _initialize() instances
# the fight scene; _process() runs once per real engine frame — which is what
# keeps Godot's Input "just_pressed" edge working (it clears each real frame) —
# and drives EXACTLY one deterministic fight.sim_tick() per frame, so one frame
# == one sim tick == the schedule's tick units. Fighters already set_process
# (false) on themselves; fight.sim_tick() advances both of them in fixed order.
_PROBE_TEMPLATE = r"""
extends SceneTree

const SCHEDULE_JSON := %SCHEDULE_JSON%
const MAX_TICKS := %MAX_TICKS%
const SAMPLE_EVERY := %SAMPLE_EVERY%

var _fight: Node = null
var _player: Node = null
var _opponent: Node = null
var _schedule: Array = []
var _active := {}          # action -> tick at which to release
var _tick := 0
var _samples: Array = []
var _notes: Array = []
var _done := false

func _num(obj, prop):
	# Best-effort numeric property read; null if the fighter doesn't expose it.
	if obj == null:
		return null
	var v = obj.get(prop)
	if typeof(v) == TYPE_FLOAT or typeof(v) == TYPE_INT:
		return v
	return null

func _load_fight() -> void:
	var candidates: Array = ["res://scenes/main.tscn"]
	var main_scene := str(ProjectSettings.get_setting("application/run/main_scene", ""))
	if main_scene != "" and not candidates.has(main_scene):
		candidates.append(main_scene)
	for path in candidates:
		if not ResourceLoader.exists(path):
			continue
		var packed = load(path)
		if packed == null or not (packed is PackedScene):
			continue
		var inst = packed.instantiate()
		if inst == null:
			continue
		get_root().add_child(inst)
		var pl = inst.find_child("Player", true, false)
		var op = inst.find_child("Opponent", true, false)
		if pl != null and op != null:
			_fight = inst
			_player = pl
			_opponent = op
			_notes.append("loaded fight scene: " + path)
			# We are the tick authority — stop the engine from ALSO auto-ticking
			# the controller each frame; we call sim_tick() ourselves.
			if _fight.has_method("set_process"):
				_fight.set_process(false)
			return
		# Wrong scene (no two fighters) — drop it and try the next candidate.
		get_root().remove_child(inst)
		inst.free()
	_notes.append("no scene with both a Player and an Opponent node was found")

func _initialize() -> void:
	var parsed = JSON.parse_string(SCHEDULE_JSON)
	if parsed is Array:
		_schedule = parsed
	else:
		_notes.append("schedule did not parse to an array; running an idle match")
	# Wrap scene loading so a broken game can't crash the probe with no output.
	_load_fight()

func _sample() -> void:
	if _player == null or _opponent == null:
		return
	_samples.append({
		"tick": _tick,
		"player_x": snappedf(_player.position.x, 0.01),
		"opponent_x": snappedf(_opponent.position.x, 0.01),
		"distance": snappedf(absf(_player.position.x - _opponent.position.x), 0.01),
		"player_hp": _num(_player, "hp"),
		"opponent_hp": _num(_opponent, "hp"),
		"player_stamina": _num(_player, "stamina"),
	})

func _emit_result() -> void:
	if _done:
		return
	_done = true
	# Release anything still held so we leave Input clean (defensive).
	for a in _active.keys():
		if InputMap.has_action(a):
			Input.action_release(a)
	var final = _samples[-1] if _samples.size() > 0 else {}
	var summary := {
		"ticks": _tick,
		"requested_ticks": MAX_TICKS,
		"sample_count": _samples.size(),
		"samples": _samples,
		"final": final,
		"notes": _notes,
		"has_fight": _player != null and _opponent != null,
	}
	print("PROBE_JSON:" + JSON.stringify(summary))

func _process(_delta: float) -> bool:
	# No fight loaded: report once and quit rather than spin.
	if _player == null or _opponent == null:
		_emit_result()
		return true

	# Fire scheduled presses whose tick has arrived.
	for entry in _schedule:
		if typeof(entry) != TYPE_DICTIONARY:
			continue
		if int(entry.get("at_tick", -1)) == _tick:
			var act := str(entry.get("action", ""))
			if act != "" and InputMap.has_action(act):
				Input.action_press(act)
				var hold := maxi(1, int(entry.get("hold_ticks", 1)))
				_active[act] = _tick + hold

	# Release presses whose hold window has elapsed.
	for a in _active.keys():
		if _tick >= int(_active[a]):
			if InputMap.has_action(a):
				Input.action_release(a)
			_active.erase(a)

	# Advance exactly one deterministic sim tick.
	if _fight.has_method("sim_tick"):
		_fight.sim_tick()

	if _tick % SAMPLE_EVERY == 0:
		_sample()

	_tick += 1
	if _tick > MAX_TICKS:
		_sample()
		_emit_result()
		return true
	return false
"""


def _build_probe(actions: list, ticks: int) -> str:
    schedule = []
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        act = str(a.get("action", "")).strip()
        if not act:
            continue
        try:
            at = int(a.get("at_tick", 0))
        except (TypeError, ValueError):
            at = 0
        try:
            hold = int(a.get("hold_ticks", 1))
        except (TypeError, ValueError):
            hold = 1
        schedule.append({"action": act, "at_tick": max(0, at),
                         "hold_ticks": max(1, hold)})
    # Embed the schedule as a JSON *string* the GDScript parses at runtime —
    # avoids hand-assembling a GDScript array literal. Escape for a GDScript
    # double-quoted string literal.
    schedule_json = json.dumps(schedule)
    gd_literal = '"' + schedule_json.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return (
        _PROBE_TEMPLATE
        .replace("%SCHEDULE_JSON%", gd_literal)
        .replace("%MAX_TICKS%", str(int(ticks)))
        .replace("%SAMPLE_EVERY%", str(_SAMPLE_EVERY))
    )


def _parse_probe_json(stdout: str) -> Optional[dict]:
    """Pull the PROBE_JSON:{...} line out of Godot's chatty stdout."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("PROBE_JSON:"):
            blob = line[len("PROBE_JSON:"):]
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                return None
    return None


@router.post("/api/qa-bots/run")
def qa_bots_run(payload: Optional[dict] = None) -> dict:
    """Run one bot match: assemble the probe from the bot's action schedule,
    drive the real fight headless, and return the parsed sample summary."""
    payload = payload or {}
    actions = payload.get("actions") or []
    if not isinstance(actions, list):
        raise HTTPException(400, "actions must be a list")

    try:
        ticks = int(payload.get("ticks", _DEFAULT_TICKS))
    except (TypeError, ValueError):
        ticks = _DEFAULT_TICKS
    ticks = max(1, min(ticks, _MAX_TICKS))

    project = _game_dir()
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no Godot project at {project}",
                "summary": None, "stdout": "", "stderr": "", "errors": []}

    script = _build_probe(actions, ticks)
    try:
        result = _godot.run_script(script, str(project), timeout=90)
    except _godot.GodotNotFound as exc:
        return {"ok": False, "error": str(exc), "summary": None,
                "stdout": "", "stderr": "", "errors": []}
    except Exception as exc:  # never 500 on a probe run
        return {"ok": False, "error": f"probe run failed: {exc}",
                "summary": None, "stdout": "", "stderr": "", "errors": []}

    stdout = result.get("stdout", "")
    summary = _parse_probe_json(stdout)
    ran_ok = bool(result.get("ok")) and summary is not None and summary.get("has_fight")
    return {
        "ok": ran_ok,
        "summary": summary,
        "stdout": stdout,
        "stderr": result.get("stderr", ""),
        "errors": result.get("errors", []),
        "seconds": result.get("seconds"),
        "exit_code": result.get("exit_code"),
        "error": None if summary is not None else (
            result.get("error") or "probe produced no PROBE_JSON line"),
    }


@router.get("/api/qa-bots/actions")
def qa_bots_actions() -> dict:
    """The game's own input action names (project.godot [input] section), so the
    UI can offer a dropdown of real actions. Best-effort — returns a sensible
    default list if the file can't be read."""
    default = ["move_left", "move_right", "jump", "jab", "hook",
               "block", "duck", "kick_light", "kick_heavy"]
    try:
        pg = _game_dir() / "project.godot"
        text = pg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"actions": default, "source": "default"}

    actions: list[str] = []
    in_input = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_input = stripped.lower() == "[input]"
            continue
        if not in_input:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
        if m:
            name = m.group(1)
            if not _BUILTIN_ACTIONS.match(name) and name not in actions:
                actions.append(name)
    if not actions:
        return {"actions": default, "source": "default"}
    return {"actions": actions, "source": "project.godot"}
