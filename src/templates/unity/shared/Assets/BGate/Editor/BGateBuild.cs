// Builders Gate player build: a Windows standalone player from batchmode.
//
// Invoked by the adapter as
//     Unity -batchmode -quit -projectPath X -executeMethod BGate.Editor.BGateBuild.Player
// with BGATE_BUILD_OUT naming the .exe to write (environment, not argv, for
// the same reason BGateCapture reads it: paths with spaces). Builds every
// enabled scene in Build Settings, in order; the first is the one that boots.
// This is what playtest_agent_run and unity_play launch, because batchmode
// never enters play mode and a bot needs Update() to run.

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
            var outPath = Environment.GetEnvironmentVariable("BGATE_BUILD_OUT");
            if (string.IsNullOrEmpty(outPath))
            {
                Debug.LogError("BGateBuild: BGATE_BUILD_OUT is not set");
                EditorApplication.Exit(2);
                return;
            }
            var scenes = new List<string>();
            foreach (var s in EditorBuildSettings.scenes)
                if (s.enabled && !string.IsNullOrEmpty(s.path)) scenes.Add(s.path);
            if (scenes.Count == 0)
            {
                Debug.LogError("BGateBuild: no enabled scene in Build Settings; add one "
                               + "(File > Build Profiles) so the player has something to boot");
                EditorApplication.Exit(3);
                return;
            }
            var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = scenes.ToArray(),
                locationPathName = outPath,
                target = BuildTarget.StandaloneWindows64,
                targetGroup = BuildTargetGroup.Standalone,
                options = BuildOptions.None,
            });
            if (report == null)
            {
                // BuildPlayer logged its own exception (a refused path, say).
                Debug.LogError("BGateBuild: BuildPlayer returned no report for " + outPath);
                EditorApplication.Exit(1);
                return;
            }
            var summary = report.summary;
            Debug.Log("BGateBuild: " + summary.result + " " + outPath + " scenes="
                      + scenes.Count + " errors=" + summary.totalErrors);
            EditorApplication.Exit(summary.result == BuildResult.Succeeded ? 0 : 1);
        }
    }
}
