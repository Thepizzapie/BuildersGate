# Meridian Scout

Original low-poly, single-engine bush plane. Flat-shaded geometry with eight
editable material colors; no textures or external asset dependencies.

## Use in Godot 4

Copy this folder into your project. Drag `parked_plane.tscn` into a scene for
a grounded aircraft with simple collision. Drag `meridian_scout.tscn` under
your own vehicle body for the animated visual model without nested collision.
The scene dependencies use relative paths, so the folder can be relocated.

The model is 9.44 metres across, faces **+Z**, and uses **+Y** up. The origin
is at ground level beneath the cabin. Materials are named by purpose.

`plane_visual.gd` exposes propeller RPM, roll, pitch, rudder, flap extension,
and wheel speed. These animate the separate mesh pivots. They do not implement
flight physics, player entry, or an engine sound.

Named mounting points inside `Model`: `PilotSeat`, `CockpitCamera`,
`EntryLeft`, `EntryRight`, `CenterOfMass`, and `ChaseCamera`.

`meridian_scout.glb` also works independently in other glTF-compatible engines.
`meridian_scout.blend` is the editable source. Its studio collection is not
included in the GLB. `build_plane.py` regenerates the model using Blender 5.2.
`metrics.json` records the exported geometry budget.
