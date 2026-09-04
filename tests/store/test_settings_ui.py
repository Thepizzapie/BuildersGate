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
  * A credential read from the settings screen. ``ProviderKeys.tsx`` is the
    ONLY surface that may write an API key, it fetches ``/api/providers``
    itself, and its key field is write-only: type=password, cleared on submit,
    never echoed. Settings.tsx renders it and never touches the endpoint.
  * The FLAGS the hierarchy leans on, which are the registry's side of the
    contract and are unchanged by which framework draws the rows.

Static reads only — no server, no browser.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from bgate_core.store import settings as registry

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "public"
INDEX = STATIC / "index.html"
CSS = STATIC / "app.css"
SETTINGS = ROOT / "frontend" / "src" / "shell" / "settings" / "Settings.tsx"
PROVIDERS = ROOT / "frontend" / "src" / "shell" / "settings" / "ProviderKeys.tsx"


def screen() -> str:
    return SETTINGS.read_text(encoding="utf-8")


class TestTheReplacedModulesAreGone:
    """Deleted, not orphaned. A dead module that still ships is a file the next
    reader has to open to discover it is dead, and a page that still loads is a
    page somebody will edit by mistake."""

    GONE = ["settingsview.js", "agents_console.js",
            # The generator panels: ported to frontend/src/shell/settings/
            # (ProviderKeys.tsx, LocalGenerators.tsx, AgentClis.tsx).
            "providerkeys.js", "localsetup.js"]

    @pytest.mark.parametrize("name", GONE)
    def test_the_file_is_deleted(self, name):
        assert not (STATIC / name).exists()

    @pytest.mark.parametrize("name", GONE)
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
            "Settings.tsx must not fetch credentials; ProviderKeys.tsx owns "
            "them and is the only surface that may write one")

    def test_the_panel_is_rendered_by_the_screen_and_nowhere_classic(self):
        """The key panel is a React component the screen renders. Nothing in the
        classic page carries a host for it any more, and no classic script
        writes a key."""
        body = screen()
        assert "<ProviderKeys" in body, "nothing renders the credentials panel"
        index = INDEX.read_text(encoding="utf-8")
        assert 'id="pv-host"' not in index
        assert "providerkeys.js" not in index

    def test_the_key_field_is_write_only(self):
        """The value has exactly one journey, keystrokes -> POST body. The field
        is a password input, it is blanked the moment the save returns whether
        or not it worked, and the row repaints from the response."""
        body = PROVIDERS.read_text(encoding="utf-8")
        assert 'type="password"' in body
        assert 'setKey("")' in body, "the field is not cleared after a save"
        assert '/key`' in body and 'method: "POST"' in body
        # Never a query string, never a path segment: the key rides in the body.
        assert "body: { key, scope }" in body
        assert "?key=" not in body and "key=${" not in body
        # The only thing about the value ever drawn is the last-4 fingerprint.
        assert "row.last4" in body


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

    @pytest.mark.parametrize("flag", ["guard", "human_only", "advanced"])
    def test_the_flags_the_hierarchy_leans_on_are_in_the_payload(self, flag):
        """describe() has to keep sending these, or the page goes flat again."""
        src = Path(registry.__file__).read_text(encoding="utf-8")
        field = src.split("def _field(", 1)[1].split("def describe(", 1)[0]
        assert f'"{flag}"' in field, (
            f"_field() stopped sending {flag}; the settings screen weights its "
            "rows by it and would silently give every switch equal weight")
