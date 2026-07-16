from __future__ import annotations

import pytest

from bgate_core import db, project


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def root(tmp_path):
    """A fresh initialized project per test. Connections are per-path, so the
    cache is dropped afterward to keep tmp dirs from leaking handles on Windows."""
    project.init(tmp_path, "Test Game", pitch="a game for tests")
    yield tmp_path
    db.close_all()
