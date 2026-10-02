import { useState } from "react";
import { mutate, toast } from "../../bridge";
import { Head, Nothing, Tag, Banner, ReadError } from "./prims";
import type { Tone } from "./prims";
import { useJSON } from "./api";
import type { SeatBodyProps } from "./types";
import "./plan.css";

/* PLAN — the end state this seat builds toward, live against the build.
 *
 * Every seat gets this tab: its own discipline's plan (gameplay holds three:
 * gameplay, level, ui; art holds art and animation), and the director sees
 * all ten. A plan is written by domain_plan_set or a deployed brainstorm, not
 * here: the seat scope and the validator live on that one path, and an HTTP
 * editor would be a second, quieter door around both. What this tab writes is
 * the one thing that is the director's (and the human's) call: promoting spec
 * rows to the board.
 */

type Row = { name: string; state: string; slice: boolean; item?: number | null };
type Check = { check: string; state: string; at?: string | null; evidence?: string };
type Flag = { reason: string; source: string; at: string };
type Domain = {
  planned: boolean; seat: string; goal?: string; done_when?: string[];
  leaves_dark?: string[]; open_questions?: string[]; revision?: number;
  entries?: number; in_game?: number; slice?: number; complete?: boolean;
  checks?: Check[]; replan?: Flag[]; rows?: Row[];
};
type Finding = { domain: string; severity: string; text: string };
type Plans = {
  domains?: Record<string, Domain>; findings?: Finding[];
  held?: string[]; seat_domains?: Record<string, string>;
};

const STATE_TONE: Record<string, Tone> = {
  spec: "off", on_board: "seat", lost: "bad", built: "warn",
  wired: "good", verified: "good", test: "off",
};
const CHECK_TONE: Record<string, Tone> = {
  pass: "good", fail: "bad", stale: "warn", open: "off",
};
const FINDING_TONE: Record<string, Tone> = {
  gap: "warn", decide: "seat", replan: "bad", check: "bad", warn: "warn",
};

export function Plan({ seat, active }: SeatBodyProps) {
  const data = useJSON<Plans>("/api/plans", {}, 8000, active);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);

  const all = data.domains || {};
  const director = seat.role === "director";
  const mine = Object.keys(all).filter((d) => director || all[d].seat === seat.role);
  const findings = (data.findings || []).filter((f) => mine.includes(f.domain));
  const held = (data.held || []).includes(seat.role);
  const chosen = Object.keys(picked).filter((n) => picked[n]);

  async function promote() {
    if (!chosen.length) return;
    setBusy(true);
    const r = await mutate<{ filed?: { id: number }[] }>("/api/plans/promote",
      { body: { names: chosen }, quiet: true });
    setBusy(false);
    if (r.ok) {
      toast(`filed ${r.data?.filed?.length ?? 0} item(s)`, "ok");
      setPicked({});
      data.__refresh?.();
    } else {
      toast(r.error || "promote was refused", "error");
    }
  }

  return (
    <div className="bgp">
      <ReadError error={data.__error} what="the domain plans" />
      {held && (
        <Banner icon="hand-stop" tone="warn">
          This seat's work is held until its discipline has a plan. The
          planning item on the board is the one that dispatches.
        </Banner>
      )}
      {findings.length > 0 && (
        <div className="bgp-findings">
          <Head label="Findings" hint="the plans read against each other" />
          {findings.slice(0, 12).map((f, i) => (
            <div className="bgp-finding" key={i}>
              <Tag tone={FINDING_TONE[f.severity] || "off"}>{f.severity}</Tag>
              <span className="d">{f.domain}</span>
              <span className="t">{f.text}</span>
            </div>
          ))}
        </div>
      )}
      {director && chosen.length > 0 && (
        <div className="bgp-promote">
          <span>{chosen.length} row(s) selected</span>
          <button className="bgs-btn" disabled={busy} onClick={promote}>
            Promote to the board
          </button>
        </div>
      )}
      {mine.map((d) => {
        const p = all[d];
        if (!p.planned) {
          return (
            <div className="bgp-domain" key={d}>
              <Head label={d} right={<Tag tone="warn">no plan</Tag>} />
              <Nothing what={`No ${d} plan yet`}
                       how={`domain_plan_draft('${d}') then domain_plan_set — goal, done_when, every deliverable with its acceptance test`} />
            </div>
          );
        }
        return (
          <div className="bgp-domain" key={d}>
            <Head label={d}
                  hint={`rev ${p.revision} · ${p.in_game}/${p.entries} in the game · ${p.slice} in the slice`}
                  right={<Tag tone={p.complete ? "good" : "off"}>{p.complete ? "complete" : "in progress"}</Tag>} />
            <div className="bgp-goal">{p.goal}</div>
            {(p.replan || []).map((f) => (
              <div className="bgp-replan" key={f.source}>needs revision — {f.reason}</div>
            ))}
            <div className="bgp-checks">
              {(p.checks || []).map((c) => (
                <div className="bgp-check" key={c.check} title={c.evidence || "no evidence recorded"}>
                  <Tag tone={CHECK_TONE[c.state] || "off"}>{c.state}</Tag>
                  <span>{c.check}</span>
                </div>
              ))}
            </div>
            {(p.open_questions || []).length > 0 && (
              <ul className="bgp-q">
                {p.open_questions!.map((q) => <li key={q}>{q}</li>)}
              </ul>
            )}
            {(p.leaves_dark || []).length > 0 && (
              <div className="bgp-dark">leaves dark: {p.leaves_dark!.join(" · ")}</div>
            )}
            <div className="bgp-rows">
              {(p.rows || []).map((r) => (
                <label className="bgp-row" key={r.name}>
                  {director && (r.state === "spec" || r.state === "lost") && (
                    <input type="checkbox" checked={!!picked[r.name]}
                           onChange={(e) => setPicked({ ...picked, [r.name]: e.target.checked })} />
                  )}
                  <span className="n">{r.name}</span>
                  {r.slice && <Tag tone="seat">slice</Tag>}
                  <Tag tone={STATE_TONE[r.state] || "off"}>{r.state}</Tag>
                  {r.item ? <span className="i">#{r.item}</span> : null}
                </label>
              ))}
              {!(p.rows || []).length && <span className="bgs-dim">every entry is in the game</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
