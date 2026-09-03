import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Button, ScrollArea, Text, Textarea, Tooltip } from "@mantine/core";
import { Ti } from "../Ti";
import { useEvents } from "../../hooks";
import {
  canonCheck, loreAddFact, loreAllFacts, loreBrief, loreList, loreSave,
  loreSetStatus,
  type Brief, type CanonVerdict, type Entity, type FactRow, EMPTY_BRIEF,
} from "./api";
import type { HeadSlot } from "./head";

/* 10a · LORE — prose and assertions, side by side.
 *
 * lore.py splits them on purpose, and says why in its first paragraph:
 * paragraphs live in the entity body for humans, one-sentence claims live in
 * canon_fact, "because you cannot diff a paragraph for contradictions, but you
 * can diff a sentence". The old World view stacked the two, which quietly
 * argued they were the same material at different lengths. Here they are two
 * columns of equal width: the body you read, and the facts canon_check will
 * actually enforce against the next thing anyone writes.
 *
 * WHAT WOULD BREAK is the third band, and it is the seat's real question before
 * an edit. It is drawn from lore.links_of — the edges pointing at this entity
 * are the other entities whose meaning depends on it. The design sketched file
 * paths there; nothing in the backend maps an entity to the files that
 * reference it, so this shows the edges that do exist rather than a plausible
 * list of filenames.
 *
 * THE GATE IS A BUTTON AND A SAVE. canon_check runs on demand while you edit,
 * and PATCH /api/lore/{ref} runs it again server-side and answers 409 on a hard
 * conflict — so the button is a courtesy and the refusal is the guarantee.
 *
 * AND THE PANEL CAN NOW FEED THE GATE, WHICH IT COULD NOT. It reported that an
 * entity had no facts and offered no way to assert one; it drew the status
 * canon_check reads and offered no way to move it; it showed a summary in an
 * editor that sent only the body. All three were MCP-only doors on the screen
 * whose subject is the gate. Every one of them goes through world.py, which
 * runs canon.check first and requires a human actor for the two that decide
 * what canon IS — locking a fact, and declaring an entity canon or retired.
 */

/* Slow-moving material. The list is polled so an agent's lore_add appears
   without a reload; the OPEN entity is not, because a poll landing mid-edit
   would overwrite the paragraph being typed. */
const LIST_MS = 20000;

/** The three the module defines, in the order a piece of fiction moves through
 *  them. `retired` is not a delete: canon_check turns every later mention of a
 *  retired entity into a hard conflict, which is the point of it. */
const STATUSES = ["draft", "canon", "retired"] as const;

const STATUS_COLOR: Record<string, string> = {
  canon: "var(--good, #4ec98f)",
  draft: "var(--warn, #ffbb45)",
  retired: "var(--text-3)",
};

const VERDICT_ICON: Record<string, string> = {
  ok: "circle-check", review: "eye", conflict: "alert-hexagon",
};
const VERDICT_COLOR: Record<string, string> = {
  ok: "var(--good, #4ec98f)", review: "var(--warn, #ffbb45)",
  conflict: "var(--bad, #ff7a6b)",
};

export function Lore({ head, active }: { head: HeadSlot; active: boolean }) {
  const [entities, setEntities] = useState<Entity[]>([]);
  const [listError, setListError] = useState<string | undefined>();
  const [kind, setKind] = useState("");
  const [query, setQuery] = useState("");
  const [slug, setSlug] = useState("");
  const [brief, setBrief] = useState<Brief>(EMPTY_BRIEF);
  const [briefError, setBriefError] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [sumDraft, setSumDraft] = useState("");
  const [verdict, setVerdict] = useState<CanonVerdict | null>(null);
  const [busy, setBusy] = useState<null | "check" | "save" | "fact" | "status">(null);
  const [refused, setRefused] = useState("");

  /* Every fact in the project, one request. The per-entity count on the rail
     and the locked census in the header both come from here — see api.ts for
     why the graph could not supply them. */
  const [facts, setFacts] = useState<FactRow[]>([]);

  /* The new-fact form, open or not. `null` is closed; "" is a fact being
     written and not yet said. */
  const [fact, setFact] = useState<null | { statement: string; source: string; locked: boolean }>(null);
  const [factErr, setFactErr] = useState("");

  const refresh = useCallback(async () => {
    const [{ entities: rows, error }, all] = await Promise.all([
      loreList(), loreAllFacts(),
    ]);
    setListError(error);
    setEntities(rows);
    /* A failed fact read is NOT zero facts. Leaving the previous census in
       place is the honest degradation: a count that was true a minute ago
       beats a zero that was never true. */
    if (!all.error) setFacts(all.facts);
    setSlug((s) => (s || rows[0]?.slug || ""));
  }, []);
  useEvents(refresh, { enabled: active, kinds: [], fallbackMs: LIST_MS });

  const open = useCallback(async (ref: string) => {
    const b = await loreBrief(ref);
    /* A brief that could not be read used to render as "pick an entity on the
       left" — underneath the entity that was already picked and highlighted.
       The read failure now says so. */
    const err = (b as unknown as { __error?: string }).__error;
    setBriefError(err || "");
    setBrief(b?.entity ? b : EMPTY_BRIEF);
    setEditing(false);
    setVerdict(null);
    setRefused("");
    setFact(null);
    setFactErr("");
  }, []);
  useEffect(() => { if (slug) void open(slug); }, [slug, open]);

  const kinds = useMemo(() => {
    /* Only the kinds this project actually uses. lore.KINDS has seven and the
       list would otherwise show four chips reading zero, which is a filter that
       returns nothing and looks broken. */
    const n = new Map<string, number>();
    entities.forEach((e) => n.set(e.kind, (n.get(e.kind) || 0) + 1));
    return Array.from(n.entries()).sort((a, b) => b[1] - a[1]);
  }, [entities]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return entities.filter((e) =>
      (!kind || e.kind === kind)
      && (!q || e.slug.includes(q) || e.name.toLowerCase().includes(q)));
  }, [entities, kind, query]);

  const statuses = useMemo(() => {
    const n = new Map<string, number>();
    entities.forEach((e) => n.set(e.status, (n.get(e.status) || 0) + 1));
    return ["canon", "draft", "retired"].map((s) => [s, n.get(s) || 0] as const)
      .filter(([, c]) => c > 0);
  }, [entities]);

  const entity = brief.entity;

  /* slug → how many facts, and how many of those are locked. A locked fact is
     the only kind that turns a canon_check flag into a refusal, so the two
     numbers are not interchangeable and the rail shows the one that matters. */
  const census = useMemo(() => {
    const m = new Map<string, { n: number; locked: number }>();
    for (const f of facts) {
      const at = m.get(f.slug) || { n: 0, locked: 0 };
      at.n += 1;
      if (f.locked) at.locked += 1;
      m.set(f.slug, at);
    }
    return m;
  }, [facts]);

  /* The prose canon_check is being asked about: the summary and the body, the
     same pair PATCH /api/lore/{ref} joins and gates server-side. Checking only
     the body meant the button's verdict and the save's verdict were answers to
     two different questions. */
  const prose = () => [sumDraft, draft].filter((s) => s.trim()).join("\n");

  const runCheck = async () => {
    if (!entity) return;
    setBusy("check");
    const r = await canonCheck(prose(), [entity.slug]);
    setBusy(null);
    if (r.ok && r.data) setVerdict(r.data);
    else setRefused(r.error || "canon_check did not answer");
  };

  const save = async () => {
    if (!entity) return;
    setBusy("save");
    /* Check first, then write. The write checks again — this one is so the
       flags are on screen when the refusal arrives, because mutate() carries
       only the message and the server's flags do not survive it. */
    const pre = await canonCheck(prose(), [entity.slug]);
    if (pre.ok && pre.data) setVerdict(pre.data);
    /* BOTH FIELDS. The editor has always shown the summary and sent only the
       body, so a summary edited here was discarded on save without a word. */
    const r = await loreSave(entity.slug, { summary: sumDraft, body: draft });
    setBusy(null);
    if (!r.ok) { setRefused(r.error || "the write was refused"); return; }
    setRefused("");
    setEditing(false);
    void open(entity.slug);
    void refresh();
  };

  /* ASSERTING A FACT IS THE WRITE canon_check EXISTS FOR — world.py runs the
     gate over the statement and answers 409 when it contradicts locked canon.
     The panel could report that there were no facts and offered no way to
     write one, so the only door to the thing the whole screen is built around
     was an MCP call. */
  const assert_ = async () => {
    if (!entity || !fact) return;
    setBusy("fact");
    setFactErr("");
    const r = await loreAddFact(entity.slug, {
      statement: fact.statement, source: fact.source,
      locked: fact.locked,
    });
    setBusy(null);
    if (!r.ok) { setFactErr(r.error || "the fact was refused"); return; }
    setFact(null);
    void open(entity.slug);
    void refresh();
  };

  /* Status is what canon_check READS. Moving one is human-only server-side, so
     a refusal here is a real answer and not a bug. */
  const moveStatus = async (next: string) => {
    if (!entity || next === entity.status) return;
    setBusy("status");
    const r = await loreSetStatus(entity.slug, next);
    setBusy(null);
    if (!r.ok) { setRefused(r.error || "the status did not move"); return; }
    setRefused("");
    void open(entity.slug);
    void refresh();
  };

  const chips = (
    <>
      {statuses.map(([s, n]) => (
        <span className="bg4-narchip" key={s} style={{ color: STATUS_COLOR[s] }}>
          <i style={{ background: STATUS_COLOR[s] }} />{n} {s}
        </span>
      ))}
      {facts.length > 0 && (
        /* The census the graph could not deliver. Locked is called out because
           it is the only number that predicts a refusal. */
        <span className="bg4-narchip">
          <Ti name="lock" size={13} />
          {facts.filter((f) => f.locked).length} locked of {facts.length} facts
        </span>
      )}
      {verdict && (
        <span className="bg4-narchip" style={{ color: VERDICT_COLOR[verdict.verdict] }}>
          <Ti name={VERDICT_ICON[verdict.verdict] || "eye"} size={13} />
          canon_check · {verdict.verdict}
        </span>
      )}
    </>
  );

  return (
    <div className="bg4-nar-lore">
      <div className="bg4-narlist">
        <div className="bg4-narlist-head">
          <Ti name="search" size={14} />
          <input value={query} placeholder={`${entities.length} entities`}
                 aria-label="filter entities"
                 onChange={(e) => setQuery(e.currentTarget.value)} />
        </div>
        <div className="bg4-narkinds">
          <button className={`k${kind ? "" : " on"}`} onClick={() => setKind("")}>
            all<span>{entities.length}</span>
          </button>
          {kinds.map(([k, n]) => (
            <button key={k} className={`k${kind === k ? " on" : ""}`}
                    onClick={() => setKind(kind === k ? "" : k)}>
              {k}<span>{n}</span>
            </button>
          ))}
        </div>
        <ScrollArea className="bg4-narscroll">
          {!shown.length && (
            <div className="bg4-empty">
              {listError ? `could not read the lore graph — ${listError}`
               : entities.length ? "no entity matches that filter"
               : "no lore entities yet — lore_add, or the World bible view, puts one here"}
            </div>
          )}
          {shown.map((e) => {
            const c = census.get(e.slug);
            return (
              <button key={e.slug} className={`bg4-narrow${e.slug === slug ? " on" : ""}`}
                      onClick={() => setSlug(e.slug)}>
                <span className="l">
                  <i className="dot" style={{ background: STATUS_COLOR[e.status] || "var(--text-3)" }} />
                  <span className="s">{e.slug}</span>
                </span>
                {/* THE FACT COUNT, MEASURED. An entity with no facts asserts
                    nothing, so nothing it says can be contradicted — that is
                    the single most useful thing to know about a row here, and
                    it is why the count is not omitted when it is zero. */}
                <span className="k">
                  {e.kind} · {c ? `${c.n} fact${c.n === 1 ? "" : "s"}` : "0 facts"}
                  {c?.locked ? ` · ${c.locked} locked` : ""}
                </span>
              </button>
            );
          })}
        </ScrollArea>
      </div>

      <div className="bg4-narmain">
        {head(chips)}
        <ScrollArea className="bg4-narbody">
          {!entity && (
            <div className="bg4-empty">
              {briefError ? `${slug} could not be read — ${briefError}`
               : listError ? `could not read the lore graph — ${listError}`
               : "pick an entity on the left to read its prose and its facts"}
            </div>
          )}
          {entity && (
            <div className="bg4-narpane">
              <div className="bg4-narttl">
                <h3>{entity.name}</h3>
                <Badge size="xs" variant="light" color="grape">{entity.kind}</Badge>
                {/* STATUS IS THE ONE FIELD canon_check ITSELF READS: retired
                    makes every later mention a hard conflict, draft makes it a
                    review flag, canon makes it settled. It was drawn as a word
                    and could only be moved by an MCP call, so the screen that
                    reports the gate could not touch the input the gate uses.
                    The server refuses this for an agent actor; a human
                    dashboard session is exactly who may. */}
                <Tooltip label="what canon_check reads: retired refuses a later mention, draft flags it, canon settles it">
                  <select className="bg4-narstatus" value={entity.status}
                          disabled={busy === "status"}
                          aria-label="status"
                          style={{ color: STATUS_COLOR[entity.status] }}
                          onChange={(e) => void moveStatus(e.currentTarget.value)}>
                    {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
                    {!STATUSES.includes(entity.status as typeof STATUSES[number]) && (
                      <option value={entity.status}>{entity.status}</option>
                    )}
                  </select>
                </Tooltip>
                <span className="sp" />
                {!editing && (
                  <Button size="compact-xs" variant="default"
                          leftSection={<Ti name="pencil" size={13} />}
                          onClick={() => {
                            setDraft(entity.body || "");
                            setSumDraft(entity.summary || "");
                            setEditing(true);
                          }}>
                    edit
                  </Button>
                )}
                {editing && (
                  <>
                    <Button size="compact-xs" variant="default" loading={busy === "check"}
                            leftSection={<Ti name="shield-check" size={13} />}
                            onClick={runCheck}>
                      canon_check
                    </Button>
                    <Button size="compact-xs" variant="default"
                            onClick={() => { setEditing(false); setRefused(""); }}>
                      cancel
                    </Button>
                    <Button size="compact-xs" loading={busy === "save"} onClick={save}>
                      save
                    </Button>
                  </>
                )}
              </div>
              {/* The summary is prose, it passes the same gate, and PATCH takes
                  it — it was rendered read-only beside an editor that silently
                  dropped it. */}
              {editing ? (
                <Textarea autosize minRows={2} maxRows={6} value={sumDraft}
                          aria-label="summary"
                          placeholder="one or two sentences — the summary a reader gets first"
                          styles={{ input: { fontSize: 12.5, lineHeight: 1.7 } }}
                          mt={8}
                          onChange={(e) => setSumDraft(e.currentTarget.value)} />
              ) : entity.summary ? (
                <p className="bg4-narsum">{entity.summary}</p>
              ) : null}

              <div className="bg4-narsplit">
                <section>
                  <div className="bg4-narlbl">
                    <b>Body</b><span>prose — for people</span>
                  </div>
                  {editing ? (
                    <Textarea autosize minRows={12} maxRows={30} value={draft}
                              styles={{ input: { fontSize: 12.5, lineHeight: 1.75 } }}
                              onChange={(e) => setDraft(e.currentTarget.value)} />
                  ) : (
                    <div className="bg4-narprose">
                      {entity.body || <span className="bg4-narnone">
                        no body yet — lore_update writes the paragraphs a reader gets
                      </span>}
                    </div>
                  )}
                </section>

                <section>
                  <div className="bg4-narlbl">
                    <b>Facts</b><span>one claim each — checkable</span>
                    <span className="sp" />
                    <span className="n">{brief.facts.length}</span>
                    <Button size="compact-xs" variant="default" ml={8}
                            leftSection={<Ti name="plus" size={12} />}
                            onClick={() => setFact({ statement: "", source: "", locked: false })}>
                      assert
                    </Button>
                  </div>
                  {!brief.facts.length && (
                    <div className="bg4-narnone">
                      no facts asserted — canon_check has nothing to enforce for
                      this entity, so nothing it says can be contradicted
                    </div>
                  )}
                  {brief.facts.map((f) => (
                    <div className={`bg4-narfact${f.locked ? " locked" : ""}`} key={f.id}>
                      <Tooltip label={f.locked
                        ? "locked — a contradiction here REFUSES the write"
                        : "unlocked — a contradiction here is flagged for review"}>
                        <span className="i">
                          <Ti name={f.locked ? "lock" : "point"} size={13} />
                        </span>
                      </Tooltip>
                      <span className="t">{f.statement}</span>
                      {f.source && <span className="src">{f.source}</span>}
                    </div>
                  ))}

                  {fact && (
                    <div className="bg4-narfactform">
                      <textarea autoFocus rows={2} value={fact.statement}
                                aria-label="statement"
                                placeholder="One sentence, and only one claim — “Morale is HP and Caffeine is the action resource”. Two claims in a sentence cannot be contradicted one at a time."
                                onChange={(e) => setFact({ ...fact, statement: e.currentTarget.value })} />
                      <div className="r">
                        <input value={fact.source} aria-label="source"
                               placeholder="source — bible pillar 3, item-4 tone bible"
                               onChange={(e) => setFact({ ...fact, source: e.currentTarget.value })} />
                        <Tooltip label="locked: a contradiction REFUSES the write instead of flagging it. Only a human may set this.">
                          <label>
                            <input type="checkbox" checked={fact.locked}
                                   onChange={(e) => setFact({ ...fact, locked: e.currentTarget.checked })} />
                            locked
                          </label>
                        </Tooltip>
                        <span className="sp" />
                        <Button size="compact-xs" variant="default"
                                onClick={() => { setFact(null); setFactErr(""); }}>
                          cancel
                        </Button>
                        <Button size="compact-xs" loading={busy === "fact"}
                                disabled={!fact.statement.trim()}
                                onClick={assert_}>
                          assert
                        </Button>
                      </div>
                      <span className="h">
                        canon_check runs on this statement before it lands, and a
                        conflict with a locked fact is refused
                      </span>
                      {factErr && <div className="e">{factErr}</div>}
                    </div>
                  )}
                </section>
              </div>

              {verdict && (
                <div className={`bg4-narverdict ${verdict.verdict}`}>
                  <div className="h">
                    <Ti name={VERDICT_ICON[verdict.verdict]} size={16} />
                    <b>canon_check · {verdict.verdict}</b>
                    <span className="sp" />
                    <span className="m">
                      {verdict.verdict === "conflict"
                        ? "this will be refused on save"
                        : verdict.verdict === "review"
                        ? "flagged, not blocking — a first draft normally looks like this"
                        : `nothing to look at · ${verdict.canon.length} facts consulted`}
                    </span>
                  </div>
                  {verdict.flags.map((f, i) => (
                    <div className={`f ${f.level}`} key={i}>
                      <span className="c">{f.code}</span>
                      <span className="t">{f.message || f.code}</span>
                      {f.canon && <span className="q">canon: {f.canon}</span>}
                    </div>
                  ))}
                </div>
              )}
              {refused && (
                <div className="bg4-narverdict conflict">
                  <div className="h">
                    <Ti name="alert-hexagon" size={16} />
                    <b>the write did not land</b>
                    <span className="sp" />
                    <span className="m">the entity on the server is unchanged</span>
                  </div>
                  <div className="f conflict"><span className="t">{refused}</span></div>
                </div>
              )}

              <div className="bg4-narband">
                <b>What would break</b>
                <span className="rule" />
                <span className="n">
                  {brief.links.length} {brief.links.length === 1 ? "reference" : "references"}
                </span>
              </div>
              {!brief.links.length ? (
                <div className="bg4-narnone">
                  nothing links to this entity — lore_link draws the edges that make
                  an edit here matter somewhere else
                </div>
              ) : (
                <div className="bg4-narrefs">
                  {brief.links.map((l, i) => (
                    <button className="bg4-narref" key={`${l.dir}${l.slug}${l.rel}${i}`}
                            onClick={() => setSlug(l.slug)}>
                      <Ti name={l.dir === "in" ? "arrow-narrow-left" : "arrow-narrow-right"}
                          size={14} />
                      <span className="p">{l.slug}</span>
                      <span className="tag">{l.rel}</span>
                    </button>
                  ))}
                </div>
              )}
              {brief.links.length > 0 && (
                <Text size="10px" c="dimmed" ff="var(--mono)" mt={8}>
                  an inbound edge is a place this entity is assumed; changing what it
                  says changes what those entities mean
                </Text>
              )}
            </div>
          )}
        </ScrollArea>
      </div>
    </div>
  );
}
