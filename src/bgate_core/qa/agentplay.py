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
        # A web game posts its telemetry here and says when its run is over
        # (src/bgate/agent.ts); a Godot game writes its own file instead.
        self.telemetry_path = out_dir / "telemetry.jsonl"
        self.done = False
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def _cors(self) -> None:
                # The browser page is served by the game's dev server, a
                # different origin than this loopback relay.
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

            def do_OPTIONS(self) -> None:                        # noqa: N802
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_POST(self) -> None:                           # noqa: N802
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(size) or b"{}")
                    path = self.path.split("?", 1)[0]
                    if path == "/events":
                        out = relay.events(body.get("events") or [])
                    elif path == "/done":
                        relay.done = True
                        out = {"ok": True}
                    else:
                        out = relay.decide(body)
                    code = 200
                except Exception as exc:                         # noqa: BLE001
                    out, code = {"error": str(exc)}, 500
                    with relay._lock:
                        relay.errors.append(str(exc))
                raw = json.dumps(out).encode("utf-8")
                self.send_response(code)
                self._cors()
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

    def events(self, events: list) -> dict:
        """Telemetry a web game forwards, in the telemetry.jsonl shape."""
        with self._lock, open(self.telemetry_path, "a", encoding="utf-8") as f:
            for e in events[:500]:
                if isinstance(e, dict) and e.get("kind"):
                    f.write(json.dumps(e) + "\n")
        return {"ok": True}

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


def _lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines()]


def _place_script(template: Path, dest: Path, *, refresh: bool) -> dict:
    """Put a template script where the game expects it, without overwriting
    the game's own copy in silence.

    MEASURED on Turf War (2026-10-17): the game had extended ``agent.ts``
    with a ``run`` hook, the one way a command-driven game hands the bot a
    plan. Every feel check copied the template over it because the bytes
    differed, deleted the hook it depended on, and reported UNMEASURED. So:
    a missing file is written; an identical one (line endings and trailing
    whitespace aside) is left; a DIFFERENT one is left as it is and the
    result says so, with ``script_extends_template`` telling whether the
    game's copy still carries every template line. ``refresh=True`` is the
    explicit way to replace it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    want = template.read_text(encoding="utf-8")
    out: dict = {"script": str(dest), "script_updated": False,
                 "script_differs": False}
    if not dest.is_file():
        shutil.copyfile(template, dest)
        return {**out, "script_updated": True}
    have = dest.read_text(encoding="utf-8", errors="replace")
    if _lines(have) == _lines(want):
        return out
    if refresh:
        shutil.copyfile(template, dest)
        return {**out, "script_updated": True,
                "note": f"{dest.name} differed from the template and was "
                        "replaced (refresh=True)"}
    extends = set(ln.strip() for ln in _lines(want) if ln.strip()) \
        <= set(ln.strip() for ln in _lines(have) if ln.strip())
    why = ("it carries every line the template has plus the game's own"
           if extends else
           f"it lacks lines the template has; diff it against {template}")
    return {**out, "script_differs": True, "script_extends_template": extends,
            "note": f"{dest.name} differs from the template and was LEFT AS "
                    f"IS ({why}); pass refresh=True to replace it"}


def install(root: Any, *, refresh: bool = False) -> dict:
    """Put the agent script and its autoload line in the Godot project.

    Additive and re-runnable, the same way adopt installs telemetry: the
    script is written when missing, the game's own edits to it are kept
    (``refresh=True`` replaces them), the autoload merged in beside the
    game's own."""
    from ..store.adopt import _merge_autoloads
    from .playtest import _godot_project_dir

    game = _godot_project_dir(root)
    config = game / "project.godot"
    if not config.is_file():
        raise RuntimeError(f"no project.godot at {game}")
    placed = _place_script(TEMPLATE, game / "addons" / "bgate" / "bgate_agent.gd",
                           refresh=refresh)
    merged = _merge_autoloads(config, (AGENT_AUTOLOAD,))
    return {**placed, "autoload": merged.get("action")}


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
    from .playtest import _godot_project_dir, _web_project_dir

    warmed: dict = {}
    if policy == "jev":
        from bgate_adapters import jev
        ok = jev.available(root)
        if not ok["available"]:
            raise RuntimeError(ok["reason"])
        # a local model loads in seconds; pay that before the clock starts
        # rather than on the bot's first in-game decision
        warmed = jev.warm(root, model=model)
    web_dir = _web_project_dir(root)
    installed = install_web(web_dir) if web_dir is not None else install(root)
    game = _godot_project_dir(root)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(root) / ".bgate" / "agentplay" / stamp
    out.mkdir(parents=True, exist_ok=True)
    if not max_decisions:
        max_decisions = int(float(seconds) * 60 / max(1, int(decide_every)) * 1.5) + 20
    relay = Relay(root, out, goal=goal, policy=policy, model=model, seed=seed,
                  max_decisions=max_decisions, max_usd=max_usd)
    if web_dir is not None:
        t0 = time.time()
        exit_code, timed_out = _play_web(web_dir, relay, out, seconds=seconds,
                                         every_s=max(1, int(decide_every)) / 60.0,
                                         scene=scene, headless=headless)
        return _finish(relay, out, t0, timed_out, exit_code, policy=policy,
                       goal=goal, seconds=seconds, max_usd=max_usd,
                       max_decisions=max_decisions, installed=installed,
                       warmed=warmed)
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
    return _finish(relay, out, t0, timed_out, proc.returncode, policy=policy,
                   goal=goal, seconds=seconds, max_usd=max_usd,
                   max_decisions=max_decisions, installed=installed,
                       warmed=warmed)


def _finish(relay: "Relay", out: Path, t0: float, timed_out: bool,
            exit_code: Any, *, policy: str, goal: str, seconds: float,
            max_usd: float, max_decisions: int, installed: dict,
            warmed: Optional[dict] = None) -> dict:
    relay.close()
    summary = summarize(out)
    summary.update({
        "ok": relay.count > 0 and not timed_out and not relay.exhausted,
        "policy": policy, "goal": goal, "game_seconds": seconds,
        "warm": warmed or {},
        "wall_seconds": round(time.time() - t0, 1), "timed_out": timed_out,
        "exit_code": exit_code, "relay_errors": relay.errors[:5],
        "capped": relay.exhausted, "max_usd": max_usd,
        "max_decisions": max_decisions,
        "usd": round(relay.usd, 6), "install": installed, "dir": str(out)})
    (out / "summary.json").write_text(json.dumps(summary, indent=2),
                                      encoding="utf-8")
    return summary


WEB_TEMPLATE = (Path(__file__).resolve().parents[2] / "templates" / "web"
                / "shared" / "src" / "bgate" / "agent.ts")


def install_web(web_dir: Path, *, refresh: bool = False) -> dict:
    """Put src/bgate/agent.ts in a web game. The game wires agent.tick and
    agent.isPaused into its own loop - that part is the game's, by design -
    and may extend the file (a ``run`` hook, say); its copy is kept unless
    ``refresh=True``. See :func:`_place_script`."""
    placed = _place_script(WEB_TEMPLATE, Path(web_dir) / "src" / "bgate" / "agent.ts",
                           refresh=refresh)
    wired = any("agent.tick" in f.read_text(encoding="utf-8", errors="replace")
                for f in (Path(web_dir) / "src").rglob("*.ts")
                if f.name != "agent.ts")
    return {**placed, "wired": wired}


def _play_web(web_dir: Path, relay: "Relay", out: Path, *, seconds: float,
              every_s: float, scene: str, headless: bool) -> tuple:
    """Serve the game, open it in a browser with the agent switched on, and
    wait for the page's own end-of-run signal, a cap, or the wall clock.

    ``scene`` is appended to the page URL as given (a path or a query the
    game reads, e.g. "?shift=3")."""
    from urllib.parse import urlencode

    from bgate_adapters import web

    dev = web.dev_start(str(web_dir))
    if not dev.get("ok"):
        raise RuntimeError(f"the dev server would not start: {dev.get('error')}")
    url = dev["url"].rstrip("/") + "/" + str(scene or "").lstrip("/")
    sep = "&" if "?" in url else "?"
    url += sep + urlencode({"bgate_agent": relay.url, "bgate_seconds": seconds,
                            "bgate_every": round(every_s, 4)})
    from playwright.sync_api import sync_playwright

    wall_cap = float(seconds) * 10 + 60
    t0 = time.time()
    timed_out = False
    with open(out / "game.log", "w", encoding="utf-8") as log, \
            sync_playwright() as play:
        channel = web._system_channel() or None
        browser = (play.chromium.launch(channel=channel, headless=headless)
                   if channel else play.chromium.launch(headless=headless))
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.on("console", lambda m: log.write(
                f"{m.type.upper()}: {m.text[:500]}\n"))
            page.on("pageerror", lambda e: log.write(f"ERROR: {e}\n"))
            page.goto(url, wait_until="load")
            while not relay.done and not relay.exhausted:
                if time.time() - t0 > wall_cap:
                    timed_out = True
                    break
                page.wait_for_timeout(500)
            # A run that never decided anything usually means the game never
            # calls agent.tick - say so instead of reporting an empty run.
            if relay.count == 0:
                log.write("ERROR: no decision was requested - is agent.tick "
                          "called from the game loop?\n")
        finally:
            browser.close()
    return (0 if relay.done else None), timed_out


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
