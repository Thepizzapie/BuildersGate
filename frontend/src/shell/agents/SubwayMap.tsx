import { useEffect, useMemo, useRef, useState } from "react";
import { SEAT_COLOR } from "../nav";

/* SUBWAY MAP — the board read top to bottom.
 *
 * Each connected piece of work is a LINE running down the page: a ticket is a
 * station, the next ticket in the chain is the next station below it. Work
 * that can run in parallel BRANCHES sideways off the line and the branches
 * MERGE back where a ticket waits on all of them. Every station carries its
 * title and its state in words, at full size - the map scrolls instead of
 * zooming, because a picture you have to zoom into to read is the one that
 * was rejected.
 *
 * Lines sit side by side and wrap to the panel width; tickets with no
 * dependency either way are one list at the end. Station positions and line
 * shapes transition, so a poll that moves something slides it.
 */

export type MapNode = {
  id: number; title: string; seat: string; state: string; status: string;
  parents: number[]; spawned_by?: number | null; chain_id: string;
  checkpoint: boolean; source: string; runs: number; elapsed_s: number | null;
  waiting_on: number[];
};

const COLW = 190;      // one branch column: the station plus its label
const ROWH = 56;       // one station
const HEAD = 34;       // a line's title
const GAPX = 36, GAPY = 30, PAD = 16;
const AMBER = "#ffbb45";

type Station = { n: MapNode; x: number; y: number };
type Line = { key: string; title: string; sub: string; x: number; y: number; w: number; h: number };

function seatColor(seat: string): string {
  return SEAT_COLOR[seat] || "var(--text-3)";
}

function fmtDur(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/* The words under a station's title: what is true about it right now. */
function caption(n: MapNode): { text: string; tone: string } {
  if (n.state === "checkpoint") return { text: "your sign-off", tone: AMBER };
  if (n.state === "review") return { text: "in review", tone: AMBER };
  if (n.state === "running") {
    return { text: `running${n.elapsed_s ? " · " + fmtDur(n.elapsed_s) : ""}`, tone: seatColor(n.seat) };
  }
  if (n.state === "failed") return { text: `failed${n.runs > 1 ? ` · ${n.runs} runs` : ""}`, tone: "var(--bad)" };
  if (n.state === "blocked") return { text: "blocked - a parent failed", tone: "var(--bad)" };
  if (n.state === "waiting") {
    const w = n.waiting_on.slice(0, 3).map((p) => "#" + p).join(", ");
    return { text: `waits on ${w || "a parent"}`, tone: "var(--text-3)" };
  }
  if (n.state === "held") return { text: "held", tone: "var(--text-3)" };
  if (n.state === "done") return { text: `${n.seat} · done${n.runs > 1 ? ` · ${n.runs} runs` : ""}`, tone: "var(--text-3)" };
  const runs = n.runs > 1 ? ` · ${n.runs} runs` : "";
  return { text: `${n.seat} · ${n.state}${runs}`, tone: n.runs > 1 ? "var(--warn)" : "var(--text-3)" };
}

function layout(nodes: MapNode[], width: number) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const up = (n: MapNode) => {
    const deps = n.parents.filter((p) => byId.has(p));
    if (deps.length) return deps;
    return n.spawned_by && byId.has(n.spawned_by) ? [n.spawned_by] : [];
  };
  const depth = new Map<number, number>();
  const depthOf = (id: number, guard = 0): number => {
    if (depth.has(id)) return depth.get(id)!;
    const ups = guard > 80 ? [] : up(byId.get(id)!);
    const d = ups.length ? 1 + Math.max(...ups.map((p) => depthOf(p, guard + 1))) : 0;
    depth.set(id, d);
    return d;
  };
  nodes.forEach((n) => depthOf(n.id));
  const children = new Map<number, number[]>();
  nodes.forEach((n) => up(n).forEach((p) => {
    (children.get(p) || children.set(p, []).get(p)!).push(n.id);
  }));
  children.forEach((l) => l.sort((a, b) => a - b));

  // Connected pieces: each becomes one line on the map.
  const parent = new Map<number, number>();
  const find = (x: number): number => {
    let r = x;
    while (parent.get(r) !== r) r = parent.get(r)!;
    return r;
  };
  nodes.forEach((n) => parent.set(n.id, n.id));
  nodes.forEach((n) => up(n).forEach((p) => {
    const a = find(n.id), b = find(p);
    if (a !== b) parent.set(a, b);
  }));
  const groups = new Map<number, MapNode[]>();
  nodes.forEach((n) => { const r = find(n.id); (groups.get(r) || groups.set(r, []).get(r)!).push(n); });
  const live = (g: MapNode[]) => g.some((n) => !["done", "cancelled"].includes(n.state));
  const pieces = [...groups.values()].filter((g) => g.length > 1)
    .sort((a, b) => (Number(live(b)) - Number(live(a))) || (b.length - a.length)
      || (Math.min(...a.map((n) => n.id)) - Math.min(...b.map((n) => n.id))));
  const loose = [...groups.values()].filter((g) => g.length === 1).map((g) => g[0])
    .sort((a, b) => (Number(b.state === "running") - Number(a.state === "running")) || (b.id - a.id));

  // Branch columns inside a piece: a station keeps its parent's column when
  // it is that parent's first child, otherwise the nearest free column.
  const placeIn = (g: MapNode[]) => {
    const used = new Map<number, Set<number>>();
    const col = new Map<number, number>();
    const free = (row: number, near: number) => {
      const taken = used.get(row) || used.set(row, new Set()).get(row)!;
      for (let step = 0; step < 200; step++) {
        for (const c of step ? [near + step, near - step] : [near]) {
          if (!taken.has(c)) { taken.add(c); return c; }
        }
      }
      return near;
    };
    let rootCol = 0;
    [...g].sort((a, b) => (depth.get(a.id)! - depth.get(b.id)!) || (a.id - b.id)).forEach((n) => {
      const ups = up(n);
      let want = rootCol;
      if (ups.length) {
        const first = ups.find((p) => (children.get(p) || [])[0] === n.id);
        const cols = ups.map((p) => col.get(p) ?? 0);
        want = first != null ? col.get(first) ?? 0 : Math.round(cols.reduce((a, b) => a + b, 0) / cols.length);
      }
      const c = free(depth.get(n.id)!, want);
      col.set(n.id, c);
      if (!ups.length) rootCol = Math.max(rootCol, c + 1);
    });
    const lo = Math.min(...col.values());
    col.forEach((v, k) => col.set(k, v - lo));
    const cols = Math.max(...col.values()) + 1;
    const rows = Math.max(...g.map((n) => depth.get(n.id)!)) + 1;
    return { col, cols, rows };
  };

  const stations: Station[] = [];
  const lines: Line[] = [];
  let x = PAD, y = PAD, rowH = 0;
  const avail = Math.max(COLW + 2 * PAD, width);
  const put = (w: number, h: number) => {
    if (x > PAD && x + w > avail - PAD) { x = PAD; y += rowH + GAPY; rowH = 0; }
    const at = { x, y };
    x += w + GAPX;
    rowH = Math.max(rowH, h);
    return at;
  };
  pieces.forEach((g) => {
    const { col, cols, rows } = placeIn(g);
    const w = cols * COLW, h = HEAD + rows * ROWH;
    const at = put(w, h);
    const done = g.filter((n) => n.state === "done").length;
    const running = g.filter((n) => n.state === "running").length;
    const head = [...g].sort((a, b) => depth.get(a.id)! - depth.get(b.id)! || a.id - b.id)[0];
    const sign = g.filter((n) => n.state === "checkpoint").length;
    lines.push({
      key: "p" + head.id, x: at.x, y: at.y, w, h,
      title: head.title,
      sub: `${done} of ${g.length} done` + (running ? ` · ${running} running` : "")
        + (sign ? ` · ${sign} to sign off` : ""),
    });
    g.forEach((n) => stations.push({
      n, x: at.x + 10 + col.get(n.id)! * COLW, y: at.y + HEAD + 12 + depth.get(n.id)! * ROWH,
    }));
  });
  if (loose.length) {
    const per = Math.max(1, Math.floor((avail - 2 * PAD) / COLW));
    const rows = Math.ceil(loose.length / per);
    if (x > PAD) { x = PAD; y += rowH + GAPY; rowH = 0; }
    const at = put(per * COLW, HEAD + rows * ROWH);
    lines.push({ key: "loose", x: at.x, y: at.y, w: Math.min(loose.length, per) * COLW,
      h: HEAD + rows * ROWH, title: "Independent tickets",
      sub: `${loose.length} with no dependency either way` });
    loose.forEach((n, i) => stations.push({
      n, x: at.x + 10 + (i % per) * COLW, y: at.y + HEAD + 12 + Math.floor(i / per) * ROWH,
    }));
  }
  const pos = new Map(stations.map((s) => [s.n.id, s]));
  const edges: { a: Station; b: Station; spawn: boolean }[] = [];
  nodes.forEach((n) => {
    const b = pos.get(n.id)!;
    const deps = n.parents.filter((p) => pos.has(p));
    deps.forEach((p) => edges.push({ a: pos.get(p)!, b, spawn: false }));
    if (!deps.length && n.spawned_by && pos.has(n.spawned_by)) {
      edges.push({ a: pos.get(n.spawned_by)!, b, spawn: true });
    }
  });
  const height = Math.max(...lines.map((l) => l.y + l.h), 0) + PAD;
  /* A line wider than the panel (a wide fan-out) scrolls sideways rather than
     shrinking: text stays full size. */
  const span = Math.max(...lines.map((l) => l.x + l.w), 0) + PAD;
  return { stations, lines, edges, height, span };
}

/* A branch drops straight down past its own station's caption, turns in the
   gap between rows, and runs straight down into the station it feeds - so a
   line never crosses a title. */
function track(a: Station, b: Station): string {
  if (a.x === b.x) return `M${a.x},${a.y} L${b.x},${b.y}`;
  const y1 = a.y + 22, y2 = a.y + 38;
  return `M${a.x},${a.y} L${a.x},${y1} C${a.x},${y1 + 10} ${b.x},${y2 - 10} ${b.x},${y2} L${b.x},${b.y}`;
}

function Mark({ n, picked }: { n: MapNode; picked: boolean }) {
  const c = seatColor(n.seat);
  const st = n.state;
  const fill = st === "done" ? c : st === "running" ? c
    : st === "failed" || st === "blocked" ? "var(--bad)"
    : st === "checkpoint" || st === "review" ? AMBER : "var(--surface-1, #0f1115)";
  const stroke = st === "failed" || st === "blocked" ? "var(--bad)"
    : st === "checkpoint" || st === "review" ? AMBER
    : ["held", "waiting", "parked", "cancelled"].includes(st) ? "var(--text-3)" : c;
  const dash = st === "waiting" || st === "held" ? "3 2" : undefined;
  const sw = picked ? 3.5 : 2.5;
  const diamond = n.checkpoint || n.source === "qa-gate";
  return (
    <>
      {st === "running" && <circle r={13} className="bgm-pulse" stroke={c} />}
      {st === "checkpoint" && <circle r={14} className="bgm-pulse" stroke={AMBER} />}
      {diamond
        ? <rect x={-7} y={-7} width={14} height={14} transform="rotate(45)"
                fill={fill} stroke={stroke} strokeWidth={sw} strokeDasharray={dash} />
        : <circle r={8} fill={fill} stroke={stroke} strokeWidth={sw} strokeDasharray={dash} />}
      {st === "done" && !diamond && <path d="M-3.5,0 L-1,2.6 L3.6,-2.6" className="bgm-tick" />}
    </>
  );
}

export function SubwayMap({ nodes, pick, onPick }: {
  nodes: MapNode[]; pick: number | null; onPick: (id: number) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  const m = useMemo(() => layout(nodes, width), [nodes, width]);

  return (
    <div className="bgm" ref={host}>
      <svg width={Math.max(width, m.span)} height={m.height} className="bgm-svg">
        {m.lines.map((l) => (
          <g key={l.key} className="bgm-line" style={{ transform: `translate(${l.x}px, ${l.y}px)` }}>
            <text className="bgm-title" x={0} y={13}>{l.title.length > 44 ? l.title.slice(0, 43) + "…" : l.title}</text>
            <text className="bgm-sub" x={0} y={27}>{l.sub}</text>
          </g>
        ))}
        {m.edges.map((e) => {
          const live = e.b.n.state === "running" || e.b.n.state === "ready";
          return <path key={`${e.a.n.id}-${e.b.n.id}`}
                       className={`bgm-track${e.spawn ? " spawn" : ""}${live ? " live" : ""}${e.b.n.state === "blocked" ? " dead" : ""}`}
                       style={{ d: `path("${track(e.a, e.b)}")`, stroke: seatColor(e.b.n.seat) } as React.CSSProperties} />;
        })}
        {m.stations.map((s) => {
          const cap = caption(s.n);
          return (
            <g key={s.n.id} className={`bgm-station s-${s.n.state}${pick === s.n.id ? " picked" : ""}`}
               style={{ transform: `translate(${s.x}px, ${s.y}px)` }}
               onClick={() => onPick(s.n.id)}>
              <title>{`#${s.n.id} ${s.n.title}`}</title>
              <rect className="bgm-hit" x={-14} y={-14} width={COLW - 8} height={36} rx={6} />
              <Mark n={s.n} picked={pick === s.n.id} />
              <text className="bgm-name" x={16} y={-1}>
                {s.n.title.length > 24 ? s.n.title.slice(0, 23) + "…" : s.n.title}
              </text>
              <text className="bgm-cap" x={16} y={14} style={{ fill: cap.tone }}>{cap.text}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
