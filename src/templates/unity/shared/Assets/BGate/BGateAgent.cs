// Builders Gate agent: lets a decision model PLAY the game.
//
// SAME WIRE CONTRACT AS THE GODOT AUTOLOAD (addons/bgate/bgate_agent.gd). OFF
// unless BGATE_AGENT_URL is set (the loopback relay `playtest_agent_run`
// starts), so a normal launch costs one environment read.
//
// Every BGATE_AGENT_EVERY physics steps (default 6) it reads the game, POSTs
// {state, options, goal, progress, danger} to the relay, and holds the chosen
// option's actions in BGateInput until the next decision. The relay holds the
// API key and calls the model; the game never sees a key.
//
// LOCKSTEP (default on, BGATE_AGENT_LOCKSTEP=0 to turn off): Time.timeScale is
// 0 while a decision is in flight, so a 250 ms model call costs wall time, not
// game time. A game that sets timeScale to 0 itself (a pause menu) is left
// alone: not ours to play through.
//
// INPUT. Unity has no action map every project shares, so the game reads the
// bot through BGateInput beside its own keyboard read:
//     BGateInput.Declare("move_left", "move_right", "jump");   // once, in Awake
//     if (BGateInput.Held("move_left")) x -= 1f;
//     if (BGateInput.Down("jump")) Jump();                      // the press edge
// The template players already do this. Held() is false whenever no bot runs.
//
// WHAT IT SEES. A MonoBehaviour implementing IBGateObserver owns the answer:
//     public BGateObservation Observe() => new BGateObservation {
//         State = "...what the player can see...", Progress = 0.42f, Danger = false }
//         .Option("right", "run right", "move_right")
//         .Option("jump_right", "jump to the right", press: new[] {"move_right"},
//                 tap: new[] {"jump"});
// Progress (0..1) and Danger are what playtest_feel_check measures; without
// them it reports the scene unmeasured instead of passing it. Return null to
// say "not mine to describe" (a menu). Without an observer it builds a
// generic view: the object tagged Player, the nearest objects tagged Enemy,
// Hazard, Pickup or Finish (whichever tags the project defines), and one
// option per declared action plus "idle".
//
// BGATE_SCENE (a build-settings scene path or name) loads that scene first.
// BGATE_AGENT_SECONDS quits after that much game time (physics steps run).
// Every decision is emitted to BGateTelemetry as `agent_decision`.
//
// SCRIPTED INPUT, no model (unity_play's `inputs`): BGATE_SCRIPT is a list
// of `seconds:action` steps separated by ';', in game time. `+a` presses
// and holds, `-a` releases, a bare `a` taps (pressed for one frame, so
// Down() sees it). `1:confirm;1.5:+throttle;4:-throttle`.
// FRAMES: BGATE_CAPTURE_DIR + BGATE_CAPTURE_EVERY (game seconds) write
// frame_000.png, frame_001.png... there: what the player saw, which a
// batchmode screenshot of the saved scene cannot show. Needs a window.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;

namespace BGate
{
    /// <summary>The bot's controller. Every query is false when no bot runs.</summary>
    public static class BGateInput
    {
        static readonly HashSet<string> _held = new HashSet<string>();
        static readonly Dictionary<string, int> _downFrame = new Dictionary<string, int>();
        static readonly List<string> _declared = new List<string>();

        /// <summary>Name the actions the game reads, so the bot is offered them.</summary>
        public static void Declare(params string[] actions)
        {
            foreach (var a in actions)
                if (!string.IsNullOrEmpty(a) && !_declared.Contains(a)) _declared.Add(a);
        }

        public static IReadOnlyList<string> Declared => _declared;
        public static bool Held(string action) => _held.Contains(action);

        /// <summary>True on the frame the bot pressed it, like wasPressedThisFrame.</summary>
        public static bool Down(string action) =>
            _downFrame.TryGetValue(action, out var f) && f == Time.frameCount;

        internal static void Press(string action)
        {
            if (_held.Add(action)) _downFrame[action] = Time.frameCount;
        }

        internal static void Release(string action) => _held.Remove(action);
        internal static List<string> HeldNow() => new List<string>(_held);
    }

    /// <summary>Implement on a MonoBehaviour to tell the bot what it sees and may do.</summary>
    public interface IBGateObserver
    {
        BGateObservation Observe();
    }

    public sealed class BGateObservation
    {
        public string State = "";
        public string Goal;
        public float? Progress;
        public bool? Danger;
        internal readonly Dictionary<string, string> Options = new Dictionary<string, string>();
        internal readonly Dictionary<string, string[]> PressOf = new Dictionary<string, string[]>();
        internal readonly Dictionary<string, string[]> TapOf = new Dictionary<string, string[]>();

        /// <summary>One choice. `press` defaults to the action named `name`; a
        /// `tap` action is released and pressed again, for a fresh edge.</summary>
        public BGateObservation Option(string name, string desc, string[] press = null,
                                       string[] tap = null)
        {
            Options[name] = desc ?? "";
            PressOf[name] = press ?? new[] { name };
            TapOf[name] = tap ?? new string[0];
            return this;
        }

        public BGateObservation Option(string name, string desc, string press) =>
            Option(name, desc, new[] { press });
    }

    [DefaultExecutionOrder(-1000)]
    public sealed class BGateAgent : MonoBehaviour
    {
        static readonly string[] Tags = { "Enemy", "Hazard", "Pickup", "Finish" };
        const int MaxSeen = 12;

        static BGateAgent _instance;
        public static bool Active => _instance != null && !string.IsNullOrEmpty(_instance._url);

        string _url;
        int _every = 6;
        bool _lockstep = true;
        string _scene;
        int _tick;
        UnityWebRequest _req;
        bool _wePaused;
        float _savedScale = 1f;
        float _sentAt;
        float _seconds;
        float _played;
        BGateObservation _obs;
        readonly List<string> _pending = new List<string>();
        readonly List<(float at, string step)> _script = new List<(float, string)>();
        readonly List<string> _tapped = new List<string>();
        string _captureDir;
        float _captureEvery, _nextCapture;
        int _captured;

        [Serializable]
        class Reply
        {
            public string choice;
            public float confidence;
        }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Boot()
        {
            if (_instance != null) return;
            var url = Environment.GetEnvironmentVariable("BGATE_AGENT_URL");
            // BGATE_SCENE, BGATE_SCRIPT or BGATE_CAPTURE_DIR alone (unity_play)
            // boot it without a model.
            if (string.IsNullOrEmpty(url) && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("BGATE_SCENE"))
                && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("BGATE_SCRIPT"))
                && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("BGATE_CAPTURE_DIR")))
                return;
            var go = new GameObject("BGateAgent");
            DontDestroyOnLoad(go);
            _instance = go.AddComponent<BGateAgent>();
            _instance._url = url;
            if (int.TryParse(Environment.GetEnvironmentVariable("BGATE_AGENT_EVERY"),
                             out var every) && every > 0)
                _instance._every = every;
            _instance._lockstep = Environment.GetEnvironmentVariable("BGATE_AGENT_LOCKSTEP") != "0";
            _instance._scene = Environment.GetEnvironmentVariable("BGATE_SCENE");
            float.TryParse(Environment.GetEnvironmentVariable("BGATE_AGENT_SECONDS"), NumberStyles.Float,
                           CultureInfo.InvariantCulture, out _instance._seconds);
            _instance.ParseScript(Environment.GetEnvironmentVariable("BGATE_SCRIPT"));
            _instance._captureDir = Environment.GetEnvironmentVariable("BGATE_CAPTURE_DIR");
            float.TryParse(Environment.GetEnvironmentVariable("BGATE_CAPTURE_EVERY"), NumberStyles.Float,
                           CultureInfo.InvariantCulture, out _instance._captureEvery);
            if (!string.IsNullOrEmpty(_instance._captureDir) && _instance._captureEvery <= 0f)
                _instance._captureEvery = 1f;
            _instance._nextCapture = _instance._captureEvery;
        }

        void ParseScript(string text)
        {
            if (string.IsNullOrEmpty(text)) return;
            foreach (var raw in text.Split(';'))
            {
                var part = raw.Trim();
                var colon = part.IndexOf(':');
                if (colon <= 0) continue;
                if (!float.TryParse(part.Substring(0, colon), NumberStyles.Float, CultureInfo.InvariantCulture, out var at))
                    continue;
                var step = part.Substring(colon + 1).Trim();
                if (step.Length > 0) _script.Add((at, step));
            }
            _script.Sort((a, b) => a.at.CompareTo(b.at));
        }

        // Runs first in the frame (execution order), so a press made here is
        // the frame's Down() edge for every game script.
        void RunScript()
        {
            foreach (var a in _tapped) BGateInput.Release(a);
            _tapped.Clear();
            while (_script.Count > 0 && _script[0].at <= _played)
            {
                var step = _script[0].step;
                _script.RemoveAt(0);
                if (step[0] == '+') BGateInput.Press(step.Substring(1));
                else if (step[0] == '-') BGateInput.Release(step.Substring(1));
                else { BGateInput.Release(step); BGateInput.Press(step); _tapped.Add(step); }
                BGateTelemetry.Emit("script_step", ("at", _played), ("step", step));
            }
        }

        void Capture()
        {
            if (string.IsNullOrEmpty(_captureDir) || _played < _nextCapture) return;
            _nextCapture += _captureEvery;
            System.IO.Directory.CreateDirectory(_captureDir);
            var path = System.IO.Path.Combine(_captureDir, "frame_" + _captured.ToString("000") + ".png");
            _captured++;
            ScreenCapture.CaptureScreenshot(path);
            BGateTelemetry.Emit("frame", ("at", _played), ("path", path));
        }

        void Start()
        {
            if (string.IsNullOrEmpty(_scene)) return;
            var active = SceneManager.GetActiveScene();
            if (active.path == _scene || active.name == _scene) return;
            SceneManager.LoadScene(_scene);
        }

        void Update()
        {
            // Pressed a frame after their release, so a tapped action makes a
            // fresh Down() edge. Runs before every game script (execution order).
            if (_pending.Count > 0)
            {
                foreach (var a in _pending) BGateInput.Press(a);
                _pending.Clear();
            }
            RunScript();
            Capture();
            if (_req == null || !_req.isDone) return;
            var req = _req;
            _req = null;
            if (_wePaused)
            {
                Time.timeScale = _savedScale;
                _wePaused = false;
            }
            if (req.result != UnityWebRequest.Result.Success)
            {
                req.Dispose();
                return;
            }
            Reply got = null;
            try { got = JsonUtility.FromJson<Reply>(req.downloadHandler.text); }
            catch (Exception) { }
            req.Dispose();
            if (got == null || string.IsNullOrEmpty(got.choice) || _obs == null) return;
            _obs.PressOf.TryGetValue(got.choice, out var keep);
            _obs.TapOf.TryGetValue(got.choice, out var tap);
            keep = keep ?? new string[0];
            tap = tap ?? new string[0];
            // HOLD WHAT IS CHOSEN AGAIN, RELEASE THE REST (the Godot agent's
            // measured rule: releasing everything cut every held jump short).
            foreach (var a in BGateInput.HeldNow())
                if (Array.IndexOf(keep, a) < 0 || Array.IndexOf(tap, a) >= 0)
                    BGateInput.Release(a);
            foreach (var a in keep)
            {
                if (!BGateInput.Held(a)) _pending.Add(a);
            }
            BGateTelemetry.Emit("agent_decision", ("choice", got.choice),
                ("confidence", got.confidence),
                ("ms", (int)((Time.realtimeSinceStartup - _sentAt) * 1000f)),
                ("frame", _tick));
        }

        void FixedUpdate()
        {
            // THE RUN'S LENGTH IN GAME TIME, kept here rather than left to the
            // telemetry script's BGATE_AUTOQUIT: an adopted project keeps the
            // telemetry copy it was first given, and an older one counts wall
            // time, which lockstep pauses would eat.
            _played += Time.fixedDeltaTime;
            if (_seconds > 0f && _played >= _seconds)
            {
                _seconds = 0f;
                BGateTelemetry.Emit("autoquit", ("after", _played));
                Application.Quit();
                return;
            }
            if (_req != null || string.IsNullOrEmpty(_url)) return;
            _tick++;
            if (_tick % _every != 0) return;
            _obs = Observe();
            if (_obs.Options.Count == 0) DefaultOptions(_obs);
            var sb = new StringBuilder(512);
            sb.Append("{\"state\":").Append(Q(_obs.State ?? ""));
            sb.Append(",\"options\":{");
            var first = true;
            foreach (var kv in _obs.Options)
            {
                if (!first) sb.Append(',');
                first = false;
                sb.Append(Q(kv.Key)).Append(':').Append(Q(kv.Value));
            }
            sb.Append("},\"frame\":").Append(_tick);
            sb.Append(",\"hold_s\":").Append((_every * Time.fixedDeltaTime).ToString("R", CultureInfo.InvariantCulture));
            if (!string.IsNullOrEmpty(_obs.Goal)) sb.Append(",\"goal\":").Append(Q(_obs.Goal));
            if (_obs.Progress.HasValue)
                sb.Append(",\"progress\":").Append(_obs.Progress.Value.ToString("R", CultureInfo.InvariantCulture));
            if (_obs.Danger.HasValue) sb.Append(",\"danger\":").Append(_obs.Danger.Value ? "true" : "false");
            sb.Append('}');

            var req = new UnityWebRequest(_url, "POST");
            req.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(sb.ToString()));
            req.downloadHandler = new DownloadHandlerBuffer();
            req.SetRequestHeader("Content-Type", "application/json");
            req.timeout = 20;
            _sentAt = Time.realtimeSinceStartup;
            req.SendWebRequest();
            _req = req;
            if (_lockstep && Time.timeScale > 0f)
            {
                _savedScale = Time.timeScale;
                Time.timeScale = 0f;
                _wePaused = true;
            }
        }

        // Its own JSON quoting rather than BGateTelemetry's: an adopted
        // project keeps whatever telemetry script it was given first, and the
        // agent must compile beside any version of it.
        static string Q(string text)
        {
            var sb = new StringBuilder(text.Length + 2);
            sb.Append('"');
            foreach (var c in text)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }

        static BGateObservation Observe()
        {
            foreach (var mb in FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None))
            {
                if (mb is IBGateObserver o && mb.isActiveAndEnabled)
                {
                    var got = o.Observe();
                    if (got != null) return got;
                }
            }
            return new BGateObservation { State = GenericState() };
        }

        static void DefaultOptions(BGateObservation obs)
        {
            obs.Option("idle", "press nothing", new string[0]);
            foreach (var a in BGateInput.Declared)
                obs.Option(a, "hold " + a.Replace('_', ' '), a);
        }

        static string GenericState()
        {
            var sb = new StringBuilder(256);
            sb.Append("scene: ").Append(SceneManager.GetActiveScene().name).Append('\n');
            var player = GameObject.FindWithTag("Player");
            var origin = player != null ? player.transform.position : Vector3.zero;
            if (player != null) sb.Append("player: ").Append(Describe(player, null)).Append('\n');
            var seen = new List<(float dist, string line)>();
            foreach (var tag in Tags)
            {
                GameObject[] found;
                try { found = GameObject.FindGameObjectsWithTag(tag); }
                catch (UnityException) { continue; }   // tag not defined in this project
                foreach (var go in found)
                {
                    if (go == player || !go.activeInHierarchy) continue;
                    var dist = player != null ? Vector3.Distance(go.transform.position, origin) : 0f;
                    seen.Add((dist, tag.ToLowerInvariant() + " " + Describe(go, player)));
                }
            }
            seen.Sort((a, b) => a.dist.CompareTo(b.dist));
            if (seen.Count > 0) sb.Append("nearby:\n");
            for (var i = 0; i < seen.Count && i < MaxSeen; i++) sb.Append("- ").Append(seen[i].line).Append('\n');
            return sb.ToString();
        }

        static string Describe(GameObject go, GameObject relTo)
        {
            var p = go.transform.position;
            string F(float v) => v.ToString("0.0", CultureInfo.InvariantCulture);
            var sb = new StringBuilder(go.name);
            if (relTo != null)
            {
                var d = p - relTo.transform.position;
                sb.Append(", dx ").Append(F(d.x)).Append(", dy ").Append(F(d.y));
                if (Mathf.Abs(d.z) > 0.05f) sb.Append(", dz ").Append(F(d.z));
                sb.Append(", dist ").Append(F(d.magnitude));
            }
            else
            {
                sb.Append(", x ").Append(F(p.x)).Append(", y ").Append(F(p.y));
                if (Mathf.Abs(p.z) > 0.05f) sb.Append(", z ").Append(F(p.z));
            }
            if (go.TryGetComponent<Rigidbody2D>(out var b2))
                sb.Append(", vx ").Append(F(b2.linearVelocity.x)).Append(", vy ").Append(F(b2.linearVelocity.y));
            else if (go.TryGetComponent<Rigidbody>(out var b3))
                sb.Append(", velocity ").Append(F(b3.linearVelocity.x)).Append(' ')
                  .Append(F(b3.linearVelocity.y)).Append(' ').Append(F(b3.linearVelocity.z));
            else if (go.TryGetComponent<CharacterController>(out var cc))
                sb.Append(", on_floor ").Append(cc.isGrounded ? "yes" : "no");
            return sb.ToString();
        }
    }
}
