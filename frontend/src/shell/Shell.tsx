import { useEffect, useRef, useState } from "react";
import "./shell.css";
import { Menu, Modal, ScrollArea, TextInput } from "@mantine/core";
import { Ti } from "./Ti";
import { AREAS, LOOSE, SCREEN_NOTE, SEAT_COLOR, SEAT_ICON, areaOf, byScreen } from "./nav";
import type { Area } from "./nav";
import { Inspector } from "./Inspector";
import { Bell } from "./Bell";
import { ProjectSwitch } from "./ProjectSwitch";
import { clock, useFloor } from "./useFloor";
import type { Floor } from "./useFloor";
import { useAppState } from "../store";
import { setSelection } from "./selection";
import { setScreen as publishScreen } from "./screen";
import { AssetBrowser } from "./AssetBrowser";
import { Readiness } from "./Readiness";
import { claimUtility, onUtility, type UtilityName } from "./utility";
import { setUrlParams, urlParam } from "./urlState";
import { GROUNDS, ThemeSample, useGround } from "./ThemePicker";

/* The shell: four areas, the screens inside one of them, a header, and the
 * inspector. Everything BETWEEN those is still the existing stage.
 *
 * WHAT MOVED AND WHAT DID NOT. The rail and the status bar are gone from
 * index.html and are rendered here. The fifteen `.deck-view` sections are
 * untouched, still owned by the classic modules, still switched by
 * setWorkspace() — this component calls it and never reaches inside a deck. So
 * a redesign of the chrome cost the views nothing, and the two new screens
 * (floor, board) are decks like any other rather than a special case.
 *
 * The shell is laid out with `display: contents` so its four children land
 * directly in the body's grid alongside `.stage`. Reparenting the stage into a
 * React-owned node was the alternative and it would have reloaded every iframe
 * on the page at boot.
 */

const KEY = "bgate-screen";
const NAV_KEY = "bgate-nav-open";

type FindResult = { key: string; icon: string; label: string; detail: string; pick(): void };

function CommandPalette({ opened, onClose, onScreen, floor }: {
  opened: boolean; onClose(): void; onScreen(id: string): void; floor: Floor;
}) {
  const [query, setQuery] = useState("");
  const state = useAppState();
  useEffect(() => { if (opened) setQuery(""); }, [opened]);
  const finish = (fn: () => void) => { fn(); onClose(); };
  const screens: FindResult[] = [...AREAS.flatMap((a) => a.screens), ...LOOSE].map((s) => ({
    key: `screen-${s.id}`, icon: s.icon, label: s.label,
    detail: `${areaOf(s.id)?.label || "App"} screen`, pick: () => onScreen(s.id),
  }));
  const work: FindResult[] = floor.items.map((item) => ({
    key: `item-${item.id}`, icon: SEAT_ICON[item.seat] || "point",
    label: item.title || `Work item ${item.id}`,
    detail: `#${item.id} · ${item.seat || "unassigned"} · ${item.status || "unknown"}`,
    pick: () => { onScreen("agents"); setSelection({ key: `i${item.id}`, kind: "item",
      itemId: item.id, title: item.title, seat: item.seat }); },
  }));
  const assets: FindResult[] = state.asset_groups.map((asset) => ({
    key: `asset-${asset.logical_name}`, icon: "photo", label: asset.logical_name,
    detail: "Asset family", pick: () => onScreen("assets"),
  }));
  const needle = query.trim().toLowerCase();
  const results = [...screens, ...work, ...assets]
    .filter((r) => !needle || `${r.label} ${r.detail}`.toLowerCase().includes(needle))
    .slice(0, 30);
  return (
    <Modal opened={opened} onClose={onClose} title="Find anything" centered size="lg"
           classNames={{ content: "bg4-command", header: "bg4-command-head" }}>
      <TextInput data-autofocus value={query} onChange={(e) => setQuery(e.currentTarget.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && results[0]) finish(results[0].pick); }}
                 leftSection={<Ti name="search" size={15} />} placeholder="Screens, work, assets…"
                 aria-label="Search Builders Gate" />
      <ScrollArea.Autosize mah={420} mt="sm">
        <div className="bg4-command-results">
          {results.map((r, index) => (
            <button key={r.key} className="bg4-command-row" onClick={() => finish(r.pick)}>
              <Ti name={r.icon} size={16} />
              <span><b>{r.label}</b><small>{r.detail}</small></span>
              {index === 0 && needle && <kbd>Enter</kbd>}
            </button>
          ))}
          {!results.length && <div className="bg4-command-empty">No matching screen, work, or asset.</div>}
        </div>
      </ScrollArea.Autosize>
    </Modal>
  );
}

declare global {
  interface Window {
    /* index.html — the classic switch. It also dispatches `bgate:workspace`
       so this shell can follow a deck change it did not initiate (the vault
       chip, first-run, Atlas's own deactivation path). */
    setWorkspace?(name: string, trigger?: Element | null): void;
    /** index.html — writes data-theme, persists it, and tells the canvases. */
    setTheme?(mode: string): void;
  }
}

/* THE GROUND SWITCH LIVED ON THE OLD RAIL AND NOWHERE ELSE. Replacing that
   rail deleted the only way to change theme in the whole product — Settings
   has never carried one — so it came back with it.

   ONE BUTTON, NOT FOUR. A 2×2 of moon/sun/monitor/planet in a 56px rail is
   four glyphs that look like four destinations; nothing about them says they
   are one exclusive choice, and the planet in particular reads as a feature.
   The button wears the ground you are ON and pops the other three out, which
   is both smaller and self-explaining. */
function Grounds() {
  const [mode, setMode] = useGround();
  const current = GROUNDS.find((g) => g.id === mode) || GROUNDS[GROUNDS.length - 1];
  return (
    <Menu position="right-end" offset={8} withArrow shadow="md" width={270}>
      <Menu.Target>
        <button className="bg4-area" title={`Theme · ${current.label}`}
                aria-label="Colour theme">
          <Ti name={current.icon} size={18} />
        </button>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Appearance</Menu.Label>
        {GROUNDS.map((g) => (
          <Menu.Item key={g.id} className="bg4-theme-menu-item"
                     leftSection={<Ti name={g.icon} size={14} />}
                     rightSection={g.id === mode ? <Ti name="check" size={13} /> : undefined}
                     onClick={() => setMode(g.id)}>
            <span className="bg4-theme-menu-copy"><b>{g.label}</b><small>{g.note}</small></span>
            <ThemeSample ground={g} compact />
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  );
}

/* AN AREA BUTTON HAS TWO JOBS AND ONLY ONE OF THEM WORKED.
 *
 * Open, the rail picks the area and the column beside it lists the screens. But
 * COLLAPSED, that column is zero pixels wide — and clicking an area only ever
 * jumped to its FIRST screen, so Studio, Narrative, Atlas and the World bible
 * were unreachable by any route at all. Collapsing the nav quietly removed
 * eleven of the fifteen destinations from the product.
 *
 * So collapsed, the button opens the area's screens as a pop-out: the same
 * list, the same counts, the same current mark, in the space that exists. Open,
 * it stays a plain button, because a menu that duplicates a column you can
 * already see is a second way to do one thing.
 */
function AreaButton({ area, current, screen, collapsed, counts, onPick }: {
  area: Area; current: boolean; screen: string; collapsed: boolean;
  counts: Record<string, number>; onPick(id: string): void;
}) {
  const label = (
    <>
      {area.label}
      <span className="k"> · {area.screens.length} screens</span>
    </>
  );
  const button = (
    <button className="bg4-area" aria-current={current}
            aria-label={area.label} title={collapsed ? undefined : area.label}
            onClick={collapsed ? undefined : () => onPick(area.screens[0].id)}>
      <Ti name={area.icon} size={19} />
    </button>
  );
  if (!collapsed) return button;
  return (
    <Menu position="right-start" offset={8} withArrow shadow="md" width={210}>
      <Menu.Target>{button}</Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>{label}</Menu.Label>
        {area.screens.map((s) => (
          <Menu.Item key={s.id}
                     leftSection={<Ti name={s.icon} size={14} color={s.accent} />}
                     rightSection={
                       s.id === screen ? <Ti name="check" size={13} />
                       : s.count && counts[s.count] > 0
                         ? <span className="bg4-menun">{counts[s.count]}</span>
                         : undefined}
                     onClick={() => onPick(s.id)}>
            {s.label}
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  );
}

/* THE COUNT WAS THE WHOLE ANSWER AND IT IS NEVER THE QUESTION. "3 running" told
 * you the board was busy and then made you leave the screen you were on to find
 * out busy with what. The chip carries the list it is counting now, and a row
 * goes straight to the inspector — which is a pop-out over the stage, so
 * glancing at a run costs you nothing you were looking at.
 *
 * It reads the shell's existing floor poll rather than opening one of its own.
 * The rail's counters are already live at 6s whether or not this is open, and a
 * second poll against the same three endpoints would double the traffic to say
 * the same thing a beat sooner. */
function RunningChip({ floor, onOpenFloor }: {
  floor: Floor; onOpenFloor(): void;
}) {
  const byId = new Map(floor.items.map((i) => [i.id, i]));
  return (
    <Menu position="bottom-end" offset={8} withArrow shadow="md" width={320}>
      <Menu.Target>
        <button className="bg4-chip"
                style={{ color: floor.counts.live ? "var(--accent)" : "var(--text-3)" }}
                title="Agents currently executing a task.">
          <Ti name="player-play" />{floor.counts.live} running
        </button>
      </Menu.Target>
      <Menu.Dropdown className="bg4-runmenu">
        <Menu.Label>Running now</Menu.Label>
        {floor.agents.length === 0 && (
          <Menu.Item disabled>nothing is running</Menu.Item>
        )}
        {floor.agents.map((a) => {
          const it = byId.get(a.item_id);
          const seat = it?.seat || "";
          const title = it?.title || `item ${a.item_id}`;
          const c = SEAT_COLOR[seat] || "var(--text-3)";
          const age = clock(a.seconds);
          return (
            <Menu.Item key={a.item_id}
                       leftSection={
                         <span className="bg4-runtile" style={{ background: `${c}22`, color: c }}>
                           <Ti name={SEAT_ICON[seat] || "point"} size={13} />
                         </span>}
                       rightSection={age ? <span className="bg4-runage">{age}</span> : undefined}
                       onClick={() => setSelection({ key: `i${a.item_id}`, kind: "item",
                                                     itemId: a.item_id, title, seat })}>
              <span className="bg4-runtitle">
                <b>#{a.item_id}</b> {title}
              </span>
            </Menu.Item>
          );
        })}
        <Menu.Divider />
        {/* The chip used to BE this jump, so it stays on offer — the list answers
            "what is running", Orchestration is still where you act on all of it. */}
        <Menu.Item leftSection={<Ti name="layout-grid" size={14} />} onClick={onOpenFloor}>
          Open Orchestration
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}

export function Shell() {
  const [utility, setUtility] = useState<UtilityName | null>(null);
  const [screen, setScreen] = useState<string>(() => {
    if (new URLSearchParams(location.search).has("setting")) return "settings";
    const linked = urlParam("screen");
    if (linked && byScreen(linked)) return linked;
    try { return localStorage.getItem(KEY) || "floor"; } catch { return "floor"; }
  });
  useEffect(() => onUtility(setUtility), []);
  const visited = useRef<string[]>([screen]);
  const visitIndex = useRef(0);
  const replaying = useRef(false);
  const [, redrawHistory] = useState(0);
  useEffect(() => {
    if (replaying.current) { replaying.current = false; redrawHistory((v) => v + 1); return; }
    if (visited.current[visitIndex.current] === screen) return;
    visited.current = [...visited.current.slice(0, visitIndex.current + 1), screen];
    visitIndex.current = visited.current.length - 1;
    redrawHistory((v) => v + 1);
  }, [screen]);
  const moveHistory = (delta: number) => {
    const next = visitIndex.current + delta;
    if (next < 0 || next >= visited.current.length) return;
    visitIndex.current = next; replaying.current = true; setScreen(visited.current[next]); setSelection(null);
  };
  /* The screen column collapses. On a laptop it is 176px of labels you already
     know by heart, and the rail beside it still names every area — so the
     labels are worth having open while you learn the app and worth reclaiming
     once you have. The choice sticks per browser. */
  const [navOpen, setNavOpen] = useState(() => {
    try { return localStorage.getItem(NAV_KEY) !== "0"; } catch { return true; }
  });
  useEffect(() => {
    try { localStorage.setItem(NAV_KEY, navOpen ? "1" : "0"); } catch { /* private mode */ }
  }, [navOpen]);
  /* THE COLUMN KEEPS THE AREA YOU WERE IN. Settings belongs to no area, and
     deriving the column straight from the screen threw you into Command the
     moment you opened it — you came back from the settings screen to a nav
     column listing something else than the one you left. */
  const [areaId, setAreaId] = useState(() => areaOf(screen)?.id || AREAS[0].id);
  useEffect(() => {
    const owner = areaOf(screen);
    if (owner) setAreaId(owner.id);
  }, [screen]);
  const area = AREAS.find((a) => a.id === areaId) || AREAS[0];
  /* A SCREEN WITH NO AREA GETS NO SCREEN COLUMN. Settings belongs to none, and
     keeping the last area's list beside it meant opening Settings put the
     COMMAND strip — Orchestration, Brainstorm, Work history — next to a page that has
     nothing to do with any of them. The column is about the area you are in;
     when you are not in one, it goes away rather than showing a stale one. The
     collapse preference is untouched, so leaving Settings restores it. */
  const loose = !areaOf(screen);
  /* The last screen that HAD a column, so the mark can return there from a
     loose screen instead of being inert. */
  const lastArea = useRef<string>("overview");
  useEffect(() => { if (!loose) lastArea.current = screen; }, [screen, loose]);
  const meta = byScreen(screen);
  const state = useAppState();
  const floor = useFloor(6000, true);   // the counters in the rail, always live

  // Show the deck this screen owns, through the shell's own switch. On mount
  // too: the restored screen has to reach setWorkspace or the page opens on
  // whatever deck index.html marked active in its HTML.
  useEffect(() => {
    const deck = byScreen(screen)?.deck;
    // The floor island reads this to know which of its four questions it is
    // being asked; the deck switch below is what puts it on screen.
    publishScreen(screen);
    if (deck) window.setWorkspace?.(deck);
    try { localStorage.setItem(KEY, screen); } catch { /* private mode */ }
    setUrlParams({ screen, setting: screen === "settings" ? urlParam("setting") : null });
  }, [screen]);

  // Follow a deck change this shell did not make.
  useEffect(() => {
    const onDeck = (e: Event) => {
      const deck = (e as CustomEvent<{ name?: string }>).detail?.name;
      if (!deck) return;
      setScreen((cur) => (byScreen(cur)?.deck === deck
        ? cur
        : AREAS.flatMap((a) => a.screens).find((s) => s.deck === deck)?.id || cur));
    };
    window.addEventListener("bgate:workspace", onDeck);
    return () => window.removeEventListener("bgate:workspace", onDeck);
  }, []);

  const counts: Record<string, number> = {
    queued: floor.counts.queued, history: floor.counts.history,
    playtests: (state.sessions || []).length,
    assets: state.asset_groups.length,
  };
  const assetScreens = new Set(["assets", "spriteedit", "audiolab", "modeledit", "atlas"]);
  const drift = (state.verify.counts?.modified || 0) + (state.verify.counts?.missing || 0) + (state.verify.counts?.pending || 0);
  const review = state.asset_groups.reduce((n, g) => n + (g.candidates?.length || 0), 0);
  const readinessIssues = Number(!state.project) + Number(!state.asset_groups.length) + Number(drift > 0)
    + Number(review > 0) + Number(!state.controls?.length) + Number(!state.sessions.length);

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (utility === "command") setUtility(null); else claimUtility("command");
      }
      if (e.altKey && e.key === "ArrowLeft") { e.preventDefault(); moveHistory(-1); }
      if (e.altKey && e.key === "ArrowRight") { e.preventDefault(); moveHistory(1); }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [utility]);

  return (
    <div className={navOpen && !loose ? "bg4" : "bg4 nav-closed"}>
      <nav className="bg4-rail" aria-label="Areas">
        {/* THE MARK IS THE TRIGGER. A chevron in the nav header and a second
            chevron in the rail foot were two controls for one idea, and neither
            was where your eye already goes. The logo is the fixed point of the
            whole shell — it is in the same place whether the column is open or
            shut — so it is the one thing that can toggle it without moving. */}
        {/* ON A LOOSE SCREEN THE TOGGLE DID NOTHING. `loose` is true for any
            screen with no area — Settings is the one you reach from the rail
            — and it forces the column shut regardless of navOpen. So clicking
            the mark on Settings flipped a state variable and changed nothing
            on screen, which reads as a broken button rather than as a rule.
            There it goes BACK to the last screen that has a column, and opens
            it: the control always does the visible thing it looks like it
            does. */}
        <button className="bg4-mark"
                onClick={() => {
                  if (loose) { setScreen(lastArea.current); setNavOpen(true); }
                  else setNavOpen((v) => !v);
                }}
                aria-expanded={navOpen && !loose} aria-controls="bg4-nav"
                title={loose ? "Back to the screen list"
                       : navOpen ? "Collapse the screen list" : "Show the screen list"}
                aria-label={loose ? "Back to the screen list"
                            : navOpen ? "Collapse the screen list" : "Show the screen list"}
                dangerouslySetInnerHTML={{ __html: window.BGIcon?.logo?.({ size: 20 }) || "" }} />
        {AREAS.map((a) => (
          /* CURRENT means "the screen you are on lives here", not "this is the
             one you last clicked". Those diverge the moment anything else moves
             the screen — the vault chip, first-run, a deck event — and the rail
             then points at the wrong area while the column beside it is right. */
          <AreaButton key={a.id} area={a} collapsed={!navOpen || loose} counts={counts}
                      screen={screen}
                      current={a.screens.some((s) => s.id === screen)}
                      onPick={(id) => { setScreen(id); setAreaId(a.id); setSelection(null); }} />
        ))}
        <div className="bg4-railfoot">
          {/* THE ONLY OFFLINE SIGNAL WAS IN THE NAV FOOTER, which is the part
              that goes to zero width when you collapse. A dashboard that has
              lost its server must say so in the chrome that is always there. */}
          <span className={`bg4-dot ${state.project ? "ok" : "bad"}`}
                title={state.project
                  ? `${state.project.name} · connected`
                  : "no project — the dashboard is not talking to a server"} />
          {/* Switching project lives on the RAIL, next to the theme switch,
              because the nav column beside it collapses to zero width and took
              the only project control with it. */}
          <ProjectSwitch name={state.project?.name || "no project"}
                         connected={!!state.project} />
          <Grounds />
          <button className="bg4-area" title="Settings" aria-label="Settings"
                  aria-current={screen === "settings"}
                  onClick={() => setScreen("settings")}>
            <Ti name="settings" size={19} />
          </button>
        </div>
      </nav>

      <div className="bg4-nav" id="bg4-nav" aria-hidden={!navOpen}>
        <div className="bg4-navhead">
          <span className="bg4-eyebrow">{area.label}</span>
        </div>
        {area.screens.map((s) => (
          <button key={s.id} className="bg4-navitem" aria-current={s.id === screen}
                  onClick={() => { setScreen(s.id); setSelection(null); }}>
            <Ti name={s.icon} color={s.accent} />
            {s.label}
            {s.count && counts[s.count] > 0 && <span className="n">{counts[s.count]}</span>}
          </button>
        ))}
      </div>

      <header className="bg4-head">
        <div className="bg4-history">
          <button disabled={visitIndex.current <= 0} onClick={() => moveHistory(-1)} aria-label="Previous workspace"><Ti name="chevron-left" size={15} /></button>
          <button disabled={visitIndex.current >= visited.current.length - 1} onClick={() => moveHistory(1)} aria-label="Next workspace"><Ti name="chevron-right" size={15} /></button>
        </div>
        <div className="bg4-breadcrumb"><span>{areaOf(screen)?.label || "App"}</span><i>/</i><b>{meta?.label || "Builders Gate"}</b></div>
        <span className="bg4-note">{SCREEN_NOTE[screen] || ""}</span>
        <div className="bg4-chips">
          {assetScreens.has(screen) && <button className="bg4-chip bg4-assets-open" onClick={() => claimUtility("assets")}>
            <Ti name="stack-2" size={14} />Browse assets
          </button>}
          <button className={`bg4-chip bg4-readiness ${!state.hydrated ? "loading" : readinessIssues ? "needs" : "ready"}`}
                  aria-busy={!state.hydrated} onClick={() => claimUtility("readiness")}>
            <Ti name={!state.hydrated ? "loader-2" : readinessIssues ? "clipboard-check" : "circle-check"} size={14} />
            {!state.hydrated ? "Checking project" : readinessIssues ? `${readinessIssues} setup gaps` : "Project ready"}
          </button>
          <button className="bg4-find" onClick={() => claimUtility("command")}
                  aria-label="Find screens, work, and assets">
            <Ti name="search" size={14} /><span>Find</span><kbd>Ctrl K</kbd>
          </button>
          <RunningChip floor={floor}
                       /* "agents", NOT "floor". There is no screen with the id
                          `floor` — it is a DECK name, used by Work history.
                          setScreen() with an id that is not in nav.ts matches
                          nothing, so "Open Orchestration" changed a state
                          variable and moved nowhere: the menu closed and the
                          page stayed exactly where it was. Orchestration's
                          screen id is `agents`. */
                       onOpenFloor={() => setScreen("agents")} />
          {/* The bell renders its own #nt-host: streamer.js still anchors
              its chip beside that id. */}
          <Bell />
        </div>
      </header>

      <Inspector />
      <CommandPalette opened={utility === "command"} onClose={() => setUtility(null)}
                      onScreen={(id) => { setScreen(id); setSelection(null); }} floor={floor} />
      <AssetBrowser opened={utility === "assets"} onClose={() => setUtility(null)}
                    onScreen={(id) => { setScreen(id); setSelection(null); }} />
      <Readiness opened={utility === "readiness"} onClose={() => setUtility(null)}
                 onScreen={(id) => { setScreen(id); setSelection(null); }} />
    </div>
  );
}
