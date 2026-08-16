import { useCallback, useEffect, useState } from "react";
import { Ti } from "./Ti";
import { ResumeInCli } from "./ResumeInCli";
import { AgentMade } from "./AgentMade";
import { SEAT_COLOR } from "./nav";
import { setSelection, useSelection } from "./selection";
import { askText, mutate, readJSON, toast, watchAgent } from "../bridge";
import { say } from "./agents/api";
import { usePoll } from "../hooks";


/** Did this step fail? Read from the payload rather than from the step's own
 *  kind, because a tool that returns `{ok:false}` is recorded as a completed
 *  step — the run continues, and the only trace is inside the text. That is why
 *  a failing storyboard call could scroll past looking exactly like a passing
 *  one. Substring tests, not a parse: the payload may be truncated already, and
 *  a half-JSON string still carries the marker. */
function failed(raw: string): boolean {
  const t = raw.slice(0, 400);
  /* STRUCTURAL MARKERS ONLY. A bare /Error/ also matches a path called
     ErrorHandler.gd and a result that merely mentions one, and a step wrongly
     painted as failed is worse than one not painted at all: it sends the reader
     into a healthy call hunting a bug. `"ok": false` and a non-empty `"error"`
     are claims the payload makes about itself. */
  return /"ok"\s*:\s*false/.test(t)
      || /"error"\s*:\s*"[^"]/.test(t)
      || t.trimStart().startsWith("Traceback (most recent");
}

type Step = { kind: string; name?: string; text?: string; result?: string };
type Activity = { steps: Step[]; running: boolean; final?: { text?: string } | null };

/* â”€â”€ reading a step's result â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
 *
 * A tool result is a JSON blob on one line. Truncating it to 160 characters
 * shows the opening brace and the first key, which for a failure is
 * `{ "ok": false, "error": "RateLimitError: â€¦` â€” the answer starts exactly
 * where the slice ends. So the row is a control now, and these three decide
 * what it shows before you press it.
 */

/** Pretty-print if it parses, otherwise hand back the text unchanged. Never
 *  throws: a step's payload is whatever the agent wrote, which is often not
 *  JSON at all (a `say` step is prose). */
function pretty(raw: string): string {
  const t = raw.trim();
  if (!t.startsWith("{") && !t.startsWith("[")) return raw;
  try { return JSON.stringify(JSON.parse(t), null, 2); } catch { return raw; }
}

/** The collapsed line. Whitespace-collapsed so a pre-formatted blob does not
 *  render as one word followed by 140 spaces. */


/* A RUN, AS BEATS RATHER THAN AS A LOG.
 *
 * The feed is a flat list of say / tool / result, and it was drawn one row per
 * entry with a slice of raw JSON under each. A forty-step run was therefore
 * forty rows of plumbing with the agent's own sentences buried among them at
 * exactly the same weight - which is dense in the worst way: everything is
 * shown and nothing is legible.
 *
 * Two foldings, both of which remove rows without removing information:
 *
 *   a tool and the result that follows it are ONE beat. They always come in
 *   pairs, and "godot_run" then "result {ok:true...}" is one thing that
 *   happened, drawn as two. Halving the rows costs nothing because the pair's
 *   verdict - did it work - is the only part worth seeing at a glance, and it
 *   moves onto the tool's own row.
 *
 *   consecutive tool beats collapse into a run of chips. Eight file reads in a
 *   row is one activity, not eight events, and a reader scanning for what went
 *   wrong should not have to scroll past it.
 *
 * WHAT IS NEVER FOLDED IS WHAT THE AGENT SAID. `say` is the only entry written
 * for a human, so it keeps its own full-width row and its whole text. That is
 * the inversion this needed: prose promoted, plumbing compressed.
 */
type Beat =
  | { kind: "say"; text: string }
  | { kind: "tools"; calls: { name: string; raw: string; bad: boolean }[] };

function foldSteps(steps: Step[]): Beat[] {
  const out: Beat[] = [];
  for (let i = 0; i < steps.length; i++) {
    const s = steps[i];
    if (s.kind === "say") {
      const text = (s.text || "").trim();
      if (text) out.push({ kind: "say", text });
      continue;
    }
    if (s.kind === "result") {
      /* A result with no tool before it - the feed's ring dropped the call, or
         a re-dispatch cut the log. Kept rather than lost, under its own name. */
      const raw = s.text || s.result || "";
      out.push({ kind: "tools",
                 calls: [{ name: "result", raw, bad: failed(raw) }] });
      continue;
    }
    // A tool: take the result that follows it, if that is what follows it.
    const next = steps[i + 1];
    const raw = next && next.kind === "result"
      ? (next.text || next.result || "") : "";
    if (raw) i += 1;
    const call = { name: s.name || s.kind, raw, bad: failed(raw) };
    const last = out[out.length - 1];
    if (last && last.kind === "tools") last.calls.push(call);
    else out.push({ kind: "tools", calls: [call] });
  }
  return out;
}

export function Inspector() {
  const [sel] = useSelection();
  const [act, setAct] = useState<Activity>({ steps: [], running: false, final: null });
  const [busy, setBusy] = useState(false);

  const itemId = sel?.itemId;

  const refresh = useCallback(async () => {
    if (!itemId) { setAct({ steps: [], running: false, final: null }); return; }
    setAct(await readJSON<Activity>(`/api/agent-activity/${itemId}`,
                                    { steps: [], running: false, final: null }));
  }, [itemId]);

  // Only while something with a log is selected — an inspector describing an
  // event has nothing to poll, and a 3s request that always answers the same
  // empty object is the kind of cost nobody attributes later.
  /* GATED ON BEING OPEN, not merely on having a selection. Closing the panel
     leaves `sel` in place on purpose (re-opening should show what you were
     looking at), so keying the poll on itemId alone kept a 3s
     /api/agent-activity request running behind every other screen. */
  usePoll(refresh, 3000, !!itemId && !!sel);

  // Escape closes it. Bound while open only — a global key handler that runs
  // when the panel is shut is a keystroke stolen from whatever is focused.
  useEffect(() => {
    if (!sel) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setSelection(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sel]);

  /* CLOSED IS THE RESTING STATE. The panel stays mounted so the slide has
     something to animate and so the poll above keeps its identity across a
     re-selection; `open` is the only thing that moves. Rendering null instead
     would make every selection a fresh mount, which is a panel that jumps. */
  /* Which steps are expanded. Keyed by index and deliberately NOT persisted:
     the list is re-fetched on a poll and a step you opened is about to be
     followed by ten more — carrying the set across items would open the wrong
     rows on the next agent you click.

     DECLARED ABOVE THE EARLY RETURN, and that is the whole bug it used to be.
     It sat further down, past `if (!sel) return`, so the closed panel ran one
     fewer hook than the open one. Selecting anything — a queue card, a node, a
     row on the floor — then rendered MORE hooks than the render before it,
     which React answers by tearing down the tree: error #310, the whole bg4
     shell unmounted, and the classic console left showing through underneath.
     That is the "blank screen with a sliver down the left" this panel caused
     every single time somebody clicked an agent. */
  const [opened, setOpened] = useState<Record<string, boolean>>({});

  /* GIVING AN IDLE SEAT SOMETHING TO DO. The floor's lounge is where a seat
     with no work stands, and the only useful thing to say about one is what it
     should do next - there is no log to read and no run to steer. So the panel
     for that selection is a box, and it posts to the SAME endpoint the
     composer's "send it straight to a seat" does. Deliberately not a second
     door: /api/console/say with a seat files the item for that seat and
     dispatches it, and a second route would be a second set of rules about
     what a human typing at a seat is allowed to start.
     ABOVE THE EARLY RETURN with everything else - see the note on `opened`. */
  const [draft, setDraft] = useState("");
  const [posting, setPosting] = useState(false);
  /* The draft belongs to the seat it was typed for. Carrying it across a
     selection would offer somebody else's sentence to the next seat clicked,
     one button press away from filing it. */
  const selKey = sel?.key;
  useEffect(() => { setDraft(""); }, [selKey]);

  const open = !!sel;
  if (!sel) return <aside className="bg4-insp" aria-hidden="true" />;

  const c = SEAT_COLOR[sel.seat || ""] || "var(--accent)";
  const steps = act.steps || [];
  const running = act.running;
  /* IDLE AND ONLY IDLE. A seat standing at the Director's door is also
     item-less, and answering "this seat is waiting on YOU" with a box that
     files it more work would be the floor arguing with itself. The floor stamps
     the word; nothing here re-derives it. */
  const seat = sel.seat || "";
  const composing = !itemId && !!seat && sel.seatState === "idle";

  async function dispatch() {
    const said = draft.trim();
    if (!said || posting || !seat) return;
    setPosting(true);
    const r = await say(said, seat);
    setPosting(false);
    /* mutate() already raises the toast on a failure, and the draft is left in
       the box on purpose when one happens - the sentence is the only copy. */
    if (r.ok) { setDraft(""); toast(`filed for ${seat}`, "ok"); }
  }

  async function stop() {
    if (!itemId) return;
    setBusy(true);
    await mutate(`/api/queue/${itemId}/stop`, { ok: `item ${itemId} stopped` });
    setBusy(false); refresh();
  }

  async function steer() {
    if (!itemId) return;
    const said = await askText({
      title: "Steer this agent",
      body: "Goes to the running agent's stdin. It reads it between steps.",
      label: "What should it do differently?",
      ok: "steer",
    });
    if (said == null) return;
    setBusy(true);
    await mutate(`/api/queue/${itemId}/steer`, { body: { text: said }, ok: "steer sent" });
    setBusy(false);
  }

  return (
    <aside className={open ? "bg4-insp open" : "bg4-insp"}>
      <div className="bg4-insp-head">
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Ti name={sel.kind === "event" ? "history" : "player-play"} size={16} color={c} />
          <span className="bg4-insp-eyebrow">
            {sel.seat || "floor"}{itemId ? ` · item ${itemId}` : ""}
          </span>
          <button className="bg4-insp-x" title="Close (Esc)"
                  onClick={() => setSelection(null)}>×</button>
        </div>
        <div className="bg4-insp-title">{sel.title}</div>
        {itemId != null && (
          <div className="bg4-meter">
            <span className="big">{steps.length}</span>
            <div className="track">
              <span className="fill"
                    style={{ width: running ? "100%" : "0%",
                             animation: running ? "bgthrob 1.6s infinite" : undefined }} />
            </div>
            <span className="small">{running ? "running" : "finished"}</span>
          </div>
        )}
        {/* THE SAME DRILLDOWN SERVES THE BOARD, THE GRAPH AND THE FLOOR, so
            putting this here puts it on all three at once. */}
        {itemId != null && <ResumeInCli itemId={itemId} />}
      </div>

      <div className="bg4-insp-body">
        {composing && (
          <div className="bg4-seatbox">
            <div className="bg4-insp-eyebrow" style={{ marginBottom: 8 }}>
              Give it something
            </div>
            <p className="bg4-seatbox-note">
              Nothing is running on {seat}. This files the work for that seat and
              dispatches it in your own words. The director does not read it first.
            </p>
            <textarea className="bg4-seatbox-in" rows={4} value={draft}
                      placeholder={`tell the ${seat} seat what to do`}
                      onChange={(e) => setDraft(e.currentTarget.value)}
                      /* Enter sends, Shift+Enter is a newline - the same
                         bargain the console's composer strikes, because this is
                         the same act typed somewhere closer to the seat. */
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault(); dispatch();
                        }
                      }} />
            <button className="bg4-act primary" onClick={dispatch}
                    disabled={!draft.trim() || posting}>
              <Ti name="send" size={14} />send to {seat}
            </button>
          </div>
        )}
        {/* WHAT IT MADE COMES BEFORE WHAT IT DID. Somebody opening an art
            agent wants the picture, not the fortieth tool call - and this draws
            nothing at all on a run that produced no artifacts. */}
        {!composing && itemId != null && <AgentMade itemId={itemId} />}
        {!composing && (
        <div className="bg4-insp-eyebrow" style={{ marginBottom: 8 }}>
          {sel.kind === "event" ? "Event" : "Steps"}
        </div>
        )}
        {sel.kind === "event" && (
          <div className="bg4-step done">
            <div className="gut"><span className="dot"><Ti name="point" size={12} /></span></div>
            <div className="body"><div className="lab">{sel.title}</div></div>
          </div>
        )}
        {foldSteps(steps).map((beat, i) => {
          if (beat.kind === "say") {
            /* THE AGENT'S OWN WORDS, at full width and unclipped. This was a
               collapsed row with a 160-character slice under it, which is the
               one thing in the feed that was already written to be read. */
            return (
              <div className="bg4-said" key={i}>
                <Ti name="message-2" size={13} />
                <p>{beat.text}</p>
              </div>
            );
          }
          const anyBad = beat.calls.some((c) => c.bad);
          return (
            <div className={`bg4-beat${anyBad ? " bad" : ""}`} key={i}>
              {beat.calls.map((c, j) => {
                const key = `${i}:${j}`;
                const open = !!opened[key];
                return (
                  <div className="bg4-call" key={j}>
                    {/* THE WHOLE CHIP OPENS IT. A tool result is a JSON object
                        whose first line is `{ "ok": false, "error": …` - the
                        reason a run failed used to sit one character past the
                        cut with nothing to click. */}
                    <button className={`bg4-chip${c.bad ? " bad" : ""}`}
                            onClick={() => c.raw && setOpened({ ...opened, [key]: !open })}
                            disabled={!c.raw}
                            title={c.raw ? "show what it returned" : "no result recorded"}>
                      <Ti name={c.bad ? "alert-triangle" : "tool"} size={11} />
                      <span>{c.name}</span>
                      {c.bad && <span className="err">failed</span>}
                    </button>
                    {open && c.raw && (
                      <div className="bg4-stepfull">
                        <pre>{pretty(c.raw)}</pre>
                        <button className="bg4-copy"
                                onClick={() => {
                                  void navigator.clipboard?.writeText(c.raw);
                                  toast("copied");
                                }}>copy</button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          );
        })}
        {itemId != null && !steps.length && (
          <div className="bg4-empty" style={{ padding: 0 }}>no steps logged yet</div>
        )}
      </div>

      {/* NO FOOT ON A SEAT WITH NOTHING RUNNING. Log, stop and steer all need a
          run to act on, so all three would sit there greyed out under the one
          control that does work - three dead buttons saying the panel is broken
          rather than that the seat is free. */}
      {!composing && (
      <div className="bg4-insp-foot">
        <button className="bg4-act" disabled={!itemId}
                onClick={() => itemId && watchAgent(itemId)}>
          <Ti name="file-text" size={14} />log
        </button>
        <button className="bg4-act" disabled={!itemId || !running || busy} onClick={stop}>
          <Ti name="player-stop" size={14} />stop
        </button>
        <button className="bg4-act primary" disabled={!itemId || !running || busy}
                onClick={steer}>
          <Ti name="steering-wheel" size={14} />steer
        </button>
      </div>
      )}
    </aside>
  );
}
