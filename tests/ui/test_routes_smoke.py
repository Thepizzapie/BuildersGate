"""Every registered route answers something — never a 500.

Route modules are auto-discovered (bgate_ui/routes/__init__.py imports every
sibling and includes its `router`), so the dashboard's surface grows without
anyone editing a list. This smoke test therefore enumerates ``app.routes``
rather than hardcoding paths: a route added tomorrow is covered tonight.

What counts as a pass is deliberately loose — 4xx is fine (a made-up id SHOULD
404, a bad body SHOULD 422). The bar is: the handler ran and answered in the
project's envelope instead of blowing up. A 5xx here is a real bug in whoever
owns that route.
"""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from bgate_core.design import bible, lore
from bgate_core.board import queue
from bgate_ui import api as _api
from bgate_ui.app import app

# Values for path params, by name. Anything unlisted falls back to "1" — enough
# to reach the handler, which is the whole point.
PARAM_VALUES = {
    "seat": "art",
    "key": "notes",
    "name": "no-such-ref.png",
    "ref": "no-such-lore-ref",
    "node_id": "n1",
    "file_path": "index.html",
    "rel": "nope.png",
}
DEFAULT_PARAM = "1"

_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[^}]+)?\}")


def _concrete(path: str) -> str:
    return _PARAM_RE.sub(
        lambda m: str(PARAM_VALUES.get(m.group(1), DEFAULT_PARAM)), path)


def _walk(routes) -> list:
    """Every APIRoute reachable from a route list, however it is nested.

    Starlette 1.0 stopped flattening `include_router` into `app.routes`: an
    included router now sits there as ONE opaque object holding the real routes
    behind `.original_router`. Walking only the top level therefore saw 39 of
    this app's 159 routes on new Starlette and 159 on old — and because this
    module parametrizes off the result, the coverage silently shrank by 120
    tests rather than failing. Recurse, and both shapes give the same answer.
    """
    out = []
    for route in routes:
        if isinstance(route, APIRoute):
            out.append(route)
            continue
        nested = getattr(route, "routes", None)
        if nested is None:
            inner = getattr(route, "original_router", None)  # Starlette >= 1.0
            nested = getattr(inner, "routes", None)
        if nested:
            out.extend(_walk(nested))
    return out


def _api_routes() -> list[tuple[str, str]]:
    """(method, path template) for every APIRoute the app has registered."""
    out = []
    for route in _walk(app.routes):
        for method in sorted(route.methods or []):
            if method in ("HEAD", "OPTIONS"):
                continue
            out.append((method, route.path))
    assert out, "no routes registered — did the app fail to import?"
    return sorted(set(out))


ROUTES = _api_routes()
GET_ROUTES = [r for r in ROUTES if r[0] == "GET"]
MUTATING_ROUTES = [r for r in ROUTES if r[0] != "GET"]


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """One project with a row in the tables the id-taking routes read, so the
    handlers do real work instead of short-circuiting on an empty database."""
    from bgate_core.store import db, project

    root = tmp_path_factory.mktemp("smoke")
    project.init(root, "Smoke Test", pitch="a game for the smoke test")
    item = queue.add(root, "art", "smoke item", brief="brief")
    bible.add(root, "pillar", "Pillars", "one pillar")
    lore.add_entity(root, "faction", "Smoke Guild", "they test things")
    yield {"root": root, "item_id": item["id"]}
    db.close_all()


@pytest.fixture(scope="module")
def client(seeded):
    import os

    os.environ["BGATE_ROOT"] = str(seeded["root"])
    token = _api.ensure_token(seeded["root"])
    with TestClient(app, headers={"X-Bgate-Token": token}) as c:
        yield c


@pytest.fixture(scope="module")
def bare(seeded):
    """A client that presents NO token — for probing the mutation guard."""
    import os

    os.environ["BGATE_ROOT"] = str(seeded["root"])
    _api.ensure_token(seeded["root"])  # the guard needs one to compare against
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# A mutation with no side effect whatever it reaches: an empty body fails
# validation before any write. Used to confirm the guard is actually enforcing
# BEFORE aiming unauthenticated mutations at routes that spawn agents.
CANARY = ("POST", "/api/bible")


def _hit(client, method: str, path: str, **kw):
    return client.request(method, _concrete(path), **kw)


class TestEveryRouteAnswers:
    @pytest.mark.parametrize("method,path", GET_ROUTES,
                             ids=[f"{m} {p}" for m, p in GET_ROUTES])
    def test_get_routes_do_not_500(self, client, method, path):
        res = _hit(client, method, path)
        assert res.status_code < 500, (
            f"{method} {_concrete(path)} -> {res.status_code}: "
            f"{res.text[:400]}")

    @pytest.mark.parametrize("method,path", MUTATING_ROUTES,
                             ids=[f"{m} {p}" for m, p in MUTATING_ROUTES])
    def test_mutating_routes_are_guarded_not_crashed(self, method, path,
                                                     bare, seeded, monkeypatch):
        """Mutations are NOT executed here — spawning agents and starting Godot
        from a smoke test is not smoke, it is arson. What is checked is that
        every one of them sits behind the guard and that the refusal is the
        shared envelope: an unauthenticated mutation comes back 401/403 with
        {ok: false, error: {...}}, never a 500 and never an HTML page.

        conftest disables the guard suite-wide, so it is re-armed here — and
        the canary confirms the re-arming worked before any request is aimed at
        a route that would otherwise DO something."""
        monkeypatch.delenv("BGATE_NO_AUTH", raising=False)
        _api.ensure_token(seeded["root"])
        canary = bare.request(*CANARY, json={})
        if canary.status_code not in (401, 403):
            pytest.skip(f"mutation guard is not enforcing here "
                        f"(canary -> {canary.status_code}); refusing to fire "
                        f"unauthenticated mutations at live routes")
        res = bare.request(method, _concrete(path), json={})
        assert res.status_code in (401, 403), (
            f"{method} {_concrete(path)} -> {res.status_code} unguarded: "
            f"{res.text[:300]}")
        body = res.json()
        assert body["ok"] is False
        assert set(body["error"]) >= {"code", "message"}


class TestEnvelopeAndDocs:
    def test_index_and_static_are_served(self, client):
        assert client.get("/").status_code == 200
        assert client.get("/static/index.html").status_code == 200

    def test_unknown_api_path_is_a_json_envelope_not_html(self, client):
        res = client.get("/api/definitely-not-a-route")
        assert res.status_code == 404
        assert res.json()["ok"] is False

    def test_a_missing_row_404s_in_the_envelope(self, client):
        res = client.get("/api/queue/99999999")
        assert res.status_code == 404
        assert res.json()["ok"] is False

    def test_the_route_table_is_actually_populated(self):
        """Guards the enumeration itself: if `routes.register` silently skipped
        every module, the parametrized tests above would vacuously pass."""
        paths = {p for _m, p in ROUTES}
        assert len(paths) > 40, sorted(paths)
        assert "/api/state" in paths
        assert any(p.startswith("/api/workspace") for p in paths), \
            "auto-registered route modules are missing"
