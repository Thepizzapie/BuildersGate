"""The page learns its edition from BGATE_EDITION, and only from there."""
from bgate_ui import app


def test_edition_rides_into_the_page(monkeypatch):
    monkeypatch.setenv("BGATE_EDITION", "builder")
    html = app._inject_settings("<html><head></head><body></body></html>")
    assert '"edition": "builder"' in html


def test_no_edition_when_unset(monkeypatch):
    monkeypatch.delenv("BGATE_EDITION", raising=False)
    html = app._inject_settings("<html><head></head><body></body></html>")
    assert '"edition"' not in html
