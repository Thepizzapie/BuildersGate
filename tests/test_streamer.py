"""Streamer mode: the things that must not reach the screen.

Every test here is a leak that a plausible implementation has. They are written
as "this exact string does not survive" rather than "the function was called",
because the only failure that matters is a value on camera, and a mock cannot
tell you whether one is.
"""
from __future__ import annotations

import json

import pytest

from bgate_core import streamer


@pytest.fixture
def filt():
    """A redactor for a fixed, fake machine — never the test runner's own.

    scan_env=False on purpose: reading the real environment would make the
    suite pass or fail depending on whose keys are set, and a security test
    that is green because the machine happened to be bare is worthless.
    """
    return streamer.Redactor(
        home=r"C:\Users\marta", user="marta", host="marta-desktop",
        roots=[r"C:\Users\marta\Desktop\dungeon"],
        secrets=["hunter2-the-actual-key-value"], scan_env=False)


class TestTheSwitch:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv(streamer.ENV_VAR, raising=False)
        assert streamer.enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_on(self, monkeypatch, value):
        monkeypatch.setenv(streamer.ENV_VAR, value)
        assert streamer.enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
    def test_off(self, monkeypatch, value):
        monkeypatch.setenv(streamer.ENV_VAR, value)
        assert streamer.enabled() is False

    def test_a_typo_fails_ON_not_off(self, monkeypatch):
        """`BGATE_STREAMER=ture` must redact.

        The two failure modes are not symmetric: erring towards ON costs a
        session some readable paths, erring towards OFF costs a home directory
        in front of an audience who cannot unsee it."""
        monkeypatch.setenv(streamer.ENV_VAR, "ture")
        assert streamer.enabled() is True

    def test_read_live_not_cached_at_import(self, monkeypatch):
        """Flipping it mid-session works. Realising you are leaking is the
        entire use case, and "restart the server" is not available with an
        audience watching."""
        monkeypatch.setenv(streamer.ENV_VAR, "1")
        assert streamer.enabled() is True
        monkeypatch.setenv(streamer.ENV_VAR, "0")
        assert streamer.enabled() is False


class TestIdentity:
    def test_the_home_directory_goes(self, filt):
        out = filt.text(r"wrote C:\Users\marta\Desktop\notes.txt")
        assert "marta" not in out
        assert streamer.HOME_TOKEN in out

    def test_the_project_root_wins_over_home(self, filt):
        """It is UNDER home, so a naive ordering yields <home>\\Desktop\\... and
        the project placeholder never appears — which then breaks restore()."""
        out = filt.text(r"C:\Users\marta\Desktop\dungeon\game.tscn")
        assert out == r"<project>\game.tscn"

    def test_forward_slashes(self, filt):
        """pathlib, URLs and every traceback on Windows print it this way."""
        assert "marta" not in filt.text("C:/Users/marta/Desktop/dungeon/x.gd")

    def test_json_escaped(self, filt):
        """The dashboard sends JSON. A path inside it is double-backslashed,
        and a filter that only knows the typed spelling passes its unit test
        and leaks in the browser."""
        blob = json.dumps({"root": r"C:\Users\marta\Desktop\dungeon"})
        assert "marta" not in filt.text(blob)

    def test_percent_encoded(self, filt):
        """The preview endpoint takes paths in a query string."""
        assert "marta" not in filt.text("/api/preview?path=C:%5CUsers%5Cmarta%5Cx.png")

    def test_case_insensitive(self, filt):
        """argv, the DB and a traceback disagree about the drive letter and the
        capitalisation of Users; Windows does not care and neither can this."""
        assert "Marta" not in filt.text(r"c:\users\Marta\Desktop\a.txt")

    def test_somebody_elses_home_too(self, filt):
        """Agent logs and pasted tracebacks carry other people's paths, and
        those dox their owner exactly as hard."""
        out = filt.text("/home/bryan/games/thing.glb")
        assert "bryan" not in out
        assert out == "/home/<user>/games/thing.glb"

    def test_the_bare_username(self, filt):
        assert "marta" not in filt.text("git author: marta pushed 3 commits")

    def test_the_hostname(self, filt):
        """Usually the owner's name plus a device."""
        assert "marta-desktop" not in filt.text("host=marta-desktop ok")

    def test_email(self, filt):
        out = filt.text("Co-Authored-By: someone@example.com")
        assert "someone@example.com" not in out

    def test_a_common_username_is_not_substituted_bare(self):
        """A user called `test` or `admin` would otherwise rewrite prose into
        nonsense — "run the <user> suite" — and an unreadable dashboard is a
        dashboard the user turns the filter off to read."""
        f = streamer.Redactor(home="/home/test", user="test", host="box",
                              scan_env=False)
        assert "run the test suite" == f.text("run the test suite")
        assert "test" not in f.text("/home/test/x")  # the PATH still goes


class TestSecrets:
    @pytest.mark.parametrize("secret", [
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAA",
        "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "github_pat_AAAAAAAAAAAAAAAAAAAAAA",
        "hf_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        # No digit groups. The realistic spelling — xoxb-<workspace id>-<blob>
        # — is exactly what GitHub's push protection matches, so a fixture
        # written to look real blocks the push of the filter that redacts it.
        # The prefix is the whole discriminator; the digits taught us nothing.
        "xoxb-NOTAREALTOKEN-AAAAAAAAAAAAAAAAAAAA",
        "glpat-AAAAAAAAAAAAAAAAAAAA",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.AAAAAAAAAAAAAAAA",
    ])
    def test_vendor_shapes_die_without_being_known(self, filt, secret):
        """A key in an agent log did not come from this machine's environment,
        so there is nothing to compare it against — shape is all there is."""
        out = filt.text(f"provider said: {secret} was rejected")
        assert secret not in out
        assert streamer.SECRET_TOKEN in out

    def test_a_known_value_dies_whatever_shape_it_is(self, filt):
        """The primary defence. Literal match on this process's own key values
        catches the vendor nobody has written a pattern for yet."""
        assert "hunter2" not in filt.text("Authorization: hunter2-the-actual-key-value")

    def test_the_assignment_form(self, filt):
        out = filt.text("KREA_API_KEY=abcdef123456789")
        assert "abcdef123456789" not in out
        assert "KREA_API_KEY" in out, "the NAME is the useful half — keep it"

    def test_bearer(self, filt):
        assert "AAAAbbbbCCCCdddd" not in filt.text("Bearer AAAAbbbbCCCCdddd")

    def test_credentials_in_a_url(self, filt):
        out = filt.text("https://marta:hunter2@github.com/x/y.git")
        assert "hunter2" not in out
        assert "marta" not in out, "the username half doxes too"

    def test_private_key_blocks(self, filt):
        pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nAAAA\n"
               "-----END RSA PRIVATE KEY-----")
        out = filt.text(f"loaded:\n{pem}\ndone")
        assert "MIIEow" not in out
        assert out.startswith("loaded:") and out.endswith("done")

    def test_secrets_are_not_reversible(self, filt):
        """There is no restore for a secret, because a reverse map IS the
        secret. Identity round-trips; credentials do not come back."""
        scrubbed = filt.text("OPENAI_API_KEY=sk-AAAAAAAAAAAAAAAAAAAAAAAA")
        assert "sk-AAAA" not in filt.restore(scrubbed)

    def test_a_boolean_is_not_a_secret(self, filt):
        """`doctor` reports `openai_key: true` — that a key EXISTS is the
        opposite of a leak, and blanking it turns a working status row into an
        unreadable one."""
        assert filt.text('"api_key_set": true') == '"api_key_set": true'
        assert filt.text('"token": null') == '"token": null'


class TestRoundTrip:
    def test_a_path_survives_out_and_back(self, filt):
        """The UI sends paths back. Scrubbed on the way out and not restored on
        the way in is a broken button, and a broken button is how a feature
        like this gets switched off."""
        real = r"C:\Users\marta\Desktop\dungeon\assets\hero.glb"
        assert filt.restore(filt.text(real)) == real

    def test_nested_structures(self, filt):
        payload = {"root": r"C:\Users\marta\Desktop\dungeon",
                   "steps": [{"cmd": r"blender C:\Users\marta\x.blend"}]}
        out = filt.scrub(payload)
        assert "marta" not in json.dumps(out)
        assert filt.restore(out)["root"] == r"C:\Users\marta\Desktop\dungeon"

    def test_dict_keys_are_scrubbed(self, filt):
        """The doctor report and the project registry are both keyed BY PATH.
        A filter that only walked values would print the home directory in the
        one place a viewer is most likely to be reading."""
        out = filt.scrub({r"C:\Users\marta\Desktop\dungeon": {"ok": True}})
        assert "marta" not in json.dumps(out)

    def test_two_projects_stay_distinct(self):
        """With one root the placeholder reads <project>; with several they
        have to stay apart or restore() cannot tell which is which."""
        f = streamer.Redactor(home="/home/x", user="x", host="h", scan_env=False,
                              roots=["/games/alpha", "/games/beta"])
        out = f.scrub({"a": "/games/alpha/x", "b": "/games/beta/y"})
        assert out["a"] != out["b"]
        assert f.restore(out) == {"a": "/games/alpha/x", "b": "/games/beta/y"}

    def test_a_nested_root_wins_over_its_parent(self):
        f = streamer.Redactor(home="/home/x", user="x", host="h", scan_env=False,
                              roots=["/games", "/games/rpg"])
        assert f.restore(f.scrub("/games/rpg/main.tscn")) == "/games/rpg/main.tscn"

    def test_non_strings_pass_through(self, filt):
        payload = {"n": 3, "ok": True, "none": None, "f": 1.5}
        assert filt.scrub(payload) == payload


class TestTheIndicator:
    def test_status_answers_when_off(self, monkeypatch):
        """A filter that is quietly off looks exactly like one that is on and
        working, right up until it doesn't."""
        monkeypatch.delenv(streamer.ENV_VAR, raising=False)
        assert streamer.status()["on"] is False

    def test_status_never_lists_what_it_protects(self, filt):
        """A panel enumerating the secrets it is hiding is the leak it exists
        to prevent."""
        blob = json.dumps(filt.status())
        assert "hunter2" not in blob
        assert "marta" not in blob
        assert filt.status()["known_secrets"] == 1

    def test_scrub_is_a_no_op_when_off(self, monkeypatch):
        monkeypatch.delenv(streamer.ENV_VAR, raising=False)
        payload = {"root": r"C:\Users\marta\x"}
        assert streamer.scrub(payload) == payload
