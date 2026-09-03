import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ScrollArea, Text, Textarea, Tooltip } from "@mantine/core";
import { Ti } from "../Ti";
import { useEvents, useViewActive } from "../../hooks";
import {
  canonCheck, loreBrief, loreList,
  type Entity, type Fact,
  type CanonFlag, type CanonVerdict as Verdict,
} from "../narrative/api";
import "./canon.css";

/* 9 / 9a · THE NARRATIVE ROOM'S RIGHT COLUMN.
 *
 * Frame 9 says narrative is the only other seat allowed a room and that the
 * code makes it a genuinely different one, then names four differences from 8a.
 * This file is the first two of them, and the fourth:
 *
 *   1. The world context is not the board. It is EXISTING CANON — established
 *      entities and facts, the ones a proposal may not contradict at all
 *      sorted first. That is what <CanonColumn> renders, in place of the
 *      director room's pads/roster column.
 *   2. The sketch pad goes. There is no drawing surface in this file and that
 *      is the point: this seat argues in sentences, not diagrams. The notes
 *      pad stays with the room; a canvas would be the wrong instrument for a
 *      claim you have to be able to diff.
 *   4. Every narrative write passes canon_check, which answers ok / review /
 *      conflict. <CanonVerdict> is that verdict, small enough to sit on a plan
 *      row (see CANON_KINDS below for difference 3, which is the room's to
 *      draw because the plan preview lives there).
 *
 * WHAT "SORTED FIRST" HAS TO MEAN HERE, because the design's word and the
 * database's word are not the same word. `locked` is a column on canon_fact,
 * not on lore_entity; entities carry a STATUS (draft | canon | retired). So the
 * ordering is by HOW HARD THE REFUSAL IS, read straight out of canon.check:
 *
 *   locked facts   → contradicting one is `conflict`. Refused.
 *   retired entity → merely APPEARING in new text is `conflict`. Refused.
 *   canon entity   → its unlocked facts flag `review`.
 *   draft entity   → naming it is `review`; it is not settled canon.
 *
 * Locked facts are therefore pinned above the list as their own band, and the
 * entity list runs hard-refusal-first. Sorting alphabetically would have buried
 * the only rows whose job is to stop a write.
 *
 * THE ONE ROUTE THIS SCREEN IS MISSING. bgate_core/lore.py has all_facts() —
 * every fact in the project with its entity resolved, in one query — and
 * bgate_ui/routes/world.py does not expose it. The only fact routes are
 * per-entity (`GET /api/lore/{ref}` and `/api/lore/{ref}/facts`), so the locked
 * band below is assembled from one request per entity, chunked, once per list
 * change rather than once per poll. On the project this was built against that
 * is 28 requests against a loopback SQLite file. It works and it is honest; it
 * is also the thing to delete the day `GET /api/lore/facts` exists. Asking for
 * that route is in HANDOFF-narroom.md.
 *
 * `/api/lore`'s graph payload carries a per-entity FACT COUNT, and readJSON
 * unwraps the envelope to `data` and drops every sibling key — so that count
 * never arrives (narrative/api.ts says the same thing at more length). The
 * counts drawn here are counted from facts actually fetched, and a row that has
 * not been read yet shows nothing rather than a zero.
 */

/* Canon moves at the speed of somebody writing a paragraph, not at the speed of
   a conversation. The room polls its transcript every 6s; this list has no
   business on that cadence, and every tick of it costs a fact sweep. */
const LIST_MS = 30000;
/* Parallel briefs per wave. Six is a compromise between one-at-a-time (28
   round trips serialised behind each other) and all-at-once (28 sockets and a
   burst the dev server answers out of order). */
const WAVE = 6;
/* Long enough that a sentence being typed is checked once, not per keystroke —
   canon.check is an O(facts × sentences) scan and this is a room, not a form. */
const CHECK_MS = 650;

const STATUS_COLOR: Record<string, string> = {
  canon: "var(--good, #4ec98f)",
  draft: "var(--warn, #ffbb45)",
  retired: "var(--bad, #ff7a6b)",
};

/* Why each status is here, in the words of the check that enforces it. The
   tooltip is the only place the seat learns that `retired` is stricter than
   `draft` without reading canon.py. */
const STATUS_WHY: Record<string, string> = {
  canon: "settled. Its facts are checked against every write; a locked one refuses.",
  retired: "retired from canon. Naming it in new content is a conflict, full stop.",
  draft: "not settled canon yet. Naming it is a review flag, not a refusal.",
};

const VERDICT_ICON: Record<string, string> = {
  ok: "circle-check", review: "eye", conflict: "alert-hexagon",
};

/** What a narrative room may file. brainstorm.py lets a narrative session file
 *  for exactly one seat — itself — so the plan row's seat column would read
 *  `narrative` eleven times; frame 9 replaces it with THE KIND OF CANON. These
 *  are the four kinds the seat's items actually are (frame 9: "a lore entity, a
 *  canon fact, a relationship, a bible section — which is why it gets no
 *  manifest").
 *
 *  Exported rather than used here: the plan preview lives in the room, and no
 *  field on a work item records which of these it is. It is a choice the person
 *  filing makes, so it is offered as a list — deriving it from the title would
 *  be a guess wearing a data label. */
export const CANON_KINDS = [
  "lore entity", "canon fact", "relationship", "bible section",
] as const;

export type CanonKind = typeof CANON_KINDS[number];

/* ── the verdict ──────────────────────────────────────────────────────────── */

/** canon_check on a proposed sentence, rendered as ok / review / conflict.
 *
 *  POST /api/canon/check is real (routes/world.py) and runs the same
 *  deterministic checks as the MCP tool, so this verdict is the server's and
 *  not a local approximation. When it does not answer — no token, older build,
 *  route removed — this says so. It never falls back to "ok": a green tick
 *  synthesised from a failed request is precisely the lie the gate exists to
 *  prevent.
 *
 *  `compact` is the plan-row form: one chip, the flag count, the detail in the
 *  tooltip. A plan of eleven rows cannot afford eleven flag lists.
 */
export function CanonVerdict({ text, entities, compact }: {
  text: string;
  /** Slugs to force into the check even if the text does not name them — the
   *  entity a row is about, when you know it. Optional by design: canon.check
   *  finds mentions by itself. */
  entities?: string[];
  compact?: boolean;
}) {
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  /* Replies can land out of order — a long paste checked before a short edit
     takes longer to come back. Only the newest request may write state. */
  const seq = useRef(0);
  /* The effect keys on the JOINED slugs, not on the array. A caller writing
     `entities={[e.slug]}` hands us a new array every render; depending on the
     array itself re-ran this effect on every render, and the setBusy(true)
     inside it re-rendered — a loop that only shows up once someone renders the
     chip on a plan row, which is exactly what it is for. The live value comes
     from a ref so the check still sends the current slugs. */
  const key = (entities || []).join(",");
  const held = useRef(entities);
  held.current = entities;

  useEffect(() => {
    const body = (text || "").trim();
    const mine = ++seq.current;
    if (!body) { setVerdict(null); setError(""); setBusy(false); return; }
    setBusy(true);
    const timer = window.setTimeout(async () => {
      try {
        const r = await canonCheck(body, held.current);
        if (seq.current !== mine) return;
        setBusy(false);
        if (r.ok && r.data) { setVerdict(r.data); setError(""); }
        else { setVerdict(null); setError(r.error || "canon_check did not answer"); }
      } catch (e) {
        /* mutate() throws when the classic page is not underneath us. Outside
           the shell that is a wiring fault, not a canon verdict. */
        if (seq.current !== mine) return;
        setBusy(false); setVerdict(null);
        setError(String((e as Error).message || e));
      }
    }, CHECK_MS);
    return () => window.clearTimeout(timer);
  }, [text, key]);

  if (!text.trim())
    return compact ? null : (
      <div className="bg4-canonverdict idle">
        <Ti name="scale" size={14} />
        <span>write a sentence and canon_check reads it against the facts above</span>
      </div>
    );

  if (busy && !verdict && !error)
    return compact
      ? <span className="bg4-canonchip busy">checking…</span>
      : (
        <div className="bg4-canonverdict idle">
          <Ti name="scale" size={14} /><span>canon_check running…</span>
        </div>
      );

  if (error || !verdict)
    return compact ? (
      <Tooltip label={error} withArrow multiline w={260} openDelay={150}>
        <span className="bg4-canonchip unknown">canon_check —</span>
      </Tooltip>
    ) : (
      <div className="bg4-canonverdict unknown">
        <div className="h">
          <Ti name="alert-hexagon" size={15} />
          <b>canon_check did not answer</b>
        </div>
        <p className="m">
          {error}. Nothing is claimed about this sentence — an unanswered gate
          is not a pass.
        </p>
      </div>
    );

  const v = verdict.verdict;
  const flags = verdict.flags || [];
  const hard = flags.filter((f) => f.level === "conflict").length;

  if (compact) {
    return (
      <Tooltip withArrow multiline w={300} openDelay={150}
               label={flags.length
                 ? flags.map((f) => f.message || f.code).join(" · ")
                 : `nothing to look at · ${verdict.canon.length} facts consulted`}>
        <span className={`bg4-canonchip ${v}`}>
          <Ti name={VERDICT_ICON[v]} size={11} />
          {v}{flags.length ? ` · ${flags.length}` : ""}
        </span>
      </Tooltip>
    );
  }

  return (
    <div className={`bg4-canonverdict ${v}`}>
      <div className="h">
        <Ti name={VERDICT_ICON[v]} size={15} />
        <b>canon_check · {v}</b>
        <span className="sp" />
        <span className="m">{verdict.canon.length} facts consulted</span>
      </div>
      <p className="m">
        {v === "conflict"
          ? `${hard} hard flag${hard === 1 ? "" : "s"} — this would be refused as a `
            + "write. Fix it, or a human may override after reading the flags."
          : v === "review"
          ? "soft flags only. A first draft normally reads like this; it is a "
            + "glance, not a refusal."
          : "nothing lexical to look at. These checks do not read for theme."}
      </p>
      {flags.map((f, i) => <FlagRow key={i} flag={f} />)}
      {!!verdict.mentions.length && (
        <div className="ment">
          {verdict.mentions.map((m) => (
            <span key={m.slug} style={{ color: STATUS_COLOR[m.status] }}>
              {m.name}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** One flag. The polarity and numeric codes carry BOTH sides — the canon and
 *  the sentence — and showing only the message loses the half you need to decide
 *  which of the two is wrong. */
function FlagRow({ flag }: { flag: CanonFlag }) {
  return (
    <div className={`bg4-canonflag ${flag.level}`}>
      <span className="c">{flag.code}</span>
      <div className="t">
        <span>{flag.message || flag.code}</span>
        {flag.canon && <span className="q canon">canon: {flag.canon}</span>}
        {flag.text && <span className="q">yours: {flag.text}</span>}
      </div>
    </div>
  );
}

/* ── the column ───────────────────────────────────────────────────────────── */

type Row = {
  entity: Entity;
  /** undefined until this entity's brief has come back. `0` is a real count of
   *  zero facts; `undefined` is "not read", and the two must not collapse. */
  facts?: Fact[];
};

/** The right-hand column of a NARRATIVE room: the canon a proposal is being
 *  written against.
 *
 *  Drop-in for the director room's pads column — the room renders this instead
 *  when `session.seat === "narrative"`. It reads nothing from the session
 *  except its id, because canon belongs to the project and not to the
 *  conversation; `sessionId` is here so that switching rooms collapses whatever
 *  was expanded and clears the scratch sentence, which otherwise reads as a
 *  claim about the room you just opened.
 */
export function CanonColumn({ seat, sessionId }: {
  seat?: string;
  sessionId?: number;
}) {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [rows, setRows] = useState<Row[]>([]);
  const [listError, setListError] = useState<string | undefined>();
  const [factError, setFactError] = useState(0);
  const [sweeping, setSweeping] = useState(false);
  const [open, setOpen] = useState<string>("");
  const [probe, setProbe] = useState("");

  /* Which entities have already had their facts read, keyed by the stamp they
     had when we read them. An agent's lore_add_fact bumps `updated_at`, so a
     changed stamp is the cheapest correct cache key available without the
     all-facts route. A ref, not state: it must not trigger the render that
     schedules the sweep that writes it. */
  const read = useRef<Record<string, string>>({});

  const sweep = useCallback(async (entities: Entity[]) => {
    const need = entities.filter(
      (e) => read.current[e.slug] !== (e.updated_at || ""));
    if (!need.length) return;
    setSweeping(true);
    let failed = 0;
    for (let i = 0; i < need.length; i += WAVE) {
      const wave = need.slice(i, i + WAVE);
      const briefs = await Promise.all(wave.map((e) => loreBrief(e.slug)));
      const got: Record<string, Fact[]> = {};
      wave.forEach((e, n) => {
        const b = briefs[n];
        /* loreBrief answers the EMPTY brief with `__error` on failure rather
           than throwing. Recording an empty fact list for it would quietly
           report an entity as having no canon at all. */
        if (!b?.entity) { failed += 1; return; }
        got[e.slug] = b.facts || [];
        read.current[e.slug] = e.updated_at || "";
      });
      setRows((prev) => prev.map((r) => (
        got[r.entity.slug] ? { ...r, facts: got[r.entity.slug] } : r)));
    }
    setFactError(failed);
    setSweeping(false);
  }, []);

  const refresh = useCallback(async () => {
    const { entities, error } = await loreList();
    setListError(error);
    setRows((prev) => {
      const held = new Map(prev.map((r) => [r.entity.slug, r.facts]));
      return entities.map((e) => ({ entity: e, facts: held.get(e.slug) }));
    });
    void sweep(entities);
  }, [sweep]);

  useEvents(refresh, { enabled: active, kinds: [], fallbackMs: LIST_MS });

  /* A new room is a new argument. Anything expanded or half-typed described the
     last one. */
  useEffect(() => { setOpen(""); setProbe(""); }, [sessionId]);

  /** Every locked fact in the project, which is the band that gets pinned. The
   *  entity is carried alongside because a statement out of context ("it was
   *  seven, not three") is not a constraint anyone can apply. */
  const locked = useMemo(() => {
    const out: { fact: Fact; entity: Entity }[] = [];
    for (const r of rows)
      for (const f of r.facts || [])
        if (f.locked) out.push({ fact: f, entity: r.entity });
    return out;
  }, [rows]);

  /** Hard refusals first. See the file header for why this order and not the
   *  alphabet: these rows exist to stop a write, and a row that stops one has
   *  to be above the fold. */
  const sorted = useMemo(() => {
    const rank = (r: Row) => {
      if ((r.facts || []).some((f) => f.locked)) return 0;   // locked → refusal
      if (r.entity.status === "retired") return 1;           // named → refusal
      if (r.entity.status === "canon") return 2;             // facts → review
      return 3;                                              // draft
    };
    return [...rows].sort((a, b) =>
      rank(a) - rank(b) || a.entity.name.localeCompare(b.entity.name));
  }, [rows]);

  const counted = rows.filter((r) => r.facts !== undefined).length;

  return (
    <aside className="bg4-canon" ref={host}>
      <div className="bg4-canon-head">
        <Ti name="book-2" size={14} />
        <span>Existing canon</span>
        <span className="sub">{rows.length || "—"} entities</span>
      </div>

      <p className="bg4-canon-why">
        Not the board. This room proposes against what is already true, and
        {seat && seat !== "narrative"
          ? ` this room is owned by ${seat} — canon still binds it.`
          : " files for one seat only, itself."}
      </p>

      {/* LOCKED FIRST, AND LABELLED AS THE HARD ONES. Frame 9: "locked ones
          sorted first, because those are the ones a proposal may not contradict
          at all". */}
      <div className="bg4-canon-head bordered">
        <Ti name="lock" size={14} />
        <span>Locked facts</span>
        <span className="sub">
          {sweeping && !locked.length ? "reading…" : `${locked.length} immovable`}
        </span>
      </div>

      <div className="bg4-canon-locked">
        {locked.map(({ fact, entity }) => (
          <div key={fact.id} className="row">
            <Ti name="lock" size={12} />
            <div className="w">
              <span className="s">{fact.statement}</span>
              <span className="e">{entity.name}{fact.source ? ` · ${fact.source}` : ""}</span>
            </div>
          </div>
        ))}
        {!locked.length && !sweeping && (
          <Text size="xs" c="dimmed">
            {counted
              ? "no fact is locked yet. A locked fact is the only thing that "
                + "turns a canon_check flag into a refusal — lock one on the "
                + "narrative seat page and it pins here."
              : "facts have not been read yet."}
          </Text>
        )}
      </div>

      <div className="bg4-canon-head bordered">
        <Ti name="notebook" size={14} />
        <span>Established</span>
        <span className="sub">hardest refusal first</span>
      </div>

      <ScrollArea className="bg4-canon-scroll" type="auto">
        {sorted.map((r) => {
          const e = r.entity;
          const facts = r.facts;
          const hasLock = (facts || []).some((f) => f.locked);
          return (
            <div key={e.slug} className={`bg4-canonent ${open === e.slug ? "on" : ""}`}>
              <button className="hd"
                      onClick={() => setOpen((s) => (s === e.slug ? "" : e.slug))}>
                <Ti name={open === e.slug ? "chevron-down" : "chevron-right"} size={12} />
                <Tooltip label={STATUS_WHY[e.status] || e.status} withArrow
                         multiline w={260} openDelay={200}>
                  <i className="dot" style={{ background: STATUS_COLOR[e.status]
                                                          || "var(--text-3)" }} />
                </Tooltip>
                <span className="nm">{e.name}</span>
                <span className="kd">{e.kind}</span>
                {hasLock && <Ti name="lock" size={11} className="lk" />}
                {/* A count only once the facts behind it have arrived. An
                    unread row shows nothing — a zero here would be a claim. */}
                {facts !== undefined && (
                  <span className="n">{facts.length}</span>
                )}
              </button>
              {open === e.slug && (
                <div className="bd">
                  {e.summary && <p className="sm">{e.summary}</p>}
                  {(facts || []).map((f) => (
                    <div key={f.id} className={f.locked ? "ft lock" : "ft"}>
                      <Ti name={f.locked ? "lock" : "point-filled"} size={11} />
                      <span>{f.statement}</span>
                    </div>
                  ))}
                  {facts !== undefined && !facts.length && (
                    <Text size="xs" c="dimmed">
                      no atomic facts — only the prose above, which canon_check
                      cannot diff. A fact is one sentence, asserted.
                    </Text>
                  )}
                  {facts === undefined && (
                    <Text size="xs" c="dimmed">facts not read yet.</Text>
                  )}
                </div>
              )}
            </div>
          );
        })}

        {!rows.length && (
          <Text size="xs" c="dimmed" p="xs">
            {listError
              ? `the lore graph could not be read — ${listError}`
              : "no entities yet. A lore entity, added on the narrative seat "
                + "page or by lore_add, is what fills this."}
          </Text>
        )}
        {!!factError && (
          <Text size="xs" c="dimmed" p="xs">
            {factError} entit{factError === 1 ? "y" : "ies"} would not open — their
            facts are missing from the band above, not absent from the project.
          </Text>
        )}
      </ScrollArea>

      {/* THE FOURTH DIFFERENCE, USABLE RATHER THAN DESCRIBED. Every narrative
          write passes canon_check; here it is on a sentence you have not
          written yet, which is the cheap moment to find out. */}
      <div className="bg4-canon-head bordered">
        <Ti name="scale" size={14} />
        <span>Check a sentence</span>
        <span className="sub">writes nothing</span>
      </div>
      <Textarea className="bg4-canon-probe" variant="unstyled" autosize
                minRows={2} maxRows={5}
                placeholder="a claim you are about to make — checked, not filed"
                value={probe} onChange={(ev) => setProbe(ev.currentTarget.value)} />
      <CanonVerdict text={probe} />

      {/* The room's toolless rule has a narrative counterpart, and it is the
          one sentence this column exists to make true. */}
      <div className="bg4-canon-rule">
        <Ti name="shield-lock" size={15} />
        <div>
          A proposal is an opinion until you file it, and a narrative write is
          refused if it contradicts a locked fact. `review` is a flag you may
          file over; `conflict` is a refusal only a human may override, and only
          after reading the flags.
        </div>
      </div>
    </aside>
  );
}
