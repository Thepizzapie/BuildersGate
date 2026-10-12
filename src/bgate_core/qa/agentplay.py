"""A decision model plays the game: the relay, the launch and the read-back.

The game side is ``addons/bgate/bgate_agent.gd`` (autoload ``BGateAgent``):
every few physics frames it POSTs ``{state, options, goal}`` to the loopback
relay started here, and holds whatever option comes back. The relay asks
:mod:`bgate_adapters.jev` (or a seeded random policy, which needs no key and
proves the wiring) and logs every decision.

WHY A RELAY AND NOT A KEY IN THE GAME. The game would need the API key to call
the model itself, and a key in a game process is a key in its export.

WHY THE HISTORY LIVES HERE. Jev is stateless: one state in, one answer out. The
last few choices go into the state text so "I have been jumping for a second"
is something it can see.

Output: ``.bgate/agentplay/<stamp>/`` holding ``decisions.jsonl``,
``telemetry.jsonl``, ``game.log`` and ``summary.json``.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import threading
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from ..runtime.proc import kill_tree, popen as _popen

AGENT_AUTOLOAD = ("BGateAgent", "res://addons/bgate/bgate_agent.gd")
TEMPLATE = (Path(__file__).resolve().parents[2] / "templates" / "godot"
            / "shared" / "addons" / "bgate" / "bgate_agent.gd")
#: "hold:<option>" always picks that option when it is offered (else the
#: first offered one) - the do-nothing-clever baseline the feel check races.
POLICIES = ("jev", "random", "hold")
#: Three, not more: measured on HARDCOVER, a longer history let one early
#: choice (dash_left) reinforce itself into 90% of 511 decisions.
HISTORY = 3
STATE_MAX_CHARS = 6000
_NOISE = {"fps", "session_open", "session_close", "autoquit", "agent_decision"}


class Relay:
    """The loopback HTTP endpoint the in-game agent asks for each move."""

    def __init__(self, root: Any, out_dir: Path, *, goal: str, policy: str,
                 model: str = "", seed: int = 0, max_decisions: int = 0,
                 max_usd: float = 0.0) -> None:
        if policy.split(":", 1)[0] not in POLICIES:
            raise ValueError(f"policy must be one of {POLICIES} "
                             "(hold as 'hold:<option>')")
        self.root, self.goal, self.policy, self.model = root, goal, policy, model
        self.log_path = out_dir / "decisions.jsonl"
        self._log = open(self.log_path, "a", encoding="utf-8")
        self._recent: deque = deque(maxlen=HISTORY)
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self.count = 0
        self.errors: list[str] = []
        self.usd = 0.0
        # NO RUNAWAY CALLS. Either cap reached, every later request is refused
        # without touching the model and run() kills the game. 0 = no cap.
        self.max_decisions = int(max_decisions or 0)
        self.max_usd = float(max_usd or 0.0)
        self.exhausted = ""
        self._t0 = time.time()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:                           # noqa: N802
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(size) or b"{}")
                    out = relay.decide(body)
                    code = 200
                except Exception as exc:                         # noqa: BLE001
                    out, code = {"error": str(exc)}, 500
                    with relay._lock:
                        relay.errors.append(str(exc))
                raw = json.dumps(out).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_a: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/decide"
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self._thread.start()

    def decide(self, body: dict) -> dict:
        with self._lock:
            if (not self.exhausted and self.max_decisions
                    and self.count >= self.max_decisions):
                self.exhausted = f"decision cap {self.max_decisions} reached"
            if not self.exhausted and self.max_usd and self.usd >= self.max_usd:
                self.exhausted = f"spend cap ${self.max_usd:.2f} reached"
            if self.exhausted:
                raise RuntimeError(self.exhausted)
        options = body.get("options") or {}
        if not isinstance(options, dict) or not options:
            raise ValueError("no options sent")
        state = body.get("state") or ""
        if not isinstance(state, str):
            state = render(state)
        goal = str(body.get("goal") or self.goal)
        hold = float(body.get("hold_s") or 0.1)
        with self._lock:
            recent = list(self._recent)
        text = (f"GOAL: {goal}\n"
                f"RECENT CHOICES (oldest first): {', '.join(recent) or 'none'}\n"
                f"STATE: {state[:STATE_MAX_CHARS]}")
        t0 = time.perf_counter()
        if self.policy.startswith("hold"):
            want = self.policy.split(":", 1)[1] if ":" in self.policy else ""
            choice = want if want in options else sorted(options)[0]
            answer = {"choice": choice, "confidence": 0.0, "probabilities": {}}
            usd, model = 0.0, self.policy
        elif self.policy == "random" or len(options) == 1:
            choice = self._rng.choice(sorted(options))
            answer = {"choice": choice, "confidence": 0.0, "probabilities": {}}
            usd, model = 0.0, "random"
        else:
            from bgate_adapters import jev
            got = jev.decide(text, {"action": {
                "type": "choice",
                "instructions": (
                    "You are playing this video game. Pick the input to hold "
                    f"for the next {hold:.2f} seconds that best advances the "
                    "GOAL: avoid taking damage first, then make progress."),
                "criteria": {str(k): (str(v) or None)
                             for k, v in options.items()}}},
                root=self.root, model=self.model)
            answer, usd, model = got["answers"].get("action", {}), \
                float(got.get("usd") or 0.0), got.get("model")
            choice = str(answer.get("choice") or "")
            if choice not in options:
                raise ValueError(f"model chose {choice!r}, not an option")
        ms = round((time.perf_counter() - t0) * 1000)
        with self._lock:
            self._recent.append(choice)
            self.count += 1
            self.usd += usd
            self._log.write(json.dumps({
                "t": round(time.time() - self._t0, 3), "ts": time.time(),
                "progress": body.get("progress"), "danger": body.get("danger"),
                "frame": body.get("frame"), "choice": choice,
                "confidence": answer.get("confidence"),
                "probabilities": answer.get("probabilities"),
                "ms": ms, "usd": usd, "model": model,
                "options": sorted(options), "state": state[:2000]}) + "\n")
            self._log.flush()
        return {"choice": choice, "confidence": answer.get("confidence", 0.0),
                "probabilities": answer.get("probabilities", {}), "ms": ms}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._log.close()


def render(value: Any, indent: str = "") -> str:
    """A structured state as indented ``key: value`` lines, not JSON.

    MEASURED: the same HARDCOVER frames sent as raw JSON got mean confidence
    0.32 and a 90% dash_left collapse; as plain lines with the hit prediction
    precomputed, 0.87-1.0 and the right pick. Jev judges text; give it text."""
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{indent}{k}:")
                out.append(render(v, indent + "  "))
            else:
                out.append(f"{indent}{k}: {v}")
        return "\n".join(out)
    if isinstance(value, list):
        out = []
        for v in value:
            if isinstance(v, dict):
                out.append(indent + "- " + ", ".join(
                    f"{k} {w}" for k, w in v.items()))
            else:
                out.append(f"{indent}- {v}")
        return "\n".join(out)
    return f"{indent}{value}"


def install(root: Any) -> dict:
    """Put the agent script and its autoload line in the Godot project.

    Additive and re-runnable, the same way adopt installs telemetry: the
    script is refreshed from the template, the autoload merged in beside the
    game's own."""
    from ..store.adopt import _merge_autoloads
    from .playtest import _godot_project_dir

    game = _godot_project_dir(root)
    config = game / "project.godot"
    if not config.is_file():
        raise RuntimeError(f"no project.godot at {game}")
    dest = game / "addons" / "bgate" / "bgate_agent.gd"
    dest.parent.mkdir(parents=True, exist_ok=True)
    copied = (not dest.is_file()
              or dest.read_bytes() != TEMPLATE.read_bytes())
    if copied:
        shutil.copyfile(TEMPLATE, dest)
    merged = _merge_autoloads(config, (AGENT_AUTOLOAD,))
    return {"script": str(dest), "script_updated": copied,
            "autoload": merged.get("action")}


def run(root: Any, *, goal: str, seconds: float = 60.0, decide_every: int = 6,
        lockstep: bool = True, policy: str = "jev", model: str = "",
        headless: bool = False, scene: str = "", seed: int = 0,
        max_usd: float = 0.10, max_decisions: int = 0) -> dict:
    """Launch the game with the agent on, let it play, return what happened.

    ``seconds`` is GAME time (BGATE_AUTOQUIT counts frames the tree ran, and
    lockstep pauses the tree while deciding); the wall clock is capped at
    ten times that plus a minute.

    CAPS, so a stuck loop cannot spend: ``max_decisions`` defaults to one and a
    half times the decisions ``seconds`` can need (60 physics ticks a second /
    ``decide_every``), ``max_usd`` to ten cents (a measured 45 s run is about
    1.5 cents). Hitting either kills the game and says which in the summary."""
    from bgate_adapters import godot
    from .playtest import _godot_project_dir

    if policy == "jev":
        from bgate_adapters import jev
        ok = jev.available(root)
        if not ok["available"]:
            raise RuntimeError(ok["reason"])
    installed = install(root)
    game = _godot_project_dir(root)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(root) / ".bgate" / "agentplay" / stamp
    out.mkdir(parents=True, exist_ok=True)
    if not max_decisions:
        max_decisions = int(float(seconds) * 60 / max(1, int(decide_every)) * 1.5) + 20
    relay = Relay(root, out, goal=goal, policy=policy, model=model, seed=seed,
                  max_decisions=max_decisions, max_usd=max_usd)
    telemetry = out / "telemetry.jsonl"
    args = [godot.find_godot(), "--path", str(game)]
    if headless:
        args.insert(1, "--headless")
    if scene:
        args.append(scene if scene.startswith("res://") else f"res://{scene}")
    env = os.environ.copy()
    env.update({"BGATE_AGENT_URL": relay.url,
                "BGATE_AGENT_EVERY": str(max(1, int(decide_every))),
                "BGATE_AGENT_LOCKSTEP": "1" if lockstep else "0",
                "BGATE_TELEMETRY": str(telemetry),
                "BGATE_AUTOQUIT": str(float(seconds))})
    for key in ("TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY"):
        env.pop(key, None)   # the game never needs it; the relay holds it
    wall_cap = float(seconds) * 10 + 60
    t0 = time.time()
    timed_out = False
    with open(out / "game.log", "w", encoding="utf-8") as log:
        proc = _popen(args, cwd=str(game), env=env, stdin=subprocess.DEVNULL,
                      stdout=log, stderr=subprocess.STDOUT)
        while True:
            try:
                proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if relay.exhausted:
                    kill_tree(proc)
                    break
                if time.time() - t0 > wall_cap:
                    timed_out = True
                    kill_tree(proc)
                    break
    relay.close()
    summary = summarize(out)
    summary.update({
        "ok": relay.count > 0 and not timed_out and not relay.exhausted,
        "policy": policy, "goal": goal, "game_seconds": seconds,
        "wall_seconds": round(time.time() - t0, 1), "timed_out": timed_out,
        "exit_code": proc.returncode, "relay_errors": relay.errors[:5],
        "capped": relay.exhausted, "max_usd": max_usd,
        "max_decisions": max_decisions,
        "usd": round(relay.usd, 6), "install": installed, "dir": str(out)})
    (out / "summary.json").write_text(json.dumps(summary, indent=2),
                                      encoding="utf-8")
    return summary


def summarize(out: Path) -> dict:
    """What the bot did and what the game reported while it did it."""
    decisions = _jsonl(out / "decisions.jsonl")
    events = _jsonl(out / "telemetry.jsonl")
    ms = sorted(d.get("ms") or 0 for d in decisions)
    conf = [d["confidence"] for d in decisions
            if isinstance(d.get("confidence"), (int, float))]
    kinds = Counter(e.get("kind") for e in events
                    if e.get("kind") not in _NOISE)
    tail = [{"t": e.get("t"), "kind": e.get("kind"), "data": e.get("data")}
            for e in events if e.get("kind") not in _NOISE][-15:]
    errors = []
    log = out / "game.log"
    if log.is_file():
        errors = [ln.strip() for ln in log.read_text(
            encoding="utf-8", errors="replace").splitlines()
            if "ERROR" in ln][:10]
    return {
        "decisions": len(decisions),
        "choices": dict(Counter(d.get("choice") for d in decisions)
                        .most_common(12)),
        "mean_confidence": round(sum(conf) / len(conf), 3) if conf else None,
        "ms_median": ms[len(ms) // 2] if ms else None,
        "ms_p95": ms[int(len(ms) * 0.95)] if ms else None,
        "events": dict(kinds.most_common(20)), "last_events": tail,
        "game_errors": errors}


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
