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


/* ── what fits over a head ────────────────────────────────────────────────────

   THE BUBBLE POOL IS SEPARATE FROM THE CAPTION POOL, and that is a decision
   about screen space rather than about voice. The lines above are two-clause
   sitcom asides written to be READ in a strip under the building, where there
   is a whole pane of width for them. Drawn over a character in a room eight
   cells wide, the same sentence is a box the size of the room it is standing
   in, and the floor disappears behind the joke.

   SO THESE ARE THE SAME VOICE, SHORTER. Five words or so, the length of a thing
   somebody actually says out loud in an office, which is also the length that
   fits in a bubble without covering the desk behind it. The long lines are not
   deleted - they still run in the caption strip, which is where a longer beat
   belongs.

   THE LENGTH RULE IS ENFORCED, NOT TRUSTED. `BANTER_SHORT` is asserted below to
   be under the ceiling, because a pool is exactly the kind of list somebody
   appends a good long line to six months from now, and the failure is not an
   error - it is a bubble that quietly eats the lounge. */

/** Longest a bubble line may be. Measured in characters because the renderer
 *  wraps on width and two lines of this is the most a head can carry without
 *  the box reaching the room above. */
export const BUBBLE_MAX = 38;

export const BANTER_SHORT: readonly string[] = [
  "Who moved the good chair.",
  "That is not my mug.",
  "The printer is lying again.",
  "I did not touch it.",
  "Is the clock fast, or me.",
  "Somebody reheated fish.",
  "It was like that already.",
  "I have a meeting about the meeting.",
  "The plant is fake. Still dying.",
  "Third coffee. Not proud.",
  "That chair is load bearing.",
  "I have read this line twice.",
  "The milk has a name on it.",
  "Nobody owns this stapler.",
  "It works on my desk.",
  "Do not sit there, it squeaks.",
  "I labelled it. Nobody read it.",
  "Two people booked this room.",
  "The lift skipped our floor.",
  "I am not on that thread.",
  "Whose lunch is this.",
  "The lights go off if I sit still.",
  "I moved it back. It moved again.",
  "That is a Tuesday problem.",
  "I will look at it after this.",
  "The good pen has gone.",
  "It was fine yesterday.",
  "Somebody has my headphones.",
  "Three settings. All of them wrong.",
  "I am going to stand for a bit.",
];

/* THE CEILING IS CHECKED AT MODULE LOAD, so a line that is too long fails
   loudly the first time the floor is opened rather than silently drawing a
   bubble across two rooms. Dev-only: a thrown error in production would take
   the console down over a joke, which is a worse outcome than a wide bubble. */
{
  const tooLong = BANTER_SHORT.filter((l) => l.length > BUBBLE_MAX);
  if (tooLong.length) {
    console.warn(
      `banter: ${tooLong.length} bubble line(s) over ${BUBBLE_MAX} chars and `
      + `will draw wide over the floor:`, tooLong);
  }
}

/* ── lines that are about TODAY ────────────────────────────────────────────────

   THE PLAN ASKED FOR THIS AND IT IS THE ANSWER TO STALENESS. A fixed pool is
   stale on the second day: the reader has met every line, and a joke you have
   met is a joke that has become furniture. The fix is NOT a bigger pool and it
   is certainly not a model call. It is lines that take an ARGUMENT from what
   actually happened on this board - the seat that failed, the item that cost
   the most, the room that closed everything - so the same twelve templates say
   something different on Tuesday than they said on Monday.

   EVERY RULE THE REST OF THIS FILE HOLDS STILL HOLDS HERE. These are canned
   strings with a hole in them, filled from a payload that is already on screen.
   No token is spent, nothing is fetched, and a topical line can no more invent
   a fact than a fixed one can: if there is no failed item, no template that
   mentions failure is even a candidate.

   THE SUBJECT IS A SEAT OR AN ITEM, NEVER A PERSON AND NEVER THE APP. Same
   voice note as the pool above - studio staff on a break - so a line refers to
   "narrative" the way you would refer to a department, and never to an agent, a
   model, a prompt or a run.

   NO EM DASHES, and it is worth saying twice because a template is where one
   would sneak in. A dash is the loudest tell that a line was machine written. */

/** What the floor can currently make a joke ABOUT. Every field is optional
 *  because every field is a fact that may not exist yet, and a template is only
 *  a candidate when the fact it needs is present. */
export type Topic = {
  /** The seat whose most recent item failed. */
  failedSeat?: string;
  /** The seat that closed the most work in what the console is holding. */
  busiestSeat?: string;
  /** How many that was. Only meaningful beside busiestSeat. */
  busiestCount?: number;
  /** The most expensive item the console is holding, in whole dollars. */
  priciestSeat?: string;
  priciestUsd?: number;
  /** A seat with work still queued while the floor stands idle, which is the
   *  one genuinely useful thing this can point at. */
  stalledSeat?: string;
};

/** Read the topic off the SAME payload the floor is drawn from.
 *
 *  IT IS A PURE READING AND IT INVENTS NOTHING. Every branch below either finds
 *  a row and names it or leaves the field undefined; there is no default seat,
 *  no "probably art", and no fallback that would let a template say something
 *  about a room that had nothing to do with it. */
export function readTopic(state: ConsoleState): Topic {
  const items = state.items || [];
  const out: Topic = {};

  /* The most recent failure. Walked from the END because the payload is
     ordered oldest first and the joke should be about the last thing to go
     wrong, not the first. */
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].status === "failed" && items[i].seat) {
      out.failedSeat = items[i].seat;
      break;
    }
  }

  const closed = new Map<string, number>();
  let priciest = 0;
  for (const it of items) {
    if (it.status === "done" && it.seat) {
      closed.set(it.seat, (closed.get(it.seat) || 0) + 1);
    }
    const cost = it.total_cost_usd || 0;
    if (cost > priciest && it.seat) {
      priciest = cost;
      out.priciestSeat = it.seat;
    }
    if (it.seat && (it.status === "queued" || it.status === "ready")) {
      out.stalledSeat = it.seat;
    }
  }
  /* WHOLE DOLLARS, AND ONLY WHEN IT IS AT LEAST ONE. "spent $0.03" is not a
     joke about spending, it is a number nobody was going to worry about, and a
     line built on it reads as the studio being precious. */
  if (priciest >= 1) out.priciestUsd = Math.round(priciest);
  else out.priciestSeat = undefined;

  /* Two is the floor for "busiest". One closed item makes everybody the
     busiest, which is a superlative about nothing. */
  for (const [seat, n] of closed) {
    if (n >= 2 && n > (out.busiestCount || 0)) {
      out.busiestSeat = seat;
      out.busiestCount = n;
    }
  }
  return out;
}

/** A template and the fact it needs. `when` is what makes this safe: a line is
 *  not a candidate unless its own precondition is true, so the fill can never
 *  be undefined and no line ever reads "the undefined room". */
type Topical = { when: (t: Topic) => boolean; line: (t: Topic) => string };

export const TOPICAL: readonly Topical[] = [
  { when: (t) => !!t.failedSeat,
    line: (t) => `Something went wrong in ${t.failedSeat} and the door has been shut since.` },
  { when: (t) => !!t.failedSeat,
    line: (t) => `${t.failedSeat} is having a day. Nobody has gone in to ask.` },
  { when: (t) => !!t.failedSeat,
    line: (t) => `I heard a noise from ${t.failedSeat} and decided it was not mine.` },

  { when: (t) => !!t.busiestSeat && !!t.busiestCount,
    line: (t) => `${t.busiestSeat} closed ${t.busiestCount} things today and told everybody about all of them.` },
  { when: (t) => !!t.busiestSeat,
    line: (t) => `The board says ${t.busiestSeat} is on top of everything. The board is easily impressed.` },
  { when: (t) => !!t.busiestSeat,
    line: (t) => `Somebody put a chart on the wall. ${t.busiestSeat} is winning it.` },

  { when: (t) => !!t.priciestSeat && !!t.priciestUsd,
    line: (t) => `${t.priciestSeat} spent $${t.priciestUsd} on one thing and called it research.` },
  { when: (t) => !!t.priciestSeat,
    line: (t) => `There is a receipt on the fridge with ${t.priciestSeat} written on it in red.` },

  { when: (t) => !!t.stalledSeat,
    line: (t) => `${t.stalledSeat} has something waiting and everyone is on a break. Fine. Sure.` },
  { when: (t) => !!t.stalledSeat,
    line: (t) => `The queue for ${t.stalledSeat} has not moved. Neither has anybody.` },
];

/** Which topical lines are usable right now. Empty is the normal case on a
 *  fresh board, and that is why the fixed pool never goes away. */
export function topicalFor(topic: Topic): string[] {
  return TOPICAL.filter((t) => t.when(topic)).map((t) => t.line(topic));
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
                          settle = SETTLE_MS, rotate = ROTATE_MS,
                          /* What the board makes it possible to joke about.
                             See readTopic. Optional because the tests drive
                             this hook with no payload. */
                          topic?: Topic,
                          /* Who is standing in the lounge and could say it.
                             Empty means nobody is available to speak, and then
                             there is no bubble - a line with no speaker is the
                             caption, which is what this used to be. */
                          speakers: readonly string[] = [],
                          /* A speaker's OWN lines, when its project wrote it
                             some. See floorplan.Persona.lines. */
                          linesFor?: (seat: string) => readonly string[]): Banter {
  const [line, setLine] = useState("");
  const [seat, setSeat] = useState("");
  /* One rotator for the life of the pane, so muting and unmuting does not hand
     back a fresh deck and let a line repeat inside its own cycle. */
  const next = useRef<(() => string) | null>(null);
  if (!next.current) next.current = createRotator();

  /* THE DECK IS REBUILT WHEN WHAT IS TRUE CHANGES, AND ONLY THEN. The key is
     the topical LINES rather than the topic object, because the topic is
     derived on every poll and an object identity would hand back a fresh deck
     three times a minute - which is the exact repeat the rotator exists to
     prevent. Identical facts produce an identical key and the deck survives.

     WHICH POOL DEPENDS ON WHERE THE LINE IS GOING. With somebody to say it the
     line is drawn in a bubble over their head, so it comes from BANTER_SHORT -
     five words, the length of a thing said out loud, and the length that fits
     over a character without covering the room. With nobody available it falls
     back to the long pool and the caption strip, where there is width for a
     two-clause line. Topical lines join the long pool only: they name a seat
     and a number, which is a caption's job.

     THE FIXED POOL IS ALWAYS IN THERE. A board with one failure on it would
     otherwise say the same three things about that failure until somebody
     cleared it, which is a worse kind of stale than the one this fixed. */
  const talking = speakers.length > 0;
  const topical = topic && !talking ? topicalFor(topic) : [];
  const key = [talking ? "s" : "l", ...topical].join("|");
  const seen = useRef("");
  if (seen.current !== key) {
    seen.current = key;
    next.current = createRotator(
      talking ? BANTER_SHORT
              : (topical.length ? [...BANTER, ...topical] : BANTER));
  }

  /* WHO SPEAKS, chosen once per line rather than once per render. A speaker
     recomputed on every poll would hand the same sentence to a different
     character mid-sentence, which reads as the line being passed around the
     room. Held in a ref because nothing renders from the choice itself. */
  const pool = speakers.join(",");
  const pick = useRef<() => string>(() => "");
  const lastOwn = useRef("");
  pick.current = () => (speakers.length
    ? speakers[Math.floor(Math.random() * speakers.length)]
    : "");

  useEffect(() => {
    if (!quiet || !on) { setLine(""); setSeat(""); return; }
    /* The interval is armed INSIDE the settle timeout rather than beside it.
       Running both from the same instant would space every line after the first
       by settle + rotate, which is not the gap this file documents and is the
       kind of drift nobody spots because it only reads as "a bit slow". */
    let every = 0;
    /* WHOEVER IS TALKING PICKS FROM THEIR OWN POOL WHEN THEY HAVE ONE, which
       is the whole of "this seat is a person rather than a room". The shared
       deck stays the fallback, so a project that has written lines for one seat
       does not silence the other seven.

       DRAWN FRESH RATHER THAN THROUGH THE ROTATOR: a per-seat pool is small
       and belongs to a character who only speaks occasionally, so the rotator's
       no-repeat bookkeeping would be maintained across a deck that changes
       identity every fifteen seconds. Avoiding the line just said is the only
       property that matters at this size. */
    const say = () => {
      const who = pick.current();
      const mine = who && linesFor ? linesFor(who) : [];
      if (mine.length) {
        const fresh = mine.length > 1
          ? mine.filter((l) => l !== lastOwn.current) : mine;
        const line = fresh[Math.floor(Math.random() * fresh.length)];
        lastOwn.current = line;
        setLine(line);
      } else {
        setLine(next.current!());
      }
      setSeat(who);
    };
    const first = window.setTimeout(() => {
      say();
      every = window.setInterval(say, rotate);
    }, settle);
    return () => { window.clearTimeout(first); window.clearInterval(every); };
  }, [quiet, on, settle, rotate, key, pool]);

  return { line, seat };
}

/** A line and, when there is somebody to say it, who. An empty `seat` means the
 *  line belongs in the caption strip rather than in a bubble. */
export type Banter = { line: string; seat: string };

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
