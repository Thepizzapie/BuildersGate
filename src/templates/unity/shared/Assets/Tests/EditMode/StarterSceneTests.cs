// Smoke tests for the scaffolded starter scene, run by `unity_test_run`.
//
// They check what the rest of the loop assumes: the main scene is first in the
// build, it opens, and it holds a camera and a Player with a controller.
// Replace them with your own game's tests as it grows.

using NUnit.Framework;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace BGate.Tests
{
    public class StarterSceneTests
    {
        const string ScenePath = "Assets/Scenes/Main.unity";

        [Test]
        public void MainSceneIsFirstInBuild()
        {
            Assert.That(EditorBuildSettings.scenes.Length, Is.GreaterThan(0), "no scenes in Build Settings");
            Assert.AreEqual(ScenePath, EditorBuildSettings.scenes[0].path);
        }

        [Test]
        public void MainSceneHasCameraAndPlayer()
        {
            EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
            Assert.IsNotNull(Object.FindFirstObjectByType<Camera>(), "no Camera in " + ScenePath);
            var player = GameObject.Find("Player");
            Assert.IsNotNull(player, "no GameObject named Player in " + ScenePath);
            Assert.That(player.GetComponents<MonoBehaviour>().Length, Is.GreaterThan(0),
                "Player has no controller script");
        }
    }
}
