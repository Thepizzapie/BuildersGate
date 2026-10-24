"""The phone's touch pad comes from the project's input map, not a per-game
mapping: roles from action names, DOM key events from Godot keycodes."""
from __future__ import annotations

from bgate_core.level import controls

_KEY = ('Object(InputEventKey,"keycode":0,"physical_keycode":{code},"pressed":false)')


def _map(*pairs):
    lines = ["[input]", ""]
    for action, codes in pairs:
        events = ", ".join(_KEY.format(code=c) for c in codes)
        lines.append(f'{action}={{\n"events": [{events}]\n}}')
    return "\n".join(lines) + "\n"


def test_builtin_ui_actions_become_the_pad_and_ok_back_menu():
    text = _map(("ui_up", [4194320]), ("ui_down", [4194322]), ("ui_left", [4194319]),
                ("ui_right", [4194321]), ("ui_accept", [90, 4194309]),
                ("ui_cancel", [88, 4194305]), ("menu", [67, 4194306]))
    pad = controls.pad_layout(controls.parse_input_map(text, include_builtin=True))
    assert set(pad["dpad"]) == {"up", "down", "left", "right"}
    assert pad["dpad"]["left"] == {"label": "←", "code": "ArrowLeft", "key": "ArrowLeft",
                                  "keyCode": 37, "action": "ui_left"}
    assert [(b["label"], b["code"], b["role"]) for b in pad["buttons"]] == [
        ("OK", "KeyZ", "accept"), ("Back", "KeyX", "cancel"), ("Menu", "KeyC", "menu")]


def test_wasd_and_custom_actions_map_by_name_and_dedupe_by_key():
    text = _map(("move_left", [65]), ("move_right", [68]), ("move_forward", [87]),
                ("move_back", [83]), ("jump", [32]), ("interact", [69]),
                ("ui_accept", [4194309, 69]), ("sprint", [4194325]),
                ("horn", [72]), ("lights", [76]), ("radio", [82]), ("camera", [86]))
    pad = controls.pad_layout(controls.parse_input_map(text, include_builtin=True))
    assert pad["dpad"]["left"]["code"] == "KeyA" and pad["dpad"]["up"]["code"] == "KeyW"
    labels = [b["label"] for b in pad["buttons"]]
    # accept first, then declaration order; interact and ui_accept share E so
    # only one button; six is the cap and the rest are named as dropped
    assert labels[0] == "OK" and labels[1] == "Jump"
    assert "Interact" not in labels
    assert len(pad["buttons"]) == 6
    assert pad["dropped"] == ["camera"]


def test_a_map_with_no_keys_gives_an_empty_pad_not_a_guess():
    pad = controls.pad_layout(controls.parse_input_map("[input]\n", include_builtin=True))
    assert pad == {"dpad": {}, "buttons": [], "dropped": []}


def test_a_web_game_ships_controls_json_with_dom_codes(tmp_path):
    import json
    (tmp_path / "controls.json").write_text(json.dumps({"actions": [
        {"action": "move_left", "keys": ["KeyA", "ArrowLeft"]}, {"action": "move_right", "keys": ["KeyD"]},
        {"action": "move_up", "keys": ["KeyW"]}, {"action": "move_down", "keys": ["KeyS"]},
        {"action": "interact", "keys": ["Space"]}, {"action": "pause", "keys": ["Escape"]},
        {"action": "attack", "keys": ["KeyJ"]}, {"action": "throw", "keys": ["KeyK"]},
        {"action": "sneak", "keys": ["KeyC"]}, {"action": "restart", "keys": ["KeyR"]},
        {"action": "drop", "keys": ["KeyQ"]}, {"action": "bogus", "keys": ["NotAKey"]}]}), encoding="utf-8")
    pad = controls.pad_for_project(tmp_path)
    assert pad["dpad"]["left"] == {"label": "A", "code": "KeyA", "key": "a", "keyCode": 65, "action": "move_left"}
    assert set(pad["dpad"]) == {"up", "down", "left", "right"}
    assert [(b["label"], b["code"], b["role"]) for b in pad["buttons"]] == [
        ("OK", "Space", "accept"), ("Menu", "Escape", "menu"), ("Attack", "KeyJ", "action"),
        ("Throw", "KeyK", "action"), ("Sneak", "KeyC", "action"), ("Restart", "KeyR", "action")]
    assert pad["dropped"] == ["drop"]
    hint = controls.for_project(tmp_path)
    assert hint[0] == {"action": "move_left", "keys": ["A", "\u2190"], "buttons": [], "codes": ["KeyA", "ArrowLeft"]}
    assert controls.pad_for_project(tmp_path / "nowhere") == {"dpad": {}, "buttons": [], "dropped": []}
