"""The dashboard's origin guard, exercised for real.

conftest disables the guard for the rest of the suite (see `_no_dashboard_auth`)
so 350 tests do not each have to plumb a bearer token. This module is the
exception that keeps the guard honest: it turns it back on and proves it
actually refuses.

Why the guard exists at all: binding to 127.0.0.1 is not a security boundary.
Any page open in the browser can POST to localhost, and every mutating endpoint
here can spawn an agent with Bash access to the user's game repo.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_ui import api


@pytest.fixture()
def guarded(root, monkeypatch):
    """A client with the guard live and the project's real token in hand."""
    monkeypatch.delenv("BGATE_NO_AUTH", raising=False)
    monkeypatch.setenv("BGATE_ROOT", str(root))
    from bgate_ui.app import app

    return TestClient(app), api.ensure_token(root)


class TestMutationGuard:
    def test_mutation_without_a_token_is_refused(self, guarded):
        client, _ = guarded
        got = client.post("/api/queue", json={"seat": "tech", "title": "x"})
        assert got.status_code == 401
        assert got.json()["error"]["code"] == "unauthorized"

    def test_mutation_with_the_token_is_allowed(self, guarded):
        client, token = guarded
        got = client.post("/api/queue", json={"seat": "tech", "title": "x"},
                          headers={"X-Bgate-Token": token})
        assert got.status_code == 200

    def test_a_stale_token_is_refused(self, guarded):
        client, _ = guarded
        got = client.post("/api/queue", json={"seat": "tech", "title": "x"},
                          headers={"X-Bgate-Token": "not-the-token"})
        assert got.status_code == 401

    def test_bearer_header_is_accepted_too(self, guarded):
        client, token = guarded
        got = client.post("/api/queue", json={"seat": "tech", "title": "x"},
                          headers={"Authorization": f"Bearer {token}"})
        assert got.status_code == 200

    def test_reads_never_need_a_token(self, guarded):
        """A viewer must not be locked out of looking; only mutations are gated."""
        client, _ = guarded
        assert client.get("/api/queue").status_code == 200


class TestCrossOrigin:
    def test_foreign_origin_is_refused_even_with_a_token(self, guarded):
        """The token is in .bgate/; a hostile page cannot read it — but if it
        ever leaked, same-origin is the second lock on the door."""
        client, token = guarded
        got = client.post("/api/queue", json={"seat": "tech", "title": "x"},
                          headers={"X-Bgate-Token": token,
                                   "Origin": "http://evil.example"})
        assert got.status_code == 403
        assert got.json()["error"]["code"] == "cross_origin"

    def test_cross_site_fetch_metadata_is_refused(self, guarded):
        client, token = guarded
        got = client.post("/api/queue", json={"seat": "tech", "title": "x"},
                          headers={"X-Bgate-Token": token,
                                   "Sec-Fetch-Site": "cross-site"})
        assert got.status_code == 403


class TestToken:
    def test_token_is_stable_across_calls(self, root):
        assert api.ensure_token(root) == api.ensure_token(root)

    def test_token_lives_in_the_gitignored_bgate_dir(self, root):
        api.ensure_token(root)
        assert api.token_path(root).parent.name == ".bgate"

    def test_page_carries_the_fetch_shim(self, guarded):
        """Every same-origin fetch on the page must pick the token up without
        each of the ~200 call sites being edited."""
        client, token = guarded
        html = client.get("/").text
        assert "BGATE_TOKEN" in html and token in html
        assert "X-Bgate-Token" in html


class TestActor:
    def test_an_agent_env_identifies_as_an_agent(self, monkeypatch):
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-7")
        assert api.current_actor() == "agent:item-7"
        assert not api.is_human("agent:item-7")

    def test_a_human_is_not_an_agent(self, monkeypatch):
        monkeypatch.delenv("BGATE_ACTOR", raising=False)
        monkeypatch.setenv("BGATE_STUDIO_USER", "devon")
        assert api.current_actor() == "devon"
        assert api.is_human("devon")

    def test_require_human_refuses_an_agent(self):
        with pytest.raises(api.ApiError) as caught:
            api.require_human("agent:item-3", "approve")
        assert caught.value.status == 403
        assert "agent:item-3" in caught.value.message


class TestHostGate:
    """DNS rebinding, which every other check in the guard is blind to.

    The origin and sec-fetch-site checks both compare the request against
    whatever Host it carries, so a page on evil.com that rebinds its own DNS to
    127.0.0.1 satisfies BOTH -- the browser genuinely believes it is
    same-origin, which also means it is allowed to read the response. It then
    fetches `/`, a safe method and therefore exempt from the token, scrapes
    window.BGATE_TOKEN out of the HTML, and has the whole mutating surface --
    including POST /api/godot/run, which is arbitrary GDScript, which is a shell.

    The Host gate is the only thing that sees this, because it checks the name
    the client ASKED FOR rather than comparing the request to itself.
    """

    def test_the_all_zeros_spelling_of_loopback_is_refused(self, guarded):
        """0.0.0.0 reaches a 127.0.0.1 listener while reading as a foreign
        origin to the browser, so it walks around the private-network
        protections "localhost" gets. It is the one bypass that needs no DNS at
        all, and it was on the allowlist."""
        client, token = guarded
        got = client.get("/api/state", headers={"Host": "0.0.0.0:7788"})
        assert got.status_code == 403
        assert got.json()["error"]["code"] == "bad_host"

    def test_a_rebound_host_is_refused(self, guarded):
        client, token = guarded
        got = client.post("/api/queue", json={"seat": "tech", "title": "x"},
                          headers={"X-Bgate-Token": token,
                                   "Host": "evil.example:7788",
                                   "Origin": "http://evil.example:7788",
                                   "Sec-Fetch-Site": "same-origin"})
        assert got.status_code == 403
        assert got.json()["error"]["code"] == "bad_host"

    def test_the_gate_also_covers_reads(self, guarded):
        """`/` hands out the token, and GET is exempt from every other check.

        A rebinding attack never needs to mutate anything through the front
        door -- it only needs to READ one page. If the Host gate stopped at
        mutations it would be decorative.
        """
        client, _ = guarded
        got = client.get("/", headers={"Host": "evil.example:7788"})
        assert got.status_code == 403

    def test_a_rebound_host_is_refused_even_with_auth_disabled(self, guarded,
                                                              monkeypatch):
        """BGATE_NO_AUTH is a CI convenience; it is not a reason to answer to
        a name the user never typed."""
        client, _ = guarded
        monkeypatch.setenv("BGATE_NO_AUTH", "1")
        got = client.post("/api/queue", json={"seat": "tech", "title": "x"},
                          headers={"Host": "evil.example:7788"})
        assert got.status_code == 403

    def test_localhost_still_works(self, guarded):
        client, token = guarded
        for host in ("127.0.0.1:7788", "localhost:7788"):
            got = client.post("/api/queue", json={"seat": "tech", "title": host},
                              headers={"X-Bgate-Token": token, "Host": host})
            assert got.status_code < 400, (host, got.status_code, got.text)
