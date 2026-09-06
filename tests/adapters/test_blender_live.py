"""The live-Blender adapter: the kit inside a Blender that stays open.

The protocol tests run against a fake add-on server in a thread that speaks the
real wire format (null-delimited JSON, one request per connection, fresh
`result = {}` namespace) so they need no Blender. The last test talks to a
real live Blender when one is listening on the default port and is skipped
otherwise - the add-on has to be started by a human in Blender's preferences.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from bgate_adapters import blender_live as live


class FakeAddon:
    """The add-on's server, minus Blender: executes code in a namespace whose
    `bpy` is a stub, records every request, and remembers which modules the
    kit installer registered so `ensure_kit` can be seen to gate."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.requests: list[str] = []
        self.modules: set[str] = set()
        self.calls = 0
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            with conn:
                buf = bytearray()
                while b"\0" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                req = json.loads(bytes(buf[:buf.index(b"\0")]))
                self.calls += 1
                self.requests.append(req["code"])
                conn.sendall((json.dumps(self._execute(req["code"])) + "\0").encode())

    def _execute(self, code: str) -> dict:
        # The kit installer is recognised, not executed: the real kit needs bmesh.
        if "types.ModuleType" in code and live.KIT_MODULE in code:
            self.modules.update({live.KIT_MODULE, live.LIVE_MODULE})
            return {"status": "ok", "result": {"kit_loaded": True, "kit_functions": 40}}
        if "in sys.modules" in code and "result = {'loaded'" in code:
            return {"status": "ok", "result": {"loaded": live.KIT_MODULE in self.modules}}
        if "bpy.app.version_string" in code:
            return {"status": "ok", "result": {"version": "5.2.1", "background": False,
                                               "kit_loaded": live.KIT_MODULE in self.modules, "file": ""}}
        if "raise RuntimeError" in code:
            return {"status": "error", "message": "Traceback (most recent call last):\n  ...\nRuntimeError: boom",
                    "stdout": "before the boom\n"}
        # A wrapped kit script: report what the user's body returned.
        user = {}
        if "result = {'answer': 42}" in code:
            user = {"answer": 42}
        res = {"user": user}
        if "_bgl.report()" in code:
            res["scene"] = {"objects": [], "totals": {"tris": 0}}
        if "_bgl.export_glb(" in code:
            path = code.split("_bgl.export_glb(", 1)[1].split(")", 1)[0]
            res["export"] = {"exported": "nowhere" not in path, "path": path}
        if "_bgl.view(" in code:
            res["view"] = {"captured": True, "how": "viewport"}
        return {"status": "ok", "result": res, "stdout": "hello from blender\n"}

    def close(self):
        self._stop = True
        self.thread.join(timeout=2)
        self.sock.close()


@pytest.fixture
def addon():
    server = FakeAddon()
    yield server
    server.close()


class TestProtocol:
    def test_nothing_listening_is_a_clear_error_not_a_hang(self):
        got = live.available(port=1)
        assert got["available"] is False and "no live Blender" in got["error"]
        run = live.run("pass", port=1, timeout=2)
        assert run["ok"] is False and run["live"] is False and "Start MCP Bridge Server" in run["error"]

    def test_available_reads_the_version(self, addon):
        got = live.available(port=addon.port)
        assert got["available"] and got["version"] == "5.2.1" and got["kit_loaded"] is False

    def test_kit_is_installed_once_then_reused(self, addon):
        a = live.run("result = {'answer': 42}", port=addon.port)
        assert a["ok"] and a["result"] == {"answer": 42} and a["kit_installed_now"] is True
        b = live.run("result = {'answer': 42}", port=addon.port)
        assert b["ok"] and b["kit_installed_now"] is False
        installs = [c for c in addon.requests if "types.ModuleType" in c]
        assert len(installs) == 1
        # The installer carries the whole kit and the runner's export helpers.
        assert "def bg_hull" in installs[0] and "def _flatten_procedural_inputs" in installs[0]
        assert "def export_glb" in installs[0] and "def view" in installs[0]

    def test_scripts_run_with_the_kit_in_scope_and_state_kept(self, addon):
        live.run("x = 1", port=addon.port)
        wrapped = addon.requests[-1]
        assert f"from {live.KIT_MODULE} import *" in wrapped
        assert "bg_wipe()" not in wrapped, "the live session must never be wiped implicitly"
        assert "    x = 1" in wrapped  # the body is indented into the runner function

    def test_stdout_export_and_view_come_back(self, addon):
        got = live.run("pass", export_glb="C:/tmp/out.glb", view="C:/tmp/view.png", port=addon.port)
        assert got["ok"] and got["stdout"].startswith("hello") and got["export"]["exported"]
        assert got["view"]["how"] == "viewport" and "scene" in got

    def test_a_missing_export_file_fails_the_call(self, addon):
        got = live.run("pass", export_glb="C:/nowhere/out.glb", port=addon.port)
        assert got["ok"] is False and "wrote no file" in got["error"]

    def test_errors_carry_the_last_line_and_the_stdout_before_them(self, addon):
        got = live.run("raise RuntimeError('boom')", port=addon.port)
        assert got["ok"] is False and got["error"] == "RuntimeError: boom"
        assert "before the boom" in got["stdout"] and "Traceback" in got["traceback"]

    def test_big_requests_cross_the_wire_whole(self, addon):
        # The kit installer is ~160 KB; the framing must not depend on one recv.
        live.run("pass", port=addon.port)
        installer = next(c for c in addon.requests if "types.ModuleType" in c)
        assert len(installer) > 100_000


@pytest.mark.skipif(not live.available().get("available"), reason="no live Blender on the default port")
class TestRealLiveBlender:
    @pytest.mark.slow
    def test_the_kit_runs_and_the_scene_persists(self, tmp_path):
        a = live.run("bg_wipe()\nrock = bg_rock('LiveRock', seed=2, detail=3)\nresult = {'name': rock.name}")
        assert a["ok"], a
        b = live.run("result = {'still_there': 'LiveRock' in bpy.data.objects}")
        assert b["ok"] and b["result"]["still_there"] is True
        e = live.export_glb(tmp_path / "live.glb")
        assert e["ok"] and (tmp_path / "live.glb").exists()
