import { useEffect, useRef, useState } from "react";
import { Ti } from "../Ti";
import { askText, mutate, readJSON, toast, watchAgent } from "../../bridge";
import { Head, Nothing, Tag, ReadError, Banner } from "./prims";
import { useJSON, useList, ago, usd } from "./api";
import { useSeatChips } from "./chips";
import type { SeatBodyProps } from "./types";
import "./cineqa.css";
import "./qa.css";

/* QA — the verdict, and the contract the probe is allowed to assert against.
 *
 * THREE STATES, AND THE PANEL'S ONE JOB IS TO KEEP THEM APART:
 *
 *   PASS       a VERDICT line was written and evidence is attached to it
 *   FAIL       a VERDICT line was written and it was no — and a FAIL that
 *              cannot be sent back is a complaint, so the reopen is on the card
 *   UNDECIDED  it FINISHED and nobody wrote one
 *
 * From across the room "finished" and "passed" look identical, which is the
 * entire reason the marker exists, so undecided is drawn as its own thing and
 * never as a quieter pass. It has two flavours and they are not the same
 * accusation: a gate that RAN and wrote no line (`kind: unknown`) broke a
 * promise and is drawn loudest; an item closed with no gate filed at all
 * (`kind: ungated`) is what a project running gate-mode `none` looks like, and
 * two hundred alarm-red cards would only teach the reader to stop looking. That
 * one is dashed on the card and said ONCE, loudly, in the banner.
 *
 * EVIDENCE THE BACKEND KEPT IS DRAWN, NOT DROPPED. /api/history carries the
 * gate run's own transcript (`verdict.detail`), the id of that run
 * (`verdict.gate_item`), and the closing agent's own words (`result`). All
 * three were on the wire and none were on screen, which made every card an
 * assertion the reader had to take on faith — from the panel whose doctrine is
 * that a pass is a written verdict WITH EVIDENCE.
 *
 * THE CONTRACT IS EDITED HERE, IN A TEXTAREA, because the seat's brief says
 * correcting it is one JSON document and not a code change. It used to open a
 * one-line prompt with the document as PLACEHOLDER text, which is grey hint
 * text that vanishes on the first keystroke: "editable" meant retyping a
 * hundred lines of JSON from memory. The version marker rides along in the
 * document, so a second tab's save is a 409 rather than a silent overwrite.
 *
 * BOTS: `unknown` is not `fail`. A bot that asserted nothing did not find the
 * game innocent and did not find it guilty — reporting either as a FAIL of
 * somebody's item sends a maker seat chasing a bug in this harness.
 *
 * RUNS ARE JOBS. Both run buttons POST to endpoints that answer 202 and a
 * job id. Firing and forgetting made the button look instant, dropped every
 * error the run produced, and left the reader watching a stale list for up to
 * twenty seconds. They are polled to a terminal state now, and what comes back
 * is drawn.
 */

type Verdict = {
  kind?: string; label?: string; short?: string; why?: string;
  rounds?: number; escalated?: boolean; gate_item?: number | null;
  detail?: string; at?: string;
};
type Item = {
  id: number; seat: string; title: string; status: string; source?: string;
  cost_usd?: number; turns?: number; attempts?: number; updated_at?: string;
  closed_by?: string; approved_by?: string; stopped_by?: string;
  log_bytes?: number; result?: string; result_len?: number;
  verdict?: Verdict;
};
type Sample = { key: string; actor?: string; property?: string; round?: number };
type Contract = {
  scene?: string; source?: string; shape?: string; why?: string;
  actors?: { key: string; path?: string; find?: string }[];
  samples?: Sample[];
  sample_keys?: string[];
  tick?: { mode?: string; node?: string; method?: string };
  issues?: string[];
  alternatives?: (string | { scene?: string; why?: string })[];
};

/* THE STATE A CARD IS IN. Six, not three, because the backend distinguishes six
   and collapsing them is how "the gate itself died" ends up filed as a maker
   seat's failure. The three the doctrine cares about are pass / fail /
   undecided; the rest are drawn as themselves so they cannot be mistaken for
   any of the three. */
type State = "pass" | "fail" | "undecided" | "ungated" | "harness" | "human" | "pending";

const stateOf = (v?: Verdict): State => {
  const k = String(v?.kind || "");
  if (!v || !v.label) return "undecided";
  if (k === "pass") return "pass";
  if (k === "fail") return "fail";
  if (k === "approved") return "human";
  // The gate run itself died. Persona rule 3: not evidence about the work.
  if (k === "error") return "harness";
  if (k === "reviewing" || k === "awaiting") return "pending";
  // `ungated` and `na` are both "no independent check exists", and the second
  // is the honest one — a cancelled item was decided by whoever cancelled it.
  if (k === "ungated") return "ungated";
  if (k === "na" || k === "none") return "ungated";
  // `unknown` — a gate run finished and wrote no VERDICT line. The loud one.
  return "undecided";
};

const TONE: Record<State, "good" | "warn" | "bad" | "off"> = {
  pass: "good", fail: "bad", undecided: "bad", ungated: "off",
  harness: "warn", human: "warn", pending: "off",
};

/* The card class. `t-unknown` is the loudest treatment the seat has and it is
   spent on exactly one state, so it keeps meaning something. */
const CARD: Record<State, string> = {
  pass: "t-good", fail: "t-bad", undecided: "t-unknown", ungated: "t-ungated",
  harness: "t-warn", human: "t-warn", pending: "t-off",
};

/* NOBODY DECIDED — the wide set, for the note's emphasis. */
const UNDECIDED = new Set<State>(["undecided", "ungated", "harness"]);
/* Reopenable: it is closed, and nothing independent ever said it was right. */
const CLOSED = new Set(["done", "failed", "cancelled"]);

type Run = {
  at?: string; ok?: boolean; no_tests?: boolean; scripts_run?: number;
  scripts_failed?: number; passed?: number; failures?: number;
  seconds?: number; by?: string; error?: string;
  scripts?: { script?: string; ok?: boolean; passed?: number; failed?: number;
              error?: string }[];
};
type Engine = {
  tests_dir?: string; godot_project?: string; scripts?: string[]; why?: string;
  runs?: Run[]; last?: Run | null;
};

/* The bot-run history row as /api/qa-bots/runs really shapes it. There is no
   `ok` and no `note` on it — the component used to read both, which made every
   PASS render with a red tag, and dropped `failures` (the reasons), the
   expectation count (zero of them being the green-for-free this seat exists to
   end) and the baseline marker. */
type BotRun = {
  id: number; bot?: string; verdict?: string; created_at?: string;
  is_baseline?: boolean; build_ref?: string; expectations?: number;
  failures?: { label?: string; reason?: string }[];
  final?: Record<string, unknown> | null;
};

/* A bot verdict, in the four states the backend actually produces. */
const BOT_TONE = (v?: string): "good" | "warn" | "bad" | "off" =>
  v === "pass" ? "good" : v === "fail" ? "bad" : v === "error" ? "warn" : "bad";
const BOT_WHY: Record<string, string> = {
  pass: "every expectation held against the samples the probe printed",
  fail: "an expectation was checked against a real sample and did not hold",
  error: "the probe never got hold of the scene or an actor — this says nothing about the work under review",
  unknown: "the bot asserted nothing. A run with no expectations is not a pass",
};

type Job = {
  id?: number; state?: string; terminal?: boolean; progress?: number;
  stage?: string; result?: Record<string, unknown>; error?: string;
};

/** Poll a 202'd job to a terminal state. A run that is not watched is a button
 *  that lies about being finished, and an error nobody ever sees.
 *
 *  `alive` ends the WATCH, not the job: navigating away used to leave this
 *  loop firing a request every 1.4s for up to ~12 minutes and calling
 *  setState on a component that was gone. The job itself keeps running
 *  server-side and its outcome lands on the board either way. */
async function awaitJob(id: number, onStage: (s: string) => void,
                        alive: () => boolean = () => true): Promise<Job> {
  for (let i = 0; i < 500; i++) {
    if (!alive())
      return { state: "detached", terminal: true,
               error: "stopped watching — the job continues on the server" };
    const j = await readJSON<Job>(`/api/jobs/${id}`, {});
    if (j.__error) return { state: "failed", terminal: true, error: j.__error };
    if (!alive())
      return { state: "detached", terminal: true,
               error: "stopped watching — the job continues on the server" };
    onStage(j.stage || j.state || "running");
    if (j.terminal) return j;
    await new Promise((r) => window.setTimeout(r, 1400));
  }
  return { state: "failed", terminal: true, error: "still running after ~12 minutes" };
}

export function Qa({ seat, active, tab }: SeatBodyProps) {
  /* awaitJob's leash — see its docstring. */
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const [busy, setBusy] = useState("");
  const [stage, setStage] = useState("");
  /* Bumped after any write, so the panel shows the consequence of the button
     that was just pressed instead of the state from before it. */
  const [nonce, setNonce] = useState(0);
  const [limit, setLimit] = useState(25);
  const [filter, setFilter] = useState<"all" | "undecided" | "fail" | "pass">("all");
  const [open, setOpen] = useState<number | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [draftErr, setDraftErr] = useState("");
  const [spec, setSpec] = useState<string | null>(null);
  const [lastBot, setLastBot] = useState<Record<string, unknown> | null>(null);

  /* THE VERDICT COUNTS ARE HEADER CHIPS, so history is read on every tab of
     this seat rather than only on the one that lists it. An UNKNOWN nobody is
     looking at is the exact thing the chip exists to make unmissable. */
  const hist = useJSON<{
    items?: Item[];
    gate?: { mode?: string; label?: string; env_override?: string };
    page?: { total?: number; limit?: number };
  }>(`/api/history?limit=${limit}&n=${nonce}`, { items: [] }, 8000, active);
  const engine = useJSON<Engine>(`/api/engine-tests?n=${nonce}`, { runs: [] },
                                 20000, active && tab === "tests");
  /* Both enveloped, both unwrapped by the page: the contract arrives as
     itself, and the run list arrives as a bare array — hence useList. */
  const contract = useJSON<Contract>(
    `/api/qa-bots/contract?n=${nonce}`, {}, 20000, active && tab === "contract");
  const runs = useList<BotRun>(
    `/api/qa-bots/runs?n=${nonce}`, 10000, active && tab === "contract");
  /* The game's OWN input actions. A bot spec offering actions this InputMap has
     never heard of produces a run whose every press was silently discarded. */
  const acts = useJSON<{ actions?: string[]; source?: string }>(
    "/api/qa-bots/actions", { actions: [] }, 60000, active && tab === "contract");

  const items = hist.items || [];
  const total = hist.page?.total ?? items.length;
  const states = new Map(items.map((it) => [it.id, stateOf(it.verdict)]));
  const unknown = items.filter((it) => states.get(it.id) === "undecided");
  const ungated = items.filter((it) => states.get(it.id) === "ungated");
  const passed = items.filter((it) => states.get(it.id) === "pass");
  /* A FAIL REOPENS THE ITEM WITH A RANKED NITPICK LIST — that is the gate's own
     protocol (bgate_ui/qa_gate.py). The list itself is prose in the item's
     brief and nothing counts its lines, so what is published is what IS
     countable: the items standing rejected. */
  const rejected = items.filter(
    (it) => it.verdict?.kind === "fail" && it.status !== "done");

  useSeatChips(seat.role, [
    ...(items.length ? [{
      icon: "help-octagon",
      label: `${unknown.length} UNKNOWN`,
      color: unknown.length ? "var(--bad)" : "var(--text-3)",
      title: "gate runs that finished without writing a VERDICT line — they decided nothing",
    }, {
      icon: "checks",
      label: `${passed.length} passed`,
      color: "var(--good)",
      title: "an independent QA agent verified the claim against the real artefact",
    }] : []),
    ...(rejected.length ? [{
      icon: "flag",
      label: `${rejected.length} open, rejected`,
      color: "var(--warn)",
      title: "FAILed by the gate and reopened with a ranked nitpick list, still not done",
    }] : []),
  ]);

  async function post(path: string, label: string, body?: unknown) {
    setBusy(label);
    const r = await mutate<Record<string, unknown>>(path, { body: body ?? {}, quiet: true });
    setBusy("");
    if (!r.ok) toast(r.error || `${label} failed`, "bad");
    return r;
  }

  /** POST something that answers 202, then WATCH it. Returns the job's result,
   *  or null if it never got that far — and says which, out loud, either way. */
  async function runJob(path: string, label: string, body: Record<string, unknown>):
    Promise<Record<string, unknown> | null> {
    setBusy(label);
    setStage("starting…");
    const r = await mutate<{ job_id?: number }>(path, { body: { ...body, async: true }, quiet: true });
    if (!r.ok || !r.data?.job_id) {
      setBusy(""); setStage("");
      toast(r.error || `${label} did not start`, "bad");
      return null;
    }
    const job = await awaitJob(r.data.job_id, setStage,
                               () => mounted.current);
    if (!mounted.current) return null;
    setBusy(""); setStage("");
    setNonce((n) => n + 1);
    if (job.state === "cancelled") { toast(`${label} was cancelled`, "warn"); return null; }
    if (job.state !== "done") {
      toast(job.error || `${label} failed — the job did not finish`, "bad");
      return null;
    }
    const out = job.result || {};
    if (out.ok === false) toast(String(out.error || `${label} failed`), "bad");
    return out;
  }

  /* ── TESTS ── what the engine itself was asked to prove, over time. */
  if (tab === "tests") {
    const rows = engine.runs || [];
    const scripts = engine.scripts || [];
    /* BY NAMING CONVENTION, said as such. A control is a test written to go RED
       on purpose, and the seat's fourth rule is that an expectation which has
       never been red has never been tested. Nothing in the file format marks
       one, so the only honest signal available is the name — and a suite with
       zero of them is worth saying out loud even on a heuristic. */
    const controls = scripts.filter((s) => /control|negative/i.test(s));

    async function runSuite(paths?: string[]) {
      const out = await runJob("/api/engine-tests/run",
                               paths ? `run:${paths[0]}` : "run",
                               paths ? { paths } : {});
      if (!out) return;
      if (out.no_tests) toast(String(out.error || "nothing was run"), "bad");
      else toast(`${out.scripts_run} script(s) · ${out.passed} passed · ${out.failures} failed`,
                 out.ok ? "ok" : "bad");
    }

    return (
      <div className="bgs-pad">
        <Head label="Engine tests"
              hint="an assertion that would still pass with the feature deleted is not a test"
              right={
                <button className="bgs-btn" disabled={!!busy}
                        onClick={() => void runSuite()}>
                  {busy === "run" ? "running…" : `run all ${scripts.length || ""}`.trim()}
                </button>} />
        <ReadError error={engine.__error} what="the engine test history" />
        {!!busy && (
          <div className="bgq-job">
            <Ti name="loader" size={14} />
            <span>{busy === "run" ? "driving the whole suite" : busy}</span>
            <span className="sp" />
            <span>{stage || "…"}</span>
          </div>
        )}

        {/* THE SUITE ON DISK COMES FIRST, because "nothing has been run" and
            "there is nothing to run" are different problems with different
            fixes, and a panel that cannot tell them apart tells the reader to
            write tests that are already written. Each name is a BUTTON: the
            runner takes `paths`, so re-running the one script that went red
            never needed eighty engine boots. */}
        {scripts.length ? (
          <>
            <div className="bgs-scripts">
              {scripts.map((s) => (
                <span className={`s run${/control|negative/i.test(s) ? " ctl" : ""}`} key={s}
                      role="button" tabIndex={0}
                      title={`run only ${s} — the runner takes a path list, so one red script does not cost a whole suite`}
                      onClick={() => { if (!busy) void runSuite([`tests/${s}`]); }}
                      onKeyDown={(e) => { if (e.key === "Enter" && !busy) void runSuite([`tests/${s}`]); }}>
                  {s}
                </span>
              ))}
            </div>
            <p className="bgq-note">
              {controls.length
                ? `${controls.length} of ${scripts.length} are named as controls (dashed) — read by name, which is the only marker the format has.`
                : <>
                    <b>no script here is named as a control.</b> An expectation
                    that has never been red has never been tested; a suite of
                    those is green for free. Read by name, which is the only
                    marker the format has.
                  </>}
            </p>
          </>
        ) : engine.__error ? null : (
          /* Only when the read SUCCEEDED and came back empty. A failed read
             rendering "no Godot project" is the empty-state bug this component
             set out to stop making. */
          <Nothing what={engine.tests_dir ? `no *.gd in ${engine.tests_dir}` : "no Godot project"}
                   how={engine.why || "a regression gate with nothing in it looks exactly like a green one"} />
        )}

        {!rows.length && !!scripts.length && (
          <Nothing what="no run has been recorded against this suite"
                   how="run the suite here, or call godot_test_run — a green suite nobody has run since the change proves nothing" />
        )}
        {rows.map((r, i) => {
          /* A RUN THAT ASSERTED NOTHING IS NOT A PASS. Scripts booted, the
             engine exited clean, and zero PASS markers were printed: that is
             the same shape as a gate run with no VERDICT line and it must not
             be drawn in the same green as a suite that actually checked
             something. `no_tests` (nothing to run) was already separated; this
             is the one after it — something ran and proved nothing. */
          const mute = !r.no_tests && r.ok && !r.passed;
          const bad = r.no_tests || !r.ok || mute;
          const fails = (r.scripts || []).filter((s) => !s.ok);
          return (
            <div key={`${r.at}-${i}`}>
              <div className={`bgs-run${bad ? " bad" : ""}`}>
                <Tag tone={r.no_tests || mute ? "bad" : r.ok ? "good" : "bad"}
                     title={mute ? "the suite ran and printed no PASS marker at all — it asserted nothing"
                                 : r.no_tests ? "there was nothing on disk to run" : ""}>
                  {r.no_tests ? "NOTHING RUN" : mute ? "ASSERTED NOTHING" : r.ok ? "PASS" : "FAIL"}
                </Tag>
                <span className="t">
                  {r.no_tests
                    ? (r.error || "no test scripts were found")
                    : `${r.scripts_run} script(s) · ${r.passed} passed · ${r.failures} failed`}
                </span>
                {r.seconds ? <span className="w">{r.seconds}s</span> : null}
                {r.by && <span className="w">{r.by}</span>}
                <span className="w">{ago(r.at)}</span>
              </div>
              {/* THE REASON, NOT THE NAME. The failing scripts used to be
                  concatenated into the summary line as bare filenames, which
                  says which one to open and nothing about what went wrong —
                  while the run recorded the error text all along. */}
              {fails.map((s) => (
                <div className="bgq-fail" key={s.script}>
                  <b>{s.script}</b>
                  <span>{s.error || `${s.failed} FAIL marker(s)`}
                    {s.passed ? ` · ${s.passed} passed before it` : ""}</span>
                </div>
              ))}
            </div>
          );
        })}
      </div>
    );
  }

  /* ── CONTRACT ── */

  /** Save the hand-edited document. The `_version` marker is round-tripped
   *  inside it, so a save against a stale read is a 409 with both versions in
   *  it rather than a silent clobber of somebody else's edit. */
  async function saveContract() {
    if (draft === null) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(draft);
    } catch (err) {
      setDraftErr(`that is not JSON — ${(err as Error).message}`);
      return;
    }
    setDraftErr("");
    const r = await post("/api/qa-bots/contract", "save", { data: parsed });
    if (r.ok) {
      setDraft(null);
      setNonce((n) => n + 1);
      toast("contract declared — it will not be re-guessed", "ok");
    } else {
      setDraftErr(r.error || "the save was refused");
    }
  }

  /** One bot, driven against the real build. The old button POSTed `{}` to
   *  /run-all, which requires a non-empty `bots` list and answered 400 every
   *  single time: there is no stored roster to run, so "run every bot" was a
   *  button for a feature that does not exist. A bot is defined AT THE CALL —
   *  so the panel hands over a real spec, seeded from this project's own
   *  contract keys and its own InputMap actions, and runs that. */
  async function runBot() {
    if (spec === null) return;
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(spec) as Record<string, unknown>;
    } catch (err) {
      toast(`that is not JSON — ${(err as Error).message}`, "bad");
      return;
    }
    const out = await runJob("/api/qa-bots/run", "bot", parsed);
    if (!out) return;
    setLastBot(out);
    const v = String(out.verdict || "unknown");
    toast(`${String(parsed.bot || "bot")} — ${v}`, v === "pass" ? "ok" : v === "fail" ? "bad" : "warn");
  }

  if (tab === "contract") {
    const c = contract;
    const samples = c.samples || [];
    const byKey = new Map(samples.map((s) => [s.key, s]));
    const keys = c.sample_keys || samples.map((s) => s.key);
    const actions = acts.actions || [];
    /* The reference's rows, and every one of them is a field of the real
       contract — not a summary written beside it. */
    const kv: [string, string][] = [
      ["scene", c.scene || "—"],
      ["actors", (c.actors || []).map((a) => a.key).join(", ") || "none"],
      ["advance", c.tick?.method ? `${c.tick.method}() — named method`
                                 : (c.tick?.mode || "—")],
      ["shape", c.shape || "—"],
      ["source", c.source || "unknown"],
    ];
    const template = JSON.stringify({
      bot: "control: the player moves right",
      ticks: 60,
      actions: [{ action: actions[0] || "<no action in this project's InputMap>",
                  at_tick: 0, hold_ticks: 30 }],
      expect: [{ property: keys[0] || "<no sample key declared>",
                 comparator: "gt", value: 0,
                 label: "invert this comparator and it must go red" }],
    }, null, 2);

    return (
      <div className="bgs-split" style={{ ["--rail" as string]: "340px" }}>
        <div className="bgs-main">
          <Head label="Bot runs" hint="a probe that never ran proves nothing"
                right={
                  <button className="bgs-btn" disabled={!!busy}
                          onClick={() => setSpec(spec === null ? template : null)}>
                    {busy === "bot" ? "driving…" : spec === null ? "run a bot" : "close"}
                  </button>} />
          <ReadError error={runs.error} what="the bot runs" />
          {busy === "bot" && (
            <div className="bgq-job">
              <Ti name="loader" size={14} />
              <span>driving the game headless</span>
              <span className="sp" />
              <span>{stage || "…"}</span>
            </div>
          )}

          {spec !== null && (
            <div className="bgq-edit" style={{ padding: "0 0 12px" }}>
              <div className="why">
                A bot is one probe run: a schedule of real input actions, and
                expectations checked SERVER-SIDE against the samples the probe
                printed. Every expectation may only name a declared sample key
                (the rail lists them). Before you trust a green one, invert the
                comparator and make it go red — an expectation that has never
                been red has never been tested.
                {acts.source === "none" && (
                  <> <b style={{ color: "var(--warn)" }}>This project's
                  project.godot declares no input actions</b>, so any action
                  named here is pressed into nothing.</>)}
              </div>
              <textarea value={spec} spellCheck={false}
                        onChange={(e) => setSpec(e.target.value)} />
              <div className="row">
                <button className="bgs-btn" disabled={!!busy} onClick={() => void runBot()}>
                  drive it
                </button>
                <button className="bgs-btn" disabled={!!busy} onClick={() => setSpec(null)}>
                  cancel
                </button>
                {!!actions.length && (
                  <span className="why" style={{ marginLeft: "auto" }}>
                    actions: {actions.slice(0, 8).join(", ")}
                    {actions.length > 8 ? ` +${actions.length - 8}` : ""}
                  </span>)}
              </div>
            </div>
          )}

          {/* THE RUN THAT JUST HAPPENED, with the reason each expectation gave.
              A verdict with no evidence just moves the argument. */}
          {lastBot && (
            <div className="bgs-gate t-off" style={{ marginBottom: 12 }}>
              <div className="h">
                <Tag tone={BOT_TONE(String(lastBot.verdict || ""))}
                     title={BOT_WHY[String(lastBot.verdict || "")] || ""}>
                  {String(lastBot.verdict || "?")}
                </Tag>
                <span className="t">{String(lastBot.bot || "bot")}</span>
                <button className="bgs-btn" onClick={() => setLastBot(null)}>dismiss</button>
              </div>
              <div className="note">
                {BOT_WHY[String(lastBot.verdict || "")] || ""}
                {lastBot.error ? ` — ${String(lastBot.error)}` : ""}
              </div>
              {((lastBot.failures as { label?: string; reason?: string }[]) || []).map((f, i) => (
                <div className="bgq-fail" key={i}>
                  <b>{f.label || `expectation ${i}`}</b><span>{f.reason}</span>
                </div>
              ))}
              {!!(lastBot.contract_issues as string[] | undefined)?.length && (
                <div className="bgq-ev">
                  <span className="cap">what the probe could not work out</span>
                  {(lastBot.contract_issues as string[]).join("\n")}
                </div>
              )}
            </div>
          )}

          {!runs.rows.length && !runs.error && (
            <Nothing what="no bot has run"
                     how="run a bot here — it drives the real build headless and judges it against the contract on the right" />
          )}
          {runs.rows.map((r) => {
            const v = String(r.verdict || "unknown");
            return (
              <div className="bgs-lockrow" key={r.id} title={BOT_WHY[v] || ""}>
                <Tag tone={BOT_TONE(v)}>{v}</Tag>
                <span className="p">
                  {r.bot || `run ${r.id}`}
                  {/* ZERO EXPECTATIONS IS THE GREEN-FOR-FREE. It is why the
                      backend has an `unknown` verdict at all, and the count is
                      what proves the claim. */}
                  {` · ${r.expectations || 0} expectation${r.expectations === 1 ? "" : "s"}`}
                  {(r.failures || []).length
                    ? ` · ${r.failures?.[0]?.reason || r.failures?.[0]?.label}`
                    : ""}
                </span>
                {r.is_baseline && (
                  <Tag tone="off" title="the run the next one is diffed against — this is what 'when did this start failing' compares to">
                    baseline
                  </Tag>)}
                <span className="by">{ago(r.created_at)}</span>
              </div>
            );
          })}

          {c.why && (
            /* A DERIVED CONTRACT IS SOMETIMES WRONG and says why it chose what
               it chose. Hiding that sentence is how a bot ends up asserting
               against the wrong scene for a week. */
            <div className="bgs-reasons"><div>{c.why}</div></div>
          )}
          {!!(c.issues || []).length && (c.issues || []).map((s, i) => (
            <div className="bgs-finding" key={i}>
              <Ti name="alert-hexagon" size={14} color="var(--warn)" />
              <span>{s}</span>
            </div>
          ))}
          {/* THE OTHER SCENES IT COULD HAVE PICKED. When the derivation guessed
              wrong, this is the list the correction comes out of, and it was on
              the wire the whole time. */}
          {!!(c.alternatives || []).length && (
            <div className="bgs-reasons"><div>
              other scenes the derivation considered:{" "}
              {(c.alternatives || []).map((a) =>
                typeof a === "string" ? a : (a.scene || "")).filter(Boolean).join(", ")}
              {" — if the probe is watching the wrong one, edit the contract."}
            </div></div>
          )}
        </div>

        <div className="bgs-rail">
          <div className="bgs-railhead">
            <Ti name="file-code-2" size={15} color="var(--c-qa)" />
            <span className="lb">Probe contract</span>
            <button className="bgs-btn" style={{ marginLeft: "auto" }}
                    disabled={!!busy || draft !== null}
                    title="the whole document, editable — saving marks it declared, and a declared contract stops being re-guessed"
                    onClick={() => { setDraftErr(""); setDraft(JSON.stringify(c, null, 2)); }}>
              edit
            </button>
            <button className="bgs-btn" disabled={!!busy || draft !== null}
                    title="read the project again and replace this with what it says"
                    onClick={() => post("/api/qa-bots/contract/derive", "derive")
                              .then((r) => { if (r.ok) setNonce((n) => n + 1); })}>
              {busy === "derive" ? "…" : "re-derive"}
            </button>
          </div>

          {draft !== null ? (
            /* THE DOCUMENT ITSELF. It was a one-line prompt with the JSON as
               placeholder text before — grey hint text, gone on the first
               keystroke, which made "editable" mean "retype it from memory". */
            <div className="bgq-edit">
              <div className="why">
                One JSON document, not a code change. `_version` rides along in
                it: leave it alone and a second tab's save is refused instead of
                overwriting yours.
              </div>
              <textarea value={draft} spellCheck={false}
                        onChange={(e) => setDraft(e.target.value)} />
              {draftErr && <div className="err">{draftErr}</div>}
              <div className="row">
                <button className="bgs-btn" disabled={!!busy} onClick={() => void saveContract()}>
                  {busy === "save" ? "saving…" : "save"}
                </button>
                <button className="bgs-btn" disabled={!!busy}
                        onClick={() => { setDraft(null); setDraftErr(""); }}>cancel</button>
              </div>
            </div>
          ) : (
            <div className="bgs-railpad">
              <ReadError error={contract.__error} what="the probe contract" />
              {!c.scene ? (
                <Nothing what="no contract"
                         how="qa-bots derives one from the real scene; without it a bot has nothing it may address" />
              ) : (
                <>
                  <div className="bgs-reasons"><div>
                    {c.source === "derived"
                      ? "derived from the real scene · sometimes wrong · edit it"
                      : `${c.source || "unknown"} · what a bot may assert against`}
                  </div></div>
                  {kv.map(([k, v]) => (
                    <div className="kv" key={k}><span>{k}</span><b className="wrap">{v}</b></div>
                  ))}
                </>
              )}
            </div>
          )}

          <div className="bgs-railhead top">
            <Ti name="key" size={15} color="var(--text-3)" />
            <span className="lb">Sample keys</span>
            <span className="hint">the ONLY addressable properties</span>
          </div>
          <div className="bgs-keys" style={{ display: "block" }}>
            {!keys.length && (
              <Nothing what="no keys declared"
                       how="every expectation names one of these and nothing else; an undeclared property cannot be probed, and asking for one reads as 'the probe never sampled X' — which is a broken test, not a broken game" />
            )}
            {/* THE KEY AND WHAT IT ADDRESSES. A bare list of names does not say
                what a key is a measurement OF, which is the first thing anyone
                writing an expectation needs and it was already on the wire. */}
            {keys.map((k) => {
              const s = byKey.get(k);
              return (
                <div className="bgq-keyrow" key={k}>
                  <b>{k}</b>
                  <span className={s ? "" : "miss"}>
                    {s ? `${s.actor || "?"}.${s.property || "?"}`
                       : "declared with no actor or property — the probe cannot produce it"}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    );
  }

  /* ── GATE RUNS ── */

  /** A FAIL YOU ONLY WROTE DOWN IS A COMPLAINT, NOT A FIX ROUND. The reopen
   *  appends the reason to the item's own brief, so the next agent reads what
   *  to fix — which is why an empty one is refused here rather than spending
   *  another run on a guess. */
  async function reopen(it: Item) {
    const reason = await askText({
      title: `Reopen #${it.id}`,
      body: `${it.title}\n\nThe reason is appended to the item's brief and is what the next agent reads. Rank the nitpicks.`,
      label: "what is wrong, ranked",
      ok: "reopen",
    });
    if (reason === null) return;            // backed out, not confirmed-empty
    if (!reason.trim()) {
      toast("a reopen with no reason spends another run on a guess", "bad");
      return;
    }
    const r = await post(`/api/queue/${it.id}/reopen`, `reopen${it.id}`, { reason });
    if (r.ok) {
      toast(`#${it.id} is back in the queue`, "ok");
      setNonce((n) => n + 1);
    }
  }

  const shown = items.filter((it) => {
    const s = states.get(it.id)!;
    if (filter === "all") return true;
    if (filter === "fail") return s === "fail";
    if (filter === "pass") return s === "pass";
    return UNDECIDED.has(s);
  });

  return (
    <div className="bgs-pad">
      <Head label="Gate runs"
            hint="a run that finished without a verdict line decided nothing, in public"
            right={hist.gate?.label ? <Tag tone="off">{hist.gate.label}</Tag> : undefined} />
      <ReadError error={hist.__error} what="work history" />

      {/* SAID ONCE, LOUDLY. Under gate-mode `none` every closed item is ungated,
          and drawing two hundred alarm-red cards would only teach the reader to
          stop reading them. The standing fact belongs in one banner; the cards
          then only have to be legibly NOT-passes. */}
      {hist.gate?.mode === "none" && !!ungated.length && (
        <Banner icon="shield-off" tone="warn">
          <div className="t">
            <b>This project runs with no gate.</b> {ungated.length} of the
            newest {items.length} closed on their author's own word — no
            independent check was ever filed against them. Nothing below that is
            not a written PASS is a pass.
          </div>
          {hist.gate.env_override && <div className="s">{hist.gate.env_override}</div>}
        </Banner>
      )}
      {hist.gate?.mode !== "none" && hist.gate?.env_override && (
        <Banner icon="alert-triangle" tone="bad">
          <div className="t">{hist.gate.env_override}</div>
        </Banner>
      )}

      {/* THE THREE STATES, NAMED. If the panel cannot tell them apart on screen
          then nothing else on it matters, so the key is on the screen. */}
      {!!items.length && (
        <div className="bgq-legend">
          <span className="l"><i className="sw pass" /><span>PASS — a verdict was written, with evidence</span></span>
          <span className="l"><i className="sw fail" /><span>FAIL — a verdict was written, and it was no</span></span>
          <span className="l"><i className="sw none" /><span>no verdict — it finished and decided nothing</span></span>
        </div>
      )}

      {!!items.length && (
        <div className="bgq-filters">
          {([["all", `all ${items.length}`],
             ["undecided", `undecided ${unknown.length + ungated.length}`],
             ["fail", `failed ${items.filter((i) => states.get(i.id) === "fail").length}`],
             ["pass", `passed ${passed.length}`]] as const).map(([k, lb]) => (
            <button key={k} className={`bgs-btn${filter === k ? " on" : ""}`}
                    onClick={() => setFilter(k)}>{lb}</button>
          ))}
          <span className="sp" />
          {/* THE COUNTS ARE OVER WHAT IS LOADED, and say so. A chip reading
              "12 undecided" over a window of 25 out of 344 is a number about a
              list nobody is looking at unless the window is named. */}
          <span className="n">newest {items.length} of {total}</span>
          {items.length < total && (
            <button className="bgs-btn" onClick={() => setLimit((l) => Math.min(200, l + 75))}>
              load more
            </button>)}
        </div>
      )}

      {!items.length && <Nothing what="nothing has closed yet" how="every finished work item gets a verdict row here" />}
      {!!items.length && !shown.length && (
        <Nothing what={`nothing in the newest ${items.length} is ${filter}`}
                 how="load more, or clear the filter — this counts only what is loaded" />
      )}

      {shown.map((it) => {
        const s = states.get(it.id)!;
        const v = it.verdict;
        const isOpen = open === it.id;
        /* Evidence that EXISTS. The gate's transcript is only there when a gate
           run exists at all, and the agent's own closing words are only worth
           showing where nobody checked them — that is the text that looks like
           a pass and is not one. */
        const claim = (it.result || "").trim();
        const canReopen = CLOSED.has(it.status) && s !== "pass" && s !== "pending";
        return (
          <div className={`bgs-gate ${CARD[s]}`} key={it.id}>
            <div className="h">
              {/* The word, not the sentence. The backend's phrasing is right
                  underneath in the note; what belongs here is the one token a
                  reader scans a column of these for. */}
              <Tag tone={TONE[s]} title={v?.why || v?.label || "no verdict"}>
                {s === "undecided" ? "UNKNOWN" : (v?.label || "no verdict")}
              </Tag>
              <span className="t">{it.title}</span>
              <span className="w">#{it.id}</span>
              <span className="w">{ago(it.updated_at)}</span>
            </div>
            <div className="ev">
              <span className="pill" style={{ color: `var(--c-${it.seat}, var(--text-3))` }}>{it.seat}</span>
              <span className="pill">{it.status}</span>
              {it.turns ? <span className="pill">{it.turns} turns</span> : null}
              {it.attempts ? <span className="pill">{it.attempts} attempts</span> : null}
              {it.log_bytes ? <span className="pill">{Math.round(it.log_bytes / 1024)}kb log</span> : null}
              {usd(it.cost_usd) ? <span className="pill">{usd(it.cost_usd)}</span> : null}
              {v?.rounds ? <span className="pill">{v.rounds} gate rounds</span> : null}
              {v?.escalated ? <span className="pill warn">escalated</span> : null}
              {it.approved_by ? <span className="pill">approved by {it.approved_by}</span> : null}
              {/* WHO STOPPED IT. A run the harness killed is not a run that
                  decided anything, and the field was already on the wire. */}
              {it.stopped_by ? <span className="pill warn">stopped by {it.stopped_by}</span> : null}
            </div>
            {(v?.why || v?.short) && (
              <div className={`note${UNDECIDED.has(s) ? " loud" : ""}`}>
                {v?.why || v?.short}
              </div>
            )}

            <div className="bgq-acts">
              {/* THE EVIDENCE, ON THE CARD. `detail` is the gate run's own
                  result text — the VERDICT line and the nitpick list under it —
                  and `result` is what the closing agent claimed. Both were
                  fetched and neither was drawn. */}
              {(v?.detail || claim) && (
                <button className={`bgs-btn${isOpen ? " on" : ""}`}
                        onClick={() => setOpen(isOpen ? null : it.id)}>
                  {isOpen ? "hide evidence" : "evidence"}
                </button>)}
              {v?.gate_item ? (
                <button className="bgs-btn"
                        title="open the QA agent's own run — its transcript is the evidence for this verdict"
                        onClick={() => watchAgent(v.gate_item as number)}>
                  gate run #{v.gate_item}
                </button>) : null}
              <button className="bgs-btn" title="the log of the agent that did the work"
                      onClick={() => watchAgent(it.id)}>log</button>
              {canReopen && (
                <button className="bgs-btn" disabled={busy === `reopen${it.id}`}
                        title="send it back with a ranked nitpick list — a FAIL you only wrote down is a complaint"
                        onClick={() => void reopen(it)}>
                  {busy === `reopen${it.id}` ? "…" : "reopen"}
                </button>)}
            </div>

            {isOpen && v?.detail && (
              <div className="bgq-ev">
                <span className="cap">the gate run's own words{v.at ? ` · ${ago(v.at)} ago` : ""}</span>
                {v.detail}
                {v.detail.length >= 600 && <span className="more">{"\n… truncated at 600 chars — open the gate run for the rest"}</span>}
              </div>
            )}
            {isOpen && claim && (
              <div className="bgq-ev">
                <span className="cap">
                  what the agent claimed{UNDECIDED.has(s) ? " — and nobody checked" : ""}
                </span>
                {claim}
                {(it.result_len || 0) > claim.length &&
                  <span className="more">{`\n… ${it.result_len} chars in full — open the log`}</span>}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
