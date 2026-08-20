"""QA bot playtest endpoints — drive the real game headless and report.

The reliable QA method for this project is a RUNTIME PROBE: a headless GDScript
that instances a scene, drives it one deterministic tick at a time while
pressing real Input actions on a scripted schedule, samples declared node
properties every few ticks, and prints one JSON line. That is the ground truth a
bot match reports on — not static reasoning about the code, but what the running
engine actually did with the inputs.

WHAT IT DRIVES IS DECLARED, NOT ASSUMED. The probe used to be a 2D fighter with
the serial numbers filed off: it only accepted a scene containing child nodes
literally named Player and Opponent, sampled player_hp / opponent_hp /
player_stamina whether the game had such a thing or not, and called sim_tick()
on the scene root. Every other game got "no scene with both a Player and an
Opponent node was found" and a bot that could not fail because it sampled
nothing. bgate_core.qaprobe now holds a per-project CONTRACT — scene, actors,
sample keys, tick authority — derived from the real scene when nobody has
declared one, and this module assembles the probe from it.

A run used to end there: samples, and no verdict. It could not fail, so it could
not gate — the QA seat rendered a green "drove the game" for a match that proved
nothing. A bot now carries ``expect`` entries evaluated SERVER-SIDE against the
samples, and the run reports pass / fail / error / unknown. A bot with no
expectations reports ``unknown``, never ``pass``: proving nothing is not passing.

Every run is persisted to ``qa_bot_run`` and the last one that actually ran is
kept as that bot's baseline, so the next run can say what moved.

Auto-registers via routes/__init__.py (module-level ``router``). Everything that
can fail (godot discovery, the run, JSON parsing, project.godot reading) is
wrapped so a broken game or a missing engine returns a structured error rather
than a 500.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from bgate_adapters import godot as _godot
from bgate_core import db, jobs, qaprobe
from bgate_core import workspace as _ws
from bgate_ui import api
from bgate_ui.deps import root
from bgate_ui.routes import jobs as jobs_api

router = APIRouter()

# Built-in Godot input actions we never surface as game moves.
_BUILTIN_ACTIONS = re.compile(r"^(ui_|spatial_editor|editor_)")

# Hard cap so a runaway bot can't outlive the run_script timeout.
_MAX_TICKS = 3000
_DEFAULT_TICKS = 240
_SAMPLE_EVERY = 10
# The probe's own wall clock. Was hardcoded at 90; now the default for a
# caller-supplied value that is clamped to the same [5, 600] as every other
# engine timeout.
_DEFAULT_PROBE_TIMEOUT = 90


def _game_dir():
    """The Godot project dir for the active project: <root>/game."""
    return root() / "game"


# --- the probe -------------------------------------------------------------
# extends SceneTree so it owns the main loop headless. _initialize() instances
# the contract's scene; _process() runs once per real engine frame — which is
# what keeps Godot's Input "just_pressed" edge working (it clears each real
# frame) — and advances EXACTLY one deterministic step per frame, so one frame
# == one tick == the schedule's tick units.
#
# The step is the contract's business. When it names a method the probe becomes
# the tick authority and calls it (and set_process(false) on the node that owns
# it, so the engine does not ALSO advance it each frame — double-ticking a fixed
# step is a whole class of samples that look like a physics bug). When it does
# not, the engine processes normally and a tick is simply a frame. Saying which
# one happened is why the summary carries `tick_mode`.
_PROBE_TEMPLATE = r"""
extends SceneTree

const SCHEDULE_JSON := %SCHEDULE_JSON%
const CONTRACT_JSON := %CONTRACT_JSON%
const MAX_TICKS := %MAX_TICKS%
const SAMPLE_EVERY := %SAMPLE_EVERY%

var _contract := {}
var _scene_root: Node = null
var _tick_node: Node = null
var _tick_method := ""
var _tick_mode := "frames"
var _actors := {}          # contract actor key -> Node
var _missing: Array = []   # actor keys the scene did not contain
var _schedule: Array = []
var _active := {}          # action -> tick at which to release
var _tick := 0
var _samples: Array = []
var _notes: Array = []
var _done := false

func _read(node, prop):
	# Best-effort numeric read. A dotted property ("position.x") goes through
	# get_indexed, so a contract can name a sub-property without the probe
	# knowing anything about Vector2. Anything the node does not expose, or
	# exposes as a non-number, samples as null rather than killing the match.
	if node == null:
		return null
	var v = null
	if prop.find(".") >= 0:
		v = node.get_indexed(NodePath(prop.replace(".", ":")))
	else:
		v = node.get(prop)
	if typeof(v) == TYPE_FLOAT or typeof(v) == TYPE_INT:
		return v
	return null

func _load_scene() -> void:
	var path := str(_contract.get("scene", ""))
	if path == "":
		_notes.append("the probe contract names no scene, so there is nothing to drive")
		return
	if not ResourceLoader.exists(path):
		_notes.append("the probe contract names " + path + ", which does not exist in this project")
		return
	var packed = load(path)
	if packed == null or not (packed is PackedScene):
		_notes.append(path + " is not a PackedScene")
		return
	var inst = packed.instantiate()
	if inst == null:
		_notes.append(path + " could not be instanced")
		return
	get_root().add_child(inst)
	_scene_root = inst
	_notes.append("loaded " + path)

	for entry in _contract.get("actors", []):
		if typeof(entry) != TYPE_DICTIONARY:
			continue
		var key := str(entry.get("key", ""))
		var node_path := str(entry.get("path", ""))
		var find_name := str(entry.get("find", ""))
		var node: Node = null
		if node_path != "":
			node = inst.get_node_or_null(NodePath(node_path))
		if node == null and find_name != "":
			node = inst.find_child(find_name, true, false)
		if node == null:
			_missing.append(key)
			_notes.append("actor '" + key + "' is not in " + path + " (looked for path '" + node_path + "' and name '" + find_name + "')")
		else:
			_actors[key] = node

	var tick = _contract.get("tick", {})
	if typeof(tick) == TYPE_DICTIONARY and str(tick.get("mode", "frames")) == "method":
		var want := str(tick.get("method", ""))
		var owner_path := str(tick.get("node", ""))
		var owner: Node = inst if owner_path == "" else inst.get_node_or_null(NodePath(owner_path))
		if owner != null and want != "" and owner.has_method(want):
			_tick_node = owner
			_tick_method = want
			_tick_mode = "method"
			# We are the tick authority now — stop the engine from ALSO
			# advancing it each frame.
			owner.set_process(false)
		else:
			_notes.append("the contract's tick method '" + want + "' is not on that node, so the probe let the engine run its own frames instead")

func _initialize() -> void:
	var parsed_contract = JSON.parse_string(CONTRACT_JSON)
	if parsed_contract is Dictionary:
		_contract = parsed_contract
	else:
		_notes.append("the probe contract did not parse; nothing was driven")
	var parsed = JSON.parse_string(SCHEDULE_JSON)
	if parsed is Array:
		_schedule = parsed
	else:
		_notes.append("schedule did not parse to an array; running an idle match")
	# Wrap scene loading so a broken game can't crash the probe with no output.
	_load_scene()

func _sample() -> void:
	if _actors.is_empty():
		return
	var row := {"tick": _tick}
	for entry in _contract.get("samples", []):
		if typeof(entry) != TYPE_DICTIONARY:
			continue
		var node = _actors.get(str(entry.get("actor", "")), null)
		var v = _read(node, str(entry.get("property", "")))
		var step := float(entry.get("round", 0.0))
		if v != null and step > 0.0:
			v = snappedf(float(v), step)
		row[str(entry.get("key", ""))] = v
	for entry in _contract.get("derived", []):
		if typeof(entry) != TYPE_DICTIONARY:
			continue
		if str(entry.get("kind", "")) != "abs_diff":
			continue
		var of = entry.get("of", [])
		if typeof(of) != TYPE_ARRAY or of.size() != 2:
			continue
		var a = row.get(str(of[0]), null)
		var b = row.get(str(of[1]), null)
		var key := str(entry.get("key", ""))
		if a == null or b == null:
			row[key] = null
			continue
		var step2 := float(entry.get("round", 0.01))
		var diff := absf(float(a) - float(b))
		row[key] = snappedf(diff, step2) if step2 > 0.0 else diff
	_samples.append(row)

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
		"scene_loaded": _scene_root != null,
		"actors_found": _actors.keys(),
		"actors_missing": _missing,
		"tick_mode": _tick_mode,
		# Kept under the old name because saved expectations address it: it is
		# true when the probe actually got hold of everything it was told to
		# watch, which for the fighting shape is exactly what it used to mean.
		"has_fight": _scene_root != null and _actors.size() > 0 and _missing.is_empty(),
	}
	print("PROBE_JSON:" + JSON.stringify(summary))

func _process(_delta: float) -> bool:
	# Nothing to watch: report once, with the notes saying why, and quit rather
	# than spin out a green-looking match that sampled nothing.
	if _scene_root == null or _actors.is_empty():
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

	# Advance exactly one deterministic step, when the contract names one.
	if _tick_node != null and _tick_method != "":
		_tick_node.call(_tick_method)

	if _tick % SAMPLE_EVERY == 0:
		_sample()

	_tick += 1
	if _tick > MAX_TICKS:
		_sample()
		_emit_result()
		return true
	return false
"""


def _gd_string(payload) -> str:
    """A JSON blob as a GDScript double-quoted string literal.

    Embedding it as a *string* the GDScript parses at runtime avoids
    hand-assembling GDScript array and dictionary literals, which is where a
    quote in a node name would otherwise become a syntax error in the probe.
    """
    return '"' + json.dumps(payload).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_probe(contract: dict, actions: list, ticks: int) -> str:
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
    body = {k: contract.get(k) for k in ("scene", "actors", "samples",
                                         "derived", "tick")}
    return (
        _PROBE_TEMPLATE
        .replace("%SCHEDULE_JSON%", _gd_string(schedule))
        .replace("%CONTRACT_JSON%", _gd_string(body))
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


# --- expectations ----------------------------------------------------------
# An expectation is {property, comparator, value, at_tick?, label?}. It is
# evaluated here, on the server, against the samples the probe printed — not in
# the browser, because a gate a client can skip is not a gate.

def _numeric(*values) -> Optional[tuple]:
    """Both sides as floats, or None if either is not a number."""
    out = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            return None
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            return None
    return tuple(out)


def _cmp_eq(actual, value) -> bool:
    pair = _numeric(actual, value)
    if pair is not None:
        return abs(pair[0] - pair[1]) <= 1e-9
    return actual == value


def _cmp_order(actual, value, op: str) -> bool:
    pair = _numeric(actual, value)
    if pair is None:
        # Strings still order sensibly; anything else simply cannot be compared.
        if not isinstance(actual, str) or not isinstance(value, str):
            raise ValueError(f"{actual!r} and {value!r} are not comparable")
        pair = (actual, value)
    a, b = pair
    return {"lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[op]


def _cmp_between(actual, value) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("between wants a two-element [low, high]")
    pair = _numeric(actual, value[0], value[1])
    if pair is None:
        raise ValueError(f"{actual!r} is not a number")
    a, lo, hi = pair
    if lo > hi:
        lo, hi = hi, lo
    return lo <= a <= hi


def _cmp_contains(actual, value) -> bool:
    if isinstance(actual, (list, tuple)):
        return any(_cmp_eq(item, value) for item in actual)
    if actual is None:
        return False
    return str(value) in str(actual)


COMPARATORS = {
    "eq": _cmp_eq,
    "ne": lambda a, v: not _cmp_eq(a, v),
    "lt": lambda a, v: _cmp_order(a, v, "lt"),
    "lte": lambda a, v: _cmp_order(a, v, "lte"),
    "gt": lambda a, v: _cmp_order(a, v, "gt"),
    "gte": lambda a, v: _cmp_order(a, v, "gte"),
    "between": _cmp_between,
    "contains": _cmp_contains,
}

# Fields of the summary (not of a sample) an expectation may address.
_SUMMARY_FIELDS = {"ticks", "requested_ticks", "sample_count", "has_fight",
                   "scene_loaded", "tick_mode"}


def sample_at(summary: dict, at_tick: Optional[int]) -> dict:
    """The sample an expectation is about.

    No at_tick means "how did the fight end" — the final sample. Otherwise the
    nearest sample, because the probe only samples every _SAMPLE_EVERY ticks and
    an author who writes at_tick: 55 means "around there", not "nowhere".
    """
    samples = [s for s in (summary.get("samples") or []) if isinstance(s, dict)]
    if at_tick is None:
        final = summary.get("final")
        return final if isinstance(final, dict) and final else (
            samples[-1] if samples else {})
    if not samples:
        return {}
    return min(samples, key=lambda s: abs(int(s.get("tick", 0)) - int(at_tick)))


def _resolve(summary: dict, prop: str, at_tick: Optional[int]):
    sample = sample_at(summary, at_tick)
    if prop in sample:
        return sample[prop], sample, ""
    if prop in _SUMMARY_FIELDS:
        return summary.get(prop), sample, ""
    keys = sorted(k for k in sample if k != "tick")
    return None, sample, (
        f"the probe never sampled '{prop}' — "
        + (f"this run sampled {', '.join(keys)}. Add it to the probe contract, "
           f"or point the expectation at one of those"
           if keys else
           "this run sampled nothing at all, so no expectation can be checked"))


def normalise_expectations(raw) -> list[dict]:
    """Validate the ``expect`` list. A malformed expectation is a 400, not a
    silently-skipped check that would make the run look clean."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise api.bad_request("expect must be a list of expectations")
    out: list[dict] = []
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            raise api.bad_request(f"expect[{i}] must be an object", index=i)
        prop = str(e.get("property", "")).strip()
        comparator = str(e.get("comparator", "")).strip().lower()
        if not prop:
            raise api.bad_request(f"expect[{i}] needs a property", index=i)
        if comparator not in COMPARATORS:
            raise api.bad_request(
                f"expect[{i}] comparator '{comparator}' is not one of "
                + ", ".join(sorted(COMPARATORS)),
                index=i, comparators=sorted(COMPARATORS))
        at_tick = e.get("at_tick")
        if at_tick is not None:
            try:
                at_tick = max(0, int(at_tick))
            except (TypeError, ValueError):
                raise api.bad_request(f"expect[{i}] at_tick must be a number", index=i)
        out.append({
            "property": prop,
            "comparator": comparator,
            "value": e.get("value"),
            "at_tick": at_tick,
            "label": str(e.get("label", "")).strip()
                     or f"{prop} {comparator} {e.get('value')}",
        })
    return out


def evaluate(summary: Optional[dict], expectations: list[dict]) -> list[dict]:
    """Check every expectation and, when one fails, attach the sample that
    disproves it — a red verdict with no evidence just moves the argument."""
    results: list[dict] = []
    summary = summary or {}
    for i, e in enumerate(expectations):
        actual, sample, missing = _resolve(summary, e["property"], e["at_tick"])
        if missing:
            ok, reason = False, missing
        else:
            try:
                ok = bool(COMPARATORS[e["comparator"]](actual, e["value"]))
                reason = "" if ok else (
                    f"{e['property']} was {actual!r}, expected "
                    f"{e['comparator']} {e['value']!r}")
            except (ValueError, KeyError, TypeError) as exc:
                ok, reason = False, str(exc)
        row = {"index": i, **e, "actual": actual, "ok": ok, "reason": reason}
        if not ok:
            # The offending sample, so a human can see WHY without re-running.
            row["sample"] = sample or None
        results.append(row)
    return results


def verdict_of(expectations: list[dict], results: list[dict], ran_ok: bool,
               summary: Optional[dict] = None) -> str:
    """pass / fail / error / unknown.

    ``unknown`` is the load-bearing one: a bot with no expectations asserts
    nothing, and reporting that as a pass is exactly the green-for-free the audit
    called out.

    A run that produced NO SAMPLES is an ``error``, not a pass, even when the
    engine exited cleanly and the expectations happen to address summary fields.
    Once the probe stopped being hardcoded to one game, "the scene loaded and
    every declared actor was missing" became a shape a real project can hit, and
    a probe that watched nothing has not verified anything.
    """
    if not ran_ok:
        return "error"
    if summary is not None and not (summary.get("samples") or []):
        return "error"
    if not expectations:
        return "unknown"
    return "fail" if any(not r.get("ok") for r in results) else "pass"


# --- persistence and baselines ---------------------------------------------

def _build_ref(root_dir) -> str:
    """Which build this verdict is about. Without it a baseline diff cannot tell
    a regression from a different game."""
    try:
        from bgate_core.playtest import _build_identity
        return _build_identity(root_dir)[:120]
    except Exception:
        return ""


def baseline_for(root_dir, bot: str) -> Optional[dict]:
    row = db.connect(root_dir).execute(
        "SELECT * FROM qa_bot_run WHERE bot = ? AND is_baseline = 1 "
        "ORDER BY id DESC LIMIT 1", (bot,)).fetchone()
    return _row_to_run(row) if row else None


def _row_to_run(row) -> dict:
    out = dict(row)
    for field, empty in (("expectations_json", []), ("results_json", []),
                         ("samples_json", {})):
        try:
            out[field[:-5]] = json.loads(out.pop(field) or "null") or empty
        except Exception:
            out[field[:-5]] = empty
    out["is_baseline"] = bool(out.get("is_baseline"))
    return out


def diff_baseline(baseline: Optional[dict], summary: Optional[dict],
                  verdict: str, results: list[dict]) -> Optional[dict]:
    """What moved since the last run of this bot.

    Compares the two final samples numerically and pairs expectations by label,
    so "the fight is the same but expectation X flipped" reads differently from
    "everything drifted".

    IT ONLY DIFFS KEYS BOTH RUNS PRODUCED. The probe contract is editable, so a
    baseline can predate a change to what is sampled at all, and diffing a key
    the old run never had would report a "change" that is really a schema edit.
    Keys that appeared or vanished are reported as such, and a baseline with no
    key in common with this run is declared incomparable rather than diffed.
    """
    if baseline is None:
        return None
    base_summary = baseline.get("samples") or {}
    was_final = sample_at(base_summary, None)
    now_final = sample_at(summary or {}, None)
    was_keys = {k for k in was_final if k != "tick"}
    now_keys = {k for k in now_final if k != "tick"}
    shared = was_keys & now_keys
    was_contract = base_summary.get("contract") if isinstance(base_summary, dict) else None
    now_contract = (summary or {}).get("contract")
    contract_changed = bool(
        was_contract and now_contract
        and qaprobe.fingerprint(was_contract) != qaprobe.fingerprint(now_contract))

    note = ""
    if not shared and (was_keys or now_keys):
        note = ("this run and the baseline have no sample key in common, so "
                "they cannot be compared. The probe contract changed under "
                "this bot; re-run it once to lay down a baseline that means "
                "something.")
    elif was_keys - now_keys or now_keys - was_keys:
        note = ("the probe contract changed since the baseline, so only the "
                "keys both runs produced are compared.")
    elif contract_changed:
        note = ("the probe contract changed since the baseline (scene or tick "
                "authority), even though the sample keys did not.")

    changed = []
    for key in sorted(shared):
        was, now = was_final.get(key), now_final.get(key)
        if _cmp_eq(was, now):
            continue
        pair = _numeric(was, now) if was is not None and now is not None else None
        changed.append({"property": key, "was": was, "now": now,
                        "delta": round(pair[1] - pair[0], 4) if pair else None})

    was_by_label = {r.get("label"): r for r in (baseline.get("results") or [])
                    if isinstance(r, dict)}
    flipped = []
    for r in results:
        prev = was_by_label.get(r.get("label"))
        if prev is not None and bool(prev.get("ok")) != bool(r.get("ok")):
            flipped.append({"label": r.get("label"), "was_ok": bool(prev.get("ok")),
                            "now_ok": bool(r.get("ok")), "reason": r.get("reason", "")})

    return {
        "baseline_id": baseline.get("id"),
        "baseline_at": baseline.get("created_at"),
        "baseline_build": baseline.get("build_ref", ""),
        "verdict_was": baseline.get("verdict", "unknown"),
        "verdict_now": verdict,
        "regressed": baseline.get("verdict") == "pass" and verdict in {"fail", "error"},
        "changed": changed,
        "flipped": flipped,
        "comparable": bool(shared) or not (was_keys or now_keys),
        "contract_changed": contract_changed or bool(was_keys ^ now_keys),
        "keys_added": sorted(now_keys - was_keys),
        "keys_removed": sorted(was_keys - now_keys),
        "note": note,
    }


def record_run(root_dir, bot: str, verdict: str, expectations: list[dict],
               results: list[dict], summary: Optional[dict], ran_ok: bool) -> int:
    """Persist the run; a run that actually drove the game becomes the baseline.

    A run that errored is never promoted — otherwise a missing Godot would erase
    the last real result to diff against.
    """
    with db.tx(root_dir) as conn:
        cur = conn.execute(
            "INSERT INTO qa_bot_run (bot, verdict, expectations_json, results_json, "
            "samples_json, build_ref, is_baseline) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (bot, verdict, json.dumps(expectations), json.dumps(results),
             json.dumps(summary or {}), _build_ref(root_dir)))
        run_id = int(cur.lastrowid)
        if ran_ok:
            conn.execute("UPDATE qa_bot_run SET is_baseline = 0 WHERE bot = ?", (bot,))
            conn.execute("UPDATE qa_bot_run SET is_baseline = 1 WHERE id = ?", (run_id,))
    return run_id


# --- running ---------------------------------------------------------------

def _probe(project, contract: dict, actions: list, ticks: int,
           timeout: int) -> dict:
    """Drive the game once. Returns the flat run payload the seat JS reads."""
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no Godot project at {project}",
                "summary": None, "stdout": "", "stderr": "", "errors": []}
    script = _build_probe(contract, actions, ticks)
    try:
        result = _godot.run_script(script, str(project), timeout=timeout)
    except _godot.GodotNotFound as exc:
        return {"ok": False, "error": str(exc), "summary": None,
                "stdout": "", "stderr": "", "errors": []}
    except Exception as exc:  # never 500 on a probe run
        return {"ok": False, "error": f"probe run failed: {exc}",
                "summary": None, "stdout": "", "stderr": "", "errors": []}

    stdout = result.get("stdout", "")
    summary = _parse_probe_json(stdout)
    if summary is not None:
        # The contract rides with the samples into qa_bot_run.samples_json, so a
        # baseline recorded a month ago can still say what it was measuring.
        # There is no column for it and there does not need to be one.
        summary["contract"] = {k: contract.get(k) for k in
                               ("scene", "actors", "samples", "derived", "tick")}
        issues = [i for i in (contract.get("issues") or []) if i]
        if issues:
            summary["notes"] = list(summary.get("notes") or []) + issues
    ran_ok = bool(result.get("ok")) and summary is not None and bool(summary.get("has_fight"))
    return {
        "ok": ran_ok,
        "summary": summary,
        "contract": contract,
        "stdout": stdout,
        "stderr": result.get("stderr", ""),
        "errors": result.get("errors", []),
        "seconds": result.get("seconds"),
        "exit_code": result.get("exit_code"),
        "error": None if summary is not None else (
            result.get("error") or "probe produced no PROBE_JSON line"),
    }


def _bot_spec(payload: dict) -> dict:
    """Normalise one bot out of a request body (same shape in run and run-all)."""
    actions = payload.get("actions") or []
    if not isinstance(actions, list):
        raise HTTPException(400, "actions must be a list")
    try:
        ticks = int(payload.get("ticks", _DEFAULT_TICKS))
    except (TypeError, ValueError):
        ticks = _DEFAULT_TICKS
    return {
        "bot": str(payload.get("bot") or payload.get("name") or "unnamed bot")[:120],
        "actions": actions,
        "ticks": max(1, min(ticks, _MAX_TICKS)),
        "expect": normalise_expectations(payload.get("expect")),
        # The probe's own ceiling; clamped for the same reason every other engine
        # timeout is (see routes/godot_ws.clamp_timeout).
        "timeout": _clamp_probe_timeout(payload.get("timeout")),
    }


def _clamp_probe_timeout(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_PROBE_TIMEOUT
    return max(5, min(value, 600))


def contract_for(root_dir) -> dict:
    """The probe contract this project runs under, derived if never declared."""
    return qaprobe.load(root_dir, _game_dir())


def run_bot(root_dir, spec: dict, contract: Optional[dict] = None) -> dict:
    """One bot, end to end: drive, judge, persist, diff. The single place a
    verdict is produced, so /run and /run-all cannot disagree."""
    if contract is None:
        contract = contract_for(root_dir)
    run = _probe(_game_dir(), contract, spec["actions"], spec["ticks"],
                 spec["timeout"])
    results = evaluate(run.get("summary"), spec["expect"])
    verdict = verdict_of(spec["expect"], results, bool(run.get("ok")),
                         run.get("summary"))
    baseline = baseline_for(root_dir, spec["bot"])
    diff = diff_baseline(baseline, run.get("summary"), verdict, results)
    run_id = record_run(root_dir, spec["bot"], verdict, spec["expect"], results,
                        run.get("summary"), bool(run.get("ok")))
    run.update({
        "bot": spec["bot"],
        "run_id": run_id,
        "verdict": verdict,
        "expectations": spec["expect"],
        "results": results,
        "failures": [r for r in results if not r.get("ok")],
        "baseline_diff": diff,
        # What the probe was told to drive, and what it could not work out. A
        # run that watched nothing has to say what was missing rather than
        # leaving the seat to infer it from an empty table.
        "contract": contract,
        "contract_issues": list(contract.get("issues") or []),
    })
    return run


@router.post("/api/qa-bots/run")
def qa_bots_run(request: Request, payload: Optional[dict] = None,
                async_: int = Query(0, alias="async")):
    """Run one bot match: assemble the probe from the project's contract and the
    bot's action schedule, drive the real game headless, judge it against the
    bot's expectations.

    Keeps its flat (non-enveloped) shape — the QA seat reads ``ok``/``summary``/
    ``stdout`` off the top level — and adds ``verdict``/``results``/
    ``baseline_diff`` beside them. ``?async=1`` starts a job instead.
    """
    spec = _bot_spec(payload or {})
    root_dir = root()
    if jobs_api.wants_async(payload, async_):
        def work(job_id: int) -> dict:
            if jobs_api.is_cancelled(job_id):
                return jobs_api.cancelled_result("startup")
            jobs.progress(root_dir, job_id, fraction=0.1,
                          stage=f"driving {spec['bot']}")
            out = run_bot(root_dir, spec)
            jobs.progress(root_dir, job_id, fraction=1.0, stage=out["verdict"])
            return out
        body = jobs_api.start("qa_bots.run", work,
                              request_body={"bot": spec["bot"], "ticks": spec["ticks"],
                                            "expectations": len(spec["expect"])},
                              request=request)
        return JSONResponse(status_code=202, content=body)
    return run_bot(root_dir, spec)


def aggregate_verdict(verdicts: list[str]) -> str:
    """The gate's answer for a whole roster.

    Deliberately pessimistic: one failure fails the set, and a set containing
    anything unproven is unknown rather than green.
    """
    if not verdicts:
        return "unknown"
    if "fail" in verdicts:
        return "fail"
    if "error" in verdicts:
        return "error"
    if "unknown" in verdicts:
        return "unknown"
    return "pass"


@router.post("/api/qa-bots/run-all")
def qa_bots_run_all(request: Request, payload: Optional[dict] = None,
                    async_: int = Query(0, alias="async")):
    """Run a roster and return one verdict the QA gate can consume.

    ``{"bots": [{name, actions, ticks, expect}, ...]}``. Bots run in sequence —
    they each spawn a headless engine, and racing them would make the samples
    meaningless.
    """
    payload = payload or {}
    raw = payload.get("bots")
    if not isinstance(raw, list) or not raw:
        raise api.bad_request("bots must be a non-empty list")
    specs = [_bot_spec(b if isinstance(b, dict) else {}) for b in raw]
    root_dir = root()

    def execute(job_id: Optional[int] = None) -> dict:
        # One contract for the whole roster. Deriving it per bot would let a
        # concurrent edit land mid-roster and produce two verdicts about two
        # different games under one aggregate.
        contract = contract_for(root_dir)
        runs = []
        for i, spec in enumerate(specs):
            if job_id is not None:
                if jobs_api.is_cancelled(job_id):
                    return jobs_api.cancelled_result(f"after {i} of {len(specs)} bots")
                jobs.progress(root_dir, job_id, fraction=i / len(specs),
                              stage=f"{spec['bot']} ({i + 1}/{len(specs)})")
            runs.append(run_bot(root_dir, spec, contract))
        verdicts = [r["verdict"] for r in runs]
        return {
            "ok": True,
            "verdict": aggregate_verdict(verdicts),
            "counts": {v: verdicts.count(v)
                       for v in ("pass", "fail", "error", "unknown")},
            "regressions": [r["bot"] for r in runs
                            if (r.get("baseline_diff") or {}).get("regressed")],
            "contract": contract,
            "contract_issues": list(contract.get("issues") or []),
            "runs": [{k: v for k, v in r.items()
                      if k not in ("stdout", "contract")} for r in runs],
        }

    if jobs_api.wants_async(payload, async_):
        body = jobs_api.start("qa_bots.run_all", execute,
                              request_body={"bots": [s["bot"] for s in specs]},
                              request=request)
        return JSONResponse(status_code=202, content=body)
    return api.ok(execute())


@router.get("/api/qa-bots/runs")
def qa_bots_runs(page: api.Page = Depends(), bot: str = "",
                 verdict: str = "") -> dict:
    """Run history — 'when did this start failing'."""
    root_dir = root()
    where, params = "", []
    if bot:
        where += " AND bot = ?"
        params.append(bot)
    if verdict:
        where += " AND verdict = ?"
        params.append(verdict)
    conn = db.connect(root_dir)
    total = conn.execute(
        f"SELECT COUNT(*) FROM qa_bot_run WHERE 1=1{where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM qa_bot_run WHERE 1=1{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, page.limit, page.offset]).fetchall()
    return page.envelope([_run_summary(_row_to_run(r)) for r in rows], total)


@router.get("/api/qa-bots/baseline")
def qa_bots_baseline(bot: str) -> dict:
    """The run the next one will be diffed against."""
    base = baseline_for(root(), bot)
    if base is None:
        raise api.not_found(f"no baseline for '{bot}' yet — run it once", bot=bot)
    return api.ok(base)


def _run_summary(run: dict) -> dict:
    """History rows without the full sample table — the list view never shows it,
    and a hundred runs of samples is megabytes down the wire."""
    samples = run.get("samples") or {}
    return {
        "id": run.get("id"),
        "bot": run.get("bot"),
        "verdict": run.get("verdict"),
        "is_baseline": run.get("is_baseline"),
        "build_ref": run.get("build_ref", ""),
        "created_at": run.get("created_at"),
        "expectations": len(run.get("expectations") or []),
        "failures": [{"label": r.get("label"), "reason": r.get("reason")}
                     for r in (run.get("results") or []) if not r.get("ok")],
        "final": samples.get("final") if isinstance(samples, dict) else None,
    }


@router.get("/api/qa-bots/actions")
def qa_bots_actions() -> dict:
    """The game's own input action names (project.godot [input] section), so the
    UI can offer a dropdown of real actions.

    It used to fall back to a hardcoded boxing moveset — jab, hook, duck,
    kick_heavy — for any project it could not read, which offered every other
    game a dropdown of actions its InputMap has never heard of and a bot whose
    every press was silently discarded. An empty list is the honest answer.
    """
    default: list[str] = []
    try:
        pg = _game_dir() / "project.godot"
        text = pg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"actions": default, "source": "none"}

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
        return {"actions": default, "source": "none"}
    return {"actions": actions, "source": "project.godot"}


# --- the probe contract -----------------------------------------------------

@router.get("/api/qa-bots/contract")
def qa_bots_contract() -> dict:
    """What the probe drives on this project, derived if nobody declared it.

    ``source`` says which: ``declared`` (a human or an agent wrote it),
    ``derived`` (read off the real scene and persisted so it can be edited), or
    ``none`` (nothing could be worked out, and ``issues`` says what is missing).
    A derived contract that cannot really watch anything — actors spawned at
    runtime, or the pinned fighter shape on a game that is not the fighter —
    carries ``derived_thin: true`` with the reason in ``issues``, so a thin
    guess is never mistaken for a working probe.
    """
    contract = contract_for(root())
    contract["sample_keys"] = qaprobe.sample_keys(contract)
    return api.ok(contract)


@router.post("/api/qa-bots/contract")
def qa_bots_contract_set(payload: Optional[dict] = None) -> dict:
    """Store a hand-edited contract.

    A contract with complaints is still stored: the seat shows the complaints
    beside it, and refusing the write would leave the human editing a document
    they cannot save halfway through.
    """
    body = (payload or {}).get("data", payload) or {}
    if not isinstance(body, dict):
        raise api.bad_request("the contract must be an object")
    contract, issues = qaprobe.normalise(body)
    # Carry the version the editor loaded, so a second tab's save is a 409
    # rather than a silent overwrite (same precondition the bot roster uses).
    contract[_ws.VERSION_KEY] = str(body.get(_ws.VERSION_KEY, "") or "")
    try:
        saved = qaprobe.save(root(), contract)
    except _ws.StaleWrite as exc:
        raise api.conflict(str(exc), expected=exc.expected, actual=exc.actual)
    contract[_ws.VERSION_KEY] = saved.get(_ws.VERSION_KEY, "")
    contract["source"] = "declared"
    contract["issues"] = issues
    contract["sample_keys"] = qaprobe.sample_keys(contract)
    return api.ok(contract)


@router.post("/api/qa-bots/contract/derive")
def qa_bots_contract_derive() -> dict:
    """Re-read the project and replace the stored contract with what it says.

    The explicit version of what a fresh project gets for free, for when the
    game has moved on: a scene renamed, actors added, a sim_tick() introduced.
    """
    contract = qaprobe.derive(_game_dir())
    if contract.get("actors"):
        saved = qaprobe.save(root(), contract)
        contract[_ws.VERSION_KEY] = saved.get(_ws.VERSION_KEY, "")
    contract["sample_keys"] = qaprobe.sample_keys(contract)
    return api.ok(contract)
