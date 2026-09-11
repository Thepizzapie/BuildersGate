# __PROJECT_NAME__

A Builders Gate project. This file is what your Claude session reads first.

## What this is

A Unity game, adopted as it stands: Builders Gate did not scaffold it and does
not change its render pipeline, packages or editor version. Two scripts live
under `Assets/BGate/`: the telemetry MonoBehaviour and an editor capture
script. Delete the folder and nothing else of Builders Gate is in the project.

## The loop

From an agent session the moves are:

- `engine_status`: is the editor found, which version, does it match
  `ProjectSettings/ProjectVersion.txt`.
- `engine_check`: a batchmode compile. The result's `errors` are the compile
  errors out of the editor log, file and line each.
- `unity_test_run`: the Unity Test Framework, EditMode by default; PlayMode
  when asked. Scored from its NUnit XML into the same history the Tests tab
  draws.
- `engine_screenshot`: the bundled editor script renders a scene's camera to a
  PNG. A still of the saved scene, not a frame of play.
- `unity_execute`: `-executeMethod` on a static method the project ships.

CLOSE THE EDITOR BEFORE ANY OF THEM. Two editors cannot hold one project; a
call made while the editor is open is refused with a sentence about
`Temp/UnityLockfile`, not run.

The first batchmode open of a project imports every asset and can take
minutes. Pass a longer `timeout` before reading a slow first check as a hang.

## Telemetry

`Assets/BGate/BGateTelemetry.cs` is the bridge between "it feels wrong" and a
number. It needs no scene wiring; it boots itself on the first scene load.

```csharp
BGate.BGateTelemetry.Emit("jump", ("air_time", 0.92f), ("peak_h", 2.4f));
```

It costs nothing when nobody is recording. `playtest_start` sets
`BGATE_TELEMETRY` for the editor it launches, and every event lands beside the
video and the voice track on one clock.

## What NOT to do

- Do not open the project in a newer editor than `ProjectVersion.txt` names.
  Unity upgrades a project the moment a newer editor opens it, and the
  serialized assets it rewrites are the diff nobody wanted.
- Do not commit `Library/`, `Temp/`, `Logs/` or `obj/`.
- Do not hand-write `.meta` files. The editor generates them on import.
