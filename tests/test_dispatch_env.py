"""What a spawned agent is handed, and what it is not.

The dashboard is started from the user's shell and inherits everything that
shell holds. It used to pass that whole environment to every agent it spawned,
so a seat with a Bash tool could read a cloud token, a database URL or a signing
key that has nothing to do with making a game. These tests pin the scrub, the
aegis mode the child is told to enforce, and the containment of the directory it
is started in.
"""
from __future__ import annotations

import io
import subprocess

import sys

import pytest

from bgate_core import queue
from bgate_ui import dispatch


class FakeStdin(io.BytesIO):
    def close(self):  # dispatch closes stdin at EOF; keep it readable
        pass


class FakeProc:
    def __init__(self, pid=7000):
        self.pid = pid
        self.stdin = FakeStdin()

    def poll(self):
        return None

    def kill(self):
        pass

    def terminate(self):
        pass


@pytest.fixture()
def spawned(monkeypatch):
    """Capture the argv/cwd/env of the spawn without running anything.

    Only the agent CLI is faked. dispatch.subprocess is the subprocess module
    itself, so git calls made on the way have to pass straight through or the
    dispatcher's own dirty-tree read starts lying.
    """
    real_popen = subprocess.Popen
    calls: list[dict] = []
    monkeypatch.setattr(dispatch, "find_claude", lambda: "claude")
    monkeypatch.setattr(dispatch, "_watch_completion", lambda *a, **k: None)

    def fake_popen(args, **kw):
        if args and str(args[0]) == "git":
            return real_popen(args, **kw)
        calls.append({"args": list(args), "cwd": kw.get("cwd"),
                      "env": dict(kw.get("env") or {})})
        return FakeProc(pid=7000 + len(calls))

    monkeypatch.setattr(dispatch.subprocess, "Popen", fake_popen)
    dispatch._live.clear()
    yield calls
    dispatch._live.clear()


def _dispatch_one(root, seat="art"):
    item = queue.add(root, seat, "paint a rock")
    res = dispatch.dispatch(root, item["id"])
    assert res["ok"], res
    return res


class TestEnvironmentScrub:
    def test_unrelated_secrets_do_not_reach_the_agent(self, root, spawned,
                                                      monkeypatch):
        """The whole point. A credential the game never uses is not inherited."""
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_leak")
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@host/db")
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_leak")
        _dispatch_one(root)
        env = spawned[0]["env"]
        for var in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DATABASE_URL",
                    "STRIPE_SECRET_KEY"):
            assert var not in env
        # And no value of theirs snuck through under another name.
        assert "sk_live_leak" not in set(env.values())

    def test_the_toolchain_still_starts(self, root, spawned, monkeypatch):
        """PATH and the Windows process essentials are NOT what we are hiding.

        Dropping these is the failure worse than the leak: a child with no PATH
        cannot find its own CLI, and one with no SystemRoot starts and then
        cannot open a socket.
        """
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.CMD")
        monkeypatch.setenv("TEMP", r"C:\Temp")
        _dispatch_one(root)
        env = spawned[0]["env"]
        upper = {k.upper() for k in env}
        for var in ("PATH", "PATHEXT", "SYSTEMROOT", "TEMP"):
            assert var in upper, f"{var} was scrubbed and the agent needs it"

    def test_provider_keys_survive_because_the_mcp_tools_need_them(
            self, root, spawned, monkeypatch):
        from bgate_core.providers import PROVIDERS

        for provider in PROVIDERS:
            monkeypatch.setenv(provider.env, f"key-for-{provider.id}")
        _dispatch_one(root)
        env = spawned[0]["env"]
        for provider in PROVIDERS:
            assert env.get(provider.env) == f"key-for-{provider.id}", (
                f"{provider.id} is a registered provider; scrubbing its key "
                "produces a 'key not set' that names the wrong cause")

    def test_provider_list_is_derived_not_duplicated(self):
        """A provider added to the table must not need a second edit here."""
        from bgate_core.providers import PROVIDERS

        derived = set(dispatch._provider_env_vars())
        assert {p.env for p in PROVIDERS} <= derived

    def test_bgate_namespace_rides_through_as_a_prefix(self, root, spawned,
                                                       monkeypatch):
        """Settings keep being added; an enumerated list would stop honouring
        the newest one and look like 'the Settings panel does nothing'."""
        monkeypatch.setenv("BGATE_SOMETHING_NEW", "on")
        _dispatch_one(root)
        assert spawned[0]["env"].get("BGATE_SOMETHING_NEW") == "on"

    def test_matching_is_case_insensitive_like_windows(self, monkeypatch):
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        monkeypatch.setenv("bgate_lowercase_probe", "1")
        kept = dispatch._scrubbed_environ()
        assert any(k.upper() == "SYSTEMROOT" for k in kept)
        assert any(k.upper() == "BGATE_LOWERCASE_PROBE" for k in kept)

    def test_pinned_scope_still_wins_over_whatever_was_inherited(
            self, root, spawned, monkeypatch):
        """The scrub must not have reopened the self-approval hole: an agent
        that inherits the dashboard's seat and actor can approve its own work."""
        monkeypatch.setenv("BGATE_SEAT", "director")
        monkeypatch.setenv("BGATE_ACTOR", "human:someone")
        monkeypatch.setenv("BGATE_ROOT", r"C:\some\other\project")
        res = _dispatch_one(root, seat="art")
        env = spawned[0]["env"]
        assert env["BGATE_SEAT"] == "art"
        assert env["BGATE_ACTOR"] == f"agent:item-{res['item_id']}"
        assert env["BGATE_ROOT"] == str(root)


class TestAegisMode:
    def test_the_child_is_told_the_mode(self, root, spawned, monkeypatch):
        monkeypatch.setenv("BGATE_AEGIS", "block")
        _dispatch_one(root)
        assert spawned[0]["env"]["BGATE_AEGIS"] == "block"

    def test_an_unset_mode_is_stated_explicitly_not_left_blank(
            self, root, spawned, monkeypatch):
        """The hook and the MCP server must agree. Leaving it unset means each
        child re-derives a default, and a typo makes them disagree."""
        from bgate_core import aegis

        monkeypatch.delenv("BGATE_AEGIS", raising=False)
        _dispatch_one(root)
        assert spawned[0]["env"]["BGATE_AEGIS"] == aegis.DEFAULT_MODE

    def test_a_typo_is_normalised_rather_than_passed_on(self, root, spawned,
                                                        monkeypatch):
        from bgate_core import aegis

        monkeypatch.setenv("BGATE_AEGIS", "blcok")
        _dispatch_one(root)
        assert spawned[0]["env"]["BGATE_AEGIS"] == aegis.DEFAULT_MODE


class TestContainment:
    def test_cwd_is_inside_the_pinned_root(self, root, spawned):
        _dispatch_one(root)
        assert dispatch._contained(spawned[0]["cwd"], root)

    def test_a_worktree_counts_as_contained(self, root):
        """Worktrees live at <root>/.bgate/work/item-N, so containment needs no
        special case for them - and this proves the check knows that."""
        assert dispatch._contained(str(root / ".bgate" / "work" / "item-3"),
                                   root)

    def test_a_sibling_directory_is_not_contained(self, root, tmp_path):
        """The prefix trap: <root>-evil starts with <root> as a STRING."""
        assert not dispatch._contained(str(root.parent / "elsewhere"), root)
        assert not dispatch._contained(str(root) + "-evil", root)

    @pytest.mark.skipif(sys.platform != "win32", reason=(
        "Case-insensitivity and backslash-as-separator are Windows facts. On "
        "POSIX these ARE different paths and _contained is right to say so, so "
        "asserting the Windows answer here would be asserting a bug."))
    def test_containment_ignores_case_and_separators(self, root):
        mixed = str(root).replace("\\", "/").upper()
        assert dispatch._contained(mixed, root)

    def test_no_runner_grants_a_directory_outside_the_project(self, root,
                                                              spawned):
        _dispatch_one(root)
        assert dispatch._escaping_dir_flag(spawned[0]["args"], root) is None

    def test_an_escaping_dir_flag_is_caught(self, root):
        args = ["claude", "-p", "--add-dir", r"C:\Users\someone\secrets"]
        assert dispatch._escaping_dir_flag(args, root) is not None

    def test_a_contained_dir_flag_is_allowed(self, root):
        args = ["codex", "exec", "--cd", str(root)]
        assert dispatch._escaping_dir_flag(args, root) is None
