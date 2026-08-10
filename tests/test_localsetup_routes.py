"""The HTTP surface for local generators and coding-agent CLIs.

WHAT HAS TO HOLD HERE, in order of what it would cost to get wrong:

  * WRITING IS HUMAN-ONLY. Both writes matter and the second one is the bigger
    of the two: a runtime config value repoints what every subsequent generation
    runs, and an MCP registration changes what every future session of that CLI
    can do, on the whole machine, outside this project. An agent that could do
    the second could widen its own successors' capabilities.
  * THERE IS NO START ENDPOINT. Asserted against the route table rather than by
    trying one, because the failure mode is somebody adding it later and this is
    the test that should stop them.
  * The reads return the VALUE. That is the deliberate inversion of the
    credentials endpoint and it is what makes a path checkable.
"""
from __future__ import annotations

import json
import sys

import pytest
from fastapi.testclient import TestClient

from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    for name in ("BGATE_COMFY_URL", "BGATE_COMFY_T2I_WORKFLOW",
                 "BGATE_COMFY_EDIT_WORKFLOW", "BGATE_LOCAL_IMAGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return TestClient(app)


@pytest.fixture()
def as_agent(monkeypatch):
    """The signal api.current_actor fails closed on — set by every dispatch."""
    monkeypatch.setenv("BGATE_WORK_ITEM", "41")


class TestRead:
    def test_the_list_carries_every_runtime_with_a_stage(self, client):
        body = client.get("/api/local/runtimes?probe=0").json()
        assert body["ok"] is True
        rows = body["data"]["runtimes"]
        assert rows
        for row in rows:
            assert row["stage"] in body["data"]["stages"]
            assert "what" in row and len(row["what"]) > 40

    def test_capabilities_come_from_the_shared_vocabulary(self, client):
        body = client.get("/api/local/runtimes?probe=0").json()["data"]
        from bgate_core import providers
        assert body["capabilities"] == providers.CAPABILITIES

    def test_a_configured_path_comes_back_in_full(self, client, root, tmp_path):
        """Unlike a key. A path you cannot read is a path you cannot check."""
        wf = tmp_path / "t2i.json"
        wf.write_text("{}", encoding="utf-8")
        client.post("/api/local/runtimes/comfy-image/config",
                    json={"env": "BGATE_COMFY_T2I_WORKFLOW", "value": str(wf)})
        body = client.get("/api/local/runtimes?probe=0").json()["data"]
        field = [f for f in body["runtimes"][0]["fields"]
                 if f["env"] == "BGATE_COMFY_T2I_WORKFLOW"][0]
        assert field["value"] == str(wf)

    def test_an_unknown_runtime_is_a_404_that_lists_the_real_ones(self, client):
        got = client.get("/api/local/runtimes/nope")
        assert got.status_code == 404
        assert "comfy-image" in got.json()["error"]["message"]

    def test_inspect_degrades_when_nothing_is_running(self, client, monkeypatch):
        monkeypatch.setenv("BGATE_COMFY_URL", "http://127.0.0.1:9")
        body = client.get("/api/local/runtimes/comfy-image/inspect").json()["data"]
        assert body["server"]["ok"] is False
        # And it does NOT then claim the node query failed for a version
        # reason — a server that is off is not a version difference.
        assert "catalogue" not in body

    def test_the_agents_endpoint_reports_wiring_not_just_installation(self,
                                                                      client):
        body = client.get("/api/local/agents").json()["data"]
        assert body["interpreter"] == sys.executable
        for row in body["runners"]:
            assert "state" in row["mcp"]
            assert row["mcp"]["expected_command"] == sys.executable


class TestWrite:
    def test_a_value_saves_and_the_response_is_the_fresh_truth(self, client,
                                                               tmp_path):
        wf = tmp_path / "t2i.json"
        wf.write_text("{}", encoding="utf-8")
        got = client.post("/api/local/runtimes/comfy-image/config",
                          json={"env": "BGATE_COMFY_T2I_WORKFLOW",
                                "value": str(wf)})
        assert got.status_code == 200
        data = got.json()["data"]
        assert data["applied"]["write"] in ("created", "added", "updated")
        # Repainted from what is now TRUE, not from what was sent: a path can
        # save correctly and the runtime still not be usable.
        assert data["runtimes"][0]["stage"] != "unconfigured"

    def test_clearing_removes_it(self, client, tmp_path):
        wf = tmp_path / "t2i.json"
        wf.write_text("{}", encoding="utf-8")
        client.post("/api/local/runtimes/comfy-image/config",
                    json={"env": "BGATE_COMFY_T2I_WORKFLOW", "value": str(wf)})
        got = client.request(
            "DELETE",
            "/api/local/runtimes/comfy-image/config?env=BGATE_COMFY_T2I_WORKFLOW")
        assert got.status_code == 200
        assert got.json()["data"]["applied"]["write"] == "removed"

    def test_a_bad_address_is_a_400_with_the_fix_in_it(self, client):
        got = client.post("/api/local/runtimes/comfy-image/config",
                          json={"env": "BGATE_COMFY_URL", "value": "127.0.0.1"})
        assert got.status_code == 400
        assert "http://" in got.json()["error"]["message"]

    def test_a_malformed_body_says_what_to_send(self, client):
        got = client.post("/api/local/runtimes/comfy-image/config", json={})
        assert got.status_code == 400
        assert "env" in got.json()["error"]["message"]


class TestHumanOnly:
    def test_an_agent_cannot_change_local_generator_setup(self, client,
                                                          as_agent, tmp_path):
        got = client.post("/api/local/runtimes/comfy-image/config",
                          json={"env": "BGATE_COMFY_URL",
                                "value": "http://127.0.0.1:8188"})
        assert got.status_code == 403
        assert got.json()["error"]["code"] == "forbidden"

    def test_an_agent_cannot_clear_it_either(self, client, as_agent):
        got = client.request(
            "DELETE",
            "/api/local/runtimes/comfy-image/config?env=BGATE_COMFY_URL")
        assert got.status_code == 403

    def test_an_agent_cannot_register_an_mcp_server(self, client, as_agent):
        """The strongest case on this page: the write lands OUTSIDE the project
        and changes what every future session of that CLI can reach."""
        got = client.post("/api/local/agents/claude/register")
        assert got.status_code == 403

    def test_an_agent_cannot_remove_one(self, client, as_agent):
        assert client.request(
            "DELETE", "/api/local/agents/claude/register").status_code == 403

    def test_an_agent_cannot_execute_the_verify_probe(self, client, as_agent):
        assert client.post("/api/local/agents/claude/verify").status_code == 403

    def test_reads_stay_open_to_an_agent(self, client, as_agent):
        """An agent SHOULD be able to find out that local generation is not
        running — that is what it tells the human. Only the writes are gated."""
        assert client.get("/api/local/runtimes?probe=0").status_code == 200
        assert client.get("/api/local/agents").status_code == 200


class TestNoLauncher:
    def test_there_is_no_endpoint_that_starts_or_stops_anything(self):
        """Structural, because the risk is a start button arriving later. The
        dashboard is not a process manager for services the user owns: the
        command is unknowable, the failures are all on the far side of it, and
        an orphan holding 8 GB of VRAM is worse than a sentence telling somebody
        to start it themselves."""
        paths = [r.path for r in app.routes if hasattr(r, "path")
                 and r.path.startswith("/api/local")]
        assert paths
        for path in paths:
            assert not path.endswith("/start")
            assert not path.endswith("/stop")
            assert "kill" not in path

    def test_the_route_module_spawns_nothing_itself(self):
        import ast
        import inspect

        from bgate_ui.routes import localsetup

        tree = ast.parse(inspect.getsource(localsetup))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name != "subprocess" for a in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module != "subprocess"


class TestMcpParity:
    def test_the_read_half_exists_as_a_tool(self):
        """An agent asked to generate art must be able to find out that the
        local path is configured but not running, and say so, instead of
        failing at generation time."""
        from bgate_mcp import server

        assert hasattr(server, "local_status")

    def test_and_the_write_half_deliberately_does_not(self):
        """Parity does NOT apply to the writes, and this test is the record of
        why: they are human-only at the HTTP layer, and a tool that did the same
        thing would be that gate with a hole in it. Same reason there is no
        set_api_key tool.
        """
        from bgate_mcp import server

        source = json.dumps(sorted(
            name for name in vars(server) if name.startswith("local_")))
        assert "local_set" not in source
        assert "local_configure" not in source
        assert "local_start" not in source
