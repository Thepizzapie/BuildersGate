"""Streamer mode through the real dashboard, not the redactor in isolation.

bgate_core/streamer.py passing its own tests proves the regexes work. It proves
nothing about whether the filter is INSTALLED, whether it sees the routes that
matter, whether it breaks the page, or whether HTML — which carries the auth
token — is correctly left alone. Those are the failures that ship.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bgate_core import streamer


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A real scaffolded project under a fake home, served by the real app."""
    from bgate_core import scaffold

    home = tmp_path / "Users" / "streamer"
    root = home / "Desktop" / "mygame"
    root.mkdir(parents=True)
    scaffold.new_project(root, "mygame", kind="2d")

    monkeypatch.setenv("BGATE_ROOT", str(root))
    monkeypatch.setenv("BGATE_NO_AUTH", "1")  # the guard is not what is on test
    monkeypatch.setenv(streamer.ENV_VAR, "1")
    monkeypatch.setattr(streamer, "_ACTIVE", None)  # no bleed between tests

    # THERE ARE TWO CACHES, AND CLEARING ONE IS WHY THIS TEST WAS FLAKY.
    # streamer._ACTIVE holds the redactor; bgate_ui.redact._cache separately
    # holds the ANSWER TO "is the filter on at all", for 2 seconds, so that
    # flipping the panel switch is felt while you are looking at the screen.
    # Any earlier test that touched the dashboard with streamer mode off leaves
    # that cache saying False, and if this fixture runs inside the 2-second
    # window the filter never engages — the response comes back completely
    # unredacted and the failure reads as a broken redactor rather than a stale
    # boolean. It went unnoticed while the suite happened to be slow enough
    # between the two; adding ~95 tests elsewhere changed the timing and it
    # started failing. Resetting the clock, not the value, is what invalidate()
    # is for.
    from bgate_ui import redact as _redact

    _redact.invalidate()
    monkeypatch.setattr(_redact, "_cache", (0.0, False))

    # The redactor reads identity from the environment at construction, so the
    # fake home has to be there before the first request builds one.
    monkeypatch.setenv("USERNAME", "streamer")
    monkeypatch.setenv("USER", "streamer")
    monkeypatch.setattr(streamer, "_home", lambda: str(home))

    from bgate_ui.app import app
    with TestClient(app) as client:
        yield client, root, home


class TestTheFilterIsActuallyInstalled:
    def test_the_project_path_does_not_reach_the_page(self, project):
        """/api/state is what the dashboard paints on load, and it carries the
        root. The single most-read leak on the whole surface.

        Asserted on the PARSED body, not the raw text. `str(root) not in text`
        is vacuously true for every path in this response: JSON escapes them to
        C:\\\\Users\\\\..., so that assertion passes with no filter installed at
        all. It did, when this test was first written."""
        client, root, home = project
        raw = client.get("/api/state").text
        data = json.loads(raw)
        assert str(root) not in json.dumps(data)          # escaped spelling
        assert str(root).replace("\\", "\\\\") not in raw  # and the wire form
        assert "streamer" not in raw
        assert data["root"] == streamer.PROJECT_TOKEN, (
            "the filter must have actually substituted something — a test that "
            "only checks for absence passes when the field is missing")

    def test_the_users_OTHER_projects_go_too(self, project):
        """/api/state ships a `known` map of every project ever registered on
        this machine, keyed by name and valued by absolute path. Someone
        streaming one game leaks the paths — and therefore the names — of every
        other one, including the ones under a real home directory."""
        client, _, _ = project
        data = json.loads(client.get("/api/state").text)
        for path in (data.get("known") or {}).values():
            assert "Users\\" not in path and "/Users/" not in path, path
            assert "/home/" not in path, path

    def test_the_status_endpoint_says_it_is_on(self, project):
        client, _, _ = project
        data = client.get("/api/streamer").json()
        payload = data.get("data", data)
        assert payload["on"] is True

    def test_off_means_untouched(self, project, monkeypatch):
        """The mode must be a no-op when off — not a differently-shaped
        response, not a reordered dict. Off is the default for every existing
        user and it cannot change what they see."""
        from bgate_ui import redact

        client, root, _ = project
        monkeypatch.setenv(streamer.ENV_VAR, "0")
        monkeypatch.setattr(streamer, "_ACTIVE", None)
        # The middleware caches its answer for a beat, so a flip needs the same
        # invalidation the settings route does. Calling it here is not the test
        # cheating — it is the test asserting that the invalidation hook works,
        # which is the only reason a toggle feels immediate.
        redact.invalidate()
        assert json.loads(client.get("/api/state").text)["root"] == str(root)


class TestWhatItMustNotBreak:
    def test_html_keeps_its_auth_token(self, project):
        """index.html carries `window.BGATE_TOKEN='...'`, which matches the
        assignment pattern exactly. Scrubbing HTML logs the browser out of its
        own dashboard — the page loads, every fetch 401s, and the failure looks
        like a broken server rather than a redaction bug."""
        client, _, _ = project
        html = client.get("/").text
        assert "BGATE_TOKEN" in html
        assert streamer.SECRET_TOKEN not in html

    def test_json_is_still_valid_json(self, project):
        """Substitutions change the body length. A stale Content-Length
        truncates the response and the page fails to parse its own payload —
        which presents as a blank dashboard, not as an error."""
        client, _, _ = project
        response = client.get("/api/state")
        json.loads(response.text)  # raises if the body was truncated
        # Either the header is gone (the filtered body is chunked, and its final
        # length is not knowable until the last chunk) or it agrees with the
        # bytes. A header that survives a substitution UNCHANGED is the bug:
        # the browser reads exactly that many bytes and parses a fragment.
        declared = response.headers.get("content-length")
        assert declared is None or declared == str(len(response.content))

    def test_a_path_posted_back_still_works(self, project):
        """The page renders `<project>\\x.png` and posts it back. Without the
        inbound restore the handler gets a path that does not exist, and every
        button that round-trips one breaks."""
        client, root, _ = project
        from bgate_core import streamer as st
        filt = st.active([str(root)])
        assert filt is not None
        scrubbed = filt.text(str(root / "project.godot"))
        assert str(root) not in scrubbed
        assert filt.restore(scrubbed) == str(root / "project.godot")

    def test_a_json_mutation_does_not_500(self, project):
        """The inbound restore rewrites the request body, and doing that by
        replacing `request._receive` is where this broke: a replacement that
        answers `http.request` on EVERY call feeds a second body to
        BaseHTTPMiddleware's disconnect listener, which raises
        `RuntimeError: Unexpected message received`.

        Every JSON POST and PATCH 500s while GET traffic stays perfectly
        healthy — so the dashboard paints correctly and every button is dead.
        No unit test on the redactor can see this; it only appears with a real
        ASGI stack underneath."""
        client, _, _ = project
        response = client.patch("/api/settings", json={"privacy.streamer": True})
        assert response.status_code < 500, response.text

    def test_the_panel_switch_actually_drives_the_filter(self, project,
                                                         monkeypatch):
        """On and off from the settings endpoint, with no restart and no env
        var — the whole point of putting it in the panel."""
        monkeypatch.delenv(streamer.ENV_VAR, raising=False)
        client, root, _ = project

        client.patch("/api/settings", json={"privacy.streamer": False})
        assert json.loads(client.get("/api/state").text)["root"] == str(root)

        client.patch("/api/settings", json={"privacy.streamer": True})
        assert json.loads(client.get("/api/state").text)["root"] == \
            streamer.PROJECT_TOKEN

    def test_images_are_not_run_through_a_text_filter(self, project):
        """A PNG is bytes. Decoding one as UTF-8 and substituting inside it
        corrupts the asset previews, and a filter that breaks previews is one
        the user switches off."""
        client, root, _ = project
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        (root / "shot.png").write_bytes(png)
        response = client.get("/api/preview", params={"path": "shot.png"})
        if response.status_code == 200:
            assert response.content.startswith(b"\x89PNG")


class TestTheHardSurfaces:
    def test_an_error_body_is_scrubbed_too(self, project):
        """Tracebacks and 4xx bodies quote the path they failed on, and an
        error is exactly when a viewer is staring at the screen."""
        client, root, _ = project
        response = client.get("/api/preview", params={"path": "../../secret.png"})
        assert str(root) not in response.text
        assert "streamer" not in response.text

    def test_a_key_in_a_response_dies(self, project, monkeypatch):
        """Adapters echo the request they sent. A provider error carrying a key
        reaches /api/state through the job record."""
        client, root, _ = project
        monkeypatch.setattr(streamer, "_ACTIVE", None)
        filt = streamer.active([str(root)])
        assert filt is not None
        out = filt.text("openai: sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAA rejected")
        assert "sk-proj-" not in out

    def test_dict_keys_keyed_by_path(self, project):
        """The doctor report is keyed BY absolute path in places, and a filter
        walking only values prints the home directory anyway."""
        client, root, home = project
        body = client.get("/api/doctor").text
        assert str(home) not in body
