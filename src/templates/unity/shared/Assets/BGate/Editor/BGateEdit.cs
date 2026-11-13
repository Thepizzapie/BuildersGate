// Builders Gate edits: import an asset, place a prefab, set a component field.
//
// Invoked by the adapter as
//     Unity -batchmode -quit -projectPath X -executeMethod BGate.Editor.BGateEdit.<Verb>
// with the request in environment variables (-executeMethod passes no
// arguments, and argv parsing inside the editor is where paths with spaces
// die), and the answer written as JSON to BGATE_EDIT_RESULT so the caller
// reads a structured result instead of grepping the log.
//
// EVERY CHANGE GOES THROUGH THE EDITOR'S OWN API. Scenes are saved by
// EditorSceneManager, prefabs by PrefabUtility, imports by AssetDatabase, so
// the .unity YAML and the .meta GUIDs are always the editor's, never a
// hand-written guess that breaks on the next import.
//
//   Import    BGATE_IMPORT_PATH   asset already copied under Assets/
//             BGATE_IMPORT_SPRITE "1": texture becomes a Sprite (2D)
//             BGATE_IMPORT_PPU    sprite pixels per unit (default 100)
//             BGATE_IMPORT_POINT  "1": point filtering, no compression, no mips,
//                                 full resolution (max size 16384) for pixel art
//   ImportMany BGATE_IMPORT_LIST  a text file, one Assets/ path per line, all
//                                 already copied; the SPRITE/PPU/POINT flags
//                                 apply to every texture in it. One editor run.
//   Place    BGATE_SCENE         scene under Assets/ (empty: first build scene)
//             BGATE_PREFAB        prefab or model asset under Assets/
//             BGATE_NAME, BGATE_PARENT (hierarchy path), BGATE_POS,
//             BGATE_ROT (euler degrees), BGATE_SCALE: "x,y,z"
//   SetField  BGATE_SCENE or BGATE_PREFAB: the asset that holds the object
//             BGATE_OBJECT        hierarchy path ("Player", "Root/Arm"); empty
//                                 means the prefab root
//             BGATE_COMPONENT     type name (PlayerController, Transform)
//             BGATE_FIELD         serialized property path (speed, m_LocalPosition)
//             BGATE_VALUE         the value as text, see Assign
//             BGATE_ADD           "1": add the component when it is missing
//
// Lives under Editor/ so it compiles into the editor assembly only.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using Object = UnityEngine.Object;

namespace BGate.Editor
{
    public static class BGateEdit
    {
        static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        // ------------------------------------------------------------------
        // Import
        // ------------------------------------------------------------------
        public static void Import()
        {
            var r = new Result("import");
            var path = Env("BGATE_IMPORT_PATH");
            if (!UnderAssets(path)) { Fail(r, "BGATE_IMPORT_PATH must be a path under Assets/"); return; }
            if (!File.Exists(path)) { Fail(r, path + " does not exist"); return; }

            AssetDatabase.ImportAsset(path, ImportAssetOptions.ForceUpdate | ImportAssetOptions.ForceSynchronousImport);
            var importer = AssetImporter.GetAtPath(path);
            if (importer == null) { Fail(r, "Unity has no importer for " + path + "; is the file type supported?"); return; }

            var tex = importer as TextureImporter;
            if (tex != null)
            {
                var changed = false;
                if (Env("BGATE_IMPORT_SPRITE") == "1")
                {
                    tex.textureType = TextureImporterType.Sprite;
                    tex.spriteImportMode = SpriteImportMode.Single;
                    var ppu = ParseFloat(Env("BGATE_IMPORT_PPU"), 100f);
                    if (ppu > 0f) tex.spritePixelsPerUnit = ppu;
                    changed = true;
                }
                if (Env("BGATE_IMPORT_POINT") == "1")
                {
                    tex.filterMode = FilterMode.Point;
                    tex.textureCompression = TextureImporterCompression.Uncompressed;
                    tex.mipmapEnabled = false;
                    // Pixel art keeps every source pixel: the 2048 default halves a 2560px sheet
                    // and every atlas region read in source pixels then lands on the wrong cell.
                    tex.maxTextureSize = 16384;
                    changed = true;
                }
                if (changed) tex.SaveAndReimport();
                r.Add("texture_type", tex.textureType.ToString());
            }

            var main = AssetDatabase.LoadMainAssetAtPath(path);
            if (main == null) { Fail(r, "imported " + path + " but it loaded as nothing; read the log for the importer's error"); return; }
            r.Add("path", path);
            r.Add("guid", AssetDatabase.AssetPathToGUID(path));
            r.Add("importer", importer.GetType().Name);
            r.Add("main_type", main.GetType().Name);
            var subs = new List<string>();
            foreach (var a in AssetDatabase.LoadAllAssetsAtPath(path))
                if (a != null && a != main) subs.Add(a.GetType().Name + ":" + a.name);
            r.AddList("sub_assets", subs);
            r.Add("placeable", main is GameObject ? "true" : "false", raw: true);
            Done(r);
        }

        // ------------------------------------------------------------------
        // ImportMany: one editor run for a whole folder of art or sound.
        // ------------------------------------------------------------------
        public static void ImportMany()
        {
            var r = new Result("import_many");
            var listPath = Env("BGATE_IMPORT_LIST");
            if (string.IsNullOrEmpty(listPath) || !File.Exists(listPath))
            { Fail(r, "BGATE_IMPORT_LIST must name a file listing one Assets/ path per line"); return; }
            var paths = new List<string>();
            foreach (var raw in File.ReadAllLines(listPath, Encoding.UTF8))
            {
                var p = raw.Trim().Replace('\\', '/');
                if (p.Length == 0) continue;
                if (!UnderAssets(p)) { Fail(r, p + " is not a path under Assets/"); return; }
                paths.Add(p);
            }
            if (paths.Count == 0) { Fail(r, "BGATE_IMPORT_LIST names no files"); return; }

            var sprite = Env("BGATE_IMPORT_SPRITE") == "1";
            var point = Env("BGATE_IMPORT_POINT") == "1";
            var ppu = ParseFloat(Env("BGATE_IMPORT_PPU"), 100f);

            // The files are already on disk: one Refresh imports them all, then
            // the texture settings are queued and reimported in one batch.
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
            AssetDatabase.StartAssetEditing();
            try
            {
                foreach (var p in paths)
                {
                    var tex = AssetImporter.GetAtPath(p) as TextureImporter;
                    if (tex == null || (!sprite && !point)) continue;
                    if (sprite)
                    {
                        tex.textureType = TextureImporterType.Sprite;
                        tex.spriteImportMode = SpriteImportMode.Single;
                        if (ppu > 0f) tex.spritePixelsPerUnit = ppu;
                    }
                    if (point)
                    {
                        tex.filterMode = FilterMode.Point;
                        tex.textureCompression = TextureImporterCompression.Uncompressed;
                        tex.mipmapEnabled = false;
                        // Pixel art keeps every source pixel: the 2048 default halves a 2560px sheet
                        // and every atlas region read in source pixels then lands on the wrong cell.
                        tex.maxTextureSize = 16384;
                    }
                    tex.SaveAndReimport();
                }
            }
            finally { AssetDatabase.StopAssetEditing(); }

            var items = new List<string>();
            var failed = new List<string>();
            foreach (var p in paths)
            {
                var importer = File.Exists(p) ? AssetImporter.GetAtPath(p) : null;
                var main = importer != null ? AssetDatabase.LoadMainAssetAtPath(p) : null;
                if (main == null)
                {
                    failed.Add(p + (importer == null ? ": Unity has no importer for it" : ": it loaded as nothing; read the log"));
                    continue;
                }
                var tex = importer as TextureImporter;
                items.Add("{\"path\":" + Result.Q(p)
                          + ",\"guid\":" + Result.Q(AssetDatabase.AssetPathToGUID(p))
                          + ",\"importer\":" + Result.Q(importer.GetType().Name)
                          + ",\"main_type\":" + Result.Q(main.GetType().Name)
                          + (tex != null ? ",\"texture_type\":" + Result.Q(tex.textureType.ToString()) : "")
                          + "}");
            }
            r.Add("imported", "[" + string.Join(",", items.ToArray()) + "]", raw: true);
            r.AddList("failed", failed);
            r.Add("count", items.Count.ToString(Inv), raw: true);
            if (failed.Count > 0) { Fail(r, failed.Count + " of " + paths.Count + " files did not import: " + failed[0]); return; }
            Done(r);
        }

        // ------------------------------------------------------------------
        // Place
        // ------------------------------------------------------------------
        public static void Place()
        {
            var r = new Result("place");
            var prefabPath = Env("BGATE_PREFAB");
            if (!UnderAssets(prefabPath)) { Fail(r, "BGATE_PREFAB must be a path under Assets/"); return; }
            var asset = AssetDatabase.LoadAssetAtPath<GameObject>(prefabPath);
            if (asset == null) { Fail(r, prefabPath + " is not a prefab or model (no GameObject at that path)"); return; }

            Scene scene;
            if (!OpenScene(r, out scene)) return;

            var go = PrefabUtility.InstantiatePrefab(asset, scene) as GameObject;
            if (go == null) { Fail(r, "PrefabUtility.InstantiatePrefab returned nothing for " + prefabPath); return; }
            var name = Env("BGATE_NAME");
            if (!string.IsNullOrEmpty(name)) go.name = name;

            var parentPath = Env("BGATE_PARENT");
            if (!string.IsNullOrEmpty(parentPath))
            {
                var parent = FindInScene(scene, parentPath);
                if (parent == null) { Object.DestroyImmediate(go); Fail(r, "no object at '" + parentPath + "' in " + scene.path); return; }
                go.transform.SetParent(parent.transform, false);
            }
            Vector3 v;
            if (TryVec3(Env("BGATE_POS"), out v)) go.transform.localPosition = v;
            if (TryVec3(Env("BGATE_ROT"), out v)) go.transform.localRotation = Quaternion.Euler(v);
            if (TryVec3(Env("BGATE_SCALE"), out v)) go.transform.localScale = v;

            EditorSceneManager.MarkSceneDirty(scene);
            if (!EditorSceneManager.SaveScene(scene)) { Fail(r, "could not save " + scene.path); return; }
            r.Add("scene", scene.path);
            r.Add("prefab", prefabPath);
            r.Add("object", PathOf(go.transform));
            r.Add("position", Vec(go.transform.position));
            Done(r);
        }

        // ------------------------------------------------------------------
        // SetField
        // ------------------------------------------------------------------
        public static void SetField()
        {
            var r = new Result("set_field");
            var prefabPath = Env("BGATE_PREFAB");
            var objectPath = Env("BGATE_OBJECT");
            var typeName = Env("BGATE_COMPONENT");
            var field = Env("BGATE_FIELD");
            var value = Env("BGATE_VALUE") ?? "";
            if (string.IsNullOrEmpty(typeName) || string.IsNullOrEmpty(field)) { Fail(r, "BGATE_COMPONENT and BGATE_FIELD are required"); return; }

            GameObject root = null;
            Scene scene = default(Scene);
            var inPrefab = !string.IsNullOrEmpty(prefabPath);
            if (inPrefab)
            {
                if (!UnderAssets(prefabPath) || !prefabPath.EndsWith(".prefab", StringComparison.OrdinalIgnoreCase)) { Fail(r, "BGATE_PREFAB must be a .prefab under Assets/"); return; }
                try { root = PrefabUtility.LoadPrefabContents(prefabPath); }
                catch (Exception exc) { Fail(r, "cannot open " + prefabPath + ": " + exc.Message); return; }
            }
            else if (!OpenScene(r, out scene)) return;

            try
            {
                GameObject target;
                if (inPrefab) target = string.IsNullOrEmpty(objectPath) ? root : FindUnder(root.transform, objectPath);
                else target = string.IsNullOrEmpty(objectPath) ? null : FindInScene(scene, objectPath);
                if (target == null) { Fail(r, "no object at '" + objectPath + "' in " + (inPrefab ? prefabPath : scene.path)); return; }

                var comp = FindComponent(target, typeName);
                if (comp == null && Env("BGATE_ADD") == "1")
                {
                    var type = FindType(typeName);
                    if (type == null || !typeof(Component).IsAssignableFrom(type)) { Fail(r, "no Component type named " + typeName + " compiles in this project"); return; }
                    comp = target.AddComponent(type);
                    r.Add("added_component", "true", raw: true);
                }
                if (comp == null)
                {
                    var have = new List<string>();
                    foreach (var c in target.GetComponents<Component>()) if (c != null) have.Add(c.GetType().Name);
                    r.AddList("components", have);
                    Fail(r, target.name + " has no " + typeName + " (pass add=true to add it)");
                    return;
                }

                var so = new SerializedObject(comp);
                var prop = so.FindProperty(field);
                if (prop == null)
                {
                    var names = new List<string>();
                    var it = so.GetIterator();
                    if (it.NextVisible(true)) do { names.Add(it.propertyPath); } while (it.NextVisible(false));
                    r.AddList("fields", names);
                    Fail(r, typeName + " has no serialized field '" + field + "'");
                    return;
                }
                var before = Describe(prop);
                string why;
                if (!Assign(prop, value, out why)) { Fail(r, "cannot set " + field + " (" + prop.propertyType + "): " + why); return; }
                so.ApplyModifiedPropertiesWithoutUndo();
                so.Update();
                var after = Describe(so.FindProperty(field));

                if (inPrefab)
                {
                    bool saved;
                    PrefabUtility.SaveAsPrefabAsset(root, prefabPath, out saved);
                    if (!saved) { Fail(r, "could not save " + prefabPath); return; }
                }
                else
                {
                    EditorSceneManager.MarkSceneDirty(scene);
                    if (!EditorSceneManager.SaveScene(scene)) { Fail(r, "could not save " + scene.path); return; }
                }
                r.Add("asset", inPrefab ? prefabPath : scene.path);
                r.Add("object", inPrefab ? objectPath : PathOf(target.transform));
                r.Add("component", comp.GetType().Name);
                r.Add("field", field);
                r.Add("type", prop.propertyType.ToString());
                r.Add("before", before);
                r.Add("after", after);
                Done(r);
            }
            finally
            {
                if (root != null) PrefabUtility.UnloadPrefabContents(root);
            }
        }

        // ------------------------------------------------------------------
        // Values
        // ------------------------------------------------------------------
        // Text in, typed value out. Numbers and bools as written; vectors and
        // colours as "x,y,z" (brackets allowed); colours also "#RRGGBB[AA]";
        // enums by name or index; object references as an asset path under
        // Assets/, "Assets/hero.png#Sprite" for a sub-asset by type, or "null".
        static bool Assign(SerializedProperty p, string raw, out string why)
        {
            why = "";
            var s = raw.Trim();
            float[] n;
            switch (p.propertyType)
            {
                case SerializedPropertyType.Integer:
                case SerializedPropertyType.LayerMask:
                    long l;
                    if (!long.TryParse(s, NumberStyles.Integer, Inv, out l)) { why = "not an integer: " + s; return false; }
                    p.longValue = l; return true;
                case SerializedPropertyType.Float:
                    double d;
                    if (!double.TryParse(s, NumberStyles.Float, Inv, out d)) { why = "not a number: " + s; return false; }
                    p.doubleValue = d; return true;
                case SerializedPropertyType.Boolean:
                    if (s == "1" || s.Equals("true", StringComparison.OrdinalIgnoreCase)) { p.boolValue = true; return true; }
                    if (s == "0" || s.Equals("false", StringComparison.OrdinalIgnoreCase)) { p.boolValue = false; return true; }
                    why = "not a bool: " + s; return false;
                case SerializedPropertyType.String:
                    p.stringValue = raw; return true;
                case SerializedPropertyType.Enum:
                    // enumNames are the inspector's labels ("Hard Shadows"), not the
                    // C# names (LightShadows.Hard): match with spaces dropped, then a
                    // unique prefix, so either spelling works.
                    var names = p.enumNames;
                    var want = Squash(s);
                    var prefix = -1;
                    for (var i = 0; i < names.Length; i++)
                    {
                        var have = Squash(names[i]);
                        if (have == want) { p.enumValueIndex = i; return true; }
                        if (want.Length > 0 && have.StartsWith(want)) prefix = prefix == -1 ? i : -2;
                    }
                    if (prefix >= 0) { p.enumValueIndex = prefix; return true; }
                    int idx;
                    if (int.TryParse(s, out idx) && idx >= 0 && idx < names.Length) { p.enumValueIndex = idx; return true; }
                    why = "not one of " + string.Join(", ", names); return false;
                case SerializedPropertyType.Vector2:
                    if (!Floats(s, 2, out n)) { why = "want x,y"; return false; }
                    p.vector2Value = new Vector2(n[0], n[1]); return true;
                case SerializedPropertyType.Vector3:
                    if (!Floats(s, 3, out n)) { why = "want x,y,z"; return false; }
                    p.vector3Value = new Vector3(n[0], n[1], n[2]); return true;
                case SerializedPropertyType.Vector4:
                    if (!Floats(s, 4, out n)) { why = "want x,y,z,w"; return false; }
                    p.vector4Value = new Vector4(n[0], n[1], n[2], n[3]); return true;
                case SerializedPropertyType.Quaternion:
                    if (Floats(s, 3, out n)) { p.quaternionValue = Quaternion.Euler(n[0], n[1], n[2]); return true; }
                    why = "want euler degrees x,y,z"; return false;
                case SerializedPropertyType.Color:
                    Color c;
                    if (s.StartsWith("#") && ColorUtility.TryParseHtmlString(s, out c)) { p.colorValue = c; return true; }
                    if (Floats(s, 4, out n)) { p.colorValue = new Color(n[0], n[1], n[2], n[3]); return true; }
                    if (Floats(s, 3, out n)) { p.colorValue = new Color(n[0], n[1], n[2], 1f); return true; }
                    why = "want #RRGGBB or r,g,b[,a] in 0..1"; return false;
                case SerializedPropertyType.ObjectReference:
                    if (s == "" || s.Equals("null", StringComparison.OrdinalIgnoreCase)) { p.objectReferenceValue = null; return true; }
                    var obj = LoadRef(s, Wanted(p));
                    if (obj == null) { why = "nothing loadable at " + s; return false; }
                    p.objectReferenceValue = obj;
                    if (p.objectReferenceValue != obj) { why = obj.GetType().Name + " does not fit this field"; return false; }
                    return true;
                default:
                    why = "this property type is not settable from text"; return false;
            }
        }

        static string Wanted(SerializedProperty p)
        {
            // p.type is "PPtr<$Sprite>" for an object reference field.
            var t = p.type ?? "";
            var at = t.IndexOf('$');
            return at >= 0 ? t.Substring(at + 1).TrimEnd('>') : "";
        }

        static Object LoadRef(string spec, string wanted)
        {
            var hash = spec.IndexOf('#');
            var path = hash >= 0 ? spec.Substring(0, hash) : spec;
            var sub = hash >= 0 ? spec.Substring(hash + 1) : wanted;
            if (!UnderAssets(path)) return null;
            var all = AssetDatabase.LoadAllAssetsAtPath(path);
            if (!string.IsNullOrEmpty(sub))
                foreach (var a in all)
                    if (a != null && (a.GetType().Name == sub || a.name == sub)) return a;
            return AssetDatabase.LoadMainAssetAtPath(path);
        }

        static string Describe(SerializedProperty p)
        {
            if (p == null) return "";
            switch (p.propertyType)
            {
                case SerializedPropertyType.Integer:
                case SerializedPropertyType.LayerMask: return p.longValue.ToString(Inv);
                case SerializedPropertyType.Float: return p.doubleValue.ToString("R", Inv);
                case SerializedPropertyType.Boolean: return p.boolValue ? "true" : "false";
                case SerializedPropertyType.String: return p.stringValue;
                case SerializedPropertyType.Enum: return p.enumValueIndex >= 0 && p.enumValueIndex < p.enumNames.Length ? p.enumNames[p.enumValueIndex] : p.enumValueIndex.ToString(Inv);
                case SerializedPropertyType.Vector2: return Vec(p.vector2Value);
                case SerializedPropertyType.Vector3: return Vec(p.vector3Value);
                case SerializedPropertyType.Vector4: var v4 = p.vector4Value; return F(v4.x) + "," + F(v4.y) + "," + F(v4.z) + "," + F(v4.w);
                case SerializedPropertyType.Quaternion: return Vec(p.quaternionValue.eulerAngles);
                case SerializedPropertyType.Color: return "#" + ColorUtility.ToHtmlStringRGBA(p.colorValue);
                case SerializedPropertyType.ObjectReference:
                    var o = p.objectReferenceValue;
                    if (o == null) return "null";
                    var path = AssetDatabase.GetAssetPath(o);
                    return string.IsNullOrEmpty(path) ? o.GetType().Name + ":" + o.name : path + "#" + o.GetType().Name;
                default: return p.propertyType.ToString();
            }
        }

        // ------------------------------------------------------------------
        // Lookups
        // ------------------------------------------------------------------
        static bool OpenScene(Result r, out Scene scene)
        {
            scene = default(Scene);
            var path = Env("BGATE_SCENE");
            if (string.IsNullOrEmpty(path))
                foreach (var e in EditorBuildSettings.scenes)
                    if (e.enabled) { path = e.path; break; }
            if (string.IsNullOrEmpty(path)) { Fail(r, "no scene given and none enabled in Build Settings"); return false; }
            if (!UnderAssets(path) || !path.EndsWith(".unity", StringComparison.OrdinalIgnoreCase)) { Fail(r, "scene must be a .unity under Assets/: " + path); return false; }
            if (!File.Exists(path)) { Fail(r, path + " does not exist"); return false; }
            try { scene = EditorSceneManager.OpenScene(path, OpenSceneMode.Single); }
            catch (Exception exc) { Fail(r, "cannot open " + path + ": " + exc.Message); return false; }
            return true;
        }

        static GameObject FindInScene(Scene scene, string path)
        {
            var parts = path.Trim('/').Split(new[] { '/' }, 2);
            foreach (var root in scene.GetRootGameObjects())
            {
                if (root.name != parts[0]) continue;
                if (parts.Length == 1) return root;
                var hit = root.transform.Find(parts[1]);
                if (hit != null) return hit.gameObject;
            }
            return null;
        }

        static GameObject FindUnder(Transform root, string path)
        {
            var clean = path.Trim('/');
            if (clean == root.name) return root.gameObject;
            if (clean.StartsWith(root.name + "/")) clean = clean.Substring(root.name.Length + 1);
            var hit = root.Find(clean);
            return hit != null ? hit.gameObject : null;
        }

        static Component FindComponent(GameObject go, string typeName)
        {
            foreach (var c in go.GetComponents<Component>())
                if (c != null && (c.GetType().Name == typeName || c.GetType().FullName == typeName)) return c;
            return null;
        }

        static Type FindType(string name)
        {
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = asm.GetTypes(); } catch { continue; }
                foreach (var t in types)
                    if (t.Name == name || t.FullName == name) return t;
            }
            return null;
        }

        static string PathOf(Transform t)
        {
            var parts = new List<string>();
            for (var cur = t; cur != null; cur = cur.parent) parts.Insert(0, cur.name);
            return string.Join("/", parts.ToArray());
        }

        // ------------------------------------------------------------------
        // Plumbing
        // ------------------------------------------------------------------
        static string Env(string name) => Environment.GetEnvironmentVariable(name);

        static bool UnderAssets(string path) =>
            !string.IsNullOrEmpty(path) && path.Replace('\\', '/').StartsWith("Assets/") && !path.Contains("..");

        static float ParseFloat(string s, float fallback)
        {
            float f;
            return float.TryParse(s, NumberStyles.Float, Inv, out f) ? f : fallback;
        }

        static bool Floats(string s, int count, out float[] values)
        {
            values = null;
            var parts = s.Trim().Trim('[', ']', '(', ')').Split(',');
            if (parts.Length != count) return false;
            var got = new float[count];
            for (var i = 0; i < count; i++)
                if (!float.TryParse(parts[i].Trim(), NumberStyles.Float, Inv, out got[i])) return false;
            values = got;
            return true;
        }

        static bool TryVec3(string s, out Vector3 v)
        {
            v = Vector3.zero;
            float[] n;
            if (string.IsNullOrEmpty(s) || !Floats(s, 3, out n)) return false;
            v = new Vector3(n[0], n[1], n[2]);
            return true;
        }

        static string Squash(string s) => (s ?? "").Replace(" ", "").Replace("_", "").ToLowerInvariant();

        static string F(float f) => f.ToString("R", Inv);
        static string Vec(Vector2 v) => F(v.x) + "," + F(v.y);
        static string Vec(Vector3 v) => F(v.x) + "," + F(v.y) + "," + F(v.z);

        static void Fail(Result r, string error)
        {
            r.Add("error", error);
            r.ok = false;
            r.Write();
            Debug.LogError("BGateEdit: " + error);
            EditorApplication.Exit(2);
        }

        static void Done(Result r)
        {
            r.ok = true;
            AssetDatabase.SaveAssets();
            r.Write();
            Debug.Log("BGateEdit: " + r.verb + " ok");
        }

        // A flat JSON object, written by hand: JsonUtility cannot serialize a
        // dictionary, and this is the only shape the adapter reads.
        sealed class Result
        {
            public readonly string verb;
            public bool ok;
            readonly StringBuilder body = new StringBuilder();

            public Result(string verb) { this.verb = verb; }

            public void Add(string key, string value, bool raw = false)
            {
                body.Append(",").Append(Q(key)).Append(":").Append(raw ? value : Q(value ?? ""));
            }

            public void AddList(string key, List<string> values)
            {
                var items = new List<string>();
                foreach (var v in values) items.Add(Q(v));
                body.Append(",").Append(Q(key)).Append(":[").Append(string.Join(",", items.ToArray())).Append("]");
            }

            public void Write()
            {
                var path = Env("BGATE_EDIT_RESULT");
                if (string.IsNullOrEmpty(path)) return;
                var text = "{\"ok\":" + (ok ? "true" : "false") + ",\"verb\":" + Q(verb) + body + "}";
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                File.WriteAllText(path, text, new UTF8Encoding(false));
            }

            public static string Q(string s)
            {
                var sb = new StringBuilder("\"");
                foreach (var ch in s)
                {
                    switch (ch)
                    {
                        case '"': sb.Append("\\\""); break;
                        case '\\': sb.Append("\\\\"); break;
                        case '\n': sb.Append("\\n"); break;
                        case '\r': sb.Append("\\r"); break;
                        case '\t': sb.Append("\\t"); break;
                        default:
                            if (ch < ' ') sb.Append("\\u").Append(((int)ch).ToString("x4"));
                            else sb.Append(ch);
                            break;
                    }
                }
                return sb.Append('"').ToString();
            }
        }
    }
}
