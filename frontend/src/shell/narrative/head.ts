import type { ReactNode } from "react";

/* The header bar belongs to the screen, its right-hand chips belong to the tab.
 *
 * Lore's status is "how much of this graph is settled canon"; dialogue's is
 * "does this tree validate". Those are not the same measurement and there is no
 * honest way to render one control for both — so the screen hands each tab a
 * slot and each tab fills it with something true about itself. */
export type HeadSlot = (right?: ReactNode) => ReactNode;
