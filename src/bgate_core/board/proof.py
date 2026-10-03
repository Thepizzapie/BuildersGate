"""What the HARNESS can show for a ticket - as opposed to what the agent said.

USER DIRECTIVE (2026-10-03): "agent statements are not evidence to me". The
sign-off panel showed the agent's result note and nothing else, so approving a
checkpoint meant trusting a paragraph. Everything here is recorded by
something other than the agent's own prose:

  commits   the harness's own commits naming the ticket, with per-file counts;
            the diff is one click away (:func:`diff`).
  tests     test-suite runs from the engine test history, which the RUNNER
            writes, stamped with the run that made them (the ticket's own
            agent, or the QA gate that verified it, or a human from the
            dashboard).
  checks    other check tools the run called, with the tool's own returned
            verdict read out of the stream log (the tool result, not the
            agent's summary of it).
  images    screenshots / renders those tools produced, servable by path.

The agent's note is still returned, labelled as a claim.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

#: Tools whose RESULT is a verdict worth showing. Matched on the tail of the
#: MCP name (mcp__builders-gate__web_test_run -> web_test_run).
CHECK_TOOLS = (
    "web_test_run", "godot_test_run", "unity_test_run", "engine_check",
    "godot_check_project", "engine_screenshot", "godot_screenshot",
    "playtest_check", "traversal_prove", "evidence_assert", "evidence_check_ui",
    "screen_audit", "room_audit", "godot_export_verify", "godot_scene_audit",
    "scale_check", "sprite_sheet_check", "sprite_family_check", "asset_verify",
)
_IMG = re.compile(r"[\w./\\:-]+\.(?:png|jpe?g|webp|gif)", re.I)


def _short(name: str) -> str:
    return name.rsplit("__", 1)[-1]


def _related(root, item_id: int) -> list[int]:
    """The ticket plus the QA gates that verified it."""
    from . import queue as _queue
    ids = [int(item_id)]
    for row in _queue.list_items(root):
        title = str(row.get("title") or "")
        if title.startswith("QA gate: verify") and re.search(
                rf"#{int(item_id)}(?!\d)", title):
            ids.append(int(row["id"]))
    return ids


def commits(root, item_id: int, limit: int = 20) -> list[dict]:
    from . import gitwork as _git
    ok, out, _ = _git._run(root, [
        "log", f"-{int(limit)}", "-E", "--all-match", "--grep=^bgate: item",
        f"--grep=#{int(item_id)}([^0-9]|$)", "--numstat",
        "--format=\x1e%H\x1f%cI\x1f%s"])
    if not ok:
        return []
    found = []
    for chunk in out.split("\x1e"):
        lines = chunk.strip("\n").splitlines()
        if not lines or "\x1f" not in lines[0]:
            continue
        sha, at, subject = lines[0].split("\x1f", 2)
        files = []
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) == 3:
                files.append({"path": parts[2],
                              "added": int(parts[0]) if parts[0].isdigit() else None,
                              "removed": int(parts[1]) if parts[1].isdigit() else None})
        found.append({"sha": sha, "at": at, "subject": subject, "files": files})
    return found


def human_actor(item_id: int) -> str:
    """The stamp on a suite run a human started from this ticket's proof."""
    return f"human:proof-{int(item_id)}"


def tests(root, ids: list[int], limit: int = 400) -> list[dict]:
    from bgate_core.runtime import enginetests as _et
    actors = {f"agent:item-{i}": i for i in ids}
    actors[human_actor(ids[0])] = ids[0]
    out = []
    for row in _et.history(root, limit=limit):
        by = str(row.get("by") or "")
        if by not in actors:
            continue
        out.append({"at": row.get("at"), "by": by, "item": actors.get(by),
                    "ok": bool(row.get("ok")), "passed": row.get("passed"),
                    "failures": row.get("failures"),
                    "scripts_run": row.get("scripts_run"),
                    "scripts_failed": row.get("scripts_failed"),
                    "seconds": row.get("seconds"),
                    "failing": [s.get("script") for s in row.get("scripts") or []
                                if not s.get("ok")],
                    "error": row.get("error") or ""})
    return out


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(c.get("text") or "") for c in content
                       if isinstance(c, dict))
    return ""


def _verdict(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        return {"ok": None, "summary": text[:240]}
    if not isinstance(data, dict):
        return {"ok": None, "summary": text[:240]}
    keys = ("ok", "passed", "failed", "assertions_passed", "assertions_failed",
            "verdict", "error", "reason", "scripts_failed")
    return {"ok": data.get("ok") if isinstance(data.get("ok"), bool) else None,
            "summary": ", ".join(f"{k}={data[k]}" for k in keys
                                 if k in data and data[k] not in ("", None))[:240]}


def checks(root, ids: list[int], max_bytes: int = 8 << 20) -> tuple[list[dict], list[str]]:
    """Check-tool calls and their RETURNED verdicts, from the stream logs."""
    from . import agentlog as _agentlog
    found: list[dict] = []
    images: list[str] = []
    base = Path(root).resolve()
    for item in ids:
        path = _agentlog.log_path(root, item)
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                fh.seek(max(0, size - max_bytes))
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        pending: dict[str, dict] = {}
        for line in raw.splitlines():
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            for b in _agentlog.blocks(ev):
                if b.get("type") == "tool_use" and _short(str(b.get("name") or "")) in CHECK_TOOLS:
                    pending[str(b.get("id"))] = {"item": item,
                                                 "tool": _short(str(b.get("name"))),
                                                 "input": b.get("input") or {}}
                elif b.get("type") == "tool_result" and str(b.get("tool_use_id")) in pending:
                    call = pending.pop(str(b.get("tool_use_id")))
                    text = _result_text(b)
                    call.update(_verdict(text))
                    if b.get("is_error"):
                        call["ok"] = False
                    shots = []
                    for hit in _IMG.findall(text):
                        rel = _inside(base, hit)
                        if rel and rel not in images:
                            images.append(rel)
                            shots.append(rel)
                    call["images"] = shots
                    call.pop("input", None)
                    found.append(call)
    return found, images


def _inside(base: Path, raw: str) -> str:
    p = Path(raw.replace("\\\\", "\\"))
    p = p if p.is_absolute() else base / p
    try:
        p = p.resolve()
        rel = p.relative_to(base)
    except (OSError, ValueError):
        return ""
    return rel.as_posix() if p.is_file() else ""


def proof(root, item_id: int) -> dict:
    from . import queue as _queue
    item = _queue.get(root, int(item_id))
    ids = _related(root, int(item_id))
    found_checks, images = checks(root, ids)
    shots = [s for s in harness_shots(root, int(item_id))
             if (Path(root) / s.get("path", "")).is_file()]
    images = [s["path"] for s in shots] + [i for i in images
                                           if i not in {s["path"] for s in shots}]
    return {"item_id": int(item_id), "related": ids, "shots": shots,
            "visible": str(item.get("seat") or "") in VISIBLE_SEATS,
            "commits": commits(root, int(item_id)),
            "tests": tests(root, ids),
            "checks": found_checks, "images": images,
            "claim": str(item.get("result") or "")}


def diff(root, sha: str, max_chars: int = 200_000) -> str:
    from . import gitwork as _git
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha or ""):
        raise ValueError("not a commit id")
    ok, out, err = _git._run(root, ["show", "--stat", "--patch", "--format=%H %s", sha])
    if not ok:
        raise LookupError(err or "unknown commit")
    return out[:max_chars]


def image_path(root, rel: str) -> Path:
    """A project file to serve, refused outside the project or not an image."""
    base = Path(root).resolve()
    p = (base / rel).resolve()
    if os.path.commonpath([str(base), str(p)]) != str(base):
        raise ValueError("outside the project")
    if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif") or not p.is_file():
        raise LookupError("no such image")
    return p


#: Seats whose work shows up on screen; their finished tickets get a frame.
VISIBLE_SEATS = frozenset({"gameplay", "level", "ui", "art", "cinematic"})


def _shots_file(root, item_id: int) -> Path:
    return Path(root) / ".bgate" / "proof" / f"item-{int(item_id)}.json"


def harness_shots(root, item_id: int) -> list[dict]:
    try:
        got = json.loads(_shots_file(root, item_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in got if isinstance(s, dict)] if isinstance(got, list) else []


def capture(root, item_id: int, by: str = "harness") -> dict:
    """Photograph the running game NOW and file the frame against a ticket.

    Taken by the harness, not by the agent: it boots the project's own dev
    server and screenshots the page, so the picture is of what is actually
    on disk. Web projects only for now; anything else says so."""
    import time

    from bgate_adapters import web as _web
    from bgate_core.runtime import enginetests as _et

    base = _et._web_dir(root)
    if base is None:
        return {"ok": False, "error": "harness screenshots cover web projects only so far"}
    started = _web.dev_start(str(base), timeout=60)
    if not started.get("ok"):
        return {"ok": False, "error": "dev server: " + str(started.get("error") or "")}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(root) / ".bgate" / "proof" / f"item-{int(item_id)}-{stamp}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    got = _web.screenshot(str(started.get("url") or ""), str(out), at=2.5)
    if not got.get("ok"):
        return {"ok": False, "error": str(got.get("error") or "screenshot failed"),
                "console": got.get("console")}
    rel = Path(got.get("path") or out).resolve().relative_to(Path(root).resolve()).as_posix()
    shots = harness_shots(root, item_id)
    shots.append({"path": rel, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": by,
                  "errors": [str(e)[:200] for e in (got.get("console_errors")
                                                   or got.get("errors") or [])][:5]})
    _shots_file(root, item_id).write_text(json.dumps(shots), encoding="utf-8")
    return {"ok": True, "path": rel}
