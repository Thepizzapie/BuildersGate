"""The screen audit reads a frame's manifest and names composition faults."""
from __future__ import annotations

from bgate_core.qa import screen_audit


def _sprite(path, x, y, w, h, tex=None, cls="AnimatedSprite2D"):
    e = {"class": cls, "path": path, "screen_bounds": [x, y, x + w, y + h], "visible": True}
    if tex:
        e["texture_px"] = tex
    return e


def _frame(entities, ui=None):
    return {"viewport": [640, 360], "scale": [2, 2], "entities": entities, "ui": ui or {}}


def test_party_too_small_and_enemy_looming_are_two_findings():
    m = _frame({
        "Leader_Chuco": _sprite("Battle/Party/Leader_Chuco", 40, 200, 24, 40, [64, 96]),
        "Bryan": _sprite("Battle/Party/Bryan", 90, 200, 24, 40, [64, 96]),
        "Enemy_Flood": _sprite("Battle/Enemies/Enemy_Flood", 400, 60, 200, 260, [320, 320],
                               cls="Sprite2D"),
    })
    out = screen_audit.audit(m, party_height=96)
    codes = {f["code"] for f in out["findings"]}
    assert codes == {"party_scale", "scale_clash"}
    assert out["measured"]["party_height_px"] == 40
    assert out["measured"]["enemy_to_party_ratio"] == 6.5
    assert out["ok"] is False


def test_a_frame_in_scale_is_clean():
    m = _frame({
        "Leader_Chuco": _sprite("Battle/Party/Leader_Chuco", 40, 200, 64, 96, [64, 96]),
        "Enemy_Job": _sprite("Battle/Enemies/Enemy_Job", 400, 200, 96, 96, [96, 96],
                             cls="Sprite2D"),
    })
    out = screen_audit.audit(m, party_height=96)
    assert out["ok"] and out["findings"] == []


def test_clipped_label_and_resampled_bitmap_font():
    ui = {
        "EnemyName": {"class": "Label", "path": "Battle/EnemyWindow/EnemyName",
                      "screen_bounds": [20, 20, 220, 36], "visible": True,
                      "value": {"text": "El Nino Flood (The Return of Tulare Lake)"},
                      "text_px": 330.0, "fits": False, "autowrap": False,
                      "font_size": 8, "font_fixed_size": 8},
        "Log": {"class": "Label", "path": "Battle/Log", "screen_bounds": [20, 300, 620, 340],
                "visible": True, "value": {"text": "x"}, "text_px": 12.0, "fits": True,
                "autowrap": False, "font_size": 12, "font_fixed_size": 8},
        "Wrapped": {"class": "Label", "path": "Battle/Wrapped",
                    "screen_bounds": [0, 0, 50, 50], "visible": True,
                    "value": {"text": "long text"}, "text_px": 400.0, "fits": False,
                    "autowrap": True, "font_size": 16, "font_fixed_size": 8},
    }
    out = screen_audit.audit(_frame({}, ui))
    by = {(f["code"], f["node"]) for f in out["findings"]}
    assert ("label_overflow", "EnemyName") in by
    assert ("font_scale", "Log") in by
    assert not any(n == "Wrapped" for _, n in by)   # autowrap wraps; 16 is 2x8


def test_pixel_density_mismatch_against_the_plate():
    m = _frame({
        "Plate": _sprite("Battle/Plate", 0, 0, 640, 200, [640, 200], cls="TextureRect"),
        "Leader_Chuco": _sprite("Battle/Party/Leader_Chuco", 40, 200, 128, 192, [64, 96]),
        "Enemy": _sprite("Battle/Enemies/Enemy", 400, 100, 96, 96, [96, 96], cls="Sprite2D"),
    })
    out = screen_audit.audit(m)
    dens = [f for f in out["findings"] if f["code"] == "pixel_density"]
    assert len(dens) == 1 and dens[0]["severity"] == "warn"
    assert dens[0]["measured"][0]["node"] == "Leader_Chuco"
    assert out["ok"] is True   # a warn does not fail the frame


def test_font_check_reads_fnt_size_and_flags_odd_sizes(tmp_path):
    (tmp_path / "project.godot").write_text(
        "[rendering]\ntextures/canvas_textures/default_texture_filter=0\n", encoding="utf-8")
    fonts = tmp_path / "assets" / "ui" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "px.fnt").write_text('info face="px" size=8 bold=0\ncommon lineHeight=8\n',
                                  encoding="utf-8")
    (tmp_path / "theme.tres").write_text("default_font_size = 8\n", encoding="utf-8")
    scenes = tmp_path / "scenes"
    scenes.mkdir()
    (scenes / "a.tscn").write_text("theme_override_font_sizes/font_size = 16\n"
                                   "theme_override_font_sizes/font_size = 12\n",
                                   encoding="utf-8")
    (scenes / "b.gd").write_text('add_theme_font_size_override("font_size", 11)\n',
                                 encoding="utf-8")
    out = screen_audit.font_check(str(tmp_path))
    assert out["bitmap_fonts"] == {"assets/ui/fonts/px.fnt": 8}
    sizes = sorted(f["measured"] for f in out["findings"] if f["code"] == "font_scale")
    assert sizes == [11, 12]
    assert out["ok"] is False


def test_font_check_flags_a_linear_default_filter(tmp_path):
    (tmp_path / "project.godot").write_text(
        "textures/canvas_textures/default_texture_filter=1\n", encoding="utf-8")
    (tmp_path / "f.fnt").write_text("info size=8\n", encoding="utf-8")
    out = screen_audit.font_check(str(tmp_path))
    assert [f["code"] for f in out["findings"]] == ["texture_filter"]
