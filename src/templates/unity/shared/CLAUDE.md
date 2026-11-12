# __PROJECT_NAME__

A Builders Gate project. This file is what your Claude session reads first.

## What this is

A Unity game. Either `bgate init --engine unity` had the editor create it (a
starter scene at `Assets/Scenes/Main.unity`, a template player under
`Assets/Scripts/`, smoke tests under `Assets/Tests/EditMode/`), or `bgate
adopt` took it as it stands, without changing its render pipeline, packages
or editor version. Builders Gate's own scripts live under `Assets/BGate/`: the
telemetry MonoBehaviour and the editor capture/scaffold scripts. Delete that
folder and nothing else of Builders Gate is in the project.

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
- `unity_import_asset`: copies an image, model or sound into `Assets/` and
  has the editor import it (sprite and pixel-art settings on request). The
  result names the GUID and whether the asset can be placed.
- `unity_place_prefab`: instantiates a prefab or imported model into a scene
  at a position, under a parent, and saves the scene.
- `unity_set_field`: sets one serialized field on a component in a scene or
  a prefab, through the inspector's own path. Returns `before` and `after`.
  These three are how a scene changes. Never edit `.unity`, `.prefab` or
  `.meta` text by hand.
- `unity_build`: a Windows player of the Build Settings scenes into
  `Builds/BGatePlayer/`, reused until a file under `Assets/` changes.
- `unity_play`: runs that player for N seconds of game time with telemetry
  on and reports what it emitted and threw. Proof it RUNS, not just compiles.
  `inputs=["1:confirm", "1.5:+throttle", "4:-throttle"]` scripts presses
  (tap, hold, release) through `BGateInput`; `capture_every=2` saves a frame
  every 2 game seconds and returns their paths. Read the frames: that is
  the only way to SEE the running game.
- `playtest_agent_run` / `playtest_feel_check`: a decision model plays the
  player. It presses through `BGate.BGateInput` (`Declare`, `Held`, `Down`),
  which the template players already read beside the keyboard, and sees
  what an `IBGateObserver` MonoBehaviour returns. Give it `Progress` (0..1)
  and `Danger`, or the feel check reports the scene unmeasured.

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
