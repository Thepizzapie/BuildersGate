"""The seat-panel audit fixes: approval that actually ships, and panels that
cannot quietly go green.

Two halves. The Python half exercises the one behaviour a seat panel cannot
fake — approving a revision has to put THAT revision's pixels at the path the
game loads, and only a human may do it. The JS half is static: these panels
have no test harness, so the assertions below pin the exact lines the audit
found (default-to-PASS, /review instead of /react, a hardcoded flow whitelist,
a hardcoded .png suffix) so a regression is a red test rather than a re-audit.
"""
from __future__ import annotations

import re

from pathlib import Path

import pytest

from bgate_core import artifacts, assets, db, queue, refs

STATIC = Path(__file__).resolve().parents[1] / "bgate_ui" / "static"


def _read(*parts: str) -> str:
    return (STATIC.joinpath(*parts)).read_text(encoding="utf-8")


def _code(src: str) -> str:
    """The source with comments stripped — a fixed bug is often described in a
    comment right where it used to live, and a prose mention is not a relapse."""
    out, in_block = [], False
    for line in src.splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            in_block = "*/" not in stripped
            continue
        if stripped.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Approval installs the approved revision at the live path
# ---------------------------------------------------------------------------

@pytest.fixture()
def sheet(root):
    """A stable sheet path with two archived revisions, the way the generators
    leave it: r1 approved-worthy, r2 rejected, and r2's bytes still on disk."""
    live = root / "game" / "assets" / "hero_sheet.png"
    live.parent.mkdir(parents=True, exist_ok=True)
    archive = root / ".bgate_out" / "art"
    archive.mkdir(parents=True, exist_ok=True)

    made = []
    for revision, blob in ((1, b"\x89PNG-one" + b"a" * 64), (2, b"\x89PNG-two" + b"b" * 64)):
        snapshot = archive / f"hero_sheet.r{revision}.png"
        snapshot.write_bytes(blob)
        live.write_bytes(blob)      # every generation overwrites the same path
        made.append(artifacts.register(
            root, "hero_sheet", live, producer="image_sprites",
            metadata={"preview": str(snapshot)}))
    return {"live": live, "r1": made[0], "r2": made[1]}


class TestApprovalIsIntegration:
    def test_approving_r1_puts_r1_back_on_disk(self, root, sheet):
        """The blocker: r2 is what is on disk, so approving r1 has to move it."""
        assert sheet["live"].read_bytes() == b"\x89PNG-two" + b"b" * 64

        got = artifacts.review(root, sheet["r1"]["id"], "approved", "on model")

        assert sheet["live"].read_bytes() == b"\x89PNG-one" + b"a" * 64
        assert got["status"] == "integrated"       # it had to be installed
        assert got["metadata"]["integration"]["promoted"] is True
        assert got["metadata"]["integration"]["path"] == "game/assets/hero_sheet.png"

    def test_the_registry_follows_the_file(self, root, sheet):
        artifacts.review(root, sheet["r1"]["id"], "approved")
        tracked = assets.get(root, sheet["live"])
        assert tracked["hash"] == sheet["r1"]["hash"]

    def test_approving_what_is_already_live_does_not_pretend_to_move_it(self, root, sheet):
        got = artifacts.review(root, sheet["r2"]["id"], "approved")
        assert got["status"] == "approved"         # nothing had to be installed
        assert got["metadata"]["integration"] == {
            "ok": True, "promoted": False,
            "path": "game/assets/hero_sheet.png",
            "detail": "already the live file"}
        assert sheet["live"].read_bytes() == b"\x89PNG-two" + b"b" * 64

    def test_an_approval_that_cannot_ship_says_so_loudly(self, root):
        """No archived render and a divergent live file: the decision lands, but
        it must not claim to be live."""
        live = root / "art" / "boss.png"
        live.parent.mkdir(parents=True)
        live.write_bytes(b"first")
        item = artifacts.register(root, "boss", live, producer="image_generate")
        live.write_bytes(b"a later, unregistered render")

        got = artifacts.review(root, item["id"], "approved")

        assert got["status"] == "approved"
        assert got["metadata"]["integration"]["ok"] is False
        assert "cannot be reinstalled" in got["metadata"]["integration"]["detail"]
        assert live.read_bytes() == b"a later, unregistered render"

    def test_integration_is_human_only_and_moves_nothing_for_an_agent(
            self, root, sheet, monkeypatch):
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-9")
        with pytest.raises(PermissionError):
            artifacts.review(root, sheet["r1"]["id"], "approved")
        # the rejected revision is still the file on disk — no side effect
        assert sheet["live"].read_bytes() == b"\x89PNG-two" + b"b" * 64
        assert artifacts.get(root, sheet["r1"]["id"])["status"] == "candidate"

    def test_rejecting_never_touches_the_live_file(self, root, sheet):
        artifacts.review(root, sheet["r1"]["id"], "rejected", "off model")
        assert sheet["live"].read_bytes() == b"\x89PNG-two" + b"b" * 64

    def test_workspace_surfaces_whether_the_approved_revision_is_live(self, root, sheet):
        artifacts.review(root, sheet["r1"]["id"], "approved")
        group = artifacts.workspace(root)[0]
        assert group["approved"]["id"] == sheet["r1"]["id"]
        assert group["approved"]["integration"]["promoted"] is True
        # the shape art.js and the asset workspace view read, unchanged
        for key in ("logical_name", "approved", "candidates", "revisions", "feedback"):
            assert key in group
        for key in ("profile", "consistency", "engine_import", "ref_drift",
                    "lock", "work_item", "used_in_current_build"):
            assert key in group["revisions"][0]


# ---------------------------------------------------------------------------
# workspace() reads a fixed number of queries
# ---------------------------------------------------------------------------

def _count_workspace_queries(root) -> int:
    conn = db.connect(root)
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        artifacts.workspace(root)
    finally:
        conn.set_trace_callback(None)
    return len(seen)


def _seed(root, groups: int, per_group: int) -> None:
    for g in range(groups):
        path = root / "art" / f"asset_{g}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        work = queue.add(root, "art", f"make asset {g}")
        for r in range(per_group):
            path.write_bytes(f"asset-{g}-r{r}".encode())
            artifacts.register(root, f"asset_{g}", path, producer="art",
                               work_item_id=work["id"])


class TestWorkspaceDoesNotFanOut:
    def test_query_count_does_not_scale_with_revisions(self, root):
        _seed(root, groups=2, per_group=2)
        small = _count_workspace_queries(root)
        _seed(root, groups=8, per_group=6)      # 4 -> 52 revisions
        large = _count_workspace_queries(root)
        assert len(artifacts.list_revisions(root, limit=500)) > 50
        assert large == small, (
            f"workspace() ran {large} queries for 52 revisions and {small} for 4 "
            "— a per-revision query is back")

    def test_ref_drift_still_reports_a_repinned_anchor(self, root):
        """The batched pin index has to catch drift the per-row read caught."""
        anchor = root / "anchor.png"
        anchor.write_bytes(b"anchor-one")
        refs.pin(root, "tommy", str(anchor), kind="character")

        out = root / "art" / "tommy_idle.png"
        out.parent.mkdir(parents=True)
        out.write_bytes(b"idle")
        art = artifacts.register(root, "tommy_idle", out, producer="art",
                                 refs=["tommy"])
        assert artifacts.ref_drift(root, art) == []

        anchor.write_bytes(b"anchor-two")           # re-pin: same name, new image
        refs.pin(root, "tommy", str(anchor), kind="character")

        drift = artifacts.ref_drift(root, artifacts.get(root, art["id"]))
        assert len(drift) == 1
        assert drift[0]["name"] == "tommy"
        assert drift[0]["current_revision"] == 2
        assert artifacts.workspace(root)[0]["revisions"][0]["ref_drift"] == drift


# ---------------------------------------------------------------------------
# The panels themselves — static assertions on what the audit found
# ---------------------------------------------------------------------------

class TestQaPanelCannotDefaultToPass:
    def test_an_unmarked_done_gate_is_unknown_not_pass(self):
        src = _read("seats", "qa.js")
        start = src.index("_verdictOf(it) {")
        verdict = src[start:src.index("_paintVerdicts() {", start)]
        assert 'if (it.status === "done") return "UNKNOWN"' in verdict
        assert 'return "PASS"' not in verdict.split('it.status === "done"')[1]

    def test_the_unknown_verdict_is_rendered(self):
        src = _read("seats", "qa.js")
        assert "UNKNOWN:" in src            # a badge style of its own
        assert "qa-unknown" in src

    def test_the_result_panel_reads_the_servers_verdict(self):
        src = _read("seats", "qa.js")
        assert "r.verdict" in src
        assert "baseline_diff" in src
        assert "f.sample" in src
        assert "/api/qa-bots/run-all" in src
        assert "/api/qa-bots/runs" in src

    def test_the_playtest_widget_reads_fields_the_api_sends(self):
        src = _read("seats", "qa.js")
        assert "telemetry_events" in src
        assert "rec.event_count" not in src      # never sent
        assert "st.active" not in src            # never sent

    def test_recording_goes_through_preflight_and_the_build_check(self):
        src = _read("seats", "qa.js")
        start = src[src.index("async startPlaytest()"):src.index("async stopPlaytest()")]
        assert "/api/playtest/preflight" in start
        assert "/api/play/status" in start
        assert "/api/play/rebuild" in start
        assert "bootFrame" in start


class TestArtPanelUsesTheTeachingEndpoint:
    def test_approve_and_reject_go_through_react(self):
        src = _read("seats", "art.js")
        assert "/react" in src
        assert '"approved"' not in src.split("_react(ids, verdict, note)")[1][:1500]
        assert "/api/artifacts/${id}/review" not in src

    def test_locks_and_drift_are_surfaced(self):
        src = _read("seats", "art.js")
        assert "/api/locks" in src
        assert "_driftChips" in src and "ref_drift" in src
        assert "approved, NOT live" in src       # the integration record


class TestStudioFlowsAreDerived:
    def test_no_hardcoded_flow_whitelist(self):
        src = _read("flows.js")
        assert '["workflows", "game"].includes' not in src
        assert "flows()" in src
        assert "window.StudioFlows" in src

    def test_every_registered_flow_module_exists(self):
        """A flow named in MODULES must be a file that is really there.

        This used to name flow_asset.js, flow_agent.js and flow_game.js
        literally, from when the bug was that two finished modules were built
        and never wired up. Two of the three have since been REMOVED on purpose:
        "Asset flow" duplicated the Assets library and the art seat, and "Game
        editor" could not edit anything — no save, no write path — it read the
        Godot tree, screenshotted and dispatched queue items, all of which
        Playtests and Agents already do.

        "Agent flow" is now the third: it drew a second orchestration canvas
        over the same data the Agents console already owns, and between two
        places to watch one floor the one with the conversation, the queue and
        the live rails is the one that survived. With it went the last
        flow_*.js, so this no longer asserts that any exist — an empty registry
        and an empty directory AGREE, and demanding at least one would make
        removing the last module fail a test whose subject is agreement.

        Pinning the file list re-broke the moment the product changed, so what
        gets asserted now is the property the original test was protecting: the
        registry and the files on disk agree, in both directions.
        """
        src = _read("flows.js")
        registered = set(re.findall(r'"/static/(flow_\w+\.js)"', src))
        for module in sorted(registered):
            assert (STATIC / module).is_file(),                 f"flows.js loads {module}, which is not on disk"
        on_disk = {p.name for p in STATIC.glob("flow_*.js")}
        assert on_disk == registered,             f"orphaned flow modules nothing loads: {sorted(on_disk - registered)}"


class TestReferenceThumbnailsRespectTheSuffix:
    @pytest.mark.parametrize("name", ["wf.js", "wf_steps_asset.js", "flows.js"])
    def test_no_hardcoded_png_ref_path(self, name):
        code = _code(_read(name))
        assert '".bgate/refs/"' not in code       # no path built from the name
        assert '+ ".png"' not in code             # no suffix assumed

    def test_the_resolver_handles_versioned_pins(self):
        src = _read("wf.js")
        assert "refParse" in src and "@r" in src
        assert "refPicker" in src            # a real picker, not one sentence
        assert "/api/refs" in src


class TestWorkflowHousekeeping:
    def test_delete_confirms_and_clears_the_stored_document(self):
        src = _read("wf.js")
        body = src[src.index("async deleteSaved(id)"):src.index("/* ---- builder")]
        assert "confirm(" in body or "askConfirm(" in body
        assert '"/api/workspace/studio/wf:" + id' in body

    def test_the_run_bar_reports_why_a_step_failed(self):
        src = _read("wf.js")
        assert "failed with no reason recorded" in src
        assert "f.detail" in src


class TestGameplayControlsAndTheAtlasScan:
    def test_stop_and_steer_start_disabled(self):
        src = _read("seats", "gameplay.js")
        assert 'id="gp-stop" disabled' in src
        assert 'id="gp-steer-btn" disabled' in src
        assert "function setAgentControls(live)" in src

    def test_the_new_queue_verbs_are_reachable(self):
        src = _read("seats", "gameplay.js")
        assert "/diff" in src and "/reopen" in src and "/cancel" in src

    def test_the_atlas_nav_badge_is_gone_on_purpose(self):
        """It counted dead + missing assets and the LIST VIEW was the only place
        those were listed — its tooltip said "open Atlas to see them". The list
        and graph modes were removed on the user's instruction and the badge went
        with them, because a count you cannot click through to is a number that
        only nags. This asserts the removal rather than leaving the old
        assertion to fail, so nobody restores the badge without also deciding
        where dead and missing assets are supposed to live.
        """
        src = _read("atlas.js")
        assert "function badge()" not in src
        assert "sessionStorage" not in src, "the badge's summary cache is back"
        assert "rc-atlas" not in src

    def test_the_shared_scan_survived_the_badge(self):
        """What the other panels actually depend on. The scene builder and the
        code editor read their screen lists from this one cached scan, so the
        cache had to outlive the view that used to trigger it."""
        src = _read("atlas.js")
        assert "TTL_MS" in src
        assert "ensure" in src


class TestNarrativeIsWiredIn:
    def test_the_storyboard_reaches_canon_lore_and_the_queue(self):
        src = _read("seats", "narrative.js")
        for endpoint in ("/api/canon/check", "/api/lore", "/api/lore/link", "/api/queue"):
            assert endpoint in src

    def test_a_canon_conflict_is_shown_and_can_be_overridden(self):
        src = _read("seats", "narrative.js")
        assert "override" in src
        assert "_flagHtml" in src
        assert "CONFLICT" in src
