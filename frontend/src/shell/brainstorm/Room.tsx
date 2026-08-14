import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Badge, Button, Group, Menu, Modal, ScrollArea, Stack, Text, Textarea, Tooltip,
} from "@mantine/core";
import { Ti } from "../Ti";
import { SEAT_COLOR, SEAT_ICON } from "../nav";
import { askText, mutate, readJSON, toast } from "../../bridge";
import { CanonColumn } from "./Canon";
import { useVoice } from "./voice";
import { usePoll, useViewActive } from "../../hooks";
import { useStickyBottom } from "../sticky";
import "./room.css";

/* 11 · ONE ROOM, SEATS JOIN IT.
 *
 * This replaces 9 — the two-room version, where narrative got a room of its
 * own. Two seat-owned rooms means the interesting conversation happens twice:
 * the combat-feel room needed gameplay AND art in it, the CEO-lich room needed
 * the director. One room that seats walk into gets the same range without
 * duplicating the drawer, and narrative stops being a special case — it becomes
 * a participant that brings canon with it.
 *
 * THE GUARANTEE IS THE DESIGN PROBLEM, and it is why the roster is a panel and
 * not a row of avatars. Today's partner is spawned with an empty tool set —
 * that is the whole reason this room is cheap and safe to think in. A seat
 * agent normally arrives holding Write, Bash and queue_add, so a seat that
 * joined as itself would put the board back inside the room. The roster
 * therefore states the rule in words, at its foot, where it cannot be missed:
 * SEATS ENTER WITHOUT THEIR TOOLS.
 *
 * Which makes everything said in here an OPINION, not a commitment. So a reply
 * does not quietly become work: it carries a promote control, promotion stages
 * an item at the door, and the door is still the only way to the board. The
 * footer counts them the way the design says it — "3 opinions promoted, 0
 * filed" — because promoted and filed are genuinely different states and
 * collapsing them is how a room starts dispatching by accident.
 *
 * Kept from 8a because 8a got them right: the permanent door footer that states
 * the promise and only turns ember when there is something to file; the notes
 * pad being explicitly YOURS; and synthesize → preview → file as two steps,
 * with the plan living in this component and nowhere else until you file it.
 *
 * THE PADS ARE 8a's HALF OF THIS SCREEN AND THEY ARE NOT OPTIONAL. The first
 * pass built 11a alone and shipped a sketch panel that said "open the pad to
 * edit" with no pad to open — which was the first thing anybody noticed was
 * missing. The real editor is brainstorm.js: 2,700 lines that own the
 * conversation, the writing pad and a drawing surface with its own hit-testing,
 * undo and scene serialisation. It has ONE layout and no pads-only mode (its own
 * mount contract says cut-down variants are how one module quietly becomes two),
 * so it cannot live inside a 320px column beside this room's own conversation.
 *
 * So it opens FULL SIZE, in a modal, over this room — and only one of the two is
 * ever driving the scene, which is the thing a second inline editor could not
 * promise. The panel that stays behind is not a placeholder: the element count,
 * the thumbnail, clear and export are all computed from or written to the real
 * `session.drawing` scene, and each is wired to something that exists.
 *
 * WHAT THE BACKEND CANNOT DO YET, AND IS THEREFORE NOT DRAWN AS IF IT COULD.
 * bgate_core/brainstorm.py has one seat per session (director | narrative), a
 * message role of exactly "user" | "assistant", and no participant table. So:
 * there are no invited seats to list, no per-seat spend, and no seat on a
 * message. The roster shows the two participants that DO exist and says what
 * would fill the rest; the invite chips are drawn from the project's real seat
 * table and are disabled with the reason on screen. A promote control that
 * silently did nothing would be worse than none — this one is real, because
 * promotion is a client-side staging step that ends at the door either way.
 */

type Msg = {
  id: number; role: string; text: string; created_at?: string;
  /* Not sent today. Read anyway: the moment brainstorm_message carries a seat,
     attribution here is right without a second edit — and while it is absent
     nothing is invented, the reply is just labelled "partner". */
  seat?: string;
};
type Deploy = {
  at?: string; by?: string; summary?: string;
  items?: { id?: number; seat?: string; title?: string }[];
};
type Thinker = {
  available?: boolean; label?: string; model?: string; runner?: string;
  live?: boolean; turns?: number; spent_usd?: number; max_usd?: number;
  readonly?: boolean; readonly_by?: string;
  tools?: string[]; mcp_servers?: string[];
};
/* THE CLASSIC PAD, DECLARED WHERE IT IS USED. This lived in seats/Brainstorm.tsx
   while that file mounted brainstorm.js itself; it does not any more — it mounts
   this component — and this is the only caller left, so the declaration belongs
   here rather than in a file that no longer touches the object. */
declare global {
  interface Window {
    Brainstorm?: {
      mount(host: HTMLElement, opts?: { seat?: string }): unknown;
      unmount(): void;
      /** The drawing surface alone, for a caller with its own layout. Returns
       *  the same Pad the full workspace uses. */
      mountPad?(host: HTMLElement, opts?: {
        sessionId?: number;
        onSave?(session: { drawing?: unknown }): void;
        onError?(err: unknown): void;
      }): { destroy?(): void; load?(scene: unknown): void } | null;
    };
  }
}

type RoomRow = {
  /** Seats presently in the room, and what the room has cost — aggregates that
   *  ride on the listing so the rail can draw both without a read per row. See
   *  brainstorm.list_sessions. */
  guests?: string[]; spent_usd?: number;
  id: number; seat?: string; title?: string; status?: string;
  /* THE INDEX AND THE SESSION DISAGREE ON PURPOSE. /api/brainstorm returns a
     message COUNT (the index never carries the transcript); /api/brainstorm/:id
     returns the messages themselves. Treating the count as an array is what put
     a blank where the turn count belongs. */
  messages?: number; notes_len?: number; updated_at?: string;
};
/** One element of the drawing scene, in the fields this component actually
 *  reads. The pad emits Excalidraw-shaped elements — typed shapes with real
 *  geometry — which is exactly why a thumbnail is possible here at all: there
 *  are no strokes-as-pixels to rasterise, only boxes with x/y/w/h. */
type DrawEl = {
  id?: string; type?: string; isDeleted?: boolean;
  x?: number; y?: number; width?: number; height?: number;
};
type Scene = {
  type?: string; version?: number; source?: string;
  elements?: DrawEl[]; appState?: Record<string, unknown>;
};
/** A seat that was invited into the room.
 *
 * `state` AND `live` DISAGREE ON PURPOSE and the roster draws both. `state` is
 * the record — it says the seat is in the room. `live` is an observation — it
 * says a process exists right now. An idle reap or a dashboard restart kills
 * the process and never touches the row, so `state:"live", live:false` means
 * "in the room, not running", and the next message addressed to it starts one.
 * Collapsing the two into one dot would make a normal, recoverable state look
 * like the seat had left. */
type Participant = {
  id: number; seat: string; state: "invited" | "live" | "left"; live?: boolean;
  invited_by?: string; invited_at?: string; turns?: number; spent_usd?: number;
  thinker?: Thinker;
};

type Session = {
  id: number; seat?: string; title?: string; status?: string; notes?: string;
  created_at?: string; messages?: Msg[]; deploys?: Deploy[];
  drawing?: Scene; drawing_png?: string; thinker?: Thinker;
  participants?: Participant[];
  /** True while a round is in flight on the server. The poll is what brings
   *  the answers in, so this is the only thing that can say the room is busy. */
  answering?: boolean;
  /** Extra rounds the room talks among itself after answering you. 0 is off,
   *  and is the default — every round is one billed turn per voice present. */
  discuss_rounds?: number;
};

/** What the discussion dial offers. The server's ceiling is 6 (schema CHECK);
 *  these are the rungs worth a click — off, a follow-up, an argument, and the
 *  long one that is the room's own limit. */
const DISCUSS_STEPS = [0, 1, 2, 4] as const;

/** A staged item. `seat`, `title`, `brief` and `priority` are what
 *  validate_plan reads; `from_*` are ours, for the preview's provenance line,
 *  and the server drops them when it rebuilds the plan. */
type PlanItem = {
  seat: string; title: string; brief: string; priority?: number;
  from_msg?: number; from_who?: string;
};
/** The plan shape the API actually speaks. It is an OBJECT with an items list —
 *  deploy hands it to validate_plan, which rejects a bare array with "a plan is
 *  an object with an 'items' list". */
type Plan = {
  summary?: string; items: PlanItem[]; chained?: boolean;
  questions?: string[]; notes?: string[];
};

const EMPTY: Session = { id: 0, messages: [] };
const POLL_MS = 6000;
/* MIRRORS bgate_core/brainstorm.py's MAX_PARTICIPANTS. Duplicated rather than
   fetched because the only thing it changes here is whether a chip explains
   itself before the click; the server is still the one that refuses, and it
   refuses with the same sentence. */
const MAX_GUESTS = 4;

/* UNREAD IS A CLIENT-SIDE FACT HERE, AND IT SAYS SO ON THE ROW.
 *
 * The design puts an unread count on every room. There is no read state to read:
 * brainstorm_session carries id/seat/title/status/notes/drawing/deploys and
 * nothing about who has seen what, and inventing a server field is not this
 * screen's decision to make. What IS true is how many turns a room held the last
 * time YOU had it open in THIS browser — the index already ships the message
 * count — so the badge is the difference between the two, and its tooltip says
 * whose memory it is. A room you have never opened counts as entirely unread,
 * which is the honest reading of "never opened" and not a bug to smooth over.
 *
 * localStorage rather than a state hook: the value has to survive the reload
 * that is the whole reason a person wants to know what changed while they were
 * away. It is namespaced per project root by nothing — this dashboard serves one
 * project at a time, and the ids come from that project's db. */
const SEEN_KEY = "bg4-room-seen";

function readSeen(): Record<string, number> {
  try {
    const raw = localStorage.getItem(SEEN_KEY);
    const v = raw ? JSON.parse(raw) : null;
    return v && typeof v === "object" ? (v as Record<string, number>) : {};
  } catch { return {}; }         // a corrupt key marks everything unread, not a crash
}
function writeSeen(next: Record<string, number>): void {
  try { localStorage.setItem(SEEN_KEY, JSON.stringify(next)); } catch { /* private mode */ }
}

/** Server stamps are `YYYY-MM-DD HH:MM:SS` in UTC. Parsed as UTC EXPLICITLY:
 *  letting the browser read them as local time reports every line as hours old
 *  in one direction, or in the future in the other. */
function ago(stamp?: string): string {
  if (!stamp) return "";
  const t = Date.parse(stamp.replace(" ", "T") + "Z");
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** The first line of a reply, for the promote dialog's placeholder. A work item
 *  title is a title; six paragraphs of argument is the brief. */
function firstLine(text: string): string {
  const line = (text || "").split("\n").find((l) => l.trim()) || "";
  return line.trim().slice(0, 90);
}

/* `seat` SCOPES THE ROOM WITHOUT FORKING IT.
 *
 * The seat workspaces need this room in a tab, and the first attempt at that
 * mounted the CLASSIC brainstorm.js workspace instead — chat, a writing pad and
 * a drawing pad, and no canon column at all. That is the wrong room: the whole
 * argument of the narrative seat is that you think AGAINST the canon, with the
 * locked facts pinned where nothing can contradict them, and the classic
 * workspace has no idea canon exists.
 *
 * So the seat tab mounts THIS component with a seat, rather than a second
 * implementation. Scoping is deliberately only two things — which rooms the
 * rail lists, and which room opens first — because everything else about the
 * room is the same room. In particular the canon column, the roster, the door
 * and the promote controls are untouched: a seat-scoped room that quietly had
 * fewer guarantees than the full one is exactly the "cut-down variant" that
 * brainstorm.js's own mount contract warns turns one module into two.
 */
export function Room({ seat }: { seat?: string } = {}) {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [rooms, setRooms] = useState<RoomRow[]>([]);
  const [roomSeats, setRoomSeats] = useState<string[]>([]);
  const [fileSeats, setFileSeats] = useState<string[]>([]);
  const [session, setSession] = useState<Session>(EMPTY);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<null | "say" | "synth" | "file" | "new" | "invite">(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [notes, setNotes] = useState("");
  /* The seat table's WRITE LANES, which are the only honest answer this screen
     has to "what would this touch". See wouldTouch() below. */
  const [lanes, setLanes] = useState<Record<string, string[]>>({});
  const [seen, setSeen] = useState<Record<string, number>>(() => readSeen());
  /* Who the next message is addressed to, or null for the room. */
  const [asked, setAsked] = useState<string | null>(null);
  const [padOpen, setPadOpen] = useState(false);
  /* THE ROSTER, WHEN THE GRID CANNOT AFFORD A THIRD COLUMN.
     It used to be `display: none` under 1240px, which is not "responsive" — it
     is the panel that says who is in the room, what they have cost and how to
     invite anybody, deleted, with nothing on screen admitting it exists. On a
     laptop the room simply had no roster and no way to ask for one. */
  const [rosterOpen, setRosterOpen] = useState(false);
  const voice = useVoice(active);

  /* A FINISHED SENTENCE APPENDS; IT DOES NOT REPLACE. Deepgram finalises in
     segments, so overwriting would leave only the last clause of anything long
     — and it must not clobber what was typed before the mic was armed. The
     interim text is drawn in the box separately (see the composer) and never
     stored, because it changes on every packet. */
  useEffect(() => {
    voice.setOnFinal((said: string) => {
      setText((prev) => (prev ? `${prev.replace(/\s+$/, "")} ${said}` : said));
    });
  }, [voice]);

  const list = useCallback(async () => {
    const d = await readJSON<{ sessions: RoomRow[]; seats: string[] }>(
      "/api/brainstorm", { sessions: [], seats: [] });
    /* Filtered HERE rather than at the render, so "which room opens first" and
       "which rooms are listed" cannot disagree — a rail showing three rooms
       while the transcript shows a fourth is the bug that split would produce.
       `seat || "director"` matches the rail's own default for a row the server
       left unlabelled. */
    const mine = seat
      ? (d.sessions || []).filter((s) => (s.seat || "director") === seat)
      : (d.sessions || []);
    setRooms(mine);
    /* The new-room menu offers only this seat when scoped: opening an art room
       from inside the narrative seat's tab files it somewhere the tab will not
       list, and it would look like the click did nothing. */
    setRoomSeats(seat ? [seat] : (d.seats || []));
    return mine;
  }, [seat]);

  /* The project's OWN seat table, which is what the invite panel and the
     promote menu offer. Not a constant list: a project can disable a seat, and
     offering a seat the queue will refuse is a refusal you only see after you
     have written the title. Read once per activation — /api/state is the
     dashboard's whole world and has no business on this screen's poll. */
  const loadSeats = useCallback(async () => {
    const d = await readJSON<{ seats: { role?: string; write_globs?: string[] }[] }>(
      "/api/state", { seats: [] });
    setFileSeats((d.seats || []).map((s) => s.role || "").filter(Boolean));
    // Same read, second use: the lane globs ride on the same rows, so the
    // promote control's "would touch" costs no extra request.
    const by: Record<string, string[]> = {};
    for (const s of d.seats || [])
      if (s.role) by[s.role] = Array.isArray(s.write_globs) ? s.write_globs : [];
    setLanes(by);
  }, []);

  /** Re-read the open room. `pads` false leaves the notes textarea alone —
   *  the poll must never overwrite a sentence somebody is in the middle of. */
  const refresh = useCallback(async (id: number, pads = false) => {
    if (!id) return;
    const s = await readJSON<Session>(`/api/brainstorm/${id}`, { id });
    setSession(s);
    if (pads) setNotes(s.notes || "");
    /* READ MEANS ON SCREEN, which is why the mark happens here and not in
       open(): the poll runs while you are sitting in the room, so a reply that
       lands under your eyes is read by the time you look away. */
    setSeen((prev) => {
      const n = (s.messages || []).length;
      if (prev[id] === n) return prev;           // no write, no re-render storm
      const next = { ...prev, [id]: n };
      writeSeen(next);
      return next;
    });
  }, []);

  const open = useCallback(async (id: number) => {
    setPlan(null);          // a plan belongs to the conversation that made it
    await refresh(id, true);
  }, [refresh]);

  useEffect(() => {
    if (!active) return;
    loadSeats();
    list().then((ss) => {
      const pick = ss.find((s) => s.status === "open") || ss[0];
      if (pick) open(pick.id);
    });
  }, [active, list, loadSeats, open]);

  /* REPLIES LAND WHENEVER is the half of 11 the UI has to carry. Today a reply
     is synchronous with the POST, so this poll only catches another window's
     writes — but it is the hook an asynchronous seat reply arrives through, and
     wiring it now means the transcript is already a thing that grows on its own
     rather than a thing that grows when you press say. */
  usePoll(() => { if (session.id) refresh(session.id); },
          POLL_MS, active && !!session.id);

  /* ASK ONE SEAT — A ROUTING FIELD NOW, NOT A PREFIX IN THE TEXT.
   *
   * This used to write "@art — …" into the message body, because the room held
   * exactly one partner and there was nothing to route to. There is now:
   * `to` names a participant, the server refuses a `to` that is not in the room
   * BEFORE storing the message (a question addressed to nobody must not sit in
   * the transcript), and omitting it means everyone present answers — one
   * blocking turn each, in invite order.
   *
   * WHICH IS WHY THE COST LINE IS ON THE COMPOSER. A room with four guests is
   * five CLI turns per sentence, and the person typing is the only one who can
   * decide that is worth it. */
  /* SENDING RETURNS AS SOON AS THE MESSAGE IS STORED.
     The server hands the round to a thread and the poll brings each voice in as
     it finishes. Before this, the POST stayed open for the whole round — four
     seats and a discussion round is up to ten sequential CLI turns — so the
     send button spun for minutes and the only way to say anything else was to
     reload the page. The answers were arriving by poll the entire time. */
  async function say() {
    const said = text.trim();
    if (!said || busy) return;
    setBusy("say"); setText("");
    try {
      const r = await mutate(`/api/brainstorm/${session.id}/message`,
                             { body: asked ? { text: said, to: asked } : { text: said } });
      if (r.ok) refresh(session.id);
    } finally {
      // In a finally because a refusal (a room already mid-round) and a dead
      // backend both used to leave the button spinning forever.
      setBusy(null);
    }
  }


  /* HOW LONG THE ROOM ARGUES WITH ITSELF, and the switch that stops it.
     Stored on the room rather than in a preference, because two rooms in one
     project legitimately want different answers and the cost lands on the room.
     Optimistic locally so the dial does not lag a click behind, then reconciled
     by the refresh — a failed PATCH puts the server's number back. */
  async function setDiscuss(rounds: number) {
    const was = session.discuss_rounds || 0;
    setSession((s) => ({ ...s, discuss_rounds: rounds }));
    const r = await mutate(`/api/brainstorm/${session.id}`, {
      method: "PATCH", body: { discuss_rounds: rounds },
      ok: rounds ? `the room follows up, up to ${rounds} round${rounds === 1 ? "" : "s"}`
                 : "free discussion off — one answer each, then it stops",
    });
    if (!r.ok) setSession((s) => ({ ...s, discuss_rounds: was }));
    else refresh(session.id);
  }

  /* INVITE AND LEAVE. The invited seat is spawned by the SAME function and the
     same argv as the room's own partner — no tools, no MCP but the two-tool pad
     server, `BGATE_SEAT` stripped from its environment. It answers AS the seat;
     it does not HOLD the seat, and nothing it says reaches the board except
     through a promotion you make by hand. */
  async function invite(seat: string) {
    setBusy("invite");
    const r = await mutate(`/api/brainstorm/${session.id}/invite`,
                           { body: { seat }, ok: `${seat} joined the room` });
    setBusy(null);
    if (r.ok) refresh(session.id);
  }

  async function leave(seat: string) {
    const r = await mutate(`/api/brainstorm/${session.id}/invite/${seat}`,
                           { method: "DELETE", ok: `${seat} left the room` });
    if (r.ok) {
      /* The seat you were addressing is gone; leaving `asked` set would send the
         next message to a 400. */
      setAsked((a) => (a === seat ? null : a));
      refresh(session.id);
    }
  }

  async function newRoom(seat: string) {
    setBusy("new");
    const r = await mutate<Session>("/api/brainstorm",
                                    { body: { seat }, ok: `new ${seat} room` });
    setBusy(null);
    if (!r.ok) return;
    await list();
    if (r.data?.id) open(r.data.id);
  }

  /* SYNTHESIZE WRITES NOTHING. It reads the conversation and proposes; what it
     returns lives in this component and nowhere else until you file it.
     Anything you promoted by hand SURVIVES the synthesis and stays first — the
     model did not read your promotions, so letting its answer replace them
     would silently drop the opinions you had already picked out. Duplicates
     collapse on seat+title, which is the same pair file_plan fingerprints. */
  async function synthesize() {
    setBusy("synth");
    const r = await mutate<{ plan?: Plan }>(
      `/api/brainstorm/${session.id}/synthesize`, {});
    setBusy(null);
    if (!r.ok) return;
    const got = r.data?.plan;
    const proposed = (got?.items || []) as PlanItem[];
    setPlan((prev) => {
      const mine = prev?.items || [];
      const seen = new Set(mine.map((i) => `${i.seat}${i.title}`));
      return {
        ...got,
        items: [...mine, ...proposed.filter(
          (i) => !seen.has(`${i.seat}${i.title}`))],
      };
    });
    if (!proposed.length && !(plan?.items.length))
      toast(got?.summary ? "the partner summarised but proposed nothing to file"
                         : "the partner had nothing to file yet");
  }

  /* PROMOTION IS YOURS AND IT STOPS AT THE DOOR. It does not call /api/queue —
     that would put work on the board from inside a room whose whole promise is
     that nothing in it reaches the board without a human opening the one exit.
     It stages an item, exactly like a synthesised one, and the same file plan
     files it. The seat is a choice you make: a message carries no seat yet, and
     guessing one would be inventing the provenance the item is supposed to
     keep. */
  async function promote(m: Msg, seat: string, who: string) {
    const title = await askText({
      title: `Promote to a ${seat} item`,
      body: "It joins the plan waiting at the door. Nothing reaches the board "
          + "until you file — promotion is never automatic and never silent.",
      label: "Title for the work item",
      placeholder: firstLine(m.text),
      ok: "promote",
    });
    if (title == null) return;                     // backed out, not confirmed
    if (!title.trim()) { toast("a work item needs a title"); return; }
    const item: PlanItem = {
      seat, title: title.trim(),
      // The provenance rides in the brief because that is the only field that
      // survives filing — validate_plan rebuilds every item from four keys, and
      // an agent picking this up should know it is somebody's opinion.
      brief: `${m.text}\n\n— promoted from brainstorm room #${session.id}, `
           + `said by ${who}`,
      from_msg: m.id, from_who: who,
    };
    setPlan((p) => ({ summary: p?.summary, chained: false,
                      questions: p?.questions, notes: p?.notes,
                      items: [...(p?.items || []), item] }));
  }

  function cut(index: number) {
    setPlan((p) => (p ? { ...p, items: p.items.filter((_, i) => i !== index) } : p));
  }

  async function file() {
    if (!plan?.items.length) return;
    setBusy("file");
    // The plan goes back as an OBJECT — deploy hands it to validate_plan, which
    // refuses a bare list, and the summary is what the deploy record shows on
    // the board afterwards.
    const r = await mutate(`/api/brainstorm/${session.id}/deploy`, {
      body: { plan: { summary: plan.summary || "", items: plan.items,
                      chained: !!plan.chained } },
      ok: "filed to the board",
    });
    setBusy(null);
    if (r.ok) { setPlan(null); refresh(session.id); list(); }
  }

  /* The pad saves on blur, not per keystroke: it is a writing surface, and a
     PATCH per character would make the partner's read of it race your typing. */
  async function saveNotes() {
    if (!session.id || notes === (session.notes || "")) return;
    await mutate(`/api/brainstorm/${session.id}`,
                 { method: "PATCH", body: { notes }, quiet: true });
    setSession((s) => ({ ...s, notes }));
  }

  /* ---- the drawing pad ------------------------------------------------- */

  /* Every element the scene really holds, minus the tombstones the pad leaves
     behind: normaliseScene() drops `isDeleted` on load, so counting them here
     would report a number the editor itself does not agree with. */
  /* The narrative room is the one variant the design names. */
  const isNarrative = (session.seat || "") === "narrative";
  const drawn = useMemo(
    () => (session.drawing?.elements || []).filter((e) => e && !e.isDeleted),
    [session.drawing]);

  /* CLEAR IS A DELETION and it asks first, through the page's own dialog rather
     than a confirm() the shell would have to style twice. It writes the same
     scene shape the pad writes — the endpoint stores what it is given, and a
     bare `{elements: []}` would drop the appState the pad expects to find. */
  async function clearSketch() {
    if (!session.id || !drawn.length) return;
    const go = await askText({
      title: "Clear the sketch?",
      body: `${drawn.length} element${drawn.length === 1 ? "" : "s"} go, and `
          + "the drawing has no undo once this is stored. The conversation and "
          + "your notes are untouched. Type anything and press clear to confirm.",
      label: "Confirm",
      placeholder: "clear",
      ok: "clear the sketch",
    });
    if (go == null) return;                        // backed out, not confirmed
    const r = await mutate(`/api/brainstorm/${session.id}`, {
      method: "PATCH",
      body: { drawing: {
        type: "excalidraw", version: 2, source: "builders-gate/brainstorm",
        elements: [], appState: session.drawing?.appState || {},
      } },
      ok: "sketch cleared",
    });
    if (r.ok) refresh(session.id, true);
  }

  /* EXPORT IS THE SCENE, VERBATIM. The pad emits Excalidraw's own element shape
     inside an {type:"excalidraw", version:2, elements, appState} wrapper, which
     is literally the .excalidraw file format — so the honest export is the
     stored JSON with nothing added to it. Nothing is re-shaped on the way out:
     a field this component invented here would be a field excalidraw.com
     rejects and nobody could explain. */
  function exportScene() {
    if (!session.drawing || !drawn.length) return;
    const blob = new Blob([JSON.stringify(session.drawing, null, 2)],
                          { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `brainstorm-${session.id}.excalidraw`;
    a.click();
    URL.revokeObjectURL(url);
  }

  /* MOUNTING THE REAL WORKSPACE, ON THE ROOM YOU ARE ALREADY IN.
   *
   * `mount` takes a seat and nothing else, and then picks its own session — the
   * remembered one from `bs-last-<seat>`, unless it judges it stale, in which
   * case it opens the drawer instead. Landing in a DIFFERENT room from the one
   * behind the modal is the one outcome that would make the pads untrustworthy:
   * you would draw on the wrong scene and neither surface would say so.
   *
   * So both levers get pulled. The remembered id is set first (that is the key
   * the module reads and writes itself), and the instance mount() hands back is
   * asked to open this session directly. Whichever of the two async paths lands
   * last, both name the same id, so they cannot disagree. The workspace shows
   * the room's title in its own header, which is where you would notice if a
   * future refactor broke this.
   *
   * ONE INSTANCE EXISTS AT A TIME — the module keeps a module-level ACTIVE and
   * destroys the previous one on mount. That is a feature here (two editors over
   * one scene is the thing this modal exists to avoid) and a constraint
   * elsewhere: the seat workspace's Brainstorm tab uses the same singleton, so
   * opening this pad tears that one down. It re-mounts when that tab renders. */
  /* A CALLBACK REF, NOT AN EFFECT ON A ref.current.
     Mantine renders a Modal's children through a portal, and on the render that
     flips `padOpen` the host is not in the DOM yet — so an effect keyed on
     `padOpen` read `padHost.current === null`, returned, and never tried again.
     MEASURED: the modal opened at 90vw containing its title and nothing else,
     while mounting the same module into a hand-made div in the same modal body
     produced the full 15 kB workspace. That is the placeholder failure this
     whole rebuild exists to undo, so it is worth the extra care.

     A callback ref fires WITH the node the moment React attaches it, and again
     with null when it detaches — which is exactly the mount/unmount pair the
     module wants. */
  const padSession = useRef<number>(0);
  const inlinePadRef = useRef<{ destroy?(): void } | null>(null);
  const padHost = useCallback((el: HTMLDivElement | null) => {
    const mod = window.Brainstorm;
    if (!mod) return;
    if (!el) { mod.unmount(); return; }
    const seat = session.seat === "narrative" ? "narrative" : "director";
    /* The module picks its session from this key, so it is written BEFORE the
       mount and the returned instance is asked for the same id — two ways of
       naming one room, because being in the wrong room is silent. */
    try { localStorage.setItem(`bs-last-${seat}`, String(session.id)); } catch { /* private mode */ }
    padSession.current = session.id;
    const ws = mod.mount(el, { seat }) as { open?(id: number): void } | null;
    try { ws?.open?.(session.id); } catch { /* the remembered id still applies */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.id, session.seat]);

  /* THE INLINE PAD. A callback ref for the same reason the modal's is one: the
     host exists only while this branch renders, and the mount has to happen
     with the node rather than after a guess about when it appeared.

     It is remounted when the ROOM changes and not when the scene does — the pad
     owns its own scene once loaded, and re-loading it under the user's cursor
     because our poll returned would throw away the stroke in progress. */
  const inlinePad = useCallback((el: HTMLDivElement | null) => {
    const mod = window.Brainstorm;
    if (!mod?.mountPad) return;
    if (!el) { inlinePadRef.current?.destroy?.(); inlinePadRef.current = null; return; }
    inlinePadRef.current?.destroy?.();
    inlinePadRef.current = mod.mountPad(el, {
      sessionId: session.id,
      /* The count in the header comes off the stored scene, so it only moves
         when a save lands — which is also the only moment it is true. */
      onSave: (saved) => setSession((cur) => (
        cur.id === session.id ? { ...cur, drawing: saved?.drawing || cur.drawing } : cur)),
      onError: () => toast("the sketch did not save — the dashboard may be down", "warn"),
    });
  }, [session.id]);

  /* Whatever the pad wrote while it was open — elements, notes, a rename, even
     a filed plan — is read back on the way out, because this room drew all of
     it a second ago from a copy that is now stale. */
  function closePad() {
    setPadOpen(false);
    if (session.id) refresh(session.id, true);
  }

  const msgs = useMemo(() => session.messages || [], [session.messages]);

  /* READ ALOUD WHAT THE ROOM SAYS, once each, as it lands.
     This used to speak the POST's own reply, which no longer carries one. It
     now watches the transcript and speaks assistant turns that arrived after
     this component last looked — never the backlog of a room you have merely
     opened, which is the toggle nobody would leave on. */
  const spoken = useRef<number>(0);
  useEffect(() => {
    const assistants = msgs.filter((m) => m.role === "assistant");
    const last = assistants[assistants.length - 1];
    if (!last) return;
    if (!spoken.current) { spoken.current = last.id; return; }
    if (last.id === spoken.current) return;
    spoken.current = last.id;
    if (last.text) voice.speak(last.text);
  }, [msgs, voice]);
  const th = session.thinker || {};
  /* The room's own spend: every participant that ever answered, including the
     ones that have left. `thinker.spent_usd` is the owner's partner, which has
     no participant row of its own. */
  const spent = (session.participants || [])
    .reduce((n, p) => n + (p.spent_usd || 0), 0) + (th.spent_usd || 0);
  const items = plan?.items || [];
  /* Message id -> the seat it was promoted for. A set of ids was enough to grey
     the control out; the seat is what says which lane the item landed in, and
     the design puts that on the message. */
  const promoted = useMemo(() => {
    const by = new Map<number, string>();
    for (const i of items) if (i.from_msg) by.set(i.from_msg, i.seat);
    return by;
  }, [items]);

  /* ARRIVALS, DEPARTURES AND FILINGS ARE EVENTS IN THE TRANSCRIPT, not hidden
     state — 11 asks for that because in a room people join, a thing that
     happened between two messages has to be readable between those two
     messages. The only such events the backend records today are the room
     opening and each filing, so those are the only ones drawn. */
  const timeline = useMemo(() => {
    type Line =
      | { kind: "msg"; at: string; key: string; m: Msg }
      | { kind: "event"; at: string; key: string; icon: string; text: string };
    const lines: Line[] = msgs.map((m) => ({
      kind: "msg", at: m.created_at || "", key: `m${m.id}`, m,
    }));
    if (session.created_at)
      lines.push({ kind: "event", at: session.created_at, key: "open",
                   icon: "door-enter",
                   text: `room opened · ${session.seat || "director"} owns it` });
    for (const [i, d] of (session.deploys || []).entries()) {
      const n = d.items?.length || 0;
      lines.push({
        kind: "event", at: d.at || "", key: `d${i}`, icon: "send",
        text: `${n || "some"} item${n === 1 ? "" : "s"} filed to the board`
            + (d.by ? ` by ${d.by}` : ""),
      });
    }
    // Same stamp format everywhere, so a string compare IS a time compare.
    return lines.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
  }, [msgs, session.created_at, session.deploys, session.seat]);

  /* Keyed on the COUNT, not the array: the poll rebuilds these objects every
     three seconds and scrolling on that would fight the reader constantly. */
  const feed = useStickyBottom(timeline.length);

  /* Which seats this room may file for. It mirrors ALLOWED_SEATS in
     bgate_core/brainstorm.py: a narrative session may file for itself and
     nothing else, so offering it the full table would produce a menu whose
     every other entry is refused at file time, after you have typed a title. */
  const allowedSeats = session.seat === "narrative"
    ? ["narrative"]
    : (fileSeats.length ? fileSeats : ["director"]);
  const inviteable = (fileSeats.length ? fileSeats : [])
    .filter((s) => s !== (session.seat || "director"));
  /* The roster the server keeps. `left` rows stay in the table as history and
     are not people in the room, so they are filtered here rather than by the
     server — leaving and re-inviting is one seat, not two rows. */
  const guests = (session.participants || []).filter((p) => p.state !== "left");

  /* WHAT PROMOTING THIS WOULD TOUCH — derived, and labelled as derived.
   *
   * The reference shows a per-message `wouldTouch` ("would touch
   * game/scripts/combat.gd"). NOTHING PRODUCES THAT. A message is a role and a
   * blob of text; no seat, no file list, no static analysis of an opinion — and
   * a plausible filename under a promote button is the worst possible lie on
   * this screen, because it is exactly specific enough to be believed and it
   * would be read as a promise about what an agent is about to edit.
   *
   * The true statement in the same place is the LANE. A promoted item is filed
   * for a seat, that seat may only write inside its `write_globs`, and the hook
   * enforces it — so "an item filed for art can only land under these globs" is
   * a fact about the queue, not a guess about the text. It is written as a
   * bound, and the seat with no globs recorded says that rather than nothing. */
  function wouldTouch(seat: string): string {
    const g = lanes[seat];
    if (!g) return `${seat}: lane not read yet`;
    if (!g.length) return `${seat} has no write lane — it could touch anything`;
    return `${seat} may only write ${g.join(", ")}`;
  }

  const byS: Record<string, number> = {};
  for (const i of items) byS[i.seat] = (byS[i.seat] || 0) + 1;
  const breakdown = Object.entries(byS)
    .map(([s, n]) => `${s} ×${n}`).join(", ");

  return (
    <div className="bg4-room" ref={host}>
      <aside className="bg4-rooms">
        <div className="bg4-rooms-head">
          <span>Rooms</span>
          <Menu position="bottom-start" shadow="md" width={180}>
            <Menu.Target>
              <button className="bg4-mini" title="New room" disabled={busy === "new"}>
                <Ti name="plus" size={13} />
              </button>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>Who owns the new room</Menu.Label>
              {(roomSeats.length ? roomSeats : ["director"]).map((s) => (
                <Menu.Item key={s} onClick={() => newRoom(s)}
                           leftSection={<Ti name={SEAT_ICON[s] || "user"} size={14}
                                            color={SEAT_COLOR[s]} />}>
                  {s}
                </Menu.Item>
              ))}
            </Menu.Dropdown>
          </Menu>
        </div>
        <Stack gap={4} className="bg4-roomlist">
          {rooms.map((r) => {
            /* THE DOT IS THE ROOM'S STATE, not its seat — the design's rooms
               list reads down a column of dots to find the live one, and the
               seat is already spelled out in the meta line under it. */
            const dot = r.status === "archived" ? "var(--text-3)"
                      : r.status === "deployed" ? "var(--good)"
                      : "var(--accent)";
            const unread = Math.max(0, (r.messages ?? 0) - (seen[r.id] ?? 0));
            return (
              <button key={r.id}
                      className={r.id === session.id ? "bg4-roomrow on" : "bg4-roomrow"}
                      onClick={() => open(r.id)}>
                <span className="l">
                  <span className="t">{r.title || `room ${r.id}`}</span>
                  {!!unread && r.id !== session.id && (
                    <Tooltip withArrow openDelay={200} multiline w={230}
                             label={`${unread} turn${unread === 1 ? "" : "s"} `
                                  + "since you last had this room open in this "
                                  + "browser. Nothing on the server tracks who "
                                  + "has read what."}>
                      <span className="u">{unread}</span>
                    </Tooltip>
                  )}
                </span>
                {/* WHO IS IN THAT ROOM, AS A ROW OF DOTS. The rail's job is to
                    let you find "the one with gameplay and art in it" without
                    opening four rooms to look — and a seat is recognised by its
                    colour everywhere else in this app, so the dots need no
                    labels at this size. The title carries the names for anyone
                    who cannot use the colour. */}
                {!!(r.guests || []).length && (
                  <span className="g" title={`in the room: ${(r.guests || []).join(", ")}`}>
                    {(r.guests || []).map((s) => (
                      <i key={s} style={{ background: SEAT_COLOR[s] || "var(--text-3)" }} />
                    ))}
                  </span>
                )}
                <span className="m">
                  <i style={{ background: dot }} />
                  {r.seat || "director"} · {r.messages ?? 0} turns
                  {r.spent_usd ? ` · $${r.spent_usd.toFixed(2)}` : ""}
                  {r.updated_at ? ` · ${ago(r.updated_at)}` : ""}
                </span>
              </button>
            );
          })}
          {!rooms.length && (
            <Text size="xs" c="dimmed">
              no rooms yet — the + above opens one, and it starts empty
            </Text>
          )}
        </Stack>
        <div className="bg4-rooms-foot">
          {/* THE RUNNER, AND NOT THE PRICE. The cap was the only number in this
              column and it was the one nobody was asking for — the owner's call:
              a thinking room should not open with a budget line. The cap is
              still enforced server-side; it is just not what the room greets
              you with. */}
          {th.label || [th.runner, th.model].filter(Boolean).join(" · ") || "runner not reported"}
        </div>
      </aside>

      <section className="bg4-roomtalk">
        <header className="bg4-roomhead">
          <Ti name="users-group" size={16} color={SEAT_COLOR[session.seat || "director"]} />
          <div>
            <div className="t">{session.title || "the room"}</div>
            <div className="m">
              {/* ONE NUMBER FOR ONE WORD. `thinker.turns` counts the live
                  process's turns and the sidebar counts stored messages, so a
                  room read "3 turns" in the header and "20 turns" in the list
                  beside it. The transcript is the thing both are describing. */}
              {msgs.length} turns
              {/* WHAT THE ROOM HAS COST, beside what it has said, because those
                  are the two facts that decide whether to keep going. Summed
                  from the participant rows — a seat that LEFT keeps its spend,
                  so a room cannot become cheaper by tidying the roster. Absent
                  until something has been spent rather than drawn as $0.00,
                  which reads as a measurement of a room nobody has run. */}
              {spent > 0 && <> · ${spent.toFixed(2)}</>}
            </div>
          </div>
          {/* THE PROMISE, ON THE FACE OF THE ROOM — read from the field that
              actually asserts it.

              THIS BADGE WAS DERIVED FROM ABSENCE. It keyed on `tools.length ===
              0` and painted a green "no tools in this room". But `tools` is
              built from the LIVE PROCESS TABLE (brainsession.py) and is empty
              whenever no turn is in flight — which is every room you have just
              opened. So the reassuring green was synthesised from a field
              nobody had filled in, and a room that genuinely HAD tools would
              have shown it too until someone took a turn. Same failure as the
              invented progress bar, in a place where the whole screen's claim
              rests on it.

              `readonly` is the assertion, and `readonly_by` is its receipt —
              the argv, verbatim, down to `--tools ""` and the two-tool pad
              server. Observed tools, when a turn IS live, override it: a
              contradiction between the two is the one thing worth shouting. */}
          {/* Only drawn where the column is gone (see room.css); at full width
              the roster is already on screen and a button to "open" it would be
              a control for something you are looking at. */}
          <button className="bg4-rosterbtn" onClick={() => setRosterOpen(true)}
                  title="Who is in this room">
            <Ti name="users" size={14} />
            {1 + (th.available ? 1 : 0) + guests.length}
          </button>
          <Tooltip multiline w={330} withArrow openDelay={200}
                   label={th.readonly_by
                     || (th.readonly
                         ? "spawned read-only; the runner did not report how"
                         : "this session has not said how it was spawned")}>
            <Badge size="sm" variant="light"
                   color={(th.tools?.length || 0) > 0 ? "red"
                          : th.readonly ? "teal" : "gray"}
                   leftSection={<Ti name="tools-off" size={11} />}>
              {(th.tools?.length || 0) > 0
                ? `${th.tools?.length} tools in this room`
                : th.readonly ? "spawned without tools" : "tool set not reported"}
            </Badge>
          </Tooltip>
        </header>

        {/* Follows the newest turn while you are at the bottom, and lets go the
            moment you scroll up to read something. A room of four seats posts
            four replies to one message; without this every one of them landed
            below the fold. */}
        <ScrollArea className="bg4-roomscroll" type="auto" viewportRef={feed}>
          {timeline.map((l) => l.kind === "event" ? (
            <div key={l.key} className="bg4-event">
              <Ti name={l.icon} size={13} />{l.text}
              <span className="when">{ago(l.at)}</span>
            </div>
          ) : (
            <div key={l.key}
                 /* WHO SAID IT, AS A COLOUR AND A RAIL, not only as a word.
                    A room of five voices rendered as five identical blocks with
                    a small grey label reads as one long monologue — you lose
                    the thread of who is arguing with whom, which is the only
                    reason to have more than one seat in here. The seat's colour
                    is the same one the roster, the invite chips and the rooms
                    rail already use, so the mapping is learned once. */
                 data-seat={l.m.seat || (l.m.role === "user" ? "" : "partner")}
                 style={l.m.seat
                   ? { ["--voice" as any]: SEAT_COLOR[l.m.seat] || "var(--text-3)" }
                   : undefined}
                 className={`bg4-msg ${l.m.role === "user" ? "you" : "them"}`
                   + (l.m.seat ? " seated" : "")}>
              <div className="head">
                {l.m.role !== "user" && (
                  <span className="av" aria-hidden="true">
                    <Ti name={l.m.seat ? (SEAT_ICON[l.m.seat] || "user")
                                       : "message-2"} size={12}
                        color={l.m.seat ? SEAT_COLOR[l.m.seat] : undefined} />
                  </span>
                )}
                {/* '' on a message means the room's own partner — see migration
                    0036. It is not a gap in the data. */}
                <span className="who" style={{ color: l.m.seat
                  ? SEAT_COLOR[l.m.seat] : undefined }}>
                  {l.m.seat || (l.m.role === "user" ? "you" : "partner")}
                </span>
                {l.m.role !== "user" && (
                  <span className="tag">{th.model || "partner"}</span>
                )}
                <span className="when">{ago(l.m.created_at)}</span>
              </div>
              <div className="txt">{l.m.text}</div>
              {l.m.role !== "user" && (
                <div className="acts">
                  {promoted.get(l.m.id) ? (
                    <span className="done">
                      <Ti name="check" size={12} /> promoted — waiting at the door
                    </span>
                  ) : (
                    <Menu position="bottom-start" shadow="md" width={260}>
                      <Menu.Target>
                        <button className="bg4-promote">
                          <Ti name="arrow-bar-to-down" size={12} />promote
                        </button>
                      </Menu.Target>
                      <Menu.Dropdown>
                        <Menu.Label>File it as whose work?</Menu.Label>
                        {allowedSeats.map((s) => (
                          <Menu.Item key={s}
                                     onClick={() => promote(
                                       l.m, s, l.m.seat
                                         || (l.m.role === "user" ? "you" : "the partner"))}
                                     leftSection={<Ti name={SEAT_ICON[s] || "user"}
                                                      size={14} color={SEAT_COLOR[s]} />}>
                            {s}
                            {/* The lane rides on the entry it belongs to:
                                which seat you pick IS the answer to what this
                                could touch, so it is read at the moment of
                                choosing rather than after. */}
                            <span className="bg4-lane">
                              {(lanes[s] || []).join(", ") || "no write lane recorded"}
                            </span>
                          </Menu.Item>
                        ))}
                      </Menu.Dropdown>
                    </Menu>
                  )}
                  {/* WHAT IT WOULD TOUCH, ON THE MESSAGE. Determinate only once
                      there is one seat it could be — after promotion, or in a
                      narrative room where the seat table is one row long. With
                      eight seats on offer the honest line is that the menu
                      decides, and each entry carries its own lane. */}
                  <span className="touch">
                    {promoted.get(l.m.id)
                      ? wouldTouch(promoted.get(l.m.id) as string)
                      : allowedSeats.length === 1
                        ? wouldTouch(allowedSeats[0])
                        : "what it touches is the seat you pick — each lane is in the menu"}
                  </span>
                  <span className="hint">an opinion until you promote it</span>
                </div>
              )}
            </div>
          ))}

          {/* THE ROOM IS ANSWERING. Not a disabled composer — you can queue your
              next thought while the seats talk, and the room refuses a second
              round itself if one is already running. */}
          {session.answering && (
            <div className="bg4-thinking">
              <span className="dots"><i /><i /><i /></span>
              {guests.length
                ? `${guests.length + 1} voices are answering — each one takes a turn`
                : "the room is answering"}
            </div>
          )}
          {!msgs.length && (
            <Text size="sm" c="dimmed" p="md" maw={560}>
              Think out loud. Everyone in this room answers without tools — no
              work item, no dispatch, no generator — until you promote something
              and open the door below.
            </Text>
          )}

          {plan && (
            <div className="bg4-plan">
              <div className="h">
                <span>Waiting at the door</span>
                <span className="sub">promoted, not filed</span>
              </div>
              <p className="why">
                Read it, cut what you disagree with, then file. Nothing below
                exists on the board yet — promoting and synthesizing both wrote
                nothing, and this list disappears if you leave the room without
                filing.
              </p>
              {plan.summary && <p className="sum">{plan.summary}</p>}
              <Stack gap={6}>
                {items.map((p, i) => (
                  <div key={i} className="row">
                    <span className="n">{i + 1}</span>
                    <span className="seat" style={{ color: SEAT_COLOR[p.seat] }}>
                      {p.seat}
                    </span>
                    <span className="ttl">{p.title}</span>
                    {p.from_who && <span className="note">said by {p.from_who}</span>}
                    <button className="cut" title="cut this one"
                            onClick={() => cut(i)}>×</button>
                  </div>
                ))}
                {!items.length && (
                  <Text size="xs" c="dimmed">
                    nothing promoted yet — a reply's promote control puts it here
                  </Text>
                )}
              </Stack>
              {!!plan.notes?.length && (
                <ul className="fixes">
                  {plan.notes.map((n, i) => <li key={i}>{n}</li>)}
                </ul>
              )}
            </div>
          )}
        </ScrollArea>

        {/* The addressed-to pill, and the sentence that keeps it honest. It sits
            ABOVE the composer rather than inside the menu because the claim it
            corrects — "I have just summoned the art seat" — is one you would
            otherwise carry all the way to the reply. */}
        {asked && (
          <div className="bg4-asked">
            <Ti name="at" size={13} color={SEAT_COLOR[asked]} />
            <b>{asked}</b>
            <span>
              answers this one alone. It holds no lane and no tools either way;
              what it says is an opinion until you promote it.
            </span>
            <button className="x" onClick={() => setAsked(null)}
                    title="ask the room instead">×</button>
          </div>
        )}
        <div className="bg4-roomcomposer">
          <Menu position="top-start" shadow="md" width={230}>
            <Menu.Target>
              <button className="bg4-ask" title="Address this to one seat">
                <Ti name="at" size={14} />ask one seat
              </button>
            </Menu.Target>
            <Menu.Dropdown>
              {/* ONLY SEATS THAT ARE IN THE ROOM. `to` naming an absent seat is
                  refused before the message is stored, so offering the whole
                  seat table here would be a menu whose entries mostly produce a
                  400 after you have typed the question. */}
              <Menu.Label>Address the next message to</Menu.Label>
              {guests.map((p) => (
                <Menu.Item key={p.seat} onClick={() => setAsked(p.seat)}
                           leftSection={<Ti name={SEAT_ICON[p.seat] || "user"} size={14}
                                            color={SEAT_COLOR[p.seat]} />}>
                  {p.seat}
                </Menu.Item>
              ))}
              {!guests.length && (
                <Menu.Item disabled>
                  nobody has joined — invite a seat first
                </Menu.Item>
              )}
              <Menu.Divider />
              <Menu.Item onClick={() => setAsked(null)}
                         leftSection={<Ti name="users-group" size={14} />}
                         rightSection={guests.length
                           ? <span className="bg4-menun">{guests.length + 1} answer</span>
                           : undefined}>
                the room
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
          <Textarea autosize minRows={1} maxRows={6} variant="unstyled"
                    placeholder={voice.micLive
                      ? "listening — speak, or press Escape to stop"
                      : "say something to the room — everyone in it may answer"}
                    value={voice.heard ? `${text}${text ? " " : ""}${voice.heard}` : text}
                    onChange={(e) => setText(e.currentTarget.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); say(); }
                    }} />

          {/* THE TWO VOICE TOGGLES. Separate, because they are separate
              permissions and separate costs: the mic opens a capture device and
              bills per minute, the speaker bills per character and opens
              nothing. One control for both would mean arming a microphone to
              hear a reply. */}
          <Tooltip withArrow multiline w={260} openDelay={200}
                   label={voice.status?.available
                     ? (voice.micLive
                         ? `listening · ${voice.micState || "live"} — Escape stops it`
                         : `talk to the room — ${voice.status.listen_model || "Deepgram"}`
                           + (voice.status.usd_per_minute
                              ? `, $${voice.status.usd_per_minute}/min` : ""))
                     : voice.status?.reason || "checking the voice relay…"}>
            {/* Kept mounted and disabled rather than hidden: an absent button
                cannot explain why there is no microphone, and "no Deepgram key"
                is the normal case on most installs. */}
            <button className={voice.micLive ? "bg4-voice hot" : "bg4-voice"}
                    disabled={!voice.status?.available}
                    aria-pressed={voice.micLive}
                    aria-label={voice.micLive ? "stop listening" : "talk to the room"}
                    onClick={voice.toggleMic}>
              <Ti name={voice.micLive ? "microphone" : "microphone-2"} size={15} />
              {voice.micLive ? "listening" : "talk"}
            </button>
          </Tooltip>

          <Tooltip withArrow multiline w={260} openDelay={200}
                   label={voice.status?.available
                     ? `read replies out loud — ${voice.status.speak_model || "Deepgram"}`
                       + (voice.status.usd_per_1k_chars
                          ? `, $${voice.status.usd_per_1k_chars}/1k chars` : "")
                     : voice.status?.reason || "checking the voice relay…"}>
            <button className={voice.tts ? "bg4-voice on" : "bg4-voice"}
                    disabled={!voice.status?.available}
                    aria-pressed={voice.tts}
                    aria-label={voice.tts ? "stop reading replies aloud"
                                          : "read replies aloud"}
                    onClick={voice.toggleTts}>
              <Ti name={voice.tts ? "volume" : "volume-off"} size={15} />
              {/* THE LABEL NAMES THE STATE, NOT THE ACTION — "silent" is what
                  the room is doing, the way the classic workspace said it. A
                  button reading "speak" while silent and "silent" while
                  speaking is the toggle everybody clicks the wrong way. */}
              {voice.tts ? "aloud" : "silent"}
            </button>
          </Tooltip>

          <Button size="compact-sm" onClick={say} loading={busy === "say"}
                  disabled={!text.trim()}>say</Button>
        </div>

        {/* THE DOOR. Permanent, shut by default, ember only once something is
            waiting — the one exit this room has, stated rather than implied.
            "promoted, 0 filed" is not decoration: promoted work is still only an
            opinion you picked out, and the count is the difference. */}
        <footer className={items.length ? "bg4-door open" : "bg4-door"}>
          <Ti name={items.length ? "door-exit" : "lock"} size={18} />
          <div className="w">
            <div className="t">
              {items.length
                /* THE FILED SIDE WAS A LITERAL ZERO while `session.deploys` —
                   already read, already drawn in the timeline above — held the
                   real answer. On a room with a filing behind it the door
                   insisted nothing had ever left, which is the one claim this
                   footer exists to make truthfully. */
                ? `${items.length} opinion${items.length === 1 ? "" : "s"} promoted, `
                  + `${(session.deploys || []).length} filed`
                : "One door out, and it is shut"}
            </div>
            <div className="n">
              {items.length
                ? `${breakdown} — each keeps the seat that said it as provenance`
                : "nothing said here queues, dispatches or spends until you file a plan"}
            </div>
          </div>
          <Group gap="xs" wrap="nowrap">
            <Button variant="default" size="compact-sm" onClick={synthesize}
                    loading={busy === "synth"} disabled={!msgs.length}
                    leftSection={<Ti name="wand" size={13} />}>
              synthesize
            </Button>
            <Button size="compact-sm" onClick={file} loading={busy === "file"}
                    disabled={!items.length}
                    leftSection={<Ti name="send" size={13} />}>
              file plan
            </Button>
          </Group>
        </footer>
      </section>

      <aside className={rosterOpen ? "bg4-roster open" : "bg4-roster"}>
        {/* Closes the drawer form; inert (and invisible) as a column. */}
        <button className="bg4-rosterclose" onClick={() => setRosterOpen(false)}
                title="Close">×</button>
        <div className="bg4-padhead">
          <Ti name="users" size={14} /><span>In the room</span>
          <span className="sub">{1 + (th.available ? 1 : 0) + guests.length}</span>
        </div>

        {/* THE ROSTER IS EVERY PARTICIPANT THAT EXISTS, and there are two. The
            invited-seat rows the design shows are not drawn from nothing — see
            the empty line below, which names what would fill them. */}
        <div className="bg4-part">
          <Ti name={SEAT_ICON[session.seat || "director"] || "user-star"} size={15}
              color={SEAT_COLOR[session.seat || "director"]} />
          <div className="w">
            <div className="l">
              <span className="nm">you · {session.seat || "director"}</span>
              <span className="st own">owns the room</span>
            </div>
            <div className="note">the only one who can open the door</div>
          </div>
        </div>

        <div className="bg4-part">
          <Ti name="message-2" size={15} />
          <div className="w">
            <div className="l">
              <span className="nm">the partner</span>
              <span className={th.live ? "st live" : "st"}>
                {th.available === false ? "unavailable" : th.live ? "live" : "idle"}
              </span>
            </div>
            <div className="note">
              {th.label || [th.runner, th.model].filter(Boolean).join(" · ") || "runner not reported"}
              {" · "}
              {/* NOT "no tools, no MCP" — those arrays are empty because
                  nothing is running, and the session's own readonly_by says it
                  attaches a two-tool PAD server, so "no MCP" was also wrong on
                  its face. Say what is asserted, or say it is unobserved. */}
              {(th.tools?.length || 0) > 0
                ? `${th.tools?.length} tools observed`
                : th.readonly ? "read-only argv" : "tool set unobserved"}
            </div>
          </div>
        </div>

        {/* THE GUESTS. Each one is a real spawned CLI with the room's own
            read-only argv, so each one carries its own turn count and its own
            spend — which is the whole reason the design put a number on a
            participant row rather than one number on the room. */}
        {guests.map((p) => (
          <div className="bg4-part" key={p.seat}>
            <Ti name={SEAT_ICON[p.seat] || "user"} size={15} color={SEAT_COLOR[p.seat]} />
            <div className="w">
              <div className="l">
                <span className="nm">{p.seat}</span>
                {/* BOTH MEANINGS, because they disagree and the difference is
                    actionable: "in the room, not running" is normal and self-
                    healing; "invited" means nothing ever started. */}
                <span className={p.live ? "st live" : "st"}>
                  {p.state === "invited" ? "invited"
                    : p.live ? "live" : "in the room · not running"}
                </span>
              </div>
              <div className="note">
                {p.thinker?.label
                  || [p.thinker?.runner, p.thinker?.model].filter(Boolean).join(" · ")
                  || "no process has started for this seat yet"}
                {" · "}
                {(p.thinker?.tools?.length || 0) > 0
                  ? `${p.thinker?.tools?.length} tools observed`
                  : "read-only argv"}
                {p.turns ? ` · ${p.turns} turn${p.turns === 1 ? "" : "s"}` : ""}
              </div>
            </div>
            {/* EACH REPLY COSTS, AND THE COST IS PER SEAT. Every guest is its
                own spawned CLI, so one number on the room would hide which
                invitation is the expensive one — which is the only version of
                this number anybody can act on. Drawn only once something has
                been spent: "$0.00" beside a seat that has not answered yet
                reads as a measurement rather than as an absence. */}
            {!!p.spent_usd && (
              <span className="spend" title={`${p.seat} has spent this much answering in this room`}>
                ${p.spent_usd.toFixed(2)}
              </span>
            )}
            <button className="x" title={`${p.seat} leaves the room`}
                    onClick={() => leave(p.seat)}>×</button>
          </div>
        ))}

        {!guests.length && (
          <div className="bg4-part empty">
            <Ti name="user-off" size={15} />
            <div className="w">
              <div className="note">
                no seats have joined yet. An invited seat answers as that craft
                — it holds no lane, writes nothing, and what it says is an
                opinion until you promote it.
              </div>
            </div>
          </div>
        )}

        {/* FREE DISCUSSION — the room answering itself instead of only you.
            Off by default and never sticky across rooms: each extra round is
            one billed CLI turn per voice present, and that is a decision the
            person paying makes per room, with the roster in front of them. */}
        <div className="bg4-padhead bordered">
          <Ti name="messages" size={14} /><span>Free discussion</span>
          <span className="sub">
            {(session.discuss_rounds || 0)
              ? `up to ${session.discuss_rounds} more round${session.discuss_rounds === 1 ? "" : "s"}`
              : "off"}
          </span>
        </div>
        <div className="bg4-discuss">
          <div className="steps" role="group" aria-label="Follow-up rounds">
            {DISCUSS_STEPS.map((n) => (
              <Tooltip key={n} withArrow multiline w={280} openDelay={200}
                       label={n === 0
                         ? "Everyone answers you once, then the room stops. What it has always done."
                         : `After the first answers, the room keeps talking for up to ${n} more round${n === 1 ? "" : "s"} — each voice reads what the others just said and replies only if it has something to add. A round where everybody passes ends it early. Costs up to ${n} turn${n === 1 ? "" : "s"} per voice, per message.`}>
                <button className={(session.discuss_rounds || 0) === n ? "b on" : "b"}
                        aria-pressed={(session.discuss_rounds || 0) === n}
                        disabled={!session.id}
                        onClick={() => setDiscuss(n)}>
                  {n === 0 ? "off" : `${n}×`}
                </button>
              </Tooltip>
            ))}
          </div>
          <div className="note">
            {guests.length
              ? (session.discuss_rounds || 0)
                ? `${guests.length + 1} voices reply to each other after they answer you. Asking one seat still gets one answer.`
                : "one answer each, then the room waits for you."
              : "invite a seat first — a room of one has nobody to discuss with."}
          </div>
        </div>

        <div className="bg4-padhead bordered">
          <Ti name="user-plus" size={14} /><span>Invite</span>
          {/* THE PRICE OF THE ROOM, NOT OF A CLICK. Every guest answers every
              unaddressed message, so the cost of a sentence scales with the
              roster — say it here, where the roster is being changed. */}
          <span className="sub">
            {guests.length ? `${guests.length} in the room` : "seats answer as their craft"}
          </span>
        </div>
        <div className="bg4-invite">
          {inviteable.map((s) => {
            const here = guests.some((p) => p.seat === s);
            const full = guests.length >= MAX_GUESTS && !here;
            return (
              <Tooltip key={s} withArrow multiline w={260} openDelay={200}
                       label={here
                         ? `${s} is in the room — click to send it away`
                         : full
                         ? `The room is full at ${MAX_GUESTS} guests — every one of `
                           + "them answers every unaddressed message, one at a time."
                         : `Spawn ${s} into this room: no tools, no board, no lane. `
                           + "It reads the room and the pads and answers as that "
                           + "craft, and its opinion reaches the board only if you "
                           + "promote it."}>
                <button className={here ? "chip on" : full ? "chip off" : "chip"}
                        aria-pressed={here} disabled={busy === "invite"}
                        onClick={() => (here ? leave(s) : full ? undefined : invite(s))}>
                  <Ti name={SEAT_ICON[s] || "user"} size={12} color={SEAT_COLOR[s]} />
                  {s}
                </button>
              </Tooltip>
            );
          })}
          {!inviteable.length && (
            <Text size="xs" c="dimmed">
              the project's seat table is what fills this — none read yet
            </Text>
          )}
        </div>

        {/* YOUR PADS. 8a put them in a column of their own; one room needs that
            column for the people in it, so the pad moves under the roster and
            keeps its label. It is still the one thing in here that is yours. */}
        <div className="bg4-padhead bordered">
          <Ti name="pencil" size={14} /><span>Your notes</span>
          <span className="sub">yours · the room may read</span>
        </div>
        <Textarea className="bg4-notes" variant="unstyled" autosize minRows={5}
                  placeholder="what is actually wrong, in your words"
                  value={notes} onChange={(e) => setNotes(e.currentTarget.value)}
                  onBlur={saveNotes} />
        {/* 9/9a — THE SKETCH PAD GOES IN A NARRATIVE ROOM. Not hidden as a
            tidy-up: that seat argues in sentences, and its column carries the
            canon a proposal may not contradict instead. The notes pad stays,
            because sentences are what it writes. */}
        {isNarrative ? (
          <CanonColumn seat={session.seat} sessionId={session.id} />
        ) : (
        <>
        {/* YOUR SKETCH — THE REAL SURFACE, IN THE COLUMN.
            It used to be a 60px thumbnail with a "draw" button that opened the
            whole workspace over the page: to move two boxes you left the room,
            and the owner's verdict was that a pad you cannot draw on is not a
            pad. `Brainstorm.mountPad` gives us the SAME Pad object the
            workspace uses — same surface, same scene format, same PATCH — in a
            host of our own size. There is no second implementation. */}
        <div className="bg4-padhead bordered">
          <Ti name="pencil-plus" size={14} /><span>Your sketch</span>
          <span className="sub">
            {drawn.length} element{drawn.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="bg4-sketch">
          <div className="live" ref={inlinePad} />
          <div className="acts">
            <button className="b" onClick={() => setPadOpen(true)}
                    title="Open the pad full width, with the conversation beside it">
              <Ti name="arrows-maximize" size={12} />bigger
            </button>
            <button className="b" onClick={clearSketch} disabled={!drawn.length}>
              <Ti name="eraser" size={12} />clear
            </button>
            {/* .excalidraw because that is genuinely what comes out — the pad
                stores Excalidraw's own scene shape, so this file opens. */}
            <Tooltip withArrow openDelay={300} multiline w={220}
                     label="Downloads the stored scene as a .excalidraw file —
                            the same JSON the pad reads, unmodified.">
              <button className="b" onClick={exportScene} disabled={!drawn.length}>
                <Ti name="download" size={12} />export
              </button>
            </Tooltip>
          </div>
        </div>
        </>
        )}

        {/* THE RULE, IN WORDS, AT THE FOOT OF THE ROSTER. Outside the branch
            above on purpose: it is true of every room, and a narrative room —
            which trades its sketch pad for canon — needs it just as much. A
            room is only cheap and only safe while what is in it cannot act. */}
        <div className="bg4-rule">
          <Ti name="tools-off" size={15} />
          <div>
            A seat in here is spawned without its tools. It can read the room,
            the pads and its own lane — it cannot write, run, or queue. What it
            says is an opinion until you promote it.
          </div>
        </div>
      </aside>

      {/* THE REAL WORKSPACE, FULL SIZE, OVER THE ROOM.
          Its own header names the session, so if a future change to the
          module's session-picking ever put you somewhere else, the modal says
          which room you are actually drawing on rather than this component
          insisting it is the right one. */}
      <Modal opened={padOpen} onClose={closePad} size="90vw"
             padding={0} radius="md" className="bg4-padmodal"
             title={`${session.title || `room ${session.id}`} — pads`}>
        {window.Brainstorm ? (
          <div className="bg4-padhost" ref={padHost} />
        ) : (
          <div className="bg4-padnone">
            brainstorm.js is not on this build. The drawing pad, its undo and its
            scene serialisation all live in that file — there is no React copy of
            it, deliberately, because a second editor over one scene is how two
            people overwrite each other's elements.
          </div>
        )}
      </Modal>
    </div>
  );
}
