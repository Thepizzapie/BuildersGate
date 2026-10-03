import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Ti } from "../Ti";
import { SEAT_COLOR } from "../nav";
import { askText, mutate, readJSON, toast } from "../../bridge";
import { useEvents } from "../../hooks";
import { MergeOrder } from "./MergeOrder";
import { Pipeline } from "./Pipeline";
import { Proof } from "./Proof";
import "./lifecycle.css";

/* LIFECYCLE — the board as a source-control history.
 *
 * Every ticket is a commit, every chain a branch, every dependency a merge
 * line (bgate_core/board/lifecycle.py lays it out, git-style: a ticket keeps
 * the lane of the parent it is the first child of, other children branch, and
 * a lane is released when its last ticket has no child). Open work sits on top
 * like a working tree over its history; running tickets pulse; a ticket that
 * has needed several runs says so on the row, because that is the ticket that
 * is the wrong shape.
 *
 * HUMAN CHECKPOINTS are diamonds. A finished one waits for you here: select it
 * and it lists everything that landed since the previous checkpoint, which is
 * what approving it signs off.
 *
 * FLUID ON PURPOSE: rows are absolutely positioned and keyed by ticket id, so
 * a poll that reorders the graph slides rows into place instead of redrawing.
 */

type Run = { started: number; ended: number | null; status: string; runner: string };
type Node = {
  id: number; title: string; seat: string; status: string; state: string;
  lane: number; row: number; parents: number[]; hidden_parents: number;
  chain_id: string; kind: string; severity: string; size: string;
  checkpoint: boolean; checkpoint_note: string; split_of: number | null;
  source: string; runs: number; run_log: Run[]; elapsed_s: number | null;
  spawned_by?: number | null;
  created_at: string; updated_at: string; acceptance: string; hold: string;
  waiting_on: number[]; result: string;
};
type Graph = {
  nodes: Node[]; edges: { from: number; to: number }[]; lanes: number;
  counts: Record<string, number>; checkpoints_waiting: number[];
  multi_run: number[]; __error?: string;
};
type Covers = { covers: { id: number; seat: string; title: string; status: string; runs: number }[] };

const WINDOWS: { label: string; hours: number }[] = [
  { label: "Live", hours: 6 }, { label: "24h", hours: 24 },
  { label: "7d", hours: 168 }, { label: "All", hours: 0 },
];
const LIVE = new Set(["running", "ready", "waiting", "held", "blocked", "checkpoint", "review", "parked"]);
const STATE_LABEL: Record<string, string> = {
  running: "running", ready: "ready", waiting: "waiting", held: "held",
  human: "needs you", question: "question for you",
  blocked: "blocked", checkpoint: "checkpoint", review: "review", done: "done",
  failed: "failed", cancelled: "cancelled", parked: "parked",
};

function color(seat: string): string {
  return SEAT_COLOR[seat] || "var(--text-3)";
}

export function Lifecycle({ active = true }: { active?: boolean }) {
  const [hours, setHours] = useState(24);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [pick, setPick] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [seatFilter, setSeatFilter] = useState<string>("");
  /* pipeline (the dependency picture) or order (what lands first). */
  const [mode, setMode] = useState<string>(() => {
    try {
      const got = localStorage.getItem("bgl-mode");
      return got === "order" ? "order" : "pipeline";
    } catch { return "pipeline"; }
  });
  const pickMode = (m: string) => { setMode(m); try { localStorage.setItem("bgl-mode", m); } catch { /* private */ } };

  async function load() {
    const got = await readJSON<Graph>(`/api/lifecycle?hours=${hours}&limit=400`,
      { nodes: [], edges: [], lanes: 0, counts: {}, checkpoints_waiting: [], multi_run: [] });
    /* A failed read keeps the last good graph and only flags the error: a
       dashboard restart used to blank the pane for good, since nothing
       retried until the next board event. */
    setGraph((prev) => (got.__error && prev && !prev.__error
      ? { ...prev, __error: got.__error } : got));
  }
  useEffect(() => { if (active) void load(); }, [hours, active]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!active || !graph?.__error) return;
    const t = window.setTimeout(() => { void load(); }, 3000);
    return () => window.clearTimeout(t);
  }, [graph, active]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEvents(() => { void load(); }, { enabled: active, fallbackMs: 4000 });
  const view = useMemo(() => {
    const all = graph?.nodes || [];
    let nodes = hours === 6 ? all.filter((n) => LIVE.has(n.state)
      || Date.now() - Date.parse(n.updated_at + "Z") < 6 * 3600e3) : all;
    if (seatFilter) {
      const keep = new Set(nodes.filter((n) => n.seat === seatFilter).map((n) => n.id));
      nodes = nodes.filter((n) => keep.has(n.id));
    }
    const ordered = [...nodes].sort((a, b) => a.row - b.row);
    return { ordered };
  }, [graph, hours, seatFilter]);

  const byId = useMemo(() => new Map((graph?.nodes || []).map((n) => [n.id, n])), [graph]);
  const seats = useMemo(() => [...new Set((graph?.nodes || []).map((n) => n.seat))].sort(), [graph]);
  const selected = pick != null ? byId.get(pick) || null : null;

  const body = (
    <div className={`bgl ${expanded ? "bgl-expanded" : ""}`}>
      <div className="bgl-head">
        <div className="bgl-windows">
          {WINDOWS.map((w) => (
            <button key={w.label} className={hours === w.hours ? "on" : ""}
                    onClick={() => setHours(w.hours)}>{w.label}</button>
          ))}
        </div>
        <div className="bgl-windows">
          {["pipeline", "order"].map((m) => (
            <button key={m} className={mode === m ? "on" : ""} onClick={() => pickMode(m)}>{m}</button>
          ))}
        </div>
        <select className="bgl-seat" value={seatFilter} onChange={(e) => setSeatFilter(e.target.value)}>
          <option value="">every seat</option>
          {seats.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <span style={{ flex: 1 }} />
        <button className="bgl-icon" title={expanded ? "back to the rail" : "full screen"}
                onClick={() => setExpanded(!expanded)}>
          <Ti name={expanded ? "minimize" : "maximize"} size={14} />
        </button>
      </div>
      <div className="bgl-counts">
        {(["running", "ready", "waiting", "held", "blocked", "checkpoint", "human", "question", "done", "failed"] as const)
          .filter((k) => graph?.counts?.[k])
          .map((k) => <span key={k} className={`bgl-count s-${k}`}>{graph!.counts[k]} {STATE_LABEL[k]}</span>)}
        {!!graph?.multi_run?.length && (
          <span className="bgl-count s-multirun" title="open tickets that have needed more than one run">
            {graph.multi_run.length} multi-run
          </span>)}
      </div>
      {graph?.__error && <div className="bgl-err">could not read the board — {graph.__error}</div>}
      {mode === "pipeline"
        ? <MapBody nodes={view.ordered} pick={pick} onPick={setPick} />
        : <MergeOrder active={active} seat={seatFilter} pick={pick} onPick={setPick} hours={hours} />}
      {selected && <Detail n={selected} byId={byId} onClose={() => setPick(null)} onChanged={load} />}
    </div>
  );
  /* A portal, because the rail is a transformed ancestor: position:fixed
     inside it is fixed to the rail, not to the window. */
  return expanded ? createPortal(<div className="bgl-overlay">{body}</div>, document.body) : body;
}

function MapBody({ nodes, pick, onPick }: {
  nodes: Node[]; pick: number | null; onPick: (id: number) => void;
}) {
  const live = nodes.filter((n) => n.state === "running");
  /* DRAG TO PAN: grab the background and move the board in any direction. A
     press on a card is a click, not a drag, so it is left alone. */
  const pan = useRef<HTMLDivElement>(null);
  const grab = useRef<{ x: number; y: number; l: number; t: number } | null>(null);
  const panHandlers = {
    onPointerDown: (e: React.PointerEvent) => {
      const el = pan.current;
      if (!el || e.button !== 0) return;
      if ((e.target as Element).closest(".bgp2-card, .bgm-station, .bgp2-fold")) return;
      grab.current = { x: e.clientX, y: e.clientY, l: el.scrollLeft, t: el.scrollTop };
      el.classList.add("panning");
      el.setPointerCapture(e.pointerId);
    },
    onPointerMove: (e: React.PointerEvent) => {
      const g = grab.current, el = pan.current;
      if (!g || !el) return;
      el.scrollLeft = g.l - (e.clientX - g.x);
      el.scrollTop = g.t - (e.clientY - g.y);
    },
    onPointerUp: (e: React.PointerEvent) => {
      grab.current = null;
      pan.current?.classList.remove("panning");
      try { pan.current?.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
    },
    /* Shift + wheel scrolls sideways on every mouse, not only on the ones
       whose driver already does it. */
    onWheel: (e: React.WheelEvent) => {
      const el = pan.current;
      if (el && e.shiftKey && !e.deltaX) { el.scrollLeft += e.deltaY; }
    },
  };
  const cards: [string, string | number, string?][] = [
    ["open", nodes.filter((n) => !["done", "cancelled", "failed"].includes(n.state)).length],
    ["running", live.length, live.length ? "var(--accent)" : undefined],
    ["done", nodes.filter((n) => n.state === "done").length, "var(--good)"],
    ["waiting on you", nodes.filter((n) => ["checkpoint", "human", "question"].includes(n.state)).length,
     nodes.some((n) => ["checkpoint", "human", "question"].includes(n.state)) ? "#ffbb45" : undefined],
    ["multi-run", nodes.filter((n) => n.runs >= 2).length,
     nodes.some((n) => n.runs >= 2) ? "var(--bad)" : undefined],
  ];
  return (
    <div className="bgm-wrap">
      <div className="bgm-stats">
        {cards.map(([k, v, c]) => (
          <div key={k}><span>{k}</span><b style={c ? { color: c } : undefined}>{v}</b></div>
        ))}
      </div>
      <div className="bgm-scroll" ref={pan} {...panHandlers}>
        {nodes.length
          ? <Pipeline nodes={nodes} pick={pick} onPick={onPick} />
          : <div className="bgl-empty">nothing on the board in this window</div>}
      </div>
    </div>
  );
}

function Detail({ n, byId, onClose, onChanged }: {
  n: Node; byId: Map<number, Node>; onClose: () => void; onChanged: () => void;
}) {
  const [covers, setCovers] = useState<Covers["covers"] | null>(null);
  const [openCover, setOpenCover] = useState<number | null>(null);
  useEffect(() => {
    setCovers(null);
    if (!n.checkpoint) return;
    void readJSON<Covers>(`/api/lifecycle/checkpoint/${n.id}`, { covers: [] })
      .then((got) => setCovers(got.covers || []));
  }, [n.id, n.checkpoint]);

  async function approve() {
    const r = await mutate(`/api/queue/${n.id}/approve`, { body: { note: "" }, quiet: true });
    toast(r.ok ? `#${n.id} approved — the work behind it is released` : r.error || "refused",
          r.ok ? "ok" : undefined);
    if (r.ok) onChanged();
  }
  async function humanDone() {
    const note = await askText({ title: `done: ${n.title.replace(/^YOU: /, "")}`,
      body: "Anything the work behind it should know? Optional.", ok: "mark done" });
    if (note === null || note === undefined) return;
    const r = await mutate(`/api/queue/${n.id}/human-done`, { body: { note }, quiet: true });
    toast(r.ok ? `#${n.id} done — the work behind it is released` : r.error || "refused", r.ok ? "ok" : undefined);
    if (r.ok) onChanged();
  }
  async function toggleCheckpoint() {
    let note = "";
    if (!n.checkpoint) {
      const got = await askText({ title: `make #${n.id} a checkpoint`,
        body: "What should you look at when it finishes? Nothing behind it runs until you approve.",
        ok: "make checkpoint" });
      if (got === null || got === undefined) return;
      note = got;
    }
    const r = await mutate(`/api/queue/${n.id}/checkpoint`, { body: { on: !n.checkpoint, note }, quiet: true });
    toast(r.ok ? (n.checkpoint ? `#${n.id} is no longer a checkpoint` : `#${n.id} will wait for your sign-off`)
                : r.error || "refused", r.ok ? "ok" : undefined);
    if (r.ok) onChanged();
  }
  async function humanBefore() {
    const title = await askText({ title: `a step for you before #${n.id}`,
      body: "What do you have to do first? The item waits until you mark it done.", ok: "add step" });
    if (!title) return;
    const r = await mutate(`/api/queue/${n.id}/human-before`, { body: { title }, quiet: true });
    toast(r.ok ? `#${n.id} now waits on your step` : r.error || "refused", r.ok ? "ok" : undefined);
    if (r.ok) onChanged();
  }
  const open = !["done", "cancelled"].includes(n.status) && n.id > 0;
  async function reject() {
    const reason = await askText({ title: `send #${n.id} back`,
      body: "What has to change? The next run reads this.", ok: "send back" });
    if (!reason) return;
    const r = await mutate(`/api/queue/${n.id}/reject`, { body: { reason }, quiet: true });
    toast(r.ok ? `#${n.id} sent back` : r.error || "refused", r.ok ? "ok" : undefined);
    if (r.ok) onChanged();
  }

  return (
    <div className="bgl-detail">
      <div className="bgl-dhead">
        <span className="bgl-id">#{n.id}</span>
        <b>{n.title}</b>
        <span style={{ flex: 1 }} />
        <button className="bgl-icon" onClick={onClose}><Ti name="x" size={13} /></button>
      </div>
      <div className="bgl-dmeta">
        {["human", "question"].includes(n.state)
          ? <span style={{ color: "#ffbb45" }}>you</span>
          : <span style={{ color: color(n.seat) }}>{n.seat}</span>}
        <span className={`bgl-state s-${n.state}`}>{STATE_LABEL[n.state] || n.state}</span>
        {n.kind && <span>{n.kind}</span>}
        {n.severity && <span className="sev">{n.severity}</span>}
        {n.id > 0 && n.source !== "human-task" && <span>{n.runs} run{n.runs === 1 ? "" : "s"}</span>}
        {n.chain_id && <span>chain {n.chain_id}</span>}
        {n.split_of && <span>split of #{n.split_of}</span>}
      </div>
      {n.hold && <div className="bgl-dline warn">{n.hold}</div>}
      {!!n.waiting_on.length && <div className="bgl-dline">waiting on {n.waiting_on.map((p) =>
        `#${p} ${byId.get(p)?.title?.slice(0, 40) || ""}`).join(", ")}</div>}
      {n.acceptance && n.source !== "human-task" && <div className="bgl-dline"><b>acceptance</b> {n.acceptance}</div>}
      {n.checkpoint && n.checkpoint_note && <div className="bgl-dline"><b>look at</b> {n.checkpoint_note}</div>}
      {covers && covers.length > 0 && (
        <div className="bgl-covers">
          <b>Approving signs off {covers.length} ticket{covers.length === 1 ? "" : "s"} since the last checkpoint</b>
          {covers.map((c) => <div key={c.id}>
            <button className="bgl-cover" onClick={() => setOpenCover(openCover === c.id ? null : c.id)}>
              <Ti name={openCover === c.id ? "chevron-down" : "chevron-right"} size={11} />
              #{c.id} <span style={{ color: color(c.seat) }}>{c.seat}</span> {c.title}
              <i> {c.status}{c.runs > 1 ? `, ${c.runs} runs` : ""}</i></button>
            {openCover === c.id && <Proof id={c.id} compact />}
          </div>)}
        </div>
      )}
      {n.id > 0 && n.source !== "human-task"
        ? <Proof id={n.id} />
        : n.result && <div className="bgl-dline result">{n.result}</div>}
      {n.state === "question" && (
        <div className="bgl-dline warn">An agent asked you this. Answer it in Needs attention; the asker keeps working meanwhile.</div>
      )}
      <div className="bgl-actions">
        {n.status === "review" && <>
          <button className="bgs-btn" onClick={approve}><Ti name="check" size={13} /> Approve</button>
          <button className="bgs-btn" onClick={reject}><Ti name="arrow-back-up" size={13} /> Send back</button>
        </>}
        {n.state === "human" &&
          <button className="bgs-btn" onClick={humanDone}><Ti name="check" size={13} /> Mark done</button>}
        {n.state === "question" &&
          <button className="bgs-btn" onClick={() => window.dispatchEvent(new CustomEvent("bgate:orchestration-tab",
            { detail: { tab: "attention" } }))}><Ti name="message-question" size={13} /> Answer it</button>}
        {open && n.source !== "human-task" && n.state !== "question" && <>
          <button className="bgs-btn" onClick={toggleCheckpoint}>
            <Ti name="flag" size={13} /> {n.checkpoint ? "Remove checkpoint" : "Make checkpoint"}</button>
          <button className="bgs-btn" onClick={humanBefore}><Ti name="user-plus" size={13} /> Add a step for you first</button>
        </>}
      </div>
    </div>
  );
}
