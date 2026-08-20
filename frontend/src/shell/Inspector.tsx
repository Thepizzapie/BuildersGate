import { useCallback, useEffect, useState } from "react";
import { Ti } from "./Ti";
import { ResumeInCli } from "./ResumeInCli";
import { AgentMade } from "./AgentMade";
import { SEAT_COLOR } from "./nav";
import { setSelection, useSelection } from "./selection";
import { askText, lightbox, mutate, readJSON, toast, watchAgent } from "../bridge";
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

/* `hint` is the tool call's SUBJECT and it is what this panel is built on — see
   dispatch._tool_subject for which input key supplies it per tool. `files` and
   `images` are stamped by phases.look() on the way out of /api/agent-activity:
   project-relative paths the step named that actually exist on disk. */
type Step = {
  kind: string; name?: string; text?: string; result?: string; hint?: string;
  files?: string[]; images?: string[]; truncated?: boolean;
};
type Activity = { steps: Step[]; running: boolean; final?: { text?: string } | null };

/* ── naming what a step was about ─────────────────────────────────────────── */

/** Middle of a long hint is where the information is for a path and the start
 *  is where it is for a command, so this cuts the tail and both survive — the
 *  path already had its directories folded away by `shortPath` before it got
 *  here. */
function clip(text: string, max: number): string {
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

/** `C:\Users\robin\Desktop\downsizing\game\scenes\floor_0.tscn` is 55 characters
 *  of which 15 identify the file. The parent directory stays because
 *  `floor_0.tscn` alone does not distinguish game/scenes from a backup. */
function shortPath(raw: string): string {
  const parts = raw.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length <= 2 ? parts.join("/") : "…/" + parts.slice(-2).join("/");
}

const PATHISH = /^(?=.*[\\/])\S+$/;

/* EVERY DISPATCHED AGENT OPENS ITS COMMANDS WITH `cd "<project>" && `, because
   the runner does not set a cwd it can rely on. That prefix is identical on
   every Bash step of every run, so leaving it in meant the first forty
   characters of every command chip said the same thing. */
const CD_PREFIX = /^cd\s+(?:"[^"]*"|'[^']*'|\S+)\s*&&\s*/;

function subjectOf(verb: string, hint: string): string {
  const text = (hint || "").trim();
  if (!text) return "";
  if (verb === "Bash") {
    /* Collapsed to one line rather than cut at the first newline. A heredoc or
       a `python -c "…"` block starts with a line that is only its opening
       quote, so first-line-only rendered the row as `python -c "` — the verb
       again, in disguise. */
    const cmd = text.replace(CD_PREFIX, "").split(/\s+/).join(" ").trim();
    return clip(cmd || text, 92);
  }
  /* " · " is dispatch's own join between a search pattern and the path it was
     run against; rendered as "pattern in …/dir/file" it reads as the sentence
     the call actually was. */
  return clip(text.split(" · ")
                  .map((part) => (PATHISH.test(part) ? shortPath(part) : part))
                  .join("  in  "), 92);
}

const VERB_ICON: Record<string, string> = {
  Read: "file-text", Write: "pencil", Edit: "pencil", MultiEdit: "pencil",
  Grep: "search", Glob: "search", Bash: "terminal-2", WebFetch: "world",
  WebSearch: "world", Task: "users", TodoWrite: "checklist",
};

/** The one sentence a failure is. A tool result is a JSON blob whose `error`
 *  key holds it; a crashed Bash step has no JSON at all and the useful line is
 *  the one that says so. Shown UNCOLLAPSED on a failed row — a run that went
 *  wrong should not need a click to say how. */
function errorOf(raw: string): string {
  const found = /"error"\s*:\s*"((?:[^"\\]|\\.)*)"/.exec(raw.slice(0, 1200));
  if (found) {
    try { return JSON.parse(`"${found[1]}"`) as string; } catch { return found[1]; }
  }
  const line = raw.split("\n").find((l) => /error|traceback|failed|refus/i.test(l));
  return (line || raw).trim().slice(0, 240);
}

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


/* A RUN, AS A TIMELINE RATHER THAN AS A LOG.
 *
 * The feed is a flat list of say / tool / result. Drawn one row per entry it
 * was forty rows of plumbing with the agent's own sentences buried among them;
 * drawn as chips it became forty interchangeable words - Read, Grep, Grep,
 * Read, Bash, Bash, Grep - and neither told you what the agent was doing. The
 * chip pass fixed the density and lost the content, which was the worse trade:
 * you could see the SHAPE of a run and nothing about its subject.
 *
 * So each entry names WHAT it acted on, and three foldings keep that readable:
 *
 *   a tool and the result that follows it are ONE entry. They always come in
 *   pairs, and "godot_run" then "result {ok:true…}" is one thing that happened.
 *   The pair's verdict moves onto the tool's own row.
 *
 *   consecutive calls on the SAME subject collapse, with a count. An agent that
 *   greps one file four times refining a pattern did one thing to one file, and
 *   four identical rows is four chances to lose your place. Same-subject only:
 *   the old fold merged every consecutive tool regardless, which is what turned
 *   a run into soup.
 *
 *   a failed call never merges into a healthy one. Folding a failure into a
 *   count of five is how the reason a run died stops being visible, and that is
 *   the entire thing this panel exists to show.
 *
 * WHAT IS NEVER FOLDED IS WHAT THE AGENT SAID. `say` is the only entry written
 * for a human, so it keeps its own full-width row and its whole text - and
 * `steer` beside it, which is what a human said BACK and used to be drawn as a
 * tool chip called "steer" with the sentence hidden inside a collapsed pre.
 */
type Entry =
  | { kind: "say"; text: string }
  | { kind: "steer"; text: string }
  | { kind: "tool"; verb: string; subject: string; count: number;
      raw: string; bad: boolean; images: string[]; files: string[] };

/** Union of two path lists, order-preserving and capped. A merged entry that
 *  listed the same file eight times would undo the merge visually. */
function addAll(into: string[], more: string[] | undefined, cap: number): void {
  for (const p of more || []) {
    if (into.length >= cap) return;
    if (!into.includes(p)) into.push(p);
  }
}

function foldSteps(steps: Step[]): Entry[] {
  const out: Entry[] = [];
  for (let i = 0; i < steps.length; i++) {
    const s = steps[i];
    if (s.kind === "say" || s.kind === "steer") {
      const text = (s.text || "").trim();
      if (text) out.push({ kind: s.kind === "steer" ? "steer" : "say", text });
      continue;
    }
    let verb = s.name || s.kind;
    let subject = subjectOf(verb, s.hint || "");
    const images: string[] = [];
    const files: string[] = [];
    let raw = "";
    if (s.kind === "result") {
      /* A result with no tool before it - the feed's ring dropped the call, or
         a re-dispatch cut the log. Kept rather than lost, under its own name. */
      verb = "result";
      subject = "";
      raw = s.text || s.result || "";
      addAll(images, s.images, 4); addAll(files, s.files, 5);
    } else {
      addAll(images, s.images, 4); addAll(files, s.files, 5);
      const next = steps[i + 1];
      if (next && next.kind === "result") {
        raw = next.text || next.result || "";
        addAll(images, next.images, 4); addAll(files, next.files, 5);
        i += 1;
      }
    }
    const bad = failed(raw);
    const last = out[out.length - 1];
    if (last && last.kind === "tool" && !bad && !last.bad
        && last.verb === verb && last.subject === subject) {
      last.count += 1;
      /* The LAST result of a merged run, not the first: when an agent retries
         the same call it is the newest answer that describes where it got to. */
      if (raw) last.raw = raw;
      addAll(last.images, images, 4); addAll(last.files, files, 5);
      continue;
    }
    out.push({ kind: "tool", verb, subject, count: 1, raw, bad, images, files });
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
     for that selection is a box, and it files a work item for that seat over
     POST /api/queue - the same row a queue_add makes, so auto-deploy and the
     QA gate treat it exactly like any other.
     ABOVE THE EARLY RETURN with everything else - see the note on `opened`. */
  const [draft, setDraft] = useState("");
  const [posting, setPosting] = useState(false);
  /* The draft belongs to the seat it was typed for. Carrying it across a
     selection would offer somebody else's sentence to the next seat clicked,
     one button press away from filing it. */
  const selKey = sel?.key;
  /* EVERYTHING READ OFF A SELECTION DIES WITH IT.
     The draft belongs to the seat it was typed for - carrying it across a
     selection would offer somebody else's sentence to the next seat clicked,
     one button press away from filing it.
     `act` is the same class of bug and it was live: the poll is the only thing
     that refills it and the poll is gated off when there is no item, so
     clicking an idle seat left the PREVIOUS agent's steps on screen - three
     idle seats in a row each showed gameplay's item #472. Between two items it
     was subtler and just as wrong: the header's step count kept the old item's
     number until the first poll answered, under the new item's title.
     `opened` goes too, because it is keyed by position in a list that is about
     to be a different list. */
  useEffect(() => {
    setDraft("");
    setAct({ steps: [], running: false, final: null });
    setOpened({});
  }, [selKey]);

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
    /* FILED AS A WORK ITEM FOR THAT SEAT, in the human's own words. It used to
       go through /api/console/say, which wrapped it in a chat turn; the chat is
       a Claude session now and a message to it is not a board row. */
    const r = await mutate("/api/queue", {
      body: { seat, title: said.split("\n")[0].slice(0, 80), brief: said,
              source: "manual" }, quiet: true });
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

  /* RETIRING A FAILURE MARK.
   *
   * The floor keeps a seat red until a NEWER item for that seat closes well,
   * which is right when the work still matters - a failure that scrolls away
   * on its own is a failure nobody fixed. It is wrong when the item is not
   * coming back: #405 failed because a credit block upstream made the buy
   * impossible, the blocker is already filed as its own item, and there is
   * nothing to re-run. That mark then sits on the floor forever, because the
   * only thing that retires it is a success that will never be filed.
   *
   * So: call the item off. `cancelled` and not `done` on purpose - it says the
   * work was dropped rather than that it worked, which is the true thing, and
   * it is the one status that both clears the badge and survives being read
   * back later by somebody asking what happened to it.
   */
  async function clearFailure() {
    if (!itemId) return;
    const why = await askText({
      title: `Clear the failure on item ${itemId}`,
      body: "Calls the work off and retires the red mark on the floor. The "
          + "item stays on the board as cancelled - this does not pretend it "
          + "worked, and it does not re-run it.",
      label: "Why is this one not coming back?",
      ok: "clear it",
    });
    if (why == null) return;
    setBusy(true);
    await mutate(`/api/queue/${itemId}/cancel`, {
      body: { reason: why.trim() || "cleared from the floor" },
      ok: `item ${itemId} cleared`,
    });
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
        {/* NO TIMELINE WITHOUT AN ITEM TO DRAW ONE FOR. `act` is only ever
            refilled by the poll, and the poll is gated off when there is no
            item - so an idle seat used to render the PREVIOUS selection's steps
            underneath its own "nothing is running here" composer. Three
            different idle seats each showed gameplay's run. The effect on
            `selKey` below clears the state; this makes the render refuse to
            draw it even for the frame before that lands. */}
        {itemId != null && foldSteps(steps).map((entry, i) => {
          if (entry.kind === "say" || entry.kind === "steer") {
            /* THE PROSE, at full width and unclipped. `say` is the one thing in
               the feed that was already written to be read, and `steer` is the
               human answering it - both used to be collapsed rows with a
               160-character slice under them. */
            const isSteer = entry.kind === "steer";
            return (
              <div className={isSteer ? "bg4-said steer" : "bg4-said"} key={i}>
                <Ti name={isSteer ? "steering-wheel" : "message-2"} size={13} />
                <p>{entry.text}</p>
              </div>
            );
          }
          const key = String(i);
          const isOpen = !!opened[key];
          return (
            <div className={`bg4-tl${entry.bad ? " bad" : ""}`} key={i}>
              {/* THE WHOLE ROW OPENS IT. A tool result is a JSON object whose
                  first line is `{ "ok": false, "error": …` - the reason a run
                  failed used to sit one character past the cut with nothing to
                  click. */}
              <button className="bg4-tl-head"
                      onClick={() => entry.raw && setOpened({ ...opened, [key]: !isOpen })}
                      disabled={!entry.raw}
                      title={entry.raw ? "show what it returned"
                                       : "no result recorded"}>
                <Ti name={entry.bad ? "alert-triangle"
                                    : VERB_ICON[entry.verb] || "tool"} size={12} />
                <span className="verb">{entry.verb}</span>
                {entry.subject
                  ? <span className="subj">{entry.subject}</span>
                  : <span className="subj none">—</span>}
                {entry.count > 1 && <span className="mult">×{entry.count}</span>}
                {entry.bad && <span className="err">failed</span>}
              </button>
              {/* THE REASON, NOT A BADGE SAYING THERE IS ONE. A failed step
                  that makes you click to find out why is a failed step you
                  scroll past. */}
              {entry.bad && entry.raw && (
                <div className="bg4-tl-err">{errorOf(entry.raw)}</div>
              )}
              {/* Pictures are the preview, so they are not behind the fold -
                  the whole point of a thumbnail is that it costs a glance. */}
              {entry.images.length > 0 && (
                <div className="bg4-tl-shots">
                  {entry.images.map((p) => (
                    <button key={p} className="bg4-tl-shot" title={p}
                            onClick={() => lightbox(p)}>
                      <img src={`/api/preview?rel=${encodeURIComponent(p)}`} alt="" />
                    </button>
                  ))}
                </div>
              )}
              {isOpen && entry.raw && (
                <div className="bg4-stepfull">
                  <pre>{pretty(entry.raw)}</pre>
                  <button className="bg4-copy"
                          onClick={() => {
                            void navigator.clipboard?.writeText(entry.raw);
                            toast("copied");
                          }}>copy</button>
                </div>
              )}
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
        {/* ONLY ON A SEAT THE FLOOR IS MARKING RED, and in steer's place
            rather than beside it: a failed item has no live agent to steer,
            so the two are never both useful at once. */}
        {sel.seatState === "failed" ? (
          <button className="bg4-act primary" disabled={!itemId || busy}
                  onClick={clearFailure}
                  title="call this item off and retire the mark on the floor">
            <Ti name="circle-x" size={14} />clear
          </button>
        ) : (
          <button className="bg4-act primary" disabled={!itemId || !running || busy}
                  onClick={steer}>
            <Ti name="steering-wheel" size={14} />steer
          </button>
        )}
      </div>
      )}
    </aside>
  );
}
