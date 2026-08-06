"""The aggregate dependency check, and the per-call project root.

Two things are being pinned here. First: `doctor.check` answers in one fixed
shape whether the binaries are there or not, and never raises — a broken probe
must degrade to a row, because the caller is asking precisely when things are
broken. Second: a tool call's project comes from the CALL, not from whatever
some earlier call left behind on the server.

Every probe is monkeypatched. This suite must not depend on what happens to be
installed on the machine running it, and must never spawn Blender.
"""
from __future__ import annotations

import json

import pytest

from bgate_core import db, doctor, project
from bgate_mcp import server


@pytest.fixture(autouse=True)
def cold_cache():
    """doctor caches for a few seconds; each test starts from nothing."""
    doctor._cache.clear()
    yield
    doctor._cache.clear()


@pytest.fixture()
def everything_present(monkeypatch):
    """Every dependency installed and new enough, without touching the disk."""
    monkeypatch.setattr("shutil.which",
                        lambda name: f"C:/bin/{name}.exe" if name in
                        ("ffmpeg", "ffprobe") else None)
    monkeypatch.setattr(doctor, "_banner", lambda exe: "ffmpeg version 7.1 Copyright")
    monkeypatch.setattr("bgate_adapters.blender.available",
                        lambda: {"available": True, "path": "C:/blender.exe"})
    monkeypatch.setattr("bgate_adapters.blender.version",
                        lambda: {"path": "C:/blender.exe", "version": "Blender 4.5.0"})
    monkeypatch.setattr("bgate_adapters.godot.available",
                        lambda: {"available": True, "path": "C:/godot.exe"})
    monkeypatch.setattr("bgate_adapters.godot.version",
                        lambda: {"path": "C:/godot.exe", "version": "4.7.1.stable"})
    monkeypatch.setattr("bgate_adapters.godot.export_templates",
                        lambda platform="web": {
                            "available": True, "path": "C:/templates/4.7.1.stable",
                            "version": "4.7.1.stable", "files": ["web_release.zip"],
                            "reason": ""})
    monkeypatch.setattr("bgate_adapters.transcribe.available",
                        lambda: {"available": True, "python": "C:/py.exe",
                                 "version": "1.2.1"})
    # A GPU, stubbed like everything else here. The imageto3d row asks
    # nvidia-smi, and `shutil.which` above answers None for it — so on a
    # machine WITH a GPU this fixture was accidentally honest and on a CI
    # runner it was not, which is exactly how this test passed on two
    # developer machines and failed on every runner.
    monkeypatch.setattr("bgate_adapters.imageto3d.doctor_row",
                        lambda: {"available": True, "path": "nvidia-smi",
                                 "version": "NVIDIA GeForce RTX 3060 (12.0 GB)",
                                 "min_required": "", "reason": ""})
    # The local 2D leg, stubbed for the same reason: it probes a loopback
    # ComfyUI, and whether one is running is a property of the developer's
    # machine rather than of the code under test.
    monkeypatch.setattr("bgate_adapters.localgen.doctor_row",
                        lambda: {"name": "local_image", "available": True,
                                 "detail": "ComfyUI at http://127.0.0.1:8188, "
                                           "model sdxl", "optional": True})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")


@pytest.fixture()
def nothing_present(monkeypatch):
    """A bare machine: no binaries, no key, no whisper."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("bgate_adapters.blender.available",
                        lambda: {"available": False, "reason": "Blender not found."})
    monkeypatch.setattr("bgate_adapters.godot.available",
                        lambda: {"available": False, "reason": "Godot not found."})
    monkeypatch.setattr("bgate_adapters.godot.export_templates",
                        lambda platform="web": {
                            "available": False, "path": "", "version": "",
                            "reason": "no web export templates"})
    monkeypatch.setattr("bgate_adapters.transcribe.available",
                        lambda: {"available": False, "python": "C:/py.exe",
                                 "reason": "faster-whisper not installed"})
    monkeypatch.setattr("bgate_adapters.imageto3d.doctor_row",
                        lambda: {"available": False, "path": "", "version": "",
                                 "min_required": "",
                                 "reason": "nvidia-smi not found"})
    monkeypatch.setattr("bgate_adapters.localgen.doctor_row",
                        lambda: {"name": "local_image", "available": False,
                                 "detail": "BGATE_COMFY_T2I_WORKFLOW is not set",
                                 "optional": True})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
ROW_KEYS = {"available", "path", "version", "min_required", "reason"}


def test_every_dependency_answers_in_the_same_shape(everything_present):
    report = doctor.check()
    assert set(report) == set(doctor.CHECKS)
    for name, row in report.items():
        assert set(row) == ROW_KEYS, name
        assert isinstance(row["available"], bool), name


def test_present_binaries_report_available_with_a_path(everything_present):
    report = doctor.check()
    assert all(row["available"] for row in report.values()), doctor.summary(report)
    assert report["blender"]["version"] == "Blender 4.5.0"
    assert report["godot"]["path"] == "C:/godot.exe"
    assert report["ffmpeg"]["version"] == "7.1"
    assert report["ffprobe"]["path"] == "C:/bin/ffprobe.exe"
    assert report["whisper"]["version"] == "1.2.1"
    assert report["python"]["available"]
    # The key is reported as present, never echoed back.
    assert "sk-test-key" not in json.dumps(report)


def test_absent_binaries_report_a_reason_not_an_exception(nothing_present):
    """DERIVED FROM doctor.CHECKS, not a list written out again here.

    This used to name the seven rows and assert the string "7 unavailable".
    Adding the imageto3d row broke both halves, and only on machines without a
    GPU — so it passed on the two boxes it was written on and failed on every
    runner. A count and a list that have to be edited in step with CHECKS will
    be forgotten again; asking CHECKS cannot be.
    """
    report = doctor.check()
    absent = [n for n in doctor.CHECKS if n != "python"]
    for name in absent:
        row = report[name]
        assert row["available"] is False, name
        assert row["reason"], name
        assert row["path"] in ("", "C:/py.exe"), name
    # python is the interpreter running this — it is always there.
    assert report["python"]["available"]
    assert f"{len(absent)} unavailable" in doctor.summary(report)


def test_export_templates_must_match_the_editor_version(monkeypatch, tmp_path):
    """A near miss is the common case and the confusing one.

    Godot refuses to export against templates from another version, and the
    error it prints reads like a broken preset — so this reports unavailable
    AND names the versions, rather than saying 'not installed' about a
    directory that visibly contains web templates.
    """
    from bgate_adapters import godot

    installed = tmp_path / "export_templates" / "4.6.stable"
    installed.mkdir(parents=True)
    (installed / "web_release.zip").write_bytes(b"zip")
    monkeypatch.setattr(godot, "_template_dirs",
                        lambda: [tmp_path / "export_templates"])
    monkeypatch.setattr(godot, "version",
                        lambda: {"path": "C:/godot.exe", "version": "4.7.1.stable"})

    probe = godot.export_templates("web")
    assert probe["available"] is False
    assert "4.6.stable" in probe["reason"] and "4.7.1.stable" in probe["reason"]

    (installed.parent / "4.7.1.stable").mkdir()
    (installed.parent / "4.7.1.stable" / "web_release.zip").write_bytes(b"zip")
    probe = godot.export_templates("web")
    assert probe["available"] is True
    assert probe["version"] == "4.7.1.stable"


def test_a_binary_below_the_minimum_counts_as_unavailable(everything_present,
                                                          monkeypatch):
    monkeypatch.setattr("bgate_adapters.godot.version",
                        lambda: {"path": "C:/godot.exe", "version": "3.5.2.stable"})
    row = doctor.check()["godot"]
    assert row["available"] is False
    assert row["min_required"] == "4.0"
    assert "below the minimum" in row["reason"]


def test_a_probe_that_explodes_becomes_a_row(everything_present, monkeypatch):
    def boom():
        raise OSError("the disk went away")

    monkeypatch.setattr("bgate_adapters.blender.available", boom)
    row = doctor.check()["blender"]
    assert row["available"] is False
    assert "the disk went away" in row["reason"]


def test_results_are_cached_between_close_calls(everything_present, monkeypatch):
    calls = []

    def counted():
        calls.append(1)
        return {"available": True, "path": "C:/godot.exe"}

    monkeypatch.setattr("bgate_adapters.godot.available", counted)
    doctor.check()
    doctor.check()
    doctor.check()
    assert len(calls) == 1
    doctor.check(refresh=True)
    assert len(calls) == 2


def test_the_check_never_opens_the_microphone(everything_present, monkeypatch):
    """The whole point of splitting doctor out of the playtest preflight."""
    def forbidden(*args, **kwargs):
        raise AssertionError("doctor probed the microphone")

    monkeypatch.setattr("bgate_adapters.recorder.probe_mic", forbidden)
    monkeypatch.setattr("bgate_adapters.recorder.list_inputs", forbidden)
    assert doctor.check()["ffmpeg"]["available"]


@pytest.mark.anyio
async def test_doctor_is_reachable_as_a_tool(everything_present):
    result = await server.mcp.call_tool("bgate_doctor", {})
    content = result[0] if isinstance(result, tuple) else result
    report = json.loads(content[0].text)
    assert set(report) == set(doctor.CHECKS)


# ---------------------------------------------------------------------------
# Per-call project root: explicit project_dir > BGATE_ROOT > cwd
# ---------------------------------------------------------------------------
async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.fixture()
def other_root(tmp_path_factory):
    """A second project, so 'which one did the call hit' has a wrong answer."""
    path = tmp_path_factory.mktemp("other")
    project.init(path, "Other Game", pitch="the one that must not be written")
    yield path
    db.close_all()


@pytest.mark.anyio
async def test_explicit_project_dir_beats_bgate_root(root, other_root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    got = await call("project_status", project_dir=str(other_root))
    assert got["project"]["name"] == "Other Game"


@pytest.mark.anyio
async def test_bgate_root_beats_the_cwd(root, other_root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(other_root))
    monkeypatch.chdir(root)
    got = await call("project_status")
    assert got["project"]["name"] == "Other Game"


@pytest.mark.anyio
async def test_the_cwd_is_the_last_resort(root, monkeypatch):
    monkeypatch.delenv("BGATE_ROOT", raising=False)
    monkeypatch.chdir(root)
    got = await call("project_status")
    assert got["project"]["name"] == "Test Game"


@pytest.mark.anyio
async def test_project_dir_does_not_leak_into_the_next_call(root, other_root,
                                                            monkeypatch):
    """The race the mutable global caused: one call steering the next one."""
    monkeypatch.setenv("BGATE_ROOT", str(root))
    assert (await call("project_status",
                       project_dir=str(other_root)))["project"]["name"] == "Other Game"
    assert (await call("project_status"))["project"]["name"] == "Test Game"


@pytest.mark.anyio
async def test_project_select_no_longer_switches_the_active_project(root, other_root,
                                                                    monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    chosen = await call("project_select", project=str(other_root))
    assert chosen["use_project_dir"] and chosen["deprecated"]
    # Selecting did NOT move the server: the next unqualified call is unaffected.
    assert (await call("project_status"))["project"]["name"] == "Test Game"


def test_no_tool_reads_a_mutable_module_global():
    """The guard the fix exists for — assert the global is gone, not just unused."""
    assert not hasattr(server, "_ACTIVE_ROOT")


@pytest.mark.anyio
async def test_every_tool_advertises_project_dir():
    for tool in await server.mcp.list_tools():
        assert "project_dir" in tool.inputSchema.get("properties", {}), tool.name
