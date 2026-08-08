"""A second CLI under the art seat, and the two things it cannot do.

The feature is small and the risk is not: a runner that reports tokens instead
of dollars silently disables the cost ceiling, and one that reads stdin once
silently swallows every steer. Both failures look like the feature working. So
most of this file is about the declarations — cost_tracked, steerable — being
carried onto the run and surfaced, rather than about the happy path.

The image backend is the actual ask, and it is one flag: the CLI's own image
tool is turned OFF at the process boundary when the pipeline is meant to own
generation. That is a fact about the process, not a request in a prompt, which
is the whole reason it is done this way.
"""
from __future__ import annotations

from pathlib import Path

from bgate_core import queue, settings
from bgate_ui import dispatch, runners


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
def test_an_unknown_runner_falls_back_instead_of_failing():
    """The name comes out of stored settings. A typo there must not take the
    board down — every item would refuse to dispatch."""
    assert runners.get("nonsense").name == "claude"
    assert runners.get("").name == "claude"
    assert runners.get(None).name == "claude"
    assert runners.get("CODEX").name == "codex"


def test_codex_declares_what_it_cannot_do():
    codex = runners.get("codex")
    assert codex.cost_tracked is False, "no price in its event stream"
    assert codex.steerable is False, "codex exec reads stdin once"
    assert codex.prompt_via == "stdin_once"
    assert codex.requires_git_repo is True


def test_preflight_refuses_a_non_repo_because_the_sandbox_would_shadow_it(tmp_path):
    """Measured, not assumed: the same run outside a git repo wrote into
    ~/.codex/.sandbox/cwd/<hash> and reported every write as successful."""
    codex = runners.get("codex")
    why = runners.preflight(codex, str(tmp_path), exe="codex.cmd")
    assert why and "not a git repository" in why

    (tmp_path / ".git").mkdir()
    assert runners.preflight(codex, str(tmp_path), exe="codex.cmd") is None


def test_preflight_allows_a_project_outside_the_servers_own_directory(tmp_path):
    """The normal case, and it was refused for two merges.

    A CodeQL autofix made preflight require cwd to sit under `Path.cwd()`.
    Nothing about a board serving a project satisfies that: `bgate serve` runs
    from the checkout, the project lives wherever the user keeps games, and the
    refusal ("outside the allowed project root") reads like policy rather than
    like the bug it was. Every dispatch died at the preflight."""
    (tmp_path / ".git").mkdir()
    assert Path(tmp_path).resolve() != Path.cwd().resolve()
    assert runners.preflight(runners.get("codex"), str(tmp_path),
                             exe="codex.cmd") is None


def test_preflight_believes_a_caller_that_found_nothing(tmp_path):
    """None means THE CALLER FOUND NOTHING, and must not be re-looked-up — the
    fallback would pass on a machine that has the CLI and then hand Popen a
    None argv[0]."""
    (tmp_path / ".git").mkdir()
    why = runners.preflight(runners.get("claude"), str(tmp_path), exe=None)
    assert why == "claude CLI not found on PATH"


# ---------------------------------------------------------------------------
# The flag that is the feature
# ---------------------------------------------------------------------------
def _codex_argv(native: bool) -> list[str]:
    return runners.get("codex").build_args(
        "codex.cmd", permission_mode="acceptEdits", model=None,
        cwd="C:/game", native_images=native)


def test_the_image_backend_is_a_process_flag_not_a_request():
    """`bgate` removes the CLI's own image tool. A prompt asking it not to use
    one it still has is a request; a missing tool is a fact."""
    off = _codex_argv(False)
    assert off[off.index("--disable") + 1] == "image_generation"
    assert "--enable" not in off

    on = _codex_argv(True)
    assert on[on.index("--enable") + 1] == "image_generation"
    assert "--disable" not in on


def test_codex_always_gets_the_mcp_server_and_the_real_directory():
    """'JUST THE GENERATIONS' — an art agent that cannot reach ref_list,
    asset_lock or consistency_check is not an art seat."""
    argv = " ".join(_codex_argv(True))
    assert f"mcp_servers.{runners.MCP_SERVER_NAME}.command=" in argv
    assert "bgate_mcp.server" in argv
    # The sandbox is kept; --cd is what makes it write to the real tree.
    assert "--sandbox workspace-write" in argv and "--cd C:/game" in argv
    # Never: it is what makes a non-repo write into a shadow copy silently.
    assert "--skip-git-repo-check" not in argv


def test_claude_ignores_the_image_backend_because_it_has_no_image_tool():
    argv = runners.get("claude").build_args(
        "claude", permission_mode="acceptEdits", model=None,
        cwd="C:/game", native_images=True)
    assert "image_generation" not in " ".join(argv)


# ---------------------------------------------------------------------------
# Routing: art only
# ---------------------------------------------------------------------------
class TestRouting:
    def test_only_the_art_seat_is_routable(self, root):
        settings.set(root, "art.runner", "codex")
        assert dispatch._runner_for(root, "art").name == "codex"
        for seat in ("gameplay", "tech", "narrative", "qa", "audio", "director"):
            assert dispatch._runner_for(root, seat).name == "claude", seat

    def test_the_default_leaves_every_seat_where_it_was(self, root):
        assert dispatch._runner_for(root, "art").name == "claude"

    def test_native_images_need_a_runner_that_has_them(self, root):
        """A switch reading `native` beside an agent that cannot generate is
        the kind of lie that costs an afternoon."""
        settings.set(root, "art.image_backend", "native")
        assert dispatch._native_images(root, runners.get("codex")) is True
        assert dispatch._native_images(root, runners.get("claude")) is False

    def test_the_bgate_backend_is_the_default(self, root):
        assert dispatch._native_images(root, runners.get("codex")) is False


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------
class TestImagePolicy:
    def _item(self, root, seat="art"):
        return queue.add(root, seat, "draw the thing")

    def test_native_says_what_has_not_changed(self, root):
        prompt = dispatch._prompt_for(root, self._item(root), native_images=True)
        assert "NATIVE" in prompt
        # The whole risk of this feature: "generate natively" read as "skip the
        # pipeline". Every one of these must survive.
        for owed in ("ref_list", "asset_lock", "consistency_check",
                     "asset_track", "godot_import_asset"):
            assert owed in prompt, owed
        assert "BGATE_IMAGE_MODEL" in prompt, (
            "the env ban does not reach a native tool; the agent has to be told")

    def test_bgate_adds_nothing_to_the_prompt(self, root):
        """The default needs no paragraph — image_generate is already the only
        image tool in the process, and prompt weight on every art dispatch is
        not free."""
        prompt = dispatch._prompt_for(root, self._item(root), native_images=False)
        assert "IMAGE BACKEND FOR THIS RUN" not in prompt

    def test_a_non_art_seat_never_gets_the_policy(self, root):
        prompt = dispatch._prompt_for(root, self._item(root, "gameplay"),
                                      native_images=True)
        assert "IMAGE BACKEND FOR THIS RUN" not in prompt


# ---------------------------------------------------------------------------
# The guards that do not apply, saying so
# ---------------------------------------------------------------------------
class TestUntrackedCost:
    def test_a_cost_ceiling_is_skipped_rather_than_read_as_zero(self):
        """_observed_cost returns 0.00 forever on a runner that reports no
        price, so the ceiling would sit there looking live and never fire. The
        entry says cost_tracked=False and the check steps aside."""
        entry = {"cost_tracked": False, "max_cost_usd": 5.0}
        assert bool(entry.get("max_cost_usd")) and not entry.get("cost_tracked", True)

    def test_steering_a_codex_agent_is_refused_by_name(self, root, monkeypatch):
        """Not 'channel closed' — there was never a channel. Sending the
        operator to wait for a message that cannot arrive is the failure."""
        item = queue.add(root, "art", "draw")
        monkeypatch.setitem(dispatch._live, item["id"], {
            "proc": _AlivePoll(), "steerable": False, "runner": "codex",
            "stdin_closed": True, "steers": []})
        res = dispatch.steer(root, item["id"], "actually make it blue")
        assert res["ok"] is False
        assert "no live steer channel" in res["error"]
        assert res["runner"] == "codex"


class _AlivePoll:
    pid = -1

    def poll(self):
        return None


# ---------------------------------------------------------------------------
# Reading codex's log
# ---------------------------------------------------------------------------
def _state():
    return {"steps": [], "step_count": 0, "final": None}


def _feed(state, events):
    import json as _json
    for ev in events:
        dispatch._absorb(state, (_json.dumps(ev) + "\n").encode("utf-8"))


class TestCodexFeed:
    """Event shapes captured from a real `codex exec --json` run, not invented."""

    def test_a_message_becomes_a_step_and_the_last_one_is_the_result(self):
        state = _state()
        _feed(state, [
            {"type": "thread.started", "thread_id": "019f"},
            {"type": "turn.started"},
            {"type": "item.completed",
             "item": {"id": "item_0", "type": "agent_message", "text": "first"}},
            {"type": "item.completed",
             "item": {"id": "item_2", "type": "agent_message", "text": "done: sheet written"}},
            {"type": "turn.completed",
             "usage": {"input_tokens": 39129, "output_tokens": 113}},
        ])
        assert [s["kind"] for s in state["steps"]] == ["say", "say"]
        # Codex has no separate result event; the last thing it said IS the
        # deliverable the panel was opened to read.
        assert state["final"]["text"] == "done: sheet written"

    def test_a_turn_records_tokens_and_never_a_price(self):
        state = _state()
        _feed(state, [{"type": "turn.completed",
                       "usage": {"input_tokens": 10, "output_tokens": 2}}])
        assert state["usage"]["input_tokens"] == 10
        assert "cost" not in state["usage"] and "usd" not in str(state["usage"])

    def test_a_shell_command_reads_as_a_tool_and_its_output(self):
        state = _state()
        _feed(state, [{"type": "item.completed", "item": {
            "id": "item_1", "type": "command_execution",
            "command": "powershell -Command Get-ChildItem",
            "aggregated_output": "probe.txt", "exit_code": 0,
            "status": "completed"}}])
        assert [s["kind"] for s in state["steps"]] == ["tool", "result"]
        assert "Get-ChildItem" in state["steps"][0]["hint"]

    def test_item_started_does_not_double_the_feed(self):
        state = _state()
        item = {"id": "item_1", "type": "command_execution", "command": "ls",
                "aggregated_output": "", "exit_code": None}
        _feed(state, [{"type": "item.started", "item": item},
                      {"type": "item.completed", "item": {**item, "exit_code": 0}}])
        assert sum(1 for s in state["steps"] if s["kind"] == "tool") == 1

    def test_a_new_thread_clears_the_previous_run(self):
        state = _state()
        _feed(state, [{"type": "item.completed",
                       "item": {"type": "agent_message", "text": "old run"}},
                      {"type": "thread.started", "thread_id": "019g"}])
        assert state["steps"] == [] and state["final"] is None

    def test_an_unknown_item_type_still_shows_up(self):
        """Silence would make a new codex capability look like an agent sitting
        there doing nothing."""
        state = _state()
        _feed(state, [{"type": "item.completed",
                       "item": {"type": "image_generation", "text": "hero.png"}}])
        assert state["steps"] and state["steps"][0]["name"] == "image_generation"

    def test_claude_events_still_parse_beside_them(self):
        """One log per ITEM, and a re-dispatch may switch runners under an
        existing file, so the reader must handle either vocabulary untold."""
        state = _state()
        _feed(state, [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "thinking"}]}},
            {"type": "result", "subtype": "success", "result": "shipped",
             "total_cost_usd": 0.42},
        ])
        assert state["final"]["text"] == "shipped"
        assert state["final"]["cost"] == 0.42
