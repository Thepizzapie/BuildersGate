import { useEffect, useRef, useState } from "react";
import {
  Alert, Button, Divider, Group, Paper, Select, Stack, Text, TextInput, Title,
} from "@mantine/core";
import { Ti } from "../shell/Ti";
import { mutate, readJSON } from "../bridge";

declare global {
  interface Window {
    /* Set by showFirstRun() in index.html immediately before it dispatches, so
       a card that mounts AFTER the decision still learns of it. See the comment
       there — the module script is deferred and always loses that race. */
    __bgFirstRun?: { hint?: string };
  }
}

/* One entry per value, first occurrence winning.
 *
 * THIS IS A GUARD, NOT A TIDY-UP, and the bug it exists for cost a whole
 * release. Mantine's Select THROWS on a duplicate value rather than rendering
 * it twice, and an exception thrown during render unmounts the React tree
 * containing it. One folder registered under two names — which is what
 * renaming a project leaves behind — therefore did not produce a duplicate
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

/* The first-run card — the one screen a new user is guaranteed to meet.
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
 * queued is carrying nothing — a re-render would leave a signed-out page. */

type ProjectInfo = { cwd?: string; known?: Record<string, string>; kinds?: string[] };

const KINDS = [
  { id: "2d", label: "2D",
    blurb: "Side-on platformer slice - player, ground, ledge, jump/land telemetry." },
  { id: "3d", label: "3D",
    blurb: "First-person slice - capsule player, ground, block, jump/land telemetry." },
];

const slugify = (s: string) =>
  s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

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
  const [name, setName] = useState("");
  const [pitch, setPitch] = useState("");
  const [kind, setKind] = useState("2d");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState("");

  /* index.html shouts the reason when it puts this card up — "no .bgate project
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
    // This endpoint answers before a project — and therefore before a token —
    // exists, which is the whole reason it is separate from /api/state.
    readJSON<ProjectInfo>("/api/project", {}).then(setInfo);
    // The old card focused the name field as it opened; a hidden input cannot
    // take focus, so autoFocus at mount would have been a no-op here.
    nameRef.current?.focus();
  }, [armed]);

  const known = Object.entries(info.known || {}).sort((a, b) =>
    a[0].localeCompare(b[0]));

  // Where a new project would land, said before you commit to it — the audit's
  // other complaint about project_init was that it never told you where.
  const sep = (info.cwd || "").includes("\\") ? "\\" : "/";
  const where = info.cwd && slugify(name)
    ? `will be created at  ${info.cwd}${sep}${slugify(name)}` : "";

  async function open(root: string) {
    setErr(""); setBusy(root);
    // quiet: the reason belongs inline in this card, not in a toast behind the
    // overlay that covers the whole screen.
    const r = await mutate("/api/project/select", { quiet: true, body: { root } });
    if (!r.ok) { setErr(r.error || "could not open that project"); setBusy(null); return; }
    location.reload();
  }

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) { setErr("give it a name first"); return; }
    setErr(""); setBusy("create");
    const r = await mutate("/api/project", {
      quiet: true, body: { name: name.trim(), kind, pitch: pitch.trim() },
    });
    if (!r.ok) { setErr(r.error || "could not create that project"); setBusy(null); return; }
    location.reload();
  }

  return (
    /* MANTINE, like the rest of the shell.
     *
     * This card was the last hand-rolled form in the app — bare <input>, <label>
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
          this island — the mark above it, the `bgate init` line below — the
          latter deliberately, so it survives the bundle failing to load.
          Rendering them again put two of each on the card. */}
      <div>
        <Title order={2} fz="xl" fw={600}>Builders Gate</Title>
        <Text size="sm" c="dimmed" mt={4}>{hint}</Text>
      </div>

      {/* ONE CONTROL, NOT A LIST. Eight registered projects rendered as eight
          two-line rows made this card taller than the window, and the fix for
          that is not a scroller inside a scroller — it is not spending 400px
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
                     description="optional — one line, what is this game?"
                     value={pitch} onChange={(e) => setPitch(e.currentTarget.value)}
                     placeholder="one line - what is this game?" maxLength={200}
                     autoComplete="off" size="md" />

          {/* Cards rather than a SegmentedControl: each option carries a
              sentence describing the slice it scaffolds, and that does not fit
              in a segment. Still a radiogroup to a screen reader. */}
          <div role="radiogroup" aria-label="Starting template">
            <Group grow align="stretch" gap="sm">
              {KINDS.map((k) => (
                <Paper key={k.id} component="button" type="button" role="radio"
                       aria-checked={k.id === kind} p="sm" withBorder
                       className={k.id === kind ? "fr-kind on" : "fr-kind"}
                       onClick={() => setKind(k.id)}>
                  <Text size="sm" fw={600} ta="left">{k.label}</Text>
                  <Text size="xs" c="dimmed" mt={4} ta="left">{k.blurb}</Text>
                </Paper>
              ))}
            </Group>
          </div>

          {where && (
            <Text size="xs" c="dimmed" ff="var(--mono)">{where}</Text>
          )}

          <Button type="submit" size="md" loading={busy === "create"}
                  disabled={busy !== null}>
            Create project
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
