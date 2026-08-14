import { useEffect, useRef, useState } from "react";
import { mutate, readJSON } from "../bridge";

declare global {
  interface Window {
    /* Set by showFirstRun() in index.html immediately before it dispatches, so
       a card that mounts AFTER the decision still learns of it. See the comment
       there — the module script is deferred and always loses that race. */
    __bgFirstRun?: { hint?: string };
  }
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
    <>
      <h1>Builders Gate</h1>
      <p className="fr-hint">{hint}</p>

      {known.length > 0 && (
        <div className="fr-known">
          <div className="fr-known-h">Open an existing project</div>
          <div className="fr-known-list">
            {known.map(([label, root]) => (
              <button key={root} type="button" className="fr-known-row"
                      disabled={busy !== null} onClick={() => open(root)}>
                <b>{busy === root ? "opening…" : label}</b>
                <span>{root}</span>
              </button>
            ))}
          </div>
          <div className="fr-or"><span>or start a new one</span></div>
        </div>
      )}

      <form className="fr-form" onSubmit={create}>
        <label>
          Project name
          <input ref={nameRef} value={name} onChange={(e) => setName(e.target.value)}
                 placeholder="Ember Run" maxLength={80} autoComplete="off" />
        </label>
        <label>
          Pitch <span className="opt">optional</span>
          <input value={pitch} onChange={(e) => setPitch(e.target.value)}
                 placeholder="one line - what is this game?" maxLength={200}
                 autoComplete="off" />
        </label>
        <div className="fr-kinds" role="radiogroup" aria-label="Starting template">
          {KINDS.map((k) => (
            <button key={k.id} type="button"
                    className={k.id === kind ? "fr-kind on" : "fr-kind"}
                    aria-checked={k.id === kind} role="radio"
                    onClick={() => setKind(k.id)}>
              <b>{k.label}</b><span>{k.blurb}</span>
            </button>
          ))}
        </div>
        <div className="fr-where">{where}</div>
        <button className="fr-go" type="submit" disabled={busy !== null}>
          {busy === "create" ? "creating…" : "Create project"}
        </button>
        {err && <div className="fr-err">{err}</div>}
      </form>
    </>
  );
}
