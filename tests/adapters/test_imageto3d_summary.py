"""The status an AGENT reads must distinguish configured from running.

THE INCIDENT. ``blender_status`` answered:

    generate.usable  = ["hunyuan-local", "krea", "trellis-cpp"]

An agent tried the two local backends, got connection refused from both, and
reported image-to-3D unavailable. That was relayed upward as fact and the entire
image-to-3D path was written off for a session. Krea is HOSTED — it needs no
local server, its key was set, and it had already produced every texture in the
same build.

Two things made one list do that damage. ``usable`` means CONFIGURED, and for a
local backend configured is not running: the summary probes nothing, on purpose,
because a status call that blocks on a TCP timeout for a server nobody started
is a status call nobody makes. And the local/hosted split — which the adapter
already computes and always did — was flattened away before the agent saw it.

So these tests assert on the SHAPE the agent reads, not on this machine's actual
hardware, which is why every backend list here is stubbed.
"""
from __future__ import annotations

import pytest

from bgate_adapters import imageto3d
from bgate_mcp import server


@pytest.fixture()
def stub(monkeypatch):
    """Force a known status() answer. The real one depends on the developer's
    GPU and keys, which is exactly the kind of thing a contract test must not."""
    def _set(*, local=(), hosted=(), blocked=None):
        rows = ([{"backend": b, "kind": "local", "available": True,
                  "implemented": True} for b in local]
                + [{"backend": b, "kind": "hosted", "available": True,
                    "implemented": True} for b in hosted]
                + [{"backend": b, "kind": "hosted", "available": False,
                    "implemented": True, "reason": why}
                   for b, why in (blocked or {}).items()])
        payload = {
            "ok": bool(local or hosted),
            "gpu": {"name": "NVIDIA GeForce RTX 3060", "vram_gb": 12.0},
            "backends": rows,
            "usable": [*local, *hosted],
            "local": list(local), "hosted": list(hosted),
            "unconditional_licence": [],
            "reason": "" if (local or hosted) else "nothing configured",
        }
        monkeypatch.setattr(imageto3d, "status", lambda *a, **k: payload)
        return payload
    return _set


class TestTheSplitIsVisible:
    def test_local_and_hosted_are_named_separately(self, stub):
        stub(local=("hunyuan-local", "trellis-cpp"), hosted=("krea",))
        got = server._imageto3d_summary()
        assert got["local"] == ["hunyuan-local", "trellis-cpp"]
        assert got["hosted"] == ["krea"]

    def test_usable_is_still_there_for_existing_callers(self, stub):
        """A status field that silently changes shape is its own version of this
        bug. The split is added ALONGSIDE the old key, not instead of it."""
        stub(local=("hunyuan-local",), hosted=("krea",))
        got = server._imageto3d_summary()
        assert set(got["usable"]) == {"hunyuan-local", "krea"}
        assert got["available"] is True

    def test_it_says_out_loud_that_it_did_not_probe(self, stub):
        """The word 'usable' carried an implication the call never checked."""
        stub(local=("hunyuan-local",))
        assert "no server running" in server._imageto3d_summary()["checked"]


class TestTheNoteAnswersTheQuestionThatWasGotWrong:
    def test_a_hosted_option_is_pointed_at_by_name(self, stub):
        """The one sentence that would have saved the session."""
        stub(local=("hunyuan-local", "trellis-cpp"), hosted=("krea",))
        note = server._imageto3d_summary()["note"]
        assert "krea" in note
        assert "no local server" in note

    def test_local_only_is_warned_about_explicitly(self, stub):
        """With nothing hosted, a refused connection really can mean the path is
        down — but only after a probe, and the agent has to be told to run one
        rather than infer from an unqualified 'usable'."""
        stub(local=("hunyuan-local", "trellis-cpp"))
        note = server._imageto3d_summary()["note"]
        assert "CONFIGURED, not" in note
        assert "refused connection" in note

    def test_nothing_configured_still_says_what_to_set(self, stub):
        """The control: the empty answer must not gain a misleading hint."""
        stub()
        got = server._imageto3d_summary()
        assert got["available"] is False
        assert "nothing configured" in got["note"]
        assert "krea" not in got["note"]

    def test_the_draft_warning_survives_the_rewrite(self, stub):
        """A generated mesh is not an asset, and that sentence must not have been
        pushed out by the new hint."""
        stub(hosted=("krea",))
        assert "DRAFT" in server._imageto3d_summary()["note"]


class TestBlockedIsUnchanged:
    def test_a_blocked_backend_keeps_its_reason(self, stub):
        stub(hosted=("krea",), blocked={"meshy": "MESHY_API_KEY not set"})
        assert server._imageto3d_summary()["blocked"]["meshy"] == \
            "MESHY_API_KEY not set"
