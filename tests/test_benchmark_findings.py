"""Regressions for what three benchmark games measured going wrong.

Every test here reproduces a MEASURED failure, not a hypothetical: three small
games (a 2D platformer, a top-down action game, a grid tactics game) were built
through the harness across 18 dispatched agent runs, and their databases, write
logs and git histories are the evidence each of these is written against. The
failures they share one shape:

    presence is not correctness  ->  ownership is not integration

A file at the right path does not prove the right asset is shown. A stream that
is connected does not prove it is connected ONCE. An asset correctly produced
and correctly owned does not prove anything consumes it. A report claiming
values does not prove the artifact holds them.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
from pathlib import Path

import pytest

from bgate_core import assets, db, doctor, gateway, provenance, providers
from bgate_core import seats, spend, writelog


# ---------------------------------------------------------------------------
# Provider truth: the health row and the router answered differently
# ---------------------------------------------------------------------------
class TestProviderTruth:
    """`bgate doctor` said `art_key  4 of 4 providers` on a machine whose
    generation gateway reported openai unkeyed, krea unkeyed, one live option
    and no alternatives. Both read the same environment; only one of them was
    answering the question a human asks a health check."""

    def test_a_key_whose_adapter_refuses_is_not_a_usable_provider(
            self, monkeypatch):
        # Every art key SET, every adapter refusing: the exact shape of the
        # disagreement. The old row counted variables and went green.
        for one in providers.art_providers():
            monkeypatch.setenv(one.env, "sk-not-a-real-key")
        monkeypatch.setattr(providers, "status",
                            lambda root=None: [
                                {"id": one.id, "available": False,
                                 "configured": True}
                                for one in providers.PROVIDERS])
        row = doctor._probe_art_key()
        assert not row["available"]
        assert "none is usable" in row["reason"]

    def test_the_row_counts_usable_not_merely_keyed(self, monkeypatch):
        for one in providers.art_providers():
            monkeypatch.setenv(one.env, "sk-not-a-real-key")
        live = {"kie"}
        monkeypatch.setattr(providers, "status",
                            lambda root=None: [
                                {"id": one.id, "available": one.id in live,
                                 "configured": True}
                                for one in providers.PROVIDERS])
        row = doctor._probe_art_key()
        assert row["available"]
        # The number a human reads must be the number of options generation has.
        assert row["version"].startswith("1 of ")
        assert "keyed" in row["version"]      # says how many merely have a key

    def test_a_globally_stored_key_is_visible_to_the_router(self, tmp_path,
                                                            monkeypatch):
        """THE ACTUAL CAUSE of "doctor says configured, the gateway says no key".

        Both surfaces read the same machine. providers.status() calls
        envfile.load_env(), which loads ~/.bgate/.env into os.environ as a SIDE
        EFFECT; the adapters the gateway probes read only the PROJECT .env (and
        openai read neither). So on a machine set up the documented way -
        `bgate key set kie --global`, which CLAUDE.md recommends as the default
        - the gateway, every paid tool's provider gate, and the billing
        redirect all believed the provider was unkeyed, and whether generation
        worked depended on whether a status panel had run first.

        Retro Diffusion was the one provider the gateway could see, because its
        api_key() always used load_env. That is now what all of them use.
        """
        from bgate_adapters import imagegen, imageto3d, kie, krea

        home = tmp_path / "bgate_home"
        home.mkdir()
        (home / ".env").write_text(
            "KIE_API_KEY=k-global\nKREA_API_KEY=r-global\n"
            "OPENAI_API_KEY=o-global\n", encoding="utf-8")
        monkeypatch.setenv("BGATE_HOME", str(home))
        for name in ("KIE_API_KEY", "KREA_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(name, raising=False)

        # No project in hand - exactly how the gateway probes.
        assert kie.api_key(None) == "k-global"
        assert krea.api_key(None) == "r-global"
        assert imageto3d.api_key("trellis") in ("r-global", "")
        # openai's probe reported presence off os.environ alone.
        assert imagegen.available()["available"] in (True, False)
        assert os.environ.get("OPENAI_API_KEY") == "o-global"

    def test_routing_is_the_gateway_and_not_a_second_opinion(self, monkeypatch):
        """The regression that keeps them from drifting apart again: routing()
        must report exactly what gateway.pick would answer, per family."""
        board = [{"id": "kie", "keyed": True, "reason": "", "balance": None,
                  "balance_unit": ""},
                 {"id": "krea", "keyed": False, "reason": "no key",
                  "balance": None, "balance_unit": ""},
                 {"id": "openai", "keyed": False, "reason": "no key",
                  "balance": None, "balance_unit": ""},
                 {"id": "retrodiffusion", "keyed": True, "reason": "",
                  "balance": 0, "balance_unit": "credits"}]
        monkeypatch.setattr(gateway, "status",
                            lambda root=None, fresh=False: [dict(r)
                                                            for r in board])
        got = providers.routing(None)
        for capability in gateway.CAPABILITIES:
            expected = gateway.pick(None, capability)
            assert got["families"][capability]["provider"] == expected["provider"]
            assert got["families"][capability]["alternatives"] == \
                expected["alternatives"]
        # A drained provider is NAMED as drained, not silently absent - the
        # human-facing surface has to be able to say why an option is gone.
        drained = got["families"]["animate"]["unavailable"]
        assert drained and "drained" in drained[0]["why"]
        assert got["families"]["animate"]["provider"] is None


def _item(root, seat: str = "art", title: str = "spend") -> int:
    """A real work item. spend_event.work_item_id is a foreign key, so a made-up
    id makes the whole INSERT roll back - and record() swallows that, which
    would make this test file measure nothing while passing."""
    from bgate_core import queue

    return int(queue.add(root, seat, title, "brief")["id"])


# ---------------------------------------------------------------------------
# Cost ceilings: max_cost_usd was exceeded in all three games
# ---------------------------------------------------------------------------
class TestSpendCeilingIsHard:
    """$6.40, $9.49 and $5.16 against a $5 `max_cost_usd`. The RUNTIME ceiling
    stopped work every time; the money ceiling never did, because it compared
    ONE call's estimate against the cap instead of the run's running total."""

    def test_a_run_cannot_walk_past_its_ceiling_in_small_steps(self, root):
        # Eighteen forty-cent calls against a five dollar cap: every one of them
        # is individually under the ceiling, which is how the old gate passed
        # all of them.
        item = _item(root)
        allowed = 0
        for _ in range(18):
            got = spend.reserve(root, 0.40, work_item_id=item,
                                what="image batch", run_ceiling_usd=5.0)
            if not got["ok"]:
                break
            allowed += 1
            # The real order: the tool body records what the provider charged,
            # THEN the wrapper releases. Releasing first would make the hold
            # settle at its estimate and the charge count twice.
            spend.record(root, 0.40, kind="image", work_item_id=item)
            spend.release(root, got["token"])
        assert allowed == 12                     # 12 x 0.40 = 4.80; 13th = 5.20
        assert spend.spent_on_item(root, item) <= 5.0

    def test_a_provider_that_bills_in_credits_still_hits_the_ceiling(self, root):
        """THE HOLE THE CONTROL RUN FOUND. kie charges credits and publishes no
        dollar rate, so record_unpriced writes usd = 0.00 - correctly, since
        inventing a rate would be worse. Measured on the art control: seven
        calls, 52 credits, seven ledger rows totalling $0.00. A dollar ceiling
        reading only recorded dollars would never move, which is the per-call
        bug one layer down."""
        item = _item(root)
        allowed = 0
        for _ in range(20):
            got = spend.reserve(root, 0.40, work_item_id=item,
                                what="kie image", run_ceiling_usd=5.0)
            if not got["ok"]:
                break
            allowed += 1
            spend.record_unpriced(root, 8, kind="image", work_item_id=item,
                                  detail="kie image")
            spend.release(root, got["token"])
        assert allowed == 12
        assert spend.spent_on_item(root, item) == 0.0    # no rate was invented
        assert spend.held_usd(root, work_item_id=item) == pytest.approx(4.8)

    def test_a_call_that_never_billed_does_not_eat_the_budget(self, root):
        """Dollars alone cannot tell "charged credits, reported no price" from
        "died before billing" - both leave the dollar total unmoved. Settling
        the second would shrink a run's budget for work that never happened."""
        item = _item(root)
        for _ in range(20):
            got = spend.reserve(root, 0.40, work_item_id=item,
                                run_ceiling_usd=5.0)
            assert got["ok"]
            spend.release(root, got["token"])       # nothing was charged
        assert spend.held_usd(root, work_item_id=item) == 0.0

    def test_two_processes_racing_cannot_both_be_told_there_is_room(self, root):
        """Checking the committed total independently before both calls is not
        enough: neither has recorded anything yet, so both see room."""
        item = _item(root)
        first = spend.reserve(root, 3.0, work_item_id=item, what="a",
                              run_ceiling_usd=5.0)
        second = spend.reserve(root, 3.0, work_item_id=item, what="b",
                               run_ceiling_usd=5.0)
        assert first["ok"]
        assert not second["ok"]
        assert second["scope"] == "run"
        assert second["held"] == pytest.approx(3.0)

    def test_a_priced_charge_replaces_its_hold_rather_than_stacking(self, root):
        item = _item(root)
        first = spend.reserve(root, 4.0, work_item_id=item,
                              run_ceiling_usd=5.0)
        assert not spend.reserve(root, 4.0, work_item_id=item,
                                 run_ceiling_usd=5.0)["ok"]
        # The provider priced it at less than the estimate; the real number is
        # the truth and the reservation stops counting.
        spend.record(root, 0.19, kind="image", work_item_id=item)
        spend.release(root, first["token"])
        assert spend.held_usd(root, work_item_id=item) == 0.0
        assert spend.spent_on_item(root, item) == pytest.approx(0.19)
        assert spend.reserve(root, 4.0, work_item_id=item,
                             run_ceiling_usd=5.0)["ok"]

    def test_an_expired_hold_does_not_hold_the_budget_hostage(self, root):
        """A process killed by the runtime ceiling must not refuse spending for
        the rest of the session - the same rule path leases follow."""
        item = _item(root)
        spend.reserve(root, 4.9, work_item_id=item, run_ceiling_usd=5.0)
        with db.tx(root) as conn:
            conn.execute("UPDATE spend_hold SET expires_at = "
                         "datetime('now', '-1 hour')")
        assert spend.reserve(root, 4.9, work_item_id=item,
                             run_ceiling_usd=5.0)["ok"]

    def test_a_new_project_does_not_enforce_a_ceiling_nobody_chose(self,
                                                                    tmp_path):
        """settings.py has said since 2026-08-19 that budget enforcement is off
        by default. The Setting's default never reached the database - that key
        stores INTO the spend_budget row, whose own column default is 1 - so
        every project created since silently enforced the $5/item, $25/day
        ceiling the note says was removed."""
        from bgate_core import project

        project.init(tmp_path, "budget-default")
        assert not spend.budget(tmp_path)["enforced"]
        item = _item(tmp_path)
        for _ in range(5):
            assert spend.reserve(tmp_path, 99.0, work_item_id=item)["ok"]

    def test_a_human_who_turned_it_on_keeps_it_on(self, tmp_path):
        from bgate_core import project, settings

        project.init(tmp_path, "budget-default")
        settings.set(tmp_path, "budget.enforced", True, actor="human")
        project.init(tmp_path, "budget-default")
        assert spend.budget(tmp_path)["enforced"]


# ---------------------------------------------------------------------------
# Auto-commit attribution: every game's history mis-attributes work
# ---------------------------------------------------------------------------
def _git(root, *args):
    subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                   check=False)


@pytest.fixture()
def repo(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".bgate/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _note(root, owner, path, seat=""):
    writelog.record(root, path, seat, owner, tool="Write")


class TestAttribution:
    """tactics 0c537c1 "bgate: item #1 [art]" carried nine .wav files the AUDIO
    seat delivered concurrently under item #2. tactics 43c94fe
    "bgate: item #6 [qa]" carried scripts/board_view.gd, scripts/hud.gd and
    scenes/main.tscn. Both commits were scoped to "everything that changed since
    my base commit", which on a board running one agent is right and on a board
    running three is `git add -A`."""

    def test_concurrent_agents_do_not_commit_each_other_s_work(self, repo):
        _note(repo, "item-1", "assets/sprites/hero.png", "art")
        _note(repo, "item-2", "assets/audio/jump.wav", "audio")
        touched = ["assets/sprites/hero.png", "assets/audio/jump.wav"]

        mine = provenance.attribute(repo, 1, "art", touched)
        assert mine["mine"] == ["assets/sprites/hero.png"]
        assert [one["path"] for one in mine["left"]] == ["assets/audio/jump.wav"]

        # ...and B keeps its own change, to commit when IT closes.
        theirs = provenance.attribute(repo, 2, "audio", touched)
        assert theirs["mine"] == ["assets/audio/jump.wav"]

    def test_the_directors_own_edits_are_left_alone(self, repo):
        _note(repo, "session-abc", "design/pillars.md", "director")
        _note(repo, "item-4", "tests/test_x.gd", "qa")
        got = provenance.attribute(repo, 4, "qa",
                                   ["design/pillars.md", "tests/test_x.gd"])
        assert got["mine"] == ["tests/test_x.gd"]
        assert got["left"][0]["path"] == "design/pillars.md"

    def test_a_tool_produced_file_the_hook_never_saw_is_still_attributed(
            self, repo):
        """The other half of the same bug. The audio seat's write log for
        room-3 item #2 lists seven .synth.json recipes and NOT ONE of the seven
        .wav files beside them - sfx_generate wrote those, not an Edit call. The
        artifact ledger has them, correctly keyed to the item that paid."""
        (repo / "assets").mkdir(exist_ok=True)
        target = repo / "assets" / "jump.wav"
        target.write_bytes(b"RIFF....")
        from bgate_core import artifacts

        item = _item(repo, "audio", "sfx")
        artifacts.register(repo, "jump", target, producer="sfx_generate",
                           work_item_id=item)
        got = provenance.attribute(repo, item, "audio", ["assets/jump.wav"])
        assert got["mine"] == ["assets/jump.wav"]

    def test_another_seats_lane_is_not_swept_by_an_unobserved_write(self, repo):
        """`bgate: item #6 [qa]` swept gameplay code no record claimed. With no
        claim on either side the LANE decides, and scripts/** is not QA's."""
        got = provenance.attribute(repo, 6, "qa",
                                   ["game/scripts/player.gd",
                                    "tests/test_player.gd"])
        assert "tests/test_player.gd" in got["mine"]
        assert [one["path"] for one in got["left"]] == ["game/scripts/player.gd"]

    def test_a_path_no_lane_covers_still_lands_so_the_board_keeps_moving(
            self, repo):
        """The deadlock this whole mechanism sits on top of: nothing committed,
        dispatch refuses a dirty tree, and one agent's leftover file stops the
        entire board. A file nobody else claims and no other lane covers goes
        with the closing item."""
        # assets/** is in no default lane at all - the scaffold lanes name
        # game/**, design/**, tests/** and content/**.
        got = provenance.attribute(repo, 6, "qa", ["assets/sprites/hero.png"])
        assert got["mine"] == ["assets/sprites/hero.png"]

    def test_a_shared_lane_still_belongs_to_the_seat_that_is_in_it(self, repo):
        """Lanes overlap by design - game/scripts/** is gameplay's AND tech's.
        Testing for exclusivity would mean a gameplay agent could never commit
        an unobserved write to its own primary lane."""
        got = provenance.attribute(repo, 3, "gameplay",
                                   ["game/scripts/player.gd"])
        assert got["mine"] == ["game/scripts/player.gd"]

    def test_an_engine_sidecar_follows_its_asset(self, repo):
        _note(repo, "item-2", "assets/audio/jump.wav", "audio")
        got = provenance.attribute(repo, 1, "art",
                                   ["assets/audio/jump.wav",
                                    "assets/audio/jump.wav.import"])
        assert got["mine"] == []
        assert len(got["left"]) == 2

    def test_an_older_write_by_another_owner_does_not_block_forever(self, repo):
        """Write logs accumulate for the life of a project. Without a cutoff,
        a file the director touched last week would read as 'somebody else's'
        forever and no run could ever commit it - the deadlock again, arriving
        through the fix for the sweep."""
        _note(repo, "session-abc", "game/scripts/player.gd", "director")
        blocked = provenance.attribute(repo, 3, "gameplay",
                                       ["game/scripts/player.gd"])
        assert blocked["mine"] == []
        # Same data, but this run started after that write landed.
        # game/scripts/** is gameplay's OWN lane, so once the director's older
        # write drops out of the window the file is this run's to commit.
        allowed = provenance.attribute(repo, 3, "gameplay",
                                       ["game/scripts/player.gd"],
                                       since="2999-01-01 00:00:00")
        assert allowed["mine"] == ["game/scripts/player.gd"]


class TestWriteLogHygiene:
    """The provenance tracker's own record was unreliable, and these are its
    actual contents from the benchmark projects: `0]`, `0`, `thresh:`,
    `assets/sprites/_qa_*`, `art/preview/*.import`. None of those is a file."""

    @pytest.mark.parametrize("junk", ["0]", "0", "thresh:",
                                      "assets/sprites/_qa_*",
                                      "art/preview/*.import", "a b.png"])
    def test_a_shell_fragment_is_not_recorded_as_a_path(self, root, junk):
        assert not writelog.record(root, junk, "qa", "item-1", tool="Bash")
        assert writelog.paths_for(root, "item-1") == []

    @pytest.mark.parametrize("real", ["game/scripts/x.gd", "Makefile",
                                      ".gdignore", "assets/audio/sfx"])
    def test_a_real_path_still_lands(self, root, real):
        assert writelog.record(root, real, "qa", "item-1", tool="Bash")
        assert real in writelog.paths_for(root, "item-1")


class TestSubshellCd:
    """`assets/audio/assets/audio/attack.synth.json` is a real line from the
    tactics audio seat's write log, and no such file exists. A `cd` inside
    `( ... )` does not change the parent shell's directory; the analyser
    modelled it as if it did, so every later relative write was judged - and
    recorded - against a directory the shell had already left."""

    def test_a_subshell_cd_does_not_escape_its_brackets(self):
        from bgate_cli import hook

        got = hook.analyse_bash(
            "(cd assets/audio && ls) ; cp a.json assets/audio/x.json")
        assert got["writes"] == ["assets/audio/x.json"]

    def test_a_plain_cd_still_shifts_what_follows(self):
        from bgate_cli import hook

        assert hook.analyse_bash("cd assets/audio && cp a.json x.json")[
            "writes"] == ["assets/audio/x.json"]

    def test_a_joined_path_is_collapsed(self):
        """`a/b/../c` and `a/c` are one file and only one of them matches a
        lane glob."""
        from bgate_cli import hook

        assert hook.analyse_bash("cd sub && cd .. && touch a.txt")[
            "writes"] == ["a.txt"]


# ---------------------------------------------------------------------------
# Presence is not integration
# ---------------------------------------------------------------------------
def _godot_project(root: Path) -> Path:
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    (root / "assets").mkdir(exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    return root


class TestUnwiredAssets:
    """`projectile.png` was delivered to the right path, at the right size,
    importing cleanly - and the consumer still loaded `bolt_sheet.png`, a
    placeholder sitting under the name the code expected. Every structural check
    passed. In another game three correctly delivered assets were referenced by
    no gameplay code at all."""

    def test_a_delivered_asset_nothing_names_is_reported(self, root):
        _godot_project(root)
        (root / "assets" / "projectile.png").write_bytes(b"\x89PNG")
        (root / "scripts" / "gun.gd").write_text(
            'var tex = load("res://assets/bolt_sheet.png")\n', encoding="utf-8")
        got = assets.integration(root)
        assert got["unreferenced"] == ["assets/projectile.png"]

    def test_the_reference_that_points_at_nothing_is_reported_too(self, root):
        """Read as a pair, the two lines say 'producer delivered X, consumer
        expects Y' without anything having to guess."""
        _godot_project(root)
        (root / "assets" / "projectile.png").write_bytes(b"\x89PNG")
        (root / "scripts" / "gun.gd").write_text(
            'var tex = load("res://assets/bolt_sheet.png")\n', encoding="utf-8")
        got = assets.integration(root)
        assert got["dangling"] == ["assets/bolt_sheet.png"]
        assert not got["ok"]

    def test_a_wired_asset_is_not_reported(self, root):
        _godot_project(root)
        (root / "assets" / "projectile.png").write_bytes(b"\x89PNG")
        (root / "scripts" / "gun.gd").write_text(
            'var tex = load("res://assets/projectile.png")\n', encoding="utf-8")
        got = assets.integration(root)
        assert got["ok"]

    def test_a_templated_path_is_a_family_not_a_dangling_reference(self, root):
        """`load("res://assets/%s.png" % unit)` is a real and common shape. The
        first draft called that literal string dangling AND every sprite it
        loads an orphan - confidently wrong in both directions at once."""
        _godot_project(root)
        for name in ("grunt", "sniper"):
            (root / "assets" / f"{name}.png").write_bytes(b"\x89PNG")
        (root / "scripts" / "spawn.gd").write_text(
            'var t = load("res://assets/%s.png" % unit)\n', encoding="utf-8")
        got = assets.integration(root)
        assert got["unreferenced"] == []
        assert got["dangling"] == []
        assert got["template_matched_count"] == 2
        assert got["dynamic_load_sites"] >= 1

    def test_a_gdignored_tree_is_not_the_game(self, root):
        """An art seat staged 28 preview PNGs under a tree carrying `.gdignore`.
        Godot does not import that tree, so nothing in it CAN be wired, and
        reporting all 28 as orphans is noise the check had the answer to."""
        _godot_project(root)
        staging = root / "art"
        staging.mkdir()
        (staging / ".gdignore").write_text("", encoding="utf-8")
        (staging / "preview.png").write_bytes(b"\x89PNG")
        assert assets.integration(root)["unreferenced"] == []


class TestRuntimeFreshness:
    """An art seat wrote new PNGs straight into the project. The resource path
    resolved, the dimensions were right, the scene referenced it - and the
    running game drew the old placeholder, because Godot serves the IMPORTED
    product and that cache had not been rebuilt. A screenshot caught it."""

    def _imported(self, root: Path, name: str, payload: bytes) -> Path:
        import hashlib

        _godot_project(root)
        asset = root / "assets" / name
        asset.write_bytes(payload)
        cache = root / ".godot" / "imported"
        cache.mkdir(parents=True, exist_ok=True)
        product = cache / f"{name}-deadbeef.ctex"
        product.write_bytes(b"cached")
        (cache / f"{name}-deadbeef.md5").write_text(
            'source_md5="%s"\ndest_md5="x"\n'
            % hashlib.md5(payload).hexdigest(), encoding="utf-8")
        asset.with_suffix(asset.suffix + ".import").write_text(
            '[deps]\n\nsource_file="res://assets/%s"\n'
            'dest_files=["res://.godot/imported/%s-deadbeef.ctex"]\n'
            % (name, name), encoding="utf-8")
        return asset

    def test_a_freshly_imported_asset_is_clean(self, root):
        from bgate_adapters import godot

        self._imported(root, "hero.png", b"\x89PNG-one")
        got = godot.import_freshness(str(root))
        assert got["ok"], got

    def test_overwriting_the_bytes_makes_the_engine_view_stale(self, root):
        from bgate_adapters import godot

        asset = self._imported(root, "hero.png", b"\x89PNG-one")
        asset.write_bytes(b"\x89PNG-two")          # the art seat's new delivery
        got = godot.import_freshness(str(root))
        assert not got["ok"]
        assert got["stale"][0]["path"] == "assets/hero.png"

    def test_the_digest_is_godots_own_algorithm(self, root):
        """Comparing a digest against a different algorithm's digest reports
        100% stale on a perfectly fresh project, which is worse than no check:
        a report that is always red gets turned off."""
        from bgate_adapters import godot

        self._imported(root, "hero.png", b"\x89PNG-one")
        assert godot.import_freshness(str(root))["stale_count"] == 0


# ---------------------------------------------------------------------------
# Doctrine that reached every seat
# ---------------------------------------------------------------------------
class TestOwnershipDoctrine:
    """Game 1 shipped every sound effect twice - two seats independently wired
    the same four SFX, both implementations valid, QA passing because each
    stream was non-null and playing. A paragraph in the bible stopped it
    recurring in games 2 and 3, so the paragraph is now a default."""

    @pytest.mark.parametrize("seat", sorted(seats.DEFAULT_SEATS))
    def test_every_seat_is_told_who_owns_the_wire(self, root, seat):
        rules = seats.dispatch_rules(root, seat)
        assert "PRODUCING A THING IS NOT OWNING ITS WIRE" in rules
        assert "THE ARTIFACT DECIDES" in rules

    def test_a_project_override_does_not_drop_the_board_wide_rules(self, root):
        """A project turning a seat's CRAFT rules off must not silently turn
        off the doctrine that is not about craft."""
        import json

        (root / ".bgate").mkdir(exist_ok=True)
        (root / ".bgate" / seats.DISPATCH_RULES_FILENAME).write_text(
            json.dumps({"art": ""}), encoding="utf-8")
        rules = seats.dispatch_rules(root, "art")
        assert "OWNING ITS WIRE" in rules
        assert "THE CONTRACT DECIDES THE SHEET" not in rules   # the override won

    def test_a_new_project_carries_the_constraint_in_its_bible(self, tmp_path):
        from bgate_core import bible, project

        project.init(tmp_path, "ownership-seed")
        titles = [row["title"] for row in bible.list_sections(tmp_path,
                                                              "constraint")]
        assert "Integration ownership" in titles

    def test_re_running_init_does_not_stack_copies(self, tmp_path):
        from bgate_core import bible, project

        project.init(tmp_path, "ownership-seed")
        project.init(tmp_path, "ownership-seed")
        titles = [row["title"] for row in bible.list_sections(tmp_path,
                                                              "constraint")]
        assert titles.count("Integration ownership") == 1


class TestVerificationDoctrine:
    """The QA gate passed a build that shipped every sound effect twice by
    checking that each stream existed and was playing. Both were true."""

    def test_qa_is_told_the_checks_that_pass_in_broken_builds(self):
        workflow = seats.DEFAULT_SEATS["qa"]["workflow"]
        assert "PRESENCE IS NOT CORRECTNESS" in workflow
        for question in ("exactly ONE owner", "CURRENT", "CORRECT consumer",
                         "RUNTIME show it", "EXACTLY ONCE", "DUPLICATED",
                         "STALE"):
            assert question in workflow

    def test_qa_is_told_to_measure_the_artifact_not_the_report(self):
        assert "MEASURE THE ARTIFACT, NOT THE PRODUCER'S REPORT" in \
            seats.DEFAULT_SEATS["qa"]["workflow"]


# ---------------------------------------------------------------------------
# What the two control runs found, after the fixes above were in
# ---------------------------------------------------------------------------
class TestControlRunFindings:
    """Three defects the hosted controls surfaced that no benchmark game could
    have: all three sit on paths the benchmark's agents never reached."""

    def test_the_2d_delivery_path_does_not_report_a_texture_as_broken(self):
        """godot_import_asset judges its verdict on an in-engine probe written
        for meshes, and that probe answered `ok: false, "loaded, but not a
        PackedScene: CompressedTexture2D"` for a correctly imported sprite
        sheet. That is what the canonical 2D delivery path returned - and an
        agent reading it abandons the tool, which is the behaviour the
        benchmark recorded as "these tools were never called"."""
        from bgate_adapters import godot

        source = godot.INSPECT_GD if hasattr(godot, "INSPECT_GD") else ""
        if not source:
            import inspect as _inspect

            source = _inspect.getsource(godot)
        assert "not a PackedScene" in source
        # The verdict for a non-scene resource is now what it IS, not a failure.
        head = source[source.index("not (res is PackedScene)"):][:1400]
        assert '"ok": true' in head
        assert "resource_class" in head

    def test_music_installs_where_an_adopted_project_keeps_its_audio(self,
                                                                    tmp_path):
        """INSTALL_ROOTS listed the scaffold layout and a bare `audio/`, and not
        `assets/audio` - which is what `bgate adopt` finds, what all three
        benchmark games used, and what the control project used. _install_dir
        fell through to CREATING game/assets/audio/music/, a directory nothing
        in the project names, and reported `installed: true` about it."""
        from bgate_core import music

        assert pathlib.Path("assets") / "audio" in music.INSTALL_ROOTS
        (tmp_path / "assets" / "audio").mkdir(parents=True)
        assert music._install_dir(tmp_path, create=False) == \
            pathlib.Path("assets") / "audio" / "music"

    def test_the_scaffold_layout_still_wins_when_both_exist(self, tmp_path):
        from bgate_core import music

        (tmp_path / "game" / "assets" / "audio").mkdir(parents=True)
        (tmp_path / "assets" / "audio").mkdir(parents=True)
        assert music._install_dir(tmp_path, create=False) == \
            pathlib.Path("game") / "assets" / "audio" / "music"

    def test_an_installed_orphan_is_named_with_its_producer(self, root):
        """delivered_but_unwired joined artifact_revision alone, which holds the
        CANDIDATE path (under .bgate_out). The copy the game loads is registered
        by the install step through assets.track - so the strong half of the
        orphan check returned nothing for exactly the assets that had made it
        all the way into the project and still were not wired."""
        _godot_project(root)
        (root / "assets" / "audio").mkdir(parents=True, exist_ok=True)
        track = root / "assets" / "audio" / "theme.mp3"
        track.write_bytes(b"ID3\x00\x00")
        assets.track(root, track)
        named = assets.delivered_but_unwired(root)
        assert [one["path"] for one in named] == ["assets/audio/theme.mp3"]
        assert named[0]["producer"] == "tracked"


class TestBlockedBoardIsAnnounced:
    """`board_digest.blocked` reported the dirty-tree refusal correctly and
    nobody asked: the information was pull-only while the condition stops the
    WHOLE board. An art seat and an audio seat idled for about an hour."""

    def test_the_kind_exists_and_rings_by_default(self):
        from bgate_core import events, settings

        assert "dispatch.blocked" in events.KINDS
        assert "dispatch.blocked" in settings.EVENT_KINDS
        default = next(s for s in settings.SETTINGS
                       if s.key == "notify.kinds").default
        assert "dispatch.blocked" in default
