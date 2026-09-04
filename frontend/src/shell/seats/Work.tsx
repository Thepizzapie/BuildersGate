import { useMemo, useState } from "react";
import { mutate, toast } from "../../bridge";
import { Ti } from "../Ti";
import { Tag } from "./prims";
import { ago, usd, type QueueItem, type Seat } from "./api";
import { useSelection } from "../selection";

const LIVE = new Set(["dispatched", "running", "in_progress"]);
const OPEN = new Set(["queued", "ready", "review", "blocked", ...LIVE]);
const titleOf = (brief: string) =>
  brief.trim().split(/\r?\n/)[0].replace(/^[-*#\s]+/, "").slice(0, 80);

export function Work({ seat, items, onRefresh }: {
  seat: Seat;
  active: boolean;
  items: QueueItem[];
  onRefresh?: () => void;
}) {
  const [, setSel] = useSelection();
  const [brief, setBrief] = useState("");
  const [constraints, setConstraints] = useState("");
  const [acceptance, setAcceptance] = useState("");
  const [priority, setPriority] = useState(0);
  const [dependsOn, setDependsOn] = useState("");
  const [maxMinutes, setMaxMinutes] = useState("");
  const [busy, setBusy] = useState<"queue" | "start" | "">("");
  const current = useMemo(
    () => items.filter((item) => OPEN.has(item.status)).slice(0, 8), [items]);
  const recent = useMemo(
    () => items.filter((item) => !OPEN.has(item.status)).slice(0, 4), [items]);

  async function file(start: boolean) {
    const outcome = brief.trim();
    const text = [outcome,
      constraints.trim() && `\nConstraints\n${constraints.trim()}`,
      acceptance.trim() && `\nAcceptance\n${acceptance.trim()}`,
    ].filter(Boolean).join("\n");
    if (!text || busy || seat.enabled === false) return;
    setBusy(start ? "start" : "queue");
    const made = await mutate<{ id?: number }>("/api/queue", {
      body: { seat: seat.role, title: titleOf(outcome), brief: text,
              priority, source: "seat-desk",
              depends_on: dependsOn ? Number(dependsOn) : undefined,
              max_runtime_s: maxMinutes ? Number(maxMinutes) * 60 : undefined }, quiet: true,
    });
    if (!made.ok || !made.data?.id) {
      setBusy("");
      toast(made.error || "work could not be filed", "warn");
      return;
    }
    const id = made.data.id;
    if (start) {
      const sent = await mutate(`/api/queue/${id}/dispatch`, { quiet: true });
      if (!sent.ok) {
        toast(`Filed #${id}, but it did not start: ${sent.error || "dispatch refused"}`, "warn");
        setBusy(""); onRefresh?.(); return;
      }
    }
    setBrief(""); setConstraints(""); setAcceptance(""); setPriority(0);
    setDependsOn(""); setMaxMinutes(""); setBusy(""); onRefresh?.();
    toast(start ? `Started #${id} with ${seat.title}` : `Queued #${id} for ${seat.title}`, "ok");
  }

  function inspect(item: QueueItem) {
    setSel({ key: `i${item.id}`, kind: LIVE.has(item.status) ? "agent" : "item",
             itemId: item.id, title: item.title, seat: item.seat });
  }

  const rows = (list: QueueItem[]) => list.map((item) => (
    <button key={item.id} className="bgs-desk-item" onClick={() => inspect(item)}>
      <span className={`bgs-desk-state${LIVE.has(item.status) ? " live" : ""}`} />
      <span className="copy"><b>{item.title}</b><small>#{item.id} · {ago(item.updated_at)}</small></span>
      {usd(item.total_cost_usd) && <span className="cost">{usd(item.total_cost_usd)}</span>}
      <Tag tone={LIVE.has(item.status) ? "good" : item.status === "failed" ? "bad" : "off"}>
        {item.status}
      </Tag>
    </button>
  ));

  return (
    <div className="bgs-desk" style={{ ["--seat" as string]: `var(--c-${seat.role})` }}>
      <section className="bgs-brief">
        <div className="bgs-brief-head">
          <Ti name="send" size={18} color={`var(--c-${seat.role})`} />
          <div><h2>Brief {seat.title}</h2><p>{seat.mission}</p></div>
        </div>
        {seat.enabled === false && <div className="bgs-desk-disabled">This seat is disabled for this project.</div>}
        <label className="bgs-brief-field">Outcome
        <textarea value={brief} disabled={seat.enabled === false} rows={5}
                  placeholder={`What should ${seat.title} deliver?`}
                  onChange={(event) => setBrief(event.currentTarget.value)} />
        </label>
        <div className="bgs-brief-pair">
          <label className="bgs-brief-field">Constraints
            <textarea value={constraints} disabled={seat.enabled === false} rows={3}
                      placeholder="Files, references, limits, or decisions to preserve."
                      onChange={(event) => setConstraints(event.currentTarget.value)} />
          </label>
          <label className="bgs-brief-field">Acceptance
            <textarea value={acceptance} disabled={seat.enabled === false} rows={3}
                      placeholder="What evidence proves this is finished?"
                      onChange={(event) => setAcceptance(event.currentTarget.value)} />
          </label>
        </div>
        <div className="bgs-brief-tools">
          <label>Priority
            <select value={priority} onChange={(event) => setPriority(Number(event.currentTarget.value))}>
              <option value={0}>Normal</option><option value={1}>High</option><option value={2}>Urgent</option>
            </select>
          </label>
          <label>Wait for #
            <input inputMode="numeric" value={dependsOn} placeholder="none"
                   onChange={(event) => setDependsOn(event.currentTarget.value.replace(/\D/g, ""))} />
          </label>
          <label>Time ceiling min
            <input inputMode="numeric" value={maxMinutes} placeholder="none"
                   onChange={(event) => setMaxMinutes(event.currentTarget.value.replace(/\D/g, ""))} />
          </label>
          <span className="sp" />
          <button className="bgs-btn" disabled={!brief.trim() || !!busy || seat.enabled === false}
                  onClick={() => void file(false)}>{busy === "queue" ? "Filing…" : "Add to queue"}</button>
          <button className="bgs-btn go" disabled={!brief.trim() || !!busy || seat.enabled === false}
                  onClick={() => void file(true)}>
            <Ti name="player-play" size={13} />{busy === "start" ? "Starting…" : "Start now"}
          </button>
        </div>
        <details className="bgs-scope">
          <summary>Seat boundaries</summary>
          <p>{(seat.write_globs || []).length
            ? (seat.write_globs || []).join(" · ") : "No write paths declared."}</p>
        </details>
      </section>

      <aside className="bgs-desk-work">
        <div className="bgs-desk-title"><span>Current work</span><b>{current.length}</b></div>
        <div className="bgs-desk-list">
          {current.length ? rows(current) : <div className="bgs-desk-empty">No work is open. Write a brief to use this seat.</div>}
        </div>
        {!!recent.length && <>
          <div className="bgs-desk-title recent"><span>Recent</span></div>
          <div className="bgs-desk-list">{rows(recent)}</div>
        </>}
      </aside>
    </div>
  );
}
