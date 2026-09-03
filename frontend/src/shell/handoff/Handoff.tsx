import { useCallback, useEffect, useRef, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Button, Checkbox, NativeSelect, Radio, TextInput, Textarea } from "@mantine/core";
import { Themed } from "../../theme";
import { Ti } from "../Ti";
import { toast } from "../../bridge";
import { askConfirm } from "../../ask";
import { SEAT_COLOR } from "../nav";
import "./handoff.css";

/* Handoff — "I made a thing. Now put it in the game."
 *
 * Every editor used to dead-end at "saved". The verbs that finish the job all
 * existed — /api/scene/wire, /api/sprite/spriteframes, the Godot deliver path,
 * the queue — and none of them was reachable from the place where the asset
 * had just been made. This is the crossing, with two exits:
 *
 *   A. WIRE IT HERE — local, free, mechanical. Pick a real scene off disk, a
 *      real parent node, see the exact text that will be added, then write
 *      it. Prove it landed with godot_check_project and godot_screenshot.
 *
 *   B. HAND IT TO AN AGENT — a work item whose brief is ALREADY FILLED IN with
 *      the asset path, the chosen scene, the trigger, the Atlas references and
 *      what done looks like, so nobody retypes what is on screen.
 *
 * ONE PANEL, THREE EDITORS. The sprite editor, the audio lab and the 3D model
 * editor each call window.Handoff.fromEditor(state, {editor, meta}) and get the
 * identical panel, worded for their kind. Those editors are still classic
 * decks, so the entry point stays on window; the panel itself is React and
 * mounts into its own root at body level when opened.
 *
 * NOTHING HERE GENERATES ANYTHING. Godot and Blender are the only engines it
 * touches and both are local and free.
 *
 * DESTRUCTIVE STEPS ANNOUNCE THEMSELVES. Everything that writes into the game
 * project runs as a dry run FIRST and shows the plan; the commit is a separate,
 * differently-worded button behind askConfirm. */

type Target = {
  res?: string | null; name?: string; kind?: string; label?: string; node_type?: string;
  choices?: { type: string; property?: string }[];
  props?: Prop[];
};
type Prop = { key: string; label: string; type?: string; default?: unknown; hint?: string; literal?: string };
type Step = { id: string; label: string; why?: string; done?: boolean; target?: string };
type Ctx = {
  asset: {
    name: string; rel: string; res?: string; kind: string; bytes?: number; suffix?: string;
    stem?: string; exists?: boolean; in_godot?: boolean; wire_res?: string | null;
  };
  wire: {
    ok?: boolean; why?: string; targets?: Target[]; suggested_name?: string;
    node_type?: string; choices?: Target["choices"]; props?: Prop[];
  };
  seat?: string;
  scenes?: { label: string; scene: string; nodes: number; has_asset?: boolean }[];
  steps?: Step[];
  example?: { scene: string; type: string; node?: string; in_script?: boolean } | null;
};
type SceneInfo = {
  root?: string; lock?: { seat: string; owner?: string } | null;
  nodes: { path: string; name: string; type?: string }[];
};
type AtlasMap = { nodes?: Record<string, { kind?: string; label?: string; exists?: boolean; orphan?: boolean }> };
type Plan = { error?: string; steps?: string[]; added?: string; node?: string; reused?: boolean };
type Wrote = { summary?: string; backup?: string; node?: string; propErrors?: string[] };
type Prereq = {
  id: string; error?: string; plan?: string[]; would_write?: string; text?: string; busyNote?: string;
};

export type HandoffOpen = {
  path: string; kind?: string; name?: string; editor?: string; refs?: string[];
  meta?: { dirty?: boolean; animations?: string[]; grid?: { cell_w: number; cell_h: number; cols: number; rows: number } | null;
    [k: string]: unknown };
};

type S = {
  path: string; kindHint: string; name: string; editor: string; meta: NonNullable<HandoffOpen["meta"]>;
  ctx: Ctx | null; ctxError: string;
  target: number; exit: "wire" | "agent"; exitPinned: boolean;
  scene: string; sceneInfo: SceneInfo | null; sceneFilter: string;
  parent: string; nodeName: string; nodeType: string;
  props: Record<string, unknown>; trigger: string; notes: string;
  plan: Plan | null; wrote: Wrote | null;
  check: { ok?: boolean; seconds?: number; errors?: string[]; error?: string } | null;
  shot: { rel?: string; error?: string } | null;
  prereq: Prereq | null;
  refs: string[]; refOpen: boolean; refFilter: string;
  atlas: AtlasMap | null;
  seat: string; title: string; brief: string; briefEdited: boolean;
  filed: { id: number; seat: string } | null;
  dispatched: { ok: boolean; pid?: number; error?: string } | null;
  autopilot: { on?: boolean; unknown?: boolean } | null;
  busy: string;
};

const SEATS = Object.keys(SEAT_COLOR);

function bytes(n: unknown) {
  const v = Number(n) || 0;
  if (v < 1024) return v + " B";
  if (v < 1048576) return (v / 1024).toFixed(0) + " KB";
  return (v / 1048576).toFixed(1) + " MB";
}

/* Every endpoint on this path answers in the api.ok envelope EXCEPT the two
   engine ones (/api/godot/check, /api/godot/screenshot) and the queue, which
   answer a bare dict. One normaliser rather than a shape test at nine call
   sites. Raw fetch rather than the bridge's mutate(): mutate toasts on every
   refusal, and this panel shows refusals inline where they happened. */
type Call<T = Record<string, unknown>> = { ok: boolean; data: T; error: string; detail?: unknown };
async function call<T = Record<string, unknown>>(path: string, body?: unknown): Promise<Call<T>> {
  let r: Response;
  try {
    r = body === undefined ? await fetch(path) : await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  } catch (e) {
    return { ok: false, data: {} as T, error: "the dashboard did not answer - " + (e as Error).message };
  }
  let j: Record<string, unknown> | null = null;
  try { j = await r.json(); } catch { j = null; }
  if (!j) return { ok: false, data: {} as T, error: r.status + " " + r.statusText };
  if (typeof j.ok === "boolean" && ("data" in j || "error" in j)) {
    const err = j.error as { message?: string; detail?: unknown } | string | undefined;
    return {
      ok: j.ok, data: (j.data || {}) as T,
      error: !j.ok ? ((err && typeof err === "object" && err.message) ? err.message : String(err || "refused")) : "",
      detail: (err && typeof err === "object" && err.detail) || null,
    };
  }
  // A bare dict. Treat a missing `ok` as success (the scene reads do this).
  const okish = j.ok === undefined ? r.ok : !!j.ok;
  return { ok: okish, data: j as T, error: okish ? "" : String(j.error || r.status) };
}

const say = (m: string, ok = false) => toast(m, ok ? "ok" : undefined);

/* ── pure reads of the state ─────────────────────────────────────────────── */
/* A sheet with a SpriteFrames beside it has TWO honest answers, so the server
   hands back a `targets` list whenever there is a real choice; one accessor
   keeps every read of "what are we wiring" going through the same place. */
function tgt(s: S): Target {
  const t = (s.ctx && s.ctx.wire.targets) || [];
  if (t.length) return t[Math.min(s.target, t.length - 1)];
  const w = s.ctx ? s.ctx.wire : {};
  return {
    res: s.ctx ? s.ctx.asset.wire_res : null, name: s.ctx ? s.ctx.asset.name : "",
    kind: s.ctx ? s.ctx.asset.kind : "", label: "", node_type: w.node_type || "",
    choices: w.choices || [], props: w.props || [],
  };
}
const wireRes = (s: S) => tgt(s).res;
const propsOf = (s: S) => tgt(s).props || [];
const choicesOf = (s: S) => tgt(s).choices || [];
const defaultProps = (s: S) => Object.fromEntries(propsOf(s).map((p) => [p.key, p.default]));

function kindWord(s: S) {
  const k = s.ctx ? s.ctx.asset.kind : "";
  return ({ sprite: "sprite sheet", resource: "SpriteFrames resource", audio: "sound",
    mesh: "3D model", scene: "scene", script: "script" } as Record<string, string>)[k] || "asset";
}
function defaultTitle(s: S) {
  const where = s.scene ? " into " + s.scene.split("/").pop() : "";
  return "Wire " + (s.ctx ? s.ctx.asset.name : s.name) + where;
}
function doneLines(s: S) {
  const a = s.ctx!.asset;
  const scene = s.scene || "<the scene you choose>";
  const out = [`- ${scene} references ${wireRes(s) || a.res || a.rel} and godot_check_project comes back clean`];
  if (a.kind === "audio") {
    out.push("- the sound is on an AudioStreamPlayer node in that scene, on the right bus, at a level that does not clip");
    out.push(s.trigger ? `- it plays on ${s.trigger} and at no other time`
      : "- it plays when it should and at no other time (say when in the scene's script)");
  } else if (a.kind === "sprite" || a.kind === "resource") {
    out.push("- the node draws the right frames at the right size - godot_screenshot of the scene shows it, not a magenta box or the whole sheet");
    const anims = (s.meta.animations || []).filter(Boolean);
    if (anims.length) out.push("- these animations exist and play: " + anims.join(", "));
  } else if (a.kind === "mesh") {
    out.push("- the model loads in-engine with geometry, a material that carries its texture, a sane real-world size and a collider (the godot_deliver_asset checks)");
    out.push("- it is instanced in the scene at a position that makes sense, not at the origin by default");
  }
  out.push("- nothing else in the scene moved: the diff is the node you added and its ext_resource");
  return out;
}

/* The composed brief: the whole point of exit B. */
function composeBrief(s: S): string {
  if (!s.ctx) return "";
  const a = s.ctx.asset;
  const L: string[] = [];
  L.push("Put an asset that already exists into the game. It is made and on disk; this is the wiring, not the making. Do not regenerate it.");
  L.push("");
  L.push("ASSET");
  L.push("  path: " + a.rel + (a.res ? "   (" + a.res + ")" : ""));
  const wr = wireRes(s);
  if (wr && wr !== a.res) L.push(`  what goes in the scene: ${wr}   (${tgt(s).label || "the engine-side resource for it"})`);
  L.push(`  kind: ${kindWord(s)}  ·  ${bytes(a.bytes)}${a.suffix ? "  ·  " + a.suffix : ""}`);
  if (s.editor) L.push(`  made in: the ${s.editor} editor in the Builders Gate dashboard`);
  const anims = (s.meta.animations || []).filter(Boolean);
  if (anims.length) L.push("  animations labelled on it: " + anims.join(", "));
  const g = s.meta.grid;
  if (g && g.cell_w) L.push(`  frame grid: ${g.cell_w}x${g.cell_h} (${g.cols} x ${g.rows})`);

  L.push("");
  L.push("WHERE IT GOES");
  L.push("  scene: " + (s.scene || "NOT CHOSEN - pick the one this belongs in and say why"));
  if (s.scene) {
    L.push("  parent node: " + s.parent + (s.sceneInfo?.root ? `   (scene root is ${s.sceneInfo.root})` : ""));
    L.push(`  node to add: ${s.nodeType || "the right node for this kind"} named "${s.nodeName || a.stem}"`);
    if (s.sceneInfo?.lock) L.push(`  NOTE: this scene is locked by the ${s.sceneInfo.lock.seat} seat - take the lock or wait, do not force it`);
  }
  if (s.trigger) L.push("  trigger: " + s.trigger);
  const changed = Object.keys(s.props).filter((k) => {
    const d = propsOf(s).find((p) => p.key === k);
    return d && String(s.props[k]) !== String(d.default);
  });
  if (changed.length) L.push("  properties asked for: " + changed.map((k) => `${k} = ${s.props[k]}`).join(", "));
  if (s.ctx.example) {
    const ex = s.ctx.example;
    L.push("");
    L.push("PATTERN TO MATCH");
    L.push(ex.in_script
      // No scene in this project holds a node of this type; the pattern is in
      // GDScript. Saying "follow the existing pattern" without saying WHERE is
      // how an agent goes and invents a second one.
      ? `  ${ex.scene} is where this project builds its ${ex.type} nodes - in script, not in a scene. Read it FIRST and follow it rather than adding a node to the scene tree, unless that file says otherwise.`
      : `  ${ex.scene} already has ${/^[AEIOU]/.test(ex.type) ? "an " : "a "}${ex.type} named "${ex.node}". Read it and follow it - naming, parenting, bus, and how it is triggered.`);
  }
  if (s.refs.length) {
    const nodes = s.atlas?.nodes || {};
    L.push("");
    L.push("REFERENCE POINTS (from Atlas - these are what this asset relates to)");
    s.refs.forEach((id) => { const n = nodes[id] || {}; L.push("  - " + id + (n.kind ? `  (${n.kind})` : "")); });
  }
  const steps = (s.ctx.steps || []).filter((st) => !st.done);
  if (steps.length) {
    L.push("");
    L.push("BEFORE YOU CAN WIRE IT");
    steps.forEach((st) => L.push(`  - ${st.label}  (${st.why})`));
  }
  if (s.notes.trim()) {
    L.push("");
    L.push("FROM THE PERSON WHO MADE IT");
    s.notes.trim().split("\n").forEach((t) => L.push("  " + t));
  }
  L.push("");
  L.push("DONE LOOKS LIKE");
  doneLines(s).forEach((t) => L.push("  " + t));
  L.push("");
  L.push("TOOLS THAT DO THIS: scene_outline to read the scene, scene_node_add / scene_wire / scene_set_property to change it (dry_run first), godot_check_project and godot_screenshot to prove it. All local, all free.");
  return L.join("\n");
}

/* A .tscn PROPERTY IS A GODOT LITERAL, NOT A JAVASCRIPT VALUE, and
   scenewire._prop_value refuses anything that is not — so a text field's
   contents are spelled the way the file spells them before they are sent:
   "idle" for a String, &"Music" for a StringName. */
function godotValue(p: Prop, v: unknown): unknown {
  if (p.type === "bool") return !!v;
  if (p.type === "number") { const n = Number(v); return Number.isFinite(n) ? n : null; }
  // The literal regex allows no escapes at all, so a quote or a backslash in
  // the field cannot be represented and is dropped rather than smuggled in.
  const s = String(v == null ? "" : v).replace(/["\\\r\n]/g, "").trim();
  if (!s) return null;
  return (p.literal === "stringname" ? '&"' : '"') + s + '"';
}
type PropChange = { key: string; value: unknown };
function propChanges(s: S): PropChange[] {
  const out: PropChange[] = [];
  for (const p of propsOf(s)) {
    const raw = s.props[p.key];
    if (raw === undefined || String(raw) === String(p.default)) continue;
    const value = godotValue(p, raw);
    if (value !== null) out.push({ key: p.key, value });
  }
  return out;
}

/* The dry run answers with the WHOLE new scene file. Showing 400 lines of
   unchanged .tscn to prove a two-line addition is how a plan stops being
   read, so this pulls out the lines that are actually new. */
function addedLines(d: { text?: string; id?: string; node?: string; reused?: boolean }) {
  const text = String(d.text || "");
  const out: string[] = [];
  const ext = text.split("\n").find((l) => l.startsWith("[ext_resource") && l.includes(`id="${d.id}"`));
  if (ext && !d.reused) out.push(ext);
  else if (ext) out.push(ext + "        # already in this scene - reused, not added");
  const at = text.lastIndexOf(`[node name="${d.node}"`);
  if (at !== -1) out.push(text.slice(at).trimEnd());
  return out.length ? out.join("\n\n") : text.slice(-400);
}

function Sec({ icon, title, kind, n, a, children }: {
  icon: string; title: string; kind?: string; n?: number | string; a?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className={`spanel${kind ? " " + kind : ""}`}>
      <div className="sec-h"><Ti name={icon} size={15} /><h4 className="sec-t">{title}</h4>
        {n !== undefined && n !== "" && <span className="sec-n">{n}</span>}
        {a && <span className="sec-a">{a}</span>}
      </div>
      {children}
    </div>
  );
}

const KIND_ICON: Record<string, string> = { audio: "wave-sine", mesh: "cube" };

/* ── the panel ───────────────────────────────────────────────────────────── */
function Panel({ init, onClose, expose }: {
  init: HandoffOpen; onClose(): void; expose(s: S | null): void;
}) {
  const [s, setS] = useState<S>(() => ({
    path: String(init.path).replace(/\\/g, "/"), kindHint: init.kind || "",
    name: init.name || String(init.path).split("/").pop() || "", editor: init.editor || "",
    meta: init.meta || {}, ctx: null, ctxError: "", target: 0, exit: "wire", exitPinned: false,
    scene: "", sceneInfo: null, sceneFilter: "", parent: ".", nodeName: "", nodeType: "",
    props: {}, trigger: "", notes: "", plan: null, wrote: null, check: null, shot: null, prereq: null,
    refs: (init.refs || []).slice(), refOpen: false, refFilter: "", atlas: null,
    seat: "tech", title: "", brief: "", briefEdited: false,
    filed: null, dispatched: null, autopilot: null, busy: "",
  }));
  const ref = useRef(s);
  ref.current = s;
  const alive = useRef(true);
  useEffect(() => { expose(s); }, [s, expose]);
  useEffect(() => () => { alive.current = false; expose(null); }, [expose]);
  const up = useCallback((patch: Partial<S> | ((was: S) => Partial<S>)) =>
    setS((was) => ({ ...was, ...(typeof patch === "function" ? patch(was) : patch) })), []);

  const load = useCallback(async () => {
    const cur = ref.current;
    const q = "/api/handoff/context?path=" + encodeURIComponent(cur.path)
      + (cur.kindHint ? "&kind=" + encodeURIComponent(cur.kindHint) : "");
    const r = await call<Ctx>(q);
    if (!alive.current) return;
    if (!r.ok) { up({ ctxError: r.error || "the asset could not be read" }); return; }
    const ctx = r.data;
    up((was) => {
      const next: S = { ...was, ctx, ctxError: "", target: 0 };
      next.nodeName = ctx.wire.suggested_name || "Asset";
      next.nodeType = tgt(next).node_type || "";
      next.seat = ctx.seat || "tech";
      next.props = defaultProps(next);
      /* FOLLOW WHAT IS POSSIBLE UNTIL THE PERSON SAYS OTHERWISE. A model opens
         on the agent exit because a .glb cannot be wired yet — then they run
         the engine import, the wire becomes available, and leaving them parked
         on the other exit hides the step they just unlocked. Once they have
         clicked an exit themselves, that choice is theirs. */
      if (!next.exitPinned) next.exit = ctx.wire.ok ? "wire" : "agent";
      next.title = defaultTitle(next);
      return next;
    });
    // Autopilot and Atlas are both "nice to have by the time you get there" —
    // neither blocks the first paint, and a failure in either must not take
    // the panel down with it.
    void call<{ on?: boolean }>("/api/console/autopilot").then((a) => {
      if (alive.current) up({ autopilot: a.ok ? a.data : { on: false, unknown: true } });
    });
    const atlas = (window as unknown as { Atlas?: { ensure(): Promise<unknown>; map: AtlasMap | null } }).Atlas;
    if (atlas) {
      atlas.ensure().then(() => { if (alive.current) up({ atlas: atlas.map }); }).catch(() => { /* no scan */ });
    }
  }, [up]);
  useEffect(() => { void load(); }, [load]);

  /* Never steal Escape from a nested ask prompt: it is above this panel, and
     closing the panel out from under a confirmation is how a person loses a
     half-composed brief. */
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape") return;
      if (document.querySelector(".ask-scrim")) return;
      ev.stopPropagation(); ev.preventDefault(); onClose();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const brief = s.briefEdited ? s.brief : composeBrief(s);

  /* ── actions ── */
  const pickScene = async (scene: string) => {
    up((was) => ({
      scene, sceneInfo: null, plan: null, wrote: null, check: null, shot: null,
      title: was.briefEdited ? was.title : defaultTitle({ ...was, scene }),
    }));
    const r = await call<SceneInfo>("/api/handoff/scene?scene=" + encodeURIComponent(scene));
    if (!alive.current || ref.current.scene !== scene) return;
    if (r.ok) up({ sceneInfo: r.data, parent: "." });
    else say(r.error || "that scene could not be read");
  };
  const toggleRef = (id: string) => up((was) => ({
    refs: was.refs.includes(id) ? was.refs.filter((r) => r !== id) : [...was.refs, id],
  }));
  const setTarget = (i: number) => up((was) => {
    const next = { ...was, target: i };
    return { target: i, nodeType: tgt(next).node_type || "", props: defaultProps(next), plan: null, wrote: null };
  });

  const prereqDry = async (id: string) => {
    up({ busy: "prereq", prereq: { id } });
    const rel = ref.current.ctx!.asset.rel;
    const r = id === "spriteframes" ? await call<Prereq>("/api/sprite/spriteframes", { rel, dry_run: true })
      : id === "deliver" ? await call<Prereq>("/api/handoff/mesh/deliver", { path: rel, dry_run: true })
        : { ok: false, data: {} as Prereq, error: "unknown step" };
    if (!alive.current) return;
    up({ busy: "", prereq: r.ok ? { ...r.data, id } : { id, error: r.error } });
  };
  const prereqGo = async (id: string) => {
    const what = id === "deliver"
      ? "Import this model into the engine and write a scene for it? It runs Godot headless and can take a couple of minutes."
      : "Write the SpriteFrames resource next to the sheet? An existing one is backed up first.";
    const go = await askConfirm({ title: "Write into the game project?", body: what, ok: "write it" });
    if (!go || !alive.current) return;
    up((was) => ({ busy: "prereq", prereq: { ...(was.prereq || { id }), id,
      busyNote: id === "deliver" ? "Godot is importing - this can take a couple of minutes…" : "writing…" } }));
    const rel = ref.current.ctx!.asset.rel;
    const r = id === "spriteframes" ? await call("/api/sprite/spriteframes", { rel })
      : await call("/api/handoff/mesh/deliver", { path: rel, timeout: 420 });
    if (!alive.current) return;
    if (!r.ok) { up({ busy: "", prereq: { id, error: r.error || "the write was refused" } }); return; }
    up({ busy: "", prereq: null });
    say(id === "deliver" ? "delivered into the engine" : "wrote the SpriteFrames resource", true);
    // The context is now a different shape — a sheet has a .tres, a model has
    // a scene — so re-read it rather than patching the old one by hand.
    await load();
  };

  const wireBody = (cur: S, dry: boolean) => ({
    scene: cur.scene, asset: wireRes(cur), parent: cur.parent, node_name: cur.nodeName || undefined,
    node_type: (cur.nodeType && cur.nodeType !== "(instance)") ? cur.nodeType : undefined,
    ...(dry ? { dry_run: true } : {}),
  });
  const dryRun = async () => {
    const cur = ref.current;
    if (!cur.scene) { say("pick a scene first"); return; }
    up({ busy: "plan", plan: null });
    const r = await call<{ summary?: string; node?: string; text?: string; id?: string; reused?: boolean }>(
      "/api/scene/wire", wireBody(cur, true));
    if (!alive.current) return;
    if (!r.ok) { up({ busy: "", plan: { error: r.error || "the wire was refused" } }); return; }
    const d = r.data;
    const steps = [d.summary || `add ${cur.nodeName} under ${cur.parent}`];
    propChanges(cur).forEach((p) => steps.push(`set ${p.key} = ${p.value} on ${d.node || cur.nodeName}`));
    steps.push(`take a backup of ${cur.scene.split("/").pop()} before writing`);
    up({ busy: "", plan: { steps, added: addedLines(d), node: d.node, reused: d.reused } });
  };
  const commit = async () => {
    const cur = ref.current;
    const go = await askConfirm({
      title: `Write into ${cur.scene.split("/").pop()}?`,
      body: "This edits a scene file the engine also owns. A backup is taken first, and the change is exactly the lines shown above.",
      ok: "write it",
    });
    if (!go || !alive.current) return;
    up({ busy: "commit" });
    const r = await call<Wrote & { node: string }>("/api/scene/wire", wireBody(cur, false));
    if (!alive.current) return;
    if (!r.ok) { up((was) => ({ busy: "", plan: { ...(was.plan || {}), error: r.error } })); return; }
    const node = r.data.node;
    const nodePath = cur.parent === "." ? node : cur.parent + "/" + node;
    const failures: string[] = [];
    for (const p of propChanges(cur)) {
      const pr = await call("/api/scene/node/property", { scene: cur.scene, node: nodePath, key: p.key, value: p.value });
      if (!pr.ok) failures.push(p.key + ": " + pr.error);
    }
    if (!alive.current) return;
    up({ busy: "", wrote: { ...r.data, propErrors: failures } });
    if (failures.length) say(failures.length + " property not set");
    else say("wired into " + cur.scene.split("/").pop(), true);
  };
  const runCheck = async () => {
    up({ busy: "check", check: null });
    const r = await call<S["check"]>("/api/godot/check", {});
    if (!alive.current) return;
    up({ busy: "", check: r.data || { ok: false, error: r.error } });
  };
  const runShot = async () => {
    up({ busy: "shot", shot: null });
    const r = await call<S["shot"]>("/api/godot/screenshot", { scene: ref.current.scene });
    if (!alive.current) return;
    up({ busy: "", shot: r.data || { error: r.error } });
  };

  const fileIt = async () => {
    const cur = ref.current;
    if (!cur.title.trim()) { say("the work item needs a title"); return; }
    up({ busy: "file" });
    const r = await call<{ id: number; seat: string }>("/api/queue", {
      seat: cur.seat, title: cur.title.trim(), brief, source: "handoff", source_ref: cur.ctx!.asset.rel,
    });
    if (!alive.current) return;
    if (!r.ok || !r.data || !r.data.id) { up({ busy: "" }); say(r.error || "the board refused it"); return; }
    up({ busy: "", filed: r.data });
    say("filed as #" + r.data.id, true);
    // Re-read autopilot at the moment it matters: it may have been switched
    // on since the panel opened, and offering to dispatch a thing the board
    // is already about to take is how two agents land on one item.
    const a = await call<{ on?: boolean }>("/api/console/autopilot");
    if (alive.current && a.ok) up({ autopilot: a.data });
  };
  const dispatchIt = async () => {
    const cur = ref.current;
    const go = await askConfirm({
      title: `Spawn an agent on #${cur.filed!.id}?`,
      body: "It runs with edit permission on this project and will change files. You can stop it from the Agents console.",
      ok: "dispatch",
    });
    if (!go || !alive.current) return;
    up({ busy: "dispatch" });
    const r = await call<{ ok?: boolean; pid?: number; error?: string }>(`/api/queue/${cur.filed!.id}/dispatch`, {});
    if (!alive.current) return;
    const d = r.data && typeof r.data.ok === "boolean" ? { ok: !!r.data.ok, pid: r.data.pid, error: r.data.error }
      : { ok: r.ok, error: r.error };
    up({ busy: "", dispatched: d });
    say(d.ok ? "agent running on #" + cur.filed!.id : (d.error || "dispatch refused"), d.ok);
  };

  /* ── render ── */
  const a = s.ctx?.asset;
  const wireOk = !!s.ctx?.wire.ok;
  const steps = s.ctx?.steps || [];
  const handled = wireOk || steps.some((st) => !st.done);
  const targets = s.ctx?.wire.targets || [];
  const ch = choicesOf(s);
  const plist = propsOf(s);
  const f = s.sceneFilter.toLowerCase();
  const scenes = (s.ctx?.scenes || []).filter((sc) =>
    !f || sc.label.toLowerCase().includes(f) || sc.scene.toLowerCase().includes(f));
  const refScenes = s.refs.filter((id) => String(id).endsWith(".tscn"));
  const ap = s.autopilot;
  const apOff = !!(ap && !ap.on);
  const fieldGap = { gap: "var(--s-2)" } as React.CSSProperties;

  return (
    <div className="hoff-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="hoff" role="dialog" aria-modal="true" aria-label="Put this asset in the game">
        <div className="hoff-bar">
          <span className="t">put it in the game</span>
          <span className="hoff-asset" title={a ? a.rel : s.path}>
            {a ? <><Ti name={KIND_ICON[a.kind] || "grid-dots"} size={13} /><b>{a.name}</b><span>{a.rel}</span></> : s.path}
          </span>
          <span className="a"><Button size="compact-xs" variant="default" onClick={onClose}>close</Button></span>
        </div>
        <div className="hoff-body">
          {s.ctxError ? (
            <Sec icon="alert-triangle" title="could not open">
              <div className="hoff-warn hoff-bad">{s.ctxError}</div>
              <p className="hoff-note">The path this editor handed over was <code>{s.path}</code>. If the
                file has never been saved, save it first - the file on disk is what a scene references.</p>
            </Sec>
          ) : !s.ctx || !a ? <div className="hoff-note">reading the asset…</div> : (
            <>
              {/* ── the asset ── */}
              {(!a.exists || s.meta.dirty || (a.exists && !a.in_godot && !handled) || steps.length > 0 || s.prereq || targets.length > 1) && (
                <Sec icon="box" title="the asset" kind="k-read">
                  {!a.exists ? (
                    <div className="hoff-warn hoff-bad">There is no file at that path yet. Save in the editor
                      first - a scene can only reference a file that exists.</div>
                  ) : s.meta.dirty ? (
                    <div className="hoff-warn">This editor has unsaved changes. What gets wired is the file ON
                      DISK, not what is on your canvas. Save first if the difference matters.</div>
                  ) : null}
                  {/* "OUTSIDE THE PROJECT" IS A PROBLEM FOR A SHEET AND ROUTINE FOR A MODEL:
                      the generators write .glb to staging on purpose, and the step below is
                      what brings it in — so the banner only shows when nothing is handling it. */}
                  {a.exists && !a.in_godot && !handled && (
                    <div className="hoff-warn hoff-bad">This file is outside the Godot project, so no scene can
                      reference it. Move or import it into the project first.</div>
                  )}
                  {steps.length > 0 && (
                    <div className="hoff-steps">
                      {steps.map((st) => (
                        <div className="hoff-row" key={st.id}>
                          <span className={`hoff-tag${st.done ? " on" : ""}`}>{st.done ? "done" : "needed"}</span>
                          <span className="hoff-note" style={{ flex: 1, minWidth: 220 }}>
                            <b>{st.label}</b><br />{st.done ? "already there: " + (st.target || "") : st.why}
                          </span>
                          {!st.done && (
                            <Button size="compact-xs" variant="default" onClick={() => void prereqDry(st.id)}>
                              show me what that writes
                            </Button>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {s.prereq && (s.prereq.error
                    ? <div className="hoff-warn hoff-bad">{s.prereq.error}</div>
                    : (
                      <div className="hoff-col" style={{ marginTop: "var(--s-4)" }}>
                        <span className="hoff-lbl">what this writes - nothing has happened yet</span>
                        <ol className="hoff-plan">
                          {(s.prereq.plan || (s.prereq.would_write ? ["write " + s.prereq.would_write] : []))
                            .map((l, i) => <li key={i}>{l}</li>)}
                        </ol>
                        {s.prereq.text && <pre className="hoff-pre">{String(s.prereq.text).slice(0, 1600)}</pre>}
                        <div className="hoff-row">
                          <Button size="xs" onClick={() => void prereqGo(s.prereq!.id)}>write it for real</Button>
                          <Button size="xs" variant="default" onClick={() => up({ prereq: null })}>cancel</Button>
                          {s.busy === "prereq" && <span className="hoff-busy">{s.prereq.busyNote || "working…"}</span>}
                        </div>
                      </div>
                    ))}
                  {/* TWO HONEST ANSWERS GET A CHOOSER, not a silent pick. */}
                  {targets.length > 1 && (
                    <div className="hoff-col" style={{ marginTop: "var(--s-4)" }}>
                      <span className="hoff-lbl">what actually goes in the scene</span>
                      {targets.map((t, i) => (
                        <Radio key={i} size="xs" checked={i === s.target} onChange={() => setTarget(i)}
                          label={<span className="hoff-note"><b>{t.name}</b> - {t.label}<br /><code>{t.res}</code></span>} />
                      ))}
                    </div>
                  )}
                </Sec>
              )}

              {/* ── the two exits ── */}
              <div className="hoff-exits">
                <button type="button" className={`hoff-exit${s.exit === "wire" ? " on" : ""}${wireOk ? "" : " off"}`}
                        onClick={() => up({ exit: "wire", exitPinned: true })}>
                  <b><Ti name="player-play" size={15} />Wire it here</b>
                  <span className="free">local · free · nothing generated</span>
                  <span>Pick the scene and the node, see the exact text that gets added, then write it.
                    Prove it landed with a build check and a screenshot.</span>
                  {!wireOk && <span className="free" style={{ color: "var(--warn)" }}>{s.ctx.wire.why || "not available for this file"}</span>}
                </button>
                <button type="button" className={`hoff-exit${s.exit === "agent" ? " on" : ""}`}
                        onClick={() => up({ exit: "agent", exitPinned: true })}>
                  <b><Ti name="users" size={15} />Hand it to an agent</b>
                  <span className="free">a work item, brief already filled in</span>
                  <span>Files the asset path, the scene, the trigger and the Atlas references into a
                    brief so nobody retypes what is already on this screen.</span>
                </button>
              </div>

              {/* ── exit A: wire it here ── */}
              {s.exit === "wire" && (!wireOk ? (
                <Sec icon="player-play" title="wire it here" kind="k-read">
                  <div className="hoff-warn">{s.ctx.wire.why || "this file cannot be wired mechanically"}</div>
                  <p className="hoff-note">{steps.some((st) => !st.done)
                    ? "Run the step above first, then this comes back."
                    : "Hand it to an agent instead - that exit works for anything."}</p>
                </Sec>
              ) : (
                <>
                  <Sec icon="world" title="where it goes" kind="k-list" n={scenes.length}
                       a={<TextInput size="xs" placeholder="filter scenes" value={s.sceneFilter} style={{ width: 180 }}
                                     onChange={(e) => up({ sceneFilter: e.currentTarget.value })} />}>
                    {refScenes.length > 0 && (
                      <p className="hoff-note">From your references:{" "}
                        {refScenes.map((id) => (
                          <Button key={id} size="compact-xs" variant="default" onClick={() => void pickScene(id)}>
                            {id.split("/").pop()}
                          </Button>
                        ))}
                      </p>
                    )}
                    <div className="hoff-scenes">
                      {scenes.length ? scenes.map((sc) => (
                        <button type="button" key={sc.scene} className={`hoff-scene${sc.scene === s.scene ? " on" : ""}`}
                                onClick={() => void pickScene(sc.scene)}>
                          <span className="n">{sc.label}</span>
                          <span className="hoff-tag">{sc.nodes} nodes</span>
                          {sc.has_asset && <span className="hoff-tag on">already wired</span>}
                          <span className="p">{sc.scene}</span>
                        </button>
                      )) : <div className="hoff-note" style={{ padding: "var(--s-5)" }}>no .tscn matches that filter</div>}
                    </div>
                    {s.sceneInfo && (
                      <div className="hoff-col" style={{ marginTop: "var(--s-5)" }}>
                        {s.sceneInfo.lock && (
                          <div className="hoff-warn">This scene is held by the <b>{s.sceneInfo.lock.seat}</b> seat
                            {s.sceneInfo.lock.owner ? ` (${s.sceneInfo.lock.owner})` : ""}. A write here can be
                            overwritten by whatever that seat is mid-edit on.</div>
                        )}
                        <div className="hoff-row">
                          <span className="hoff-col" style={fieldGap}><span className="hoff-lbl">parent node</span>
                            <NativeSelect size="xs" value={s.parent}
                              onChange={(e) => up({ parent: e.currentTarget.value, plan: null })}
                              data={s.sceneInfo.nodes.map((n) => ({
                                value: n.path,
                                label: (n.path === "." ? n.name + "  (root)" : n.path) + (n.type ? "  · " + n.type : ""),
                              }))} />
                          </span>
                          <span className="hoff-col" style={fieldGap}><span className="hoff-lbl">node name</span>
                            <TextInput size="xs" value={s.nodeName}
                                       onChange={(e) => up({ nodeName: e.currentTarget.value, plan: null })} />
                          </span>
                          {ch.length > 1 ? (
                            <span className="hoff-col" style={fieldGap}><span className="hoff-lbl">node type</span>
                              <NativeSelect size="xs" value={s.nodeType}
                                onChange={(e) => up({ nodeType: e.currentTarget.value, plan: null })}
                                data={ch.map((c) => ({ value: c.type, label: `${c.type}  · ${c.property || ""}` }))} />
                            </span>
                          ) : (
                            <span className="hoff-col" style={fieldGap}><span className="hoff-lbl">node type</span>
                              <span className="hoff-note" style={{ paddingTop: 6 }}><b>{s.nodeType || "instance"}</b></span>
                            </span>
                          )}
                        </div>
                      </div>
                    )}
                  </Sec>

                  {s.scene && (plist.length > 0 || a.kind === "audio") && (
                    <Sec icon="adjustments" title="how it behaves">
                      {plist.length > 0 && (
                        <div className="hoff-row">
                          {plist.map((p) => p.type === "bool" ? (
                            <Checkbox key={p.key} size="xs" checked={!!s.props[p.key]}
                              onChange={(e) => { const v = e.currentTarget.checked; up((was) => ({ props: { ...was.props, [p.key]: v }, plan: null })); }}
                              label={<span className="hoff-note">{p.label} <code>{p.key}</code></span>} />
                          ) : (
                            <span key={p.key} className="hoff-col" style={fieldGap}>
                              <span className="hoff-lbl">{p.label}</span>
                              <TextInput size="xs" style={{ width: 170 }} value={s.props[p.key] == null ? "" : String(s.props[p.key])}
                                onChange={(e) => { const v = e.currentTarget.value; up((was) => ({ props: { ...was.props, [p.key]: v }, plan: null })); }} />
                              {p.hint && <span className="hoff-note">{p.hint}</span>}
                            </span>
                          ))}
                        </div>
                      )}
                      {a.kind === "audio" && (
                        <div className="hoff-col" style={{ marginTop: "var(--s-4)" }}>
                          <span className="hoff-lbl">plays when</span>
                          <TextInput size="xs" value={s.trigger} onChange={(e) => up({ trigger: e.currentTarget.value })}
                                     placeholder="e.g. combat_start, on the floor loading, when the lift doors open" />
                          <span className="hoff-note">Autoplay is the only trigger that is a property, and it is set
                            above. Anything else - a signal, a state change, a cue from another node - is a line of
                            GDScript, which this exit does not write. Fill this in and it travels into the agent brief.</span>
                        </div>
                      )}
                    </Sec>
                  )}

                  {s.scene && (
                    <Sec icon="shield-check" title="the plan" kind="k-read"
                         a={s.busy === "plan" ? <span className="hoff-busy">reading the scene…</span>
                           : <Button size="compact-xs" variant="default" onClick={() => void dryRun()}>{s.plan ? "re-check" : "show me the change"}</Button>}>
                      {!s.plan ? (
                        <p className="hoff-note">Nothing has been written. This runs the wire as a <b>dry run</b> first
                          and shows the exact lines it would add to {s.scene}.</p>
                      ) : s.plan.error ? <div className="hoff-warn hoff-bad">{s.plan.error}</div> : (
                        <>
                          <ol className="hoff-plan">{(s.plan.steps || []).map((st, i) => <li key={i}>{st}</li>)}</ol>
                          <pre className="hoff-pre"><span className="add">{s.plan.added}</span></pre>
                          <div className="hoff-row" style={{ marginTop: "var(--s-4)" }}>
                            {s.wrote ? (
                              <span className={`hoff-warn${(s.wrote.propErrors || []).length ? "" : " hoff-good"}`} style={{ flex: 1 }}>
                                Written. {s.wrote.summary || ""}{s.wrote.backup ? `  ·  previous copy at ${s.wrote.backup}` : ""}
                                {(s.wrote.propErrors || []).length > 0 && (
                                  <><br />The node landed; these properties did not, and the scene is otherwise fine:<br />
                                    {s.wrote.propErrors!.join(" · ")}</>
                                )}
                              </span>
                            ) : (
                              <>
                                <Button size="xs" disabled={!!s.busy} onClick={() => void commit()}>
                                  write it into {s.scene.split("/").pop()}
                                </Button>
                                <span className="hoff-note">This edits a file the engine also owns. A backup is taken.</span>
                              </>
                            )}
                            {s.busy === "commit" && <span className="hoff-busy">writing…</span>}
                          </div>
                        </>
                      )}
                    </Sec>
                  )}

                  {s.wrote && (
                    <Sec icon="search" title="proof it landed" kind="k-read">
                      <div className="hoff-row">
                        <Button size="xs" variant="default" disabled={!!s.busy} onClick={() => void runCheck()}>run the build check</Button>
                        <Button size="xs" variant="default" disabled={!!s.busy} onClick={() => void runShot()}>screenshot this scene</Button>
                        {s.busy === "check" && <span className="hoff-busy">Godot is importing the project - this can take a minute…</span>}
                        {s.busy === "shot" && <span className="hoff-busy">Godot is rendering a frame…</span>}
                      </div>
                      {s.check && (
                        <div className={`hoff-warn ${s.check.ok ? "hoff-good" : "hoff-bad"}`} style={{ marginTop: "var(--s-4)" }}>
                          {s.check.ok ? "The project imports clean" : "The import reported problems"}
                          {s.check.seconds ? ` · ${s.check.seconds}s` : ""}
                          {(s.check.errors || []).length > 0 && <><br />{s.check.errors!.slice(0, 6).join("\n")}</>}
                          {s.check.error && <><br />{s.check.error}</>}
                        </div>
                      )}
                      {s.shot && (s.shot.rel
                        ? <img className="hoff-shot" style={{ marginTop: "var(--s-4)" }} alt="the scene, rendered by Godot"
                               src={`/api/preview?rel=${encodeURIComponent(s.shot.rel)}&v=${Date.now()}`} />
                        : <div className="hoff-warn hoff-bad" style={{ marginTop: "var(--s-4)" }}>{s.shot.error || "no frame came back"}</div>)}
                    </Sec>
                  )}
                </>
              ))}

              {/* ── exit B: hand it to an agent ── */}
              {s.exit === "agent" && (
                <>
                  <Sec icon="photo" title="reference points" kind="k-list" n={s.refs.length || ""}
                       a={<Button size="compact-xs" variant="default" onClick={() => up({ refOpen: !s.refOpen })}>
                         {s.refOpen ? "done" : "pick from Atlas"}</Button>}>
                    {s.refs.length ? (
                      <div className="hoff-chips">
                        {s.refs.map((id) => (
                          <span className="hoff-chip" key={id}>{id}
                            <button type="button" title="remove" onClick={() => toggleRef(id)}>&times;</button>
                          </span>
                        ))}
                      </div>
                    ) : (
                      <p className="hoff-note">Nothing attached. A reference is the thing this asset relates to - the
                        screen it belongs on, the sheet it matches, the script that will play it. It travels into
                        the brief so the agent does not go looking.</p>
                    )}
                    {s.refOpen && (!s.atlas || !s.atlas.nodes ? (
                      <p className="hoff-note">Atlas has not scanned yet, or the scan failed. Nothing to pick from.</p>
                    ) : (() => {
                      const nodes = s.atlas.nodes!;
                      const rf = s.refFilter.toLowerCase();
                      const rows = Object.keys(nodes).filter((id) => !rf || id.toLowerCase().includes(rf)
                        || String(nodes[id]?.label || "").toLowerCase().includes(rf)).slice(0, 300);
                      return (
                        <>
                          <div className="hoff-row" style={{ margin: "var(--s-4) 0" }}>
                            <TextInput size="xs" style={{ flex: 1 }} value={s.refFilter}
                                       placeholder="search everything Atlas knows about"
                                       onChange={(e) => up({ refFilter: e.currentTarget.value })} />
                            <span className="hoff-note">{rows.length} shown</span>
                          </div>
                          <div className="hoff-refs">
                            {rows.length ? rows.map((id) => {
                              const n = nodes[id] || {};
                              return (
                                <button type="button" key={id} className={`hoff-ref${s.refs.includes(id) ? " on" : ""}`}
                                        onClick={() => toggleRef(id)}>
                                  <span className="k">{n.kind || "?"}</span><span>{n.label || id}</span>
                                  {n.exists === false && <span className="hoff-tag">missing</span>}
                                  {n.orphan && <span className="hoff-tag">dead</span>}
                                  <span className="u">{id}</span>
                                </button>
                              );
                            }) : <div className="hoff-note" style={{ padding: "var(--s-5)" }}>nothing matches</div>}
                          </div>
                        </>
                      );
                    })())}
                  </Sec>

                  <Sec icon="users" title="the work item" kind="k-doc"
                       a={<Button size="compact-xs" variant="default" disabled={!s.briefEdited}
                                  onClick={() => up({ briefEdited: false })}>rebuild the brief</Button>}>
                    <div className="hoff-row" style={{ marginBottom: "var(--s-4)" }}>
                      <span className="hoff-col" style={fieldGap}><span className="hoff-lbl">seat</span>
                        <NativeSelect size="xs" value={s.seat} data={SEATS} onChange={(e) => up({ seat: e.currentTarget.value })} />
                      </span>
                      <span className="hoff-col" style={{ ...fieldGap, flex: 1, minWidth: 260 }}><span className="hoff-lbl">title</span>
                        <TextInput size="xs" value={s.title} onChange={(e) => up({ title: e.currentTarget.value })} />
                      </span>
                    </div>
                    <div className="hoff-col" style={{ marginBottom: "var(--s-4)" }}>
                      <span className="hoff-lbl">anything you want to say that is not on this screen</span>
                      <TextInput size="xs" value={s.notes} placeholder="optional - it goes into the brief verbatim"
                                 onChange={(e) => up({ notes: e.currentTarget.value })} />
                    </div>
                    <div className="hoff-col">
                      <span className="hoff-lbl">brief - this is what the agent reads</span>
                      <Textarea className="hoff-ta" classNames={{ input: "hoff-ta" }} spellCheck={false} autosize={false} rows={10}
                                value={brief} onChange={(e) => up({ brief: e.currentTarget.value, briefEdited: true })} />
                      {!s.scene && <span className="hoff-note">No target scene is picked, so the brief asks the agent to
                        choose one. Pick one under <b>Wire it here</b> first and it will be named.</span>}
                    </div>
                    <div className="hoff-row" style={{ marginTop: "var(--s-5)" }}>
                      {s.filed
                        ? <span className="hoff-warn hoff-good" style={{ flex: 1 }}>Filed as work item #{s.filed.id} on the <b>{s.filed.seat}</b> seat.</span>
                        : <Button size="xs" disabled={!!s.busy} onClick={() => void fileIt()}>file it on the board</Button>}
                      {s.busy === "file" && <span className="hoff-busy">filing…</span>}
                    </div>
                    {s.filed ? (s.dispatched ? (
                      <div className={`hoff-warn ${s.dispatched.ok ? "hoff-good" : "hoff-bad"}`} style={{ marginTop: "var(--s-4)" }}>
                        {s.dispatched.ok
                          ? <>An agent is running on #{s.filed.id} (pid {s.dispatched.pid}).</>
                          : <>It did not dispatch: {s.dispatched.error || "no reason given"}<br />The item is still on the board - nothing was lost.</>}
                      </div>
                    ) : (
                      <div className="hoff-row" style={{ marginTop: "var(--s-4)" }}>
                        <Button size="xs" disabled={!!s.busy} onClick={() => void dispatchIt()}>
                          {apOff ? `Autopilot is off - dispatch #${s.filed.id} now` : `dispatch #${s.filed.id} now`}
                        </Button>
                        <span className="hoff-note">{apOff
                          ? "Nothing will pick this up otherwise. A queued item on a board with autopilot off looks exactly like delegated work and is not."
                          : "Autopilot will get to it; this jumps the queue."}</span>
                        {s.busy === "dispatch" && <span className="hoff-busy">spawning…</span>}
                      </div>
                    )) : ap ? (
                      <p className="hoff-note" style={{ marginTop: "var(--s-3)" }}>{apOff
                        ? <>Autopilot is <b>off</b> on this project, so a queued item will sit there until something dispatches it. You get a dispatch button once it is filed.</>
                        : <>Autopilot is <b>on</b>, so the board will pick this up on its own.</>}</p>
                    ) : null}
                  </Sec>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── the imperative surface ─────────────────────────────────────────────── */
let root: Root | null = null;
let container: HTMLElement | null = null;
let latest: S | null = null;
const expose = (s: S | null) => { latest = s; };

function close(): void {
  if (root) { try { root.unmount(); } catch { /* already down */ } }
  container?.remove();
  root = null; container = null; latest = null;
}

function open(o: HandoffOpen): boolean {
  if (!o || !o.path) { say("nothing to hand off - this editor has no file open"); return false; }
  close();
  container = document.createElement("div");
  container.id = "hoff-root";
  document.body.appendChild(container);
  root = createRoot(container);
  root.render(<Themed><Panel init={o} onClose={close} expose={expose} /></Themed>);
  return true;
}

/** The one-line call site every editor uses: it reads the editor's own state
 *  object, so a toolbar button is `Handoff.fromEditor(SpriteEdit.state, …)`. */
function fromEditor(state: { rel?: string; dirty?: boolean; rigDirty?: boolean } | null,
                    o?: Partial<HandoffOpen>): boolean {
  if (!state || !state.rel) { say("open a file first - there is nothing to put in the game"); return false; }
  return open({
    path: state.rel, name: String(state.rel).split("/").pop(),
    meta: { dirty: !!(state.dirty || state.rigDirty) },
    ...(o || {}),
  } as HandoffOpen);
}

export const Handoff = { open, close, fromEditor, get state() { return latest; } };

declare global {
  interface Window {
    /** Still on window: spriteedit.js, audiolab.js and modeledit.js are
     *  classic decks and call Handoff.fromEditor from their toolbars. */
    Handoff?: typeof Handoff;
  }
}
window.Handoff = Handoff;
