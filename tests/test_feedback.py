"""Feedback classification — a first-pass sorter, tested for both directions.

False positives here are worse than misses: an item wrongly marked 'fix' sends an
agent to change something the user liked. Negated praise ("I don't like the jump")
is the trap, and it's tested explicitly.
"""
from __future__ import annotations

import pytest

from bgate_core import feedback


class TestKind:
    @pytest.mark.parametrize("text,expected", [
        ("I really like how the jump feels", "like"),
        ("that's so satisfying", "like"),
        ("there's a bug where the enemy clips through the wall", "fix"),
        ("the movement feels really floaty", "fix"),
        ("this is frustrating and confusing", "fix"),
        ("we should add a sound when you land", "add"),
        ("there should be some feedback on hit", "add"),
        ("the enemies are too fast", "change"),
        ("make it snappier instead of that", "change"),
        ("why does the door do that", "question"),
        ("the tree is over there", "note"),
    ])
    def test_classification(self, text, expected):
        assert feedback.classify(text)[0] == expected

    def test_negated_praise_is_a_complaint_not_a_like(self):
        """The trap: 'like' appears, but the meaning is inverted."""
        for text in ("I don't like the jump", "I do not like how that feels",
                     "that's not great", "didn't like that at all"):
            assert feedback.classify(text)[0] == "fix", text

    def test_scores_are_exposed_for_inspection(self):
        kind, scores = feedback.classify("there's a bug and it's annoying")
        assert kind == "fix"
        assert scores["fix"] >= 4


class TestNoise:
    @pytest.mark.parametrize("text", [
        "", "  ", "okay", "um", "uh", "hmm", "yeah", "right",
        "Thanks for watching!",  # whisper's classic silence hallucination
        "you", ".",
    ])
    def test_filler_is_noise(self, text):
        assert feedback.is_noise(text) is True

    @pytest.mark.parametrize("text", [
        "the jump feels floaty",
        "I really like that",
        "we should add sound",
    ])
    def test_real_feedback_is_not_noise(self, text):
        assert feedback.is_noise(text) is False


class TestRouting:
    @pytest.mark.parametrize("text,seat", [
        ("the fps tanks in this room", "tech"),
        ("the music is too loud", "audio"),
        ("that sprite looks ugly", "art"),
        ("this dialogue is confusing", "narrative"),
        ("the jump feels floaty", "gameplay"),
        ("the weather is nice today", "unassigned"),
    ])
    def test_routing(self, text, seat):
        assert feedback.route(text) == seat


class TestSplitUtterances:
    """Whisper segments are not utterances — this split is load-bearing.

    Caught on real speech: one segment held a jump complaint AND a music
    compliment. Classified whole it became a single 'fix' routed to AUDIO (the
    word "music" won), so a physics complaint went to the wrong seat and the
    compliment vanished.
    """

    def test_multi_sentence_segment_splits(self):
        parts = feedback.split_utterances({
            "t_start": 0.0, "t_end": 9.0,
            "text": "The jump feels floaty. I do not like it. But I love the music here.",
        })
        assert len(parts) == 3
        assert parts[0]["text"] == "The jump feels floaty."

    def test_timestamps_are_interpolated_and_ordered(self):
        parts = feedback.split_utterances({
            "t_start": 10.0, "t_end": 20.0,
            "text": "The jump feels floaty. But I love the music here.",
        })
        assert parts[0]["t_start"] == 10.0
        assert 10.0 < parts[1]["t_start"] < 20.0
        assert parts[0]["t_end"] <= parts[1]["t_start"]
        assert parts[-1]["t_end"] <= 20.0

    def test_single_sentence_passes_through(self):
        parts = feedback.split_utterances(
            {"t_start": 1.0, "t_end": 2.0, "text": "The jump feels floaty"})
        assert len(parts) == 1
        assert parts[0]["t_start"] == 1.0

    def test_empty_segment_yields_nothing(self):
        assert feedback.split_utterances({"t_start": 0, "t_end": 1, "text": "  "}) == []

    def test_the_real_regression_each_remark_gets_its_own_seat(self):
        items = feedback.extract([{
            "id": 1, "t_start": 0.0, "t_end": 9.0,
            "text": "The jump feels really floaty. I do not like it. "
                    "But I love the music in this level.",
        }])
        by_text = {i["text"]: i for i in items}
        jump = next(i for t, i in by_text.items() if "jump" in t)
        music = next(i for t, i in by_text.items() if "music" in t)

        assert jump["seat"] == "gameplay" and jump["kind"] == "fix"
        assert music["seat"] == "audio" and music["kind"] == "like"
        assert jump["t"] < music["t"]


class TestSpeechVariance:
    """Whisper does not return the words you imagined. All observed on real audio."""

    @pytest.mark.parametrize("text", [
        "the jump feels really floating",   # whisper's rendering of "floaty"
        "the jump feels really floaty",
        "movement is janked up",
        "the camera is drifting",
    ])
    def test_stem_matching_survives_transcription(self, text):
        assert feedback.classify(text)[0] == "fix", text

    @pytest.mark.parametrize("text,seat", [
        ("the enemies are way too fast here", "gameplay"),   # plural missed \benemy\b
        ("the enemy is too fast", "gameplay"),
        ("those sounds are too loud", "audio"),
        ("the sprites look wrong", "art"),
        ("the framerates drop here", "tech"),
    ])
    def test_plurals_route(self, text, seat):
        assert feedback.route(text) == seat


class TestAnaphora:
    """'I do not like it' is real feedback with no routable noun in it."""

    def test_pronoun_utterance_inherits_seat_within_segment(self):
        items = feedback.extract([{
            "id": 1, "t_start": 0.0, "t_end": 6.0,
            "text": "The jump feels floaty. I do not like it.",
        }])
        orphan = next(i for i in items if "not like" in i["text"])
        assert orphan["seat"] == "gameplay"
        assert orphan["seat_inherited"] is True
        assert orphan["kind"] == "fix"

    def test_inheritance_does_not_cross_segments(self):
        """Across a pause, 'it' is anyone's guess — unassigned beats wrong."""
        items = feedback.extract([
            {"id": 1, "t_start": 0.0, "t_end": 2.0, "text": "The jump feels floaty."},
            {"id": 2, "t_start": 30.0, "t_end": 32.0, "text": "I do not like it."},
        ])
        orphan = next(i for i in items if "not like" in i["text"])
        assert orphan["seat"] == "unassigned"

    def test_long_sentence_is_not_treated_as_anaphoric(self):
        items = feedback.extract([{
            "id": 1, "t_start": 0.0, "t_end": 8.0,
            "text": "The jump feels floaty. "
                    "I think the whole thing needs a rethink from scratch honestly.",
        }])
        long_one = next(i for i in items if "rethink" in i["text"])
        assert long_one["seat_inherited"] is False

    def test_routable_sentence_never_inherits(self):
        items = feedback.extract([{
            "id": 1, "t_start": 0.0, "t_end": 6.0,
            "text": "The jump feels floaty. I love this music.",
        }])
        music = next(i for i in items if "music" in i["text"])
        assert music["seat"] == "audio"
        assert music["seat_inherited"] is False


class TestExtract:
    def test_drops_noise_and_keeps_signal(self):
        segments = [
            {"id": 1, "t_start": 1.0, "t_end": 2.0, "text": "okay"},
            {"id": 2, "t_start": 5.0, "t_end": 7.0, "text": "the jump feels really floaty"},
            {"id": 3, "t_start": 9.0, "t_end": 10.0, "text": "um"},
            {"id": 4, "t_start": 12.0, "t_end": 14.0, "text": "I love this music though"},
        ]
        items = feedback.extract(segments)
        assert len(items) == 2
        assert items[0]["kind"] == "fix" and items[0]["seat"] == "gameplay"
        assert items[0]["t"] == 5.0
        assert items[1]["kind"] == "like" and items[1]["seat"] == "audio"

    def test_carries_segment_id_for_traceback(self):
        items = feedback.extract(
            [{"id": 42, "t_start": 3.0, "t_end": 4.0, "text": "the jump is too slow"}])
        assert items[0]["segment_id"] == 42
