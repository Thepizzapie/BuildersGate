"""The daemon-pump shape the dashboard's background loops all share.

Three loops run beside the server — auto-deploy, the follow-up router, the steer
pump — and each one had written out the same twenty lines: a ``_started`` set
keyed by root, a lock around it, a ``while True: sleep; try: work; except: pass``
body, an idempotent ``start`` that spawns a named daemon thread, and a ``reset``
for the tests. Three copies of a concurrency primitive is three places for the
invariants to drift, and two of them had already drifted in small ways (only one
honoured its env kill switch in ``reset``; only one exposed whether it was
actually running).

The invariants, stated once:

  * PER PROJECT, NOT PER PROCESS. The active project can change under a
    long-lived server (``BGATE_ROOT``, ``bgate use``), and a single latched flag
    means the loop keeps pumping the project the user has already left.
  * IDEMPOTENT. ``start`` twice on one root is one thread. The dashboard calls it
    from a lifespan handler that a reload can run again.
  * FAIL-SAFE. A raise out of the work function is swallowed. These loops exist
    beside the server, not under it — a wedged tick must never be able to take
    the dashboard down with it, and every one of them logs its own failures.
  * SLEEP FIRST. The first tick lands one interval after boot, which is what
    keeps ``start`` cheap enough to call from startup.

What is deliberately NOT here: the work itself, the per-project state each loop
keeps (cooldowns, cursors), and the settings each one reads every tick. Those are
what make the three loops different from each other.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

# Values of an env kill switch that mean "do not start".
_OFF = ("0", "false", "off")


class Pump:
    """A named daemon loop that runs ``work(root)`` every ``poll_s`` seconds.

    ``poll_s`` may be a number or a zero-argument callable, and the loop asks for
    it again before every sleep. Each owning module passes ``lambda: POLL_S`` so
    its interval stays a module constant that a test can shorten with
    ``monkeypatch.setattr(mod, "POLL_S", 0.01)`` — reading it once at
    construction froze it at the production value and left those tests waiting
    the real interval for a tick that had to arrive inside five seconds.

    ``env_var`` is an optional kill switch read ONCE, at ``start`` — it decides
    whether the thread exists at all, so unlike the per-tick settings it cannot
    be flipped in the browser. Absent or unset means on.
    """

    def __init__(self, name: str, poll_s: float | Callable[[], float],
                 work: Callable[[str], object],
                 *, env_var: Optional[str] = None) -> None:
        self.name = name
        self._poll_s = poll_s
        self._work = work
        self._env_var = env_var
        self._started: set[str] = set()
        self._lock = threading.Lock()

    # -- state ---------------------------------------------------------------

    @property
    def poll_s(self) -> float:
        """The interval to use for the NEXT sleep."""
        return float(self._poll_s() if callable(self._poll_s) else self._poll_s)

    def running(self, root: str | os.PathLike[str]) -> bool:
        """Is a loop live for this project in this process?"""
        with self._lock:
            return str(root) in self._started

    def disabled(self) -> bool:
        """Is the env kill switch set? (False when there is no switch.)"""
        if not self._env_var:
            return False
        return os.environ.get(self._env_var, "1").strip().lower() in _OFF

    # -- lifecycle -----------------------------------------------------------

    def start(self, root: str | os.PathLike[str]) -> bool:
        """Idempotently start the loop for this project. False = kill switch."""
        if self.disabled():
            return False
        key = str(root)
        with self._lock:
            if key in self._started:
                return True
            self._started.add(key)
        threading.Thread(target=self._loop, args=(key,), daemon=True,
                         name=self.name).start()
        return True

    def reset(self, root: Optional[str | os.PathLike[str]] = None) -> None:
        """Forget that the loop started here. Tests use this; nothing else should.

        It does NOT stop the thread — a daemon thread cannot be joined out of a
        test without a shutdown protocol none of these loops have. It clears the
        latch so the next ``start`` spawns a fresh one, which is what a test that
        just built a new project root needs.
        """
        with self._lock:
            if root is None:
                self._started.clear()
            else:
                self._started.discard(str(root))

    # -- the loop ------------------------------------------------------------

    def _loop(self, root: str) -> None:
        while True:
            time.sleep(self.poll_s)
            try:
                self._work(root)
            except Exception:
                # Fail-safe: this loop must never take the dashboard down.
                pass
