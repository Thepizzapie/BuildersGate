"""The hook table: which sounds the game asks for, and which file answers.

The panel these back exists to show ONE failure — an event the game plays and
no file answers — so what is pinned here is that the scan can tell that apart
from its three neighbours: a file nobody asks for (orphan, wasted work, not a
bug), a call whose name is a variable (real call, unknowable name, must not be
guessed at), and a loudness that was not measured (must not be defaulted).
"""
from __future__ import annotations

from pathlib import Path

from bgate_core.audio import audiohooks, loudness


def _project(tmp_path: Path) -> Path:
    (tmp_path / "game" / "assets" / "audio").mkdir(parents=True)
    (tmp_path / "game" / "scripts").mkdir(parents=True)
    return tmp_path


def _sound(root: Path, name: str) -> None:
    (root / "game" / "assets" / "audio" / name).write_bytes(b"RIFF0000WAVE")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def test_a_named_call_binds_to_the_prefixed_file(tmp_path):
    """`Audio.sfx("melee_hit")` is answered by sfx_melee_hit.wav — the front
    door's prefix convention is the binding, and it is mechanical."""
    root = _project(tmp_path)
    _sound(root, "sfx_melee_hit.wav")
    (root / "game" / "scripts" / "combat.gd").write_text(
        'func hit():\n\tAudio.sfx("melee_hit")\n', encoding="utf-8")

    out = audiohooks.scan(root)
    row = {e["event"]: e for e in out["events"]}["sfx.melee_hit"]
    assert row["state"] == "wired"
    assert row["file"] == "game/assets/audio/sfx_melee_hit.wav"
    assert row["sites"][0] == {"file": "game/scripts/combat.gd", "line": 2}


def test_an_event_no_file_answers_is_unbound(tmp_path):
    """THE ROW THE PANEL EXISTS FOR. The game plays a sound at this line and
    the build makes no noise there."""
    root = _project(tmp_path)
    (root / "game" / "scripts" / "combat.gd").write_text(
        'Audio.sfx("ko_thud")\n', encoding="utf-8")

    out = audiohooks.scan(root)
    row = {e["event"]: e for e in out["events"]}["sfx.ko_thud"]
    assert row["state"] == "unbound" and row["file"] is None


def test_unbound_events_sort_first(tmp_path):
    """A table whose failure is on row nine is a table nobody reads."""
    root = _project(tmp_path)
    _sound(root, "sfx_click.wav")
    (root / "game" / "scripts" / "ui.gd").write_text(
        'Audio.sfx("click")\nAudio.sfx("click")\nAudio.sfx("missing")\n',
        encoding="utf-8")

    assert audiohooks.scan(root)["events"][0]["event"] == "sfx.missing"


def test_a_file_nobody_asks_for_is_an_orphan_not_an_unbound_event(tmp_path):
    """Different failure, different fix: wasted work, not silence in a build."""
    root = _project(tmp_path)
    _sound(root, "sfx_click.wav")
    _sound(root, "sfx_never_used.wav")
    (root / "game" / "scripts" / "ui.gd").write_text(
        'Audio.sfx("click")\n', encoding="utf-8")

    out = audiohooks.scan(root)
    assert [e["state"] for e in out["events"]] == ["wired"]
    assert audiohooks.orphans(root, out["events"]) == [
        "game/assets/audio/sfx_never_used.wav"]


def test_the_music_family_gets_its_own_prefix(tmp_path):
    root = _project(tmp_path)
    _sound(root, "music_title.ogg")
    (root / "game" / "scripts" / "menu.gd").write_text(
        'Audio.music("title")\n', encoding="utf-8")

    row = audiohooks.scan(root)["events"][0]
    assert row["event"] == "music.title"
    assert row["file"] == "game/assets/audio/music_title.ogg"


def test_an_unprefixed_file_still_answers(tmp_path):
    """Not every project's front door prefixes. `<name>` is the fallback."""
    root = _project(tmp_path)
    _sound(root, "click.wav")
    (root / "game" / "scripts" / "ui.gd").write_text(
        'Audio.play_sound("click")\n', encoding="utf-8")

    assert audiohooks.scan(root)["events"][0]["state"] == "wired"


# ---------------------------------------------------------------------------
# What must NOT be inferred
# ---------------------------------------------------------------------------
def test_a_variable_argument_is_reported_never_expanded(tmp_path):
    """`Audio.sfx(ACTION_SFX[action])` is a real call site with an unknowable
    name. Guessing what it resolves to would put invented rows in the table."""
    root = _project(tmp_path)
    (root / "game" / "scripts" / "combat.gd").write_text(
        "Audio.sfx(ACTION_SFX[action])\n", encoding="utf-8")

    out = audiohooks.scan(root)
    assert out["events"] == []
    assert out["dynamic"][0]["expr"] == "Audio.sfx(ACTION_SFX[action])"
    assert out["dynamic"][0]["line"] == 1


def test_a_bare_play_is_not_a_sound(tmp_path):
    """AnimationPlayer.play("walk") and $Overlay.play("resolved") are the same
    three letters. A table full of animation names is worse than an empty one."""
    root = _project(tmp_path)
    (root / "game" / "scripts" / "anim.gd").write_text(
        '$Anim.play("walk")\nsprite.play("idle")\n', encoding="utf-8")

    assert audiohooks.scan(root)["events"] == []


def test_the_import_cache_is_not_the_game(tmp_path):
    """.godot holds a copy of every res:// path; scanning it doubles the table
    with rows no line of gameplay asks for."""
    root = _project(tmp_path)
    (root / ".godot").mkdir()
    (root / ".godot" / "cache.gd").write_text(
        'Audio.sfx("ghost")\n', encoding="utf-8")

    assert audiohooks.scan(root)["events"] == []


def test_stopping_the_music_is_not_an_event(tmp_path):
    """`music("")` is how a front door spells "stop", not a hook named ""."""
    root = _project(tmp_path)
    (root / "game" / "scripts" / "menu.gd").write_text(
        'Audio.music("")\n', encoding="utf-8")

    assert audiohooks.scan(root)["events"] == []


def test_a_res_path_binds_by_construction_and_can_still_be_missing(tmp_path):
    root = _project(tmp_path)
    (root / "game" / "scenes").mkdir()
    (root / "game" / "scenes" / "hall.tscn").write_text(
        '[ext_resource path="res://assets/audio/gone.wav"]\n', encoding="utf-8")

    out = audiohooks.scan(root)
    row = out["events"][0]
    assert row["state"] == "unbound"
    assert out["unresolved_paths"][0]["path"] == "assets/audio/gone.wav"


def test_a_project_with_no_audio_calls_says_nothing_rather_than_guessing(tmp_path):
    root = _project(tmp_path)
    _sound(root, "sfx_click.wav")
    out = audiohooks.scan(root)
    assert out["events"] == [] and out["sound_count"] == 1


# ---------------------------------------------------------------------------
# Loudness — measured or absent, never defaulted
# ---------------------------------------------------------------------------
def test_a_file_that_cannot_be_measured_carries_no_number(tmp_path):
    """The whole reason this column was blank for a year. An unmeasurable file
    reports None and a reason; it never falls back to the target."""
    bogus = tmp_path / "not_audio.wav"
    bogus.write_bytes(b"nonsense")
    out = loudness.measure(bogus)
    assert out["lufs"] is None and out["measured"] is False and out["reason"]


def test_a_missing_file_is_a_reason_not_a_traceback(tmp_path):
    out = loudness.measure(tmp_path / "nope.wav")
    assert out["measured"] is False and out["reason"] == "no such file"


def test_the_verdict_is_a_word_and_an_unmeasured_row_gets_none():
    assert loudness.verdict(None) == ""
    assert loudness.verdict(-14.0) == "on target"
    assert loudness.verdict(-9.4) == "too loud"
    assert loudness.verdict(-21.1) == "too quiet"
    # Inside the tolerance band is on target — a decibel is inaudible on a
    # one-shot and a table that flagged it would flag everything.
    assert loudness.verdict(-16.0) == "on target"
