/* The navigation table — four areas, and the screens inside each.
 *
 * WHERE A SCREEN LIVES IS THE POINT OF THE REDESIGN. The rail this replaces
 * carried eleven flat destinations in three unlabelled runs; nothing said
 * whether Atlas was a thing you looked at or a thing you edited, so you had to
 * have learned it. Areas answer "what kind of work", the second column answers
 * "which screen", and both stay on screen while you work.
 *
 * `deck` is the id of the existing `.deck-view` a screen shows. EVERY EXISTING
 * VIEW KEEPS ITS OWN SECTION AND ITS OWN MODULE — this shell replaces the
 * chrome, not the fifteen views inside it, and setWorkspace() is still what
 * switches them. `floor` and `board` are the two genuinely new screens; they
 * are decks too, so nothing in the shell has to special-case them.
 *
 * `filter` rides along for the two floor screens, which are one screen asked
 * two questions: everything, and only what has finished. Live and Needs-you
 * were screens of their own and are not: a running row already reads as
 * running and a row that wants you already says so, so they were the same
 * stream a third and fourth time behind an extra click.
 */

import { moduleOff } from "../bridge";

export type ScreenId = string;

export type Screen = {
  id: ScreenId;
  label: string;
  icon: string;                 // tabler class suffix, e.g. "timeline-event"
  deck: string;                 // the .deck-view id this screen shows
  filter?: "all" | "done";
  accent?: string;              // CSS colour for the icon, when it means something
  count?: "queued" | "playtests" | "assets" | "history";
};

export type Area = { id: string; label: string; icon: string; screens: Screen[] };

const RAW_AREAS: Area[] = [
  {
    id: "command", label: "Command", icon: "layout-dashboard",
    screens: [
      /* EVERYTHING AND BOARD ARE GONE, by the owner's call. Both were a list of
         the same work items under a different sort — the console already shows
         what is running and Work history shows what finished, so the two of
         them were a third and fourth reading of one stream. `floor` survives as
         the deck Work history draws in; nothing renders `board`. */
      /* "ORCHESTRATION", NOT "AGENTS". The screen is not a list of agents — it
         is where you say what you want, the director answers, and work is
         split across seats and dispatched. "Agents" named the actors; this
         names the act, which is the thing the screen is for. The id and the
         deck stay `agents`: they are the key in localStorage, in
         setWorkspace(), in SeatShell.select() and in the classic index.html,
         and renaming a label is not a reason to break a restored session. */
      /* OVERVIEW CAME BACK, and it is first because it is the only screen that
         answers "what is the state of the studio" without you having to pick a
         reading of it first. The island and its deck host never went anywhere —
         frontend/src/views/Overview.tsx is still mounted into #view-overview by
         main.tsx — it simply had no entry in this list, so there was no way to
         reach it and the app opened on the console with no way back to a
         summary. */
      { id: "overview", label: "Overview", icon: "layout-dashboard", deck: "overview" },
      { id: "agents",  label: "Orchestration", icon: "message-2",      deck: "agents" },
      /* THE ROOM IS A SCREEN, NOT A TAB ON A TEXT FIELD. Dispatch and
         brainstorm have different rules, different costs and different exits —
         one files work and spends on agents, the other files nothing until you
         open its single door. A segmented control inside a composer could not
         carry that difference; swapping the whole room can. */
      { id: "brainstorm", label: "Brainstorm", icon: "bulb",           deck: "brainstorm" },
      { id: "history", label: "Work history", icon: "history",         deck: "floor", filter: "done",
        count: "history" },
    ],
  },
  {
    id: "build", label: "Build", icon: "hammer",
    screens: [
      { id: "playtests", label: "Playtests",       icon: "device-gamepad-2", deck: "playtests",
        count: "playtests" },
      { id: "studio",    label: "Studio",          icon: "topology-star-3",  deck: "studio" },
      { id: "seats",     label: "Seat workspaces", icon: "users",            deck: "seats" },
    ],
  },
  {
    id: "edit", label: "Edit", icon: "brush",
    screens: [
      { id: "spriteedit", label: "Sprite editor", icon: "brush",     deck: "spriteedit" },
      { id: "audiolab",   label: "Audio lab",     icon: "wave-sine", deck: "audiolab" },
      { id: "modeledit",  label: "3D viewer",     icon: "box",       deck: "modeledit" },
    ],
  },
  {
    id: "library", label: "Library", icon: "stack-2",
    screens: [
      { id: "assets", label: "Assets",      icon: "stack-2",     deck: "assets", count: "assets" },
      { id: "atlas",  label: "Atlas",       icon: "layout-grid", deck: "atlas" },
      { id: "world",  label: "World bible", icon: "book-2",      deck: "world" },
    ],
  },
];

/** What the header says about each screen. The note is the screen's own answer
 *  to "how much of what am I looking at" — filled in with live counts by the
 *  shell where it can. */
/* SCREENS FOLLOW THE PROJECT'S MODULE CHOICES. A screen whose module is
   switched off (Settings > Modules, or the first-run checklist) leaves the
   rail entirely — a tab for a feature the project declined is the bloat the
   switch exists to remove. Filtered once at module init: the bootstrap shim
   in <head> runs before this bundle, so the answer is already on window. */
const SCREEN_MODULE: Record<string, string> = {
  brainstorm: "brainstorm",
  playtests: "playtest",
};

export const AREAS: Area[] = RAW_AREAS
  .map((a) => ({
    ...a,
    screens: a.screens.filter((s) => {
      const m = SCREEN_MODULE[s.id];
      return !m || !moduleOff(m);
    }),
  }))
  .filter((a) => a.screens.length > 0);

export const SCREEN_NOTE: Record<string, string> = {
  floor: "everything, newest first",
  history: "finished work",
  board: "delegation, by seat",
  agents: "the director's console",
  brainstorm: "the cheap room — files nothing until you say so",
  settings: "every setting, and which layer set it",
  assets: "families, files and the review queue",
  playtests: "recorded sessions",
  studio: "node editors",
  seats: "one workspace per craft",
  atlas: "scenes, scripts and the build",
  world: "the bible and the canon",
  spriteedit: "pixels and rigs",
  audiolab: "sound and music",
  modeledit: "models and animation",
};

export const SEAT_COLOR: Record<string, string> = {
  director: "#e8c05a", narrative: "#b98cf0", gameplay: "#ff6a4a", tech: "#5aa8ff",
  art: "#ff85bd", audio: "#43d6a5", cinematic: "#ffb347", qa: "#a3e055",
};

export const SEAT_ICON: Record<string, string> = {
  director: "user-star", narrative: "book-2", gameplay: "device-gamepad-2",
  tech: "code", art: "palette", audio: "wave-sine", cinematic: "movie",
  qa: "shield-check",
};

/* SETTINGS IS A SCREEN WITH NO AREA. It hangs off the rail's foot because an
   area containing exactly one screen is a column that exists to hold one row.
   It still has to be a Screen: byScreen() is what gives the header its title
   and what tells the shell which deck to show, so while settings was missing
   from this table the settings screen was titled "Builders Gate" and a reload
   with settings remembered opened whatever deck index.html had marked active. */
export const LOOSE: Screen[] = [
  { id: "settings", label: "Settings", icon: "settings", deck: "settings" },
];

export const byScreen = (id: string): Screen | undefined => {
  for (const a of AREAS) for (const s of a.screens) if (s.id === id) return s;
  return LOOSE.find((s) => s.id === id);
};

export const areaOf = (screenId: string): Area | undefined =>
  AREAS.find((a) => a.screens.some((s) => s.id === screenId));
