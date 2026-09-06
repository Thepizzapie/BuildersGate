"""The dashboard shell, pinned against the ten frontend audit findings.

The shell is one served HTML file with no build step, so the only thing Python
can assert about it is the text it serves — but that is exactly where these
regressions live: a swallowed error, a hardcoded character name, a controls
line promising buttons the engine never binds, a media query aimed at markup
somebody deleted. Every assertion here is on a string that has to be there (or
must never come back) for the finding to stay fixed.

Two structural checks ride along, because a shell that does not parse takes the
whole product with it: every <style> block balances in-process, and the inline
scripts are handed to `node --check` when node is on the machine (skipped, not
faked, when it is not).
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


@pytest.fixture()
def page(client) -> str:
    got = client.get("/")
    assert got.status_code == 200
    return got.text


def scripts(page: str) -> list[str]:
    """Inline <script> bodies that are actually JAVASCRIPT.

    The /static/*.js modules are separate (excluded via the src= check). An
    import map (``type="importmap"``) is excluded too — it is JSON by spec,
    never JS, and handing its body to ``node --check`` fails for the same
    reason handing it a stylesheet would.
    """
    return [b for tag, b in
            re.findall(r"<script([^>]*)>(.*?)</script>", page, re.S)
            if "src=" not in tag and 'type="importmap"' not in tag]


def styles(page: str) -> list[str]:
    return re.findall(r"<style[^>]*>(.*?)</style>", page, re.S)


@pytest.fixture()
def shell(client, page) -> str:
    """The shell's markup AND its stylesheet, as one string to assert against.

    The CSS used to live in six stacked <style> blocks inside index.html, which
    is why every assertion below was written against the served page. It now
    lives in /static/app.css — one file, one cascade, declared once. The
    properties these tests pin did not change; only which response carries
    them, so they read both rather than being deleted.
    """
    got = client.get("/static/app.css")
    assert got.status_code == 200, "the shell's stylesheet must be served"
    return page + "\n" + got.text


# ---------------------------------------------------------------------------
# 1. Every mutation goes through one helper that reads the response
# ---------------------------------------------------------------------------
class TestMutationsSurfaceTheError:
    def test_the_blanket_swallowing_catch_is_gone(self, page):
        # `.catch(()=>({}))` does not even fire on a 500 — the error body is
        # valid JSON — so it never did anything but hide the response.
        collapsed = re.sub(r"\s+", "", page)
        assert ".catch(()=>({}))" not in collapsed

    def test_failures_become_visible(self, page):
        assert "function toast(" in page
        assert 'class="toasts"' in page or 'className = "toasts"' in page

    @pytest.mark.parametrize("fn, endpoint", [
        ("dispatchItem", "/dispatch"),
        ("stopItem", "/stop"),
        # `addItem` was the old Agents board's queue composer. Both the form
        # and the function are gone; what files work now is the director
        # session (which calls queue_add itself), the seat box in the inspector
        # (POST /api/queue) and the board's deploy-all — all through mutate(),
        # asserted in the React case below.
        ("reviewArtifact", "/review"),
    ])
    def test_every_named_mutation_routes_through_mutate(self, page, fn, endpoint):
        """Every write goes through mutate(), which is what surfaces the error.

        `reviewArtifact` moved into the React assets view; the rule is the same
        there and is checked against that source instead of index.html. The
        others are still classic.
        """
        if fn == "reviewArtifact":
            from pathlib import Path

            src = (Path(__file__).resolve().parents[2] / "frontend" / "src"
                   / "views" / "assets" / "Assets.tsx").read_text(encoding="utf-8")
            assert "mutate(" in src, "the assets view writes without mutate()"
            assert endpoint in src
            assert "await fetch(" not in src.split("async function audit")[0], (
                "a raw fetch crept back into the review path")
            return
        body = _function_body(page, fn)
        assert "mutate(" in body, f"{fn} still calls fetch directly"
        assert endpoint in body
        assert "await fetch(" not in body, f"{fn} still has a raw fetch"

def _function_body(page: str, name: str) -> str:
    """Source of one top-level `async function name(...)` / `function name(...)`."""
    match = re.search(rf"\n\s*(?:async\s+)?function\s+{re.escape(name)}\s*\(", page)
    assert match, f"no function {name} in the served page"
    start = page.index("{", match.end() - 1)
    depth, index = 0, start
    while index < len(page):
        if page[index] == "{":
            depth += 1
        elif page[index] == "}":
            depth -= 1
            if depth == 0:
                return page[start:index + 1]
        index += 1
    raise AssertionError(f"unbalanced body for {name}")


# ---------------------------------------------------------------------------
# 2. A fresh machine never sees the bare offline string
# ---------------------------------------------------------------------------
class TestFirstRunBeatsOffline:
    def test_state_answers_200_with_a_hint_when_there_is_no_project(
            self, tmp_path, monkeypatch):
        from bgate_core.store import db

        monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        try:
            body = TestClient(app).get("/api/state").json()
            assert body["project"] is None
            assert body["hint"]
        finally:
            db.close_all()

    def test_poll_state_reads_the_body_before_declaring_an_outage(self, page):
        body = _function_body(page, "pollState")
        # It must not build a message out of the bare status code any more.
        assert "showFirstRun" in body
        assert "apiError(body, r.status)" in body

    def test_the_offline_card_still_offers_a_retry(self, page):
        assert "function showOffline(" in page
        assert "retry" in _function_body(page, "showOffline")


# ---------------------------------------------------------------------------
# 3. Promote cannot silently file an unrouted bug under the director
# ---------------------------------------------------------------------------
class TestUnroutedStaysUnrouted:
    # The two page-level assertions here covered the shell's own promote
    # dropdown and promoteFeedback(). The playtest rewrite moved triage out of
    # index.html entirely, so both were asserting on a surface that no longer
    # exists. What survives is the rule they were protecting, on the backend.
    def test_unassigned_is_a_real_backend_seat(self):
        from bgate_core.qa import feedback

        assert "unassigned" in feedback.SEATS


# ---------------------------------------------------------------------------
# 4. Merged is not dismissed, and merging asks first
# ---------------------------------------------------------------------------
class TestMergedFeedbackIsDistinct:
    # Three page-level assertions lived here — the merged card's own state, the
    # confirm before mergeFeedback(), and the jump link to the merge target.
    # All three read index.html, and the playtest rewrite took that triage UI
    # out of the shell. The column the whole feature rests on is still checked.
    def test_backend_still_records_the_link_merge_relies_on(self, root):
        from bgate_core.store import db

        columns = {row[1] for row in
                   db.connect(root).execute("PRAGMA table_info(playtest_item)")}
        assert "merged_into_id" in columns


# ---------------------------------------------------------------------------
# 5. Nothing in the shell is named after one game
# ---------------------------------------------------------------------------
def bundle() -> str:
    """The built React bundle.

    SEVERAL INVARIANTS IN THIS FILE MOVED RATHER THAN DIED. The shell is React
    now (frontend/src/shell/), so `assetCategory`, the mutation wrappers and the
    canon-name lookup are no longer text in index.html — but they are still the
    things this file exists to protect, and asserting them against the bundle
    keeps that protection rather than deleting it. The bundle is committed, so
    this needs no build step; a stale dist is itself worth failing on.

    EVERY CHUNK, NOT THE ENTRY FILE. The shell was split into lazy per-view
    chunks (bgate-Assets.js, bgate-Overview.js, ...), which moved the asset
    categoriser out of bgate.js and turned three of these invariants red while
    the code they protect was present and correct one file over. What the
    assertions mean is "this ships in the app", and the app is the directory.
    """
    from pathlib import Path

    dist = Path(__file__).resolve().parents[2] / "src" / "bgate_ui" / "static" / "dist"
    assert (dist / "bgate.js").is_file(), (
        "no built bundle — run `cd frontend && npm run build`")
    return "\n".join(chunk.read_text(encoding="utf-8", errors="replace")
                      for chunk in sorted(dist.glob("*.js")))


def island(name: str) -> str:
    """One source file of the Playtests island (frontend/src/views/playtests/).

    The recorder, the preflight, the review overlay and the triage moved out
    of index.html into React. The invariants below that were pinned against
    the served page's inline script are pinned against the island's SOURCE
    instead — same strings, same function bodies, one directory over.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "frontend" / "src" / "views" / "playtests" / name
    assert src.is_file(), f"no {name} in the Playtests island"
    return src.read_text(encoding="utf-8")


class TestNoGameSpecificHardcoding:
    def test_the_character_names_are_gone(self, page):
        # Two character names from one game used to be substring-matched into
        # categories and floated to the top of EVERY project's library.
        lowered = page.lower()
        assert "scoville" not in lowered
        code = re.sub(r"/\*.*?\*/", "", lowered, flags=re.S)   # comments may cite them
        assert "tommy" not in code

    def test_there_is_no_fixed_category_order(self):
        """The buckets are DERIVED, never a list some game shipped with.

        This moved out of index.html into the React assets view
        (frontend/src/views/assets/categorise.ts). The invariant did not move:
        no hardcoded order, and the generic buckets still exist as the tail the
        project's own entities are sorted in front of.
        """
        src = bundle()
        assert "CAT_ORDER" not in src
        assert "arenas & world" in src, "the generic buckets went missing"

    def test_categories_come_from_the_project(self):
        # Canon entities first, else the logical name's own prefix. Neither is
        # a name this codebase knows.
        src = bundle()
        assert r"[_\-.]" in src, "the logical-name prefix fallback went missing"

    def test_canon_names_are_taken_from_state(self):
        # /api/state ships lore.canon, and the store is what hands it on.
        from pathlib import Path

        store = Path(__file__).resolve().parents[2] / "frontend" / "src" / "store.ts"
        text = store.read_text(encoding="utf-8")
        assert "lore" in text and "canon" in text

    def test_the_play_panel_advertises_no_button_the_template_lacks(self, page):
        """The play panel is the Playtests island now
        (frontend/src/views/playtests/PlayPanel.tsx). The hint still renders
        only what the backend reports — `controls` rides in on the /api/state
        push through store.ts — and promises nothing otherwise."""
        src = island("PlayPanel.tsx")
        for absent in ("J/K punch", "U/I kick", "S block", "L duck"):
            assert absent not in page, f"still advertising {absent!r}"
            assert absent not in src, f"still advertising {absent!r}"
        assert 'id="play-controls"' in src
        assert "controlsLine" in src and "input map" in src
        from pathlib import Path

        store = Path(__file__).resolve().parents[2] / "frontend" / "src" / "store.ts"
        assert "controls" in store.read_text(encoding="utf-8")

    def test_the_shipped_2d_template_really_only_binds_three_actions(self):
        from pathlib import Path

        project = Path(__file__).resolve().parents[2] / "src" / "templates" / "2d" / "project.godot"
        if not project.is_file():
            pytest.skip("templates not present in this checkout")
        text = project.read_text(encoding="utf-8")
        actions = set(re.findall(r"^(\w+)=\{", text.split("[input]")[1], re.M))
        assert actions == {"move_left", "move_right", "jump"}
        for absent in ("punch", "kick", "block", "duck"):
            assert absent not in actions


# ---------------------------------------------------------------------------
# 6. The record button says what is MISSING — and never what to type
# ---------------------------------------------------------------------------
# These tests used to require the opposite: that the panel print
# `pip install -e ".[stt,record]"` and point at `bgate doctor`. That was right
# when the dashboard was only ever run from a source checkout. It is wrong now
# that the product ships as an installer: somebody who ran BuildersGate-setup.exe
# has no checkout to run pip in, and telling them to get one tells them the
# install they chose was a mistake. Anything the app can fetch for itself is a
# button (bgate_core/toolbin), and the only instructions left are the genuinely
# human steps — plug in a microphone, start the game.
# ---------------------------------------------------------------------------
class TestPreflightIsActionable:
    # The panel is frontend/src/views/playtests/PlayPanel.tsx now; the served
    # page is still checked where the string could come back through it.
    def test_the_reason_is_no_longer_truncated(self, page):
        src = island("PlayPanel.tsx")
        for text in (page, src):
            assert "slice(0, 60)" not in text and "slice(0,60)" not in text

    def test_the_human_steps_still_carry_a_sentence(self):
        """What a person must do themselves is still spelled out."""
        src = island("PlayPanel.tsx")
        assert "const PT_FIXES" in src
        fixes = src.split("const PT_FIXES")[1].split("};")[0]
        for check in ("mic", "window", "native_game"):
            assert f"{check}:" in fixes

    def test_the_app_installs_what_it_can_instead_of_instructing(self):
        """ffmpeg is fetchable, so it is a button and not a sentence."""
        src = island("PlayPanel.tsx")
        assert "const PT_INSTALLABLE" in src
        installable = src.split("const PT_INSTALLABLE")[1].split("};")[0]
        assert "ffmpeg" in installable
        assert "function ptInstall(" in src

    def test_it_never_tells_a_packaged_user_to_open_a_terminal(self, page):
        """THE REGRESSION GUARD. These strings were on the Playtests screen of
        an app distributed as a .exe.

        COMMENTS ARE STRIPPED FIRST, deliberately. The rules that removed these
        instructions explain themselves by quoting them, so a raw substring
        search over the file matches the very comments describing why the text
        is gone. What matters is whether a USER can read it, which is the code
        with the commentary taken out. Both the served page and the island's
        source are checked: the panel moved, the rule did not.
        """
        for text in (page, island("PlayPanel.tsx")):
            code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)          # JS block
            code = re.sub(r"<!--.*?-->", "", code, flags=re.S)          # HTML
            for forbidden in ("pip install", "bgate doctor", "source checkout"):
                assert forbidden not in code, (
                    f"{forbidden!r} is back on the playtest panel — a packaged user "
                    f"cannot act on it")

    def test_the_detail_panel_exists_and_is_reachable(self):
        src = island("PlayPanel.tsx")
        assert 'id="pt-why"' in src
        assert "function togglePtWhy(" in src
        # "what do I install?" presumed the answer was always an install. It is
        # "what is missing?" now, because the answer is often a button.
        assert "what is missing?" in src

    def test_doctor_really_can_answer_without_a_microphone(self):
        from bgate_core.runtime import doctor

        assert "ffmpeg" in doctor.CHECKS and "whisper" in doctor.CHECKS
        assert "microphone" in doctor.__doc__.lower()


# ---------------------------------------------------------------------------
# 7. Responsive rules target markup that exists
# ---------------------------------------------------------------------------
DELETED_MARKUP = ("app-shell", "studio-layout", "build-stage",
                  "iteration-sidebar", "workspace-view", "workspace-tab",
                  "side-card", "band", "lower")


class TestResponsiveTargetsLiveMarkup:
    @pytest.mark.parametrize("selector", DELETED_MARKUP)
    def test_no_media_query_targets_deleted_markup(self, page, selector):
        for block in re.findall(r"@media[^{]*\{(.*?\n  \})", page, re.S):
            assert f".{selector}" not in block, \
                f"a breakpoint still targets .{selector}, which no longer exists"

    def test_the_game_frame_grows_with_the_stage(self, shell):
        assert "aspect-ratio:16/9" in shell.replace(" ", "")
        # the fixed letterbox height is gone
        assert "#play-holder,#gameframe{min-height:300px}" not in shell.replace(" ", "")

    def test_the_rail_is_collapsed_by_default(self, shell):
        """The icon rail is the resting state, not a narrow-window fallback.

        This test used to assert that ``@media(max-width:1180px)`` rewrote the
        deck to ``64px 1fr`` — which was the entire collapse mechanism back
        when the DEFAULT was a 236px track. The default is the icon rail now:
        64px, expanding on hover and focus as an overlay so the stage never
        reflows. The assertions move with it.
        """
        flat = shell.replace(" ", "")
        assert "--rail-c:64px" in flat, "the collapsed width is no longer a token"
        assert ".deck{display:grid;grid-template-columns:var(--rail-c)1fr" in flat, \
            "the rail no longer starts collapsed"
        # Expanding must not touch the grid — only the overlay's own width.
        assert ".rail-inner{" in flat
        assert ":is(.rail:hover,.rail:focus-within).rail-inner{" in flat, \
            "the rail no longer opens for a keyboard"

    def test_only_a_pinned_rail_takes_room_from_the_stage(self, shell):
        """A real track and a drag handle appear together, or not at all.

        A splitter that writes a width onto a track nobody reads is a value
        waiting to bite when the state changes, so the handle is in the layout
        for exactly the state that can use it.
        """
        flat = shell.replace(" ", "")
        assert ".deck:has(.rail.pin){grid-template-columns:var(--rail-w)" in flat
        assert '.deck>.split[data-split="rail"]{display:none}' in flat
        assert '.deck:has(.rail.pin)>.split[data-split="rail"]{display:block}' in flat

    def test_the_pin_cannot_survive_a_narrow_window(self, shell, page):
        """A held-open rail may not eat the stage on a small screen.

        Belt (the script drops the class, keeping the stored preference so it
        comes back when the window does) and braces (the stylesheet forces the
        track back to 64px even if the script never ran).
        """
        flat = shell.replace(" ", "")
        assert "@media(max-width:1180px)" in flat
        narrow = flat.split("@media(max-width:1180px){", 1)[1].split("\n}", 1)[0]
        # Unconditional here — the pin has no standing at this width, so the
        # track and the handle go back to what an unpinned rail gets. (The
        # responsive layer is declared last, so this outranks the :has() rule
        # in the layout layer without needing to out-specify it.)
        assert ".deck{grid-template-columns:var(--rail-c)1fr}" in narrow
        assert ".deck>.split{display:none}" in narrow
        assert ".rail.pin.rail-inner{width:var(--rail-c)" in narrow

        assert "bgate-rail-pinned" in page, "the pin no longer persists"
        body = _function_body(page, "syncRailPin")
        assert "railNarrow.matches" in body, \
            "the pin is applied without asking how wide the window is"
        assert 'classList.toggle("pin"' in body

    def test_reduced_motion_is_still_honoured(self, shell):
        assert "prefers-reduced-motion" in shell


# ---------------------------------------------------------------------------
# 8. Seeking is offset-corrected, like the frame extraction already is
# ---------------------------------------------------------------------------
class TestVideoOffset:
    # The review overlay is frontend/src/views/playtests/Review.tsx now.
    def test_the_shell_applies_video_offset_when_seeking(self):
        src = island("Review.tsx")
        assert "activeReviewOffset" in src
        assert "video_offset_s" in src
        body = _function_body(src, "seekReview")
        assert "toVideoTime(t)" in body

    def test_the_backend_stores_the_offset_the_shell_wants(self, root):
        from bgate_core.store import db

        columns = {row[1] for row in
                   db.connect(root).execute("PRAGMA table_info(playtest_session)")}
        assert "video_offset_s" in columns


# ---------------------------------------------------------------------------
# 9. Preflight no longer opens the mic on a hot loop
# ---------------------------------------------------------------------------
class TestPreflightPolling:
    # The preflight is frontend/src/views/playtests/PlayPanel.tsx now. No
    # setInterval anywhere in it: the 30s tick is the bus's fallback timer.
    def test_the_fifteen_second_loop_is_gone(self, page):
        src = island("PlayPanel.tsx")
        assert "setInterval(ptPreflight, 15000)" not in page
        assert "setInterval(" not in src

    def test_it_throttles_and_backs_off_once_ready(self):
        src = island("PlayPanel.tsx")
        assert "PT_FRESH_MS" in src and "PT_RETRY_MS" in src
        body = _function_body(src, "ptPreflight")
        assert "_ptLastCheck" in body
        assert "panelVisible()" in body

    def test_it_only_runs_while_the_panel_is_on_screen(self):
        # The deck's own .active flag (useViewActive), not a hard-coded view
        # id — the panel sits on the Playtests deck, and the check follows it.
        body = _function_body(island("PlayPanel.tsx"), "panelVisible")
        assert "activeRef.current" in body
        assert "visibilityState" in body

    def test_it_tries_the_cheap_mic_free_probe_first(self):
        body = _function_body(island("PlayPanel.tsx"), "ptPreflight")
        assert "/api/doctor" in body
        assert body.index("/api/doctor") < body.index("/api/playtest/preflight")
        assert "_ptDoctorGone" in body      # one 404 and it stops asking

# ---------------------------------------------------------------------------
# 10. One theme layer, and no font that cannot load
# ---------------------------------------------------------------------------
class TestOneThemeLayer:
    def test_the_tokens_are_declared_in_exactly_one_place(self, page, shell):
        """One token layer, not four stacked ones.

        This used to count ``:root{`` in the served page and demand exactly one,
        which was the right guard when six <style> blocks were fighting inside
        index.html. Two things changed. The CSS moved to app.css, and the app
        grew a light ground as well as a dark one — so there are now several
        :root rules ON PURPOSE (the dark default, [data-theme="light"], and a
        prefers-color-scheme block), and counting them proves nothing.

        The property that actually mattered is stronger now and is what gets
        asserted: the shell markup carries NO stylesheet of its own, so there is
        exactly one file where a token can be defined.
        """
        assert not styles(page), \
            "index.html grew a <style> block again — tokens belong in app.css"
        # The href now carries a ?v=<mtime> cache-buster, same as the script
        # tags — without it a stylesheet edit sat in the browser cache and the
        # fix appeared not to work. The property this guards is "app.css is the
        # one stylesheet", which a version query does not weaken, so match the
        # link rather than one exact spelling of it.
        assert re.search(r'<link rel="stylesheet" href="/static/app\.css(\?v=\d+)?">',
                         page), "app.css is no longer the page's stylesheet"
        # Each ground declares its palette once, and only inside app.css.
        assert len(re.findall(r"--accent:#", shell)) == len(
            re.findall(r"(?m)^\s*--accent:#", shell)), "accent declared oddly"

    def test_no_font_is_declared_that_can_never_load(self, page):
        # No CDN and no @font-face, so a webfont name in a font stack is a lie:
        # the browser silently falls through and the declaration means nothing.
        assert "@font-face" not in page
        stacks = re.findall(r"font-family:([^;}]+)|--(?:sans|mono):([^;]+);", page)
        declared = " ".join(part for pair in stacks for part in pair)
        for webfont in ("Inter", "JetBrains Mono", "SF Mono", "Roboto", "Manrope"):
            assert webfont not in declared, f"{webfont} is declared and never loads"

    def test_the_surviving_stack_is_all_system_fonts(self, shell):
        sans = re.search(r"--sans:([^;]+);", shell).group(1)
        assert "Segoe UI" in sans and "system-ui" in sans

    def test_body_is_styled_once_not_four_times(self, shell):
        assert len(re.findall(r"(?m)^\s*body\{", shell)) == 1

    def test_the_accent_survived_the_flattening(self, shell):
        """Ember is still the accent, and every alias still resolves.

        The old form of this test pinned two literals: ``--ember:#ff6a3d`` and
        ``--void:#000000``. Both moved, one deliberately.

        --ember is now an ALIAS of --accent (about 1800 var(--…) references
        across 29 JS files point at the old names, so the aliases stay), and
        --void aliases --bg, whose value is no longer pure black: on true black
        a 1px hairline of rgba(255,255,255,.075) computes to ~#131317 and is
        invisible, so every card leaned on a shadow it could not cast either.

        What must remain true is the thing the test was named for — ember is the
        accent, and the legacy names still resolve to something.
        """
        assert "--accent:#ff6a3d" in shell, "ember is no longer the dark accent"
        for alias in ("--ember:", "--void:", "--plate:", "--seam:", "--bone:"):
            assert alias in shell, f"{alias} alias dropped; ~1800 refs rely on it"
        # The canvas is intentionally off pure black now.
        assert "--void:#000000" not in shell


# ---------------------------------------------------------------------------
# Structure — the shell has to parse, whatever else is true of it
# ---------------------------------------------------------------------------
class TestTheShellStillParses:
    def test_every_style_block_is_balanced(self, page):
        for index, block in enumerate(styles(page)):
            assert block.count("{") == block.count("}"), f"style block {index}"

    def test_every_inline_script_parses(self, page, tmp_path):
        """Real parse, via node when it is on this machine.

        A shell that does not parse takes the whole product down, and the file
        is edited by hand — so this is worth a subprocess.
        """
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("node not available to parse-check the inline scripts")
        blocks = scripts(page)
        assert len(blocks) >= 2
        for index, block in enumerate(blocks):
            path = tmp_path / f"block_{index}.js"
            path.write_text(block, encoding="utf-8")
            done = subprocess.run([node, "--check", str(path)],
                                  capture_output=True, text=True)
            assert done.returncode == 0, \
                f"inline script {index} does not parse:\n{done.stderr}"

    def test_the_nav_the_first_run_card_and_the_world_host_all_survive(self, page):
        # THE ANCHOR LIST FOLLOWED THE SHELL. The rail is React (the 4a shell),
        # so `id="rail-nav"` and the SeatShell owner entry are gone by design;
        # what still has to be true is that the page mounts the shell, still
        # owns the workspace switch and the first-run decision, and still hosts
        # the classic views that were never converted.
        # The World deck is a React island too now (frontend/src/views/world/),
        # so its anchor is the data-react host rather than #world-root and the
        # World.activate() call that used to wake world.js.
        for anchor in ('data-react="shell"', 'id="firstrun"', 'data-react="world"',
                       'id="view-overview"', "function setWorkspace(",
                       "function showFirstRun("):
            assert anchor in page, f"{anchor} went missing"

    def test_the_polling_loop_is_intact(self, page):
        """The four readers are BGEvents watchers now (frontend/public/events.js):
        run now, on a matching event, and on a slow fallback timer. The timers
        themselves are gone, and must stay gone - one of them coming back is a
        panel asking the server every few seconds what the bus already said."""
        for poll in ("BGEvents.watch(pollState,", "BGEvents.watch(pollActivity,",
                     "BGEvents.watch(pollQueue,"):
            assert poll in page
        for timer in ("setInterval(pollState,", "setInterval(pollActivity,",
                      "setInterval(pollQueue,", "setInterval(ptPoll,"):
            assert timer not in page
        assert 'src="/static/events.js?v=' in page   # stamped, like every module
        # The recorder's watcher moved with the recorder into the Playtests
        # island: useEvents on playtest.*, fast only while a session is live.
        src = island("PlayPanel.tsx")
        assert "useEvents(ptPoll, { kinds: [\"playtest.*\"]" in src
        assert "setInterval(" not in src
