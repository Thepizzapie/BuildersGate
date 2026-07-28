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
    """Inline <script> bodies only — the /static/*.js modules are separate."""
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.S)


def styles(page: str) -> list[str]:
    return re.findall(r"<style[^>]*>(.*?)</style>", page, re.S)


# ---------------------------------------------------------------------------
# 1. Every mutation goes through one helper that reads the response
# ---------------------------------------------------------------------------
class TestMutationsSurfaceTheError:
    def test_the_page_ships_a_shared_mutate_helper(self, page):
        assert "async function mutate(" in page
        # It has to understand BOTH envelopes the backend speaks.
        assert "function apiError(" in page
        assert "function unwrap(" in page

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
        ("addItem", "/api/queue"),
        ("reviewArtifact", "/review"),
        ("promoteFeedback", "/promote"),
        ("dismissFeedback", "/dismiss"),
        ("mergeFeedback", "/merge"),
    ])
    def test_every_named_mutation_routes_through_mutate(self, page, fn, endpoint):
        body = _function_body(page, fn)
        assert "mutate(" in body, f"{fn} still calls fetch directly"
        assert endpoint in body
        assert "await fetch(" not in body, f"{fn} still has a raw fetch"

    def test_a_failed_mutation_skips_the_re_render(self, page):
        # `if (!r.ok) return;` is what keeps the operator's selection alive.
        for fn in ("dispatchItem", "stopItem", "dismissFeedback"):
            assert "if (!r.ok) return;" in _function_body(page, fn)


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
        from bgate_core import db

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
    def test_the_seat_dropdown_offers_unassigned(self, page):
        assert '"unassigned", ...Object.keys(SEATS)' in page

    def test_promote_refuses_an_unrouted_item(self, page):
        body = _function_body(page, "promoteFeedback")
        assert 'seat === "unassigned"' in body
        # and it bails before the request rather than defaulting
        assert body.index('seat === "unassigned"') < body.index("mutate(")

    def test_unassigned_is_a_real_backend_seat(self):
        from bgate_core import feedback

        assert "unassigned" in feedback.SEATS


# ---------------------------------------------------------------------------
# 4. Merged is not dismissed, and merging asks first
# ---------------------------------------------------------------------------
class TestMergedFeedbackIsDistinct:
    def test_merged_items_render_their_own_state(self, page):
        assert "merged_into_id" in page
        assert "merged into" in page
        assert ".feedback-card.st-merged" in page
        assert ".chip.merged" in page

    def test_merging_confirms_because_it_cannot_be_undone(self, page):
        body = _function_body(page, "mergeFeedback")
        assert "confirm(" in body
        assert body.index("confirm(") < body.index("mutate(")

    def test_the_merge_target_links_back_to_the_target_card(self, page):
        assert "function jumpToFeedback(" in page
        assert 'id="feedback-${item.id}"' in page

    def test_backend_still_records_the_link_merge_relies_on(self, root):
        from bgate_core import db

        columns = {row[1] for row in
                   db.connect(root).execute("PRAGMA table_info(playtest_item)")}
        assert "merged_into_id" in columns


# ---------------------------------------------------------------------------
# 5. Nothing in the shell is named after one game
# ---------------------------------------------------------------------------
class TestNoGameSpecificHardcoding:
    def test_the_character_names_are_gone(self, page):
        # Two character names from one game used to be substring-matched into
        # categories and floated to the top of EVERY project's library.
        lowered = page.lower()
        assert "scoville" not in lowered
        code = re.sub(r"/\*.*?\*/", "", lowered, flags=re.S)   # comments may cite them
        assert "tommy" not in code

    def test_there_is_no_fixed_category_order(self, page):
        assert "CAT_ORDER" not in page
        assert "function catOrder(" in page
        assert "GENERIC_CATS" in page

    def test_categories_come_from_the_project(self, page):
        body = _function_body(page, "assetCategory")
        assert "window._canonNames" in body
        assert "split(/[_\\-.]/)" in body       # logical-name prefix fallback

    def test_canon_names_are_taken_from_state(self, page):
        assert "s.lore && s.lore.canon" in page

    def test_the_play_panel_advertises_no_button_the_template_lacks(self, page):
        for absent in ("J/K punch", "U/I kick", "S block", "L duck"):
            assert absent not in page, f"still advertising {absent!r}"
        assert 'id="play-controls"' in page
        assert "function renderControlsHint(" in page

    def test_the_shipped_2d_template_really_only_binds_three_actions(self):
        from pathlib import Path

        project = Path(__file__).resolve().parents[1] / "templates" / "2d" / "project.godot"
        if not project.is_file():
            pytest.skip("templates not present in this checkout")
        text = project.read_text(encoding="utf-8")
        actions = set(re.findall(r"^(\w+)=\{", text.split("[input]")[1], re.M))
        assert actions == {"move_left", "move_right", "jump"}
        for absent in ("punch", "kick", "block", "duck"):
            assert absent not in actions


# ---------------------------------------------------------------------------
# 6. The record button says what to install
# ---------------------------------------------------------------------------
class TestPreflightIsActionable:
    def test_the_reason_is_no_longer_truncated(self, page):
        assert "slice(0, 60)" not in page and "slice(0,60)" not in page

    def test_each_failing_check_carries_a_literal_fix(self, page):
        assert "const PT_FIXES = {" in page
        for check in ("ffmpeg", "transcriber", "mic", "window", "native_game"):
            assert f"{check}:" in page.split("const PT_FIXES = {")[1].split("};")[0]

    def test_it_names_the_install_commands_and_the_doctor(self, page):
        assert 'pip install -e ".[stt,record]"' in page
        assert "bgate doctor" in page

    def test_the_detail_panel_exists_and_is_reachable(self, page):
        assert 'id="pt-why"' in page
        assert "function togglePtWhy(" in page
        assert "what do I install?" in page

    def test_doctor_really_can_answer_without_a_microphone(self):
        from bgate_core import doctor

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

    def test_the_game_frame_grows_with_the_stage(self, page):
        assert "aspect-ratio:16/9" in page.replace(" ", "")
        # the fixed letterbox height is gone
        assert "#play-holder,#gameframe{min-height:300px}" not in page.replace(" ", "")

    def test_the_rail_collapses_before_the_stage_does(self, page):
        flat = page.replace(" ", "")
        assert "@media(max-width:1180px)" in flat
        assert ".deck{grid-template-columns:64px1fr}" in flat

    def test_reduced_motion_is_still_honoured(self, page):
        assert "prefers-reduced-motion" in page


# ---------------------------------------------------------------------------
# 8. Seeking is offset-corrected, like the frame extraction already is
# ---------------------------------------------------------------------------
class TestVideoOffset:
    def test_the_shell_applies_video_offset_when_seeking(self, page):
        assert "activeReviewOffset" in page
        assert "video_offset_s" in page
        body = _function_body(page, "seekReview")
        assert "toVideoTime(t)" in body

    def test_transcript_sync_converts_back_to_session_time(self, page):
        body = _function_body(page, "syncTranscript")
        assert "video.currentTime + activeReviewOffset" in body

    def test_it_degrades_to_zero_when_the_backend_omits_the_field(self, page):
        assert "d.session.video_offset_s ?? d.video_offset_s ?? 0" in page

    def test_the_backend_stores_the_offset_the_shell_wants(self, root):
        from bgate_core import db

        columns = {row[1] for row in
                   db.connect(root).execute("PRAGMA table_info(playtest_session)")}
        assert "video_offset_s" in columns


# ---------------------------------------------------------------------------
# 9. Preflight no longer opens the mic on a hot loop
# ---------------------------------------------------------------------------
class TestPreflightPolling:
    def test_the_fifteen_second_loop_is_gone(self, page):
        assert "setInterval(ptPreflight, 15000)" not in page

    def test_it_throttles_and_backs_off_once_ready(self, page):
        assert "PT_FRESH_MS" in page and "PT_RETRY_MS" in page
        body = _function_body(page, "ptPreflight")
        assert "_ptLastCheck" in body
        assert "ptPanelVisible()" in body

    def test_it_only_runs_while_the_panel_is_on_screen(self, page):
        body = _function_body(page, "ptPanelVisible")
        assert "view-overview" in body
        assert "visibilityState" in body

    def test_it_tries_the_cheap_mic_free_probe_first(self, page):
        body = _function_body(page, "ptPreflight")
        assert "/api/doctor" in body
        assert body.index("/api/doctor") < body.index("/api/playtest/preflight")
        assert "_ptDoctorGone" in body      # one 404 and it stops asking

    def test_it_never_probes_while_recording(self, page):
        assert "if (ptRecording) return;" in _function_body(page, "ptPreflight")


# ---------------------------------------------------------------------------
# 10. One theme layer, and no font that cannot load
# ---------------------------------------------------------------------------
class TestOneThemeLayer:
    def test_the_tokens_are_declared_exactly_once(self, page):
        assert len(re.findall(r"(?m)^\s*:root\{", page)) == 1

    def test_no_font_is_declared_that_can_never_load(self, page):
        # No CDN and no @font-face, so a webfont name in a font stack is a lie:
        # the browser silently falls through and the declaration means nothing.
        assert "@font-face" not in page
        stacks = re.findall(r"font-family:([^;}]+)|--(?:sans|mono):([^;]+);", page)
        declared = " ".join(part for pair in stacks for part in pair)
        for webfont in ("Inter", "JetBrains Mono", "SF Mono", "Roboto", "Manrope"):
            assert webfont not in declared, f"{webfont} is declared and never loads"

    def test_the_surviving_stack_is_all_system_fonts(self, page):
        sans = re.search(r"--sans:([^;]+);", page).group(1)
        assert "Segoe UI" in sans and "system-ui" in sans

    def test_body_is_styled_once_not_four_times(self, page):
        assert len(re.findall(r"(?m)^\s*body\{", page)) == 1

    def test_the_accent_survived_the_flattening(self, page):
        assert "--ember:#ff6a3d" in page
        assert "--void:#000000" in page


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
        for anchor in ('id="rail-nav"', 'id="firstrun"', 'id="world-root"',
                       'id="view-overview"', "function setWorkspace(",
                       "function showFirstRun(", "SeatShell", "World.activate"):
            assert anchor in page, f"{anchor} went missing"

    def test_the_polling_loop_is_intact(self, page):
        for poll in ("setInterval(pollState,", "setInterval(pollActivity,",
                     "setInterval(pollQueue,", "setInterval(ptPoll,"):
            assert poll in page
