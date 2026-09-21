from bgate_core.board.seats import _bounded_blockers, STAGE_BLOCKERS_SHOWN
def test_bounded_blockers_caps_and_tallies():
    bl=[f"presentation QA: thing {i}" for i in range(400)]+["scale: x"]*3
    out=_bounded_blockers(bl)
    assert len(out["blocking_the_next_stage"])==STAGE_BLOCKERS_SHOWN
    more=out["blocking_the_next_stage_more"]
    assert more["not_shown"]==403-STAGE_BLOCKERS_SHOWN
    assert more["by_family"]["scale"]==3
def test_bounded_blockers_short_list_untouched():
    assert _bounded_blockers(["a: b"])=={"blocking_the_next_stage":["a: b"]}
