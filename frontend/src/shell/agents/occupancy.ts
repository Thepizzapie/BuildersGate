import { type ConsoleState, type Item } from "./api";
import { type Handoff } from "./handoff";

/* WHO IS WHERE ON THE FLOOR, decided from the poll and from nothing else.
 *
 * The floor's whole claim is that POSITION IS STATE: a character at a desk is
 * an agent that is running right now, one standing at the Director's door is
 * work that cannot move without a human. That claim is worth more than the
 * picture, so this file is the picture's conscience. A character shown at a
 * desk when nothing is running is worse than an empty floor, because an empty
 * floor makes you go and look and a wrong one stops you.
 *
 * The rules, therefore:
 *   · every placement names the row from /api/console/state that justifies it
 *     (`itemId`, or a question/gate id), and a seat we know nothing about is
 *     IDLE, which is the honest word for no information. Never a guess.
 *   · "live" is `agent.state === "running"` and only that. The roster in
 *     `state.agents` is every agent the dispatcher has ever tracked this run,
 *     finished and failed included; reading the whole list as running is a bug
 *     this codebase has already shipped once, and on the floor it would have
 *     sat a dead agent at a desk.
 *   · a gate only moves somebody to the door when it is `blocking`. A plain
 *     qa-gate is an agent reviewing an agent (the route stamps `blocking:false`
 *     on it), and queuing that outside the Director's office would put a human
 *     in the way of work that never wanted one. A missing `blocking` is treated
 *     as not blocking: unknown must not become a claim.
 *
 * THERE IS STILL NO ANIMATION IN HERE, and that was the condition on adding
 * one. The walking lives in floorRender.ts, which interpolates between two
 * consecutive outputs of this function and cannot invent a third: the DOM at
 * every moment says what was placed here, only the paint lags. Nothing below
 * softened to make a movement look better, because a placement that is wrong
 * for one poll is now wrong for one poll AND a walk toward it.
 *
 * The two placements step 2 adds, and what each one is standing on:
 *
 *   · CHAINED. A queue_add_chain files every link at once, so the whole chain
 *     leaves the lounge on the same poll and then works in order. A link whose
 *     predecessor has not landed carries `ready:false` from _chain_state, and
 *     it stands in its own room rather than at its desk: the desk is running,
 *     and no process exists for this link yet. Like `dispatched`, it is a claim
 *     about the BOARD and not about a running agent, which is why it is its own
 *     word and not a dimmer shade of running.
 *   · DELIVERING. The seat closed an item while this session was watching (see
 *     handoff.ts) and walks the result to the Director. It sits below
 *     `dispatched` on purpose: a seat that has already picked up its next piece
 *     of work has moved on, and drawing it at the office would hide live work
 *     behind a courtesy. The NOTE still reaches the desk in that case, because
 *     the note is a fact about the item and not about where anybody is standing.
 */

/** What the server said about this seat. */
export type FloorState =
  /** An agent is alive on this seat's item right now. */
  | "running"
  /** The item was handed out and no live agent has appeared for it yet. */
  | "dispatched"
  /** A human is the blocker: an open ask_human, or a gate only a person opens. */
  | "waiting"
  /** In a chain, holding a link whose predecessor has not landed. */
  | "chained"
  /** Closed an item this session and is walking the result to the Director. */
  | "delivering"
  /** The seat's last word is a failure nobody has reopened. */
  | "failed"
  /** Nothing was reported about this seat. Not "doing nothing", "not known". */
  | "idle";

/** Where the character stands. One zone per state, and the mapping is fixed:
 *  the zone is how the state is READ, so a state that could appear in two
 *  places would make the floor unreadable. */
export type Zone = "desk" | "room" | "door" | "lounge" | "office";

export type Occupant = {
  seat: string;
  state: FloorState;
  zone: Zone;
  /** The work item this placement is standing on, when a work item is what
   *  justified it. Null only for `idle`. */
  itemId: number | null;
  /** That item's title, for the hover. Never invented. */
  title?: string;
  /** Why, in the studio's own words - one short phrase for the tooltip. */
  note: string;
  /** The chain this seat's current item belongs to, when it belongs to one.
   *  The floor uses it for ONE thing: everybody in a chain steps off on the
   *  same frame, because a chain is filed as one act and reads as one. */
  chainId: number | null;
  /** Position in that chain, for the tooltip. `lane 2` on a card is this. */
  chainPos: number | null;
  /** The note being carried, when the state is `delivering`. It IS the item's
   *  result and it is copied, not composed. */
  carrying?: Handoff;
};

/* Field and record separators for the signature below. Control characters
   rather than punctuation, because a work item's TITLE goes into that string and
   a title containing whatever we picked would let two different boards produce
   one signature - which is the one failure mode of comparing by string. */
const FS = "\u0001", RS = "\u0002", GS = "\u0003";

/** A CHEAP STRING THAT CHANGES WHENEVER THE PLACEMENT COULD.
 *
 * consoleState() builds a fresh object and a fresh items array every 3s, so a
 * byte-identical payload still arrives with a new identity and every memo keyed
 * on it misses. On this pane that meant planFloor and placeFloor re-ran, the
 * whole room tree took new props, and every one of those commits handed the walk
 * a floor to re-measure - on an idle board with nothing happening, forever.
 * This is the equality the object identity cannot give.
 *
 * EVERY FIELD placeFloor READS IS IN HERE, and that is the contract: a field
 * that moves somebody but is missing from this string is a floor that stops
 * updating, which is far worse than the work it saves. Add one there, add it
 * here.
 */
export function floorSignature(state: ConsoleState): string {
  const items = (state.items || []).map((i) => [
    i.id, i.seat, i.status, i.title, i.chain_id ?? "", i.chain_pos ?? "",
    i.ready === false ? "0" : "",
    i.waiting_on ? `${i.waiting_on.id}:${i.waiting_on.seat || ""}` : "",
  ].join(FS));
  const agents = (state.agents || []).map((a) => `${a.item_id}${FS}${a.state || ""}`);
  const questions = (state.questions || [])
    .map((q) => `${q.id}${FS}${q.seat || ""}`);
  const gates = (state.gates || []).map((g) => [
    g.id, g.seat || "", g.blocking === true ? "1" : "", g.item_id ?? "",
    g.title || "", g.parked ? "1" : "",
  ].join(FS));
  /* The error belongs in it too: a floor drawn from a failed read is a
     different floor, and it has to be able to change back. */
  return [items.join(RS), agents.join(RS), questions.join(RS), gates.join(RS),
          state.__error || ""].join(GS);
}

/** Item ids with a genuinely live agent. Exported because "who is running" is
 *  the question every other reader of this pane has to answer the same way. */
export function liveItemIds(state: ConsoleState): Set<number> {
  const live = new Set<number>();
  for (const a of state.agents || []) {
    if (a.state === "running" && typeof a.item_id === "number") live.add(a.item_id);
  }
  return live;
}

/** Closings that answer a failure. `cancelled` and `rejected` are NOT in here:
 *  neither one is the work getting done, and a seat whose retry was rejected
 *  still has a problem in the room. */
const SUPERSEDES = new Set(["done", "approved"]);

const ZONE: Record<FloorState, Zone> = {
  running: "desk",
  dispatched: "room",
  waiting: "door",
  chained: "room",
  delivering: "office",
  failed: "room",
  idle: "lounge",
};

/** Place one character per seat.
 *
 * PRECEDENCE, and why it is this order. A seat can be several true things at
 * once - a failed item from Tuesday, a question open since lunch, and an agent
 * running right now - and only one of them gets to be a position.
 *
 *   1. running    an agent that is alive is where that seat's person IS. It
 *                 outranks a blocking gate, because a gate can hang over work
 *                 that finished while the seat has already moved on to the next
 *                 item, and drawing that seat at the door would say the studio
 *                 is stalled when it is not.
 *   2. waiting    nothing is running and a human is the blocker. This is the
 *                 one placement that asks something of the person reading it,
 *                 which is why it gets the door rather than a badge.
 *   3. dispatched work is out and the runner has not come up yet. In the room,
 *                 not at the desk: the desk means running, and the gap between
 *                 dispatch and first step is exactly where a runner that failed
 *                 to start hides.
 *   4. delivering the seat closed something and nothing newer has been handed
 *                 to it. It walks the result to the Director's office and the
 *                 note stays on the desk for a few seconds.
 *   5. chained    holding a link that is blocked behind an earlier one. In its
 *                 own room, waiting its turn - the chain walked out together
 *                 and only the ready link is at a desk.
 *   6. failed     the seat's most recent failure, while nothing newer is live
 *                 and nothing newer has closed well. A failed item stays failed
 *                 until somebody deals with it, so this room stays marked until
 *                 they do - which is the point of marking it - but a later item
 *                 that finished is the board saying they did.
 *   7. idle       nothing known. The lounge.
 *
 * An open ask_human keeps its agent alive in the dispatcher, so a seat that is
 * both running and questioned lands on `running` by rule 1 and the question
 * still shows on the item. That is the one place the ordering costs something,
 * and it is deliberate: the agent process really is up.
 */
export function placeFloor(
  seats: string[], state: ConsoleState,
  /* The notes this session watched being written. Optional so the placement
     stays callable without a React tree behind it - the whole point of it being
     a pure function is that the claim can be checked without a browser. */
  handoffs?: Map<string, Handoff>,
): Occupant[] {
  const live = liveItemIds(state);
  const items = state.items || [];

  /* Newest first is how "the seat's current item" gets decided everywhere else
     in this shell, and id order is the only ordering every row is guaranteed to
     have - updated_at is absent on rows the board did not touch. */
  const byNewest = [...items].sort((a, b) => b.id - a.id);

  const firstFor = (seat: string, match: (item: Item) => boolean) =>
    byNewest.find((i) => i.seat === seat && match(i)) || null;

  /* Chain fields, from whatever item the placement landed on. Read through one
     helper so every branch reports the chain the same way and a placement
     cannot quietly forget it - the chain is what makes a group of characters
     step off together, so a null here breaks the movement, not the label. */
  const chain = (it: Item | null) => ({
    chainId: (it && it.chain_id) || null,
    chainPos: (it && it.chain_pos) || null,
  });

  const questionFor = (seat: string) =>
    (state.questions || []).find((q) => q.seat === seat) || null;

  /* `blocking !== true` and not `=== false`: an older server that never sent
     the field must not silently promote every gate to a human blocker. */
  const gateFor = (seat: string) =>
    (state.gates || []).find((g) => g.seat === seat && g.blocking === true) || null;

  return seats.map((seat): Occupant => {
    const running = firstFor(seat, (i) => live.has(i.id));
    if (running) {
      return { seat, state: "running", zone: ZONE.running, itemId: running.id,
               title: running.title, note: "at the desk, running",
               ...chain(running) };
    }

    /* NOBODY QUEUES OUTSIDE THEIR OWN DOOR. The door is where you go to reach
       the Director, so a waiting Director has nowhere to walk to and stays in
       the office. The state is still `waiting` - it is true and the room says
       so - only the zone differs. The same is true of a handover: the Director
       does not walk a note across the floor to itself. */
    /* THE DIRECTOR NEVER LEAVES THE OFFICE, and that is now true of every
       state rather than only the two that used to ask.

       NOBODY QUEUES OUTSIDE THEIR OWN DOOR: the door is where you go to reach
       the Director, so a waiting Director has nowhere to walk to. The same
       logic applies to a HANDOVER - it does not carry a note across the floor
       to itself - and to being IDLE, which is the case this missed. An idle
       Director walked to the lounge and stood in the crowd, which is both wrong
       about the fiction (the office is where you go to find it) and wrong about
       the pane's own rule, since the office is the room the queue forms outside
       of. If the Director is not in it, the queue is pointing at an empty
       room. */
    const home = (zone: Zone): Zone => (seat === "director" ? "room" : zone);

    const question = questionFor(seat);
    if (question) {
      return { seat, state: "waiting", zone: home(ZONE.waiting),
               itemId: null, note: "asked you a question",
               chainId: null, chainPos: null };
    }
    const gate = gateFor(seat);
    if (gate) {
      return { seat, state: "waiting", zone: home(ZONE.waiting),
               itemId: typeof gate.item_id === "number" ? gate.item_id : null,
               title: gate.title,
               note: gate.parked ? "parked, waiting on your sign-off"
                                 : "waiting on your call",
               chainId: null, chainPos: null };
    }

    const dispatched = firstFor(seat, (i) => i.status === "dispatched"
                                             && !live.has(i.id));
    if (dispatched) {
      return { seat, state: "dispatched", zone: home(ZONE.dispatched), itemId: dispatched.id,
               title: dispatched.title, note: "dispatched, not started yet",
               ...chain(dispatched) };
    }

    const carrying = handoffs?.get(seat);
    if (carrying) {
      return { seat, state: "delivering", zone: home(ZONE.delivering),
               itemId: carrying.itemId, title: carrying.title,
               note: carrying.status === "done" ? "handing over the result"
                                                : `handing over: ${carrying.status}`,
               carrying, chainId: null, chainPos: null };
    }

    /* `ready === false` and not `!ready`: _chain_state only stamps readiness
       when SOMETHING on the board is chained, and an undefined field on an
       unchained item would otherwise march every queued seat into a room it has
       no business being in. Queued only, because a chained link that reached
       `dispatched` was already caught above. */
    const chained = firstFor(seat, (i) => !!i.chain_id && i.ready === false
                                          && i.status === "queued");
    if (chained) {
      const on = chained.waiting_on;
      return { seat, state: "chained", zone: home(ZONE.chained), itemId: chained.id,
               title: chained.title,
               note: on ? `next up, waiting on ${on.seat || "#" + on.id}`
                        : "next up in the chain",
               ...chain(chained) };
    }

    /* THE FAILURE HAS TO STILL BE THE SEAT'S LAST WORD.
       /api/console/state sorts failed rows ahead of done ones and returns up to
       80 of them, so a failure from Tuesday sits in the payload for days. Taking
       the newest FAILED row on its own therefore left the room red and the
       tooltip saying "last item failed" after the human had refiled the work and
       watched it finish - the same payload, one card away on the board, showing
       that seat's most recent item as done. A newer row that CLOSED WELL is the
       board saying the failure was dealt with, and it retires the mark. A newer
       row that is merely queued does not: nobody has fixed anything yet. */
    const failed = firstFor(seat, (i) => i.status === "failed");
    const settled = failed
      && byNewest.some((i) => i.seat === seat && i.id > failed.id
                              && SUPERSEDES.has(i.status));
    if (failed && !settled) {
      return { seat, state: "failed", zone: ZONE.failed, itemId: failed.id,
               title: failed.title, note: "last finished item failed",
               ...chain(failed) };
    }

    return { seat, state: "idle", zone: home(ZONE.idle), itemId: null,
             note: "nothing running", chainId: null, chainPos: null };
  });
}
