"""Jev - TypeSafe AI's decision model - over plain HTTP.

Jev does not write. It takes a STATE (text) and named QUESTIONS and returns a
typed answer per question: ``noul`` (a probability that something is true),
``choice`` (one of up to 255 named options, with a probability for each) or
``score`` (a position on a 2-10 level scale). That is the shape a bot needs to
play a game - "given this frame, which of these inputs" - and the shape a seat
needs for a yes/no it would otherwise guess.

TWO ROUTES, ONE BODY. TypeSafe's own API and Vercel's AI Gateway take the same
``/v1/systemone`` request and return the same answers; only the base URL, the
key and the model id differ. TypeSafe's own key wins when both are set.

A THIRD ROUTE, LOCAL AND LAST. Ollama 0.40+ serves the SAME ``/v1/systemone``
natively for its decision models (``laya`` 421M, ``tev1`` 4B, ``nimble`` 9B),
so ``BGATE_JEV_OLLAMA_MODEL=<model>`` is a table row: same body, same answers,
``127.0.0.1:11434`` (or ``OLLAMA_HOST``), no key, no cost. Any hosted key still
wins: the local route is for a machine with no key and no one at the keyboard.
A chat model is refused by Ollama (400 "not a decision model"), not prompted
into JSON here: a shim's calibration is not Jev's, and the bots read the
probabilities. The local models are weaker than hosted Jev (Ollama's own
figures: nimble 75.7%, tev1 73.3%, Jev 76.0% on the same labelled sets), so
a feel check run on one is not comparable to one run on the hosted model;
every result says ``route``, quote it with the number.

THE KEY NEVER LEAVES PYTHON. The game-side agent (``BGateAgent``) talks to a
loopback relay (``bgate_core.qa.agentplay``), and the relay calls this module.
A key in a game build is a key in every copy of the game.

Cost: $0.042 per million input tokens, output free. A 300-token state is about
$0.0000126 a decision, so a thousand-decision playtest is about a cent.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from bgate_core.store import envfile

from . import _http

#: route id -> (env var, base URL, default model)
ROUTES: dict[str, tuple[str, str, str]] = {
    "typesafe": ("TYPESAFE_API_KEY", "https://api.typesafe.ai", "jev-latest"),
    "ai_gateway": ("AI_GATEWAY_API_KEY", "https://ai-gateway.vercel.sh/typesafe",
                   "typesafe-ai/jev"),
    # The env var holds the MODEL NAME, not a key: Ollama has none. Setting it
    # is the opt-in; "1"/"on" means the default model. OLLAMA_HOST, when set,
    # replaces the base URL.
    # nimble by default: on the same Turf War frame it alone saw the danger
    # (0.75) and ranked the two right moves first; tev1 stood still under a
    # ball, laya called danger a coin flip. 106 ms warm, 7.9 s to load, so
    # warm() before a game launches. Measured 2026-10-17 on an RTX 5090.
    "ollama": ("BGATE_JEV_OLLAMA_MODEL", "http://127.0.0.1:11434", "nimble"),
}
ORDER = ("typesafe", "ai_gateway", "ollama")
HOSTED = ("typesafe", "ai_gateway")
#: the other spelling of the opt-in, so a brief that says LOCAL still works
OLLAMA_MODEL_ALIAS_ENV = "BGATE_JEV_LOCAL_MODEL"
OLLAMA_HOST_ENV = "OLLAMA_HOST"
#: a cold model load is seconds, not the 15 s a hosted call is allowed
OLLAMA_MIN_TIMEOUT = 60.0
_ON = ("1", "on", "true", "yes")
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000
TYPES = ("noul", "choice", "score")
#: Vercel's own Decision API spells noul "boolean"; accept it on the way in.
_ALIASES = {"boolean": "noul", "bool": "noul", "yes_no": "noul"}
MAX_CHOICES = 255


class JevError(_http.ProviderError):
    """A decision that could not be made, in a sentence worth surfacing."""


def _key(env: str, root: Any) -> str:
    try:
        envfile.load_env(root)
    except Exception:                                            # noqa: BLE001
        pass
    got = (os.environ.get(env) or "").strip()
    if not got and env == ROUTES["ollama"][0]:
        got = (os.environ.get(OLLAMA_MODEL_ALIAS_ENV) or "").strip()
    return got


def ollama_model(root: Any = None) -> str:
    """The local model the opt-in names, or the route's default for a bare
    ``1``/``on``. Empty when the local route is not opted in."""
    raw = _key(ROUTES["ollama"][0], root)
    return ROUTES["ollama"][2] if raw.lower() in _ON else raw


WARM_QUESTIONS = {"ready": {"type": "noul",
                            "instructions": "Is this a warm-up call?"}}


def warm(root: Any = None, *, model: str = "", timeout: float = 120.0) -> dict:
    """Pay the local model's load now, not on the bot's first in-game decision.

    Hosted routes have nothing to load, so this makes no call for them. Never
    raises: a warm-up that fails is reported, and the first real decision then
    carries the load (and its own error, if any)."""
    rid = route(root)
    if rid != "ollama":
        return {"warmed": False, "route": rid, "reason": "nothing to load"}
    try:
        got = decide("warm-up", WARM_QUESTIONS, root=root, model=model,
                     timeout=timeout, retries=1)
    except JevError as exc:
        return {"warmed": False, "route": rid, "reason": str(exc)}
    return {"warmed": True, "route": rid, "model": got["model"], "ms": got["ms"]}


def ollama_base(root: Any = None) -> str:
    """Ollama's base URL: ``OLLAMA_HOST`` as the Ollama CLI reads it (host, or
    host:port, or a full URL), else loopback on the default port."""
    raw = (os.environ.get(OLLAMA_HOST_ENV) or "").strip() or ROUTES["ollama"][1]
    if "://" not in raw:
        raw = "http://" + raw
    return raw.rstrip("/")


def route(root: Any = None) -> Optional[str]:
    """The route a call would take right now, or None with no key at all."""
    for rid in ORDER:
        if _key(ROUTES[rid][0], root):
            return rid
    return None


def available(root: Any = None, *, route: str = "") -> dict:
    """Presence only. With ``route`` the answer is about that route's key."""
    ids = (route,) if route else ORDER
    for rid in ids:
        if _key(ROUTES[rid][0], root):
            return {"available": True, "reason": "", "route": rid}
    if ids == ("ollama",):
        return {"available": False,
                "reason": f"{ROUTES['ollama'][0]} not set - name a pulled "
                          "Ollama model, e.g. BGATE_JEV_OLLAMA_MODEL=qwen2.5:7b "
                          "(`ollama list` shows them)"}
    hosted = [r for r in ids if r in HOSTED]
    envs = " or ".join(ROUTES[r][0] for r in hosted)
    reason = f"{envs} not set - run `bgate key set {hosted[0]} --global`"
    if "ollama" in ids:
        reason += (f"; or set {ROUTES['ollama'][0]} to a pulled Ollama model "
                   "for a local, slower bot")
    return {"available": False, "reason": reason}


def normalize_questions(questions: dict) -> dict:
    """Validate and canonicalise a question set before it costs a call.

    A choice's ``criteria`` may be a list of names (descriptions null) or a
    name -> description dict; a score's must be a 2-10 item list. Errors name
    the question, because 422 from the API names nothing useful."""
    if not isinstance(questions, dict) or not questions:
        raise JevError("questions must be a non-empty {name: question} object")
    out: dict = {}
    for name, q in questions.items():
        if not isinstance(q, dict):
            raise JevError(f"question {name!r} must be an object")
        kind = _ALIASES.get(str(q.get("type", "")).lower(),
                            str(q.get("type", "")).lower())
        if kind not in TYPES:
            raise JevError(f"question {name!r}: type must be one of "
                           f"noul, choice, score (got {q.get('type')!r})")
        instructions = str(q.get("instructions") or "").strip()
        if not instructions:
            raise JevError(f"question {name!r} has no instructions")
        one: dict = {"type": kind, "instructions": instructions}
        criteria = q.get("criteria")
        if kind == "choice":
            if isinstance(criteria, (list, tuple)):
                criteria = {str(c): None for c in criteria}
            if not isinstance(criteria, dict) or len(criteria) < 2:
                raise JevError(f"choice {name!r} needs at least two options")
            if len(criteria) > MAX_CHOICES:
                raise JevError(f"choice {name!r} has {len(criteria)} options; "
                               f"the limit is {MAX_CHOICES}")
            one["criteria"] = {str(k): (None if v in (None, "") else str(v))
                               for k, v in criteria.items()}
        elif kind == "score":
            if not isinstance(criteria, (list, tuple)) \
                    or not 2 <= len(criteria) <= 10:
                raise JevError(f"score {name!r} needs a list of 2-10 levels")
            one["criteria"] = [str(c) for c in criteria]
        out[str(name)] = one
    return out


def decide(state: str, questions: dict, *, root: Any = None, model: str = "",
           timeout: float = 15.0, retries: int = 2) -> dict:
    """Ask Jev ``questions`` about ``state``. Raises :class:`JevError`.

    Returns ``{ok, answers, model, route, usage, usd, ms}``; ``answers`` is the
    API's own per-question object (``choice`` + ``probabilities`` +
    ``confidence``, ``score`` + ..., or ``noul``)."""
    if not str(state or "").strip():
        raise JevError("state is empty - Jev judges a state, give it one")
    qs = normalize_questions(questions)
    rid = route(root)
    if rid is None:
        raise JevError(available(root)["reason"], provider="jev")
    env, base, default_model = ROUTES[rid]
    headers = {"Content-Type": "application/json"}
    if rid == "ollama":
        # same body, same answers; a local server, no bearer, a load to wait for
        base = ollama_base(root)
        name = model or ollama_model(root)
        timeout = max(float(timeout), OLLAMA_MIN_TIMEOUT)
    else:
        headers["Authorization"] = f"Bearer {_key(env, root)}"
        name = model or default_model
    t0 = time.perf_counter()
    try:
        got = _http.request(
            "POST", base + "/v1/systemone", headers=headers,
            json={"model": name, "state": str(state), "questions": qs},
            timeout=timeout, retries=retries, retry_submit=True,
            provider="jev")
    except _http.ProviderError as exc:
        if exc.status == 0:
            hint = " - is Ollama running? `ollama serve`" if rid == "ollama" else ""
            raise JevError(f"Jev unreachable ({rid}): {exc.body}{hint}",
                           provider="jev") from exc
        raise JevError(f"Jev {rid} -> {exc.status}: {_detail(exc.body)}"
                       f"{_ollama_hint(rid, exc.status, exc.body, name)}",
                       provider="jev", status=exc.status, body=exc.body,
                       billing=exc.billing) from exc
    ms = round((time.perf_counter() - t0) * 1000)
    body = got.json(provider="jev")
    usage = body.get("usage") or {}
    return {"ok": True, "answers": body.get("answers") or {},
            "model": body.get("model") or name, "route": rid, "usage": usage,
            "usd": 0.0 if rid == "ollama" else _usd(body, got.headers, usage),
            "ms": ms}


def _ollama_hint(rid: str, status: int, body: str, name: str) -> str:
    """What to type next, for the two local refusals worth naming: the model
    is not pulled, or it is a chat model and Ollama will not decide with it."""
    if rid != "ollama":
        return ""
    if status == 404:
        return f"; `ollama pull {name}`"
    if status == 400 and "decision model" in (body or ""):
        return ("; only a decision model answers /v1/systemone: "
                "`ollama pull nimble` (9B, the default) or `ollama pull laya` "
                "(421M, fastest)")
    return ""


def _usd(body: dict, headers: dict, usage: dict) -> float:
    """The provider's own cost figure when it gives one, the published rate
    otherwise."""
    gw = ((body.get("provider_metadata") or {}).get("gateway") or {})
    for raw in (gw.get("cost"), headers.get("x-cost-usd")):
        try:
            if raw not in (None, ""):
                return float(raw)
        except (TypeError, ValueError):
            pass
    return float(usage.get("input_tokens") or 0) * USD_PER_INPUT_TOKEN


def _detail(body: str) -> str:
    import json
    try:
        parsed = json.loads(body)
    except Exception:                                            # noqa: BLE001
        return (body or "")[:300]
    if isinstance(parsed, dict):
        return str(parsed.get("message") or parsed.get("error")
                   or parsed.get("detail") or parsed)[:300]
    return str(parsed)[:300]
