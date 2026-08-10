"""The Settings panel's structural promises, pinned in source.

The page is a two-column shell now — a nav built from the registry's own groups,
three lenses across them, a filter, deep links, and Credentials as a peer
section. None of that is worth much if the next feature quietly re-introduces
the thing the rework removed, and every one of those regressions is invisible
in a screenshot:

  * A key or a group named in JS. The whole scalability claim is that adding a
    switch is one Python entry, so the moment the panel special-cases
    ``gate.mode`` the pile is back and nobody notices until the fifth switch.
  * A credential rendered from the settings payload. ``/api/settings`` returns
    every field's ``value`` verbatim — exactly what lets a new switch render
    with no JS change, and exactly why a secret must never be described there.
    The panel must not read the providers endpoint either.
  * A stolen CSS prefix. ``providerkeys.js`` shipped as ``pk-``, which app.css
    already owned for the peek overlay, and inherited its ``position:fixed``:
    the panel floated over the whole dashboard. Prefixes are global.
  * A rebuilt ``#pv-host``. settingsview.js MOVES that element into its
    Credentials pane; a fresh div with the same id looks identical and is dead,
    because providerkeys.js wired its listeners onto the original.

Static reads only — no server, no browser. These are the invariants a reviewer
would otherwise have to re-derive from two modules and a stylesheet.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from bgate_core import settings as registry

STATIC = Path(__file__).resolve().parents[1] / "bgate_ui" / "static"
VIEW = STATIC / "settingsview.js"
INDEX = STATIC / "index.html"
CSS = STATIC / "app.css"


def source() -> str:
    return VIEW.read_text(encoding="utf-8")


def code() -> str:
    """The module with its comments stripped.

    The prose deliberately NAMES the two switches that explain why the guard
    flag exists; a test that could not tell a comment from a branch would
    forbid the module from explaining itself.
    """
    src = re.sub(r"/\*.*?\*/", "", source(), flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def icons_asked() -> set[str]:
    """Every icon name the panel can ask BGIcon for.

    Three shapes: a literal ``icon("x")``, the third argument of the nav's
    ``item(...)`` helper, and the values of GROUP_ICON.
    """
    body = code()
    names = set(re.findall(r'icon\("(\w+)"', body))
    names |= set(re.findall(r'item\([^,\n]+,\s*"[^"]*",\s*"(\w+)"', body))
    icon_map = re.search(r"const GROUP_ICON = \{(.*?)\};", body, re.S)
    if icon_map:
        names |= set(re.findall(r':\s*"(\w+)"', icon_map.group(1)))
    return names


class TestRendersFromTheDescription:
    def test_no_registry_key_is_named_in_code(self):
        named = [key for key in registry.keys() if key in code()]
        assert not named, (
            f"settingsview.js branches on {named} — the panel renders from "
            "GET /api/settings and may not know that a key exists")

    def test_no_group_is_named_outside_the_icon_map(self):
        """GROUP_ICON is the one allowed mention, and it is a lookup with a
        fallback: a group this map has never heard of still gets a nav row."""
        body = code()
        icon_map = re.search(r"const GROUP_ICON = \{.*?\};", body, re.S)
        assert icon_map, "GROUP_ICON went missing — is the group nav hardcoded?"
        rest = body.replace(icon_map.group(0), "")
        named = [g for g in registry.GROUPS if g in rest]
        assert not named, (
            f"settingsview.js hardcodes the group(s) {named}; the nav is built "
            "from the groups the server sent")

    def test_the_icon_map_falls_back_rather_than_blanking(self):
        assert re.search(r"GROUP_ICON\[\s*name\s*\]\s*\|\|", code())

    def test_every_icon_it_asks_for_exists(self):
        """A missing icon renders as a dashed box. Cheap to catch here."""
        block = (STATIC / "icons.js").read_text(encoding="utf-8")
        known = set(re.findall(r"^\s*(\w+):\s*`",
                               block.split("const P = {", 1)[1]
                                    .split("\n  };", 1)[0], re.M))
        asked = icons_asked()
        assert asked, "no icons requested — did the nav lose its glyphs?"
        assert asked <= known, f"unknown icon(s): {sorted(asked - known)}"

    def test_a_new_group_needs_no_layout_work(self):
        """The panes come from payload.groups, not from a list in this file."""
        body = code()
        assert "this.groups()" in body
        assert "(this.payload && this.payload.groups)" in body


class TestCredentialsStaySeparate:
    def test_the_panel_never_reads_the_providers_endpoint(self):
        """code(), not source(): the comment saying it never reads that
        endpoint is not the module reading that endpoint. This asserted against
        raw text and so failed the moment the module explained itself, which is
        the exact failure code() exists to prevent."""
        assert "/api/providers" not in code(), (
            "settingsview.js must not fetch credentials; providerkeys.js owns "
            "that endpoint, and this module owns none of the values")

    def test_it_renders_no_provider_markup(self):
        """It HOSTS the panel. It does not draw a card, a key or a last-4."""
        body = code()
        assert ".pv-" not in body, "settingsview.js is styling provider cards"
        assert "data-pv-" not in body, "settingsview.js is driving provider controls"
        assert "last4" not in body
        assert "api_key" not in body.lower()

    def test_the_host_is_moved_not_rebuilt(self):
        """The id may be reached through the FOREIGN table rather than spelled
        into a getElementById call. What matters is that the element is FOUND
        and re-parented, not the shape of the lookup: there are three of these
        hosts now and they share one code path."""
        body = code()
        assert "pv-host" in body, "the host is named"
        assert "getElementById(" in body, "the host is found by id"
        assert "appendChild" in body, "the existing element is re-parented"
        assert 'id="pv-host"' not in body, (
            "settingsview.js is building a second #pv-host — providerkeys.js "
            "holds a reference to the original and its listeners are on it")

    def test_index_still_declares_the_host_outside_the_settings_host(self):
        html = INDEX.read_text(encoding="utf-8")
        assert html.count('id="pv-host"') == 1
        assert html.index('id="st-host"') < html.index('id="pv-host"')

    def test_the_credentials_pane_is_a_peer_destination(self):
        body = code()
        assert "cfg-creds-slot" in body, "no slot for the credentials host"
        assert "Credentials" in body, "no nav entry for credentials"


class TestPrecedenceStaysVisible:
    def test_a_locked_field_names_its_variable_and_both_values(self):
        body = code()
        assert "_precedence" in body
        assert "in force:" in body, "the effective value has to be stated"
        assert "saved here:" in body, (
            "an env override hides a stored value; a panel that shows only the "
            "effective one makes a landed save look like a failed one")

    def test_a_locked_field_cannot_be_edited(self):
        assert 'f.locked ? " disabled" : ""' in code()

    def test_the_source_of_every_row_is_drawn(self):
        assert "st-src" in code() and "SOURCE_NOTE" in code()

    def test_there_is_a_lens_for_what_the_environment_owns(self):
        assert "Env-forced" in code()


class TestPrefixIsNotStolen:
    """The cfg- namespace, checked the way the pk- collision should have been."""

    def test_no_other_static_file_uses_the_prefix(self):
        others = []
        for path in STATIC.rglob("*"):
            if path.suffix not in (".js", ".css", ".html") or path == VIEW:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"""["'.\s]cfg-""", text):
                others.append(path.name)
        assert not others, f"cfg- is not ours alone: {others}"

    def test_app_css_still_owns_the_st_controls(self):
        """The toggles, segments and chips keep their app.css rules; only the
        new chrome is injected, so a stylesheet fix is never shadowed."""
        css = CSS.read_text(encoding="utf-8")
        for cls in (".st-toggle", ".st-seg", ".st-chip", ".st-row"):
            assert cls in css
        injected = re.search(r's\.textContent = \[(.*?)\]\.join\(""\)',
                             source(), re.S)
        assert injected, "the injected stylesheet moved"
        # An st- class may be SCOPED (.st-row.cfg-guard) but never redeclared:
        # the injected sheet is unlayered and would beat app.css outright.
        for head, tail in re.findall(r'"\s*(\.st-[\w-]+)([^"]*)',
                                     injected.group(1)):
            assert "cfg-" in head + tail, (
                f"{head} is redeclared in JS; app.css owns it and would lose")


class TestFindability:
    def test_the_filter_covers_help_and_env_vars(self):
        """The word in hand is usually from the help text, or is the variable
        name doing the overriding — not the dotted key."""
        match = re.search(r"_match\(f, terms\) \{.*?\n    \},", source(), re.S)
        assert match, "the filter predicate moved"
        for part in ("f.key", "f.group", "f.help", "env_vars"):
            assert part in match.group(0)

    def test_settings_are_deep_linkable(self):
        body = code()
        assert "#settings/" in body
        assert "hashchange" in body, "a hash typed into an open tab has to work"

    def test_non_default_values_are_surfaced(self):
        body = code()
        assert "isChanged" in body
        assert 'f.source === "stored"' in body

    def test_consequence_comes_from_declared_flags_not_a_key_list(self):
        body = code()
        assert "f.guard" in body and "f.human_only" in body

    @pytest.mark.parametrize("flag", ["guard", "human_only"])
    def test_the_flags_the_hierarchy_leans_on_are_in_the_payload(self, flag):
        """describe() has to keep sending these, or the page goes flat again."""
        src = Path(registry.__file__).read_text(encoding="utf-8")
        field = src.split("def _field(", 1)[1].split("def describe(", 1)[0]
        assert f'"{flag}"' in field, (
            f"_field() stopped sending {flag}; the settings panel weights its "
            "rows by it and would silently give every switch equal weight")
