/* WHAT HAS TO HAPPEN BEFORE THIS, IN THE ORDER IT HAPPENS.
 *
 * THE DEFECT THIS DRAWS AWAY. The board showed
 *
 *     #42 enlarge rooms        done
 *     #45 swap in furniture    running
 *     #43 rebuild routes       queued
 *
 * and it reads as a scheduler that skipped #43. It did not. #43 was filed
 * after #42; #45 was inserted BETWEEN them later, because the route
 * measurements had to wait for real furniture dimensions. The dependency
 * engine was correct every step of the way. The PRESENTATION made it look
 * broken — and an operator who believes the scheduler is broken starts working
 * around it, which is the expensive part.
 *
 * IDS ARE NEVER RENUMBERED. They are in briefs, in commit messages, in result
 * notes and in the human's head; a display that renumbered them would trade one
 * confusion for a worse one. So the fix is not to change the ids, it is to stop
 * making the reader infer execution order from an ordering that never meant it.
 *
 * DEPENDENCY DIRECTION IS VISUALLY DOMINANT. The chain runs down the panel with
 * an arrow between each link, so the eye reads the order off the arrows and
 * never off the numbers. The one predecessor currently holding this item is
 * marked; the rest are context.
 *
 * ONE GRAPH, NOT TWO TABLES. `work_item.depends_on` holds one parent and
 * `work_item_dep` holds the rest. Which of the two a link lives in is a fact
 * about the database and it is not a question anybody looking at a board should
 * have to answer, so the endpoint merges them and this never mentions it.
 */
import { useCallback, useState } from "react";
import { readJSON } from "../bridge";
import { useEvents, WORK_KINDS } from "../hooks";
import { Ti } from "./Ti";

type Link = {
  id: number;
  title: string;
  seat: string;
  status: string;
  depends_on: number[];
};

type PathReply = { item?: number; path?: Link[]; waiting_line?: string };

/* The colour and icon each state earns. `waiting` is deliberately quiet — the
   board is working and nobody should touch it — while `blocked` and
   `exhausted` are loud, because both need a person and neither used to be
   distinguishable from a queued row. */
const MARK: Record<string, { icon: string; tone: string }> = {
  done: { icon: "check", tone: "var(--ok, #4ade80)" },
  dispatched: { icon: "player-play", tone: "var(--live, #60a5fa)" },
  review: { icon: "eye", tone: "var(--warn, #fbbf24)" },
  failed: { icon: "alert-triangle", tone: "var(--bad, #f87171)" },
  cancelled: { icon: "x", tone: "var(--text-3)" },
  queued: { icon: "clock", tone: "var(--text-3)" },
};

export function ExecutionPath({ itemId }: { itemId: number }) {
  const [reply, setReply] = useState<PathReply>({});

  const refresh = useCallback(async () => {
    setReply(await readJSON<PathReply>(`/api/queue/${itemId}/path`, {}));
  }, [itemId]);

  useEvents(refresh, { kinds: WORK_KINDS, key: itemId });

  const path = reply.path || [];
  /* A single link is this item alone with nothing before it. Drawing a
     one-node "execution path" is noise on every unchained item on the board,
     which is most of them. */
  if (path.length < 2) return null;

  const self = path[path.length - 1];
  const blocking = self.depends_on.find(
    (id) => (path.find((l) => l.id === id)?.status ?? "done") !== "done");

  return (
    <div className="bg4-execpath">
      <div className="bg4-insp-eyebrow">Execution path</div>
      {reply.waiting_line && (
        <div className="bg4-execwait">
          <Ti name="hand-stop" size={12} />
          <span>{reply.waiting_line}</span>
        </div>
      )}
      <ol className="bg4-execchain">
        {path.map((link, i) => {
          const mark = MARK[link.status] || MARK.queued;
          const isSelf = link.id === self.id;
          const holds = link.id === blocking;
          return (
            <li key={link.id}
                className={`${isSelf ? "self" : ""}${holds ? " holds" : ""}`}>
              {i > 0 && <span className="arrow" aria-hidden="true">↓</span>}
              <span className="node" style={{ borderColor: `${mark.tone}55` }}>
                <Ti name={mark.icon} size={12} color={mark.tone} />
                {/* The id is deliberately the SMALL half of this line. It
                    identifies the row; it does not order it. */}
                <span className="id">#{link.id}</span>
                <span className="ttl">{link.title}</span>
                <span className="st" style={{ color: mark.tone }}>
                  {link.status}
                </span>
                {holds && <span className="holdsnow">blocking now</span>}
              </span>
            </li>
          );
        })}
      </ol>
      <p className="bg4-execnote">
        Order comes from the dependencies, not from the numbers — a
        later-numbered item can legitimately run first when it was inserted into
        an existing chain. Ids are creation identifiers and never change.
      </p>
    </div>
  );
}
