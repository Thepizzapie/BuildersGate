"""The steer inbox — a message channel to a running agent that crosses processes.

Steering works by writing a user turn into a live claude session's stdin, and
that pipe exists only inside the dashboard process that spawned it
(``bgate_ui.dispatch._live``). Anything else that wants to steer — the MCP
server, a CLI command, and above all the DIRECTOR agent, which runs as its own
claude process — has no way to reach it.

So the message is written to disk instead, and the dashboard delivers it. One
small file per message under ``.bgate/steer/``, drained by a pump thread in the
server. The consequences of that shape are the point:

  * The director can steer its own workers. It is the seat that decides who does
    what; being unable to say "not like that, use the pinned ref" while an agent
    is mid-run made it a dispatcher, not a director.
  * A message written while no dashboard is running is not lost — it waits.
  * A message for an item with no live agent is not silently dropped either: the
    pump answers it (see bgate_ui.steerpump) rather than deleting it blind.

Deliberately files rather than a table: the writer may be a process that only
has the project path, the payload is tiny, and a crashed reader must leave the
message where it was rather than half-consumed inside a transaction.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

MAX_TEXT = 2000
# A message nobody has collected is stale rather than pending: an agent that
# finished twenty minutes ago is not going to read it, and delivering it to the
# NEXT run of the same item would steer a fresh agent with an old complaint.
STALE_S = 15 * 60


def box(root: str | os.PathLike[str]) -> Path:
    return Path(root) / ".bgate" / "steer"


def post(root: str | os.PathLike[str], item_id: int, text: str, *,
         by: str = "", note: str = "") -> dict:
    """Leave a message for whoever is running ``item_id``."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("a steer needs something to say")
    if len(text) > MAX_TEXT:
        raise ValueError(f"a steer is capped at {MAX_TEXT} characters")
    directory = box(root)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": uuid.uuid4().hex[:12],
        "item_id": int(item_id),
        "text": text,
        "by": by or "",
        "note": note or "",
        "at": time.time(),
    }
    # Write beside, then rename: a reader must never see half a message.
    tmp = directory / f".{payload['id']}.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(directory / f"{int(payload['at'] * 1000)}-{payload['id']}.json")
    return payload


def pending(root: str | os.PathLike[str],
            item_id: int | None = None) -> list[dict]:
    """Undelivered messages, oldest first. Reads only — nothing is consumed."""
    directory = box(root)
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if item_id is not None and int(data.get("item_id", 0)) != int(item_id):
            continue
        data["_path"] = str(path)
        out.append(data)
    return out


def take(root: str | os.PathLike[str]) -> list[dict]:
    """Claim every pending message. Stale ones are dropped, not delivered."""
    now = time.time()
    claimed: list[dict] = []
    for data in pending(root):
        path = Path(data.pop("_path"))
        try:
            path.unlink()
        except OSError:
            continue  # somebody else got it first
        if now - float(data.get("at") or 0) > STALE_S:
            data["stale"] = True
        claimed.append(data)
    return claimed
