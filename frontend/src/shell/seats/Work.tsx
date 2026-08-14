import { Ti } from "../Ti";
import { Head, Nothing, Tag } from "./prims";
import { ago, usd, type QueueItem } from "./api";
import { useSelection } from "../selection";

/* The one panel every seat shares: what is on THIS seat's desk.
 *
 * The board screen arranges the same queue by owner to answer "who is free".
 * Inside a workspace the question is the other one — "what am I holding" — so
 * the rows are grouped by state and the seat column is gone, because every row
 * has the same answer.
 *
 * Clicking a row sets the shell's selection, which is what the pop-out
 * inspector reads. The workspace does NOT grow its own detail pane: two places
 * that show an item's log is how the classic seats ended up with a log reader
 * per seat, nine copies of the same 400 lines.
 *
 * The rows arrive as a PROP rather than from a hook of this component's own.
 * The shell already reads /api/queue to badge the seat picker, and a second
 * poller on the same URL would double the most frequent request on the screen
 * to render a subset of what the first one fetched.
 */

const GROUPS: { key: string; label: string; hint: string; match: (s: string) => boolean }[] = [
  { key: "live", label: "Running", hint: "an agent holds this seat's lanes right now",
    match: (s) => s === "dispatched" || s === "running" || s === "in_progress" },
  { key: "queued", label: "Queued", hint: "filed, waiting for a slot",
    match: (s) => s === "queued" || s === "ready" },
  { key: "review", label: "Waiting on a human", hint: "the gate parked it",
    match: (s) => s === "review" || s === "blocked" },
  { key: "closed", label: "Closed", hint: "done, failed or cancelled",
    match: (s) => s === "done" || s === "failed" || s === "cancelled" },
];

export function Work({ seat, items }: { seat: string; active: boolean; items: QueueItem[] }) {
  const [sel, setSel] = useSelection();

  return (
    <div className="bgs-pad">
      {!items.length && (
        <Nothing what="nothing filed for this seat"
                 how="queue_add(seat, title, brief) files work; the dashboard is what dispatches it" />
      )}
      {GROUPS.map((g) => {
        const rows = items.filter((i) => g.match(i.status));
        if (!rows.length) return null;
        return (
          <div key={g.key}>
            <Head label={`${g.label} · ${rows.length}`} hint={g.hint} />
            {rows.map((it) => (
              <button key={it.id}
                      className={`bgs-item${sel?.itemId === it.id ? " on" : ""}`}
                      onClick={() => setSel({
                        key: `i${it.id}`,
                        kind: g.key === "live" ? "agent" : "item",
                        itemId: it.id, title: it.title, seat: it.seat,
                      })}>
                <Ti name={g.key === "live" ? "player-play" : "clock"} size={13}
                    color={g.key === "live" ? `var(--c-${seat}, var(--accent))` : "var(--text-3)"} />
                <span className="t">{it.title}</span>
                <span className="m">
                  {usd(it.total_cost_usd) && <span>{usd(it.total_cost_usd)}</span>}
                  <span>{ago(it.updated_at)}</span>
                  <Tag tone={g.key === "live" ? "good" : g.key === "review" ? "warn" : "off"}>
                    {it.status}
                  </Tag>
                </span>
              </button>
            ))}
          </div>
        );
      })}
    </div>
  );
}
