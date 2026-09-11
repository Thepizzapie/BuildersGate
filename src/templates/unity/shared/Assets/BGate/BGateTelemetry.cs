// Builders Gate telemetry: the bridge between "it feels wrong" and a number.
//
// SAME WIRE CONTRACT AS THE GODOT AUTOLOAD. One JSON object per line, appended
// to the file named by the BGATE_TELEMETRY environment variable. When that
// variable is unset (you pressed Play, or opened a build normally) this does
// nothing at all: no file, no allocation past the first check, no error. The
// recorder ingests the file when the session stops.
//
// Every event carries `ts`, a UNIX WALL-CLOCK timestamp, not seconds since
// launch. The recorder's clock starts when recording starts, which is not when
// the game starts; wall clock is the only axis the two share. `t` is included
// for human reading, but the aligner uses `ts`.
//
// NO SCENE WIRING. [RuntimeInitializeOnLoadMethod] creates the singleton on
// the first scene load, so adopting a project is one file under Assets/BGate.
//
// Usage from anywhere:
//     BGateTelemetry.Emit("jump", ("air_time", 0.92f), ("peak_h", 2.4f));
//     BGateTelemetry.Emit("hit", ("enemy", "grunt"), ("damage", 12));
//
// BGATE_AUTOQUIT=<seconds> quits the player unattended after that long, for
// headless smoke runs where nobody is there to close the window.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using UnityEngine;

namespace BGate
{
    public sealed class BGateTelemetry : MonoBehaviour
    {
        public const int SchemaVersion = 1;
        const float FlushInterval = 1.0f;
        const float FpsInterval = 2.0f;

        static BGateTelemetry _instance;
        StreamWriter _writer;
        float _t0;
        float _sinceFlush;
        float _fpsAccum;
        float _autoquitAfter;
        float _elapsed;

        public static bool Enabled => _instance != null && _instance._writer != null;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Boot()
        {
            if (_instance != null) return;
            var path = Environment.GetEnvironmentVariable("BGATE_TELEMETRY");
            var autoquit = Environment.GetEnvironmentVariable("BGATE_AUTOQUIT");
            if (string.IsNullOrEmpty(path) && string.IsNullOrEmpty(autoquit)) return;

            var go = new GameObject("BGateTelemetry");
            DontDestroyOnLoad(go);
            _instance = go.AddComponent<BGateTelemetry>();
            _instance.Open(path, autoquit);
        }

        void Open(string path, string autoquit)
        {
            _t0 = Time.realtimeSinceStartup;
            if (!string.IsNullOrEmpty(autoquit) &&
                float.TryParse(autoquit, NumberStyles.Float, CultureInfo.InvariantCulture, out var seconds))
                _autoquitAfter = seconds;
            if (string.IsNullOrEmpty(path)) return;
            try
            {
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                _writer = new StreamWriter(path, true, new UTF8Encoding(false));
                Emit("session_start", ("engine", "unity"),
                     ("unity_version", Application.unityVersion),
                     ("product", Application.productName));
            }
            catch (Exception exc)
            {
                Debug.LogWarning("BGateTelemetry: cannot open " + path + ": " + exc.Message);
                _writer = null;
            }
        }

        void Update()
        {
            var dt = Time.unscaledDeltaTime;
            _elapsed += dt;
            if (_autoquitAfter > 0 && _elapsed >= _autoquitAfter)
            {
                Emit("autoquit", ("after", _autoquitAfter));
                Flush();
                Application.Quit();
#if UNITY_EDITOR
                UnityEditor.EditorApplication.isPlaying = false;
#endif
                _autoquitAfter = 0;
            }
            if (_writer == null) return;
            _fpsAccum += dt;
            if (_fpsAccum >= FpsInterval)
            {
                _fpsAccum = 0;
                Emit("fps", ("fps", dt > 0 ? 1.0f / dt : 0f));
            }
            _sinceFlush += dt;
            if (_sinceFlush >= FlushInterval)
            {
                _sinceFlush = 0;
                Flush();
            }
        }

        void OnApplicationQuit()
        {
            Emit("session_end");
            Flush();
            _writer?.Dispose();
            _writer = null;
        }

        void Flush()
        {
            // A crash mid-session would otherwise lose exactly the events that
            // explain the crash. Flush on a timer so the tail always survives.
            try { _writer?.Flush(); } catch (Exception) { }
        }

        /// <summary>Record one event. `kind` is a short name; each pair is one data field.</summary>
        public static void Emit(string kind, params (string key, object value)[] data)
        {
            if (_instance == null || _instance._writer == null) return;
            _instance.Write(kind, data);
        }

        void Write(string kind, (string key, object value)[] data)
        {
            var sb = new StringBuilder(128);
            sb.Append("{\"schema\":").Append(SchemaVersion);
            sb.Append(",\"ts\":").Append((DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0)
                                          .ToString("F3", CultureInfo.InvariantCulture));
            sb.Append(",\"t\":").Append((Time.realtimeSinceStartup - _t0)
                                         .ToString("F3", CultureInfo.InvariantCulture));
            sb.Append(",\"kind\":").Append(Quote(kind));
            sb.Append(",\"data\":{");
            for (var i = 0; i < data.Length; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(Quote(data[i].key)).Append(':').Append(Json(data[i].value));
            }
            sb.Append("}}");
            try { _writer.WriteLine(sb.ToString()); } catch (Exception) { }
        }

        // A small JSON writer rather than JsonUtility, which cannot serialise a
        // dictionary or a bare value and would need a class per event shape.
        static string Json(object value)
        {
            switch (value)
            {
                case null: return "null";
                case bool b: return b ? "true" : "false";
                case string s: return Quote(s);
                case float f: return f.ToString("R", CultureInfo.InvariantCulture);
                case double d: return d.ToString("R", CultureInfo.InvariantCulture);
                case int i: return i.ToString(CultureInfo.InvariantCulture);
                case long l: return l.ToString(CultureInfo.InvariantCulture);
                case Vector2 v2: return "[" + Json(v2.x) + "," + Json(v2.y) + "]";
                case Vector3 v3: return "[" + Json(v3.x) + "," + Json(v3.y) + "," + Json(v3.z) + "]";
                case IEnumerable<object> list:
                {
                    var parts = new List<string>();
                    foreach (var item in list) parts.Add(Json(item));
                    return "[" + string.Join(",", parts) + "]";
                }
                default: return Quote(value.ToString());
            }
        }

        static string Quote(string text)
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
    }
}
