import { useCallback, useRef, useState } from "react";
import { readJSON, seatColor, watchAgent } from "../bridge";
import { usePoll, useViewActive } from "../hooks";
import { Icon } from "../components/Icon";

/* The Overview deck — the studio at a glance.
 *
 * Converted first because it is the smallest honest test of the island model:
 * it is read-only, it polls five endpoints, it reads the seat colour tokens,
 * and it hands a click back to the classic inspector. Anything the bridge is
 * missing shows up here rather than three views deep.
 *
 * Behaviour is deliberately identical to the `refreshOverview` it replaces,
 * INCLUDING the cadence and the "did any read fail" line: five parallel reads,
 * the last nine activity events newest-first, and an error only surfaced when
 * there is nothing else to say. The one change is that it stops polling when
 * the deck is not on screen instead of returning early from a fired timer. */

type QueueItem = { id: number; title?: string; seat?: string; status?: string };
type Agent = { item_id: number; state?: string };
type Artifact = { status?: string };
type Event = { seat?: string; summary?: string; kind?: string };

const POLL_MS = 4000;

export default function Overview() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);

  const [items, setItems] = useState<QueueItem[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [sessions, setSessions] = useState<unknown[]>([]);
  const [assets, setAssets] = useState<unknown[]>([]);
  const [readError, setReadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [st, q, ag, arts, act] = await Promise.all([
      readJSON<{ sessions?: unknown[]; assets?: unknown[] }>("/api/state", {}),
      readJSON<{ items?: QueueItem[] }>("/api/queue", { items: [] }),
      readJSON<{ agents?: Agent[] }>("/api/agents", { agents: [] }),
      readJSON<{ artifacts?: Artifact[] }>("/api/artifacts", { artifacts: [] }),
      readJSON<{ events?: Event[] }>("/api/activity?after_id=0", { events: [] }),
    ]);
    setReadError([st, q, ag, arts, act].map((x) => x.__error).find(Boolean) || null);
    setSessions(st.sessions || []);
    setAssets(st.assets || []);
    setItems(q.items || []);
    setAgents(ag.agents || []);
    setArtifacts(arts.artifacts || []);
    setEvents(act.events || []);
  }, []);

  usePoll(refresh, POLL_MS, active);

  const live = agents.filter((a) => a.state === "running");
  const byId = new Map(items.map((i) => [i.id, i]));
  const tiles = [
    { n: live.length, l: "live agents", hot: live.length > 0 },
    { n: items.filter((i) => i.status === "queued").length, l: "queued" },
    { n: artifacts.filter((a) => a.status === "candidate").length, l: "candidates" },
    { n: sessions.length, l: "playtests" },
    { n: assets.length, l: "tracked" },
  ];
  const recent = events.slice(-9).reverse();

  return (
    <div ref={host}>
      <div className="ov-stats">
        {tiles.map((t) => (
          <div key={t.l} className={t.hot ? "ov-tile hot" : "ov-tile"}>
            <div className="n">{t.n}</div>
            <div className="l">{t.l}</div>
          </div>
        ))}
      </div>

      <div className="col2" style={{ marginBottom: 16 }}>
        <div className="spanel k-read">
          <div className="sec-h">
            <Icon name="run" size={15} />
            <h3 className="sec-t">Running now</h3>
            <span className="sec-n">{live.length || ""}</span>
          </div>
          <div>
            {live.length ? (
              live.map((a) => {
                const it = byId.get(a.item_id);
                return (
                  <div
                    key={a.item_id}
                    className="ov-run-row"
                    onClick={() => watchAgent(a.item_id)}
                  >
                    <span className="p" />
                    <span className="rt">{it?.title || `item ${a.item_id}`}</span>
                    <span className="rs" style={{ color: seatColor(it?.seat) }}>
                      {it?.seat || ""}
                    </span>
                  </div>
                );
              })
            ) : (
              <div className="empty">nothing dispatched</div>
            )}
          </div>
        </div>

        <div className="spanel k-read">
          <div className="sec-h">
            <Icon name="timeline" size={15} />
            <h3 className="sec-t">Recent activity</h3>
          </div>
          <div className="ledger" style={{ maxHeight: 240 }}>
            {recent.length ? (
              recent.map((e, i) => (
                <div key={i} className="line">
                  <span className="who" style={{ color: seatColor(e.seat) }}>
                    {e.seat || ""}
                  </span>
                  <span className="what">{e.summary || e.kind || ""}</span>
                </div>
              ))
            ) : readError ? (
              <div className="empty err">could not read the studio - {readError}</div>
            ) : (
              <div className="empty">quiet</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
