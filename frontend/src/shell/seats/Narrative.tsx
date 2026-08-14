import type { ReactNode } from "react";
import type { SeatBodyProps } from "./types";
import { Lore } from "../narrative/Lore";
import { Dialogue } from "../narrative/Dialogue";
import { Quests } from "../narrative/Quests";
import "../narrative/narrative.css";

/* NARRATIVE — the seat's body IS the lore graph and the dialogue graph.
 *
 * THERE USED TO BE TWO OF THESE. A "Narrative" screen under Build drew the
 * full lore-and-dialogue pair, and this seat drew a thinner entity/facts view
 * beside it whose dialogue tab was a sentence telling you to go to the other
 * screen. Two ways to read one graph is two things to keep in agreement, and
 * the thin one lost every time it was compared. The screen is gone; the richer
 * pair (shell/narrative/Lore.tsx, Dialogue.tsx) is mounted here instead.
 *
 * THE HEAD SLOT SHRINKS TO ITS CHIPS. Lore and Dialogue each fill a header
 * slot with a measurement that is true about that tab and only that tab —
 * "how settled is this graph" versus "does this tree validate" (see
 * ../narrative/head.ts). On the standalone screen that slot also carried the
 * title and the tab switcher; in the seat the topbar above already carries
 * both, so the slot here is the chips and nothing else, and it collapses when
 * a tab has nothing to report.
 */

export function Narrative(props: SeatBodyProps) {
  const { active, tab } = props;

  /* The chips-only head. Same HeadSlot signature both tabs already call. */
  const head = (right?: ReactNode) => (
    <div className="bg4-narseathead">{right}</div>
  );


  /* QUESTS ARE MODELLED NOW. This tab used to say they were not, which was
     true — lore.py had entities, facts and links and nothing that was a quest,
     so the third noun in the seat's own mission had no home. See migration
     0038, bgate_core/quests.py and ../narrative/Quests.tsx. */
  return (
    <div className="bg4-narseat">
      {tab === "dialogue" ? <Dialogue head={head} active={active} />
       : tab === "quests" ? <Quests head={head} active={active} />
       : <Lore head={head} active={active} />}
    </div>
  );
}
