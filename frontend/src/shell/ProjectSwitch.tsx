/* The project switcher.
 *
 * WHY THIS DID NOT EXIST, which is worth writing down because the API was
 * finished the whole time: POST /api/project/select works, GET /api/project
 * returns `known` — every project this machine has registered — and the ONLY
 * caller of either was views/FirstRun.tsx. First run shows once. After that the
 * app remembers the last project in ~/.bgate/active.json and opens straight
 * into it, forever, with no way to reach any other one.
 *
 * So a user with two games could open the second only by deleting a JSON file
 * they had no reason to know about. Reported, accurately, as "there is
 * literally no way to change the project".
 *
 * It lives on the project name in the nav footer because that is the one place
 * in the app that already answers "which game am I in" — the question whose
 * answer you click when you want a different one.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { mutate, readJSON, toast } from "../bridge";
import { Ti } from "./Ti";
import { EMPTY_FLEET, readFleet, type Fleet } from "./settings/AgentFleet";

declare global {
  interface Window {
    /** index.html — reveals the create/adopt card. It owns the boot-path
     *  decision about whether a project exists; this only asks it to show. */
    showFirstRun?(hint?: string): void;
  }
}

/* `known` is a MAPPING of slug -> absolute root, exactly as ~/.bgate/projects.json
   stores it, not a list of records. Written down because the obvious guess is
   an array of objects and the first version of this component made it. */
type ProjectDoc = {
  project?: { name?: string } | null;
  root?: string | null;
  known?: Record<string, string>;
};

/** "grand-theft-algorithm" -> "Grand Theft Algorithm". The registry keys are
 *  slugs; only the OPEN project's real name is known, and re-titling a slug is
 *  better than showing a user a hyphenated identifier. */
function pretty(slug: string): string {
  return slug.replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function ProjectSwitch({ name, connected }: { name: string; connected: boolean }) {
  const [open, setOpen] = useState(false);
  const [doc, setDoc] = useState<ProjectDoc | null>(null);
  const [busy, setBusy] = useState("");
  /* What is running right now, machine-wide. Switching project reloads the
     page, and a reload does not touch a spawned agent — see `arm` below. */
  const [fleet, setFleet] = useState<Fleet>(EMPTY_FLEET);
  /* The project whose switch is one click from happening. Only used while
     agents are running; a plain switch stays a single click. */
  const [arm, setArm] = useState("");
  const host = useRef<HTMLDivElement>(null);
  /* The portalled menu. It is NOT a descendant of `host`, so the
     click-outside handler has to be told about it explicitly — see below. */
  const menu = useRef<HTMLDivElement>(null);
  /* Where to put the portalled menu: beside the rail button, aligned to its
     bottom. Read on open, because the rail does not move while it is open. */
  const [anchor, setAnchor] = useState<{ left: number; bottom: number } | null>(null);

  /* Read on OPEN, not on mount. The list changes when a project is created or
     adopted elsewhere, and a footer has no business polling for that. */
  useEffect(() => {
    if (!open) { setAnchor(null); setArm(""); return; }
    readJSON<ProjectDoc>("/api/project", {}).then(setDoc);
    /* Read at the same moment and for the same reason: this menu is the last
       point at which anybody can be told that leaving does not stop anything. */
    readFleet().then(setFleet);
    const r = host.current?.getBoundingClientRect();
    if (r) setAnchor({ left: r.right + 8, bottom: window.innerHeight - r.bottom });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      /* THE BUG THIS FIXES: "clicking a project does nothing".
         The menu is portalled to <body>, so it is not inside `host`. This
         handler runs on MOUSEDOWN, decided the press was outside, and closed
         the menu — unmounting the button before its CLICK could fire. choose()
         was never called once. Same fault as the notification drawer's, which
         became a sibling of its host at the same time.
         Both containers are checked now. */
      const t = e.target as Node;
      if (host.current?.contains(t)) return;
      if (menu.current?.contains(t)) return;
      setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  async function choose(root: string) {
    if (busy) return;
    /* AGENTS DO NOT FOLLOW YOU, AND THEY DO NOT STOP EITHER.
     *
     * Every dispatched agent has BGATE_ROOT pinned into its environment at
     * spawn, and the hook and the MCP server both enforce against that pinned
     * value. That is deliberate and must stay true: an agent halfway through
     * editing a scene cannot be re-scoped to a different game mid-run without
     * its next write landing somewhere nobody asked for.
     *
     * What was wrong was the silence. The switch reloads the page, every panel
     * repoints at the new database, the old project's agents keep writing
     * files and keep billing — and nothing anywhere said so, so the reasonable
     * reading of a reload is that whatever was running stopped. It did not.
     * One click becomes two, with the count and the sentence in between; the
     * Running agents panel in Settings is where they can actually be stopped.
     */
    if (fleet.total > 0 && arm !== root) { setArm(root); return; }
    setBusy(root);
    const r = await mutate("/api/project/select", { body: { root }, quiet: true });
    if (!r.ok) {
      toast(r.error || "could not open that project", "err");
      setBusy("");
      return;
    }
    /* A FULL RELOAD, deliberately. Switching project changes the database
       every panel on the page is reading — the board, the bible, the seat
       config, the asset library, the graph. Re-fetching them piecemeal would
       leave whichever ones do not poll showing the previous game's data, which
       is a worse failure than a one-second reload because it is silent. */
    window.location.reload();
  }

  const current = doc?.root || "";
  const known = Object.entries(doc?.known || {})
    .map(([slug, root]) => ({ slug, root }))
    .filter((k) => !!k.root)
    /* Current project first, then alphabetical — a list whose order comes from
       a JSON object's insertion order changes under the reader for no reason. */
    .sort((a, b) =>
      (a.root === current ? -1 : b.root === current ? 1 : 0)
      || a.slug.localeCompare(b.slug));

  /* ON THE ICON RAIL, not in the nav footer where this started.
   *
   * The footer lives inside .bg4-nav, and that column collapses to ZERO WIDTH
   * — so the only control for changing project disappeared exactly when the
   * user narrowed the chrome, which is the same class of bug as the area
   * buttons being unreachable when collapsed. The rail is the part that is
   * always there, which is also why the offline dot and the theme switch are
   * on it. This sits with them. */
  return (
    <div className="bg4-projswitch" ref={host}>
      <button className="bg4-area" onClick={() => setOpen((v) => !v)}
              aria-expanded={open} aria-label="Switch project"
              title={connected ? `${name} — click to switch project`
                               : "no project — click to open one"}>
        <Ti name="folder" size={19} />
        {!connected && <span className="bg4-projwarn" aria-hidden="true" />}
      </button>

      {/* PORTALLED TO <body>, and that is the fix rather than styling.
          This menu was a child of .bg4-rail, and in orbit the rail carries a
          backdrop-filter. An element with backdrop-filter becomes a BACKDROP
          ROOT: a descendant's own backdrop-filter can only sample content
          inside that root, so the menu's blur had nothing but the 56px rail
          behind it and did nothing at all. The same nesting also sealed the
          menu into the rail's stacking context, which is why page content
          painted over it.

          At body level it sits above everything, frosts the actual page, and
          needs no z-index arms race with the stage. Anchored by the button's
          measured rect because it no longer shares a coordinate space with
          it. */}
      {open && anchor && createPortal(
        <div className="bg4-projmenu" role="menu" ref={menu}
             style={{ left: anchor.left, bottom: anchor.bottom }}>
          <div className="bg4-projhead">Projects on this machine</div>
          {/* SAID BEFORE THE CLICK, not after it. Switching does not stop an
              agent and cannot re-scope one, and a reload looks exactly like a
              stop from where the user is sitting. */}
          {fleet.total > 0 && (
            <div className="bg4-projrunning">
              <Ti name="alert-triangle" size={13} />
              <span>
                {fleet.total} agent{fleet.total === 1 ? "" : "s"} running
                {fleet.projects.length > 1
                  ? ` across ${fleet.projects.length} projects`
                  : ""}. They stay pinned to the project they were started in and
                keep working after you switch. Stop them in Settings → Running
                agents.
              </span>
            </div>
          )}
          {known.length === 0 && <div className="bg4-projempty">none registered yet</div>}
          {known.map((k) => {
            const here = k.root === current;
            const armed = arm === k.root;
            return (
              <button key={k.root}
                      className={armed ? "bg4-projitem armed" : "bg4-projitem"}
                      role="menuitem"
                      aria-current={here} disabled={here || !!busy}
                      onClick={() => choose(k.root)}>
                <span className="nm">{here ? name : pretty(k.slug)}</span>
                <span className="pa">{k.root}</span>
                {here && <span className="cur">open</span>}
                {armed && !busy && <span className="cur">switch anyway</span>}
                {busy === k.root && <span className="cur">opening…</span>}
              </button>
            );
          })}

          {/* NEW AND EXISTING GAMES, from the same menu.
              The create/adopt card already exists and already knows how to do
              both — it was simply unreachable once a project had been opened,
              because the only thing that ever showed it was the boot path
              deciding there was no project at all. It is a global function on
              the page, so this just calls it. */}
          <div className="bg4-projsep" />
          <button className="bg4-projitem new" role="menuitem"
                  onClick={() => { setOpen(false); window.showFirstRun?.("new"); }}>
            <span className="nm">New game…</span>
            <span className="pa">scaffold a fresh Godot project</span>
          </button>
          <button className="bg4-projitem new" role="menuitem"
                  onClick={() => { setOpen(false); window.showFirstRun?.("adopt"); }}>
            <span className="nm">Open an existing game…</span>
            <span className="pa">point Builders Gate at a folder you already have</span>
          </button>
        </div>,
        document.body)}
    </div>
  );
}
