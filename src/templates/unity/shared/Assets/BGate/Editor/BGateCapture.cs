// Builders Gate capture: render a scene's camera to a PNG from batchmode.
//
// Invoked by the adapter as
//     Unity -batchmode -quit -projectPath X -executeMethod BGate.Editor.BGateCapture.Run
// with the request in environment variables, because -executeMethod passes no
// arguments and argv parsing inside the editor is where paths with spaces die:
//     BGATE_SHOT_OUT    absolute path of the PNG to write (required)
//     BGATE_SHOT_SCENE  scene path under Assets/, or empty for the first
//                       enabled scene in Build Settings
//     BGATE_SHOT_W / BGATE_SHOT_H   pixel size, default 1280 x 720
//
// A STILL OF THE SAVED SCENE, NOT A FRAME OF PLAY. Batchmode never enters play
// mode, so nothing has run Start() or Update(): the picture is the level as
// laid out and lit. Lives under Editor/ so it compiles into the editor
// assembly only and never ships in a player.

using System;
using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace BGate.Editor
{
    public static class BGateCapture
    {
        public static void Run()
        {
            var outPath = Environment.GetEnvironmentVariable("BGATE_SHOT_OUT");
            var scenePath = Environment.GetEnvironmentVariable("BGATE_SHOT_SCENE");
            var width = ReadInt("BGATE_SHOT_W", 1280);
            var height = ReadInt("BGATE_SHOT_H", 720);
            if (string.IsNullOrEmpty(outPath))
            {
                Debug.LogError("BGateCapture: BGATE_SHOT_OUT is not set");
                EditorApplication.Exit(2);
                return;
            }

            if (string.IsNullOrEmpty(scenePath))
            {
                foreach (var entry in EditorBuildSettings.scenes)
                {
                    if (entry.enabled) { scenePath = entry.path; break; }
                }
            }
            if (string.IsNullOrEmpty(scenePath))
            {
                Debug.LogError("BGateCapture: no scene given and none enabled in Build Settings");
                EditorApplication.Exit(2);
                return;
            }

            try
            {
                EditorSceneManager.OpenScene(scenePath, OpenSceneMode.Single);
            }
            catch (Exception exc)
            {
                Debug.LogError("BGateCapture: cannot open " + scenePath + ": " + exc.Message);
                EditorApplication.Exit(2);
                return;
            }

            var camera = Camera.main;
            if (camera == null)
            {
                var all = UnityEngine.Object.FindObjectsOfType<Camera>();
                if (all.Length > 0) camera = all[0];
            }
            if (camera == null)
            {
                Debug.LogError("BGateCapture: " + scenePath + " has no Camera to render from");
                EditorApplication.Exit(2);
                return;
            }

            var rt = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32);
            var previous = camera.targetTexture;
            var active = RenderTexture.active;
            try
            {
                camera.targetTexture = rt;
                camera.Render();
                RenderTexture.active = rt;
                var tex = new Texture2D(width, height, TextureFormat.RGB24, false);
                tex.ReadPixels(new Rect(0, 0, width, height), 0, 0);
                tex.Apply();
                var dir = Path.GetDirectoryName(outPath);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                File.WriteAllBytes(outPath, tex.EncodeToPNG());
                UnityEngine.Object.DestroyImmediate(tex);
                Debug.Log("BGateCapture: wrote " + outPath + " from " + scenePath +
                          " via " + camera.name + " at " + width + "x" + height);
            }
            catch (Exception exc)
            {
                Debug.LogError("BGateCapture: render failed: " + exc.Message);
                EditorApplication.Exit(2);
                return;
            }
            finally
            {
                camera.targetTexture = previous;
                RenderTexture.active = active;
                UnityEngine.Object.DestroyImmediate(rt);
            }
        }

        static int ReadInt(string name, int fallback)
        {
            var raw = Environment.GetEnvironmentVariable(name);
            return int.TryParse(raw, out var value) && value > 0 ? value : fallback;
        }
    }
}
