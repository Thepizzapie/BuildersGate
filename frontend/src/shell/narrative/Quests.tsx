import { useCallback, useMemo, useState } from "react";
import { ScrollArea } from "@mantine/core";
import { Ti } from "../Ti";
import { usePoll } from "../../hooks";
import {
  EMPTY_QUESTS, questAdd, questAddStep, questCutStep, questList, questSetState,
  type CanonVerdict, type Quest, type QuestBrief, type Step,
} from "./api";
import type { HeadSlot } from "./head";

/* QUESTS — the third noun in the seat's mission.
 *
 * "Own the lore graph, quests, and dialogue." Entities and facts had lore.py,
 * trees had dialogue.py, and quests had nothing — so this tab drew one sentence
 * saying quests are not modelled, on every project, forever. bgate_core/
 * quests.py, migration 0038 and bgate_ui/routes/quests.py are the other half of
 * this file.
 *
 * WHAT THE PANEL IS SHAPED AROUND is the one column that makes a quest a quest:
 * `done_when`. A step is drawn as two lines — what the player does, and the
 * observable that closes it — because a step list without the second line is a
 * to-do list nobody can finish, and the whole reason the column is mandatory is
 * that it is the one people skip.
 *
 * THE VERDICT IS DRAWN LIKE DIALOGUE'S, on purpose. Both panels answer the same
 * question ("does this hold together") and both name the step at fault rather
 * than saying "invalid", so the reader learns one vocabulary and not two.
 *
 * WRITING GOES THROUGH THE ROUTE, WHICH RUNS canon_check BEFORE IT WRITES. A
 * conflict comes back as a refusal with the flag that caused it; a review flag
 * lands and is reported. That is the same latitude the MCP tool gives an agent —
 * "the wizard is still draft" is information, not an error.
 */

const POLL_MS = 15000;

const STATE_TONE: Record<string, string> = {
  draft: "var(--text-3)", active: "var(--good)",
  done: "var(--c-narrative)", cut: "var(--text-dim)",
};

type Draft = { text: string; done_when: string; optional: boolean };

const BLANK: Draft = { text: "", done_when: "", optional: false };

export function Quests({ head, active }: { head: HeadSlot; active: boolean }) {
  const [brief, setBrief] = useState<QuestBrief & { __error?: string }>(EMPTY_QUESTS);
  const [pick, setPick] = useState("");
  const [writing, setWriting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  /* The new-quest form. Steps are local until the whole thing is posted —
     quests.add takes them in one call because a quest with no steps is one of
     the three refusals, so a create-then-append flow would put every quest
     through an invalid state on the way in. */
  const [title, setTitle] = useState("");
  const [premise, setPremise] = useState("");
  /* `reward` was drawn on the detail pane and had no input anywhere, so the
     column could only ever be filled by an MCP call — a field the panel reads
     and cannot write. It is create-only here on purpose: PATCH /api/quests
     runs no canon check (see api.ts), and reward is prose. */
  const [reward, setReward] = useState("");
  const [giver, setGiver] = useState("");
  const [steps, setSteps] = useState<Draft[]>([{ ...BLANK }]);

  /* The verdict the write came back with. POST refuses a `conflict` outright
     (that arrives as an error), so what lands here is the review level — the
     draft entities and unknown names this quest just introduced. It is the
     information content of a successful narrative write and it was being
     discarded. */
  const [said, setSaid] = useState<{ what: string; canon: CanonVerdict } | null>(null);

  /* Appending to an existing quest, which IS a one-at-a-time act. */
  const [adding, setAdding] = useState<Draft | null>(null);

  const load = useCallback(async () => {
    setBrief(await questList());
  }, []);
  usePoll(load, POLL_MS, active);

  const quests = brief.quests || [];
  const quest: Quest | undefined =
    quests.find((q) => q.slug === pick) || quests[0];

  const broken = useMemo(() => quests.filter((q) => !q.ok).length, [quests]);

  const chips = (
    <>
      {!!quests.length && (
        <span className="bg4-narchip">{quests.length} quests</span>
      )}
      {!!quests.length && (
        <span className="bg4-narchip"
              style={{ color: broken ? "var(--bad)" : "var(--good)" }}>
          <Ti name={broken ? "alert-hexagon" : "circle-check"} size={13} />
          {broken ? `${broken} do not hold together` : "all hold together"}
        </span>
      )}
    </>
  );

  const reset = () => {
    setTitle(""); setPremise(""); setReward(""); setGiver("");
    setSteps([{ ...BLANK }]); setErr(""); setWriting(false);
  };

  const file = async () => {
    setBusy(true);
    setErr("");
    setSaid(null);
    const usable = steps.filter((s) => s.text.trim() && s.done_when.trim());
    const r = await questAdd({
      title, premise, reward, giver: giver || undefined,
      steps: usable.map((s) => ({
        text: s.text, done_when: s.done_when, optional: s.optional,
      })),
    });
    setBusy(false);
    if (!r.ok) { setErr(r.error || "the write failed"); return; }
    const slug = r.data?.quest?.slug || "";
    if (r.data?.canon) setSaid({ what: "this quest", canon: r.data.canon });
    reset();
    setPick(slug);
    load();
  };

  const append = async () => {
    if (!quest || !adding) return;
    setBusy(true);
    setErr("");
    setSaid(null);
    const r = await questAddStep(quest.slug, {
      text: adding.text, done_when: adding.done_when, optional: adding.optional,
    });
    setBusy(false);
    if (!r.ok) { setErr(r.error || "the write failed"); return; }
    if (r.data?.canon) setSaid({ what: "this step", canon: r.data.canon });
    setAdding(null);
    load();
  };

  const cut = async (step: Step) => {
    setBusy(true);
    await questCutStep(step.id);
    setBusy(false);
    load();
  };

  const setState = async (next: string) => {
    if (!quest) return;
    setErr("");
    const r = await questSetState(quest.slug, next);
    /* A refused PATCH used to leave the select showing the new value until the
       next 15s poll silently put the old one back — the failure looked exactly
       like the success for a quarter of a minute. */
    if (!r.ok) setErr(r.error || "the state did not move");
    load();
  };

  /* THE NEW-QUEST FORM IS A STEP TABLE, not two free-text areas. done_when has
     its own column with its own placeholder because the placeholder is the
     teaching: an example of an observable, next to an example of an action. */
  const form = (
    <div className="bg4-qform">
      <label>
        <span>the quest</span>
        <input value={title} onChange={(e) => setTitle(e.target.value)}
               placeholder="The unsigned form" />
      </label>
      <label>
        <span>premise</span>
        <textarea rows={2} value={premise}
                  onChange={(e) => setPremise(e.target.value)}
                  placeholder="A form from before the merger was never signed, and the department cannot close its quarter until it is." />
      </label>
      <label>
        <span>reward</span>
        <input value={reward} onChange={(e) => setReward(e.target.value)}
               placeholder="the signed form, and a name in Accounting who owes you one" />
      </label>
      <label>
        <span>given by</span>
        <select value={giver} onChange={(e) => setGiver(e.target.value)}>
          <option value="">nobody — it comes from the world</option>
          {(brief.givers || []).map((g) => (
            <option key={g.slug} value={g.slug}>
              {g.name} · {g.kind}{g.status === "canon" ? "" : ` · ${g.status}`}
            </option>
          ))}
        </select>
      </label>

      <div className="bg4-qsteps">
        <div className="hd">
          <span>what the player does</span>
          <span>done when — the observable that closes it</span>
          <span className="o">optional</span>
          <span />
        </div>
        {steps.map((s, i) => (
          <div className="row" key={i}>
            <input value={s.text} placeholder="Find the form"
                   onChange={(e) => setSteps(steps.map((x, j) =>
                     j === i ? { ...x, text: e.target.value } : x))} />
            <input value={s.done_when}
                   placeholder="form_found is true in the save"
                   onChange={(e) => setSteps(steps.map((x, j) =>
                     j === i ? { ...x, done_when: e.target.value } : x))} />
            <label className="o" title="a step that does not gate completion">
              <input type="checkbox" checked={s.optional}
                     onChange={(e) => setSteps(steps.map((x, j) =>
                       j === i ? { ...x, optional: e.target.checked } : x))} />
            </label>
            <button className="bgs-btn" disabled={steps.length === 1}
                    onClick={() => setSteps(steps.filter((_, j) => j !== i))}
                    title={steps.length === 1
                      ? "a quest with no steps is refused"
                      : "drop this step"}>
              <Ti name="x" size={12} />
            </button>
          </div>
        ))}
        <button className="bgs-btn"
                onClick={() => setSteps([...steps, { ...BLANK }])}>
          <Ti name="plus" size={12} /> another step
        </button>
      </div>

      {err && <div className="bgs-readerr">{err}</div>}
      <div className="acts">
        <span className="hint">
          canon_check runs on the title, the premise and every done_when before
          this lands — the reward is not read by it
        </span>
        <span className="sp" />
        <button className="bgs-btn" onClick={reset} disabled={busy}>cancel</button>
        <button className="bgs-btn go" onClick={file}
                disabled={busy || !title.trim()
                          || !steps.some((s) => s.text.trim() && s.done_when.trim())}>
          {busy ? "…" : "file the quest"}
        </button>
      </div>
    </div>
  );

  return (
    <div className="bg4-nar-dlg">
      <div className="bg4-narlist">
        <div className="bg4-narlist-head">
          <Ti name="flag" size={14} />
          <span className="ro">{quests.length} quests</span>
          <button className="bgs-btn" onClick={() => setWriting(true)}
                  style={{ marginLeft: "auto" }}>new</button>
        </div>
        <ScrollArea className="bg4-narscroll">
          {!quests.length && (
            /* NOT "see the canvas". There is no canvas on this tab — that
               sentence was carried over from the dialogue rail, where the word
               means the graph. Here it points at either the empty state to the
               right or, if `new` was pressed, at a form; and when the READ
               failed there are no quests to list for a reason that has nothing
               to do with there being none. */
            <div className="bg4-empty">
              {brief.__error
                ? `could not read the quests — ${brief.__error}`
                : writing ? "filling in the form on the right"
                : "no quests yet — “new” above writes the first one"}
            </div>
          )}
          {quests.map((q) => (
            <button key={q.slug}
                    className={`bg4-narrow${q.slug === quest?.slug && !writing ? " on" : ""}`}
                    onClick={() => { setWriting(false); setPick(q.slug); }}>
              <span className="l">
                <Ti name={q.ok ? "flag" : "alert-hexagon"} size={14}
                    color={q.ok ? STATE_TONE[q.state] : "var(--bad)"} />
                <span className="s">{q.title}</span>
              </span>
              <span className="k">
                {q.ok
                  ? `${q.steps.length} step${q.steps.length === 1 ? "" : "s"} · ${q.state}`
                  : q.problems[0]?.kind.replace("-", " ")}
              </span>
            </button>
          ))}
        </ScrollArea>
      </div>

      <div className="bg4-narmain">
        {head(chips)}

        <div className="bg4-qbody">
          {/* WHAT THE GATE SAID ABOUT THE THING THAT JUST LANDED. Drawn with
              Lore's verdict classes rather than a second style, because it is
              the same verdict from the same checker and the reader should not
              have to learn it twice. */}
          {said && (
            <div className={`bg4-narverdict ${said.canon.verdict}`}
                 style={{ marginBottom: 14 }}>
              <div className="h">
                <Ti name={said.canon.verdict === "ok" ? "circle-check" : "eye"} size={16} />
                <b>canon_check · {said.canon.verdict} — {said.what} landed</b>
                <span className="sp" />
                <button className="bgs-btn" onClick={() => setSaid(null)}>dismiss</button>
              </div>
              {said.canon.verdict === "ok" ? (
                <div className="f">
                  <span className="t">
                    nothing to look at · {said.canon.canon.length} facts consulted
                  </span>
                </div>
              ) : said.canon.flags.map((f, i) => (
                <div className={`f ${f.level}`} key={i}>
                  <span className="c">{f.code}</span>
                  <span className="t">{f.message || f.code}</span>
                  {f.canon && <span className="q">canon: {f.canon}</span>}
                </div>
              ))}
            </div>
          )}
          {writing ? form : brief.__error ? (
            <div className="bg4-narempty">
              <b>the quest list could not be read</b>
              <span>{brief.__error}</span>
            </div>
          ) : !quest ? (
            <div className="bg4-narempty">
              <b>no quests yet</b>
              <span>
                A quest here is a title, who hands it out, and an ordered list of
                steps — each naming the observable that closes it. That last part
                is the whole point and it is mandatory: a step reading “talk to
                the accounting wizard” cannot be finished by anything, because
                nothing says what counted. <code>done_when</code> is where you
                say it.
              </span>
              <div className="acts">
                <button className="bgs-btn go" onClick={() => setWriting(true)}>
                  write the first quest
                </button>
                <span className="hint">
                  or call <code>quest_add</code> from the narrative seat
                </span>
              </div>
            </div>
          ) : (
            <>
              <div className="bg4-qhead">
                <h3>{quest.title}</h3>
                {quest.giver
                  ? <span className="giver">
                      <Ti name="user" size={12} />{quest.giver.name}
                    </span>
                  : <span className="giver none" title="not handed out by anybody in the lore graph">
                      from the world
                    </span>}
                <span className="sp" />
                {/* The state is the one field worth changing from here: it is
                    what moves a quest from written to in-the-game, and it is a
                    single word with a fixed vocabulary. Everything else is
                    prose and belongs to whoever is writing it. */}
                <select className="bg4-qstate" value={quest.state}
                        onChange={(e) => setState(e.target.value)}>
                  {(brief.states || []).map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>

              {quest.premise && <p className="bg4-qpremise">{quest.premise}</p>}

              {!quest.ok && (
                <div className="bg4-qrefuse">
                  <div className="h">
                    <Ti name="alert-hexagon" size={16} />
                    <b>
                      This quest does not hold together — {quest.problems.length}{" "}
                      problem{quest.problems.length === 1 ? "" : "s"}
                    </b>
                  </div>
                  {quest.problems.map((p, i) => (
                    <div className="p" key={i}>
                      <span className="k">{p.kind.replace("-", " ")}</span>
                      <span className="t">{p.text}</span>
                    </div>
                  ))}
                </div>
              )}

              <div className="bg4-qlist">
                {quest.steps.map((s) => (
                  <div className={`st${s.optional ? " opt" : ""}`} key={s.id}>
                    <span className="n">{s.ord + 1}</span>
                    <div className="b">
                      <div className="t">{s.text}</div>
                      {/* THE SECOND LINE IS THE POINT OF THE PANEL. Drawn with
                          its own label rather than as a subtitle, so it cannot
                          be read as a description of the step. */}
                      <div className="w">
                        <span className="lb">done when</span>
                        {s.done_when}
                      </div>
                    </div>
                    {!!s.optional && <span className="tagopt">optional</span>}
                    <button className="bgs-btn" disabled={busy}
                            onClick={() => cut(s)} title="cut this step and close the gap">
                      <Ti name="x" size={12} />
                    </button>
                  </div>
                ))}
              </div>

              {adding ? (
                <div className="bg4-qadd">
                  <input autoFocus value={adding.text} placeholder="what the player does"
                         onChange={(e) => setAdding({ ...adding, text: e.target.value })} />
                  <input value={adding.done_when}
                         placeholder="done when — the observable that closes it"
                         onChange={(e) => setAdding({ ...adding, done_when: e.target.value })} />
                  <label title="a step that does not gate completion">
                    <input type="checkbox" checked={adding.optional}
                           onChange={(e) => setAdding({ ...adding, optional: e.target.checked })} />
                    optional
                  </label>
                  <button className="bgs-btn" onClick={() => setAdding(null)}>cancel</button>
                  <button className="bgs-btn go" disabled={busy
                            || !adding.text.trim() || !adding.done_when.trim()}
                          onClick={append}>add</button>
                </div>
              ) : (
                <button className="bgs-btn" onClick={() => setAdding({ ...BLANK })}>
                  <Ti name="plus" size={12} /> add a step
                </button>
              )}

              {err && <div className="bgs-readerr">{err}</div>}
              {quest.reward && (
                <div className="bg4-qreward">
                  <Ti name="gift" size={14} />{quest.reward}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
