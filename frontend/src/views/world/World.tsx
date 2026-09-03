import { useRef, useState } from "react";
import { useViewActive } from "../../hooks";
import { Bible } from "./Bible";
import { LoreGraph } from "./LoreGraph";
import "./world.css";

/* World — the producer's surface: the design bible with its reference
 * anchors, and the lore graph. Two tabs, drawn with the same sub-nav the
 * other decks wear (.seat-subnav / .seat-tab in app.css).
 *
 * The scope tiers, the "Stranded by the line" and "The line" panels, and
 * GET /api/scope are gone (2026-08-10): the gate underneath them never refused
 * an item. What is left is a write surface — see Bible.tsx and LoreGraph.tsx. */

const TABS = [
  { id: "bible", label: "Bible" },
  { id: "lore", label: "Lore graph" },
] as const;
type Tab = typeof TABS[number]["id"];

export default function World() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [tab, setTab] = useState<Tab>("bible");

  return (
    <div ref={host}>
      <div className="seat-subnav" id="world-subnav">
        {TABS.map((t) => (
          <button key={t.id} className={`seat-tab${t.id === tab ? " active" : ""}`}
                  onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </div>
      <div id="world-root">
        {tab === "bible" ? <Bible active={active} /> : <LoreGraph active={active} />}
      </div>
    </div>
  );
}
