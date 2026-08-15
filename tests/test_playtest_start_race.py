"""Two record clicks must not become two recording sessions.

THE BUG. start() used to SELECT for a live session, raise if it found one, and
INSERT afterwards — three statements with two gaps. A second request arriving in
either gap also saw no live session, so both inserted, and the project ended up
with two rows marked `recording`: two ffmpeg captures fighting over the same
window, and a panel that could only ever stop one of them.

It took no special timing to hit. The record button gave no feedback for the
several seconds start() spends in preflight and export, so clicking it twice was
the natural response to "did that work?" — the reported symptom was "sometimes
the game resets and makes it start 2 recording sessions".

The fix is one atomic INSERT ... SELECT ... WHERE NOT EXISTS, which is what
these tests hold in place. The frontend also guards the second click, but a
guard in the page is a convenience; only the database can actually promise it,
since the MCP tools reach start() without going through any page at all.
"""
from __future__ import annotations

import threading

import pytest

from bgate_core import db, iterations, playtest


@pytest.fixture()
def game(tmp_path):
    """A project with the schema in place and nothing recording."""
    root = tmp_path / "game"
    root.mkdir()
    playtest.db.connect(root)          # creates .bgate/game.db and migrates
    return root


def _no_git(monkeypatch):
    """Stub the git calls, not the iteration.

    iterations.create() shells out to git four times and dominated the runtime
    (30s for two tests). Faking its RETURN value instead is wrong: the session
    row carries iteration_id as a foreign key, so an invented id fails the
    constraint and the test stops testing what it claims to.
    """
    import subprocess
    monkeypatch.setattr(
        iterations, "_run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, "", ""))


def _recording(root) -> list[int]:
    with db.tx(root) as conn:
        return [r[0] for r in conn.execute(
            "SELECT id FROM playtest_session WHERE status = 'recording'"
        ).fetchall()]


class TestOnlyOneRecordingSession:
    def test_second_start_is_refused_while_one_is_live(self, game, monkeypatch):
        """The plain case: start twice in a row, get one session and an error."""
        monkeypatch.setattr(playtest, "_build_identity", lambda root: "test")
        _no_git(monkeypatch)

        # Stop before the recorder is touched — the row is what is under test.
        monkeypatch.setattr(playtest, "_session_dir",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here")))

        with pytest.raises(RuntimeError):
            playtest.start(game, "first")
        assert len(_recording(game)) == 1, "the first session must exist"

        with pytest.raises(RuntimeError, match="already recording"):
            playtest.start(game, "second")

        assert len(_recording(game)) == 1, "a second row was inserted anyway"

    def test_concurrent_starts_produce_exactly_one(self, game, monkeypatch):
        """THE RACE ITSELF. Two threads into start() at once."""
        monkeypatch.setattr(playtest, "_build_identity", lambda root: "test")
        _no_git(monkeypatch)
        monkeypatch.setattr(playtest, "_session_dir",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here")))

        ready = threading.Barrier(2)
        errors: list[BaseException] = []

        def race(n: int) -> None:
            ready.wait()                      # both arrive together
            try:
                playtest.start(game, f"session {n}")
            except BaseException as exc:      # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=race, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        live = _recording(game)
        assert len(live) == 1, f"{len(live)} sessions ended up recording: {live}"
        assert len(errors) == 2, "both calls stop before the recorder either way"
