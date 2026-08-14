"""Every setting has a human name, and every group has a glyph.

WHY THIS IS A TEST AND NOT A CONVENTION. The Settings screen used to title each
row with its KEY — `dispatch.allow_dirty`, `notify.question_stale_h` — which
reads as a config file to anyone who did not write the code. Names fixed that,
but a name lives in a table beside the registry rather than in the Setting
itself, so the failure mode is a NEW switch quietly falling back to showing its
identifier while every older row looks fine. Nobody notices that for a release.

The fallback in label_for() is deliberately decent, which is exactly why the
gap has to be caught here instead of on screen.
"""
from __future__ import annotations

from bgate_core import settings


class TestEverySettingIsNamed:
    def test_no_setting_falls_back_to_its_key(self):
        missing = [s.key for s in settings.SETTINGS if s.key not in settings.LABELS]
        assert not missing, (
            "these settings would be titled with their identifier: "
            + ", ".join(missing))

    def test_no_label_for_a_setting_that_does_not_exist(self):
        """A renamed key leaves its old label behind, pointing at nothing."""
        known = {s.key for s in settings.SETTINGS}
        stray = [k for k in settings.LABELS if k not in known]
        assert not stray, f"labels for keys no longer in the registry: {stray}"

    def test_labels_are_not_just_the_key_again(self):
        """A label that restates the identifier is not a name."""
        lazy = [k for k, v in settings.LABELS.items()
                if v.lower().replace(" ", "_") in k.lower()]
        assert not lazy, f"these labels only echo the key: {lazy}"

    def test_labels_read_as_english(self):
        for key, label in settings.LABELS.items():
            assert label[:1].isupper(), f"{key}: label should start capitalised"
            assert "_" not in label, f"{key}: {label!r} still has an identifier in it"
            assert len(label) < 70, f"{key}: label is a sentence, not a name"


class TestEveryGroupHasAnIcon:
    def test_all_groups_covered(self):
        missing = [g for g in settings.GROUPS if g not in settings.GROUP_ICONS]
        assert not missing, f"groups with no icon: {missing}"

    def test_describe_emits_label_and_icon(self, tmp_path):
        """The panel reads these off the API, so the API has to carry them."""
        from bgate_core import db
        db.connect(tmp_path)
        out = settings.describe(tmp_path)
        assert out["groups"], "describe returned no groups"
        for group in out["groups"]:
            assert group.get("icon"), f"{group['name']} has no icon in the payload"
            for field in group["fields"]:
                assert field.get("label"), f"{field['key']} has no label in the payload"
