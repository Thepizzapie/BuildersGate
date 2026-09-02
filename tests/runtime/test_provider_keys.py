"""Art-provider API keys: the registry, the .env writer, and the endpoints.

The property under test is mostly a NEGATIVE one — no surface returns a key —
and negative properties rot silently, so they are asserted against the whole
serialised response rather than against a field list somebody has to remember to
extend. If a future field carries the value, `test_no_endpoint_ever_returns_the
_key` fails on the substring, not on a schema nobody updated.

The rest is the two failures this feature is one careless commit away from
repeating: a .env rewrite that eats the user's other variables, and an agent
writing a credential the human never chose.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import envfile
from bgate_core.runtime import providers

# Never a real credential, and shaped so a leak is greppable in any encoding.
FAKE = "sk-test-DO-NOT-USE-000000000000abcd"
FAKE_KREA = "krea-test-DO-NOT-USE-11111111wxyz"


@pytest.fixture(autouse=True)
def _clean_env():
    """No inherited key decides the outcome, and none of ours escapes.

    Snapshot-and-restore rather than monkeypatch.delenv: ``set_key`` assigns
    ``os.environ`` DIRECTLY — that is the point of it — so monkeypatch never
    sees the write and cannot undo it. A leaked KREA_API_KEY made test_doctor's
    "a bare machine" fixture report art as available, but only when this file
    ran first, which is the worst kind of failure to be handed.
    """
    import os
    before = {var: os.environ.get(var) for var in providers.env_vars()}
    for var in before:
        os.environ.pop(var, None)
    envfile.reset_cache()
    try:
        yield
    finally:
        for var, was in before.items():
            if was is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = was
        envfile.reset_cache()


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    from bgate_ui.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_the_wired_providers_are_declared(self):
        """A floor, not an exact set. The registry is meant to grow — a third
        provider landed while this file was being written and pinning equality
        here would have failed a correct addition."""
        assert set(providers.ids()) >= {"openai", "krea"}
        assert set(providers.env_vars()) >= {"OPENAI_API_KEY", "KREA_API_KEY"}
        assert len(set(providers.ids())) == len(providers.PROVIDERS), "duplicate id"
        assert len(set(providers.env_vars())) == len(providers.PROVIDERS), \
            "two providers claim one env var — set_key would fight itself"

    def test_every_provider_says_what_it_powers_and_where_to_get_a_key(self):
        """A card with no capability and no link is a dead end — the user is
        looking at this panel BECAUSE they do not have the key yet."""
        for one in providers.PROVIDERS:
            assert one.powers, one.id
            assert set(one.powers) <= set(providers.CAPABILITIES), one.id
            assert one.key_url.startswith("https://"), one.id
            assert one.help.strip(), one.id

    def test_there_is_no_getter_for_a_key(self):
        """The module's central promise. A function that returns the value is
        the thing that ends up interpolated into a log line."""
        for name in dir(providers):
            assert "get_key" not in name and "read_key" not in name, name

    def test_unknown_provider_names_the_legal_ones(self):
        with pytest.raises(providers.ProviderError) as exc:
            providers.by_id("midjourney")
        assert "openai" in str(exc.value) and "krea" in str(exc.value)


# ---------------------------------------------------------------------------
# The .env writer
# ---------------------------------------------------------------------------
class TestEnvWriter:
    def test_writing_preserves_every_unrelated_line(self, root):
        """The whole reason this is read-modify-write. A user's .env has
        comments explaining which account a key came from and variables no part
        of Builders Gate knows about."""
        original = (
            "# my keys, do not share\n"
            "SOME_OTHER_TOOL=abc123\n"
            "\n"
            "# openai, personal account\n"
            "OPENAI_API_KEY=old-value\n"
            "TRAILING_THING=42\n"
        )
        (root / ".env").write_text(original, encoding="utf-8")

        assert envfile.write_var(root, "OPENAI_API_KEY", FAKE) == "updated"

        text = (root / ".env").read_text(encoding="utf-8")
        assert "# my keys, do not share" in text
        assert "SOME_OTHER_TOOL=abc123" in text
        assert "# openai, personal account" in text
        assert "TRAILING_THING=42" in text
        assert "old-value" not in text
        assert f"OPENAI_API_KEY={FAKE}" in text
        # Order too: the key stays where the user put it, not at the bottom.
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert lines.index(f"OPENAI_API_KEY={FAKE}") == 3

    def test_a_new_variable_is_appended_not_substituted(self, root):
        (root / ".env").write_text("KEEP=1\n", encoding="utf-8")
        assert envfile.write_var(root, "KREA_API_KEY", FAKE_KREA) == "added"
        text = (root / ".env").read_text(encoding="utf-8")
        assert text.splitlines() == ["KEEP=1", f"KREA_API_KEY={FAKE_KREA}"]

    def test_a_commented_out_assignment_is_left_alone(self, root):
        """It is a note the user left themselves, not an assignment."""
        (root / ".env").write_text("#OPENAI_API_KEY=the-old-one\n", encoding="utf-8")
        envfile.write_var(root, "OPENAI_API_KEY", FAKE)
        text = (root / ".env").read_text(encoding="utf-8")
        assert "#OPENAI_API_KEY=the-old-one" in text
        assert f"OPENAI_API_KEY={FAKE}" in text

    def test_crlf_survives(self, root):
        """A .env last touched by Notepad is CRLF; rewriting it LF-only turns a
        one-line change into a whole-file diff."""
        (root / ".env").write_bytes(b"A=1\r\nOPENAI_API_KEY=old\r\n")
        envfile.write_var(root, "OPENAI_API_KEY", FAKE)
        raw = (root / ".env").read_bytes()
        assert raw.count(b"\r\n") == 2
        assert raw.count(b"\n") == 2, "a bare LF crept in beside the CRLFs"
        assert raw == b"A=1\r\nOPENAI_API_KEY=" + FAKE.encode() + b"\r\n"

    def test_whitespace_in_the_value_is_refused_without_echoing_it(self, root):
        with pytest.raises(envfile.EnvWriteError) as exc:
            envfile.write_var(root, "OPENAI_API_KEY", "sk-part one")
        assert "sk-part" not in str(exc.value)
        assert not (root / ".env").exists()

    def test_removing_takes_every_duplicate(self, root):
        """The loader takes the LAST assignment, so leaving one behind clears a
        key that is still in force."""
        (root / ".env").write_text(
            "OPENAI_API_KEY=one\nKEEP=yes\nOPENAI_API_KEY=two\n", encoding="utf-8")
        assert envfile.remove_var(root, "OPENAI_API_KEY") is True
        assert (root / ".env").read_text(encoding="utf-8").splitlines() == ["KEEP=yes"]

    def test_removing_something_absent_is_not_an_error(self, root):
        (root / ".env").write_text("KEEP=yes\n", encoding="utf-8")
        assert envfile.remove_var(root, "OPENAI_API_KEY") is False


# ---------------------------------------------------------------------------
# set_key / clear_key
# ---------------------------------------------------------------------------
class TestSetAndClear:
    def test_a_saved_key_is_live_in_this_process(self, root, monkeypatch):
        """The bug this prevents: load_project_env refuses to overwrite a name
        already in os.environ, so after the first save the file can never update
        the live value again — the user sets a key, nothing changes, and they
        conclude the panel is broken."""
        import os
        providers.set_key(root, "openai", "first-value-0000")
        assert os.environ["OPENAI_API_KEY"] == "first-value-0000"
        providers.set_key(root, "openai", FAKE)
        assert os.environ["OPENAI_API_KEY"] == FAKE

    def test_the_stamp_cache_is_dropped_on_write(self, root, monkeypatch):
        """A same-length replacement inside one filesystem tick changes neither
        mtime nor size — exactly the case reset_cache() exists for."""
        import os
        (root / ".env").write_text("OPENAI_API_KEY=aaaaaaaaaaaaaaaa\n", encoding="utf-8")
        envfile.load_project_env(root)
        assert os.environ["OPENAI_API_KEY"] == "aaaaaaaaaaaaaaaa"
        providers.set_key(root, "openai", "bbbbbbbbbbbbbbbb")
        monkeypatch.delenv("OPENAI_API_KEY")
        envfile.load_project_env(root)
        assert os.environ["OPENAI_API_KEY"] == "bbbbbbbbbbbbbbbb"

    def test_clearing_removes_it_from_the_file_and_the_process(self, root):
        import os
        providers.set_key(root, "krea", FAKE_KREA)
        providers.clear_key(root, "krea")
        assert "KREA_API_KEY" not in os.environ
        assert "KREA_API_KEY" not in (root / ".env").read_text(encoding="utf-8")

    def test_an_unprotected_env_is_gitignored_before_the_key_lands(self, root):
        """The incident in CLAUDE.md, in one click. A project adopted before the
        ignore rules shipped still has an unprotected .env."""
        (root / ".git").mkdir(exist_ok=True)
        (root / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        assert providers.env_is_ignored(root) is False

        row = providers.set_key(root, "openai", FAKE)

        assert row["gitignore"], "the write did not report touching .gitignore"
        assert providers.env_is_ignored(root) is True
        assert "*.tmp" in (root / ".gitignore").read_text(encoding="utf-8")

    def test_status_reports_presence_and_never_the_value(self, root):
        providers.set_key(root, "openai", FAKE)
        rows = providers.status(root)
        blob = json.dumps(rows)
        assert FAKE not in blob
        row = [r for r in rows if r["id"] == "openai"][0]
        assert row["configured"] is True
        assert row["last4"] == FAKE[-4:]
        assert row["source"] == "env_file"

    def test_a_shell_variable_shadowing_the_file_is_named(self, root, monkeypatch):
        """load_project_env lets the shell win. A panel reading os.environ alone
        would call a saved key 'in force' while a stale export is what actually
        gets sent."""
        (root / ".env").write_text(f"OPENAI_API_KEY={FAKE}\n", encoding="utf-8")
        monkeypatch.setenv("OPENAI_API_KEY", "from-the-shell-9999")
        row = [r for r in providers.status(root) if r["id"] == "openai"][0]
        assert row["source"] == "environment"
        assert row["last4"] == "9999"


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------
class TestEndpoints:
    def test_the_list_describes_every_provider(self, client):
        body = client.get("/api/providers").json()
        assert body["ok"] is True
        got = {p["id"] for p in body["data"]["providers"]}
        assert got == set(providers.ids())
        for row in body["data"]["providers"]:
            assert row["available"] or row["reason"], row["id"]

    def test_a_key_is_set_over_http_and_shows_as_set(self, client):
        r = client.post("/api/providers/openai/key", json={"key": FAKE})
        assert r.status_code == 200, r.text
        row = [p for p in r.json()["data"]["providers"] if p["id"] == "openai"][0]
        assert row["configured"] is True
        assert row["last4"] == FAKE[-4:]

    def test_no_endpoint_ever_returns_the_key(self, client):
        """The one assertion this whole module is for. Checked against the raw
        response text, so a field added later that carries the value fails here
        rather than passing a schema check nobody remembered to update."""
        client.post("/api/providers/openai/key", json={"key": FAKE})
        client.post("/api/providers/krea/key", json={"key": FAKE_KREA})
        for path in ("/api/providers", "/api/doctor", "/api/settings"):
            text = client.get(path).text
            assert FAKE not in text, path
            assert FAKE_KREA not in text, path
        # ...including the write's own response, and a refusal's.
        assert FAKE not in client.post("/api/providers/openai/key",
                                       json={"key": FAKE}).text
        bad = client.post("/api/providers/openai/key", json={"key": "has space"})
        assert bad.status_code == 400
        assert "has space" not in bad.text

    def test_an_agent_may_not_write_a_credential(self, client, monkeypatch):
        """Same rule as PATCH /api/settings: an agent that can write keys can
        hand itself a provider the human never paid for."""
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-7")
        assert client.post("/api/providers/openai/key",
                           json={"key": FAKE}).status_code == 403
        assert client.delete("/api/providers/openai/key").status_code == 403
        monkeypatch.delenv("BGATE_ACTOR")
        assert client.post("/api/providers/openai/key",
                           json={"key": FAKE}).status_code == 200

    def test_clearing_over_http(self, client):
        client.post("/api/providers/krea/key", json={"key": FAKE_KREA})
        body = client.delete("/api/providers/krea/key").json()
        row = [p for p in body["data"]["providers"] if p["id"] == "krea"][0]
        assert row["configured"] is False
        assert row["in_env_file"] is False

    def test_an_unknown_provider_is_a_400_that_lists_the_real_ones(self, client):
        r = client.post("/api/providers/midjourney/key", json={"key": FAKE})
        assert r.status_code == 400
        assert "krea" in r.text

    def test_a_missing_body_field_does_not_500(self, client):
        assert client.post("/api/providers/openai/key", json={}).status_code == 400


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------
class TestDoctorReportsPerProvider:
    def test_a_krea_only_project_is_green(self, root, monkeypatch):
        """The documented bug: doctor probed OPENAI_API_KEY alone, so a working
        Krea-only setup got MISS and a non-zero exit."""
        from bgate_core.runtime import doctor
        monkeypatch.setenv("KREA_API_KEY", FAKE_KREA)
        row = doctor.check(root, refresh=True)["art_key"]
        assert row["available"] is True
        assert row["path"] == "KREA_API_KEY"

    def test_no_key_at_all_names_every_provider_to_set(self, root):
        from bgate_core.runtime import doctor
        row = doctor.check(root, refresh=True)["art_key"]
        assert row["available"] is False
        assert "OPENAI_API_KEY" in row["reason"] and "KREA_API_KEY" in row["reason"]

    def test_the_row_never_carries_the_value(self, root, monkeypatch):
        from bgate_core.runtime import doctor
        monkeypatch.setenv("OPENAI_API_KEY", FAKE)
        assert FAKE not in json.dumps(doctor.check(root, refresh=True))
