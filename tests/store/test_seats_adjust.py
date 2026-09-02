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

from bgate_core.store import artifacts, assets, db
from bgate_core.board import queue
from bgate_core.art import refs

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "public"


def _react(name: str) -> str:
    """One of the React seat workspaces, by file name.

    The seats deck is frontend/src/shell/seats/ now. The classic per-seat
    modules under frontend/public/seats/ were unloaded when that landed and
    deleted once nothing referenced them; _core.js is the one that stayed,
    because nine other views take BGWS off it.
    """
    root = Path(__file__).resolve().parents[2] / "frontend" / "src" / "shell" / "seats"
    return (root / name).read_text(encoding="utf-8")


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
    """THE PROPERTY, AGAINST THE SCREEN THAT DRAWS IT NOW.

    These read seats/qa.js, which the React QA workspace replaced. That file
    stopped being loaded when the seats deck became an island, and was deleted
    once nothing referenced it, so the assertions were passing against code the
    browser had not run in weeks.

    What survives is the property that still matters: a `done` gate nobody
    marked is UNKNOWN, never PASS. A verdict has to be written down with
    evidence before it counts as one.
    """

    def test_an_unmarked_done_gate_is_unknown_not_pass(self):
        src = _react("Qa.tsx")
        assert '"unknown"' in src, "the third state is gone; done would read as pass"
        assert "UNKNOWN" in src, "nothing renders the unknown verdict"

    def test_the_verdict_carries_its_evidence(self):
        """A pass with no transcript, no gate id and no closing words is a
        claim. The type is what forces the backend to keep sending them."""
        src = _react("Qa.tsx")
        for field in ("detail", "gate_item", "result"):
            assert field in src, f"the verdict dropped {field}"

    def test_the_bot_history_is_read_from_the_server(self):
        src = _react("Qa.tsx")
        assert "/api/qa-bots/runs" in src
        assert "/api/qa-bots/contract" in src


class TestArtPanelSurfacesLocksAndDrift:
    """Same move as above: seats/art.js is gone and Art.tsx draws this now."""

    def test_locks_are_read_directly_rather_than_through_the_shared_hook(self):
        src = _react("Art.tsx")
        assert "/api/locks" in src
        assert "useLocks" in src, (
            "the comment explaining why this reads /api/locks directly went "
            "with the code it explained")

    def test_the_sheet_is_what_reports_what_generation_measured(self):
        src = _react("Art.tsx")
        assert "/api/art/sheet" in src
        assert "/api/refs" in src


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
    """seats/gameplay.js is gone; Gameplay.tsx is the workspace.

    WHAT DID NOT COME ACROSS, said out loud rather than deleted quietly: the
    classic panel had stop and steer controls that started disabled until an
    agent was live, and reached /diff, /reopen and /cancel on a queue item. The
    React workspace has none of those verbs. That capability went when the deck
    became an island, not when the file was deleted, and it is a gap worth
    filing rather than a test worth rewriting into something weaker.
    """

    def test_the_workspace_reads_the_item_it_is_showing(self):
        src = _react("Gameplay.tsx")
        assert "/api/" in src, "the workspace fetches nothing"
        assert "telemetry_events" in src, (
            "the playtest numbers the API sends are not read")

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
    """The narrative surface moved twice: out of seats/narrative.js and into
    frontend/src/shell/narrative/, which is where the lore graph, the quests and
    the canon check live now. The endpoints are reached through that module's
    own api.ts rather than by each screen."""

    def test_canon_and_lore_are_reachable(self):
        src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "shell"
               / "narrative" / "api.ts").read_text(encoding="utf-8")
        for endpoint in ("/api/canon/check", "/api/lore"):
            assert endpoint in src, f"the narrative surface lost {endpoint}"
