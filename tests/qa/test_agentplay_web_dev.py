"""A web bot run stops the dev server it started, and only that one.

Every playtest_agent_run on a web game used to leave its vite (a node
process) running after the run, one more per project per run.
"""
from __future__ import annotations

import sys
import types

import pytest

from bgate_adapters import web
from bgate_core.qa import agentplay


class _Relay:
    url = "http://127.0.0.1:1/agent"
    done = False
    exhausted = False
    count = 0


def _browser_fails(monkeypatch):
    """A playwright whose launch raises, so the run ends mid-way."""
    def sync_playwright():
        raise RuntimeError("no browser here")
    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)


@pytest.mark.parametrize("already, stopped", [(False, True), (True, False)])
def test_the_run_stops_only_a_server_it_started(tmp_path, monkeypatch,
                                                 already, stopped):
    stops = []
    start = {"ok": True, "url": "http://127.0.0.1:5173/"}
    if already:
        start["already_running"] = True
    monkeypatch.setattr(web, "dev_start", lambda d: start)
    monkeypatch.setattr(web, "dev_stop", lambda d: stops.append(d))
    _browser_fails(monkeypatch)

    with pytest.raises(RuntimeError, match="no browser"):
        agentplay._play_web(tmp_path, _Relay(), tmp_path, seconds=1,
                            every_s=0.5, scene="", headless=True)
    assert stops == ([str(tmp_path)] if stopped else [])


def test_a_server_that_would_not_start_is_not_stopped(tmp_path, monkeypatch):
    stops = []
    monkeypatch.setattr(web, "dev_start", lambda d: {"ok": False, "error": "x"})
    monkeypatch.setattr(web, "dev_stop", lambda d: stops.append(d))
    with pytest.raises(RuntimeError, match="would not start"):
        agentplay._play_web(tmp_path, _Relay(), tmp_path, seconds=1,
                            every_s=0.5, scene="", headless=True)
    assert stops == []
