import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Ti } from "../Ti";
import { SEAT_COLOR } from "../nav";
import { askText, mutate, readJSON, toast } from "../../bridge";
import { useEvents } from "../../hooks";
import { SubwayMap } from "./SubwayMap";
import { Pipeline } from "./Pipeline";
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

const ROW = 30;
const LANE = 15;
const PAD = 10;
const WINDOWS: { label: string; hours: number }[] = [
  { label: "Live", hours: 6 }, { label: "24h", hours: 24 },
  { label: "7d", hours: 168 }, { label: "All", hours: 0 },
];
const LIVE = new Set(["running", "ready", "waiting", "held", "blocked", "checkpoint", "review", "parked"]);
const STATE_LABEL: Record<string, string> = {
  running: "running", ready: "ready", waiting: "waiting", held: "held",
  blocked: "blocked", checkpoint: "checkpoint", review: "review", done: "done",
  failed: "failed", cancelled: "cancelled", parked: "parked",
};

function color(seat: string): string {
  return SEAT_COLOR[seat] || "var(--text-3)";
}

function fmtDur(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function Lifecycle({ active = true }: { active?: boolean }) {
  const [hours, setHours] = useState(24);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [pick, setPick] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [seatFilter, setSeatFilter] = useState<string>("");
  /* Flow (the pipeline) or Log (the git-style history list). */
  const [mode, setMode] = useState<string>(() => {
    try {
      const got = localStorage.getItem("bgl-mode");
      return got === "log" || got === "map" ? got : "pipeline";
    } catch { return "pipeline"; }
  });
  const pickMode = (m: string) => { setMode(m); try { localStorage.setItem("bgl-mode", m); } catch { /* private */ } };
  const [tick, setTick] = useState(0);

  async function load() {
    const got = await readJSON<Graph>(`/api/lifecycle?hours=${hours}&limit=400`,
      { nodes: [], edges: [], lanes: 0, counts: {}, checkpoints_waiting: [], multi_run: [] });
    setGraph(got);
  }
  useEffect(() => { if (active) void load(); }, [hours, active]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEvents(() => { void load(); }, { enabled: active, fallbackMs: 4000 });
  /* The elapsed clocks on running rows tick between polls. */
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [active]);

  const view = useMemo(() => {
    const all = graph?.nodes || [];
    let nodes = hours === 6 ? all.filter((n) => LIVE.has(n.state)
      || Date.now() - Date.parse(n.updated_at + "Z") < 6 * 3600e3) : all;
    if (seatFilter) {
      const keep = new Set(nodes.filter((n) => n.seat === seatFilter).map((n) => n.id));
      nodes = nodes.filter((n) => keep.has(n.id));
    }
    /* Newest first: a working tree over its history. Display row = reversed
       topological row, compacted after filtering. */
    const ordered = [...nodes].sort((a, b) => b.row - a.row);
    const y = new Map<number, number>();
    ordered.forEach((n, i) => y.set(n.id, i));
    const lanesUsed = Math.max(1, ...nodes.map((n) => n.lane + 1));
    return { ordered, y, lanesUsed };
  }, [graph, hours, seatFilter]);

  const byId = useMemo(() => new Map((graph?.nodes || []).map((n) => [n.id, n])), [graph]);
  const gutter = PAD * 2 + view.lanesUsed * LANE;
  const height = Math.max(ROW, view.ordered.length * ROW);
  const seats = useMemo(() => [...new Set((graph?.nodes || []).map((n) => n.seat))].sort(), [graph]);
  const selected = pick != null ? byId.get(pick) || null : null;

  /* The run timeline (expanded view): one bar per run across the window. */
  const span = useMemo(() => {
    const starts = (graph?.nodes || []).flatMap((n) => n.run_log.map((r) => r.started));
    const now = Date.now() / 1000;
    const lo = starts.length ? Math.min(...starts) : now - 3600;
    return { lo, hi: now, w: Math.max(60, now - lo) };
  }, [graph, tick]);   // eslint-disable-line react-hooks/exhaustive-deps

  const cx = (n: Node) => PAD + n.lane * LANE + LANE / 2;
  const cy = (n: Node) => (view.y.get(n.id) || 0) * ROW + ROW / 2;

  const edges = (graph?.edges || []).filter((e) => view.y.has(e.from) && view.y.has(e.to));

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
          {["pipeline", "map", "log"].map((m) => (
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
        {(["running", "ready", "waiting", "held", "blocked", "checkpoint", "done", "failed"] as const)
          .filter((k) => graph?.counts?.[k])
          .map((k) => <span key={k} className={`bgl-count s-${k}`}>{graph!.counts[k]} {STATE_LABEL[k]}</span>)}
        {!!graph?.multi_run?.length && (
          <span className="bgl-count s-multirun" title="open tickets that have needed more than one run">
            {graph.multi_run.length} multi-run
          </span>)}
      </div>
      {graph?.__error && <div className="bgl-err">could not read the board — {graph.__error}</div>}
      {mode !== "log" && <MapBody nodes={view.ordered} pick={pick} onPick={setPick} pipeline={mode === "pipeline"} />}
      <div className="bgl-scroll" style={mode !== "log" ? { display: "none" } : undefined}>
        {!view.ordered.length && <div className="bgl-empty">nothing on the board in this window</div>}
        <div className="bgl-canvas" style={{ height }}>
          <svg className="bgl-svg" width={gutter} height={height}>
            {edges.map((e) => {
              const a = byId.get(e.from)!, b = byId.get(e.to)!;
              const x1 = cx(a), y1 = cy(a), x2 = cx(b), y2 = cy(b);
              const mid = (y1 + y2) / 2;
              const d = x1 === x2 ? `M${x1},${y1} L${x2},${y2}`
                : `M${x1},${y1} C${x1},${mid} ${x2},${mid} ${x2},${y2}`;
              const live = b.state === "running" || b.state === "ready";
              return <path key={`${e.from}-${e.to}`} d={d}
                           className={`bgl-edge${live ? " live" : ""}${b.state === "blocked" ? " dead" : ""}`}
                           stroke={color(b.seat)} />;
            })}
            {view.ordered.map((n) => {
              const x = cx(n), y = cy(n), c = color(n.seat);
              const cls = `bgl-dot s-${n.state}${pick === n.id ? " picked" : ""}`;
              return (
                <g key={n.id} className={cls} onClick={() => setPick(n.id)}
                   style={{ transform: `translate(${x}px, ${y}px)` }}>
                  {n.checkpoint
                    ? <rect x={-6} y={-6} width={12} height={12} transform="rotate(45)"
                            fill={n.state === "done" || n.state === "checkpoint" ? "#ffbb45" : "var(--surface-1, #0f1115)"}
                            stroke="#ffbb45" strokeWidth={2} />
                    : <circle r={5.5} stroke={c} strokeWidth={2}
                              fill={["done", "running"].includes(n.state) ? c : "var(--surface-1, #0f1115)"} />}
                  {n.state === "running" && <circle r={9} className="bgl-pulse" stroke={c} />}
                  {n.state === "checkpoint" && <circle r={11} className="bgl-pulse" stroke="#ffbb45" />}
                </g>
              );
            })}
          </svg>
          {view.ordered.map((n) => {
            const y = (view.y.get(n.id) || 0) * ROW;
            const open = n.run_log.find((r) => r.ended == null);
            const elapsed = open ? Date.now() / 1000 - open.started : n.elapsed_s;
            return (
              <div key={n.id} className={`bgl-row s-${n.state}${pick === n.id ? " picked" : ""}`}
                   style={{ transform: `translateY(${y}px)`, left: gutter }}
                   onClick={() => setPick(n.id)} title={n.hold || n.title}>
                <span className="bgl-id">#{n.id}</span>
                <span className="bgl-title">{n.title}</span>
                {n.checkpoint && <span className="bgl-tag cp" title={n.checkpoint_note || "human checkpoint"}>
                  <Ti name="flag" size={11} /></span>}
                {n.runs >= 2 && <span className={`bgl-tag runs${n.runs >= 3 ? " hot" : ""}`}
                                      title="runs this ticket has needed">{n.runs}×</span>}
                {n.state === "running" && elapsed != null &&
                  <span className="bgl-tag clock">{fmtDur(elapsed)}</span>}
                <span className="bgl-seatchip" style={{ color: color(n.seat) }}>{n.seat}</span>
                <span className={`bgl-state s-${n.state}`}>{STATE_LABEL[n.state] || n.state}</span>
                {expanded && <RunBars n={n} span={span} />}
              </div>
            );
          })}
        </div>
      </div>
      {selected && <Detail n={selected} byId={byId} onClose={() => setPick(null)} onChanged={load} />}
    </div>
  );
  /* A portal, because the rail is a transformed ancestor: position:fixed
     inside it is fixed to the rail, not to the window. */
  return expanded ? createPortal(<div className="bgl-overlay">{body}</div>, document.body) : body;
}

function MapBody({ nodes, pick, onPick, pipeline }: {
  nodes: Node[]; pick: number | null; onPick: (id: number) => void; pipeline: boolean;
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
    ["to sign off", nodes.filter((n) => n.state === "checkpoint").length,
     nodes.some((n) => n.state === "checkpoint") ? "#ffbb45" : undefined],
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
          ? pipeline ? <Pipeline nodes={nodes} pick={pick} onPick={onPick} />
            : <SubwayMap nodes={nodes} pick={pick} onPick={onPick} />
          : <div className="bgl-empty">nothing on the board in this window</div>}
      </div>
    </div>
  );
}

function RunBars({ n, span }: { n: Node; span: { lo: number; hi: number; w: number } }) {
  return (
    <span className="bgl-runs">
      {n.run_log.map((r, i) => {
        const end = r.ended ?? span.hi;
        const left = ((r.started - span.lo) / span.w) * 100;
        const width = Math.max(0.6, ((end - r.started) / span.w) * 100);
        return <i key={i} className={`r-${r.ended ? r.status : "open"}`}
                  style={{ left: `${left}%`, width: `${width}%`, background: r.ended ? undefined : color(n.seat) }}
                  title={`run ${i + 1}: ${fmtDur(end - r.started)} ${r.ended ? r.status : "running"}`} />;
      })}
    </span>
  );
}

function Detail({ n, byId, onClose, onChanged }: {
  n: Node; byId: Map<number, Node>; onClose: () => void; onChanged: () => void;
}) {
  const [covers, setCovers] = useState<Covers["covers"] | null>(null);
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
        <span style={{ color: color(n.seat) }}>{n.seat}</span>
        <span className={`bgl-state s-${n.state}`}>{STATE_LABEL[n.state] || n.state}</span>
        {n.kind && <span>{n.kind}</span>}
        {n.severity && <span className="sev">{n.severity}</span>}
        <span>{n.runs} run{n.runs === 1 ? "" : "s"}</span>
        {n.chain_id && <span>chain {n.chain_id}</span>}
        {n.split_of && <span>split of #{n.split_of}</span>}
      </div>
      {n.hold && <div className="bgl-dline warn">{n.hold}</div>}
      {!!n.waiting_on.length && <div className="bgl-dline">waiting on {n.waiting_on.map((p) =>
        `#${p} ${byId.get(p)?.title?.slice(0, 40) || ""}`).join(", ")}</div>}
      {n.acceptance && <div className="bgl-dline"><b>acceptance</b> {n.acceptance}</div>}
      {n.checkpoint && n.checkpoint_note && <div className="bgl-dline"><b>look at</b> {n.checkpoint_note}</div>}
      {covers && covers.length > 0 && (
        <div className="bgl-covers">
          <b>Approving signs off {covers.length} ticket{covers.length === 1 ? "" : "s"} since the last checkpoint</b>
          {covers.map((c) => <div key={c.id}>#{c.id} <span style={{ color: color(c.seat) }}>{c.seat}</span> {c.title}
            <i> {c.status}{c.runs > 1 ? `, ${c.runs} runs` : ""}</i></div>)}
        </div>
      )}
      {n.result && <div className="bgl-dline result">{n.result}</div>}
      {n.status === "review" && (
        <div className="bgl-actions">
          <button className="bgs-btn" onClick={approve}><Ti name="check" size={13} /> Approve</button>
          <button className="bgs-btn" onClick={reject}><Ti name="arrow-back-up" size={13} /> Send back</button>
        </div>
      )}
    </div>
  );
}
