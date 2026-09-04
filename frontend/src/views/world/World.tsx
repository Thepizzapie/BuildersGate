import { useRef, useState, type ReactNode } from "react";
import { useViewActive } from "../../hooks";
import { Bible } from "./Bible";
import { LoreGraph } from "./LoreGraph";
import { Lore } from "../../shell/narrative/Lore";
import { Dialogue } from "../../shell/narrative/Dialogue";
import { Quests } from "../../shell/narrative/Quests";
import "../../shell/narrative/narrative.css";
import "./world.css";
import { setUrlParams, urlParam } from "../../shell/urlState";

/* World — the producer's surface: the design bible with its reference
 * anchors, and the lore graph. Two tabs, drawn with the same sub-nav the
 * other decks wear (.seat-subnav / .seat-tab in app.css).
 *
 * The scope tiers, the "Stranded by the line" and "The line" panels, and
 * GET /api/scope are gone (2026-08-10): the gate underneath them never refused
 * an item. What is left is a write surface — see Bible.tsx and LoreGraph.tsx. */

const TABS = [
  { id: "bible", label: "Design bible" },
  { id: "graph", label: "Relationships" },
  { id: "lore", label: "Lore" },
  { id: "dialogue", label: "Dialogue" },
  { id: "quests", label: "Quests" },
] as const;
type Tab = typeof TABS[number]["id"];

export default function World() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [tab, setTab] = useState<Tab>(() => {
    const linked = urlParam("world_tab") as Tab;
    return TABS.some((item) => item.id === linked) ? linked : "bible";
  });
  const head = (right?: ReactNode) => <div className="world-author-head">{right}</div>;

  return (
    <div ref={host}>
      <div className="seat-subnav" id="world-subnav">
        {TABS.map((t) => (
          <button key={t.id} className={`seat-tab${t.id === tab ? " active" : ""}`}
                  onClick={() => { setTab(t.id); setUrlParams({ world_tab: t.id }); }}>{t.label}</button>
        ))}
      </div>
      <div id="world-root">
        {tab === "bible" ? <Bible active={active} />
          : tab === "graph" ? <LoreGraph active={active} />
          : tab === "dialogue" ? <div className="world-author"><Dialogue head={head} active={active} /></div>
          : tab === "quests" ? <div className="world-author"><Quests head={head} active={active} /></div>
          : <div className="world-author"><Lore head={head} active={active} /></div>}
      </div>
    </div>
  );
}
