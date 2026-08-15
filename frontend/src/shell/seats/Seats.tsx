import { useCallback, useEffect, useRef, useState } from "react";
import { Ti } from "../Ti";
import { SEAT_ICON } from "../nav";
import { useViewActive } from "../../hooks";
import { useJSON, useSeats, type QueueItem, type Seat as SeatRow } from "./api";
import type { SeatBodyProps, SeatTab } from "./types";
import { ReadError } from "./prims";
import { Director } from "./Director";
import { Narrative } from "./Narrative";
import { Gameplay } from "./Gameplay";
import { Tech } from "./Tech";
import { Art } from "./Art";
import { Audio } from "./Audio";
import { Cinematic } from "./Cinematic";
import { Qa } from "./Qa";
import { Persona } from "./Persona";
import { Work } from "./Work";
import { SeatBrainstorm } from "./Brainstorm";
import { Generate } from "./Generate";
import { ChipSink, type Chip } from "./chips";
import "./seats.css";

/* 11b · Seat workspaces — one workspace per CRAFT, not one lane per name.
 *
 * THE COMPLAINT THIS ANSWERS is the one written into the classic seat core:
 * "seat workspaces is still a mess … it's all just square components w basic
 * info". The reason was structural. Every seat got the same furniture — a work
 * list, a couple of cards, a log tail — and the furniture said nothing about
 * what that seat DOES. A director arbitrates and refuses; an art seat pins a
 * reference and measures against it; a cinematic seat spends money per frame.
 * Those are four different rooms, and drawing them as one room with a different
 * heading is why nobody could tell what to do first.
 *
 * So each seat below owns its own body and shares only the grammar (see
 * prims.tsx). The header is the same across seats because the header is about
 * IDENTITY, not craft: whose seat, what its mission says, which paths it may
 * write, what is currently on its desk.
 *
 * THE MISSION IS READ FROM THE PROJECT, NOT COPIED INTO THIS FILE. /api/state
 * carries each seat's mission and write globs out of bgate_core/seats.py, and a
 * project may customise both. A hard-coded mission would be a UI that quietly
 * disagrees with the brief the agent holding the seat was actually given.
 *
 * ONE POLL PER VISIBLE PANEL. Everything here is gated on `active` — the seats
 * deck being on screen AND this seat being the selected one — so seven of the
 * eight workspaces cost nothing while you read the eighth.
 */

const BODIES: Record<string, (p: SeatBodyProps) => React.JSX.Element> = {
  director: Director, narrative: Narrative, gameplay: Gameplay, tech: Tech,
  art: Art, audio: Audio, cinematic: Cinematic, qa: Qa,
};

/* Tabs are the seat's STAGES, ordered by the work. Only tabs whose panel has
   something real behind it exist — a tab that opens onto a permanently empty
   panel teaches people not to click tabs. */
const TABS: Record<string, SeatTab[]> = {
  director: [
    { id: "decisions", label: "Decisions", icon: "gavel", hint: "what is settled, what is refused, what is waiting on you" },
    /* THE ROOM IS A TAB, per the reference. It was cut once on the argument that
       a room in a seat and a room on its own screen are two entrances to one
       conversation — but the two entrances are not equivalent. The room reached
       from the seat opens ALREADY SCOPED to that seat (Brainstorm.mount takes
       `seat`), and it is the cheap place where a decision is argued before it is
       filed one tab to the left. Cutting it moved the argument off the screen
       where its conclusion gets written down. Only director and narrative get
       it, because they are the only two seats the backend lets open a room. */
    { id: "room", label: "Brainstorm", icon: "bulb", hint: "the cheap room — nothing here reaches the board until you file" },
    { id: "pillars", label: "Pillars", icon: "columns-3", hint: "the bible's pillars and core loop" },
    { id: "work", label: "Queue", icon: "list-check" },
  ],
  narrative: [
    { id: "lore", label: "Lore", icon: "book-2", hint: "entities, their prose and their checkable facts" },
    { id: "room", label: "Brainstorm", icon: "bulb", hint: "the cheap room — nothing here reaches the board until you file" },
    { id: "dialogue", label: "Dialogue", icon: "messages", hint: "the graph is the editor" },
    { id: "quests", label: "Quests", icon: "flag", hint: "what the lore is actually asking the player to do" },
    { id: "work", label: "Queue", icon: "list-check" },
  ],
  gameplay: [
    { id: "tunables", label: "Tunables", icon: "adjustments", hint: "the knob and what was measured about it" },
    { id: "feedback", label: "Feedback", icon: "message-report", hint: "what players said, with the session's numbers" },
    { id: "input", label: "Input", icon: "keyboard", hint: "what the build actually binds" },
    { id: "systems", label: "Systems", icon: "topology-star-3", hint: "what the rules are made of" },
    { id: "work", label: "Queue", icon: "list-check" },
  ],
  tech: [
    { id: "project", label: "Project", icon: "checkup-list", hint: "engine, build, and does it still compile" },
    { id: "generators", label: "Generators", icon: "tool", hint: "ships --check, defaults to dry" },
    { id: "build", label: "Build", icon: "package", hint: "what is exported, and whether it is older than the source" },
    { id: "work", label: "Queue", icon: "list-check" },
  ],
  art: [
    { id: "sheets", label: "Sheets", icon: "photo", hint: "the sheet in progress and its measurement" },
    { id: "generate", label: "Generate", icon: "wand", hint: "this craft's generation workflows — board, generate, gate, install" },
    { id: "locks", label: "Locks", icon: "lock", hint: "binaries do not merge" },
    { id: "work", label: "Queue", icon: "list-check" },
  ],
  audio: [
    { id: "hooks", label: "Hooks", icon: "plug", hint: "which game events actually play a sound" },
    { id: "generate", label: "Generate", icon: "wand", hint: "this craft's generation workflows — board, generate, gate, install" },
    { id: "library", label: "Library", icon: "music", hint: "files on disk, and whether anything asks for them" },
    { id: "work", label: "Queue", icon: "list-check" },
  ],
  cinematic: [
    { id: "shots", label: "Shot list", icon: "list-numbers", hint: "board it, then write it, then buy a frame" },
    { id: "generate", label: "Generate", icon: "wand", hint: "this craft's generation workflows — board, generate, gate, install" },
    { id: "storyboard", label: "Storyboard", icon: "layout-grid", hint: "board it before you buy a frame" },
    { id: "cut", label: "Cut", icon: "scissors", hint: "what is kept, installed and playable" },
    { id: "work", label: "Queue", icon: "list-check" },
  ],
  qa: [
    { id: "gate", label: "Gate runs", icon: "shield-check", hint: "a run with no verdict line decided nothing" },
    { id: "contract", label: "Contract", icon: "file-code-2", hint: "the only addressable properties" },
    { id: "tests", label: "Tests", icon: "test-pipe", hint: "what the engine itself was asked to prove" },
    { id: "work", label: "Queue", icon: "list-check" },
  ],
};

/* EVERY SEAT GETS THIS ONE, appended rather than typed into eight lists - it is
   the same panel for all of them because it is about IDENTITY, like the header,
   and nothing in it is craft-specific. It goes LAST: a seat's own work comes
   before what its carpet is made of. */
const PERSONA_TAB: SeatTab = {
  id: "persona", label: "Look", icon: "palette",
  hint: "how this seat looks on the studio floor",
};

function tabsFor(role: string): SeatTab[] {
  return [...(TABS[role] || []), PERSONA_TAB];
}

const LIVE = new Set(["dispatched", "running", "in_progress"]);
const OPEN = new Set(["queued", "ready", "review", "blocked", ...LIVE]);

/** The picked seat and tab survive a reload — a workspace you were reading is
 *  a place, and a rail click that dumps you back on the director every time is
 *  the behaviour the classic seat shell was rightly criticised for. */
const remember = (k: string, v: string) => {
  try { localStorage.setItem(`bgs-${k}`, v); } catch { /* private mode */ }
};
const recall = (k: string, fallback: string) => {
  try { return localStorage.getItem(`bgs-${k}`) || fallback; } catch { return fallback; }
};

declare global {
  interface Window {
    /* seats/_core.js still owns this object for BGWS and SeatStage; this screen
       only replaces its `select`. */
    SeatShell?: { select?(role: string): void } & Record<string, unknown>;
  }
}

export function Seats() {
  const host = useRef<HTMLDivElement>(null);
  const onScreen = useViewActive(host);
  const { seats, error } = useSeats(onScreen);
  const queue = useJSON<{ items?: QueueItem[] }>(
    "/api/queue?limit=200", { items: [] }, 5000, onScreen);

  const [pick, setPick] = useState(() => recall("seat", "director"));
  const [tabs, setTabs] = useState<Record<string, string>>({});
  /* Per-seat header chips, published upward by the seat bodies. Keyed by seat
     so an unmount clearing its own row cannot blank another's. */
  const [chips, setChips] = useState<Record<string, Chip[]>>({});
  const sink = useCallback((role: string, next: Chip[]) => {
    setChips((prev) => (prev[role] === next ? prev : { ...prev, [role]: next }));
  }, []);

  const known = seats.filter((s) => BODIES[s.role]);
  const seat: SeatRow | undefined =
    known.find((s) => s.role === pick) || known[0];
  const tabList = seat ? tabsFor(seat.role) : [];
  const tab = seat
    ? (tabs[seat.role] || recall(`tab-${seat.role}`, tabList[0]?.id || "")) : "";

  const items = queue.items || [];
  const openFor = (role: string) =>
    items.filter((i) => i.seat === role && OPEN.has(i.status)).length;
  const liveFor = (role: string) =>
    items.filter((i) => i.seat === role && LIVE.has(i.status)).length;

  const Body = seat ? BODIES[seat.role] : null;
  const mine = seat ? items.filter((i) => i.seat === seat.role) : [];

  function pickSeat(role: string) {
    setPick(role);
    remember("seat", role);
  }

  /* THE OLD SHELL'S ONE PUBLIC VERB, KEPT ALIVE.
     chatlive.js calls SeatShell.select("director") to jump the user to a seat,
     and seats/_core.js still defines it — but its DOM (#seat-subnav,
     #seat-body) is gone, so the call would land on nothing and the jump would
     silently do nothing. Re-pointing it here keeps that one caller working
     without _core.js knowing React exists. Registered while this screen is
     mounted and handed back on unmount, so the classic implementation is never
     permanently clobbered. */
  useEffect(() => {
    const prev = window.SeatShell;
    window.SeatShell = {
      ...(prev || {}),
      select: (role: string) => {
        window.setWorkspace?.("seats");
        if (BODIES[role]) pickSeat(role);
      },
    };
    return () => { window.SeatShell = prev; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  function pickTab(id: string) {
    if (!seat) return;
    setTabs({ ...tabs, [seat.role]: id });
    remember(`tab-${seat.role}`, id);
  }

  return (
    <div className="bgs" ref={host}>
      <div className="bgs-seats">
        <div className="bgs-seatslb">Seats</div>
        {!known.length && (
          <div className="bgs-railpad">
            <ReadError error={error} what="the seat table" />
            {!error && <span className="bgs-dim">no project open</span>}
          </div>
        )}
        {known.map((s) => {
          const on = seat?.role === s.role;
          const n = openFor(s.role);
          return (
            <button key={s.role}
                    className={`bgs-seat${on ? " on" : ""}${s.enabled === false ? " off" : ""}`}
                    style={on ? { boxShadow: `inset 2px 0 0 var(--c-${s.role})` } : undefined}
                    onClick={() => pickSeat(s.role)}
                    title={s.enabled === false ? "this seat is disabled for this project" : s.mission}>
              <Ti name={SEAT_ICON[s.role] || "user"} size={17} color={`var(--c-${s.role})`} />
              <span className="lb">{s.title}</span>
              {liveFor(s.role) > 0 && <span className="live" />}
              {n > 0 && <span className="n">{n}</span>}
            </button>
          );
        })}
      </div>

      <div className="bgs-col">
        {seat && (
          <>
            <div className="bgs-topbar">
              <Ti name={SEAT_ICON[seat.role] || "user"} size={17} color={`var(--c-${seat.role})`} />
              <span className="ttl">{seat.title}</span>
              <div className="bgs-tabs">
                {tabList.map((t) => (
                  <button key={t.id} className={`bgs-tab${t.id === tab ? " on" : ""}`}
                          style={t.id === tab ? { color: `var(--c-${seat.role})` } : undefined}
                          title={t.hint} onClick={() => pickTab(t.id)}>
                    <Ti name={t.icon} size={13} />{t.label}
                  </button>
                ))}
              </div>
              <span className="sp" />
              <div className="bgs-chipsrow">
                {/* THE SEAT'S OWN THREE FIGURES, published by its body — see
                    seats/chips.tsx. The shell cannot compute "18 findings" or
                    "-14 LUFS target"; the craft that already holds the read
                    can, and the version that could not drew the same four
                    generic counts on all eight headers. */}
                {(chips[seat.role] || []).map((c) => (
                  <span className="bgs-hchip" key={c.label}
                        style={c.color ? { color: c.color } : undefined}
                        title={c.title}>
                    <Ti name={c.icon} size={13} />{c.label}
                  </span>
                ))}
                {liveFor(seat.role) > 0 && (
                  <span className="bgs-hchip" style={{ color: `var(--c-${seat.role})` }}>
                    <Ti name="player-play" size={13} />{liveFor(seat.role)} running
                  </span>
                )}
                {openFor(seat.role) > 0 && (
                  <span className="bgs-hchip"><Ti name="list-check" size={13} />{openFor(seat.role)} open</span>
                )}
                {!!(seat.locks || []).length && (
                  <span className="bgs-hchip"><Ti name="lock" size={13} />{(seat.locks || []).length} locked</span>
                )}
                {!!seat.promoted_feedback && (
                  <span className="bgs-hchip"><Ti name="message-report" size={13} />{seat.promoted_feedback} promoted</span>
                )}
              </div>
            </div>

            <div className="bgs-mission">
              <Ti name="target" size={15} color="var(--text-3)" />
              <div className="m">{seat.mission}</div>
              <span className="sp" />
              <span className="globs">{(seat.write_globs || []).join(" · ")}</span>
            </div>

            <div className="bgs-body">
              <ChipSink.Provider value={sink}>
                {tab === "persona"
                  ? <Persona seat={seat.role} active={onScreen} />
                  : tab === "work"
                  ? <Work seat={seat.role} active={onScreen} items={mine} />
                  /* The room is intercepted here rather than inside Director and
                     Narrative: brainstorm.js owns every pixel under its host, so
                     a seat body that also mounted it would be two owners of one
                     subtree. Keying on the seat forces a remount when you switch
                     between the two seats that have a room — `mount` scopes to
                     the seat it was handed and would otherwise keep the first. */
                  : tab === "room"
                  ? <SeatBrainstorm key={seat.role} seat={seat} active={onScreen} tab={tab} />
                  /* Same interception, same reason: WF owns its host's
                     subtree, so the seat body must not also be mounted into
                     it. Keyed on the seat so switching art→audio re-opens the
                     library scoped to the seat you arrived at. */
                  : tab === "generate"
                  ? <Generate key={seat.role} seat={seat} active={onScreen} tab={tab} />
                  : Body && <Body seat={seat} active={onScreen} tab={tab} />}
              </ChipSink.Provider>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
