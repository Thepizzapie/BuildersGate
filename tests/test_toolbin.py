"""A tool name is an identifier, and it is used to build a file path.

CodeQL flagged bgate_core/toolbin.local() for uncontrolled data in a path
expression, and it was right about the shape even though it was unreachable in
practice: the name arrives from /api/tools/{name}, the routes only accept keys
of TOOLS, and so nothing hostile ever got through. But the guarantee lived in
the CALLER. The second caller would not have inherited it, and
"ffmpeg/../../.ssh/id_rsa" is a perfectly ordinary string until something
joins it to a directory.
"""
from __future__ import annotations

import pytest

from bgate_core import toolbin


REFUSED = [
    "../../../etc/passwd",
    "ffmpeg/../..",
    "C:/Windows/System32/cmd",
    r"..\..\windows",
    "/etc/shadow",
    "ffmpeg.exe",
    "",
    " ",
    "FFMPEG",              # upper case is not the registry's shape
    "a" * 64,              # unbounded names are their own problem
]


class TestNamesThatBuildPaths:
    @pytest.mark.parametrize("name", REFUSED)
    def test_a_name_that_is_not_an_identifier_is_refused(self, name):
        with pytest.raises(ValueError):
            toolbin.local(name)

    @pytest.mark.parametrize("name", REFUSED)
    def test_resolve_refuses_the_same_names(self, name):
        with pytest.raises(ValueError):
            toolbin.resolve(name)

    @pytest.mark.parametrize("name", REFUSED)
    def test_the_env_override_name_is_guarded_too(self, name):
        """BGATE_<NAME> is read from the environment; the name shapes the key."""
        with pytest.raises(ValueError):
            toolbin.env_var(name)

    def test_a_real_tool_still_resolves(self):
        assert toolbin.env_var("ffmpeg") == "BGATE_FFMPEG"
        # local() returns None or a path depending on the machine; what matters
        # is that it does not raise for a legitimate name.
        toolbin.local("ffmpeg")

    def test_a_plausible_future_tool_is_accepted(self):
        """The guard must not be so tight that adding a tool trips it."""
        for ok in ("godot", "whisper-cpp", "blender4", "ffprobe"):
            assert toolbin.env_var(ok).startswith("BGATE_")


class TestTheRegistryItself:
    def test_every_registered_name_passes_its_own_guard(self):
        for name in toolbin.TOOLS:
            assert toolbin.env_var(name).startswith("BGATE_")

    def test_every_tool_is_pinned_and_hashed(self):
        """An unpinned or unhashed entry is not installable, by construction."""
        for name, tool in toolbin.TOOLS.items():
            assert tool.url.startswith("https://"), name
            assert len(tool.sha256) == 64, name
            assert tool.members and tool.exes, name
