"""The Settings panel's structural promises, pinned in source.

WHAT THIS FILE USED TO GUARD, AND WHY MOST OF IT IS GONE. The settings panel was
``settingsview.js``: 1,155 lines that built a two-column shell, injected its own
stylesheet, and rendered every switch from the registry. It has been replaced by
``frontend/src/shell/settings/Settings.tsx`` and the file has been deleted, so
the assertions that read its source went with it rather than being retargeted
line-for-line at a component that solves the same problems differently (an
env-forced row is a `locked` class and a greyed control now, not a string that
says "in force:").

WHAT SURVIVES IS WHAT IS STILL TRUE OF THE PRODUCT, not of a module:

  * A key or a group named in the screen's code. The whole scalability claim is
    that adding a switch is one Python entry, so the moment the panel
    special-cases ``gate.mode`` the pile is back and nobody notices until the
    fifth switch.
  * A credential rendered from the settings payload. ``/api/settings`` returns
    every field's ``value`` verbatim — exactly what lets a new switch render
    with no JS change, and exactly why a secret must never be described there.
  * A stolen CSS prefix. ``providerkeys.js`` shipped as ``pk-``, which app.css
    already owned for the peek overlay, and inherited its ``position:fixed``:
    the panel floated over the whole dashboard. Prefixes are global.
  * A rebuilt ``#pv-host``. providerkeys.js is the ONLY surface that may write
    an API key and it wires its listeners onto that element; a second div with
    the same id looks identical and is dead.
  * The FLAGS the hierarchy leans on, which are the registry's side of the
    contract and are unchanged by which framework draws the rows.

Static reads only — no server, no browser.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from bgate_core import settings as registry

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "public"
INDEX = STATIC / "index.html"
CSS = STATIC / "app.css"
SETTINGS = ROOT / "frontend" / "src" / "shell" / "settings" / "Settings.tsx"


def screen() -> str:
    return SETTINGS.read_text(encoding="utf-8")


class TestTheReplacedModulesAreGone:
    """Deleted, not orphaned. A dead module that still ships is a file the next
    reader has to open to discover it is dead, and a page that still loads is a
    page somebody will edit by mistake."""

    @pytest.mark.parametrize("name", ["settingsview.js", "agents_console.js"])
    def test_the_file_is_deleted(self, name):
        assert not (STATIC / name).exists()

    @pytest.mark.parametrize("name", ["settingsview.js", "agents_console.js"])
    def test_nothing_loads_it(self, name):
        for page in STATIC.glob("*.html"):
            text = page.read_text(encoding="utf-8", errors="ignore")
            assert f'src="/static/{name}"' not in text, f"{page.name} still loads {name}"


class TestRendersFromTheDescription:
    """The registry describes the switches; the screen draws whatever it is
    handed. A key named in the component is a switch that needs UI work."""

    def test_no_registry_key_is_named_in_code(self):
        body = screen()
        keys = [f["key"] for g in registry.describe(ROOT)["groups"]
                for f in g["fields"]]
        assert keys, "the registry describes nothing — this test has gone vacuous"
        named = sorted(k for k in keys if f'"{k}"' in body)
        assert not named, (
            f"Settings.tsx branches on {named} — the screen renders from the "
            "payload, and a key in the code is a switch that needs UI work")


class TestCredentialsStaySeparate:
    def test_the_screen_never_reads_the_providers_endpoint(self):
        assert "/api/providers" not in screen(), (
            "Settings.tsx must not fetch credentials; providerkeys.js owns "
            "them and is the only surface that may write one")

    def test_the_host_is_declared_exactly_once_and_not_in_the_classic_page(self):
        """providerkeys.js mounts into `#pv-host` and wires listeners onto that
        element. A second one with the same id looks identical and is dead."""
        body = screen()
        assert body.count('id="pv-host"') == 1, "the credentials host moved or doubled"
        assert "ProviderKeys" in body, "nothing asks the credentials panel to paint"
        assert 'id="pv-host"' not in INDEX.read_text(encoding="utf-8")


class TestPrecedenceStaysVisible:
    """An env-forced value that renders as an editable control is a lie: the
    save lands, the value does not change, and nothing on screen says why."""

    def test_an_env_forced_row_is_locked_and_its_control_disabled(self):
        body = screen()
        assert "env_override" in body, "nothing reads the overriding variable"
        assert "locked" in body and "disabled: locked" in body, (
            "a locked row has to disable its control, not just style itself")

    def test_the_source_of_every_row_is_drawn(self):
        body = screen()
        assert "f.source" in body, "nothing distinguishes stored from default"
        assert "Env-forced" in body, "no lens for what the environment owns"


class TestPrefixIsNotStolen:
    """The cfg- namespace, checked the way the pk- collision should have been."""

    def test_no_static_file_uses_the_prefix(self):
        others = []
        for path in STATIC.rglob("*"):
            if path.suffix not in (".js", ".css", ".html"):
                continue
            if "dist" in path.parts or "vendor" in path.parts:
                continue
            if re.search(r"""["'.\s]cfg-""", path.read_text(encoding="utf-8",
                                                            errors="ignore")):
                others.append(path.name)
        assert not others, f"cfg- is not ours alone: {others}"

    def test_app_css_still_owns_the_st_controls(self):
        """The toggles, segments and chips keep their app.css rules, so a
        stylesheet fix is never shadowed by an injected sheet."""
        css = CSS.read_text(encoding="utf-8")
        for cls in (".st-toggle", ".st-seg", ".st-chip", ".st-row"):
            assert cls in css


class TestFindability:
    def test_the_filter_covers_help_and_env_vars(self):
        """The word in hand is usually from the help text, or is the variable
        name doing the overriding — not the dotted key."""
        body = screen()
        for part in ("help", "env_vars", "group"):
            assert part in body, f"the filter cannot match on {part}"

    def test_non_default_values_are_surfaced(self):
        body = screen()
        assert 'f.source !== "default"' in body, (
            "nothing marks a row the project has actually changed")

    @pytest.mark.parametrize("flag", ["guard", "human_only"])
    def test_the_flags_the_hierarchy_leans_on_are_in_the_payload(self, flag):
        """describe() has to keep sending these, or the page goes flat again."""
        src = Path(registry.__file__).read_text(encoding="utf-8")
        field = src.split("def _field(", 1)[1].split("def describe(", 1)[0]
        assert f'"{flag}"' in field, (
            f"_field() stopped sending {flag}; the settings screen weights its "
            "rows by it and would silently give every switch equal weight")
