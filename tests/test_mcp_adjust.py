"""The MCP server's teeth: permissions, the event loop, one failure shape, and
the two ways a run can spend or clobber more than it was asked to.

Everything here is dispatched through FastMCP rather than by calling the plain
function, because every one of these behaviours lives in the `_tool` decorator
or in what the decorator hands the body — calling the function directly tests
code that no client ever reaches.

Nothing in this file may touch the network, Blender, Godot or gpt-image. The
paying adapters are monkeypatched to EXPLODE where the point is that they are
never called; that is the assertion, not a convenience.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from bgate_core import seats, spend
from bgate_mcp import server


@pytest.fixture()
def wired(root, monkeypatch):
    """A project, and a session that is nobody in particular (a human at the
    machine). Each agent-identity signal is cleared explicitly: leaving one set
    from the ambient environment would silently flip the permission tests."""
    monkeypatch.setenv("BGATE_ROOT", str(root))
    for var in ("BGATE_ACTOR", "BGATE_SEAT", "BGATE_WORK_ITEM"):
        monkeypatch.delenv(var, raising=False)
    return root


async def call(tool: str, /, **kwargs) -> dict:
    """Dispatch through FastMCP and decode the payload a client would receive."""
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


# ---------------------------------------------------------------------------
# A seat may not widen its own lanes
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_an_agent_cannot_widen_its_own_write_lanes(wired, monkeypatch):
    monkeypatch.setenv("BGATE_ACTOR", "agent:item-7")
    before = seats.roles_for(wired)["art"]["write_globs"]

    got = await call("seat_configure", role="art", write_globs=["**"])

    assert got["ok"] is False and "error" in got
    assert "write_globs" in got["error"]
    assert seats.roles_for(wired)["art"]["write_globs"] == before


@pytest.mark.anyio
async def test_a_dispatched_seat_is_an_agent_even_without_bgate_actor(wired,
                                                                     monkeypatch):
    """The gap that would have made the gate decorative: dispatch.py stamps
    BGATE_SEAT and BGATE_WORK_ITEM into a spawned agent and does NOT stamp
    BGATE_ACTOR, so an actor-only check reads every dispatched agent as the
    human at the keyboard."""
    monkeypatch.setenv("BGATE_SEAT", "art")
    monkeypatch.setenv("BGATE_WORK_ITEM", "12")

    got = await call("seat_configure", role="qa", enabled=False)

    assert got["ok"] is False
    assert "qa" in seats.roles_for(wired)  # the seat that would have been muted


@pytest.mark.anyio
async def test_an_agent_may_still_rewrite_a_mission(wired, monkeypatch):
    """Mission is prose about focus, not a permission — gating it would only
    push agents to stop recording what they are doing."""
    monkeypatch.setenv("BGATE_ACTOR", "agent:item-7")

    got = await call("seat_configure", role="art", mission="Ship the gear set.")

    assert got.get("mission") == "Ship the gear set."
    assert "error" not in got


@pytest.mark.anyio
async def test_a_human_can_still_set_lanes(wired):
    got = await call("seat_configure", role="art", write_globs=["art/**"])

    assert got["write_globs"] == ["art/**"]
    assert seats.roles_for(wired)["art"]["write_globs"] == ["art/**"]


# ---------------------------------------------------------------------------
# Tool bodies do not hold the event loop
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_two_slow_tool_calls_overlap_instead_of_serialising(wired,
                                                                 monkeypatch):
    """The blocking-loop blocker, asserted as concurrency rather than as an
    implementation detail: if the bodies ran on the loop, the two sleeps
    would run back to back and every other seat would wait behind them.

    MEASURED AS WINDOW OVERLAP, not against a wall-clock budget - the same
    lesson the next test's docstring records. The first version demanded
    both calls inside 0.7s, which is 0.3s of headroom for anyio's thread
    spin-up on a shared runner; it passed everywhere and then failed once
    on a busy Windows box at 1.28s, which read as "the calls serialised"
    and was really "the runner stalled". Serialised execution has DISJOINT
    sleep windows however slow the hardware; overlap is the property.
    """
    import threading

    windows: list[tuple[float, float]] = []
    note = threading.Lock()

    def slow(*args, **kwargs):
        t0 = time.monotonic()
        time.sleep(0.4)
        t1 = time.monotonic()
        with note:
            windows.append((t0, t1))
        return []

    monkeypatch.setattr(server._seats, "read_notes", slow)

    both = await asyncio.gather(call("seat_notes"), call("seat_notes"))

    assert all("notes" in one for one in both)
    (a0, a1), (b0, b1) = sorted(windows)
    assert a0 < b1 and b0 < a1, \
        f"the two calls serialised (windows {windows})"


@pytest.mark.anyio
async def test_the_loop_still_answers_while_a_tool_is_blocked(wired, monkeypatch):
    """The failure the user actually reports: the dashboard goes dead while one
    seat is mid-batch. A cheap call must return while the slow one is running.

    MEASURED AGAINST THE BLOCKER'S OWN WINDOW, not against any clock budget.
    Two earlier versions each failed once on a busy CI box: a fixed 0.3s
    budget (0.53s read as "the loop blocks" and was really "the runner
    stalled"), then a half-the-sleep ratio, which failed the same way at
    0.53s of a 1.0s block - a stall that lands while the quick call is in
    flight eats any margin denominated in seconds OR in fractions. The
    property has a stall-proof phrasing: the quick call must COMPLETE BEFORE
    THE BLOCKER'S SLEEP ENDS. A freeze delays the quick call and does not
    extend the sleep, so it costs headroom only up to the whole block; a
    genuinely held loop cannot pass at all, because the quick call then
    starts after the sleep has already ended.
    """
    import threading

    BLOCK_FOR = 1.0          # long enough that scheduler noise is not the signal
    entered = threading.Event()
    slow_end = [0.0]

    def slow(*args, **kwargs):
        entered.set()
        time.sleep(BLOCK_FOR)
        slow_end[0] = time.monotonic()
        return []

    monkeypatch.setattr(server._seats, "read_notes", slow)

    slow_call = asyncio.ensure_future(call("seat_notes"))
    while not entered.is_set():        # the blocker is genuinely running
        await asyncio.sleep(0.01)
    quick = await call("project_status")
    quick_done = time.monotonic()
    await slow_call

    assert quick["project"]["name"] == "Test Game"
    assert quick_done < slow_end[0], (
        f"the quick call queued behind the slow one: it finished "
        f"{quick_done - slow_end[0]:.2f}s AFTER the {BLOCK_FOR:.1f}s block "
        "ended, so the loop was not answering during it")


@pytest.mark.anyio
async def test_project_dir_does_not_leak_between_concurrent_calls(wired, tmp_path,
                                                                  monkeypatch):
    """The ContextVar has to survive the hop into a worker thread — and the pool
    reuses threads, so a root left behind on one is a root the next call reads."""
    from bgate_core import db, project

    other = tmp_path / "other"
    project.init(other, "Other Game", pitch="the one that must not be written")
    try:
        names = await asyncio.gather(*[
            call("project_status", project_dir=str(other)) if i % 2 else
            call("project_status") for i in range(8)])
        assert [n["project"]["name"] for n in names] == [
            "Test Game", "Other Game"] * 4
        # And after all that traffic, an unqualified call is still the default.
        assert (await call("project_status"))["project"]["name"] == "Test Game"
    finally:
        db.close_all()


def test_a_tool_may_not_declare_project_dir_itself():
    """Kept from the refactor: a tool with its own project_dir would shadow the
    root binding and put the which-project ambiguity straight back."""
    def rogue(project_dir: str = "") -> dict:
        return {}

    with pytest.raises(TypeError, match="reserved"):
        server._tool(rogue)


# ---------------------------------------------------------------------------
# One failure shape
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_the_bare_error_shape_gains_the_predicate(wired):
    got = await call("lore_brief", ref="nobody-here")
    assert got["ok"] is False and got["error"]


@pytest.mark.anyio
async def test_the_ok_false_shape_keeps_its_error(wired, monkeypatch):
    """item_variants already refused over-limit grids with {ok, error}; the
    normalizer must leave a shape that was already right alone."""
    monkeypatch.setattr(server._items, "estimate_cost", lambda n, q="medium": 9.99)
    got = await call("item_variants", item_class="main_hand", base_name="saber",
                     descriptor="curved saber", materials=["iron", "bone"],
                     tiers=["common", "rare"], limit=1)
    assert got["ok"] is False
    assert "limit" in got["error"]


@pytest.mark.anyio
async def test_the_available_false_shape_gains_the_predicate(wired, monkeypatch):
    """A missing binary used to answer in a dialect of its own — true for
    blender_status, godot_status and image_status, none of which said 'error'."""
    monkeypatch.setattr(server._blender, "available",
                        lambda: {"available": False, "reason": "Blender not found."})
    got = await call("blender_status")

    assert got["ok"] is False
    assert got["error"] == "Blender not found."
    assert got["available"] is False and got["reason"]  # legacy keys survive


def test_a_failure_with_no_stated_reason_still_gets_one():
    got = server._normalize({"ok": False, "stage": "poses"})
    assert got["error"]
    assert got["stage"] == "poses"


def test_success_payloads_are_left_alone():
    """No cosmetic ok=true: an absent 'error' IS the success signal, and adding
    keys to every success payload breaks callers that compare shapes."""
    report = {"blender": {"available": False, "reason": "missing"}}
    assert server._normalize(report) == report
    assert server._normalize({"seats": []}) == {"seats": []}


@pytest.mark.anyio
async def test_an_answer_of_no_is_not_a_failure(wired):
    """seat_can_write's refusal is the tool WORKING. Normalizing it into an
    error would make 'this path is out of lane' indistinguishable from 'the
    oracle is broken'."""
    got = await call("seat_can_write", role="art", path="tests/test_thing.py")
    assert got["allowed"] is False
    assert "error" not in got and "ok" not in got


# ---------------------------------------------------------------------------
# The expensive tool is capped
# ---------------------------------------------------------------------------
@pytest.fixture()
def no_paid_calls(monkeypatch):
    """Any real generation in these tests is a bug, so make it a loud one."""
    from bgate_adapters import imagegen

    def forbidden(*args, **kwargs):
        raise AssertionError("a paid image call escaped the spend gate")

    monkeypatch.setattr(imagegen, "generate", forbidden)
    monkeypatch.setattr(imagegen, "edit", forbidden)


POSES = [{"name": "idle/0", "description": "standing"},
         {"name": "idle/1", "description": "breathing in"},
         {"name": "jab/0", "description": "lead fist out"}]


@pytest.mark.anyio
async def test_image_sprites_refuses_past_the_per_run_ceiling(wired, no_paid_calls):
    spend.set_budget(wired, per_item_usd=0.05, enforced=1)

    got = await call("image_sprites", character_prompt="a fighter", poses=POSES,
                     name="tommy")

    assert got["ok"] is False and got["stage"] == "spend_gate"
    assert got["estimated_usd"] > 0.05 and got["ceiling_usd"] == 0.05
    assert "ceiling" in got["error"]


@pytest.mark.anyio
async def test_image_sprites_refuses_past_the_project_budget(wired, no_paid_calls):
    spend.set_budget(wired, per_project_usd=1.0, enforced=1)
    spend.record(wired, 0.95, kind="image", detail="earlier batch")

    got = await call("image_sprites", character_prompt="a fighter", poses=POSES,
                     name="tommy")

    assert got["ok"] is False and got["stage"] == "spend_gate"
    assert "budget" in got["error"]


@pytest.mark.anyio
async def test_an_affordable_run_passes_the_gate(wired, monkeypatch):
    """The cap must refuse the overrun and nothing else — a gate that also
    blocks affordable work is just an outage."""
    spend.set_budget(wired, per_item_usd=5.0, enforced=1)
    reached = []

    def generate(*args, **kwargs):
        reached.append(kwargs.get("timeout"))
        return {"ok": False, "error": "no API key"}  # stop before any pose

    from bgate_adapters import imagegen
    monkeypatch.setattr(imagegen, "generate", generate)

    got = await call("image_sprites", character_prompt="a fighter", poses=POSES,
                     name="tommy", max_cost_usd=5.0)

    assert got["stage"] == "reference"      # got past the gate, into the work
    assert reached == [300.0]               # and the call carried a timeout


@pytest.mark.anyio
async def test_max_cost_usd_overrides_the_configured_ceiling(wired, monkeypatch):
    """The confirm-the-spend escape hatch item_variants already had as `limit`:
    a tighter cap than the budget still binds, a deliberate looser one buys."""
    spend.set_budget(wired, per_item_usd=0.05, enforced=1)
    bought = []

    from bgate_adapters import imagegen
    monkeypatch.setattr(imagegen, "generate", lambda *a, **k: (
        bought.append(1), {"ok": False, "error": "no API key"})[1])

    tight = await call("image_sprites", character_prompt="a fighter", poses=POSES,
                       name="tommy", max_cost_usd=0.01)
    assert tight["ok"] is False and tight["ceiling_usd"] == 0.01
    assert not bought                       # refused before anything was bought

    loose = await call("image_sprites", character_prompt="a fighter", poses=POSES,
                       name="tommy", max_cost_usd=50.0)
    assert loose["stage"] == "reference" and bought


# ---------------------------------------------------------------------------
# Concurrent seats do not overwrite each other's output
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_concurrent_screenshots_land_on_different_paths(wired, monkeypatch):
    """Two seats, one shot.png: the second write landed under the path the first
    call had just returned, so a seat reviewed the other seat's game."""
    seen = []

    def fake_shot(project, out, **kwargs):
        seen.append(out)
        time.sleep(0.05)                    # overlap the two calls for real
        return {"ok": True, "path": out}

    monkeypatch.setattr(server._godot, "screenshot", fake_shot)

    await asyncio.gather(call("godot_screenshot", godot_project="game", label="a"),
                         call("godot_screenshot", godot_project="game", label="b"))

    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(path.endswith(".png") for path in seen)


@pytest.mark.anyio
async def test_concurrent_blender_runs_render_into_different_dirs(wired, monkeypatch):
    """The adapter always writes <out_dir>/render.png, so the uniqueness has to
    be in the directory this tool hands it."""
    seen = []

    def fake_run(script, **kwargs):
        seen.append(kwargs.get("out_dir"))
        return {"ok": True, "scene": {"totals": {"tris": 0}}}

    monkeypatch.setattr(server._blender, "run_script", fake_run)

    await asyncio.gather(call("blender_run", script="pass", label="hat"),
                         call("blender_run", script="pass", label="hat"))

    assert len(seen) == 2 and seen[0] != seen[1]


def test_a_run_tag_is_unique_across_a_burst():
    """Same label, same second, same process — the case a timestamp alone loses."""
    tags = {server._run_tag("shot") for _ in range(200)}
    assert len(tags) == 200


# ---------------------------------------------------------------------------
# The per-pixel loop that was moved onto Pillow primitives
# ---------------------------------------------------------------------------
def _reference_chroma_key(img, chroma, tol=125, despill=185):
    """The per-pixel implementation, kept here as the oracle: the vectorised
    version is allowed to be faster, not different.

    TWO PASSES NOW, and the second one is a deliberate behaviour change rather
    than drift — see bgate_core.chroma.pinkness. Krea paints a DESATURATED
    magenta (~#c0559f, 143 from the contract colour and so outside tol) instead
    of the flat chroma it is asked for, so a distance key left the interior of
    every frame opaque while the border keyed. The union pass catches the
    backdrop by pinkness; this oracle reimplements that rule per-pixel rather
    than importing it, so the two can still disagree.
    """
    from bgate_core.chroma import (PINK_CHROMA_MIN, PINK_KEY_FRACTION,
                                   is_pinker_than, pinkness)

    px = img.load()
    W, H = img.size
    cr, cg, cb = chroma
    pink_floor = (int(pinkness(chroma) * PINK_KEY_FRACTION)
                  if pinkness(chroma) >= PINK_CHROMA_MIN else None)
    for y in range(H):
        for x in range(W):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            d = ((r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2) ** 0.5
            pinkish = (pink_floor is not None
                       and is_pinker_than((r, g, b), pink_floor))
            if d < tol or pinkish:
                px[x, y] = (0, 0, 0, 0)
            elif d < despill:
                m = (r + g + b) // 3
                px[x, y] = ((r + m) // 2, (g + m) // 2, (b + m) // 2, a)
    return img


def _key_case(chroma, seed=7):
    from PIL import Image

    import random

    random.seed(seed)
    pixels = [(random.randrange(256), random.randrange(256),
               random.randrange(256), 255) for _ in range(64 * 64)]
    # Guarantee all three branches are exercised, not just the random middle.
    pixels[:64] = [tuple(chroma) + (255,)] * 64                # keyed
    pixels[64:128] = [(200, 40, 200, 255)] * 64                # despill fringe
    source = Image.new("RGBA", (64, 64))
    source.putdata(pixels)
    return source


def _assert_matches_reference(chroma):
    source = _key_case(chroma)
    expected = _reference_chroma_key(source.copy(), chroma)
    got = server._chroma_key(source.copy(), chroma)
    for i, (want, have) in enumerate(zip(expected.getdata(), got.getdata())):
        assert want[3] == have[3], f"alpha differs at {i}"
        # Band math floors where the loop floored; allow one step of slack on
        # the despilled channels rather than pinning float32 rounding.
        assert all(abs(a - b) <= 1 for a, b in zip(want[:3], have[:3])), i


def test_the_chroma_key_matches_the_per_pixel_original():
    _assert_matches_reference((255, 0, 255))


def test_a_non_pink_chroma_is_pure_distance_keying():
    """THE ORIGINAL INVARIANT, unchanged and still guarded. The pinkness pass is
    gated on the chroma being a pink, so a cyan key must behave exactly as it did
    before that pass existed — same pixels, same despill, no widening."""
    _assert_matches_reference((0, 255, 255))


def test_the_pink_pass_only_ever_adds():
    """It is a UNION with the distance key, never a replacement. Anything the
    distance rule cut must still be cut — a 'fix' that traded one set of missed
    pixels for another would satisfy the oracle above and still be wrong."""
    chroma = (255, 0, 255)
    source = _key_case(chroma)
    distance_only = _reference_chroma_key(source.copy(), (0, 255, 255))
    both = server._chroma_key(source.copy(), chroma)
    # Every pixel the CHROMA's own distance rule keyed, keyed by hand here so the
    # comparison does not lean on the implementation being tested.
    for i, px in enumerate(source.getdata()):
        d = sum((c - k) ** 2 for c, k in zip(px[:3], chroma)) ** 0.5
        if d < 125:
            assert both.getdata()[i][3] == 0, i
    assert distance_only is not None      # the copy above was not mutated in place


def test_keyed_pixels_carry_no_colour_underneath():
    """consistency_check auto-fails 'dirty alpha' — transparent pixels holding
    RGB. Leaving the chroma under alpha 0 would fail the frames this produces."""
    from PIL import Image

    source = Image.new("RGBA", (8, 8), (0, 255, 0, 255))
    keyed = server._chroma_key(source, (0, 255, 0))

    assert set(keyed.getdata()) == {(0, 0, 0, 0)}
