import { useCallback, useRef, useState } from "react";
import { readJSON, seatColor, watchAgent } from "../bridge";
import { useEvents, useViewActive } from "../hooks";
import { Icon } from "../components/Icon";
import { consoleState, EMPTY_CONSOLE, type ConsoleState, type Item } from "../shell/agents/api";
import {
  gateAttentionKey, itemAttentionKey, questionAttentionKey, readDismissedAttention,
} from "../shell/agents/attention";
import { setSelection } from "../shell/selection";
import { openAttention } from "../shell/screen";
import "./overview.css";

type Artifact = { status?: string };
type Event = { seat?: string; summary?: string; kind?: string };

function openOrchestration(item?: Item) {
  if (item) setSelection({ key: `i${item.id}`, kind: "item", itemId: item.id,
    title: item.title, seat: item.seat });
  window.setWorkspace?.("agents");
}

export default function Overview() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [consoleData, setConsoleData] = useState<ConsoleState>(EMPTY_CONSOLE);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [sessions, setSessions] = useState<unknown[]>([]);
  const [assets, setAssets] = useState<unknown[]>([]);
  const [readError, setReadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [st, board, arts, act] = await Promise.all([
      readJSON<{ sessions?: unknown[]; assets?: unknown[] }>("/api/state", {}), consoleState(),
      readJSON<{ artifacts?: Artifact[] }>("/api/artifacts", { artifacts: [] }),
      readJSON<{ events?: Event[] }>("/api/activity?after_id=0", { events: [] }),
    ]);
    setReadError([st, board, arts, act].map((x) => x.__error).find(Boolean) || null);
    if (!board.__error) setConsoleData(board);
    setSessions(st.sessions || []); setAssets(st.assets || []);
    setArtifacts(arts.artifacts || []); setEvents(act.events || []);
  }, []);
  useEvents(refresh, { enabled: active });

  const items = consoleData.items || [];
  const dismissed = new Set([...(consoleData.dismissed_attention || []), ...readDismissedAttention()]);
  const liveAgents = (consoleData.agents || []).filter((a) => a.state === "running");
  const byId = new Map(items.map((i) => [i.id, i]));
  const failed = items.filter((i) => i.status === "failed" && !dismissed.has(itemAttentionKey(i)));
  const blocked = items.filter((i) => ["blocked", "held", "exhausted"].includes(i.execution_state || "")
    && !dismissed.has(itemAttentionKey(i)));
  const gates = (consoleData.gates || []).filter((g) => g.blocking !== false
    && !dismissed.has(gateAttentionKey(g)));
  const questions = consoleData.questions.filter((q) => !dismissed.has(questionAttentionKey(q)));
  const attentionCount = questions.length + gates.length + failed.length + blocked.length;
  const queued = items.filter((i) => i.status === "queued");
  const recent = events.slice(-8).reverse();

  return (
    <main ref={host} className="ov-brief">
      <section className={`ov-callout ${attentionCount ? "needs" : "clear"}`}>
        <div className="ov-callout-copy"><span className="ov-kicker">Studio briefing</span>
          <h2>{attentionCount ? `${attentionCount} decision${attentionCount === 1 ? "" : "s"} need you` : "The studio can keep moving"}</h2>
          <p>{attentionCount ? "Questions, approvals, blocked work, and failures are grouped in one place."
            : liveAgents.length ? `${liveAgents.length} agent${liveAgents.length === 1 ? " is" : "s are"} executing the current plan.`
            : queued.length ? `${queued.length} queued item${queued.length === 1 ? " is" : "s are"} ready for dispatch.`
            : "No work is blocked or waiting for a decision."}</p>
        </div>
        <button className="ov-primary" onClick={() => attentionCount ? openAttention() : openOrchestration()}>
          {attentionCount ? "Review decisions" : queued.length ? "Open queue" : "Direct the studio"}<Icon name="arrow-right" size={15} />
        </button>
      </section>

      <section className="ov-strip" aria-label="Studio totals">
        <div><strong>{liveAgents.length}</strong><span>Running</span></div><div><strong>{queued.length}</strong><span>Queued</span></div>
        <div><strong>{artifacts.filter((a) => a.status === "candidate").length}</strong><span>Awaiting review</span></div>
        <div><strong>{sessions.length}</strong><span>Playtests</span></div><div><strong>{assets.length}</strong><span>Tracked assets</span></div>
      </section>

      <div className="ov-grid">
        <section className="ov-panel ov-active"><div className="ov-panel-head"><h3>Active work</h3><button onClick={() => openOrchestration()}>View queue</button></div>
          <div className="ov-work-list">{liveAgents.length ? liveAgents.map((a) => { const item = byId.get(a.item_id); return <button key={a.item_id} className="ov-work" onClick={() => watchAgent(a.item_id)}>
            <span className="ov-live-dot" /><span className="ov-work-copy"><b>{item?.title || `Work item ${a.item_id}`}</b><small style={{ color: seatColor(item?.seat) }}>{item?.seat || "agent"}</small></span><Icon name="chevron-right" size={14} />
          </button>; }) : queued.slice(0, 5).map((item) => <button key={item.id} className="ov-work" onClick={() => openOrchestration(item)}>
            <span className="ov-queue-mark">{item.id}</span><span className="ov-work-copy"><b>{item.title}</b><small style={{ color: seatColor(item.seat) }}>{item.waiting_line || item.seat}</small></span><Icon name="chevron-right" size={14} />
          </button>)}{!liveAgents.length && !queued.length && <div className="ov-empty">No active or queued work.</div>}</div>
        </section>
        <section className="ov-panel"><div className="ov-panel-head"><h3>Latest from the studio</h3></div>
          <div className="ov-ledger">{recent.length ? recent.map((e, i) => <div key={i} className="ov-event"><span style={{ color: seatColor(e.seat) }}>{e.seat || "studio"}</span><p>{e.summary || e.kind || "Activity recorded"}</p></div>)
            : <div className={`ov-empty ${readError ? "err" : ""}`}>{readError ? `Could not read the studio — ${readError}` : "No recent activity."}</div>}</div>
        </section>
      </div>
    </main>
  );
}
