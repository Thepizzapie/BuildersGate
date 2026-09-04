"""Claude Code status-line adapter for the opt-in usage bridge."""
from __future__ import annotations

import sys

from bgate_ui.agents import claudeusage


def main() -> int:
    raw = sys.stdin.read()
    claudeusage.capture(raw)
    previous = claudeusage.run_previous(raw)
    if previous:
        print(previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
