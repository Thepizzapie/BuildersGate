import { useCallback, useEffect, useState } from "react";
import { Ti } from "./Ti";
import { SEAT_COLOR } from "./nav";
import { setSelection, useSelection } from "./selection";
import { askText, mutate, readJSON, toast, watchAgent } from "../bridge";
import { usePoll } from "../hooks";

/* The inspector — what you selected, in a panel that pops out over the stage.
 *
 * The old dashboard put this behind a MODAL: you clicked a running agent, an
 * overlay covered the floor, and closing it to look at the next row lost your
 * place. This has no scrim and traps nothing, so the floor underneath stays
 * clickable and the panel just follows your selection — inspecting six agents
 * is six clicks, not six open-read-close cycles.
 *
 * It is also not a permanent column, which was the prototype's shape and the
 * first thing built here: 340px is a third of a laptop's usable width, held
 * whether or not anything is selected, and most of the time nothing is.
 *
 * WHEN THE THING IS AN AGENT, this is a live log: /api/agent-activity is the
 * same feed the console reads, rendered as the stepper the prototype drew. Tool
 * calls are steps; the assistant's own sentences are steps; the last one is the
 * one happening now. Nothing is invented — a run with two steps shows two.
 */

type Step = { kind: string; name?: string; text?: string; result?: string };
type Activity = { steps: Step[]; running: boolean; final?: { text?: string } | null };

/* ── reading a step's result ───────────────────────────────────────────────
 *
 * A tool result is a JSON blob on one line. Truncating it to 160 characters
 * shows the opening brace and the first key, which for a failure is
 * `{ "ok": false, "error": "RateLimitError: …` — the answer starts exactly
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
function oneLine(raw: string): string {
  return raw.replace(/\s+/g, " ").trim().slice(0, 160);
}

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
  const [opened, setOpened] = useState<Record<number, boolean>>({});

  const open = !!sel;
  if (!sel) return <aside className="bg4-insp" aria-hidden="true" />;

  const c = SEAT_COLOR[sel.seat || ""] || "var(--accent)";
  const steps = act.steps || [];
  const running = act.running;

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
      </div>

      <div className="bg4-insp-body">
        <div className="bg4-insp-eyebrow" style={{ marginBottom: 8 }}>
          {sel.kind === "event" ? "Event" : "Steps"}
        </div>
        {sel.kind === "event" && (
          <div className="bg4-step done">
            <div className="gut"><span className="dot"><Ti name="point" size={12} /></span></div>
            <div className="body"><div className="lab">{sel.title}</div></div>
          </div>
        )}
        {steps.map((s, i) => {
          const now = running && i === steps.length - 1;
          const raw = s.text || s.result || "";
          const open = !!opened[i];
          const bad = failed(raw);
          return (
            <div className={`bg4-step ${now ? "now" : "done"}${bad ? " bad" : ""}`} key={i}>
              <div className="gut">
                <span className="dot">
                  <Ti name={bad ? "alert-triangle"
                            : s.kind === "tool" ? "tool" : "message-2"} size={12} />
                </span>
                {i < steps.length - 1 && <span className="rail" />}
              </div>
              <div className="body">
                {/* THE WHOLE ROW OPENS IT. A 160-character slice of a tool
                    result is the first line of a JSON object — `{ "ok": false,
                    "error": "RateLimit…` — so a run could fail and the reason
                    sat one character past the cut with nothing to click. */}
                <button className="bg4-steprow"
                        onClick={() => raw && setOpened({ ...opened, [i]: !open })}
                        disabled={!raw}>
                  <span className="lab">{s.name || s.kind}</span>
                  {bad && <span className="err">failed</span>}
                  {raw && (
                    <Ti name={open ? "chevron-down" : "chevron-right"} size={12} />
                  )}
                </button>
                {raw && !open && <div className="sub">{oneLine(raw)}</div>}
                {raw && open && (
                  <div className="bg4-stepfull">
                    <pre>{pretty(raw)}</pre>
                    <button className="bg4-copy"
                            onClick={() => {
                              void navigator.clipboard?.writeText(raw);
                              toast("copied");
                            }}>copy</button>
                  </div>
                )}
              </div>
            </div>
          );
        })}
        {itemId != null && !steps.length && (
          <div className="bg4-empty" style={{ padding: 0 }}>no steps logged yet</div>
        )}
      </div>

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
    </aside>
  );
}
