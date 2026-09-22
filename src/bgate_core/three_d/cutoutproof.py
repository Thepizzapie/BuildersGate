"""The rig's EYES: a proof sheet of every clip, rendered in the engine after
every write, handed back to the agent as an image.

USER DIRECTIVE (2026-09-22): "give the art agent eyes to look at this instead
of guessing every time." Until now a cutout write returned a JSON report -
sprite counts, clip names, byte sizes - and the agent judged a rig it had
never seen. The human judged it on sight and it was a paper doll: a gun
hanging at 45 degrees in the aim, an arm floating off a torso, a death that
rotated the standing figure.

WHAT THIS DOES. After the emitter writes the scene, a small gym scene is
written beside the character (one rig per clip, paused at a telling time,
labelled), the engine renders it once, and the PNG comes back in the tool
result as image content, beside the JSON. The agent sees the rig the way
the human will. The completion gate then asks for that image: a rig write
with no proof rendered is not 'done'.

The gym lives in the character's own folder (art's lane) and is regenerated
on every write; the shot lands under .bgate_out/shots/ like every other
render so the evidence gate recognises it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

from . import cutout

# One rig per pose, at the moment that shows the clip's shape. Two rows of
# seven at 1280x720; the rig is scaled so a 200 px figure reads.
PROOF_POSES: list[tuple[str, float]] = [
    ("idle", 0.5), ("walk", 0.2), ("run", 0.0), ("run", 0.28), ("jump", 0.35),
    ("fall", 0.2), ("crouch", 0.3),
    ("slide", 0.1), ("aim", 0.2), ("fire", 0.04), ("attack_melee", 0.24),
    ("hurt", 0.08), ("death", 0.5), ("death", 1.2),
]
COLS = 7
VIEW = (1280, 720)
RIG_SCALE = 1.25

# What the agent is told to look for. Every line is a failure the human
# found on a rig an agent had reported as done.
LOOK = (
    "THE PROOF SHEET IS IN THIS RESPONSE - LOOK AT IT BEFORE YOU DO ANYTHING "
    "ELSE. Judge it as the player will: (1) does every limb stay attached to "
    "the body in every pose, no gap at a shoulder, hip or knee; (2) is any "
    "part floating, doubled, or the wrong way round; (3) does the weapon lie "
    "along the forearm and sit in the hand in aim, fire and run; (4) do the "
    "parts share one style and one scale with each other and the figure; "
    "(5) do crouch, slide and death read as those actions and not as a "
    "standing figure moved. One wrong answer is a part to rerun "
    "(cutout_part_rerun), a pivot to author (cutout_equip / cutout_assemble "
    "pivot=), or a bone adjustment - not a reason to report done. Say in your "
    "result note what you saw on the sheet.")

# Injectable for tests: (project_dir, out_path, at=, scene=, timeout=) -> dict
shooter: Optional[Callable] = None


def _shoot(project_dir, out_path, *, at: float, scene: str, timeout: int) -> dict:
    fn = shooter
    if fn is None:
        from bgate_adapters import godot as _godot
        fn = _godot.screenshot
    return fn(str(project_dir), str(out_path), at=at, scene=scene, timeout=timeout)


def _res(project_dir: Path, path: Path) -> str:
    rel = Path(path).resolve().relative_to(Path(project_dir).resolve())
    return "res://" + rel.as_posix()


def write_gym(project_dir: str | os.PathLike[str], name: str,
              scene_path: str | os.PathLike[str], *,
              poses: Optional[list] = None,
              out_dir: Optional[str | os.PathLike[str]] = None) -> dict:
    """Write the proof gym scene and script beside the character.

    Returns ``{gym, gym_res, script, poses}``. Clips the rig does not have
    are still placed: the script logs them and the rig shows its rest pose
    under the label, which is itself a finding.
    """
    project = Path(project_dir)
    scene = Path(scene_path)
    home = Path(out_dir) if out_dir else scene.parent / "proof"
    home.mkdir(parents=True, exist_ok=True)
    poses = [tuple(p) for p in (poses or PROOF_POSES)]
    known = set(cutout.clip_names())
    cell_w, cell_h = VIEW[0] // COLS, VIEW[1] // 2
    nodes, table = [], {}
    for i, (clip, at) in enumerate(poses):
        node = f"P{i}"
        r, c = divmod(i, COLS)
        x = c * cell_w + cell_w // 2
        y = r * cell_h + cell_h - 44
        nodes.append(
            f'[node name="{node}" parent="." instance=ExtResource("2_rig")]\n'
            f'position = Vector2({x}, {y})\n'
            f'scale = Vector2({RIG_SCALE}, {RIG_SCALE})\n')
        table[node] = [clip, float(at), clip in known]
    script = (
        "extends Node2D\n"
        "## Builders Gate rig proof gym - regenerated on every cutout write.\n"
        "## One rig per pose, paused at a telling time, labelled.\n"
        f"const POSES := {json.dumps(table)}\n\n"
        "func _ready() -> void:\n"
        "\tawait get_tree().process_frame\n"
        "\tfor n in POSES:\n"
        "\t\tvar rig := get_node_or_null(NodePath(n))\n"
        "\t\tif rig == null:\n"
        "\t\t\tcontinue\n"
        "\t\tvar lbl := Label.new()\n"
        "\t\tlbl.text = \"%s @%.2f\" % [POSES[n][0], POSES[n][1]]\n"
        "\t\tlbl.position = Vector2(-56, 18)\n"
        "\t\tlbl.add_theme_color_override(\"font_color\", Color.BLACK)\n"
        "\t\trig.add_child(lbl)\n"
        "\t\tif not rig.has_method(\"play\") or not rig.play(StringName(POSES[n][0])):\n"
        "\t\t\tlbl.text += \"  (NO CLIP)\"\n"
        "\t\t\tcontinue\n"
        "\t\trig.seek_quiet(POSES[n][1])\n"
        "\t\tvar ap := rig.get_node_or_null(\"AnimationPlayer\")\n"
        "\t\tif ap != null:\n"
        "\t\t\tap.pause()\n")
    gym = home / f"{name}_proof_gym.tscn"
    gd = home / f"{name}_proof_gym.gd"
    gd.write_text(script, encoding="utf-8")
    text = (
        "[gd_scene load_steps=3 format=3]\n\n"
        f'[ext_resource type="Script" path="{_res(project, gd)}" id="1_gym"]\n'
        f'[ext_resource type="PackedScene" path="{_res(project, scene)}" id="2_rig"]\n\n'
        '[node name="RigProof" type="Node2D"]\nscript = ExtResource("1_gym")\n\n'
        '[node name="Backdrop" type="ColorRect" parent="."]\n'
        f"offset_right = {VIEW[0]}.0\noffset_bottom = {VIEW[1]}.0\n"
        "color = Color(0.82, 0.8, 0.76, 1)\n\n" + "\n".join(nodes))
    gym.write_text(text, encoding="utf-8")
    return {"gym": str(gym), "gym_res": _res(project, gym), "script": str(gd),
            "poses": [[c, a] for c, a in poses]}


def proof(project_dir: str | os.PathLike[str], name: str,
          scene_path: str | os.PathLike[str], *, poses: Optional[list] = None,
          at: float = 1.0, timeout: int = 120) -> dict:
    """Write the gym and render it. Never raises: a rig write must not fail
    because the engine could not open a window; the missing proof is
    reported and the completion gate asks for it.

    Returns ``{ok, image, gym_res, poses, errors, look}``.
    """
    project = Path(project_dir)
    try:
        gym = write_gym(project, name, scene_path, poses=poses)
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "image": "", "error": f"gym not written: {exc}",
                "look": LOOK}
    shots = project / ".bgate_out" / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    out = shots / f"rigproof-{name}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    try:
        got = _shoot(project, out, at=at, scene=gym["gym_res"], timeout=timeout) or {}
    except Exception as exc:                                   # noqa: BLE001
        got = {"ok": False, "error": str(exc)}
    image = str(got.get("path") or out) if got.get("ok") and out.is_file() else ""
    return {"ok": bool(image), "image": image, "gym_res": gym["gym_res"],
            "poses": gym["poses"], "errors": list(got.get("errors") or []),
            "error": "" if image else str(got.get("error") or "no frame captured"),
            "look": LOOK}


def latest_proof(project_dir: str | os.PathLike[str], name: str) -> Optional[Path]:
    shots = Path(project_dir) / ".bgate_out" / "shots"
    if not shots.is_dir():
        return None
    found = sorted(shots.glob(f"rigproof-{name}-*.png"))
    return found[-1] if found else None
