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

/* WHAT THE BADGE SAYS, AND WHY IT IS NOT `status`.
 *
 *     #42 enlarge rooms       done
 *     #45 swap in furniture   running
 *     #43 rebuild routes      queued
 *
 * That read as a scheduler that skipped #43, and the scheduler was right: #45
 * was inserted between #42 and #43 later, because the route measurements had
 * to wait for real furniture dimensions. `queued` was true and useless. These
 * five words are the difference between "the board is working, leave it" and
 * "this needs a person", which `queued` could not express at all.
 *
 * WAITING is deliberately the quiet one. BLOCKED, HELD and EXHAUSTED are the
 * loud ones, because each of those needs somebody and none of them used to be
 * distinguishable from an ordinary queued row. */
const STATE_LABEL: Record<string, string> = {
  ready: "ready",
  waiting: "waiting",
  blocked: "blocked",
  held: "held",
  exhausted: "exhausted",
};
const STATE_ICON: Record<string, string> = {
  waiting: "clock-pause",
  blocked: "hand-stop",
  held: "user-question",
  exhausted: "alert-triangle",
};
/* Loud states get their own class so the card can carry a border rather than
   hiding the fact in a tooltip. */
const NEEDS_A_PERSON = new Set(["blocked", "held", "exhausted"]);

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
                      <span className={`badge${
                        NEEDS_A_PERSON.has(t.execution_state || "") ? " needs" : ""}`}>
                        {isLive
                          ? (t.phase && t.phases ? `phase ${t.phase}` : "running")
                          : (STATE_LABEL[t.execution_state || ""] || t.status)}
                      </span>
                    </span>
                    {/* THE BLOCKER'S TITLE, ON THE CARD. `waiting_on` was on
                        the wire as a nested object nobody rendered, so the
                        reader had to open the item to learn what it was behind
                        - and the whole defect is that they were not opening
                        it. The server writes the sentence; this draws it. */}
                    {!isLive && t.waiting_line && (
                      <span className={`note wait${
                        NEEDS_A_PERSON.has(t.execution_state || "") ? " needs" : ""}`}>
                        <Ti name={STATE_ICON[t.execution_state || ""] || "clock"}
                            size={11} />
                        {t.waiting_line}
                      </span>
                    )}
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
