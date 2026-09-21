import { useEffect, useRef, useState } from "react";
import {
  Alert, Button, Checkbox, CloseButton, Divider, Group, Paper, Select, SimpleGrid,
  Stack, Text, Textarea, TextInput, Title,
} from "@mantine/core";
import { Ti } from "../shell/Ti";
import { mutate, readJSON } from "../bridge";

declare global {
  interface Window {
    /* Set by showFirstRun() in index.html immediately before it dispatches, so
       a card that mounts AFTER the decision still learns of it. See the comment
       there, the module script is deferred and always loses that race. */
    __bgFirstRun?: { hint?: string };
  }
}

/* One entry per value, first occurrence winning.
 *
 * THIS IS A GUARD, NOT A TIDY-UP, and the bug it exists for cost a whole
 * release. Mantine's Select THROWS on a duplicate value rather than rendering
 * it twice, and an exception thrown during render unmounts the React tree
 * containing it. One folder registered under two names, which is what
 * renaming a project leaves behind, therefore did not produce a duplicate
 * menu row. It produced a black window: the packaged app opened, painted
 * nothing, and sat there while the server behind it answered every request
 * perfectly, which is the hardest possible version of this to diagnose.
 *
 * known_projects() now guarantees uniqueness on the server side, so this is
 * the second of two locks. It stays because the cost of the list being wrong
 * must be a missing row, never a missing application.
 */
function oncePerValue<T extends { value: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  return items.filter(({ value }) =>
    seen.has(value) ? false : (seen.add(value), true));
}

/* The first-run card, the one screen a new user is guaranteed to meet.
 *
 * Two ways in, and OPENING COMES FIRST because it is the commoner act. This
 * screen could only create: someone with eight registered games who opened the
 * dashboard from the wrong directory was told they had none and invited to make
 * a ninth. The registry was already in /api/project's `known` and simply had no
 * button attached.
 *
 * The overlay's visibility is NOT React's. pollState() in index.html decides
 * when there is no project, and it toggles `#firstrun[hidden]` exactly as it
 * always did; this component only fills the card. That keeps the boot path's
 * control flow in one place instead of splitting it across two runtimes, and it
 * is why the component listens for a hint rather than fetching /api/state.
 *
 * Both actions end in location.reload(). The dashboard token is minted per
 * project and this page was served without one, so every fetch the shell has
 * queued is carrying nothing, a re-render would leave a signed-out page. */

type Template = { kind: string; engine: string; available: boolean; description: string };
type Engine = { name: string; label: string; blurb: string; supported: boolean;
                templates: Template[]; adopt_only?: boolean };
type ProjectInfo = { cwd?: string; known?: Record<string, string>; kinds?: string[];
                     engines?: Engine[] };
/* A reference image waiting to be pinned. `data` is a data-URL: the upload
   goes as base64 in the JSON body, the same shape /api/refs/upload takes, so
   the server needs no multipart handler for this either. */
type Ref = { name: string; data: string; ext: string; bytes: number };

/* What renders before /api/project answers, and what an older backend without
   `engines` still gets: the Godot templates, worded as the scaffolder words
   them. Once the registry arrives the cards are driven by it, so a third
   engine needs no edit here. */
const FALLBACK_ENGINES: Engine[] = [
  { name: "godot", label: "Godot", supported: true,
    blurb: "Godot 4. Scene surgery, in-engine checks, screenshots and the telemetry autoload.",
    templates: [
      { kind: "2d", engine: "godot", available: true,
        description: "Side-on platformer slice - player, ground, ledge, jump/land telemetry." },
      { kind: "3d", engine: "godot", available: true,
        description: "Third-person slice - controller, camera rig, prop kit, jump/land telemetry." },
    ] },
];

const slugify = (s: string) =>
  s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

/* The server's caps, mirrored so the card says no before the round trip. */
const MAX_BRIEF = 60000;
const MAX_REFS = 24;
const MAX_REF_BYTES = 12 * 1024 * 1024;
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"];

function readAsDataURL(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export default function FirstRun() {
  const [hint, setHint] = useState(
    () => window.__bgFirstRun?.hint || "No project here yet.");
  // Armed = the shell has actually decided there is no project. Until then this
  // component is mounted but dormant inside a hidden overlay, and must not
  // spend a request on a registry nobody is going to see: a healthy boot would
  // otherwise fetch /api/project on every single page load.
  const [armed, setArmed] = useState(() => !!window.__bgFirstRun);
  const [info, setInfo] = useState<ProjectInfo>({});
  const nameRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  const [pitch, setPitch] = useState("");
  const [brief, setBrief] = useState("");
  const [refs, setRefs] = useState<Ref[]>([]);
  const [kickoff, setKickoff] = useState(true);
  const [dragging, setDragging] = useState(false);
  const [kind, setKind] = useState("2d");
  const [engine, setEngine] = useState("godot");
  const [adoptPath, setAdoptPath] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState("");

  /* index.html shouts the reason when it puts this card up, "no .bgate project
     at or above the cwd", or whatever the backend actually said. It is dispatched
     rather than passed because the shell has no handle on a React component. */
  useEffect(() => {
    const onShow = (e: Event) => {
      const detail = (e as CustomEvent<{ hint?: string }>).detail;
      if (detail?.hint) setHint(detail.hint);
      setArmed(true);
    };
    window.addEventListener("bgate:firstrun", onShow);
    return () => window.removeEventListener("bgate:firstrun", onShow);
  }, []);

  useEffect(() => {
    if (!armed) return;
    // This endpoint answers before a project, and therefore before a token -
    // exists, which is the whole reason it is separate from /api/state.
    readJSON<ProjectInfo>("/api/project", {}).then(setInfo);
    // The old card focused the name field as it opened; a hidden input cannot
    // take focus, so autoFocus at mount would have been a no-op here.
    nameRef.current?.focus();
  }, [armed]);

  const known = Object.entries(info.known || {}).sort((a, b) =>
    a[0].localeCompare(b[0]));
  const engines = (info.engines && info.engines.length) ? info.engines : FALLBACK_ENGINES;
  const current = engines.find((e) => e.name === engine) || engines[0];
  const kinds = current.templates.filter((t) => t.available);
  // An engine with no template is adopted, never scaffolded: the form points
  // at the project the engine's own tooling made.
  const adopting = !!current.adopt_only;

  // Where a new project would land, said before you commit to it, the audit's
  // other complaint about project_init was that it never told you where.
  const sep = (info.cwd || "").includes("\\") ? "\\" : "/";
  const where = info.cwd && slugify(name)
    ? `will be created at  ${info.cwd}${sep}${slugify(name)}` : "";

  const seeding = brief.trim().length > 0 || refs.length > 0;

  /* THE PROJECT START IS THE BRIEF AND THE SCREENSHOTS. Every project so far
     began the same way: a long brief pasted into the director chat with a row
     of reference images and "pin these in the bible". This card took a
     one-line pitch, so that start happened a screen later and by hand. Now the
     paste lands here, the server files the brief, pins the images into the
     bible, and hands the director the same first turn the human used to
     type. Images arrive by the picker, by drop, or pasted straight into the
     brief box, because that is how a screenshot reaches a chat box. */
  async function addFiles(files: Iterable<File>, label?: string) {
    const picked: Ref[] = [];
    let n = refs.length;
    for (const file of files) {
      if (!IMAGE_TYPES.includes(file.type)) { setErr(`${file.name || "that file"} is not an image`); continue; }
      if (file.size > MAX_REF_BYTES) { setErr(`${file.name} is over 12 MB; export a smaller copy`); continue; }
      if (n + picked.length >= MAX_REFS) { setErr(`${MAX_REFS} reference images is the cap`); break; }
      const data = await readAsDataURL(file);
      const ext = (file.type.split("/")[1] || "png").replace("svg+xml", "svg");
      const stem = (file.name || "").replace(/\.[a-z0-9]+$/i, "");
      const base = slugify(stem) || `${label || "pasted"}-${n + picked.length + 1}`;
      picked.push({ name: base, data, ext, bytes: file.size });
    }
    if (picked.length) {
      setErr("");
      setRefs((prev) => {
        // A second copy of the same name would re-pin over the first; give it
        // a suffix instead so both survive to the bible.
        const taken = new Set(prev.map((r) => r.name));
        const out = [...prev];
        for (const r of picked) {
          let nm = r.name; let i = 2;
          while (taken.has(nm)) nm = `${r.name}-${i++}`;
          taken.add(nm); out.push({ ...r, name: nm });
        }
        return out;
      });
    }
  }

  function onPaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const items = Array.from(e.clipboardData?.items || []);
    const images = items.filter((it) => it.kind === "file" && it.type.startsWith("image/"))
      .map((it) => it.getAsFile()).filter((f): f is File => !!f);
    if (!images.length) return;
    // An image paste is a reference, not text: keep it out of the brief.
    e.preventDefault();
    void addFiles(images, "pasted");
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault(); setDragging(false);
    if (e.dataTransfer?.files?.length) void addFiles(Array.from(e.dataTransfer.files), "dropped");
  }

  function seedBody() {
    return {
      brief: brief.trim(),
      refs: refs.map(({ name: n, data, ext }) => ({ name: n, data, ext, kind: "concept" })),
      kickoff: seeding && kickoff,
    };
  }

  async function open(root: string) {
    setErr(""); setBusy(root);
    // quiet: the reason belongs inline in this card, not in a toast behind the
    // overlay that covers the whole screen.
    const r = await mutate("/api/project/select", { quiet: true, body: { root } });
    if (!r.ok) { setErr(r.error || "could not open that project"); setBusy(null); return; }
    location.reload();
  }

  async function adopt(e: React.FormEvent) {
    e.preventDefault();
    if (!adoptPath.trim()) { setErr("point at the project folder first"); return; }
    setErr(""); setBusy("adopt");
    const r = await mutate("/api/project/adopt", {
      quiet: true,
      body: { path: adoptPath.trim(), name: name.trim(), pitch: pitch.trim(), ...seedBody() },
    });
    if (!r.ok) { setErr(r.error || "could not adopt that project"); setBusy(null); return; }
    location.reload();
  }

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (adopting) return adopt(e);
    if (!name.trim()) { setErr("give it a name first"); return; }
    if (brief.length > MAX_BRIEF) { setErr(`the brief is ${brief.length} characters; ${MAX_BRIEF} is the cap`); return; }
    setErr(""); setBusy("create");
    const r = await mutate("/api/project", {
      quiet: true,
      body: { name: name.trim(), kind, engine, pitch: pitch.trim(), ...seedBody() },
    });
    if (!r.ok) { setErr(r.error || "could not create that project"); setBusy(null); return; }
    location.reload();
  }

  return (
    /* MANTINE, like the rest of the shell.
     *
     * This card was the last hand-rolled form in the app, bare <input>, <label>
     * and eight fr-* classes, styled in app.css and drifting from every other
     * surface: its inputs had a different height, focus ring and disabled state
     * than the ones three clicks away in Settings. It is also the FIRST screen a
     * new user meets, so it was the one making the first impression while being
     * the one piece not using the design system.
     *
     * Behaviour is unchanged and deliberately so: the overlay's visibility is
     * still index.html's (#firstrun[hidden]), both actions still end in
     * location.reload() because the dashboard token is minted per project, and
     * the registry still comes from /api/project's `known`. */
    <Stack gap="md">
      {/* NO MARK AND NO TERMINAL FOOTER HERE. index.html renders both OUTSIDE
          this island, the mark above it, the `bgate init` line below, the
          latter deliberately, so it survives the bundle failing to load.
          Rendering them again put two of each on the card. */}
      <div>
        <Title order={2} fz="xl" fw={600}>Builders Gate</Title>
        <Text size="sm" c="dimmed" mt={4}>{hint}</Text>
      </div>

      {/* ONE CONTROL, NOT A LIST. Eight registered projects rendered as eight
          two-line rows made this card taller than the window, and the fix for
          that is not a scroller inside a scroller, it is not spending 400px
          on a list you pick one item from once. A Select is the same choice in
          40px, and it grows to fifty projects without the card changing size
          at all. */}
      {known.length > 0 && (
        <Stack gap="xs">
          <Select label="Open an existing project"
                  placeholder={busy && busy !== "create" ? "opening…" : "pick a project"}
                  data={oncePerValue(known.map(([label, root]) => ({ value: root, label })))}
                  disabled={busy !== null}
                  searchable={known.length > 6}
                  leftSection={<Ti name="folder" size={15} />}
                  onChange={(root) => root && open(root)}
                  size="md" />
          <Divider label="or start a new one" labelPosition="center" my={2} />
        </Stack>
      )}

      <form onSubmit={create}>
        <Stack gap="sm">
          <TextInput ref={nameRef} label="Project name" value={name}
                     onChange={(e) => setName(e.currentTarget.value)}
                     placeholder="Ember Run" maxLength={80} autoComplete="off"
                     size="md" />
          <TextInput label="Pitch"
                     description="optional, one line, what is this game?"
                     value={pitch} onChange={(e) => setPitch(e.currentTarget.value)}
                     placeholder="one line - what is this game?" maxLength={200}
                     autoComplete="off" size="md" />

          <Textarea label="Brief"
                    description="optional, the whole thing: premise, loop, tone, what it is not. Saved to design/brief.md; the director opens with it as its first turn. Paste screenshots here and they become references."
                    value={brief} onChange={(e) => setBrief(e.currentTarget.value)}
                    onPaste={onPaste}
                    placeholder={"Build EXIT 67 as a complete, replayable 2D side-scrolling roguelite run-and-gun…"}
                    autosize minRows={4} maxRows={14} maxLength={MAX_BRIEF} size="md" />
          {brief.length > MAX_BRIEF * 0.9 && (
            <Text size="xs" c={brief.length > MAX_BRIEF ? "red" : "dimmed"}>
              {brief.length.toLocaleString()} / {MAX_BRIEF.toLocaleString()} characters
            </Text>
          )}

          {/* The references. A drop target and a picker, and the thumbnails of
              what will be pinned, each removable, because a wrong screenshot
              pinned into the bible is a wrong screenshot every seat copies. */}
          <Paper withBorder p="sm" radius="md"
                 className={dragging ? "fr-kind on" : "fr-kind"}
                 onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                 onDragLeave={() => setDragging(false)}
                 onDrop={onDrop}
                 style={{ cursor: "default" }}>
            <Group justify="space-between" align="center" wrap="nowrap">
              <div>
                <Text size="sm" fw={600}>Reference images</Text>
                <Text size="xs" c="dimmed">
                  {refs.length
                    ? `${refs.length} pinned into the bible as concept refs at creation`
                    : "optional, drop screenshots or concept art here; they are pinned into the bible at creation"}
                </Text>
              </div>
              <Button variant="default" size="xs" onClick={() => fileRef.current?.click()}
                      leftSection={<Ti name="photo-plus" size={14} />} disabled={busy !== null}>
                Add images
              </Button>
              <input ref={fileRef} type="file" multiple hidden
                     accept={IMAGE_TYPES.join(",")}
                     onChange={(e) => {
                       if (e.currentTarget.files) void addFiles(Array.from(e.currentTarget.files));
                       e.currentTarget.value = "";
                     }} />
            </Group>
            {refs.length > 0 && (
              <SimpleGrid cols={{ base: 3, sm: 4 }} spacing="xs" mt="sm">
                {refs.map((r) => (
                  <div key={r.name} style={{ position: "relative" }}>
                    <img src={r.data} alt={r.name}
                         style={{ width: "100%", aspectRatio: "4 / 3", objectFit: "cover",
                                  borderRadius: 6, display: "block" }} />
                    <CloseButton size="xs" aria-label={`remove ${r.name}`}
                                 style={{ position: "absolute", top: 4, right: 4 }}
                                 onClick={() => setRefs((prev) => prev.filter((x) => x.name !== r.name))} />
                    <Text size="xs" c="dimmed" truncate mt={2}>{r.name}</Text>
                  </div>
                ))}
              </SimpleGrid>
            )}
          </Paper>

          {seeding && (
            <Checkbox checked={kickoff} onChange={(e) => setKickoff(e.currentTarget.checked)}
                      label="Start the director on this brief as soon as the project exists"
                      description="off: the brief and refs are filed, the kickoff waits on the thread for whoever opens the chat" />
          )}

          {/* Cards rather than a SegmentedControl: each option carries a
              sentence describing the slice it scaffolds, and that does not fit
              in a segment. Still a radiogroup to a screen reader. */}
          {/* The engine first, because it decides which templates exist and
              which tools every seat is handed afterwards. One engine renders
              no picker at all. */}
          {engines.length > 1 && (
            <div role="radiogroup" aria-label="Engine">
              <Group grow align="stretch" gap="sm">
                {engines.map((e) => (
                  <Paper key={e.name} component="button" type="button" role="radio"
                         aria-checked={e.name === engine} p="sm" withBorder
                         className={e.name === engine ? "fr-kind on" : "fr-kind"}
                         onClick={() => {
                           setEngine(e.name);
                           if (!e.templates.some((t) => t.kind === kind && t.available)) {
                             const first = e.templates.find((t) => t.available);
                             if (first) setKind(first.kind);
                           }
                         }}>
                    <Text size="sm" fw={600} ta="left">{e.label}</Text>
                    <Text size="xs" c="dimmed" mt={4} ta="left">{e.blurb}</Text>
                  </Paper>
                ))}
              </Group>
            </div>
          )}

          {adopting && (
            <TextInput label={`${current.label} project folder`}
                       description="the directory the engine's own tooling created; adopted as it stands, nothing is overwritten"
                       value={adoptPath} onChange={(e) => setAdoptPath(e.currentTarget.value)}
                       placeholder={"C:\\games\\my-unity-game"} autoComplete="off" size="md" />
          )}

          {!adopting && <div role="radiogroup" aria-label="Starting template">
            <Group grow align="stretch" gap="sm">
              {kinds.map((k) => (
                <Paper key={k.kind} component="button" type="button" role="radio"
                       aria-checked={k.kind === kind} p="sm" withBorder
                       className={k.kind === kind ? "fr-kind on" : "fr-kind"}
                       onClick={() => setKind(k.kind)}>
                  <Text size="sm" fw={600} ta="left">{k.kind.toUpperCase()}</Text>
                  <Text size="xs" c="dimmed" mt={4} ta="left">{k.description}</Text>
                </Paper>
              ))}
            </Group>
          </div>}

          {where && !adopting && (
            <Text size="xs" c="dimmed" ff="var(--mono)">{where}</Text>
          )}

          <Button type="submit" size="md" loading={busy === "create" || busy === "adopt"}
                  disabled={busy !== null}>
            {adopting ? "Adopt project" : seeding ? "Create project and kick off" : "Create project"}
          </Button>

          {err && (
            <Alert color="red" variant="light" icon={<Ti name="alert-triangle" size={15} />}>
              {err}
            </Alert>
          )}
        </Stack>
      </form>
    </Stack>
  );
}
