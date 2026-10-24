"""The agent-script installers keep the game's own copy.

Measured on Turf War (2026-10-17): the game had extended ``agent.ts`` with a
``run`` hook and every feel check copied the template back over it, so the
bot made zero decisions and the check reported UNMEASURED. Each test here
has the control that would pass with the old byte-compare-and-copy.
"""
from __future__ import annotations

from pathlib import Path

from bgate_core.qa import agentplay


def _dest(web: Path) -> Path:
    return web / "src" / "bgate" / "agent.ts"


def _template() -> str:
    return agentplay.WEB_TEMPLATE.read_text(encoding="utf-8")


def test_a_fresh_project_gets_the_template(tmp_path):
    got = agentplay.install_web(tmp_path)
    assert got["script_updated"] is True and got["script_differs"] is False
    assert _dest(tmp_path).read_bytes() == agentplay.WEB_TEMPLATE.read_bytes()


def test_an_identical_copy_is_left_alone_even_with_other_line_endings(tmp_path):
    dest = _dest(tmp_path)
    dest.parent.mkdir(parents=True)
    crlf = _template().replace("\n", "\r\n").encode("utf-8")
    dest.write_bytes(crlf)
    got = agentplay.install_web(tmp_path)
    assert got["script_updated"] is False and got["script_differs"] is False
    assert dest.read_bytes() == crlf          # not rewritten to LF either


def test_the_games_extension_is_kept_and_named(tmp_path):
    dest = _dest(tmp_path)
    dest.parent.mkdir(parents=True)
    extended = _template().replace(
        "export const agent = {",
        "// turf war: command plans arrive through run()\nexport const agent = {")
    dest.write_text(extended, encoding="utf-8")
    got = agentplay.install_web(tmp_path)
    assert got["script_updated"] is False
    assert got["script_differs"] is True
    assert got["script_extends_template"] is True
    assert "LEFT AS IS" in got["note"] and "refresh=True" in got["note"]
    assert dest.read_text(encoding="utf-8") == extended


def test_a_copy_missing_template_lines_is_kept_but_flagged(tmp_path):
    dest = _dest(tmp_path)
    dest.parent.mkdir(parents=True)
    trimmed = _template().replace("    opt.run?.();\n", "")
    dest.write_text(trimmed, encoding="utf-8")
    got = agentplay.install_web(tmp_path)
    assert got["script_updated"] is False and got["script_differs"] is True
    assert got["script_extends_template"] is False
    assert "lacks lines" in got["note"]
    assert dest.read_text(encoding="utf-8") == trimmed


def test_refresh_replaces_the_copy_and_says_so(tmp_path):
    dest = _dest(tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_text("// the game's own\n" + _template(), encoding="utf-8")
    got = agentplay.install_web(tmp_path, refresh=True)
    assert got["script_updated"] is True and got["script_differs"] is False
    assert "replaced" in got["note"]
    assert dest.read_bytes() == agentplay.WEB_TEMPLATE.read_bytes()


def test_the_web_template_carries_the_run_hook():
    """A command-driven game (Turf War) hands the bot a plan through
    ``run``; the template must offer it or every adopter re-adds it."""
    src = _template()
    assert "run?: () => void" in src
    assert "opt.run?.()" in src
    assert 'params.get("bgate_agent")' in src     # still inert without the URL
