import { useRef, useState } from "react";
import { Ti } from "./Ti";
import { SEAT_COLOR } from "./nav";
import { useFloor, type Row } from "./useFloor";
import { useSelection } from "./selection";
import { useViewActive } from "../hooks";
import { useScreen } from "./screen";
import { byScreen } from "./nav";
import { mutate, toast } from "../bridge";

/* The floor — one stream of everything the studio is doing, newest first, with
 * the director's dispatch box under it.
 *
 * THIS IS THE SCREEN THE REDESIGN IS FOR. What it replaces is an Overview deck
 * of five counters and a nine-line ledger, next to an Agents deck that had the
 * conversation, next to a board that had the work — three places to look for
 * one question ("what is happening"), none of which answered it alone.
 *
 * Everything and Work history are this component with one filter between them.
 * There were two more — Live and Needs you — and they were the same rows a
 * third time: a live row already reads as live and a row that wants you already
 * says so, so the screens added a click and a place to be wrong about.
 */

export function Floor() {
  const screen = useScreen();
  const filter = byScreen(screen)?.filter || "all";
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const floor = useFloor(4000, active);
  const [sel, setSel] = useSelection();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);

  // Live and warn rows are STILL MARKED, just no longer split onto screens of
  // their own — they sort and colour themselves in place, which is what the
  // single stream was for.
  const rows = filter === "done" ? floor.rows.filter((r) => !r.live) : floor.rows;

  async function dispatch() {
    const said = text.trim();
    if (!said) return;
    setSending(true);
    // The same endpoint the console's talk box posts to: this files a work item
    // and spawns the director on it. Deliberately NOT a second way to dispatch —
    // one door, two places to knock on it.
    const r = await mutate("/api/console/say", { body: { text: said } });
    setSending(false);
    if (r.ok) { setText(""); toast("dispatched", "ok"); }
  }

  return (
    <div className="bg4-floor" ref={host}>
      <div className="bg4-stream">
        {rows.map((r) => (
          <FloorRow key={r.key} row={r}
                    selected={sel?.key === r.key}
                    onPick={() => setSel({
                      key: r.key, kind: r.kind, itemId: r.itemId,
                      eventId: r.eventId, title: r.text, seat: r.seat,
                    })} />
        ))}
        {!rows.length && (
          <div className="bg4-empty">
            {floor.error ? `could not read the floor — ${floor.error}` : "quiet"}
          </div>
        )}
      </div>

      <div className="bg4-dispatch">
        <Ti name="user-star" size={17} color={SEAT_COLOR.director} />
        <input value={text} onChange={(e) => setText(e.currentTarget.value)}
               onKeyDown={(e) => { if (e.key === "Enter") dispatch(); }}
               placeholder="tell the director what you want"
               aria-label="Tell the director what you want" />
        <button className="bg4-send" onClick={dispatch} disabled={sending || !text.trim()}>
          <Ti name="send" size={14} />{sending ? "sending…" : "dispatch"}
        </button>
      </div>
    </div>
  );
}

function FloorRow({ row, selected, onPick }: {
  row: Row; selected: boolean; onPick: () => void;
}) {
  const c = SEAT_COLOR[row.seat] || "var(--text-3)";
  return (
    <button className={`bg4-row${row.live ? " live" : ""}${row.warn ? " warn" : ""}`}
            aria-selected={selected} onClick={onPick}>
      <span className="line">
        <span className="tile" style={{ background: `${c}22`, color: c }}>
          <Ti name={row.icon} />
        </span>
        <span className="t">{row.time}</span>
        <span className="txt">{row.text}</span>
        <span className="meta">{row.meta}</span>
      </span>
      {row.pct != null && (
        <span className="bg4-bar"><span style={{ width: `${row.pct}%` }} /></span>
      )}
    </button>
  );
}
