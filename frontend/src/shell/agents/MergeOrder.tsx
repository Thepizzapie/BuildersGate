import { useEffect, useMemo, useRef, useState } from "react";
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

type Landed = { id: number; title: string; seat: string; landed_at: string;
  commits: { sha: string; at: string; files: number }[]; after: number[]; runs: number;
  human: boolean; qa: boolean };
type Entry = {
  landed?: Landed;
  rank: number; wave: number; goal: number; goal_title: string; solo: boolean;
  id: number; title: string; seat: string; state: string;
  human: boolean; critical: boolean; path: number; unblocks: number;
  after: number[]; before: number[]; runs: number; why: string;
};
type Order = { order: Entry[]; waves: number; critical_path: number[]; human_steps: number[];
  landed?: Landed[]; __error?: string };

/* A landed ticket as a row of the same log, so history and the open order
   read as one line of commits: what merged, then what merges next. */
function fromLanded(l: Landed): Entry {
  return { landed: l, rank: 0, wave: -1, goal: 0, goal_title: "", solo: false,
    id: l.id, title: l.title, seat: l.seat, state: "landed", human: l.human,
    critical: false, path: 0, unblocks: 0, after: l.after, before: [], runs: l.runs, why: "" };
}

function when(at: string): string {
  const t = Date.parse(at);
  if (Number.isNaN(t)) return at;
  const d = new Date(t);
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

const ROW = 40, HEAD = 34, LANE = 16, PAD = 12;
const AMBER = "#ffbb45";

function color(e: Entry): string {
  return e.human ? AMBER : SEAT_COLOR[e.seat] || "var(--text-3)";
}

function layout(rows: Entry[]) {
  const pos = new Map<number, number>();            // id -> index in rows
  rows.forEach((e, i) => pos.set(e.id, i));
  // A header before each goal: the goal and the work feeding it, together.
  const ys: number[] = [];
  const heads: { key: string; y: number; label: string; sub: string }[] = [];
  let y = 0, group = "";
  const keyOf = (e: Entry) => (e.landed ? "landed" : e.solo ? "solo" : `g${e.goal}`);
  rows.forEach((e) => {
    const k = keyOf(e);
    if (k !== group) {
      group = k;
      const members = rows.filter((r) => keyOf(r) === k);
      const now = members.filter((r) => r.wave === 0 && r.state !== "running").length;
      const live = members.filter((r) => r.state === "running").length;
      if (e.landed) {
        heads.push({ key: k, y, label: "Landed", sub: `${members.length} merged, oldest first` });
        y += HEAD;
        ys.push(y + ROW / 2);
        y += ROW;
        return;
      }
      if (rows.some((r) => r.landed) && !heads.some((h) => h.key === "now")) {
        heads.push({ key: "now", y, label: "Next to land", sub: "" });
        y += HEAD / 2;
      }
      heads.push({ key: k, y,
        label: e.solo ? "Independent" : `Toward #${e.goal} ${e.goal_title}`,
        sub: `${members.length} step${members.length === 1 ? "" : "s"}`
          + (now ? ` · ${now} can land now` : "") + (live ? ` · ${live} running` : "") });
      y += HEAD;
    }
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

export function MergeOrder({ active, seat, pick, onPick, hours }: {
  active: boolean; seat: string; pick: number | null; onPick: (id: number) => void; hours: number;
}) {
  const [data, setData] = useState<Order | null>(null);
  const [chores, setChores] = useState(false);
  const nowRef = useRef<HTMLDivElement | null>(null);
  const scrolled = useRef(false);
  /* Live is open work only; the other windows carry what landed in them. */
  const span = hours === 6 ? -1 : hours;
  async function load() {
    const got = await readJSON<Order>(`/api/lifecycle/merge-order?hours=${span}`,
      { order: [], waves: 0, critical_path: [], human_steps: [] });
    setData((prev) => (got.__error && prev && !prev.__error ? prev : got));
  }
  useEffect(() => { if (active) void load(); }, [active, span]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!active || !data?.__error) return;
    const t = window.setTimeout(() => { void load(); }, 3000);
    return () => window.clearTimeout(t);
  }, [data, active]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEvents(() => { void load(); }, { enabled: active, fallbackMs: 4000 });

  /* QA gates and unblock tickets are bookkeeping around a ticket, not work
     that merged; folded unless asked for. */
  const isChore = (l: Landed) => l.qa || l.title.startsWith("UNBLOCK");
  const landedAll = useMemo(() => (data?.landed || []).filter((l) => !seat || l.seat === seat), [data, seat]);
  const choreCount = landedAll.filter(isChore).length;
  const rows = useMemo(() => [
    ...landedAll.filter((l) => chores || !isChore(l)).map(fromLanded),
    ...(data?.order || []).filter((e) => !seat || e.seat === seat),
  ], [data, seat, chores, landedAll]);   // eslint-disable-line react-hooks/exhaustive-deps
  const g = useMemo(() => layout(rows), [rows]);
  useEffect(() => {
    if (scrolled.current || !nowRef.current) return;
    scrolled.current = true;
    nowRef.current.scrollIntoView({ block: "center" });
  });
  if (data?.__error) return <div className="bgl-err">could not read the order — {data.__error}</div>;
  if (!rows.length) return <div className="bgl-empty">nothing landed in this window and no open work</div>;
  const gutter = PAD * 2 + g.lanes * LANE;

  return (
    <div className="bgo">
      <div className="bgo-sum">
        <span><b>{data!.waves}</b> waves</span>
        <span><b>{data!.critical_path.length}</b> on the critical path</span>
        <span className={data!.human_steps.length ? "you" : ""}><b>{data!.human_steps.length}</b> waiting on you</span>
        {span !== -1 && <span><b>{landedAll.length - choreCount}</b> landed</span>}
        {span !== -1 && !!choreCount &&
          <button className="bgo-chores" onClick={() => setChores(!chores)}>
            {chores ? "hide" : "show"} {choreCount} QA / unblock steps</button>}
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
                {e.landed
                  ? <circle r={5} fill={c} stroke={c} strokeWidth={1.5} opacity={0.75} />
                  : e.human
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
          <div key={h.key} ref={h.key === "now" ? nowRef : undefined}
               className={`bgo-wh${h.key === "now" ? " now" : ""}`} style={{ top: h.y, left: gutter }}>
            <span className="bgo-goal">{h.label}</span><span className="bgo-gsub">{h.sub}</span>
          </div>
        ))}
        {rows.map((e, i) => (
          <div key={e.id}
               className={`bgo-row${e.landed ? " landed" : ""}${e.human ? " human" : ""}${e.critical ? " crit" : ""}${pick === e.id ? " picked" : ""}`}
               style={{ top: g.ys[i] - ROW / 2, left: gutter }}
               onClick={() => onPick(e.id)}>
            <span className="bgo-rank">{e.landed ? "✓" : e.rank}</span>
            <div className="bgo-main">
              <div className="bgo-t"><span className="bgo-id">#{e.id}</span> {e.title}</div>
              {e.landed ? <div className="bgo-why">
                <span style={{ color: color(e) }}>{e.human ? "you" : e.seat}</span>
                {" · landed "}{when(e.landed.landed_at)}
                {e.landed.commits.length
                  ? " · " + e.landed.commits.map((c) => `${c.sha} (${c.files} file${c.files === 1 ? "" : "s"})`).join(", ")
                  : " · no commit"}
                {e.runs > 1 ? ` · ${e.runs} runs` : ""}
              </div> : <div className="bgo-why">
                <span style={{ color: color(e) }}>{e.human ? "you" : e.seat}</span>
                {" · "}{e.wave === 0 && !["running", "needs you", "parked"].includes(e.state) ? "can land now" : e.state}
                {e.runs > 1 ? ` · ${e.runs} runs` : ""}{e.why ? ` · ${e.why}` : ""}
              </div>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
