import { useState } from "react";
import { Button } from "@mantine/core";
import type { AssetGroup } from "../../store";
import { mutate, previewURL, toast } from "../../bridge";
import { useJSON } from "../../shell/seats/api";
import { IMG_RE } from "./categorise";

/* The pairwise art tournament for one asset: start it, watch the reviewer,
 * read the ranking, approve the winner.
 *
 * It existed only as two HTTP routes nothing called, so no tournament ever
 * started. The ranking is the reviewer's opinion, not a promotion: approving
 * stays the human's click, through the same review path as the panes above. */

type Row = { artifact_id: number; rating: number; matches: number; wins: number;
             path?: string; revision?: number; status?: string };
type Status = { tournament_ref: string | null; standings: Row[];
                decided_matches: number; pending_matches: number;
                review: { id: number; status: string } | null };

const EMPTY: Status = { tournament_ref: null, standings: [], decided_matches: 0,
                        pending_matches: 0, review: null };
const LIVE = new Set(["queued", "dispatched", "integrating"]);

export function Tournament({ group, onReview }: {
  group: AssetGroup;
  onReview: (id: number, status: string) => void;
}) {
  const name = group.logical_name;
  const st = useJSON<Status>(
    `/api/art-tournament/status?logical_name=${encodeURIComponent(name)}`,
    EMPTY, 10000, true);
  const [busy, setBusy] = useState(false);
  const candidates = group.candidates?.length || 0;
  const live = !!st.review && LIVE.has(st.review.status);
  const stuck = st.pending_matches > 0 && !live;

  async function start() {
    setBusy(true);
    const r = await mutate<{ match_count: number; review_item_id: number; error?: string }>(
      "/api/art-tournament/start", { body: { logical_name: name }, quiet: true });
    setBusy(false);
    if (r.ok && r.data) {
      toast(`tournament opened: ${r.data.match_count} matches, reviewer #${r.data.review_item_id}`
            + (r.data.error ? ` (dispatch: ${r.data.error})` : ""), r.data.error ? "" : "ok");
    } else {
      const body = r.body as { error?: { message?: string }; detail?: string } | null;
      toast(body?.error?.message || body?.detail || "the tournament could not start");
    }
    st.__refresh?.();
  }

  async function discard() {
    const r = await mutate("/api/art-tournament/discard",
                           { body: { logical_name: name }, ok: "open matches discarded" });
    if (r.ok) st.__refresh?.();
  }

  if (candidates < 2 && !st.tournament_ref) return null;
  const top = st.standings[0];

  return (
    <div className="drawer-sec tournament">
      <div className="tournament-head">
        <strong>Pairwise ranking</strong>
        <span className="chip">
          {st.tournament_ref
            ? `${st.decided_matches} decided · ${st.pending_matches} open`
            : `${candidates} candidates`}
        </span>
        {st.review && (
          <span className={live ? "chip" : "chip health-good"}>
            reviewer #{st.review.id} · {st.review.status}
          </span>
        )}
      </div>
      <div className="tournament-actions">
        {candidates >= 2 && !st.pending_matches && (
          <Button size="compact-sm" variant="default" loading={busy} onClick={start}
                  title="An independent qa agent judges every pair of candidates; ratings fall out of the wins">
            rank {candidates} candidates
          </Button>
        )}
        {stuck && (
          <Button size="compact-sm" variant="subtle" onClick={discard}
                  title="The reviewer is no longer running; clear its undecided matches so a new ranking can start">
            discard {st.pending_matches} open
          </Button>
        )}
      </div>
      {st.__error && <div className="empty">{st.__error}</div>}
      {st.standings.length > 0 && (
        <ol className="tournament-standings">
          {st.standings.map((row, i) => (
            <li key={row.artifact_id} className={i === 0 ? "lead" : ""}>
              {row.path && IMG_RE.test(row.path)
                ? <img src={previewURL(row.path)} alt={`r${row.revision}`} />
                : <span className="chip">#{row.artifact_id}</span>}
              <span>r{row.revision ?? "?"} · {row.status ?? "?"}</span>
              <span>{Math.round(row.rating)} · {row.wins}–{row.matches - row.wins}</span>
            </li>
          ))}
        </ol>
      )}
      {top && top.status === "candidate" && !st.pending_matches && (
        <Button size="compact-sm" mt={7} onClick={() => onReview(top.artifact_id, "approved")}>
          approve the winner (r{top.revision})
        </Button>
      )}
    </div>
  );
}
