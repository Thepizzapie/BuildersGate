"""The seat briefs, held to the things prose cannot check about itself.

A brief is not documentation. It is the program a spawned agent actually runs,
and it is the only program in this repo that nothing compiles, nothing type
checks and no import breaks when it goes wrong. Two failure modes have already
been paid for:

  * SIZE. The art seat's workflow reached ~1,500 words, of which 669 were a 3D
    sequence every 2D sprite request also carried. Under that load a model keeps
    the mechanically-rewarded steps — build layers, call blender_combine, read
    `checks` — and quietly drops the expensive unenforced ones. The observed
    drops were exactly the predicted ones.
  * DRIFT AHEAD OF THE TOOLS. The brief told the seat to condition
    image_generate on the pinned refs; image_generate has no reference
    parameter. It told the seat to stop and wait on ask_human; ask_human's own
    docstring says it returns immediately and does not block. It said eight
    layers was a ceiling; blender_combine warns and assembles anyway. Prose
    cannot notice this happening. These tests can.

The last class is the durable one. It reads the tool surface out of
bgate_mcp/server.py and refuses any promise the surface does not back — in BOTH
directions, so a capability landing later makes the brief's denial of it fail
just as loudly as a capability being removed.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from bgate_core import project, seats

SERVER_PY = Path(__file__).resolve().parents[1] / "bgate_mcp" / "server.py"


# ---------------------------------------------------------------------------
# The tool surface, read from the source rather than imported.
#
# AST, not `import bgate_mcp.server`: every tool is registered through
# `mcp.tool()(wrapper)`, so the module attribute is a FastMCP object whose
# internals are not this repo's contract. The `@_tool`-decorated `def` IS the
# contract — the name the model calls and the parameters it may pass — and it is
# readable without importing FastMCP, Blender, or an image key.
# ---------------------------------------------------------------------------
def _is_tool_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_tool")


def _tool_surface() -> dict[str, set[str]]:
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    defs: dict[str, set[str]] = {}
    registered: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            defs[node.name] = {
                a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
            if any((isinstance(d, ast.Name) and d.id == "_tool")
                   or _is_tool_call(d) for d in node.decorator_list):
                registered.add(node.name)
        # `vfx_animate = _tool(vfx_animate)` — registration by assignment, used
        # where a docstring has to be .format()ed from live data before the tool
        # is built. It is as registered as any decorated one, and a surface
        # reader that only understood decorators reported it MISSING, which
        # would have failed a brief for naming a tool that exists.
        elif isinstance(node, ast.Assign) and _is_tool_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    registered.add(target.id)
    return {name: defs.get(name, set()) for name in registered}


TOOLS = _tool_surface()


def _seat_texts() -> dict[str, str]:
    """Every stretch of prose a seat is handed, by where it lives."""
    texts = {"3d": seats.ART_3D_WORKFLOW,
             "identity": seats.SEAT_IDENTITY,
             "director-protocol": seats.DIRECTOR_PROTOCOL,
             "kind-note": seats._kind_note("art", "2d")}
    for role, cfg in seats.DEFAULT_SEATS.items():
        texts[f"{role}:mission"] = cfg.get("mission", "")
        if cfg.get("workflow"):
            texts[f"{role}:workflow"] = cfg["workflow"]
    return texts


def art_text() -> str:
    """The whole art brief a 3D project's agent reads, in one string."""
    art = seats.DEFAULT_SEATS["art"]
    return "\n".join([art["mission"],
                      seats.workflow_for("art", "3d", art.get("workflow", ""))])


def words(text: str) -> int:
    return len(text.split())


def numbered_steps(text: str) -> list[str]:
    """The top-level numbered imperatives — '1. ', '2. ' at the start of a line.

    This is the countable meaning of 'one imperative': a thing the agent is told
    to do, in order, that it can tick off. Sub-clauses inside a step are the
    justification for that step, not another instruction.
    """
    return re.findall(r"(?m)^(\d+)\.\s", text)


# Snake_case identifiers a brief may name that are NOT MCP tools: kit functions
# inside blender_run, tool PARAMETERS, result KEYS, and Godot API. The list is
# explicit on purpose — the point of the tool-name test is that inventing a
# plausible-sounding tool fails loudly, and a regex clever enough to guess which
# unknown identifier was meant as a tool would defeat it.
NOT_TOOLS = {
    # the modelling kit, injected into blender_run's namespace
    "bg_ball", "bg_bone_chain", "bg_box", "bg_clean", "bg_cyl", "bg_finish",
    "bg_help", "bg_join", "bg_mat", "bg_mirror", "bg_plane", "bg_smooth",
    "bg_stats", "bg_taper", "bg_unwrap",
    # parameters and result keys
    "artifact_id", "decal_on", "dry_run", "max_cost_usd", "ref_image",
    "ref_images", "ref_strength", "task_kind", "tileable", "unweighted_verts",
    "use_pinned", "source_ref", "work_item_id",
    "first_frame", "last_frame", "style_note", "style_refs",
    "audio_track", "transition_s",
    # not ours
    "add_child", "one_of",
}


def named_identifiers(text: str) -> set[str]:
    return set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text))


class TestTheThreeDBlockFitsItsBudget:
    """The 3D sequence, held to a stated size.

    669 words and seven lettered sub-steps is what it was when the expensive
    steps started going missing. The budget is deliberately a little slack —
    this is a drift guard, not a golden file — but it fails long before the
    block can grow back to what it was.
    """

    # Stated, not derived: a budget you can read off the test is one a future
    # author has to argue with deliberately.
    MAX_WORDS = 650
    MAX_STEPS = 10

    def test_the_sequence_is_ten_steps_or_fewer(self):
        steps = numbered_steps(seats.ART_3D_WORKFLOW)
        assert len(steps) <= self.MAX_STEPS, (
            f"the 3D sequence is {len(steps)} steps; the budget is "
            f"{self.MAX_STEPS}. An eleventh step is a request to drop one.")
        # In order, starting at 1 — a jump is a step that was deleted from the
        # middle and left a hole where the agent looks for its next move.
        assert [int(s) for s in steps] == list(range(1, len(steps) + 1))

    def test_the_sequence_is_under_its_word_budget(self):
        count = words(seats.ART_3D_WORKFLOW)
        assert count <= self.MAX_WORDS, (
            f"the 3D sequence is {count} words, over its {self.MAX_WORDS} "
            "budget. It was cut from 669 because at that size the unenforced "
            "steps stopped happening; growing it back undoes that.")

    def test_most_steps_are_anchored_to_something_callable(self):
        # A step with no mechanical anchor is doctrine, and doctrine is what a
        # loaded model drops first. Step 1 is a genuine judgement call and has
        # no tool behind it, which is fine for one step out of ten and is the
        # tell for a block sliding back into prose if it spreads.
        anchors = set(TOOLS) | NOT_TOOLS | {"checks", "bound", "unbound",
                                            "manifest", "rig", "bind"}
        body = seats.ART_3D_WORKFLOW.split("TEN STEPS, IN ORDER.", 1)[-1]
        steps = re.split(r"(?m)^\d+\.\s", body)[1:]
        anchored = [step for step in steps
                    if (named_identifiers(step)
                        | set(re.findall(r"`([a-z_]+)`", step))) & anchors]
        assert len(anchored) >= len(steps) - 1, (
            f"only {len(anchored)} of {len(steps)} steps name anything "
            "callable; the rest cannot be checked and will not be done")


class TestTheThreeDBlockIsKindKeyed:
    """A 2D sprite request must not carry 669 words about armature binding.

    `project.dimension` already exists ('2d' | '3d' | '2d+3d') and is set by
    init and adopt, so the brief has a real key to switch on rather than a
    guess. This is the structural half of the size fix: the other half is only
    a cut, and a cut is something the next author can undo without noticing.
    """

    def test_a_two_dimensional_project_gets_none_of_it(self):
        text = seats.workflow_for("art", "2d", seats.DEFAULT_SEATS["art"]["workflow"])
        assert "TEN STEPS, IN ORDER." not in text
        assert "blender_combine" not in text
        assert "armature" not in text

    def test_a_two_dimensional_project_is_told_where_it_went(self):
        # Silence is indistinguishable from the sequence not existing, and an
        # agent reconstructing it from memory is what the cut was meant to stop.
        text = seats.workflow_for("art", "2d", seats.DEFAULT_SEATS["art"]["workflow"])
        assert "dimension" in text
        assert "do not reconstruct" in text

    @pytest.mark.parametrize("dimension", ["3d", "2d+3d"])
    def test_a_project_that_makes_meshes_gets_all_of_it(self, dimension):
        text = seats.workflow_for("art", dimension,
                                  seats.DEFAULT_SEATS["art"]["workflow"])
        assert seats.ART_3D_WORKFLOW in text
        # And it is APPENDED, not a replacement: the painted-art rules are true
        # whatever the project makes, and a 3D project still generates its
        # textures and concept refs through them.
        assert "EIGHT RULES" in text

    def test_the_keyed_dimensions_are_real_ones(self):
        for role, table in seats.WORKFLOW_BY_DIMENSION.items():
            assert role in seats.DEFAULT_SEATS
            for dimension in table:
                assert dimension in project.DIMENSIONS, (
                    f"{dimension!r} is not a project dimension, so this block "
                    "can never be selected")

    def test_a_brief_reports_the_dimension_it_keyed_on(self, root):
        # Without this the agent cannot tell "no 3D block" from "no 3D project".
        payload = seats.brief(root, "art")
        assert payload["dimension"] in project.DIMENSIONS

    def test_an_unreadable_project_row_still_briefs_a_seat(self, tmp_path):
        # A brief that raises is a dispatch that never starts. '2d' is the safe
        # degradation: it withholds the 3D sequence rather than handing a sprite
        # job five hundred words about binding.
        assert seats._dimension(tmp_path) == "2d"


class TestEveryToolTheBriefsNameExists:
    """A brief that names a tool the server does not register is unfollowable.

    This is how "re-run that one layer" survived for months as an instruction
    with nothing behind it: prose can name anything.
    """

    def test_no_brief_names_a_tool_that_is_not_registered(self):
        for where, text in _seat_texts().items():
            unknown = named_identifiers(text) - set(TOOLS) - NOT_TOOLS
            assert not unknown, (
                f"{where} names {sorted(unknown)}, which is neither a "
                "registered MCP tool nor a known parameter/kit function. "
                "Either the tool was renamed, or the brief invented it.")

    def test_the_art_brief_still_names_the_tools_its_steps_depend_on(self):
        # The inverse guard: a rename that silently emptied the brief of its
        # anchors would pass the test above by naming nothing at all.
        text = art_text()
        for tool in ("blender_run", "blender_combine", "blender_texture",
                     "blender_turnaround", "blender_sweep", "image_edit",
                     "image_generate", "image_sprites", "ask_human"):
            assert tool in text, f"the art brief no longer names {tool}"


class TestNoTwoRulesFightOverOneDecision:
    """The pairs that were competing, each pinned to its resolution.

    A model handed two rules that decide the same thing follows whichever it
    read last, and neither author finds out. Each test here names the decision,
    not the rules.
    """

    def test_generating_a_texture_per_layer_is_not_a_breach_of_the_minimum(self):
        # THE DECISION: how many images to buy. Rule 1 says generate the
        # minimum; the 3D sequence says one texture per layer, which is six for
        # a baseball player. Both are right about different things and the
        # brief has to say which, or the seat picks one and drops the other —
        # and the one it drops is the expensive one.
        text = seats.ART_3D_WORKFLOW
        assert "GENERATE THE MINIMUM" in text, (
            "the per-layer texture rule no longer reconciles itself with rule "
            "1, so the two compete again")
        assert re.search(r"counts FRAMES", text), (
            "the reconciliation has to say what the minimum counts — frames of "
            "one subject, which arithmetic derives — or it is just an assertion")

    def test_a_blown_frame_is_a_lighting_fix_and_looking_is_still_required(self):
        # THE DECISION: what to do when a turnaround frame comes back white.
        # The mission says LOOK at the frame; the sequence says a blown frame is
        # lighting, do not touch geometry. Those only compete while nobody says
        # the two answer different questions.
        text = seats.ART_3D_WORKFLOW
        assert "exposure=" in text, (
            "the fix for an unreadable frame must be named, or 'do not change "
            "geometry' leaves the seat with no move at all")
        assert "never with geometry" in text
        assert re.search(r"[Yy]our eyes answer", text), (
            "the brief no longer distinguishes the verdict's question from the "
            "one only a human eye answers, so 'LOOK at the frame' and 'a blown "
            "frame is lighting' compete again")

    def test_the_layer_ceiling_is_described_as_the_warning_it_is(self):
        # THE DECISION: what happens at nine layers. blender_combine warns and
        # assembles; the old brief implied a refusal. A rule an agent believes
        # is enforced is a rule it stops keeping.
        text = seats.ART_3D_WORKFLOW
        assert "warns above eight" in text
        assert "nothing refuses you" in text
        assert "EIGHT IS THE CEILING" not in text, (
            "this phrasing claims an enforcement that does not exist")

    def test_the_brief_does_not_tell_the_seat_to_wait_for_a_human(self):
        # THE DECISION: whether to stop before spending. ask_human returns
        # immediately; there is no blocking checkpoint anywhere in this
        # sequence, so an instruction to stop is an instruction to hang or to
        # lie about having waited.
        text = seats.ART_3D_WORKFLOW
        assert "DECLARE IT AND STOP" not in text
        assert "DOES NOT BLOCK" in text
        assert re.search(r"steer|handoff", text), (
            "the brief must say where the answer actually arrives, or the seat "
            "has no reason to look for it")


class TestNoPromiseTheCodeCannotKeep:
    """The durable one: the brief may not run ahead of the tool surface.

    Everything above is a fact about today's text. This class re-derives its
    expectations from bgate_mcp/server.py on every run, so it keeps failing for
    the RIGHT reason after the text changes — including when a tool grows the
    capability the brief currently, correctly, denies it has.
    """

    def test_every_keyword_a_brief_shows_is_a_real_parameter(self):
        # Catches the exact shape of the original bug: the brief said
        # "image_generate with task_kind='vfx'" and image_generate has no
        # task_kind. A worked call in a brief is a promise the signature keeps.
        for where, text in _seat_texts().items():
            for tool, arglist in re.findall(r"\b([a-z][a-z0-9_]+)\(([^)]*)\)", text):
                if tool not in TOOLS:
                    continue
                for kwarg in re.findall(r"([a-z][a-z0-9_]*)\s*=", arglist):
                    assert kwarg in TOOLS[tool], (
                        f"{where} calls {tool}({kwarg}=...) but {tool} takes "
                        f"{sorted(TOOLS[tool])}")
            for tool, kwarg in re.findall(
                    r"\b([a-z][a-z0-9_]+)\s+(?:with|takes|passing)\s+"
                    r"([a-z][a-z0-9_]*)\s*=", text):
                if tool in TOOLS:
                    assert kwarg in TOOLS[tool], (
                        f"{where} tells the seat to pass {kwarg}= to {tool}, "
                        f"which takes {sorted(TOOLS[tool])}")

    def test_the_brief_and_image_generate_agree_about_references(self):
        # BOTH DIRECTIONS. This is the bug the whole file exists for: the brief
        # said "image_generate conditioned on the pinned refs" while the tool
        # had no reference parameter at all, and prose cannot notice that. The
        # parameter has since landed, so the brief now names it — and if it is
        # ever removed, the brief's use of it fails here rather than in a run.
        ref_params = TOOLS["image_generate"] & {
            "ref_images", "ref_image", "refs", "ref_paths", "use_pinned"}
        text = art_text()
        uses_it = bool(re.search(r"image_generate\([^)]*ref_images=", text))
        denies = "image_generate takes no reference" in text
        if ref_params:
            assert not denies, (
                f"image_generate takes {sorted(ref_params)} now, but the art "
                "brief still tells the seat it cannot be conditioned — the seat "
                "will keep routing texture work the long way round")
            assert uses_it, (
                "image_generate can be conditioned on the pinned refs and the "
                "art brief does not show the seat how. An unconditioned texture "
                "is how an assembled character ends up 21 materials of "
                "somebody's typed colour")
        else:
            assert not re.search(r"image_generate\s+conditioned", text)
            assert not uses_it, (
                "the brief passes ref_images= to image_generate, which does not "
                "take one; image_edit is the conditioned call")
            assert denies, (
                "with no reference parameter the brief has to say so, or the "
                "seat assumes it can condition and silently does not")

    def test_the_brief_names_the_single_layer_rerun_tool_if_there_is_one(self):
        # "re-run that one layer, not the character" was false for as long as
        # nothing could do it — the recipe sat in the manifest and nothing read
        # it back. blender_layer_rerun is what made it true. If it is ever
        # withdrawn, the brief must fall back to naming the manifest rather than
        # keeping a promise nothing keeps.
        text = art_text()
        rerun = sorted(name for name in TOOLS if "rerun" in name)
        if rerun:
            assert any(name in text for name in rerun), (
                f"{rerun} exists and the art brief still tells the seat to "
                "reassemble by hand; the brief is behind the tools")
        else:
            assert "manifest" in text, (
                "with no re-run tool the manifest is the only thing that makes "
                "one layer rebuildable — the brief must name it")
            assert not re.search(r"\blayer_rerun\b", text)

    def test_the_brief_does_not_claim_a_checkpoint_ask_human_cannot_provide(self):
        # Read from the docstring, not from memory: ask_human is the one place
        # the brief's single human checkpoint lived, and the docstring is the
        # statement of record that it does not block.
        source = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
        doc = ""
        for node in source.body:
            if isinstance(node, ast.FunctionDef) and node.name == "ask_human":
                doc = ast.get_docstring(node) or ""
        assert doc, "ask_human is gone; the art brief still routes through it"
        # Whitespace-normalized: the claim is wrapped across two source lines,
        # and a substring check that cared would report the docstring as having
        # changed every time somebody reflowed it.
        flat = " ".join(doc.split()).upper()
        text = art_text()
        if "DOES NOT BLOCK" not in flat:
            pytest.fail(
                "ask_human no longer says it does not block — re-read it and "
                "decide whether the art brief may stop and wait again")
        assert not re.search(r"(?i)\bstop\b[^.]{0,40}\bbefore one image\b", text)
        assert "DOES NOT BLOCK" in text, (
            "ask_human does not block and the brief must say so where the seat "
            "reads it, not only where the tool's docstring does")


class TestTheBriefRoutesToTheStrongerTool:
    """The 3D path's ceiling, stated where the decision is made.

    The primitive vocabulary cannot produce a hero character, and the painted
    path can — a real user got an excellent one out of it. A brief that implies
    otherwise costs a human a rejection cycle for something the product could
    have done well the first time.
    """

    def test_the_3d_block_opens_by_saying_what_it_is_not_for(self):
        opening = seats.ART_3D_WORKFLOW.split("TEN STEPS", 1)[0]
        assert "CEILING" in opening
        assert "image_sprites" in opening, (
            "the 3D block has to name the painted path it is routing away to, "
            "or 'not a hero character' is a complaint rather than a route")
        assert "krea" in opening

    def test_the_two_dimensional_note_routes_the_same_way(self):
        note = seats._kind_note("art", "2d")
        assert "image_sprites" in note or "image_talkhead" in note


class TestTheBriefActuallyFitsItsCeiling:
    """BRIEF_CHARS was a suggestion, and nothing measured it.

    `_fit` walked a ladder of trims and returned whatever it had when the
    ladder ran out — so a brief could exceed the ceiling by two thirds and
    still report, truthfully and uselessly, that it HAD been shrunk. Measured
    on a real project: 40,198 characters against a 24,000 ceiling, ~10k tokens
    injected into every agent at startup and then re-sent on all 8,304 model
    calls of that night.

    Two causes, one symptom. `pinned_refs` was the largest field in the payload
    and appeared in no trim step; and blanking a bible body left the id, rank,
    version and two timestamps of every section behind, which was 10,038
    characters of nothing a seat can act on.
    """

    @pytest.fixture()
    def crowded(self, root):
        """A project big enough to blow the ceiling — which the default test
        project is not, and that is why this went unnoticed."""
        from bgate_core import bible, refs
        for i in range(40):
            bible.add(root, "constraint", f"chapter {i}",
                      body="prose about the world. " * 120)
        art = root / "art"
        art.mkdir(exist_ok=True)
        for i in range(30):
            src = art / f"ref_{i}.png"
            src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            refs.pin(root, f"reference number {i}", str(src),
                     note="why this reference matters, at length. " * 20)
        return root

    def test_a_crowded_project_still_gets_a_brief_under_the_ceiling(self, crowded):
        import json
        for role in seats.roles_for(crowded):
            payload = json.dumps(seats.brief(crowded, role), default=str)
            assert len(payload) <= seats.BRIEF_CHARS, (
                f"{role}: {len(payload)} chars against a "
                f"{seats.BRIEF_CHARS} ceiling")

    def test_what_was_cut_is_named_rather_than_silently_dropped(self, crowded):
        brief = seats.brief(crowded, "art")
        assert brief["truncated"], "a shrunk brief that says nothing was cut"
        assert "bible_read" in json.dumps(brief["truncated"]), (
            "the seat has to be told which tool pages the prose back")

    def test_a_trimmed_bible_keeps_titles_so_the_seat_knows_what_exists(
            self, crowded):
        # A group can legitimately be None — that chapter has no sections. The
        # assertion is about the ones that DO exist keeping their titles.
        sections = [s for group in seats.brief(crowded, "art")["bible"].values()
                    for s in (group if isinstance(group, list) else [group])
                    if isinstance(s, dict)]
        assert sections, "the bible was dropped rather than trimmed"
        assert all(s.get("title") for s in sections), (
            "a chapter with no title is one the seat cannot ask bible_read for")

    def test_a_trimmed_ref_keeps_its_path(self, crowded):
        for ref in seats.brief(crowded, "art")["pinned_refs"]:
            assert ref.get("path") or ref.get("logical_name"), (
                "a pinned ref trimmed past identification is not a pointer")
