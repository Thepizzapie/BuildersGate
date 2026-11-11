// Builders Gate scaffold, 3D: saves the starter scene from code.
//
// Run once by `bgate init --engine unity --kind 3d` as
//     Unity -batchmode -quit -projectPath X -executeMethod BGate.Editor.BGateScaffold.Build
// The scene is BUILT, not copied: a hand-written .unity file and its .meta
// GUIDs are exactly what a template must never ship, so the editor writes both.
// Leaves an existing Assets/Scenes/Main.unity alone. Safe to delete afterwards.

using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace BGate.Editor
{
    public static class BGateScaffold
    {
        const string ScenePath = "Assets/Scenes/Main.unity";

        public static void Build()
        {
            EditorSettings.defaultBehaviorMode = EditorBehaviorMode.Mode3D;
            if (File.Exists(ScenePath))
            {
                Debug.Log("BGateScaffold: " + ScenePath + " exists, left alone");
                EnsureInBuild();
                return;
            }

            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

            var camGo = new GameObject("Main Camera");
            camGo.tag = "MainCamera";
            camGo.transform.position = new Vector3(0f, 5f, -9f);
            camGo.transform.rotation = Quaternion.Euler(25f, 0f, 0f);
            var cam = camGo.AddComponent<Camera>();
            cam.clearFlags = CameraClearFlags.SolidColor;
            cam.backgroundColor = new Color(0.11f, 0.12f, 0.15f);

            var sun = new GameObject("Sun");
            sun.transform.rotation = Quaternion.Euler(50f, -30f, 0f);
            var light = sun.AddComponent<Light>();
            light.type = LightType.Directional;
            light.intensity = 1.1f;
            light.shadows = LightShadows.Soft;

            var ground = GameObject.CreatePrimitive(PrimitiveType.Plane);
            ground.name = "Ground";
            ground.transform.localScale = new Vector3(3f, 1f, 3f);
            Paint(ground, "Ground", new Color(0.22f, 0.25f, 0.30f));

            var ledge = GameObject.CreatePrimitive(PrimitiveType.Cube);
            ledge.name = "Ledge";
            ledge.transform.position = new Vector3(4f, 0.75f, 3f);
            ledge.transform.localScale = new Vector3(3f, 1.5f, 3f);
            Paint(ledge, "Ledge", new Color(0.27f, 0.31f, 0.37f));

            var player = GameObject.CreatePrimitive(PrimitiveType.Capsule);
            player.name = "Player";
            player.transform.position = new Vector3(0f, 1.1f, 0f);
            Object.DestroyImmediate(player.GetComponent<CapsuleCollider>());
            var cc = player.AddComponent<CharacterController>();
            cc.height = 2f;
            cc.radius = 0.5f;
            Paint(player, "Player", new Color(0.85f, 0.35f, 0.20f));
            // By name, so deleting the template script does not break the scaffold.
            var controller = System.Type.GetType("PlayerController, Assembly-CSharp");
            if (controller != null) player.AddComponent(controller);
            else Debug.LogWarning("BGateScaffold: PlayerController not found; Player has no controller");

            Directory.CreateDirectory("Assets/Scenes");
            if (!EditorSceneManager.SaveScene(scene, ScenePath))
            {
                Debug.LogError("BGateScaffold: could not save " + ScenePath);
                EditorApplication.Exit(2);
                return;
            }
            EnsureInBuild();
            AssetDatabase.SaveAssets();
            Debug.Log("BGateScaffold: saved " + ScenePath);
        }

        // One material asset per role, so recolouring the player is one edit.
        static void Paint(GameObject go, string name, Color color)
        {
            Directory.CreateDirectory("Assets/Art");
            var path = "Assets/Art/" + name + ".mat";
            var mat = AssetDatabase.LoadAssetAtPath<Material>(path);
            if (mat == null)
            {
                var shader = Shader.Find("Standard") ?? Shader.Find("Universal Render Pipeline/Lit");
                mat = new Material(shader) { color = color };
                AssetDatabase.CreateAsset(mat, path);
            }
            go.GetComponent<Renderer>().sharedMaterial = mat;
        }

        static void EnsureInBuild()
        {
            var scenes = new System.Collections.Generic.List<EditorBuildSettingsScene>(EditorBuildSettings.scenes);
            scenes.RemoveAll(s => s.path == ScenePath);
            scenes.Insert(0, new EditorBuildSettingsScene(ScenePath, true));
            EditorBuildSettings.scenes = scenes.ToArray();
        }
    }
}
