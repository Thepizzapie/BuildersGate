// Builders Gate scaffold, 2D: saves the starter scene from code.
//
// Run once by `bgate init --engine unity --kind 2d` as
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
        const string SpritePath = "Assets/Art/square.png";

        public static void Build()
        {
            EditorSettings.defaultBehaviorMode = EditorBehaviorMode.Mode2D;
            if (File.Exists(ScenePath))
            {
                Debug.Log("BGateScaffold: " + ScenePath + " exists, left alone");
                EnsureInBuild();
                return;
            }

            var sprite = WhiteSquare();
            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

            var camGo = new GameObject("Main Camera");
            camGo.tag = "MainCamera";
            camGo.transform.position = new Vector3(0f, 0f, -10f);
            var cam = camGo.AddComponent<Camera>();
            cam.orthographic = true;
            cam.orthographicSize = 5f;
            cam.clearFlags = CameraClearFlags.SolidColor;
            cam.backgroundColor = new Color(0.11f, 0.12f, 0.15f);

            Block("Ground", sprite, new Vector2(0f, -4f), new Vector2(18f, 1f), new Color(0.22f, 0.25f, 0.30f));
            Block("Ledge", sprite, new Vector2(4f, -1.5f), new Vector2(3f, 0.5f), new Color(0.27f, 0.31f, 0.37f));

            var player = new GameObject("Player");
            player.tag = "Player";   // what BGateAgent's generic view looks for
            player.transform.position = new Vector3(-4f, -2.5f, 0f);
            player.transform.localScale = new Vector3(0.8f, 1.2f, 1f);
            var sr = player.AddComponent<SpriteRenderer>();
            sr.sprite = sprite;
            sr.color = new Color(0.85f, 0.35f, 0.20f);
            player.AddComponent<BoxCollider2D>();
            var body = player.AddComponent<Rigidbody2D>();
            body.gravityScale = 3f;
            body.freezeRotation = true;
            body.interpolation = RigidbodyInterpolation2D.Interpolate;
            body.collisionDetectionMode = CollisionDetectionMode2D.Continuous;
            // By name, so deleting the template script does not break the scaffold.
            var controller = System.Type.GetType("PlayerController2D, Assembly-CSharp");
            if (controller != null) player.AddComponent(controller);
            else Debug.LogWarning("BGateScaffold: PlayerController2D not found; Player has no controller");

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

        static void Block(string name, Sprite sprite, Vector2 pos, Vector2 size, Color color)
        {
            var go = new GameObject(name);
            go.transform.position = pos;
            go.transform.localScale = new Vector3(size.x, size.y, 1f);
            var sr = go.AddComponent<SpriteRenderer>();
            sr.sprite = sprite;
            sr.color = color;
            go.AddComponent<BoxCollider2D>();
        }

        // A 4x4 white PNG imported as a 1-unit sprite: tinted per object, so
        // the whole starter scene needs one texture and no art pipeline.
        static Sprite WhiteSquare()
        {
            if (!File.Exists(SpritePath))
            {
                Directory.CreateDirectory("Assets/Art");
                var tex = new Texture2D(4, 4);
                var px = new Color[16];
                for (int i = 0; i < px.Length; i++) px[i] = Color.white;
                tex.SetPixels(px);
                tex.Apply();
                File.WriteAllBytes(SpritePath, tex.EncodeToPNG());
                Object.DestroyImmediate(tex);
                AssetDatabase.ImportAsset(SpritePath);
                var importer = (TextureImporter)AssetImporter.GetAtPath(SpritePath);
                importer.textureType = TextureImporterType.Sprite;
                importer.spritePixelsPerUnit = 4f;
                importer.filterMode = FilterMode.Point;
                importer.SaveAndReimport();
            }
            return AssetDatabase.LoadAssetAtPath<Sprite>(SpritePath);
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
