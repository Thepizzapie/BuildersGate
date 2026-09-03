import { useCallback, useState } from "react";
import { Ti } from "../Ti";
import { Head, Nothing, Tag, ReadError } from "./prims";
import { useJSON, ago } from "./api";
import { readJSON, mutate, askText } from "../../bridge";
import { setSelection } from "../selection";
import { useEvents, FALLBACK_MS } from "../../hooks";
import type { SeatBodyProps } from "./types";
import "./director.css";

/* DIRECTOR — rooms, rulings, and the two lists nobody else keeps.
 *
 * The seat's own brief says a settled decision NAMES ITS ACCEPTANCE TEST AND
 * WHAT IT LEAVES DARK, and that an unsaid no gets built anyway. Those are two
 * lists with a shape, so they get furniture with that shape rather than a
 * kanban lane called "director".
 *
 * WHERE THE DATA IS. All of it is real now. The register and the no-list were
 * read out of the generic per-seat document store — a schemaless JSON blob no
 * tool could append one row to — which is why this panel was empty on every
 * project that has ever existed. They have their own tables (migration 0037),
 * their own module (bgate_core/decisions.py), their own routes
 * (/api/decisions/*, /api/not-building) and their own MCP tools. "Awaiting a
 * ruling" was always real and still is: the console's gate list and its open
 * ask_human questions come down one endpoint, and open PROPOSALS now join them
 * there, because a proposal is by definition a thing waiting on a human.
 *
 * THE SPLIT THIS PANEL DRAWS IS A PERMISSION BOUNDARY, NOT A LAYOUT CHOICE.
 * Agents may read both lists and may PROPOSE; only a human settles a decision
 * or writes a line on the no-rail. That is why the Settle button lives beside
 * the proposal rather than the proposal being edited in place — settling is one
 * act with one owner, and the rail is where the human meets it.
 *
 * A RAIL THAT LISTS WHAT IS WAITING AND CANNOT ACT ON IT IS A NOTIFICATION,
 * NOT A DESK, and that is what this was. Three of the four things in "Awaiting
 * a ruling" had an endpoint written for exactly this seat and no control here
 * that reached it:
 *
 *   an ask_human question  POST /api/console/answer, whose own docstring calls
 *                          it "answer a DIRECTOR'S question". Drawn as a line
 *                          of text with nothing to type into. The agent that
 *                          asked is either still running and steerable or about
 *                          to leave a handoff note — both windows close while
 *                          the question sits here being read.
 *   a sign-off gate        POST /api/console/signoff (accept | reopen). Under
 *                          the builder's gate a completion PARKS in 'review'
 *                          and the chain behind it does not move until a human
 *                          acts. This panel drew that as one indistinguishable
 *                          grey row: a stopped chain, on the arbitration seat,
 *                          with no control and no word saying it was stopped.
 *   a superseded ruling    POST /api/decisions/{id}/supersede. The register
 *                          renders a whole Superseded section that nothing in
 *                          the product could ever produce.
 *
 * So every row here now carries the act it is waiting for. The one exception is
 * an art candidate, which is human-only to promote but is promoted in Assets
 * against the image — approving a picture from a 340px rail that cannot show it
 * is a rubber stamp — so that row says where it goes and takes you there.
 */

type Decision = {
  id: number; title: string; acceptance: string; leaves_dark: string;
  state: string; actor?: string; work_item_id?: number | null;
  superseded_by?: number | null; created_at?: string;
};
type NoRow = {
  id: number; text: string; reason: string; tag?: string; actor?: string;
  decision_id?: number | null; created_at?: string;
};
type Overview = {
  decisions: Decision[]; open: Decision[]; superseded: Decision[];
  not_building: NoRow[];
};
/* The gate shape is _gates() in bgate_ui/routes/console.py, whole. The fields
   this panel used to declare were the four that happen to be common to all
   three kinds, which is why a parked sign-off and a finished one looked the
   same: `parked` was on the wire the entire time. */
type Gate = {
  id?: string; kind?: string; title?: string; seat?: string;
  item_id?: number | null; over_item_id?: number | null; artifact_id?: number;
  status?: string; parked?: boolean; result?: string; path?: string;
  blocking?: boolean; gate_mode?: string; created_at?: string;
};
type Question = {
  event_seq?: number; question?: string; seat?: string; asked_at?: string;
  item_id?: number; refs?: string[]; asked_by?: string;
};
type ConsoleState = { gates?: Gate[]; questions?: Question[] };

const EMPTY: Overview = { decisions: [], open: [], superseded: [], not_building: [] };
const NO_CONSOLE: ConsoleState = { gates: [], questions: [] };

/* Not the shared useJSON, and the difference is the reason: usePoll holds its
 * callback in a ref and rebuilds the interval only when the CADENCE changes, so
 * nothing in the shared hook can force a read NOW. Every write on this panel has
 * to be visible immediately — a Settle that leaves the proposal sitting in the
 * rail for twenty seconds reads as a failed click, and the second click files
 * the ruling twice. Owning the loader gives the mutations something to call.
 *
 * The console read is the same hook for the same reason, which it was not:
 * answering a question or signing off left the card on screen for a further
 * five seconds, which is exactly long enough to answer it twice — and the
 * second answer comes back 409, so the person is told their click failed after
 * it worked. */
function usePolled<T extends Record<string, unknown>>(
  path: string, fallback: T, ms: number, active: boolean,
) {
  const [view, setView] = useState<T & { __error?: string }>(fallback);
  const load = useCallback(async () => {
    setView(await readJSON<T>(path, fallback));
    // fallback is a fresh literal per render; it is only ever read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);
  useEvents(load, { enabled: active, fallbackMs: Math.max(ms, FALLBACK_MS) });
  return { view, load };
}

/* ── the two authoring forms ───────────────────────────────────────────────
 * One component, because the two forms differ only in their fields and their
 * endpoint, and two near-identical copies drift the moment one of them gains a
 * validation message.
 *
 * THE HINTS ARE NOT DECORATION. The server refuses a blank acceptance test or a
 * blank left-dark, and it says why — but a form that only shows that refusal
 * after a submit has already taught the person to type anything at all to get
 * past it. The hint carries a real example so the first attempt is a real one.
 */
type FieldSpec = {
  key: string; label: string; hint?: string; placeholder?: string;
  lines?: number; optional?: boolean;
};

function Compose({ fields, path, ok, initial, extra, check, onDone, onCancel }: {
  fields: FieldSpec[]; path: string; ok: string;
  initial?: Record<string, string>;
  /* Body keys with no field — the not_building.decision_id link, which is the
     row that says WHICH proposal this refusal answers. It is a foreign key, not
     something to type. */
  extra?: Record<string, unknown>;
  /* A check that has to hit the network, run BEFORE the write. work_item_id is
     a real foreign key with PRAGMA foreign_keys=ON, so a mistyped number is not
     a validation error on the way in — it is an IntegrityError on the way out,
     which reaches the operator as "request failed · 500" and loses the
     decision they had just typed. Returns a sentence, or "". */
  check?: (values: Record<string, string>) => Promise<string>;
  onDone: () => void; onCancel: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(initial || {});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    setBusy(true);
    setErr("");
    if (check) {
      const bad = await check(values);
      if (bad) { setBusy(false); setErr(bad); return; }
    }
    /* `quiet` because the failure this form produces most often is "you left the
       acceptance test blank", and that sentence belongs under the field it is
       about, not in a toast that slides away while the person is still reading
       the form. */
    const r = await mutate(path, {
      method: "POST", body: { ...values, ...(extra || {}) }, quiet: true,
    });
    setBusy(false);
    if (!r.ok) { setErr(r.error || "the write failed"); return; }
    setValues({});
    onDone();
  };

  const missing = fields.some((f) => !f.optional && !(values[f.key] || "").trim());

  return (
    <div className="bgd-form">
      {fields.map((f) => (
        <label key={f.key}>
          <span>{f.label}{f.optional ? " (optional)" : ""}</span>
          {f.hint && <span className="hint">{f.hint}</span>}
          {f.lines ? (
            <textarea rows={f.lines} placeholder={f.placeholder}
                      value={values[f.key] || ""}
                      onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
          ) : (
            <input type="text" placeholder={f.placeholder}
                   value={values[f.key] || ""}
                   onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
          )}
        </label>
      ))}
      {err && <div className="err">{err}</div>}
      <div className="row">
        <span className="sp" />
        <button className="bgs-btn" onClick={onCancel} disabled={busy}>cancel</button>
        <button className="bgs-btn" onClick={submit} disabled={busy || missing}>
          {busy ? "…" : ok}
        </button>
      </div>
    </div>
  );
}

const DECISION_FIELDS: FieldSpec[] = [
  { key: "title", label: "the call", placeholder: "inventory is a fixed grid" },
  {
    key: "acceptance", label: "acceptance test", lines: 2,
    hint: "how anyone checks the call was honoured. Without one this is an " +
          "opinion, and nobody can tell later whether it held.",
    placeholder: "a 6x4 grid holds 24 stacks and refuses the 25th",
  },
  {
    key: "leaves_dark", label: "what it leaves dark", lines: 2,
    hint: "the part this call deliberately does NOT cover. A deferral nobody " +
          "labelled gets 'fixed' as a bug by the next agent that finds it.",
    placeholder: "says nothing about weight or encumbrance",
  },
  /* work_item_id has been a column on `decision` and an argument to
     decisions.add since the table shipped, and nothing in the product ever set
     it — so idx_decision_item indexed a column that was NULL on every row and
     "what was decided about this item" had no answer. It is optional because
     most rulings are about the project, not about one queued job. */
  {
    key: "work_item_id", label: "about work item", optional: true,
    hint: "the board item this ruling is about, if it is about one. It becomes " +
          "the link back from that item to why it was built this way.",
    placeholder: "225",
  },
];

const NO_FIELDS: FieldSpec[] = [
  { key: "text", label: "not building", placeholder: "online co-op" },
  {
    key: "reason", label: "why", lines: 2,
    hint: "an unexplained no is re-proposed every few weeks by somebody who " +
          "cannot see what was wrong with it.",
    placeholder: "one person cannot test netcode",
  },
  { key: "tag", label: "tag", optional: true, placeholder: "scope" },
];

const STATE_TONE = (s?: string) =>
  s === "open" ? "warn" : s === "superseded" ? "off" : "good";

/** Open the inspector on a work item. The register links rulings to board rows
 *  and the link was printed as dead grey text — the shell has one selection
 *  store and every other screen writes to it. */
const openItem = (id: number, title?: string, seat?: string) =>
  setSelection({ key: `i${id}`, kind: "item", itemId: id, title, seat });

/** Does that board item exist? Asked before filing a decision that links to it —
 *  see Compose's `check`. A blank field is not a link and is not checked. */
async function checkItem(values: Record<string, string>): Promise<string> {
  const raw = (values.work_item_id || "").trim().replace(/^#/, "");
  if (!raw) return "";
  if (!/^\d+$/.test(raw)) return `"${raw}" is not a work item number`;
  const got = await readJSON<{ id?: number }>(`/api/queue/${raw}`, {});
  if (got.__error || !got.id) {
    return `there is no work item #${raw} — leave it blank if this ruling is ` +
           `about the project rather than one queued job`;
  }
  return "";
}

export function Director(props: SeatBodyProps) {
  const { active, tab } = props;
  const decks = tab !== "pillars";
  const { view, load } = usePolled<Overview>(
    "/api/decisions/overview", EMPTY, 20000, active && decks);
  const { view: console_, load: reloadConsole } = usePolled<ConsoleState>(
    "/api/console/state", NO_CONSOLE, 5000, active && decks);
  /* /api/bible is enveloped and the page unwraps it: the kinds arrive at the
     top level. */
  const bible = useJSON<Record<string, { id: number; title: string; body: string }[]>>(
    "/api/bible", {}, 20000, active && tab === "pillars");

  const [filing, setFiling] = useState(false);
  const [refusing, setRefusing] = useState<null | { text?: string; decision_id?: number }>(null);
  const [answering, setAnswering] = useState<number | null>(null);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);

  const rows = view.decisions || [];
  const noes = view.not_building || [];
  const proposals = view.open || [];
  const old = view.superseded || [];
  /* A non-escalation QA gate is an AGENT reviewing an agent — it is queued work,
     not a thing waiting on a human, and console.py stamps exactly that as
     blocking:false. Everything left is something only a person can end. */
  const gates = (console_.gates || []).filter((g) => g.blocking !== false);
  const questions = console_.questions || [];
  const rulings = gates.length + questions.length + proposals.length;
  /* A read that FAILED is not a project with nothing in it, and the difference
     is the whole reason Nothing exists. Both rails drew their empty state off
     `!length` alone, so a 500 on /api/decisions/overview told the owner in bold
     that nobody had written down a single refusal. */
  const railDead = !!view.__error;
  const rulingsDead = railDead || !!console_.__error;

  const settle = async (id: number) => {
    await mutate(`/api/decisions/${id}/settle`, { method: "POST", ok: "settled" });
    load();
  };

  /* SUPERSEDING NAMES THE REPLACEMENT, and the server refuses without it: a
     replacement nobody named is a deletion. Asked as an id rather than picked
     from a list because the replacement is usually the ruling filed seconds ago
     and its number is on screen directly above. */
  const supersede = async (d: Decision) => {
    const said = await askText({
      title: `Supersede #${d.id}?`,
      body: `"${d.title}" stays in the register — it is what tells the next ` +
            `person this was already tried. Name the decision that replaced it.`,
      label: "the replacing decision's number",
      placeholder: String(d.id + 1),
      ok: "Supersede",
    });
    const by = Number((said || "").trim().replace(/^#/, ""));
    if (!said || !Number.isInteger(by) || by <= 0) return;
    await mutate(`/api/decisions/${d.id}/supersede`,
                 { method: "POST", body: { by_id: by }, ok: `superseded by #${by}` });
    load();
  };

  /* LIFTING A NO RELEASES WORK THAT WAS STOPPED, so it asks first — the house
     rule for anything destructive. Typed confirmation rather than a yes/no,
     because the rail rows are one line tall and a misplaced click on a dense
     list is exactly how a refusal disappears without anyone deciding to. */
  const lift = async (row: NoRow) => {
    const said = await askText({
      title: "Lift this refusal?",
      body: `"${row.text}" — every agent reads the no-list as binding, so ` +
            `lifting this releases work that was stopped.`,
      label: 'type "lift" to confirm',
      ok: "Lift",
    });
    if ((said || "").trim().toLowerCase() !== "lift") return;
    await mutate(`/api/not-building/${row.id}`, { method: "DELETE", ok: "lifted" });
    load();
  };

  /* ANSWERING IS THE POINT OF ask_human. The route decides itself where the
     answer goes — a steer into a live agent, a handoff note for a dead one — so
     there is nothing to choose here beyond the words. 409 means somebody
     already answered it; mutate surfaces that sentence and the reload clears
     the card. */
  const sendAnswer = async (q: Question) => {
    if (!answer.trim()) return;
    setBusy(true);
    const r = await mutate("/api/console/answer",
                           { method: "POST", body: { seq: q.event_seq, answer: answer.trim() },
                             ok: "answer delivered" });
    setBusy(false);
    if (r.ok) { setAnswering(null); setAnswer(""); }
    reloadConsole();
  };

  /* ACCEPT ENDS THE GATE; SEND-BACK REQUEUES THE ITEM with the reason appended
     to its brief. The reason is mandatory server-side and that is correct — a
     reopen with no reason spends another agent on the same guess — so it is
     asked for here rather than discovered as a 400. */
  const signoff = async (g: Gate, verdict: "accept" | "reopen") => {
    if (!g.item_id) return;
    let reason = "";
    if (verdict === "reopen") {
      const said = await askText({
        title: `Send #${g.item_id} back?`,
        body: `"${g.title}" returns to the queue and the next agent reads this ` +
              `reason in the brief. Say what is actually wrong with it.`,
        label: "what is wrong",
        ok: "Send back",
      });
      reason = (said || "").trim();
      if (!reason) return;
    }
    setBusy(true);
    await mutate("/api/console/signoff",
                 { method: "POST", body: { item_id: g.item_id, verdict, reason },
                   ok: verdict === "accept" ? "signed off" : "sent back" });
    setBusy(false);
    reloadConsole();
  };

  if (tab === "pillars") {
    /* THE KINDS ARE bible.KINDS AND THERE ARE FOUR OF THEM. This tab drew two
       and dropped `constraint` on the floor — thirteen rows on the project this
       was written against — which is the one kind that is purely this seat's
       business: a constraint is a standing no with a reason, the same act as
       the no-rail one tab to the left, written in the bible instead. */
    const pillars = bible.pillars || [];   // `pillars`. The singular key does not exist.
    const loop = bible.loop || [];
    const constraints = bible.constraints || [];
    const dead = !!bible.__error;
    const card = (p: { id: number; title: string; body: string }) => (
      <div className="bgs-card" key={p.id}>
        <div className="t">{p.title}</div>
        <div className="p">{p.body}</div>
      </div>
    );
    return (
      <div className="bgs-pad">
        <ReadError error={bible.__error} what="the bible" />
        <Head label="Pillars" hint="what the project is, in the order it decided them" />
        {!pillars.length && !dead && <Nothing what="no pillars written"
          how="bible_add('pillar', …) or the World bible screen writes them" />}
        {pillars.map(card)}
        {/* THE HEAD IS UNCONDITIONAL NOW. It was drawn only when the loop had
            rows, so a project that had never written one — the failure this
            seat's mission names first — saw no mention of the core loop at all,
            and the tab looked complete. */}
        <Head label="Core loop" hint="the thing the player does again" />
        {!loop.length && !dead && <Nothing what="no core loop written"
          how={"bible_add('loop', …) — this seat owns the pillars AND the loop, " +
               "and a loop nobody wrote down is argued fresh in every brainstorm"} />}
        {loop.map(card)}
        <Head label="Constraints" hint="what the project has bound itself to, in the bible" />
        {!constraints.length && !dead && <Nothing what="no constraints written"
          how={"bible_add('constraint', …). A constraint the bible does not " +
               "hold is one an agent cannot read before it breaks it"} />}
        {constraints.map(card)}
      </div>
    );
  }

  return (
    <div className="bgs-split" style={{ ["--rail" as string]: "340px" }}>
      <div className="bgs-main">
        <Head label="Settled decisions"
              hint="each names its acceptance test and what it leaves dark"
              right={
                <button className="bgs-btn" onClick={() => setFiling(!filing)}>
                  {filing ? "close" : "file a decision"}
                </button>
              } />
        <ReadError error={view.__error} what="the decision register" />
        {filing && (
          <Compose fields={DECISION_FIELDS} path="/api/decisions" ok="settle it"
                   check={checkItem}
                   onCancel={() => setFiling(false)}
                   onDone={() => { setFiling(false); load(); }} />
        )}
        {!rows.length && !view.__error && (
          <Nothing what="no decisions filed"
                   how={"file one above, or call decision_add — a call, the " +
                        "test that says it was honoured, and the part it " +
                        "deliberately leaves dark"} />
        )}
        {rows.map((d) => (
          <div className="bgs-card" key={d.id}>
            <div className="h">
              <span className="id">#{d.id}</span>
              <span className="t">{d.title}</span>
              <Tag tone={STATE_TONE(d.state)}>{d.state}</Tag>
            </div>
            {/* SIDE BY SIDE, not stacked. What a decision covers and what it
                pointedly does not are read against each other or not at all. */}
            <div className="bgs-two">
              <div className="lim good">
                <div className="k">Acceptance test</div>
                <div className="v">{d.acceptance}</div>
              </div>
              <div className="lim">
                <div className="k">Left dark</div>
                <div className="v">{d.leaves_dark}</div>
              </div>
            </div>
            <div className="bgd-meta">
              {d.actor && <span>{d.actor}</span>}
              {!!d.work_item_id && (
                <button className="bgd-link" title="open this work item in the inspector"
                        onClick={() => openItem(d.work_item_id as number, d.title)}>
                  item #{d.work_item_id}
                </button>
              )}
              <span className="sp" />
              <span>{ago(d.created_at)}</span>
              {/* The one act that retires a ruling without erasing it. Without
                  this button the Superseded section below could never fill. */}
              <button className="bgs-btn" onClick={() => supersede(d)}>superseded by…</button>
            </div>
          </div>
        ))}

        {!!old.length && (
          <Head label="Superseded"
                hint="kept, because 'we tried that' is the most useful row here" />
        )}
        {old.map((d) => (
          <div className="bgs-card bgd-old" key={d.id}>
            <div className="h">
              <span className="id">#{d.id}</span>
              <span className="t">{d.title}</span>
              <Tag tone="off">superseded</Tag>
            </div>
            <div className="was">
              replaced by #{d.superseded_by} — its acceptance test was:
              {" "}{d.acceptance}
            </div>
          </div>
        ))}
      </div>

      <div className="bgs-rail">
        <div className="bgs-railhead">
          <Ti name="ban" size={15} color="var(--bad)" />
          <span className="lb">Not building</span>
          <span className="hint">an unsaid no gets built</span>
          <button className="bgs-btn" onClick={() => setRefusing(refusing ? null : {})}>
            {refusing ? "close" : "add"}
          </button>
        </div>
        {/* The register's ReadError lives in the main column, and the no-list
            comes down the SAME endpoint — so a failed read used to blank this
            rail with the explanation two hundred pixels away in another scroll
            container. */}
        <ReadError error={view.__error} what="the no-list" />
        {refusing && (
          <div className="bgd-railform">
            {/* keyed on the proposal so that opening it from a DIFFERENT
                proposal remounts with that one's text, rather than keeping the
                first one's in state. */}
            <Compose key={`no${refusing.decision_id || 0}`}
                     fields={NO_FIELDS} path="/api/not-building" ok="say no"
                     initial={refusing.text ? { text: refusing.text } : undefined}
                     extra={refusing.decision_id ? { decision_id: refusing.decision_id } : undefined}
                     onCancel={() => setRefusing(null)}
                     onDone={() => { setRefusing(null); load(); }} />
          </div>
        )}
        {!noes.length && !refusing && !railDead && (
          <div className="bgs-railpad">
            <Nothing what="no refusals written down"
                     how={"add one above, or call not_building_add — each " +
                          "line is one thing this project has said no to, and " +
                          "why, so an agent can read it before building the no"} />
          </div>
        )}
        {noes.map((n) => (
          <div className="bgs-norow" key={n.id}>
            <Ti name="x" size={12} color="var(--bad)" />
            <div>
              <div className="t">{n.text}</div>
              <div className="bgd-why">{n.reason}</div>
              <div className="bgd-norow-act">
                {n.tag && <span className="tag">{n.tag}</span>}
                {/* WHO wrote a line every agent reads as binding. It is on the
                    row and was not on the screen. */}
                {n.actor && <span className="tag">{n.actor}</span>}
                {!!n.decision_id && <span className="tag">from #{n.decision_id}</span>}
                <span className="tag">{ago(n.created_at)}</span>
                <button className="bgs-btn" onClick={() => lift(n)}>lift</button>
              </div>
            </div>
          </div>
        ))}

        <div className="bgs-railhead top">
          <Ti name="gavel" size={15} color="var(--warn)" />
          <span className="lb">Awaiting a ruling</span>
          <span className="n">{rulings || ""}</span>
        </div>
        <ReadError error={console_.__error} what="the console" />
        {!rulings && !rulingsDead && (
          <div className="bgs-railpad">
            <Nothing what="nothing is waiting on you"
                     how={"proposals an agent filed, open ask_human questions, " +
                          "sign-off on finished work and escalated QA all land " +
                          "here — and each one is acted on from this rail"} />
          </div>
        )}
        {/* PROPOSALS FIRST. A gate blocks one work item; a proposal is a
            question about what the project IS, and it is the only one of the
            three that nothing will ever escalate on its own. */}
        {proposals.map((d) => (
          <div className="bgs-rule" key={`d${d.id}`}>
            <div className="t">{d.title}</div>
            {/* BOTH HALVES, because settling binds every other seat and the
                half that says what the proposal does NOT cover is the half a
                settler needs. Showing only the acceptance test asked for a
                signature on a page that was folded over. */}
            <div className="bgd-why"><b>test:</b> {d.acceptance}</div>
            <div className="bgd-why"><b>leaves dark:</b> {d.leaves_dark}</div>
            <div className="m">
              <span className="k">proposed</span>
              {d.actor && <span>{d.actor}</span>}
              <span className="sp" />
              <span>{ago(d.created_at)}</span>
            </div>
            <div className="bgd-norow-act">
              <button className="bgs-btn" onClick={() => settle(d.id)}>settle</button>
              {/* THE OTHER HALF OF A RULING. decisions.py is explicit that there
                  is no 'rejected' state: a proposal turned down becomes a line
                  on the no-list, linked back by not_building.decision_id, so the
                  next agent reads the no instead of re-proposing it. There was
                  no way to do that from here at all. */}
              <button className="bgs-btn"
                      onClick={() => setRefusing({ text: d.title, decision_id: d.id })}>
                refuse
              </button>
            </div>
          </div>
        ))}
        {/* QUESTIONS BEFORE GATES. An agent that called ask_human is either
            still running — in which case the answer arrives as a steer and the
            run continues — or about to exit, and the window for the cheap
            outcome is open only while it lives. A sign-off gate is patient in a
            way a live agent is not. */}
        {questions.map((q) => (
          <div className="bgs-rule" key={`q${q.event_seq}`}>
            <div className="t">{q.question}</div>
            <div className="m">
              <span className="k">question</span>
              {q.seat && <span className="who" style={{ color: `var(--c-${q.seat}, var(--text-3))` }}>{q.seat}</span>}
              {!!q.item_id && (
                <button className="bgd-link" onClick={() => openItem(q.item_id as number, q.question, q.seat)}>
                  item #{q.item_id}
                </button>
              )}
              <span className="sp" />
              <span>{ago(q.asked_at)}</span>
            </div>
            {/* The refs an agent attached are the files it is asking ABOUT.
                They came down the same payload and were dropped. */}
            {!!(q.refs || []).length && (
              <div className="bgd-refs">{(q.refs || []).map((r) => <span key={r}>{r}</span>)}</div>
            )}
            {answering === q.event_seq ? (
              <div className="bgd-answer">
                <textarea rows={3} autoFocus value={answer}
                          placeholder="the answer, in the words the agent will act on"
                          onChange={(e) => setAnswer(e.target.value)} />
                <div className="bgd-norow-act">
                  <span className="sp" />
                  <button className="bgs-btn" disabled={busy}
                          onClick={() => { setAnswering(null); setAnswer(""); }}>cancel</button>
                  <button className="bgs-btn" disabled={busy || !answer.trim()}
                          onClick={() => sendAnswer(q)}>answer</button>
                </div>
              </div>
            ) : (
              <div className="bgd-norow-act">
                {/* /api/console/answer is addressed by the event id and nothing
                    else, so a row without one cannot be answered — say that on
                    the button rather than opening a box that will 400. */}
                <button className="bgs-btn" disabled={!q.event_seq}
                        title={q.event_seq ? "" : "this question has no event id — nothing to answer against"}
                        onClick={() => { setAnswering(q.event_seq ?? null); setAnswer(""); }}>
                  answer
                </button>
              </div>
            )}
          </div>
        ))}
        {gates.map((g) => (
          <div className="bgs-rule" key={g.id}>
            <div className="t">{g.title}</div>
            <div className="m">
              <span className="k">{g.kind}</span>
              {g.seat && <span className="who" style={{ color: `var(--c-${g.seat}, var(--text-3))` }}>{g.seat}</span>}
              {/* PARKED IS NOT A DECORATION. Under the builder's gate a parked
                  item is a CHAIN STOPPED DEAD on this decision — every item
                  queued behind it waits — and it was drawn identically to a
                  finished item somebody might glance at. */}
              {g.parked && <Tag tone="bad" title="the chain behind this item is stopped until you act">chain stopped</Tag>}
              {!!g.item_id && (
                <button className="bgd-link" onClick={() => openItem(g.item_id as number, g.title, g.seat)}>
                  item #{g.item_id}
                </button>
              )}
              <span className="sp" />
              <span>{ago(g.created_at)}</span>
            </div>
            {g.kind === "signoff" && !!g.result && (
              <div className="bgd-why bgd-result">{g.result}</div>
            )}
            {g.kind === "signoff" && (
              <div className="bgd-norow-act">
                <button className="bgs-btn" disabled={busy}
                        onClick={() => signoff(g, "reopen")}>send back</button>
                <button className="bgs-btn" disabled={busy}
                        onClick={() => signoff(g, "accept")}>
                  {g.parked ? "approve — releases the chain" : "accept"}
                </button>
              </div>
            )}
            {/* AN ART CANDIDATE IS NOT RULED ON HERE, and saying so is the fix.
                Promotion is human-only, but it is a judgement about an IMAGE and
                this rail is 340px of text — an approve button here is a
                rubber stamp with a nicer name. The row names the file and opens
                the screen that can actually show it. */}
            {g.kind === "art" && (
              <>
                {g.path && <div className="bgd-why bgd-result">{g.path}</div>}
                <div className="bgd-norow-act">
                  <span className="bgd-note">only a human promotes a candidate — against the image</span>
                  <button className="bgs-btn"
                          onClick={() => window.setWorkspace?.("assets")}>open in Assets</button>
                </div>
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
