import { useEffect, useMemo, useRef, useState } from "react";
import { SEAT_COLOR } from "../nav";
import type { MapNode } from "./SubwayMap";

/* PIPELINE — the board read left to right, every step titled.
 *
 * Each connected piece of work is a BAND: its steps are cards in columns by
 * dependency depth, so a chain reads left to right. Steps in the same column
 * can run in parallel; when there are several they are boxed and the box says
 * how many and how many are done. Lines run from the right edge of a step to
 * the left edge of what waits on it, so a fan-out spreads and a fan-in
 * converges. Text is full size; a band wider than the panel scrolls sideways.
 */

const CW = 176, CH = 40;          // a card
const PITCH_X = 214, PITCH_Y = 52;
const HEAD = 30, BOXPAD = 8, BOXLABEL = 18, GAPY = 26, PAD = 16;
const AMBER = "#ffbb45";

type Card = { n: MapNode & { members?: number[]; group?: string; caption?: string }; x: number; y: number; gutter: number };
type Band = { key: string; title: string; sub: string; x: number; y: number; h: number; w: number };
type Group = { key: string; x: number; y: number; w: number; h: number; label: string; fold?: string };

function seatColor(seat: string): string {
  return SEAT_COLOR[seat] || "var(--text-3)";
}

function fmtDur(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function caption(n: MapNode): { text: string; tone: string } {
  const runs = n.runs > 1 ? ` · ${n.runs} runs` : "";
  switch (n.state) {
    case "checkpoint": return { text: "your sign-off", tone: AMBER };
    case "review": return { text: "in review", tone: AMBER };
    case "running": return { text: `running${n.elapsed_s ? " · " + fmtDur(n.elapsed_s) : ""}`, tone: "var(--accent)" };
    case "failed": return { text: `failed${runs}`, tone: "var(--bad)" };
    case "blocked": return { text: "blocked", tone: "var(--bad)" };
    case "waiting": return { text: `waits on ${n.waiting_on.length || 1}`, tone: "var(--text-3)" };
    case "done": return { text: `${n.seat} · done${runs}`, tone: "var(--text-3)" };
    default: return { text: `${n.seat} · ${n.state}${runs}`, tone: n.runs > 1 ? "var(--warn)" : "var(--text-3)" };
  }
}

type PNode = MapNode & { members?: number[]; group?: string; caption?: string };

/* FINISHED FAN-OUTS FOLD. Siblings that share the same parents (a fan-out of
   four or more) keep their unfinished members on the board and fold every
   finished one into a single "N done" card, which expands on click. History
   stays one card wide instead of eight. */
function fold(nodes: MapNode[], expanded: Set<string>): PNode[] {
  /* Siblings in the SAME POSITION: same parents and same children. Those are
     interchangeable on the picture (eight props feeding one dressing pass),
     so folding the finished ones loses nothing; a sibling with its own
     children is structure and is never folded. */
  const children = new Map<number, number[]>();
  nodes.forEach((n) => n.parents.forEach((p) => (children.get(p) || children.set(p, []).get(p)!).push(n.id)));
  const fans = new Map<string, MapNode[]>();
  nodes.forEach((n) => {
    if (!n.parents.length) return;
    const key = [...n.parents].sort((a, b) => a - b).join(",") + "|"
      + [...(children.get(n.id) || [])].sort((a, b) => a - b).join(",");
    (fans.get(key) || fans.set(key, []).get(key)!).push(n);
  });
  const into = new Map<number, number>();       // folded member -> summary id
  const summaries: PNode[] = [];
  let synth = -1;
  fans.forEach((list, key) => {
    if (list.length < 4 || expanded.has(key)) return;
    const done = list.filter((n) => n.state === "done" || n.state === "cancelled");
    if (done.length < 2) return;
    const id = synth--;
    done.forEach((n) => into.set(n.id, id));
    const seats = [...new Set(done.map((n) => n.seat))];
    summaries.push({
      ...done[0], id, title: `${done[0].title} +${done.length - 1}`,
      seat: seats.length === 1 ? seats[0] : done[0].seat, state: "done", status: "done",
      spawned_by: null, checkpoint: false, runs: 1, elapsed_s: null, waiting_on: [],
      members: done.map((n) => n.id), group: key,
      caption: `${done.length} of ${list.length} done · show`,
    } as PNode);
  });
  const remap = (ids: number[]) => [...new Set(ids.map((p) => into.get(p) ?? p))];
  const out: PNode[] = summaries.map((n) => ({ ...n, parents: remap(n.parents) }));
  nodes.forEach((n) => {
    if (into.has(n.id)) return;
    const spawned = n.spawned_by != null ? (into.get(n.spawned_by) ?? n.spawned_by) : n.spawned_by;
    out.push({ ...n, parents: remap(n.parents), spawned_by: spawned });
  });
  return out;
}

function layout(raw: MapNode[], expanded: Set<string>) {
  const nodes = fold(raw, expanded);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const up = (n: PNode) => {
    const deps = n.parents.filter((p) => byId.has(p));
    if (deps.length) return deps;
    return n.spawned_by != null && byId.has(n.spawned_by) ? [n.spawned_by] : [];
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
  const kids = new Map<number, number[]>();
  nodes.forEach((n) => up(n).forEach((p) => (kids.get(p) || kids.set(p, []).get(p)!).push(n.id)));
  const order = [...nodes].sort((a, b) => (depth.get(a.id)! - depth.get(b.id)!) || (a.id - b.id));

  /* STREAMS, not connected pieces. A ticket belongs to the stream of what it
     waits on; a ticket that waits on SEVERAL streams starts a new one (the
     boss fight that needs the forest and the hero is its own stream, not the
     forest's tail). One band per stream; columns stay global, so a wire
     between bands still runs left to right. */
  const stream = new Map<number, number>();
  order.forEach((n) => {
    const ss = [...new Set(up(n).map((p) => stream.get(p)!))];
    stream.set(n.id, ss.length === 1 ? ss[0] : n.id);
  });
  const members = new Map<number, PNode[]>();
  order.forEach((n) => { const s = stream.get(n.id)!; (members.get(s) || members.set(s, []).get(s)!).push(n); });
  const isolated = (g: PNode[]) => g.length === 1 && !up(g[0]).length && !(kids.get(g[0].id) || []).length;
  const streams = [...members.entries()].filter(([, g]) => !isolated(g));
  const loose = [...members.values()].filter(isolated).map((g) => g[0])
    .sort((a, b) => (Number(b.state === "running") - Number(a.state === "running")) || b.id - a.id);

  const cards: Card[] = [];
  const bands: Band[] = [];
  const boxes: Group[] = [];
  let y = PAD;
  streams.forEach(([sid, g]) => {
    const inBand = new Set(g.map((n) => n.id));
    const used = new Map<number, Set<number>>();
    const track = new Map<number, number>();
    const free = (col: number, near: number) => {
      const taken = used.get(col) || used.set(col, new Set()).get(col)!;
      for (let s = 0; s < 300; s++) {
        for (const t of s ? [near + s, near - s] : [near]) if (!taken.has(t)) { taken.add(t); return t; }
      }
      return near;
    };
    let rootTrack = 0;
    g.forEach((n) => {
      const ups = up(n).filter((p) => inBand.has(p));
      let want = rootTrack;
      if (ups.length) {
        const first = ups.find((p) => (kids.get(p) || []).filter((k) => inBand.has(k))[0] === n.id);
        const ts = ups.map((p) => track.get(p) ?? 0);
        want = first != null ? track.get(first)! : Math.round(ts.reduce((a, b) => a + b, 0) / ts.length);
      }
      const t = free(depth.get(n.id)!, want);
      track.set(n.id, t);
      if (!ups.length) rootTrack = Math.max(rootTrack, t + 1);
    });
    const lo = Math.min(...track.values()), hi = Math.max(...track.values());
    const rows = hi - lo + 1;
    // Fan-out boxes: siblings sharing parents, three or more, in this band.
    const fans = new Map<string, PNode[]>();
    g.forEach((n) => {
      if (!n.parents.length) return;
      const key = [...n.parents].sort((a, b) => a - b).join(",") + "@" + depth.get(n.id);
      (fans.get(key) || fans.set(key, []).get(key)!).push(n);
    });
    const boxed = [...fans.values()].some((l) => l.length >= 3);
    const top = y + HEAD + (boxed ? BOXPAD : 0);
    const bodyH = rows * PITCH_Y + (boxed ? BOXPAD * 2 + BOXLABEL : 0);
    const gutter = y + HEAD + bodyH + 4;
    g.forEach((n) => cards.push({
      n, x: PAD + depth.get(n.id)! * PITCH_X, y: top + (track.get(n.id)! - lo) * PITCH_Y, gutter }));
    fans.forEach((list, key) => {
      if (list.length < 3) return;
      const ys = list.map((n) => top + (track.get(n.id)! - lo) * PITCH_Y);
      const done = list.filter((n) => n.state === "done").reduce((a, n) => a + (n.members?.length || 1), 0);
      const total = list.reduce((a, n) => a + (n.members?.length || 1), 0);
      const running = list.filter((n) => n.state === "running").length;
      boxes.push({ key: "f" + sid + key, x: PAD + depth.get(list[0].id)! * PITCH_X - BOXPAD,
        y: Math.min(...ys) - BOXPAD, w: CW + BOXPAD * 2, h: Math.max(...ys) - Math.min(...ys) + CH + BOXPAD * 2,
        label: `${total} in parallel · ${done} done${running ? ` · ${running} running` : ""}`,
        fold: [...expanded].find((k) => k.startsWith(key.split("@")[0] + "|")) });
    });
    const real = g.reduce((a, n) => a + (n.members?.length || 1), 0);
    const done = g.reduce((a, n) => a + (n.state === "done" ? (n.members?.length || 1) : 0), 0);
    const running = g.filter((n) => n.state === "running").length;
    const sign = g.filter((n) => n.state === "checkpoint").length;
    const failed = g.filter((n) => n.state === "failed" || n.state === "blocked").length;
    const first = g[0];
    const joins = up(first).filter((p) => !inBand.has(p)).length;
    bands.push({ key: "s" + sid, y, h: HEAD + bodyH,
      x: PAD + Math.min(...g.map((n) => depth.get(n.id)!)) * PITCH_X,
      w: PAD + (Math.max(...g.map((n) => depth.get(n.id)!)) + 1) * PITCH_X,
      title: first.title + (joins > 1 ? `  (joins ${joins} streams)` : ""),
      sub: `${done} of ${real} done` + (running ? ` · ${running} running` : "")
        + (failed ? ` · ${failed} failed` : "") + (sign ? ` · ${sign} to sign off` : "") });
    y += HEAD + bodyH + GAPY;
  });
  if (loose.length) {
    const per = 6;
    const rows = Math.ceil(loose.length / per);
    loose.forEach((n, i) => cards.push({
      n, x: PAD + (i % per) * PITCH_X, y: y + HEAD + Math.floor(i / per) * PITCH_Y, gutter: 0 }));
    bands.push({ key: "loose", title: "Independent tickets", y, x: PAD, h: HEAD + rows * PITCH_Y,
      w: PAD + Math.min(loose.length, per) * PITCH_X,
      sub: `${loose.length} with no dependency either way` });
    y += HEAD + rows * PITCH_Y + GAPY;
  }
  const at = new Map(cards.map((c) => [c.n.id, c]));
  const edges: { a: Card; b: Card; spawn: boolean }[] = [];
  nodes.forEach((n) => {
    const b = at.get(n.id)!;
    const deps = n.parents.filter((p) => at.has(p));
    deps.forEach((p) => edges.push({ a: at.get(p)!, b, spawn: false }));
    if (!deps.length && n.spawned_by != null && at.has(n.spawned_by)) {
      edges.push({ a: at.get(n.spawned_by)!, b, spawn: true });
    }
  });
  const width = Math.max(...bands.map((b) => b.w), 0) + PAD;
  return { cards, bands, boxes, edges, height: y, width };
}

/* A wire that skips a column would run through the cards in it, so it drops
   into the band's gutter in the gap after its source, runs along under the
   band, and climbs into its target in the gap before it. */
function wire(a: Card, b: Card): string {
  const x1 = a.x + CW, y1 = a.y + CH / 2, x2 = b.x, y2 = b.y + CH / 2;
  if (b.x - a.x > PITCH_X + 1 && a.gutter) {
    const g = a.gutter, gx1 = x1 + 19, gx2 = x2 - 19;
    return `M${x1},${y1} C${gx1},${y1} ${gx1},${y1} ${gx1},${y1 + 12} L${gx1},${g - 10}`
      + ` C${gx1},${g} ${gx1},${g} ${gx1 + 10},${g} L${gx2 - 10},${g}`
      + ` C${gx2},${g} ${gx2},${g} ${gx2},${g - 10} L${gx2},${y2 + 12} C${gx2},${y2} ${gx2},${y2} ${x2},${y2}`;
  }
  if (y1 === y2) return `M${x1},${y1} L${x2},${y2}`;
  const mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}

export function Pipeline({ nodes, pick, onPick }: {
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
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const p = useMemo(() => layout(nodes, expanded), [nodes, expanded]);
  const toggle = (key: string) => setExpanded((cur) => {
    const next = new Set(cur);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  return (
    <div className="bgp2" ref={host}>
      <svg width={Math.max(width, p.width)} height={p.height} className="bgp2-svg">
        {p.bands.map((b) => (
          <g key={b.key} className="bgp2-band" style={{ transform: `translate(${b.x}px, ${b.y}px)` }}>
            <text className="bgp2-title" y={13}>{b.title.length > 60 ? b.title.slice(0, 59) + "…" : b.title}</text>
            <text className="bgp2-sub" y={13} x={Math.min(440, b.title.length * 7.4 + 14)}>{b.sub}</text>
          </g>
        ))}
        {p.boxes.map((g) => (
          <g key={g.key} className="bgp2-box">
            <rect x={g.x} y={g.y} width={g.w} height={g.h} rx={8} />
            <text x={g.x + 4} y={g.y + g.h + 13}>{g.label}</text>
            {g.fold && <text className="bgp2-fold" x={g.x + g.w - 4} y={g.y + g.h + 13}
                             textAnchor="end" onClick={() => toggle(g.fold!)}>fold done</text>}
          </g>
        ))}
        {p.edges.map((e) => {
          const live = e.b.n.state === "running" || e.b.n.state === "ready";
          return <path key={`${e.a.n.id}-${e.b.n.id}`}
                       className={`bgp2-wire${e.spawn ? " spawn" : ""}${live ? " live" : ""}${e.b.n.state === "blocked" ? " dead" : ""}`}
                       style={{ d: `path("${wire(e.a, e.b)}")`, stroke: seatColor(e.b.n.seat) } as React.CSSProperties} />;
        })}
        {p.cards.map((c) => {
          const n = c.n;
          const cap = n.group ? { text: n.caption || "show", tone: "var(--text-3)" } : caption(n);
          const cp = n.checkpoint || n.source === "qa-gate";
          return (
            <g key={n.id} className={`bgp2-card s-${n.state}${pick === n.id ? " picked" : ""}`}
               style={{ transform: `translate(${c.x}px, ${c.y}px)`, ["--seat" as string]: seatColor(n.seat) } as React.CSSProperties}
               onClick={() => (n.group ? toggle(n.group) : onPick(n.id))}>
              <title>{`#${n.id} ${n.title}`}</title>
              <rect className="bgp2-face" width={CW} height={CH} rx={7} />
              <rect className="bgp2-edge" width={4} height={CH} rx={2} />
              {n.state === "running" && <circle className="bgp2-pulse" cx={CW - 12} cy={12} r={5} />}
              <text className="bgp2-name" x={12} y={17}>
                {(cp ? "◆ " : n.state === "done" ? "✓ " : "") + (n.title.length > 21 ? n.title.slice(0, 20) + "…" : n.title)}
              </text>
              <text className="bgp2-cap" x={12} y={32} style={{ fill: cap.tone }}>{cap.text}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
