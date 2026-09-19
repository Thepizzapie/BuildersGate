"""A tool body that never returns must not hold the MCP call open forever."""
import threading
import time

import pytest

from bgate_mcp import server


def test_stuck_body_returns_resumable_error(monkeypatch):
    monkeypatch.setattr(server, "TOOL_CEILING_S", 0.3)
    release = threading.Event()

    def stuck():
        release.wait(30)
        return {"ok": True}

    t0 = time.monotonic()
    out = server._run_with_ceiling(stuck, (), {})
    release.set()
    assert time.monotonic() - t0 < 5
    assert out["ok"] is False and out["timed_out"] is True
    assert "stuck" in out["error"] and "resumes" in out["error"]


def test_fast_body_passes_value_and_context_through():
    server._CALL_TOOL.set("probe_tool")

    def body(a, b=0):
        return {"sum": a + b, "tool": server._CALL_TOOL.get()}

    assert server._run_with_ceiling(body, (2,), {"b": 3}) == {
        "sum": 5, "tool": "probe_tool"}


def test_body_exception_propagates():
    def boom():
        raise ValueError("no")

    with pytest.raises(ValueError):
        server._run_with_ceiling(boom, (), {})
