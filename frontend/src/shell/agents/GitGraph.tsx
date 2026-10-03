import { useEffect, useMemo, useRef, useState } from "react";
import { SEAT_COLOR } from "../nav";
import { readJSON } from "../../bridge";
import { useEvents } from "../../hooks";

/* MERGE GRAPH — the board drawn the way GitKraken draws a repository.
 *
 * `main` is the trunk on the left. Every ticket is a branch: it leaves main
 * where its first run started and merges back where its work landed (the
 * harness commit). Tickets that were in flight at the same time sit in
 * parallel lanes; a lane is reused once its ticket has merged. Running work
 * is a branch still open at the top; queued work is drawn above HEAD as the
 * planned merges, in the recommended order (bgate_core/board/mergeorder.py),
 * the next one nearest HEAD. Newest at the top.
 */

type Landed = { id: number; title: string; seat: string; landed_at: string; landed_ts: number;
  started_ts: number; commits: { sha: string; at: string; files: number }[]; after: number[];
  runs: number; human: boolean; qa: boolean };
type Running = { id: number; title: string; seat: string; started_ts: number };
type Planned = { rank: number; id: number; title: string; seat: string; state: string;
  human: boolean; critical: boolean; after: number[]; wave: number; runs: number; why: string };
type Data = { order: Planned[]; landed?: Landed[]; running?: Running[]; human_steps: number[];
  critical_path: number[]; __error?: string };

type Row =
  | { kind: "planned"; id: number; p: Planned }
  | { kind: "running"; id: number; r: Running; ts: number }
  | { kind: "merge"; id: number; l: Landed; ts: number };

const ROW = 38, LANE = 18, PAD = 14, AMBER = "#ffbb45", MAIN = "#8b93a7";

function seatColor(seat: string, human = false): string {
  return human ? AMBER : SEAT_COLOR[seat] || "var(--text-3)";
}

function when(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function span(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function build(rows: Row[]) {
  const now = Date.now() / 1000;
  const y = (i: number) => i * ROW + ROW / 2;
  // Time -> y over the timed rows (running + merges, newest first), so a
  // branch leaves main at the height its run actually started.
  const timed = rows.map((r, i) => ({ i, ts: r.kind === "planned" ? Infinity : r.ts }))
    .filter((t) => t.ts !== Infinity);
  const yAt = (ts: number) => {
    if (!timed.length) return y(rows.length);
    for (let k = 0; k < timed.length; k++) {
      if (timed[k].ts <= ts) {
        if (k === 0) return y(timed[0].i) - ROW * 0.4;
        const a = timed[k - 1], b = timed[k];
        const f = a.ts === b.ts ? 0.5 : (a.ts - ts) / (a.ts - b.ts);
        return y(a.i) + (y(b.i) - y(a.i)) * Math.min(1, Math.max(0, f));
      }
    }
    return y(timed[timed.length - 1].i) + ROW * 0.7;   // started before the window
  };
  // Lanes: interval scheduling over [start, end], oldest first; lane 0 is main.
  const spans = rows.filter((r) => r.kind !== "planned").map((r) => {
    const start = r.kind === "merge" ? r.l.started_ts : (r as { r: Running }).r.started_ts;
    const end = r.kind === "merge" ? r.l.landed_ts : now;
    return { id: r.id, start: start || end, end };
  }).sort((a, b) => a.start - b.start || a.end - b.end);
  const laneEnd: number[] = [];
  const lane = new Map<number, number>();
  spans.forEach((s) => {
    let l = laneEnd.findIndex((e) => e <= s.start);
    if (l < 0) { l = laneEnd.length; laneEnd.push(0); }
    laneEnd[l] = s.end + 1;
    lane.set(s.id, l + 1);
  });
  const lanes = laneEnd.length + 1;
  const lx = (l: number) => PAD + l * LANE;
  const head = rows.findIndex((r) => r.kind === "merge");
  return { y, yAt, lane, lanes, lx, head, height: rows.length * ROW + ROW };
}

export function GitGraph({ active, seat, pick, onPick, hours }: {
  active: boolean; seat: string; pick: number | null; onPick: (id: number) => void; hours: number;
}) {
  const [data, setData] = useState<Data | null>(null);
  const [chores, setChores] = useState(false);
  const range = hours === 6 ? 6 : hours;       // Live = the last six hours here
  async function load() {
    const got = await readJSON<Data>(`/api/lifecycle/merge-order?hours=${range}`,
      { order: [], landed: [], running: [], human_steps: [], critical_path: [] });
    setData((prev) => (got.__error && prev && !prev.__error ? prev : got));
  }
  useEffect(() => { if (active) void load(); }, [active, range]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!active || !data?.__error) return;
    const t = window.setTimeout(() => { void load(); }, 3000);
    return () => window.clearTimeout(t);
  }, [data, active]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEvents(() => { void load(); }, { enabled: active, fallbackMs: 4000 });

  const isChore = (t: string) => t.startsWith("QA gate") || t.startsWith("UNBLOCK");
  const landedAll = (data?.landed || []).filter((l) => !seat || l.seat === seat);
  const choreCount = landedAll.filter((l) => isChore(l.title)).length;
  const rows: Row[] = useMemo(() => {
    const running = new Set((data?.running || []).map((r) => r.id));
    const planned = (data?.order || []).filter((p) => (!seat || p.seat === seat)
      && !running.has(p.id) && (chores || !isChore(p.title)));
    const live = (data?.running || []).filter((r) => (!seat || r.seat === seat)
      && (chores || !isChore(r.title)));
    const merged = landedAll.filter((l) => chores || !isChore(l.title));
    return [
      ...planned.slice().reverse().map((p) => ({ kind: "planned" as const, id: p.id, p })),
      ...live.slice().sort((a, b) => b.started_ts - a.started_ts)
        .map((r) => ({ kind: "running" as const, id: r.id, r, ts: Date.now() / 1000 })),
      ...merged.slice().sort((a, b) => b.landed_ts - a.landed_ts)
        .map((l) => ({ kind: "merge" as const, id: l.id, l, ts: l.landed_ts })),
    ];
  }, [data, seat, chores]);   // eslint-disable-line react-hooks/exhaustive-deps
  const g = useMemo(() => build(rows), [rows]);
  const headRef = useRef<HTMLDivElement | null>(null);
  const scrolled = useRef(false);
  useEffect(() => {
    if (scrolled.current || !headRef.current) return;
    scrolled.current = true;
    headRef.current.scrollIntoView({ block: "center" });
  });

  if (data?.__error) return <div className="bgl-err">could not read the history — {data.__error}</div>;
  if (!rows.length) return <div className="bgl-empty">nothing merged in this window and no open work</div>;
  const gutter = PAD * 2 + g.lanes * LANE;
  const x0 = g.lx(0);
  const firstPlanned = rows.findIndex((r) => r.kind === "planned");
  const lastPlanned = rows.map((r) => r.kind).lastIndexOf("planned");
  const bottom = rows.length ? g.y(rows.length - 1) + ROW * 0.6 : 0;
  const topSolid = g.head >= 0 ? g.y(g.head) : bottom;

  return (
    <div className="bgk">
      <div className="bgo-sum">
        <span><b>{landedAll.length - choreCount}</b> merged</span>
        <span><b>{(data?.running || []).length}</b> open branches</span>
        <span><b>{(data?.order || []).length - (data?.running || []).length}</b> planned</span>
        <span className={data!.human_steps.length ? "you" : ""}><b>{data!.human_steps.length}</b> waiting on you</span>
        {!!choreCount && <button className="bgo-chores" onClick={() => setChores(!chores)}>
          {chores ? "hide" : "show"} {choreCount} QA / unblock merges</button>}
      </div>
      <div className="bgk-canvas" style={{ height: g.height }}>
        <svg className="bgo-svg" width={gutter} height={g.height}>
          {/* main: solid up to HEAD, dashed through the planned merges */}
          <line x1={x0} x2={x0} y1={topSolid} y2={bottom} stroke={MAIN} strokeWidth={3} />
          {firstPlanned >= 0 && <line x1={x0} x2={x0} y1={g.y(firstPlanned)} y2={g.head >= 0 ? topSolid : bottom}
            stroke={MAIN} strokeWidth={2} strokeDasharray="4 4" opacity={0.6} />}
          {rows.map((r, i) => {
            if (r.kind === "planned") return null;
            const l = g.lane.get(r.id) || 1, xl = g.lx(l), yr = g.y(i);
            const start = r.kind === "merge" ? r.l.started_ts : r.r.started_ts;
            const yf = Math.max(yr + ROW * 0.5, g.yAt(start || r.ts));
            const c = r.kind === "merge" ? seatColor(r.l.seat, r.l.human) : seatColor(r.r.seat);
            // fork out of main at yf, run up the lane, merge back at the row
            const fork = `M${x0},${yf} C${x0},${yf - 10} ${xl},${yf - 6} ${xl},${yf - 16} L${xl},${yr + 8}`;
            const merge = r.kind === "merge" ? ` C${xl},${yr} ${x0 + 8},${yr} ${x0},${yr}` : ` L${xl},${yr}`;
            return <path key={`b${r.id}`} d={fork + merge} fill="none" stroke={c} strokeWidth={2}
                         opacity={pick === r.id ? 1 : 0.75}
                         className={r.kind === "running" ? "bgk-live" : undefined} />;
          })}
          {rows.map((r, i) => {
            const yr = g.y(i);
            if (r.kind === "planned") {
              const c = seatColor(r.p.seat, r.p.human);
              return <g key={`d${r.id}`} onClick={() => onPick(r.id)} className="bgo-dot">
                {r.p.human
                  ? <rect x={x0 - 5} y={yr - 5} width={10} height={10} transform={`rotate(45 ${x0} ${yr})`}
                          fill="none" stroke={AMBER} strokeWidth={2} />
                  : <circle cx={x0} cy={yr} r={5.5} fill="var(--surface-1, #0f1115)" stroke={c}
                            strokeWidth={2} strokeDasharray="3 2" />}
              </g>;
            }
            const l = g.lane.get(r.id) || 1, xl = g.lx(l);
            const c = r.kind === "merge" ? seatColor(r.l.seat, r.l.human) : seatColor(r.r.seat);
            return <g key={`d${r.id}`} onClick={() => onPick(r.id)} className="bgo-dot">
              {r.kind === "running" && <circle cx={xl} cy={yr} r={10} className="bgl-pulse" stroke={c} />}
              <circle cx={xl} cy={yr} r={5} fill={c} stroke="var(--surface-1, #0f1115)" strokeWidth={1.5} />
              {r.kind === "merge" && <circle cx={x0} cy={yr} r={6} fill={MAIN}
                                             stroke="var(--surface-1, #0f1115)" strokeWidth={2} />}
            </g>;
          })}
        </svg>
        {lastPlanned >= 0 && (
          <div className="bgk-band" style={{ top: 0, left: gutter, height: (lastPlanned + 1) * ROW }}>planned</div>
        )}
        {rows.map((r, i) => {
          const top = g.y(i) - ROW / 2;
          const isHead = i === g.head;
          if (r.kind === "planned") return (
            <div key={`r${r.id}`} className={`bgk-row planned${pick === r.id ? " picked" : ""}`}
                 style={{ top, left: gutter }} onClick={() => onPick(r.id)}>
              <div className="bgo-t"><span className="bgo-id">#{r.id}</span> {r.p.title}</div>
              <div className="bgo-why"><span style={{ color: seatColor(r.p.seat, r.p.human) }}>
                {r.p.human ? "you" : r.p.seat}</span> · planned merge {r.p.rank}
                {r.p.wave === 0 && r.p.state !== "parked" ? " · can start now" : ` · ${r.p.state}`}
                {r.p.why && r.p.why !== "independent" ? ` · ${r.p.why}` : ""}</div>
            </div>);
          if (r.kind === "running") return (
            <div key={`r${r.id}`} className={`bgk-row live${pick === r.id ? " picked" : ""}`}
                 style={{ top, left: gutter }} onClick={() => onPick(r.id)}>
              <div className="bgo-t"><span className="bgk-tag live">branch</span>
                <span className="bgo-id">#{r.id}</span> {r.r.title}</div>
              <div className="bgo-why"><span style={{ color: seatColor(r.r.seat) }}>{r.r.seat}</span>
                {" · open "}{r.r.started_ts ? span(Date.now() / 1000 - r.r.started_ts) : ""} · not merged</div>
            </div>);
          const l = r.l;
          return (
            <div key={`r${r.id}`} ref={isHead ? headRef : undefined}
                 className={`bgk-row${pick === r.id ? " picked" : ""}`}
                 style={{ top, left: gutter }} onClick={() => onPick(r.id)}>
              <div className="bgo-t">
                {isHead && <span className="bgk-tag main">main</span>}
                <span className="bgo-id">#{l.id}</span> {l.title}</div>
              <div className="bgo-why"><span style={{ color: seatColor(l.seat, l.human) }}>{l.human ? "you" : l.seat}</span>
                {" · merged "}{when(l.landed_ts)}
                {l.started_ts && l.landed_ts > l.started_ts ? ` after ${span(l.landed_ts - l.started_ts)}` : ""}
                {l.commits.length ? " · " + l.commits.map((c) => c.sha).join(" ") : " · no commit"}
                {l.runs > 1 ? ` · ${l.runs} runs` : ""}</div>
            </div>);
        })}
      </div>
    </div>
  );
}
