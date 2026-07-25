"""PreToolUse hook — the teeth on seat lanes and asset locks.

Claude Code pipes the pending tool call as JSON on stdin. Exit 2 blocks the call
(stderr is shown to the model); exit 0 allows it. This hook asks the same oracle
the tools expose (seats.can_write): lane check AND lock check, both gates.

It also TAKES an advisory lease on every path it lets through, keyed to this
run's BGATE_LOCK_OWNER. Binaries lock because they cannot merge; two agents in
overlapping lanes editing one .gd is last-write-wins with no warning at all. The
lease turns that into a block that names the item already in the file.

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

# Long enough to cover a working stretch between writes, short enough that a
# killed agent's claim clears on its own. Refreshed on every write it makes.
DEFAULT_LEASE_S = 900


def decide(payload: dict, seat: str, owner: str = "") -> tuple[int, str]:
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

    from bgate_core import assets, db, seats

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

    verdict = seats.can_write(root, seat, str(rel), owner=owner)
    if verdict["allowed"]:
        _hold(assets, root, str(rel), seat, owner)
        return ALLOW, ""

    blocker = verdict.get("owner") or ""
    return BLOCK, (
        f"[builders-gate] seat {seat!r} may not write {verdict['path']}: "
        f"{verdict['reason']}."
        + (f" {blocker} is in that file — coordinate with it (seat_post_note) or "
           "work on something else; do not edit around it."
           if blocker else
           " Use seat_can_write to find your lanes, or asset_lock if you need to "
           "claim a binary.")
    )


def _hold(assets, root, rel: str, seat: str, owner: str) -> None:
    """Claim the path for this run so the next agent's write is a block, not a
    silent overwrite. Best effort by design — a lease we could not take must
    never stop a write the oracle already allowed."""
    if not owner:
        return  # no execution identity to attribute the lease to
    try:
        lease_s = int(os.environ.get("BGATE_LEASE_S", "") or DEFAULT_LEASE_S)
    except ValueError:
        lease_s = DEFAULT_LEASE_S
    try:
        assets.acquire_path_lease(root, rel, seat, owner, lease_s=lease_s)
    except Exception:
        pass


def main() -> int:
    try:
        seat = os.environ.get("BGATE_SEAT", "").strip()
        if not seat:
            return ALLOW  # no adopted identity, nothing to enforce
        owner = os.environ.get("BGATE_LOCK_OWNER", "").strip()
        payload = json.loads(sys.stdin.read() or "{}")
        code, message = decide(payload, seat, owner)
        if message:
            print(message, file=sys.stderr)
        return code
    except Exception:
        return ALLOW  # fail-safe: a broken hook must never dam the session


if __name__ == "__main__":
    sys.exit(main())
