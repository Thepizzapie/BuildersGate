"""The corny-copy check: the lexical tells catch the zingers that shipped in
Vibe Coder, and leave the sincere lines alone."""
from __future__ import annotations

from bgate_core.design import voice

CORNY = [
    "You're absolutely right, that key is real. I have rotated my confidence, not the key.",
    "You're absolutely right, that API does not exist. It felt like it should, and I committed to the bit.",
    "Here is the same diff, again, but warmer.",
    "You review what the AI wrote. Prod is watching.",
    "Root cause: you",
    "Thanks for the feedback! I've addressed it by changing nothing.",
]
PLAIN = [
    "You're absolutely right! Here is the updated change.",
    "Fixed the failing test! Totals are consistent now.",
    "can we get this out today?",
    "Delete expired sessions in batches of 500",
    "Press ENTER to clock in",
]


def test_the_shipped_zingers_are_flagged_without_a_model():
    got = voice.check(CORNY, use_model=False)
    assert len(got["flagged"]) == len(CORNY) and not got["ok"]


def test_sincere_lines_pass():
    got = voice.check(PLAIN, use_model=False)
    assert got["flagged"] == [] and got["ok"]


def test_scan_finds_prose_and_skips_code(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text(
        'const t = "Press ENTER to clock in";\n'
        'els.push(row("a", "b"));\nconst css = "rgba(0, 0, 0, 0.5)";\n')
    (tmp_path / "content").mkdir()
    (tmp_path / "content" / "d.json").write_text(
        '{"ai_line": "Fixed the failing test!", "lines": ["+ const x = a + b;"]}')
    got = voice.scan(tmp_path)
    assert "Press ENTER to clock in" in got and "Fixed the failing test!" in got
    assert not any("rgba" in g or "const x" in g or "push" in g for g in got)
