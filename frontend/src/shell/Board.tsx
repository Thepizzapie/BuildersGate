import { useRef } from "react";
import { Ti } from "./Ti";
import { SEAT_COLOR, SEAT_ICON } from "./nav";
import { useFloor, type QueueItem } from "./useFloor";
import { useSelection } from "./selection";
import { useViewActive } from "../hooks";

/* The board — the same queue, arranged by who is doing it.
 *
 * A lane per seat that has work, the seat's own colour on its card, and the
 * live items first. The lane header carries the load ("2 working · 3 queued")
 * because the question the board is actually asked is "who is free".
 *
 * Seats with nothing are NOT drawn. An empty lane per seat looked like a
 * dashboard of eight idle departments; the floor's whole argument is that the
 * screen should show what is happening, and a seat nobody has dispatched is
 * not happening.
 */

const LIVE_STATUS = new Set(["running", "in_progress", "working"]);

export function Board() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const floor = useFloor(4000, active);
  const [sel, setSel] = useSelection();

  const running = new Set(floor.agents.map((a) => a.item_id));
  const open = floor.items.filter((i) =>
    i.status === "queued" || LIVE_STATUS.has(i.status) || running.has(i.id));

  const seats: string[] = [];
  const lanes = new Map<string, QueueItem[]>();
  open.forEach((i) => {
    const seat = i.seat || "unassigned";
    if (!lanes.has(seat)) { lanes.set(seat, []); seats.push(seat); }
    lanes.get(seat)!.push(i);
  });
  seats.sort((a, b) => {
    const la = lanes.get(a)!.filter((i) => running.has(i.id)).length;
    const lb = lanes.get(b)!.filter((i) => running.has(i.id)).length;
    return lb - la || a.localeCompare(b);          // busiest lane first
  });

  return (
    <div className="bg4-board" ref={host}>
      {!seats.length && (
        <div className="bg4-empty">
          {floor.error ? `could not read the board — ${floor.error}`
           : "nothing queued and nothing running"}
        </div>
      )}
      {seats.map((seat) => {
        const tasks = lanes.get(seat)!
          .slice().sort((a, b) => Number(running.has(b.id)) - Number(running.has(a.id)));
        const live = tasks.filter((t) => running.has(t.id)).length;
        const c = SEAT_COLOR[seat] || "var(--text-3)";
        return (
          <div className="bg4-lane" key={seat}>
            <div className="bg4-lanecard" style={{ borderColor: `${c}55` }}>
              <div className="h">
                <Ti name={SEAT_ICON[seat] || "user"} color={c} />
                <span className="nm" style={{ color: c }}>{seat}</span>
                {live > 0 && <span className="live">live</span>}
              </div>
              <div className="load">
                {live} working · {tasks.length - live} queued
              </div>
            </div>
            <div className="bg4-tasks">
              {tasks.map((t) => {
                const isLive = running.has(t.id);
                const agent = floor.agents.find((a) => a.item_id === t.id);
                const pct = t.phase && t.phases
                  ? Math.round((t.phase / t.phases) * 100) : undefined;
                return (
                  <button key={t.id}
                          className={`bg4-task${isLive ? " live" : ""}`}
                          aria-selected={sel?.itemId === t.id}
                          onClick={() => setSel({
                            key: `i${t.id}`, kind: isLive ? "agent" : "item",
                            itemId: t.id, title: t.title, seat: t.seat,
                          })}>
                    <span className="line">
                      <Ti name={isLive ? "player-play" : "clock"} />
                      <span className="ttl">{t.title}</span>
                      <span className="badge">
                        {isLive
                          ? (t.phase && t.phases ? `phase ${t.phase}` : "running")
                          : t.status}
                      </span>
                    </span>
                    {(t.note || agent) && (
                      <span className="note">
                        {/* dispatch.status() documents `seconds: 0` as NOT RECORDED rather than
                            zero elapsed, and the runner is the one fact this board exists to
                            distinguish — a default of "claude" is the wrong guess to make on a
                            screen about which runner has which guarantees. */
                        t.note || [agent?.runner, agent?.seconds ? `${agent.seconds}s` : null]
                          .filter(Boolean).join(" · ") || "running"}
                      </span>
                    )}
                    {pct != null && (
                      <span className="bg4-bar"><span style={{ width: `${pct}%` }} /></span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
