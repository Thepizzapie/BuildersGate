import { useEffect, useMemo, useState } from "react";
import { SEAT_COLOR } from "../nav";
import { readJSON } from "../../bridge";
import { useEvents } from "../../hooks";

/* MERGE ORDER — what has to land first, drawn like a git history.
 *
 * Rows are the recommended landing order (bgate_core/board/mergeorder.py):
 * wave by wave, the critical path first, then whatever unblocks the most -
 * the same order agents read in their brief and dispatch follows. The gutter
 * on the left is the dependency graph laid over that order, git-style: an
 * item continues the lane of the item it lands after when it is that item's
 * first follower, other followers branch into a free lane, and a lane ends
 * when nothing else lands after it. Lines run UP to what has to land first.
 * Human steps are amber diamonds; running work pulses.
 */

type Entry = {
  rank: number; wave: number; id: number; title: string; seat: string; state: string;
  human: boolean; critical: boolean; path: number; unblocks: number;
  after: number[]; before: number[]; runs: number; why: string;
};
type Order = { order: Entry[]; waves: number; critical_path: number[]; human_steps: number[]; __error?: string };

const ROW = 40, HEAD = 28, LANE = 16, PAD = 12;
const AMBER = "#ffbb45";
const WAVE_NAME = (w: number) => (w === 0 ? "Land now" : w === 1 ? "Next" : `Wave ${w + 1}`);

function color(e: Entry): string {
  return e.human ? AMBER : SEAT_COLOR[e.seat] || "var(--text-3)";
}

function layout(rows: Entry[]) {
  const pos = new Map<number, number>();            // id -> index in rows
  rows.forEach((e, i) => pos.set(e.id, i));
  // y with a header row before each new wave
  const ys: number[] = [];
  const heads: { wave: number; y: number }[] = [];
  let y = 0, wave = -1;
  rows.forEach((e) => {
    if (e.wave !== wave) { wave = e.wave; heads.push({ wave, y }); y += HEAD; }
    ys.push(y + ROW / 2);
    y += ROW;
  });
  /* LANES THAT MERGE. A lane belongs to the item it LEADS TO: the first item
     heading for a target opens a lane for it, every later item heading for
     the same target joins that lane right below its own row, and the target
     sits on the lane when its turn comes. Six things feeding one target are
     one trunk, not six parallel lines. */
  const follow = new Map<number, number[]>();       // id -> what lands after it
  rows.forEach((e) => e.after.filter((p) => pos.has(p))
    .forEach((p) => (follow.get(p) || follow.set(p, []).get(p)!).push(e.id)));
  const lane = new Map<number, number>();
  const into = new Map<number, number>();           // target -> lane it arrives on
  const via = new Map<string, number>();            // "parent>target" -> lane used
  const busy = new Set<number>();
  const freeLane = () => { let n = 0; while (busy.has(n)) n++; busy.add(n); return n; };
  rows.forEach((e) => {
    let l = into.get(e.id);
    if (l === undefined) l = freeLane();
    into.delete(e.id);
    lane.set(e.id, l);
    const outs = (follow.get(e.id) || []).slice().sort((a, b) => pos.get(a)! - pos.get(b)!);
    let kept = false;
    outs.forEach((t) => {
      if (into.has(t)) { via.set(`${e.id}>${t}`, into.get(t)!); return; }
      const nl = kept ? freeLane() : l;              // first new target keeps this lane
      kept = true;
      into.set(t, nl);
      via.set(`${e.id}>${t}`, nl);
    });
    if (!kept) busy.delete(l);
  });
  const lanes = Math.max(1, ...[...lane.values(), ...via.values()].map((v) => v + 1));
  const lx = (l: number) => PAD + l * LANE + LANE / 2;
  const x = (id: number) => lx(lane.get(id)!);
  const edges: { d: string; e: Entry; live: boolean }[] = [];
  rows.forEach((e, i) => {
    e.after.filter((p) => pos.has(p)).forEach((p) => {
      const j = pos.get(p)!;
      const x1 = x(p), y1 = ys[j], x2 = x(e.id), y2 = ys[i];
      const xl = lx(via.get(`${p}>${e.id}`) ?? lane.get(e.id)!);
      const bend = y1 + ROW * 0.55;
      let d = `M${x1},${y1}`;
      if (xl !== x1) d += ` C${x1},${y1 + ROW * 0.35} ${xl},${y1 + ROW * 0.2} ${xl},${bend}`;
      if (xl === x2) d += ` L${x2},${y2}`;
      else d += ` L${xl},${y2 - ROW * 0.55} C${xl},${y2 - ROW * 0.2} ${x2},${y2 - ROW * 0.35} ${x2},${y2}`;
      edges.push({ d, e, live: e.state === "running" });
    });
  });
  return { ys, heads, lane, lanes, x, edges, height: y };
}

export function MergeOrder({ active, seat, pick, onPick }: {
  active: boolean; seat: string; pick: number | null; onPick: (id: number) => void;
}) {
  const [data, setData] = useState<Order | null>(null);
  async function load() {
    setData(await readJSON<Order>("/api/lifecycle/merge-order",
      { order: [], waves: 0, critical_path: [], human_steps: [] }));
  }
  useEffect(() => { if (active) void load(); }, [active]);
  useEvents(() => { void load(); }, { enabled: active, fallbackMs: 4000 });

  const rows = useMemo(() => (data?.order || []).filter((e) => !seat || e.seat === seat), [data, seat]);
  const g = useMemo(() => layout(rows), [rows]);
  if (data?.__error) return <div className="bgl-err">could not read the order — {data.__error}</div>;
  if (!rows.length) return <div className="bgl-empty">no open work — nothing to order</div>;
  const gutter = PAD * 2 + g.lanes * LANE;

  return (
    <div className="bgo">
      <div className="bgo-sum">
        <span><b>{data!.waves}</b> waves</span>
        <span><b>{data!.critical_path.length}</b> on the critical path</span>
        <span className={data!.human_steps.length ? "you" : ""}><b>{data!.human_steps.length}</b> waiting on you</span>
      </div>
      <div className="bgo-canvas" style={{ height: g.height }}>
        <svg className="bgo-svg" width={gutter} height={g.height}>
          {g.edges.map((ed, i) => (
            <path key={i} style={{ d: `path("${ed.d}")`, stroke: color(ed.e) } as React.CSSProperties}
                  className={`bgo-edge${ed.live ? " live" : ""}${ed.e.critical ? " crit" : ""}`} />
          ))}
          {rows.map((e, i) => {
            const cx = g.x(e.id), cy = g.ys[i], c = color(e);
            return (
              <g key={e.id} className="bgo-dot" style={{ transform: `translate(${cx}px, ${cy}px)` }}
                 onClick={() => onPick(e.id)}>
                {e.state === "running" && <circle r={10} className="bgl-pulse" stroke={c} />}
                {e.human
                  ? <rect x={-6} y={-6} width={12} height={12} transform="rotate(45)" fill={AMBER} stroke={AMBER} strokeWidth={2} />
                  : <circle r={6} stroke={c} strokeWidth={2.2}
                            strokeDasharray={e.state === "waiting" ? "3 2" : undefined}
                            fill={e.state === "running" ? c : "var(--surface-1, #0f1115)"} />}
                {e.critical && !e.human && <circle r={2.4} fill={c} />}
              </g>
            );
          })}
        </svg>
        {g.heads.map((h) => (
          <div key={h.wave} className="bgo-wh" style={{ top: h.y, left: gutter }}>{WAVE_NAME(h.wave)}</div>
        ))}
        {rows.map((e, i) => (
          <div key={e.id}
               className={`bgo-row${e.human ? " human" : ""}${e.critical ? " crit" : ""}${pick === e.id ? " picked" : ""}`}
               style={{ top: g.ys[i] - ROW / 2, left: gutter }}
               onClick={() => onPick(e.id)}>
            <span className="bgo-rank">{e.rank}</span>
            <div className="bgo-main">
              <div className="bgo-t"><span className="bgo-id">#{e.id}</span> {e.title}</div>
              <div className="bgo-why">
                <span style={{ color: color(e) }}>{e.human ? "you" : e.seat}</span>
                {" · "}{e.state}{e.runs > 1 ? ` · ${e.runs} runs` : ""}{e.why ? ` · ${e.why}` : ""}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
