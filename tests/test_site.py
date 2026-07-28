"""The arcade: what ships, what does not, and what the host will accept.

The expensive half of publishing (Godot's exporter) is stubbed here — these
tests are about the site around the build, and a suite that shells out to a
40-second engine export is a suite nobody runs. The export itself is covered by
tests/test_godot.py and, for the preset, by an actual export on a real machine.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

import bgate_site
from bgate_core import db, project
from bgate_site import builder, collect


def _game_files(root: Path, name: str = "Test Game") -> None:
    """A project.godot at the root, as `bgate init` writes one."""
    (root / "project.godot").write_text(
        f'config_version=5\n\n[application]\n\nconfig/name="{name}"\n\n'
        '[input]\n\njump={\n"deadzone": 0.5,\n"events": [Object(InputEventKey,'
        '"physical_keycode":32,"keycode":0)\n]\n}\n', encoding="utf-8")


def _fake_build(root: Path, wasm_bytes: int = 1024) -> Path:
    """A stand-in for export/web — same filenames the engine writes."""
    web = root / "export" / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "index.html").write_text("<html>godot shell</html>", encoding="utf-8")
    (web / "index.js").write_text("// loader", encoding="utf-8")
    (web / "index.pck").write_bytes(b"P" * 64)
    # Incompressible content, so a size test cannot pass by accident.
    (web / "index.wasm").write_bytes(bytes(range(256)) * (wasm_bytes // 256))
    return web


@pytest.fixture()
def game(tmp_path):
    root = tmp_path / "ember-run"
    root.mkdir()
    project.init(root, "Ember Run", pitch="A dash through a furnace.")
    _game_files(root, "Ember Run")
    _fake_build(root)
    yield root
    db.close_all()


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------
def test_describe_reads_store_game_and_overrides(game):
    card = collect.describe(game)
    assert card["title"] == "Ember Run"
    assert card["tagline"] == "A dash through a furnace."
    assert card["publishable"] is True
    assert [c["action"] for c in card["controls"]] == ["jump"]

    (game / ".bgate" / "site.json").write_text(json.dumps({
        "title": "EMBER RUN", "tagline": "over the pitch",
        "tags": ["platformer"], "order": 1}), encoding="utf-8")
    card = collect.describe(game)
    assert card["title"] == "EMBER RUN"
    assert card["tagline"] == "over the pitch"        # the override wins
    assert card["tags"] == ["platformer"]


def test_hidden_projects_are_reported_not_silently_dropped(game):
    (game / ".bgate" / "site.json").write_text('{"hidden": true}', encoding="utf-8")
    card = collect.describe(game)
    assert card["publishable"] is False
    assert "hidden" in card["skip_reason"]


def test_a_project_without_a_godot_game_is_not_publishable(tmp_path):
    root = tmp_path / "notes-only"
    project.init(root, "Notes Only")
    try:
        card = collect.describe(root)
        assert card["publishable"] is False
        assert "no Godot project" in card["skip_reason"]
    finally:
        db.close_all()


def test_a_cover_path_cannot_escape_the_project(game):
    outside = game.parent / "secret.png"
    outside.write_bytes(b"\x89PNG")
    (game / ".bgate" / "site.json").write_text(
        json.dumps({"cover": "../secret.png"}), encoding="utf-8")
    assert collect.describe(game)["cover"] == ""


def test_site_config_falls_back_to_defaults_on_broken_json(tmp_path):
    bad = tmp_path / "arcade.json"
    bad.write_text("{not json", encoding="utf-8")
    config = collect.site_config(bad)
    assert config["title"] == collect.DEFAULT_CONFIG["title"]
    assert config["source"] == str(bad)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def test_publish_writes_a_playable_tree(tmp_path, game):
    out = tmp_path / "arcade"
    report = bgate_site.build(out, roots=[game], rebuild="never", host="none")

    assert report["ok"] and not report["errors"]
    assert [g["slug"] for g in report["games"]] == ["ember-run"]
    assert (out / "index.html").is_file()
    assert (out / "arcade.css").is_file()
    assert (out / "_headers").is_file()
    # The engine's own shell keeps its filenames, one level down from the page.
    assert (out / "games" / "ember-run" / "index.html").is_file()
    assert (out / "games" / "ember-run" / "build" / "index.wasm").is_file()
    assert "Ember Run" in (out / "index.html").read_text(encoding="utf-8")
    assert json.loads((out / "games.json").read_text(encoding="utf-8"))["games"]


def test_publish_refuses_a_directory_it_did_not_create(tmp_path, game):
    out = tmp_path / "not-ours"
    out.mkdir()
    (out / "important.txt").write_text("do not delete me", encoding="utf-8")

    report = bgate_site.build(out, roots=[game], rebuild="never")
    assert report["ok"] is False
    assert "--force" in report["error"]
    assert (out / "important.txt").is_file()

    forced = bgate_site.build(out, roots=[game], rebuild="never", force=True,
                              host="none")
    assert forced["ok"] and (out / "important.txt").is_file()


def test_republishing_prunes_a_game_that_went_away(tmp_path, game):
    out = tmp_path / "arcade"
    bgate_site.build(out, roots=[game], rebuild="never", host="none")
    stale = out / "games" / "gone-game"
    stale.mkdir()
    (stale / "index.html").write_text("old", encoding="utf-8")

    report = bgate_site.build(out, roots=[game], rebuild="never", host="none")
    assert report["pruned"] == ["gone-game"]
    assert not stale.exists()
    assert (out / "games" / "ember-run").is_dir()


def test_two_projects_with_the_same_name_get_distinct_urls(tmp_path):
    roots = []
    for i in (1, 2):
        root = tmp_path / f"copy{i}" / "ember-run"
        root.mkdir(parents=True)
        project.init(root, "Ember Run")
        _game_files(root)
        _fake_build(root)
        roots.append(root)
    try:
        report = bgate_site.build(tmp_path / "arcade", roots=roots,
                                  rebuild="never", host="none")
        slugs = sorted(g["slug"] for g in report["games"])
        assert slugs == ["ember-run", "ember-run-2"]
    finally:
        db.close_all()


def test_dry_run_writes_nothing(tmp_path, game):
    out = tmp_path / "arcade"
    report = bgate_site.build(out, roots=[game], rebuild="never", dry_run=True)
    assert report["games"] and not out.exists()


def test_an_unknown_host_is_refused_before_anything_is_written(tmp_path, game):
    out = tmp_path / "arcade"
    report = bgate_site.build(out, roots=[game], rebuild="never", host="geocities")
    assert report["ok"] is False and not out.exists()


# ---------------------------------------------------------------------------
# host limits — the part that decides whether the deploy is accepted
# ---------------------------------------------------------------------------
def test_a_file_over_the_host_limit_is_gzipped_under_its_own_name(tmp_path, game):
    _fake_build(game, wasm_bytes=8 * builder.MIB)
    out = tmp_path / "arcade"
    limit = 2 * builder.MIB
    monkey = dict(builder.HOSTS["cloudflare"], limit=limit)
    builder.HOSTS["test-host"] = monkey
    try:
        report = bgate_site.build(out, roots=[game], rebuild="never",
                                  host="test-host")
    finally:
        builder.HOSTS.pop("test-host")

    wasm = out / "games" / "ember-run" / "build" / "index.wasm"
    assert wasm.stat().st_size <= limit
    # Same URL, gzip body — which is only correct if the header says so.
    assert gzip.decompress(wasm.read_bytes())[:4] == bytes(range(4))
    headers = (out / "_headers").read_text(encoding="utf-8")
    assert "/games/ember-run/build/index.wasm" in headers
    assert "Content-Encoding: gzip" in headers
    assert "Content-Type: application/wasm" in headers
    assert report["compressed"][0]["was"] > report["compressed"][0]["now"]


def test_a_file_that_stays_too_big_is_an_error_not_a_surprise(tmp_path, game):
    _fake_build(game, wasm_bytes=4 * builder.MIB)
    builder.HOSTS["tiny"] = {"limit": 1024, "precompress": True, "deploy": ""}
    try:
        report = bgate_site.build(tmp_path / "arcade", roots=[game],
                                  rebuild="never", host="tiny")
    finally:
        builder.HOSTS.pop("tiny")
    assert report["oversize"]
    assert any(r["stage"] == "size" for r in report["errors"])
    assert "will be rejected" in report["errors"][0]["error"]


def test_host_none_leaves_the_build_untouched(tmp_path, game):
    _fake_build(game, wasm_bytes=4 * builder.MIB)
    out = tmp_path / "arcade"
    bgate_site.build(out, roots=[game], rebuild="never", host="none")
    wasm = out / "games" / "ember-run" / "build" / "index.wasm"
    assert wasm.stat().st_size == 4 * builder.MIB
    assert "Content-Encoding" not in (out / "_headers").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# escaping — the games' own text ends up in HTML
# ---------------------------------------------------------------------------
def test_project_text_cannot_inject_markup(tmp_path, game):
    (game / ".bgate" / "site.json").write_text(json.dumps({
        "title": "<script>alert(1)</script>",
        "description": "closing </div> and \"quotes\""}), encoding="utf-8")
    out = tmp_path / "arcade"
    bgate_site.build(out, roots=[game], rebuild="never", host="none")
    page = (out / "games" / "ember-run" / "index.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# ---------------------------------------------------------------------------
# the preview server must not disagree with the host
# ---------------------------------------------------------------------------
def test_preview_parses_the_generated_headers(tmp_path):
    from bgate_cli.main import _parse_headers_file

    path = tmp_path / "_headers"
    path.write_text(
        "# a comment\n/*\n  Cross-Origin-Opener-Policy: same-origin\n\n"
        "/games/x/build/index.wasm\n  Content-Encoding: gzip\n"
        "  Content-Type: application/wasm\n", encoding="utf-8")
    rules = _parse_headers_file(path)
    assert rules[0][0] == "/*"
    assert rules[0][1]["Cross-Origin-Opener-Policy"] == "same-origin"
    assert rules[1][1]["Content-Encoding"] == "gzip"


def test_the_shipped_web_preset_is_a_web_preset():
    """The scaffold's export_presets.cfg is what makes publishing possible at
    all; a rename or a typo in it fails far away from here."""
    preset = Path(builder.__file__).resolve().parents[1] / "templates" / \
        "shared" / "export_presets.cfg"
    text = preset.read_text(encoding="utf-8")
    assert 'name="Web"' in text and 'platform="Web"' in text
    # Threads would need cross-origin isolation on the host; the site does not
    # promise that, so the preset must not quietly turn it on.
    assert "variant/thread_support=false" in text
