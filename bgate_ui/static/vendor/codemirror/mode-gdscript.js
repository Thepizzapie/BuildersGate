/* mode-gdscript.js — GDScript 2.0 (Godot 4) for CodeMirror 5.
 *
 * NOT vendored. CodeMirror 5 ships no GDScript mode and its Python mode is a
 * near-miss that gets exactly the tokens you look at wrong: `func`, `var`,
 * `signal` and `extends` fall through as plain identifiers, `@export` reads as
 * an operator followed by a name, and `$Player/Sprite2D` — the most GDScript
 * thing there is — lexes as three separate things. A near-miss highlighter is
 * worse than none, because it looks authoritative while mis-colouring the
 * declarations you scan for.
 *
 * Built on the simple-mode addon: GDScript's grammar needs no parser state
 * beyond "am I inside a triple-quoted string", and a state machine that small
 * is worth far less than the ~40 lines it would take to hand-roll.
 */
(function (mod) {
  if (typeof exports === "object" && typeof module === "object") mod(require("../../codemirror"));
  else if (typeof define === "function" && define.amd) define(["../../codemirror"], mod);
  else mod(CodeMirror);
})(function (CodeMirror) {
  "use strict";

  var KEYWORD = [
    "if", "elif", "else", "for", "while", "match", "when", "break", "continue",
    "pass", "return", "class", "class_name", "extends", "is", "in", "as",
    "self", "super", "signal", "func", "static", "const", "enum", "var",
    "breakpoint", "preload", "await", "assert", "void", "and", "or", "not",
    "namespace", "trait", "yield",
  ];
  var TYPE = [
    "bool", "int", "float", "String", "StringName", "NodePath", "Vector2",
    "Vector2i", "Vector3", "Vector3i", "Vector4", "Vector4i", "Rect2",
    "Rect2i", "Transform2D", "Transform3D", "Plane", "Quaternion", "AABB",
    "Basis", "Projection", "Color", "RID", "Object", "Callable", "Signal",
    "Dictionary", "Array", "PackedByteArray", "PackedInt32Array",
    "PackedInt64Array", "PackedFloat32Array", "PackedFloat64Array",
    "PackedStringArray", "PackedVector2Array", "PackedVector3Array",
    "PackedColorArray",
  ];
  var ATOM = ["true", "false", "null", "PI", "TAU", "INF", "NAN"];

  var words = function (list) {
    return new RegExp("(?:" + list.join("|") + ")\\b");
  };

  CodeMirror.defineSimpleMode("gdscript", {
    start: [
      // Triple-quoted first: a """ that fell through to the single-quote rule
      // opens and closes a string on the same line and desyncs the rest of it.
      { regex: /"""/, token: "string", next: "long2" },
      { regex: /'''/, token: "string", next: "long1" },
      { regex: /#.*/, token: "comment" },

      // Annotations carry the export/tool/rpc semantics. They are the reason a
      // property shows up in the inspector, so they get their own colour.
      { regex: /@[A-Za-z_]\w*/, token: "meta" },

      // $Node/Path, $"quoted name", %UniqueName — scene addressing, not maths.
      { regex: /\$(?:"(?:[^\\"]|\\.)*"|[A-Za-z_][\w/]*)/, token: "variable-3" },
      { regex: /%[A-Za-z_]\w*/, token: "variable-3" },

      // Declarations name the thing being declared; colouring the NAME is what
      // makes a file skimmable for "where is X defined".
      { regex: /(func|class_name|class|signal|enum)(\s+)([A-Za-z_]\w*)/,
        token: ["keyword", null, "def"] },
      { regex: /(extends)(\s+)([A-Za-z_]\w*)/, token: ["keyword", null, "variable-2"] },
      { regex: /(var|const)(\s+)([A-Za-z_]\w*)/, token: ["keyword", null, "variable-2"] },

      { regex: /(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|(?:\d[\d_]*)?\.?\d[\d_]*(?:[eE][+-]?\d+)?)/,
        token: "number" },
      { regex: /"(?:[^\\"]|\\.)*"?/, token: "string" },
      { regex: /'(?:[^\\']|\\.)*'?/, token: "string" },

      { regex: words(ATOM), token: "atom" },
      { regex: words(KEYWORD), token: "keyword" },
      { regex: words(TYPE), token: "variable-2" },

      // A name followed by `(` is a call; everything else stays plain so the
      // colour budget is spent on structure rather than on every identifier.
      { regex: /[A-Za-z_]\w*(?=\s*\()/, token: "builtin" },
      { regex: /[+\-*/%&|^~<>!=]+|:=|->/, token: "operator" },
      { regex: /[{[(]/, indent: true },
      { regex: /[}\])]/, dedent: true },
      { regex: /[A-Za-z_]\w*/, token: "variable" },
    ],
    long1: [
      { regex: /.*?'''/, token: "string", next: "start" },
      { regex: /.*/, token: "string" },
    ],
    long2: [
      { regex: /.*?"""/, token: "string", next: "start" },
      { regex: /.*/, token: "string" },
    ],
    meta: {
      dontIndentStates: ["long1", "long2"],
      lineComment: "#",
      // GDScript is indentation-significant and Godot's own convention is a
      // hard tab. Emitting spaces here produces files the engine opens with
      // mixed indentation, which it then reformats on its next save.
      electricInput: /^\s*(?:else|elif|func|class)\b.*:$/,
    },
  });

  CodeMirror.defineMIME("text/x-gdscript", "gdscript");
});
