// Builders Gate player build: a Windows standalone player from batchmode.
//
// Invoked by the adapter as
//     Unity -batchmode -quit -projectPath X -executeMethod BGate.Editor.BGateBuild.Player
// with BGATE_BUILD_OUT naming the .exe to write (environment, not argv, for
// the same reason BGateCapture reads it: paths with spaces). Builds every
// enabled scene in Build Settings, in order; the first is the one that boots.
// This is what playtest_agent_run and unity_play launch, because batchmode
// never enters play mode and a bot needs Update() to run. BGateBuild.WebGL
// is the same build for the browser: what the arcade and the phone serve.

using System;
using System.Collections.Generic;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace BGate.Editor
{
    public static class BGateBuild
    {
        public static void Player()
        {
            EditorApplication.Exit(Build(BuildTarget.StandaloneWindows64, BuildTargetGroup.Standalone));
        }

        // A WebGL build into the DIRECTORY BGATE_BUILD_OUT names: index.html,
        // Build/ and TemplateData/, what the arcade and the phone serve.
        // Compression is switched off for the build and put back after: a
        // gzip/brotli build needs Content-Encoding headers a plain static
        // host does not send, and the project's own setting is not ours to
        // rewrite.
        public static void WebGL()
        {
            if (!BuildPipeline.IsBuildTargetSupported(BuildTargetGroup.WebGL, BuildTarget.WebGL))
            {
                Debug.LogError("BGateBuild: WebGL Build Support is not installed for this editor");
                EditorApplication.Exit(4);
                return;
            }
            var compression = PlayerSettings.WebGL.compressionFormat;
            PlayerSettings.WebGL.compressionFormat = WebGLCompressionFormat.Disabled;
            int code;
            try
            {
                code = Build(BuildTarget.WebGL, BuildTargetGroup.WebGL);
            }
            finally
            {
                // Restored before Exit, which ends the process on the spot.
                PlayerSettings.WebGL.compressionFormat = compression;
            }
            EditorApplication.Exit(code);
        }

        // The exit code; the caller exits, so WebGL() can restore settings first.
        static int Build(BuildTarget target, BuildTargetGroup group)
        {
            var outPath = Environment.GetEnvironmentVariable("BGATE_BUILD_OUT");
            if (string.IsNullOrEmpty(outPath))
            {
                Debug.LogError("BGateBuild: BGATE_BUILD_OUT is not set");
                return 2;
            }
            var scenes = new List<string>();
            foreach (var s in EditorBuildSettings.scenes)
                if (s.enabled && !string.IsNullOrEmpty(s.path)) scenes.Add(s.path);
            if (scenes.Count == 0)
            {
                Debug.LogError("BGateBuild: no enabled scene in Build Settings; add one "
                               + "(File > Build Profiles) so the player has something to boot");
                return 3;
            }
            var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = scenes.ToArray(),
                locationPathName = outPath,
                target = target,
                targetGroup = group,
                options = BuildOptions.None,
            });
            if (report == null)
            {
                // BuildPlayer logged its own exception (a refused path, say).
                Debug.LogError("BGateBuild: BuildPlayer returned no report for " + outPath);
                return 1;
            }
            var summary = report.summary;
            Debug.Log("BGateBuild: " + summary.result + " " + target + " " + outPath
                      + " scenes=" + scenes.Count + " errors=" + summary.totalErrors);
            return summary.result == BuildResult.Succeeded ? 0 : 1;
        }
    }
}
