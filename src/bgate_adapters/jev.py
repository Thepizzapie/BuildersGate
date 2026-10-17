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

A THIRD ROUTE, LOCAL AND LAST. ``BGATE_JEV_OLLAMA_MODEL=<model>`` names a
pulled Ollama model and the same questions go to ``127.0.0.1:11434`` as one
JSON-schema-constrained chat call; the answer is mapped back into Jev's
shape, so nothing above this module notices. Any hosted key still wins: the
local route is for a machine with no key and no one at the keyboard, and its
bot is slower and dumber than Jev (a 3B model answers in about 200 ms once
loaded, but the first call pays a model load of several seconds), so a feel
check run on it is not comparable to one run on the hosted model. Say which
route a number came from when you quote it.

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
    # is the opt-in. OLLAMA_HOST, when set, replaces the base URL.
    "ollama": ("BGATE_JEV_OLLAMA_MODEL", "http://127.0.0.1:11434", ""),
}
ORDER = ("typesafe", "ai_gateway", "ollama")
HOSTED = ("typesafe", "ai_gateway")
OLLAMA_HOST_ENV = "OLLAMA_HOST"
#: a cold model load is seconds, not the 15 s a hosted call is allowed; and
#: keep it resident for a whole playtest rather than Ollama's 5-minute default
OLLAMA_MIN_TIMEOUT = 60.0
OLLAMA_KEEP_ALIVE = "30m"
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
    return (os.environ.get(env) or "").strip()


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
    if rid == "ollama":
        return _decide_ollama(str(state), qs, root=root, model=model,
                              timeout=timeout, retries=retries)
    env, base, default_model = ROUTES[rid]
    t0 = time.perf_counter()
    try:
        got = _http.request(
            "POST", base + "/v1/systemone",
            headers={"Authorization": f"Bearer {_key(env, root)}",
                     "Content-Type": "application/json"},
            json={"model": model or default_model, "state": str(state),
                  "questions": qs},
            timeout=timeout, retries=retries, retry_submit=True,
            provider="jev")
    except _http.ProviderError as exc:
        if exc.status == 0:
            raise JevError(f"Jev unreachable ({rid}): {exc.body}",
                           provider="jev") from exc
        raise JevError(f"Jev {rid} -> {exc.status}: {_detail(exc.body)}",
                       provider="jev", status=exc.status, body=exc.body,
                       billing=exc.billing) from exc
    ms = round((time.perf_counter() - t0) * 1000)
    body = got.json(provider="jev")
    usage = body.get("usage") or {}
    return {"ok": True, "answers": body.get("answers") or {},
            "model": body.get("model"), "route": rid, "usage": usage,
            "usd": _usd(body, got.headers, usage), "ms": ms}


# ---------------------------------------------------------------------------
# the local route: Ollama, one schema-constrained chat call per decision
# ---------------------------------------------------------------------------
def ollama_base(root: Any = None) -> str:
    """Ollama's base URL: ``OLLAMA_HOST`` as the Ollama CLI reads it (host, or
    host:port, or a full URL), else loopback on the default port."""
    raw = (os.environ.get(OLLAMA_HOST_ENV) or "").strip() or ROUTES["ollama"][1]
    if "://" not in raw:
        raw = "http://" + raw
    return raw.rstrip("/")


def ollama_schema(qs: dict) -> dict:
    """The JSON schema Ollama is told to fill: one object per question, so the
    model can only answer with a listed option or a number in range. The
    schema does the validating that TypeSafe's server does for the hosted
    routes."""
    unit = {"type": "number", "minimum": 0, "maximum": 1}
    props: dict = {}
    for name, q in qs.items():
        if q["type"] == "choice":
            props[name] = {"type": "object",
                           "properties": {"choice": {"type": "string",
                                                     "enum": list(q["criteria"])},
                                          "confidence": unit},
                           "required": ["choice", "confidence"]}
        elif q["type"] == "score":
            props[name] = {"type": "object",
                           "properties": {"score": {"type": "string",
                                                    "enum": list(q["criteria"])},
                                          "confidence": unit},
                           "required": ["score", "confidence"]}
        else:
            props[name] = {"type": "object",
                           "properties": {"probability": unit},
                           "required": ["probability"]}
    return {"type": "object", "properties": props, "required": list(props)}


_OLLAMA_SYSTEM = (
    "You are a decision model for a game bot. You do not write prose. Given a "
    "game state and typed questions, answer every question with exactly the "
    "JSON the schema demands: for a choice, one of the listed options and how "
    "sure you are (0 to 1); for a probability, a number from 0 to 1; for a "
    "score, one of the listed levels, lowest first, and how sure you are. "
    "Decide fast and literally from the state given.")


def ollama_messages(state: str, qs: dict) -> list[dict]:
    """The hosted body rendered as a prompt: the state, then each question
    with its options or levels on the lines under it."""
    lines = ["STATE:", state.strip(), "", "QUESTIONS:"]
    for name, q in qs.items():
        kind = {"noul": "probability 0-1", "choice": "choice",
                "score": "score, levels lowest to highest"}[q["type"]]
        lines.append(f"- {name} ({kind}): {q['instructions']}")
        if q["type"] == "choice":
            opts = [f"{k}: {v}" if v else k for k, v in q["criteria"].items()]
            lines.append("    options: " + "; ".join(opts))
        elif q["type"] == "score":
            lines.append("    levels: " + ", ".join(q["criteria"]))
    return [{"role": "system", "content": _OLLAMA_SYSTEM},
            {"role": "user", "content": "\n".join(lines)}]


def _unit(value: Any, name: str, field: str) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        raise JevError(f"ollama answered {name!r} with a non-numeric {field}: "
                       f"{value!r}", provider="jev") from None


def _spread(pick: str, options: list[str], confidence: float) -> dict:
    """Jev gives a probability per option; a chat model gives one pick and a
    confidence. Hand the pick its confidence and split the rest evenly, so
    callers that read ``probabilities`` see a distribution that sums to one."""
    rest = (1.0 - confidence) / max(1, len(options) - 1)
    return {o: round(confidence if o == pick else rest, 6) for o in options}


def ollama_answers(content: str, qs: dict) -> dict:
    """Map the model's JSON back into Jev's per-question answer objects, or
    refuse by name: a pick outside the options is an error, never a guess."""
    import json
    try:
        got = json.loads(content or "")
    except ValueError:
        raise JevError(f"ollama returned non-JSON: {(content or '')[:200]}",
                       provider="jev") from None
    if not isinstance(got, dict):
        raise JevError("ollama returned JSON that is not an object", provider="jev")
    answers: dict = {}
    for name, q in qs.items():
        one = got.get(name)
        if not isinstance(one, dict):
            raise JevError(f"ollama did not answer {name!r}", provider="jev")
        if q["type"] == "noul":
            p = _unit(one.get("probability"), name, "probability")
            answers[name] = {"type": "noul", "noul": p,
                             "confidence": round(abs(2 * p - 1), 6)}
            continue
        field = "choice" if q["type"] == "choice" else "score"
        options = list(q["criteria"])
        pick = str(one.get(field) or "")
        if pick not in options:
            raise JevError(f"ollama answered {name!r} with {pick!r}, not one of "
                           f"{', '.join(options)}", provider="jev")
        confidence = _unit(one.get("confidence", 1.0), name, "confidence")
        answers[name] = {"type": q["type"], field: pick,
                         "probabilities": _spread(pick, options, confidence),
                         "confidence": confidence}
    return answers


def _decide_ollama(state: str, qs: dict, *, root: Any, model: str,
                   timeout: float, retries: int) -> dict:
    name = model or _key(ROUTES["ollama"][0], root)
    base = ollama_base(root)
    t0 = time.perf_counter()
    try:
        got = _http.request(
            "POST", base + "/api/chat",
            json={"model": name, "stream": False, "think": False,
                  "keep_alive": OLLAMA_KEEP_ALIVE,
                  "options": {"temperature": 0},
                  "messages": ollama_messages(state, qs),
                  "format": ollama_schema(qs)},
            timeout=max(float(timeout), OLLAMA_MIN_TIMEOUT), retries=retries,
            retry_submit=True, provider="jev")
    except _http.ProviderError as exc:
        if exc.status == 0:
            raise JevError(f"Jev unreachable (ollama at {base}): {exc.body} - "
                           "is Ollama running? `ollama serve`",
                           provider="jev") from exc
        hint = f"; `ollama pull {name}`" if exc.status == 404 else ""
        raise JevError(f"Jev ollama -> {exc.status}: {_detail(exc.body)}{hint}",
                       provider="jev", status=exc.status, body=exc.body) from exc
    ms = round((time.perf_counter() - t0) * 1000)
    body = got.json(provider="jev")
    content = (body.get("message") or {}).get("content") or ""
    usage = {"input_tokens": int(body.get("prompt_eval_count") or 0),
             "output_tokens": int(body.get("eval_count") or 0)}
    return {"ok": True, "answers": ollama_answers(content, qs),
            "model": body.get("model") or name, "route": "ollama",
            "usage": usage, "usd": 0.0, "ms": ms}


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
