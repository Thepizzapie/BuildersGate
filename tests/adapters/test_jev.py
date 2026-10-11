"""The Jev adapter and the playtest relay, offline.

What the adapter owes the pipeline is the CONTRACT: questions validated before
they cost a call, the route chosen by which key is set, the provider's own
cost figure preferred, and a refusal that names the key to set. The relay owes
the game a choice from ITS options and a log line per decision.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from bgate_adapters import _http, jev
from bgate_core.qa import agentplay


@pytest.fixture()
def nokeys(monkeypatch):
    for rid in jev.ORDER:
        monkeypatch.delenv(jev.ROUTES[rid][0], raising=False)
    monkeypatch.setattr(jev.envfile, "load_env", lambda root=None: {})


@pytest.fixture()
def transport(monkeypatch):
    calls = []

    def fake(method, url, **kw):
        calls.append({"url": url, **kw})
        body = {"model": "jev-1.13.0",
                "answers": {"action": {"type": "choice", "choice": "jump",
                                       "probabilities": {"jump": 0.9,
                                                         "wait": 0.1},
                                       "confidence": 0.8}},
                "usage": {"input_tokens": 1000, "output_tokens": 20}}
        return _http.Response(200, json.dumps(body).encode(), {}, url)

    monkeypatch.setattr(jev._http, "request", fake)
    return calls


QS = {"action": {"type": "choice", "instructions": "pick",
                 "criteria": ["jump", "wait"]}}


def test_questions_are_normalised_before_any_call():
    out = jev.normalize_questions({
        "a": {"type": "boolean", "instructions": "x"},
        "b": {"type": "choice", "instructions": "y", "criteria": ["l", "r"]},
        "c": {"type": "score", "instructions": "z",
              "criteria": ["low", "mid", "high"]}})
    assert out["a"]["type"] == "noul"
    assert out["b"]["criteria"] == {"l": None, "r": None}
    assert out["c"]["criteria"] == ["low", "mid", "high"]


@pytest.mark.parametrize("bad", [
    {},
    {"a": {"type": "essay", "instructions": "x"}},
    {"a": {"type": "noul"}},
    {"a": {"type": "choice", "instructions": "x", "criteria": ["one"]}},
    {"a": {"type": "score", "instructions": "x", "criteria": ["one"]}},
])
def test_bad_questions_refused_by_name(bad):
    with pytest.raises(jev.JevError):
        jev.normalize_questions(bad)


def test_no_key_names_the_command(nokeys):
    got = jev.available()
    assert got["available"] is False
    assert "bgate key set typesafe" in got["reason"]
    with pytest.raises(jev.JevError, match="TYPESAFE_API_KEY"):
        jev.decide("state", QS)


def test_typesafe_key_wins_over_gateway(nokeys, monkeypatch, transport):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts")
    got = jev.decide("a goomba approaches", QS)
    assert transport[0]["url"] == "https://api.typesafe.ai/v1/systemone"
    assert transport[0]["headers"]["Authorization"] == "Bearer ts"
    assert transport[0]["json"]["model"] == "jev-latest"
    assert got["route"] == "typesafe"
    assert got["answers"]["action"]["choice"] == "jump"
    # no provider cost figure: the published rate, 1000 tokens
    assert got["usd"] == pytest.approx(1000 * jev.USD_PER_INPUT_TOKEN)


def test_gateway_route_and_its_cost(nokeys, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw")
    seen = {}

    def fake(method, url, **kw):
        seen.update(url=url, model=kw["json"]["model"])
        body = {"answers": {}, "usage": {"input_tokens": 5},
                "provider_metadata": {"gateway": {"cost": "0.00002"}}}
        return _http.Response(200, json.dumps(body).encode(), {}, url)

    monkeypatch.setattr(jev._http, "request", fake)
    got = jev.decide("s", QS)
    assert seen["url"] == "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
    assert seen["model"] == "typesafe-ai/jev"
    assert got["usd"] == pytest.approx(0.00002)


def test_render_is_lines_not_json():
    text = agentplay.render({"player": {"hp": 3},
                             "nearby": [{"name": "goomba", "dx": 40}]})
    assert "{" not in text
    assert "hp: 3" in text and "- name goomba, dx 40" in text


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_relay_random_policy_picks_an_offered_option(tmp_path):
    relay = agentplay.Relay(tmp_path, tmp_path, goal="win", policy="random",
                            seed=1)
    try:
        got = _post(relay.url, {"state": {"hp": 3},
                                "options": {"left": "", "right": ""}})
    finally:
        relay.close()
    assert got["choice"] in ("left", "right")
    logged = [json.loads(line) for line in
              (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert len(logged) == 1 and logged[0]["choice"] == got["choice"]
    assert "hp: 3" in logged[0]["state"]


def test_relay_refuses_a_choice_outside_the_options(tmp_path, monkeypatch):
    monkeypatch.setattr(jev, "decide", lambda *a, **k: {
        "answers": {"action": {"choice": "teleport"}}, "usd": 0.0})
    relay = agentplay.Relay(tmp_path, tmp_path, goal="win", policy="jev")
    try:
        with pytest.raises(urllib.error.HTTPError):
            _post(relay.url, {"state": "s",
                              "options": {"left": "", "right": ""}})
    finally:
        relay.close()
    assert relay.errors and "teleport" in relay.errors[0]


def test_agent_template_is_inert_without_the_url():
    src = agentplay.TEMPLATE.read_text(encoding="utf-8")
    assert 'OS.get_environment("BGATE_AGENT_URL")' in src
    assert "set_physics_process(false)" in src
    assert "API_KEY" not in src


def test_triage_keeps_lexical_without_a_key(nokeys):
    from bgate_core.qa import feedback
    items = [{"text": "the jump feels floaty", "kind": "fix", "seat": "gameplay"}]
    feedback.triage(items)
    assert items[0]["kind"] == "fix" and items[0]["seat"] == "gameplay"
    assert items[0]["classifier"] == "lexical"


def test_triage_overrides_only_when_confident(monkeypatch):
    from bgate_core.qa import feedback
    monkeypatch.setattr(jev, "available", lambda root=None, **k: {"available": True})
    answers = iter([
        {"kind": {"choice": "fix", "confidence": 0.99},
         "seat": {"choice": "audio", "confidence": 0.92}},
        {"kind": {"choice": "like", "confidence": 0.2},
         "seat": {"choice": "art", "confidence": 0.3}},
    ])
    monkeypatch.setattr(jev, "decide",
                        lambda *a, **k: {"answers": next(answers), "usd": 0.0})
    items = [{"text": "making my ears bleed", "kind": "note", "seat": "unassigned"},
             {"text": "hmm", "kind": "note", "seat": "unassigned"}]
    feedback.triage(items, workers=1)
    assert (items[0]["kind"], items[0]["seat"], items[0]["classifier"]) == \
        ("fix", "audio", "jev")
    assert (items[1]["kind"], items[1]["seat"], items[1]["classifier"]) == \
        ("note", "unassigned", "lexical")


def test_relay_stops_at_its_decision_cap(tmp_path):
    relay = agentplay.Relay(tmp_path, tmp_path, goal="win", policy="random",
                            max_decisions=2)
    try:
        for _ in range(2):
            _post(relay.url, {"state": "s", "options": {"a": "", "b": ""}})
        with pytest.raises(urllib.error.HTTPError):
            _post(relay.url, {"state": "s", "options": {"a": "", "b": ""}})
    finally:
        relay.close()
    assert relay.count == 2 and "decision cap" in relay.exhausted


def test_relay_stops_at_its_spend_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(jev, "decide", lambda *a, **k: {
        "answers": {"action": {"choice": "a"}}, "usd": 0.06})
    relay = agentplay.Relay(tmp_path, tmp_path, goal="win", policy="jev",
                            max_usd=0.10)
    try:
        for _ in range(2):
            _post(relay.url, {"state": "s", "options": {"a": "", "b": ""}})
        with pytest.raises(urllib.error.HTTPError):
            _post(relay.url, {"state": "s", "options": {"a": "", "b": ""}})
    finally:
        relay.close()
    assert relay.count == 2 and "spend cap" in relay.exhausted
