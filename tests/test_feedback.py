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

    def test_continuous_speech_is_ONE_item_not_shredded(self):
        """The deliberate reversal (real playtest feedback): a continuous
        stretch of talking is ONE feedback item, even when it spans topics —
        because in practice a real complaint spans several segments and
        shredding it routed one issue to three seats. A single segment holding
        two topics spoken with no pause can't be split by a time rule; that
        semantic split is the director's job when it reads the transcript.
        """
        items = feedback.extract([{
            "id": 1, "t_start": 0.0, "t_end": 9.0,
            "text": "The jump feels really floaty. I do not like it. "
                    "But I love the music in this level.",
        }])
        assert len(items) == 1  # one thought, not three shreds
        assert items[0]["segment_ids"] == [1]


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


class TestGroupThoughts:
    """One spoken thought = one item. Consecutive segments merge while the
    speaker keeps talking; a pause starts the next. This replaced sentence-
    splitting, which shredded one complaint across several items/seats."""

    def test_continuous_segments_merge_into_one_thought(self):
        # Same facing complaint, spoken continuously (0s gaps) -> ONE item,
        # routed on the full text, instead of three items across three seats.
        items = feedback.extract([
            {"id": 1, "t_start": 0.0, "t_end": 2.0, "text": "The way he faces is wrong."},
            {"id": 2, "t_start": 2.0, "t_end": 4.0, "text": "He hits me from behind."},
            {"id": 3, "t_start": 4.0, "t_end": 6.0, "text": "He should only hit forward."},
        ])
        assert len(items) == 1
        assert items[0]["seat"] == "gameplay"
        assert items[0]["segment_ids"] == [1, 2, 3]
        assert "faces" in items[0]["text"] and "forward" in items[0]["text"]

    def test_a_pause_starts_a_new_thought(self):
        items = feedback.extract([
            {"id": 1, "t_start": 0.0, "t_end": 2.0, "text": "The jump feels great."},
            {"id": 2, "t_start": 8.0, "t_end": 10.0, "text": "The music is too loud."},
        ])
        assert len(items) == 2
        assert items[0]["seat"] == "gameplay"
        assert items[1]["seat"] == "audio"

    def test_gap_threshold_is_tunable(self):
        segs = [
            {"id": 1, "t_start": 0.0, "t_end": 2.0, "text": "the jump is floaty"},
            {"id": 2, "t_start": 3.5, "t_end": 5.0, "text": "the music is loud"},
        ]
        assert len(feedback.extract(segs, max_gap=1.0)) == 2   # 1.5s gap splits
        assert len(feedback.extract(segs, max_gap=3.0)) == 1   # merges

    def test_group_thoughts_orders_by_time(self):
        thoughts = feedback.group_thoughts([
            {"id": 2, "t_start": 5.0, "t_end": 6.0, "text": "second"},
            {"id": 1, "t_start": 0.0, "t_end": 1.0, "text": "first"},
        ])
        assert thoughts[0]["text"] == "first"


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
