"""PreToolUse hook — the teeth on seat lanes and asset locks.

Claude Code pipes the pending tool call as JSON on stdin. Exit 2 blocks the call
(stderr is shown to the model); exit 0 allows it. This hook asks the same oracle
the tools expose (seats.can_write): lane check AND lock check, both gates.

Inert unless BOTH hold:
  * BGATE_SEAT is set in the session's environment (the identity to enforce)
  * the file being written lives under a .bgate project

FAIL-SAFE RULE: this must NEVER raise or exit nonzero by accident — a crashing
hook blocks every write in the session. Any unexpected error means exit 0.
"""
from __future__ import annotations

import json
import os
import sys

# Tool → the input key that carries the file path.
_PATH_KEYS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

ALLOW, BLOCK = 0, 2


def decide(payload: dict, seat: str) -> tuple[int, str]:
    """Pure decision, separated from stdio so tests can hit it directly."""
    tool = payload.get("tool_name", "")
    key = _PATH_KEYS.get(tool)
    if key is None:
        return ALLOW, ""  # not a file write — not this hook's business

    target = (payload.get("tool_input") or {}).get(key)
    if not target:
        return ALLOW, ""

    # Lazy imports keep the hook fast on the (common) inert path.
    from pathlib import Path

    from bgate_core import db, seats

    # A relative file_path is relative to the SESSION's cwd (in the payload),
    # never to this hook process's cwd — resolving against the wrong one lets
    # relative writes silently bypass enforcement.
    session_cwd = Path(payload.get("cwd") or os.getcwd())
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = session_cwd / target_path
    target_path = target_path.resolve()

    root = db.resolve_root(target_path.parent)
    if root is None:
        return ALLOW, ""  # not a Builders Gate project — stay out of the way

    try:
        rel = target_path.relative_to(Path(root).resolve())
    except ValueError:
        return ALLOW, ""  # writing outside the project — not ours to police

    verdict = seats.can_write(root, seat, str(rel))
    if verdict["allowed"]:
        return ALLOW, ""
    return BLOCK, (
        f"[builders-gate] seat {seat!r} may not write {verdict['path']}: "
        f"{verdict['reason']}. Use seat_can_write to find your lanes, or "
        "asset_lock if you need to claim a binary."
    )


def main() -> int:
    try:
        seat = os.environ.get("BGATE_SEAT", "").strip()
        if not seat:
            return ALLOW  # no adopted identity, nothing to enforce
        payload = json.loads(sys.stdin.read() or "{}")
        code, message = decide(payload, seat)
        if message:
            print(message, file=sys.stderr)
        return code
    except Exception:
        return ALLOW  # fail-safe: a broken hook must never dam the session


if __name__ == "__main__":
    sys.exit(main())
