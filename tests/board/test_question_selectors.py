"""Questions to the human are multiple-choice selectors."""
from __future__ import annotations

import json

import pytest

from bgate_core.board import steerbox
from bgate_mcp import server


async def _ask(**kw) -> dict:
    result = await server.mcp.call_tool("ask_human", kw)
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


@pytest.mark.anyio
async def test_a_question_without_choices_is_refused(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    got = await _ask(question="What should the boss drop?")
    assert got["refused"] == "no_options"
    got = await _ask(question="What should the boss drop?", options=["a sword"])
    assert got["refused"] == "no_options"


@pytest.mark.anyio
async def test_choices_carry_details_and_multi_select(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    got = await _ask(question="Which seats should replan?", multi_select=True,
                     options=[{"label": "Art", "detail": "portraits drifted"},
                              {"label": "Audio", "detail": "cues missing"},
                              "Level"])
    assert got["ok"]
    shown = steerbox.open_questions(root)[-1]
    assert shown["options"] == ["Art", "Audio", "Level"]
    assert shown["option_details"] == ["portraits drifted", "cues missing", ""]
    assert shown["multi"] is True
