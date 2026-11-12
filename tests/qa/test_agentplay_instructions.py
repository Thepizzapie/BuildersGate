"""The decision model's standing instruction (agentplay.instructions)."""
from bgate_core.qa import agentplay


def test_progress_is_the_objective_and_survival_the_constraint():
    """Measured (ff-b67-unity, the Rig): "avoid taking damage first" made
    DEFEND 97% of a turn-based bot's decisions."""
    text = agentplay.instructions(0.1)
    assert "avoid taking damage first" not in text
    assert "progress" in text and "Stay alive" in text
    assert text.index("GOAL") < text.index("Stay alive")


def test_the_hold_window_is_still_named():
    assert "0.25 seconds" in agentplay.instructions(0.25)
