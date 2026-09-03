import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { Textarea } from "@mantine/core";
import { Icon } from "../../components/Icon";
import { useEvents } from "../../hooks";
import {
  ask, bibleAdd, bibleRead, bibleRemove, bibleReorder, bibleUpdate,
  EMPTY_BIBLE, type Bible as BibleDoc, type Section,
} from "./api";
import { measure, peek } from "./prose";
import { Refs } from "./Refs";

/* The design bible — every section editable in place, drag-ordered within its
 * kind. The bible is a design document, not a settings list: ~52k characters
 * across 33 sections used to paint as 33 always-open bodies in one column.
 * Sections collapse to title + one-line peek + weight, the spine gives a
 * 14-part arc a shape you can take in at once, and the big kinds get the full
 * column. */

const KIND_LABEL: Record<string, string> = {
  pillar: "Pillars", loop: "Core loop", constraint: "Constraints", reference: "References",
};
const EDITABLE_KINDS = ["pillar", "loop", "constraint", "reference"] as const;
type Kind = typeof EDITABLE_KINDS[number];
/* Every section header names an icon from icons.js, chosen for what the
   section IS: an anchor holds the design down, a loop is a loop, a lock is a
   constraint. */
const KIND_ICON: Record<Kind, string> = {
  pillar: "anchor", loop: "loop", constraint: "lock", reference: "reference",
};
// /api/bible answers the four editable kinds under their own plural keys.
const GROUP_KEY: Record<Kind, keyof BibleDoc> = {
  pillar: "pillars", loop: "loop", constraint: "constraints", reference: "references",
};

/* No event kind describes a bible write (bgate_core/store/events.py), so the
   document refreshes on the bus's fallback timer — and never while the reader
   is typing in it, because a refetch that landed mid-edit would reseed the
   field under the caret. */
const REFRESH_MS = 30000;

export function Bible({ active }: { active: boolean }) {
  const host = useRef<HTMLDivElement>(null);
  const [bible, setBible] = useState<BibleDoc>(EMPTY_BIBLE);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  /* Which sections are open. Lives in state rather than the DOM so a save that
     rebuilds the rows cannot collapse everything the reader had opened. */
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [lit, setLit] = useState<number | null>(null);

  const editing = () => !!host.current && host.current.contains(document.activeElement);

  const refresh = useCallback(async () => {
    if (editing()) return;
    const b = await bibleRead();
    setLoaded(true);
    if (b.__error) { setError(b.__error); return; }
    setError("");
    setBible(b);
  }, []);
  useEvents(refresh, { enabled: active, kinds: [], fallbackMs: REFRESH_MS });

  const reload = useCallback(async () => {
    const b = await bibleRead();
    setLoaded(true);
    if (b.__error) { setError(b.__error); return; }
    setError("");
    setBible(b);
  }, []);

  const sectionsOf = (kind: Kind): Section[] => (bible[GROUP_KEY[kind]] as Section[]) || [];

  const toggle = (id: number, force?: boolean) => {
    const key = String(id);
    setExpanded((prev) => {
      const next = new Set(prev);
      const open = force === undefined ? !next.has(key) : force;
      if (open) next.add(key); else next.delete(key);
      return next;
    });
  };
  const toggleKind = (kind: Kind) => {
    const list = sectionsOf(kind);
    const allOpen = list.length > 0 && list.every((s) => expanded.has(String(s.id)));
    setExpanded((prev) => {
      const next = new Set(prev);
      list.forEach((s) => { if (allOpen) next.delete(String(s.id)); else next.add(String(s.id)); });
      return next;
    });
  };
  const jump = (id: number) => {
    toggle(id, true);
    setLit(id);
    window.setTimeout(() => setLit((l) => (l === id ? null : l)), 900);
    window.setTimeout(() => {
      host.current?.querySelector(`.wl-sec[data-id="${id}"]`)
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  };

  /* ---- mutations --------------------------------------------------------- */
  const addSection = async (kind: Kind) => {
    const title = await ask({
      title: `New ${KIND_LABEL[kind] || kind}`, label: "Title",
      multiline: false, required: true, ok: "add",
    });
    if (!title || !title.trim()) return;
    // Append, never rank 0: passing 0 for every kind filed each new section
    // ahead of everything already written, so order was arbitrary.
    const res = await bibleAdd(kind, title.trim(), sectionsOf(kind).length + 1);
    if (res.ok) void reload();
  };
  const removeSection = async (id: number) => {
    const res = await bibleRemove(id);
    if (res.ok) void reload();
  };
  const retitle = async (section: Section, text: string) => {
    const title = text.trim();
    if (!title) return reload();   // refuse the empty title, restore it
    if (title === section.title) return;
    const res = await bibleUpdate(section.id, { title, version: section.version });
    if (!res.ok) return reload();
    void reload();
  };
  const rebody = async (section: Section, body: string) => {
    if (body === (section.body || "")) return;
    const res = await bibleUpdate(section.id, { body, version: section.version });
    if (!res.ok) return reload();
    /* No full reload here: the reader is still in the document, and the row
       already shows what it holds. The version is refreshed in place so the
       NEXT save carries the one the server just handed out. */
    const next = res.data;
    if (next) setBible((prev) => ({
      ...prev,
      [GROUP_KEY[section.kind as Kind]]:
        sectionsOf(section.kind as Kind).map((s) => s.id === section.id ? { ...s, ...next } : s),
      sections: prev.sections.map((s) => s.id === section.id ? { ...s, ...next } : s),
    }));
  };
  const reorder = async (kind: Kind, order: number[]) => {
    const res = await bibleReorder(kind, order);
    if (res.ok) void reload();
  };

  /* ---- render ------------------------------------------------------------ */
  const groups = Object.fromEntries(EDITABLE_KINDS.map((k) => [k, sectionsOf(k)])) as Record<Kind, Section[]>;
  const total = EDITABLE_KINDS.reduce((n, k) => n + groups[k].length, 0);

  return (
    <div ref={host}>
      {!loaded && <div className="empty">reading the bible…</div>}
      {loaded && error && <div className="empty err">{error}</div>}
      {loaded && !error && (
        <div className="wl-doc">
          <nav className="spanel k-read wl-toc" aria-label="Bible contents">
            <div className="sec-h"><Icon name="outline" size={15} />
              <h3 className="sec-t">Contents</h3><span className="sec-n">{total}</span></div>
            {!total && <div className="empty">nothing written yet</div>}
            {EDITABLE_KINDS.filter((k) => groups[k].length).map((kind) => (
              <div key={kind}>
                <div className="wl-toc-k sec-sub"><Icon name={KIND_ICON[kind]} size={12} />
                  <span>{KIND_LABEL[kind]}</span>
                  <span className="wl-n">{groups[kind].length}</span></div>
                <ol className="wl-toc-l">
                  {groups[kind].map((s, i) => (
                    <li key={s.id}>
                      <button className="wl-toc-i" title={s.title} onClick={() => jump(s.id)}>
                        <span className="wl-toc-r">{i + 1}</span>
                        <span className="wl-toc-t">{s.title}</span>
                      </button>
                    </li>
                  ))}
                </ol>
              </div>
            ))}
          </nav>
          <div className="wl-kinds">
            {EDITABLE_KINDS.map((kind) => {
              const list = groups[kind];
              const allOpen = list.length > 0 && list.every((s) => expanded.has(String(s.id)));
              return (
                <section key={kind} className="spanel k-doc wl-card wl-kind" data-wide={list.length > 5 ? "1" : "0"}>
                  <div className="sec-h"><Icon name={KIND_ICON[kind]} size={15} />
                    <h3 className="sec-t">{KIND_LABEL[kind]}</h3>
                    <span className="sec-n">{list.length}</span>
                    <div className="sec-a">
                      {list.length > 0 && (
                        <button className="qbtn small ghost" onClick={() => toggleKind(kind)}>
                          {allOpen ? "collapse all" : "expand all"}
                        </button>
                      )}
                      <button className="qbtn small ghost" onClick={() => void addSection(kind)}>＋ add</button>
                    </div>
                  </div>
                  <SortableList kind={kind} list={list} onReorder={(order) => void reorder(kind, order)}
                                row={(s, drag) => (
                                  <SectionRow key={s.id} section={s} kind={kind} drag={drag}
                                              open={expanded.has(String(s.id))} lit={lit === s.id}
                                              onToggle={() => toggle(s.id)}
                                              onRemove={() => void removeSection(s.id)}
                                              onTitle={(t) => void retitle(s, t)}
                                              onBody={(b) => void rebody(s, b)} />
                                )} />
                </section>
              );
            })}
          </div>
        </div>
      )}
      {/* The pictures a section MEANS — below the document, never inside it:
          it reads the same section list and refreshes on its own. */}
      {loaded && !error && <Refs active={active} sections={bible.sections} />}
    </div>
  );
}

/* ---- dragging sections into order ------------------------------------------
 * One binder for every ordered list on the tab; POST /api/bible/reorder has
 * always taken any kind. Sections hang draggable on the grip, not the row, so
 * a text selection in the title or body cannot start a reorder. */
type Drag = "" | "dragging" | "over";

function SortableList({ kind, list, onReorder, row }: {
  kind: Kind; list: Section[]; onReorder: (order: number[]) => void;
  row: (s: Section, drag: Drag) => React.ReactNode;
}) {
  const [from, setFrom] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  const rowOf = (e: DragEvent) => (e.target as HTMLElement).closest<HTMLElement>(".wl-sec");
  const indexOf = (row: HTMLElement | null) => row ? list.findIndex((s) => String(s.id) === row.dataset.id) : -1;

  return (
    <div className="wl-list wl-secs" id={`wl-list-${kind}`}
         onDragStart={(e) => {
           const row = rowOf(e); if (!row) return;
           const i = indexOf(row);
           setFrom(i);
           e.dataTransfer.effectAllowed = "move";
           try { e.dataTransfer.setData("text/plain", String(i)); } catch { /* ie */ }
           // Drag the row, or the ghost is a lone ⠿.
           if (row !== e.target && e.dataTransfer.setDragImage) {
             try { e.dataTransfer.setDragImage(row, 16, 12); } catch { /* ie */ }
           }
         }}
         onDragEnd={() => { setFrom(null); setOver(null); }}
         onDragOver={(e) => { e.preventDefault(); setOver(indexOf(rowOf(e))); }}
         onDrop={(e) => {
           e.preventDefault();
           const to = indexOf(rowOf(e));
           setOver(null);
           if (from === null || to < 0 || to === from) { setFrom(null); return; }
           const next = list.map((s) => s.id);
           next.splice(to, 0, next.splice(from, 1)[0]);
           setFrom(null);
           onReorder(next);
         }}>
      {list.length
        ? list.map((s, i) => row(s, i === from ? "dragging" : i === over ? "over" : ""))
        : <div className="empty">nothing written yet</div>}
    </div>
  );
}

function SectionRow({ section, kind, drag, open, lit, onToggle, onRemove, onTitle, onBody }: {
  section: Section; kind: Kind; drag: Drag; open: boolean; lit: boolean;
  onToggle: () => void; onRemove: () => void;
  onTitle: (t: string) => void; onBody: (b: string) => void;
}) {
  /* Local drafts, seeded from the server and reseeded when its version moves
     — unless the field is the one being typed in. */
  const [title, setTitle] = useState(section.title);
  const [body, setBody] = useState(section.body || "");
  const titleRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (document.activeElement !== titleRef.current) setTitle(section.title);
    if (document.activeElement !== bodyRef.current) setBody(section.body || "");
  }, [section.version, section.title, section.body]);

  const m = measure(body);

  return (
    <article className={`wl-sec${open ? " open" : ""}${lit ? " lit" : ""}${drag ? " " + drag : ""}`}
             data-id={section.id} data-kind={kind}>
      <div className="wl-sec-h">
        <span className="wl-grip" draggable title="Drag to re-order">⠿</span>
        <button className="wl-disc" aria-expanded={open} aria-label="Expand section" onClick={onToggle}>▸</button>
        <input ref={titleRef} className="wl-title" value={title} spellCheck={false} aria-label="Section title"
               onChange={(e) => setTitle(e.currentTarget.value)}
               onBlur={() => onTitle(title)}
               onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); e.currentTarget.blur(); } }} />
        <span className="wl-meas" title={m.full}>{m.short}</span>
        <button className="wl-x" title="Delete" onClick={onRemove}>✕</button>
      </div>
      <button className="wl-peek" tabIndex={-1} onClick={onToggle}>{peek(body)}</button>
      <div className="wl-bodywrap">
        <Textarea ref={bodyRef} variant="unstyled" autosize minRows={1} value={body}
                  spellCheck={false} aria-label="Section body"
                  placeholder="say more - the seats read this"
                  classNames={{ input: "wl-body" }}
                  onChange={(e) => setBody(e.currentTarget.value)}
                  onBlur={() => onBody(body)} />
      </div>
    </article>
  );
}
