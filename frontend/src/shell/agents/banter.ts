import { useEffect, useRef, useState } from "react";
import { type ConsoleState } from "./api";
import { type Occupant } from "./occupancy";

/* WHAT THE LOUNGE SAYS WHEN THERE IS NOTHING TO SAY.
 *
 * THIS REPO HAS SHIPPED A LINE ROTATOR BEFORE AND IT WAS THE WRONG ONE. Read
 * the docstring at the top of Cat.tsx: the classic console gave the mascot
 * canned quotes on a timer, and the most animated thing on the page became the
 * one element guaranteed to be saying nothing true. The cat was then fed the
 * director's real voice and put the same sentence on screen twice, six lines
 * apart, with the copy in the louder box. Both failures are the same failure:
 * words that look like an agent talking, while an agent is talking somewhere
 * else.
 *
 * So this is allowed to exist under four conditions, and each one is a wall
 * against one of those failures.
 *
 *   1. IT ONLY SPEAKS INTO SILENCE. `floorIsQuiet` below is the gate, and it is
 *      deliberately strict: every seat idle, nothing running, queued,
 *      dispatched or in review, no gate open, no question open, the director
 *      not mid-reply, nothing on the handover desk. The moment any of that
 *      stops being true the words stop mid-rotation. A room with real work in
 *      it never has to compete with a joke, which is the whole objection to a
 *      canned rotator and it is answered by never overlapping with the thing
 *      being talked over.
 *   2. IT IS NOT SPEECH AND IT IS NOT ATTACHED TO ANYBODY. No bubble, no tail,
 *      no seat colour, no character. It is a caption on the lounge, tagged
 *      `overheard`, in an italic that appears nowhere else in this shell. A
 *      seat's actual output belongs in the transcript and in the handover note,
 *      and nothing here may be mistaken for either.
 *   3. IT IS DATA, NEVER A MODEL CALL. The pool below is the whole source. No
 *      token is spent on it, nothing is fetched for it, and a floor left open
 *      overnight on a dead network keeps working exactly as well.
 *   4. IT CAN BE TURNED OFF, and the off switch survives a reload. Somebody who
 *      does not want a joke in their studio gets a studio with no jokes.
 *
 * VOICE: office sitcom cold open. Dry, workplace, and the joke is usually
 * somebody being slightly wrong about something small. These are studio staff
 * on a break, so nothing here refers to agents, models, prompts or the app
 * itself. A line that could only be said by software is the exact tell that
 * gave the old rotator away.
 */

/** The pool. Plain data, kept flat and in no order that means anything - the
 *  rotation shuffles it, so a line's neighbours here are not its neighbours on
 *  screen. Add to it freely; the only rule is the voice note above. */
export const BANTER: readonly string[] = [
  "Somebody labelled the milk do not drink, and then drank it.",
  "The good chair is at the wrong desk again.",
  "The printer says it is out of magenta. We have never printed in colour.",
  "I called him Dave for two years. His name is Dan. He answered every time.",
  "The meeting room is booked until 2040 by an invite nobody can delete.",
  "New rule went up on the whiteboard. The rule is no new rules.",
  "Half this floor thinks the plant is real. It is not. It is also dying.",
  "There is a second kitchen upstairs. I found out last week.",
  "Someone reheated fish. There is now a meeting about it.",
  "The clock in here is eleven minutes fast. Nobody fixed it. Everybody adjusted.",
  "I asked for the file and got a folder with one file in it called final.",
  "The lift announces the floor after you have already arrived at it.",
  "Coffee machine has a setting called strong. It is the old setting with a light on it.",
  "That poster has been crooked since March. I have decided it is load bearing.",
  "The badge reader works on Tuesdays. Nobody has explained Tuesdays.",
  "We have a suggestion box. It is locked. The key went in the suggestion box.",
  "He has been saying he does not need a chair for six months. Standing.",
  "The wifi is faster in that corner. It is the same wifi.",
  "Someone brought a cake in and would not say what for. We ate it anyway.",
  "There is a stapler with a name on it and the name is not any of ours.",
  "Facilities confirmed the thermostat is a decoy. It is still on the wall.",
  "Somebody alphabetised the shelf and filed The Manual under T.",
  "I have never once seen the person whose mug that is.",
  "The fire exit map shows you exiting into another fire exit map.",
  "New whiteboard markers arrived. They are all one colour and that colour is grey.",
  "A phone alarm goes off at ten past three every day. He leaves at two.",
  "The window opens two inches for safety. We are on the ground floor.",
  "Two people have claimed that desk and neither one has sat at it.",
  "There is a box labelled cables. It has one cable in it and a shoe.",
  "The chair with the broken lever is the one everybody fights over.",
  "New badge photos went up and two of them are the same person.",
  "Reception got a bell. It is louder than the fire alarm.",
  "He says the recycling is symbolic. He still separates it.",
  "Someone has been signing the cards from the team. There is no team called the team.",
  "The lights go off if you sit still long enough. We have all learned to wave.",
  "There is a second staircase. It goes to the same place, slower.",
  "The good pens are in a drawer and the drawer is locked.",
  "Nobody has claimed the umbrella, so it has been claimed by everybody.",
  "There is a jar in the fridge with a date on it. The date is a year.",
  "The sign says reserved. It does not say for whom, so it is reserved.",
  "Somebody set the shared machine's wallpaper to a photo of this room.",
  "The paper towels were a temporary supplier. That was three years ago.",
  "Someone has been watering the fake plant and honestly it looks great.",
  "The office quiz has a champion nobody remembers competing against.",
  "We were told the tape on the floor was temporary. It has a scuff pattern now.",
  "I have been taking the long way round to avoid a door that was fixed in June.",
];

/** One line, and not the one before it, and not one this cycle has already
 *  used. A pool that repeats is a pool that stops being read - the old rotator
 *  died of exactly that, on a shorter list and a faster timer.
 *
 *  A drawn-down shuffled deck rather than a random pick: random repeats, and a
 *  repeat two lines apart is what somebody notices first. The one join a deck
 *  cannot cover on its own is the seam between cycles, where the last card of
 *  one shuffle can be the first of the next, so a fresh deck ending on the line
 *  just spoken swaps that card to the other end. */
export function createRotator(pool: readonly string[] = BANTER,
                              rand: () => number = Math.random) {
  /* Drawn from the END with pop(), so the deck shrinks without reindexing. */
  let deck: string[] = [];
  let last = "";
  return function next(): string {
    if (!pool.length) return "";
    if (!deck.length) {
      deck = [...pool];
      for (let i = deck.length - 1; i > 0; i -= 1) {
        const j = Math.floor(rand() * (i + 1));
        [deck[i], deck[j]] = [deck[j], deck[i]];
      }
      const top = deck.length - 1;
      if (deck.length > 1 && deck[top] === last) {
        [deck[top], deck[0]] = [deck[0], deck[top]];
      }
    }
    last = deck.pop() as string;
    return last;
  };
}

/** IS ANYTHING REAL HAPPENING. Every clause here is a reason to shut up, and
 *  they are separate clauses rather than one count because they fail
 *  separately: a gate open with an empty board is still a studio waiting on a
 *  person, and a seat marked failed is still a room with a problem in it.
 *
 *  `people` is the placement occupancy.ts produced, not the raw roster - it is
 *  already the answer to "where is everybody", and re-deriving it here would be
 *  a second opinion that could disagree with the picture on screen.
 *
 *  Read the counts as `>` rather than truthiness of an object: `floor` is
 *  optional on the type and an older server that never sent it must not read as
 *  a busy floor OR as a quiet one on its own. The occupants carry that. */
export function floorIsQuiet(
  state: ConsoleState,
  people: Iterable<Occupant>,
  /** Handover notes still on the Director's desk. Something landed in the last
   *  few seconds and is being read; that is news, and news outranks a joke. */
  notesOnDesk = 0,
): boolean {
  /* A PAYLOAD NOBODY ANSWERED FOR IS NOT A QUIET STUDIO. When the read fails
     the state beside this flag is whatever was last known, and the one thing we
     do NOT know is whether anything is running. Joking through a backend outage
     is the loudest possible version of this file's original mistake. */
  if (state.__error) return false;
  if (notesOnDesk > 0) return false;
  for (const p of people) if (p.state !== "idle") return false;
  if ((state.questions || []).length) return false;
  /* EVERY gate, not only the blocking ones. A plain qa-gate does not want a
     human, but it is an agent reviewing an agent, which is work in flight. The
     door test is about who is needed; this one is about whether the building is
     empty. */
  if ((state.gates || []).length) return false;
  /* The director mid-sentence. The transcript is where words go, and two
     sources of words on one screen is the failure this file is built around. */
  if ((state.turns || []).some((t) => t.reply?.running)) return false;
  const f = state.floor || {};
  return !(f.running || f.queued || f.dispatched || f.review);
}

/** Two polls of quiet before anybody says anything. A studio that has just gone
 *  still is not yet a studio on a break, and a line that lands on the same
 *  frame as the last item closing reads as a comment on that item. */
const SETTLE_MS = 6500;

/** How long a line stays up. Slower than it wants to be: this is peripheral
 *  text in a pane somebody is using for something else, and anything quicker
 *  pulls the eye off the floor it is meant to be watching. */
const ROTATE_MS = 15000;

/** The current line, or "" when the lounge should be silent.
 *
 *  THE SILENCE IS IMMEDIATE AND THE SPEECH IS NOT. Work starting clears the
 *  line on the same render, with no fade and no finishing the sentence, because
 *  the one promise this thing makes is that it is never on screen at the same
 *  time as something real. Coming back the other way it waits. */
export function useBanter(quiet: boolean, on: boolean,
                          settle = SETTLE_MS, rotate = ROTATE_MS): string {
  const [line, setLine] = useState("");
  /* One rotator for the life of the pane, so muting and unmuting does not hand
     back a fresh deck and let a line repeat inside its own cycle. */
  const next = useRef<(() => string) | null>(null);
  if (!next.current) next.current = createRotator();

  useEffect(() => {
    if (!quiet || !on) { setLine(""); return; }
    /* The interval is armed INSIDE the settle timeout rather than beside it.
       Running both from the same instant would space every line after the first
       by settle + rotate, which is not the gap this file documents and is the
       kind of drift nobody spots because it only reads as "a bit slow". */
    let every = 0;
    const first = window.setTimeout(() => {
      setLine(next.current!());
      every = window.setInterval(() => setLine(next.current!()), rotate);
    }, settle);
    return () => { window.clearTimeout(first); window.clearInterval(every); };
  }, [quiet, on, settle, rotate]);

  return line;
}

const KEY = "bgate-floor-banter";

/** Off is remembered, on is the default. Stored as a flag rather than in the
 *  pane's state because the pane is remounted by every screen switch, and a
 *  mute that lasts until you look at the board is not a mute. */
export function banterOnStored(): boolean {
  try { return localStorage.getItem(KEY) !== "0"; } catch { return true; }
}

export function storeBanterOn(on: boolean) {
  try { localStorage.setItem(KEY, on ? "1" : "0"); } catch { /* private mode */ }
}
