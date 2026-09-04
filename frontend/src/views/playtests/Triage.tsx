import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "../../components/Icon";
import { mutate, pollQueue, pollState, previewURL, toast } from "../../bridge";
import { askConfirm } from "../../ask";
import {
  KINDS, SEATS, clock, heard, partition, realEvents, scored,
  type Item, type Review,
} from "./api";

/* Triage for a recorded playtest.
 *
 * ── THE DECISION THE SCREEN IS FOR ─────────────────────────────────────────
 * A person reading playtest feedback answers ONE question per line: is this
 * worth acting on? Yes, it becomes work. No, it goes in the bin. Assigning a
 * seat, correcting the kind, folding a duplicate into another item and linking
 * an asset are all REFINEMENTS OF "YES" — so: two buttons on the face of every
 * row, everything else behind one disclosure, and the row is a row rather than
 * a card because the job is to read down a list.
 *
 * ── WHY THE GROUPING IS BY DECISION AND NOT BY KIND ────────────────────────
 * The classifier is a lexical first pass that fires on no rules at all for
 * most spoken lines, so sorting by kind produces one enormous "note" bucket.
 * What the reader needs partitioned is what is STILL OPEN versus settled: to
 * decide, low signal, praise, filed, binned. Kind is a subhead inside "to
 * decide", and only once there are enough items for it to save a scan.
 *
 * ── THE FOUR MEASURED BUGS ─────────────────────────────────────────────────
 * 1. "speech confidence -1.17" was a log probability wearing the word
 *    confidence — heard() translates at the render boundary and says nothing
 *    on a clean hearing.
 * 2. "classifier 0% - note" was a label with nothing behind it — when no rule
 *    matched the row says nothing about kind; the ratio lives in the drawer.
 * 3. "director recommends review" named no action — the recommendation moves
 *    the BUTTON ('promote' fills it) instead of adding a badge.
 * 4. A mic check got the same nine controls as a crash — quiet() folds those
 *    into one counted, honestly-labelled group. Never filtered: a hidden item
 *    is a lost item.
 *
 * PROMOTING AN ITEM DOES NOT RESTART THE VIDEO. This panel owns only its own
 * subtree; the Review above keeps the <video> mounted across a reload, so the
 * recording keeps playing at the second you left it. */

/* Below this many open items a kind subhead costs a line of chrome and saves
   nobody a scan. Two items do not need a taxonomy. */
const KIND_SUBHEAD_AT = 6;
const KIND_ORDER = ["fix", "change", "add", "question", "note", "like"];
const KIND_LABEL: Record<string, string> = {
  fix: "to fix", add: "to add", change: "to change",
  question: "questions", like: "praise", note: "notes",
};

type Picks = { seat?: string; kind?: string; merge?: string; asset?: string };

type Props = {
  data: Review;
  /** Refetch the session and repaint — the caller owns the payload. */
  reload: () => Promise<void>;
  /** Play the recording from a session-time second. */
  seek: (t: number) => void;
  /** The "show work" jump leaves the overlay. */
  close: () => void;
};

export function Triage({ data, reload, seek, close }: Props) {
  const items = data.items || [];
  const sessionId = data.session.id;

  /* State that belongs to THIS session: which drawer is open, which fold,
     where the cursor is. A different session drops it. */
  const [drawers, setDrawers] = useState<Set<number>>(() => new Set());
  const [folds, setFolds] = useState<Map<string, boolean>>(() => new Map());
  const [cursor, setCursor] = useState<number | null>(null);
  const [picks, setPicks] = useState<Record<number, Picks>>({});
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<number | null>(null);
  const [needsSeat, setNeedsSeat] = useState<number | null>(null);
  const host = useRef<HTMLDivElement>(null);
  const seatPickers = useRef(new Map<number, HTMLSelectElement>());

  useEffect(() => {
    setDrawers(new Set()); setFolds(new Map()); setCursor(null); setPicks({});
  }, [sessionId]);

  const itemById = useCallback(
    (id: number | null) => items.find((i) => i.id === Number(id)) || null, [items]);
  const pick = (id: number): Picks => picks[id] || {};
  const setPick = (id: number, patch: Picks) =>
    setPicks((p) => ({ ...p, [id]: { ...(p[id] || {}), ...patch } }));
  const toggleDrawer = (id: number) => setDrawers((d) => {
    const next = new Set(d);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const openDrawer = (id: number) => setDrawers((d) => new Set(d).add(id));
  const closeDrawer = (id: number) => setDrawers((d) => {
    const next = new Set(d); next.delete(id); return next;
  });
  const setFold = (key: string, open: boolean) =>
    setFolds((f) => new Map(f).set(key, open));

  /* ── grouping ─────────────────────────────────────────────────────────── */
  const g = useMemo(() => partition(items), [items]);
  const todo = g.open.length + g.quiet.length + g.praise.length;

  /* Kind only earns a subhead once the list is long enough. */
  const openGroups = useMemo(() => {
    const kinds = [...new Set(g.open.map((i) => i.kind))];
    if (g.open.length < KIND_SUBHEAD_AT || kinds.length < 2) return null;
    return KIND_ORDER.filter((k) => kinds.includes(k))
      .map((k) => ({ kind: k, of: g.open.filter((i) => i.kind === k) }));
  }, [g.open]);

  /* Every row the keyboard can reach, in the order they are painted. Rows
     inside a folded group are genuinely not on screen and are skipped. */
  const visible = useMemo(() => {
    const out: number[] = [];
    if (openGroups) for (const grp of openGroups) out.push(...grp.of.map((i) => i.id));
    else out.push(...g.open.map((i) => i.id));
    for (const [key, list] of [["quiet", g.quiet], ["praise", g.praise],
                               ["filed", g.filed], ["binned", g.binned]] as const) {
      if (folds.get(key) === true) out.push(...list.map((i) => i.id));
    }
    return out;
  }, [g, openGroups, folds]);

  /* ── evidence ─────────────────────────────────────────────────────────── */
  const rowEl = (id: number) =>
    host.current?.querySelector<HTMLElement>(`.ptr-row[data-id="${id}"]`) || null;

  const playAt = (t: number) => {
    seek(t);
    const stage = document.querySelector(".review-body .video-stage");
    // Sticky already keeps it on screen once it has been passed; this covers
    // the case where the reader has scrolled ABOVE it.
    if (stage && stage.getBoundingClientRect().top < 0) {
      stage.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };

  /* ── mutations ────────────────────────────────────────────────────────── */
  async function finish() {
    await reload();
    pollState(); pollQueue();
  }

  async function promote(id: number) {
    const item = itemById(id);
    if (!item) return;
    const seat = pick(id).seat ?? item.seat;
    if (!seat || seat === "unassigned") {
      // An unrouted item stays visibly unrouted rather than being filed under
      // whichever seat the browser happened to select first.
      openDrawer(id);
      setNeedsSeat(id);
      setTimeout(() => seatPickers.current.get(id)?.focus(), 0);
      setTimeout(() => setNeedsSeat((n) => (n === id ? null : n)), 2500);
      toast("nothing routed this one - pick the seat that owns it, then promote");
      return;
    }
    setBusy(true);
    const r = await mutate(`/api/playtest/items/${id}/promote`, {
      body: { seat, kind: pick(id).kind ?? item.kind },
      ok: `#${id} accepted for ${seat} - not queued yet` });
    setBusy(false);
    if (!r.ok) return;
    closeDrawer(id);
    await finish();
  }

  async function dismiss(id: number) {
    setBusy(true);
    const r = await mutate(`/api/playtest/items/${id}/dismiss`, { ok: `#${id} binned` });
    setBusy(false);
    if (!r.ok) return;
    closeDrawer(id);
    await finish();
  }

  async function merge(id: number) {
    const target = pick(id).merge || "";
    if (!target) { toast("pick the item this one duplicates first"); return; }
    // One-way on the backend: it writes status='dismissed' plus merged_into_id
    // and there is no unmerge endpoint. So it confirms rather than firing on
    // a single click.
    const t = itemById(Number(target));
    const label = t ? `#${t.id} ${String(t.text || "").slice(0, 60)}` : `#${target}`;
    const go = await askConfirm({
      title: `Merge #${id} into ${label}?`,
      body: `#${id} stops being actionable on its own and its text lives on `
          + `under the target. There is no undo yet - this cannot be reversed `
          + `from the dashboard.`,
      ok: "merge", danger: true });
    if (!go) return;
    setBusy(true);
    const r = await mutate(`/api/playtest/items/${id}/merge`, {
      body: { target_id: Number(target) }, ok: `#${id} merged into #${target}` });
    setBusy(false);
    if (!r.ok) return;
    closeDrawer(id);
    await finish();
  }

  async function link(id: number) {
    const options = data.asset_options || [];
    const artifactId = pick(id).asset || (options[0] ? String(options[0].artifact_id) : "");
    if (!artifactId) { toast("pick an asset to link"); return; }
    setBusy(true);
    const r = await mutate(`/api/artifacts/${artifactId}/feedback/${id}`, { body: {}, ok: "asset linked" });
    setBusy(false);
    if (!r.ok) return;
    await finish();
  }

  function jump(id: number) {
    const target = itemById(id);
    if (!target) return;
    setCursor(id);
    // A row inside a folded group cannot be scrolled to while it is folded.
    const key = target.merged_into_id || target.status === "dismissed" ? "binned"
      : target.status === "promoted" ? "filed" : null;
    if (key) setFold(key, true);
    setFlash(id);
    setTimeout(() => {
      rowEl(id)?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 0);
    setTimeout(() => setFlash((f) => (f === id ? null : f)), 1400);
  }

  function showWork(workId: number) {
    close();
    document.getElementById(`work-${workId}`)
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  /* ── keyboard ─────────────────────────────────────────────────────────── */
  const keys = useRef({ visible, cursor, busy, items, drawers });
  keys.current = { visible, cursor, busy, items, drawers };
  const actions = useRef({ promote, dismiss, playAt });
  actions.current = { promote, dismiss, playAt };

  useEffect(() => {
    const move = (delta: number) => {
      const rows = keys.current.visible;
      if (!rows.length) return;
      let at = rows.indexOf(keys.current.cursor ?? -1);
      at = at < 0 ? (delta > 0 ? 0 : rows.length - 1)
                  : Math.min(rows.length - 1, Math.max(0, at + delta));
      const id = rows[at];
      setCursor(id);
      setTimeout(() => rowEl(id)?.scrollIntoView({ block: "nearest" }), 0);
    };
    const onKey = (event: KeyboardEvent) => {
      if (!host.current?.isConnected || keys.current.busy) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      // Never steal a key from something that collects text, from an open
      // select, or from the video's own controls.
      const on = event.target as HTMLElement | null;
      if (on && (on.closest?.("input, textarea, select, [contenteditable='true'], video")
                 || on.isContentEditable)) return;
      const key = event.key;
      if (key === "j" || key === "ArrowDown") { event.preventDefault(); move(1); return; }
      if (key === "k" || key === "ArrowUp") { event.preventDefault(); move(-1); return; }
      const cur = keys.current.cursor;
      if (cur == null) return;
      const item = keys.current.items.find((i) => i.id === cur);
      if (!item) return;
      if (key === " ") { event.preventDefault(); actions.current.playAt(Number(item.t) || 0); return; }
      if (key === "Enter") { event.preventDefault(); toggleDrawer(cur); return; }
      if (item.status !== "new" || item.merged_into_id) return;
      if (key === "p") { event.preventDefault(); void actions.current.promote(cur); return; }
      if (key === "x") { event.preventDefault(); void actions.current.dismiss(cur); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  /* ── one row ──────────────────────────────────────────────────────────── */
  function tags(item: Item) {
    const out: React.ReactNode[] = [];
    if (item.source === "typed") {
      out.push(<span key="src" className="ptr-tag">typed</span>);
    } else if (item.source === "chat") {
      out.push(<span key="src" className="ptr-tag info">
        from chat{item.author ? ` - ${item.author}` : ""}</span>);
    }
    // Bug 1: words, not a log probability. Silent on a clean hearing.
    const h = heard(item.transcript_confidence);
    if (h) out.push(<span key="heard" className={`ptr-tag ${h.tone}`} title={h.tip}>{h.word}</span>);
    // Bug 2: a classification with nothing behind it says nothing at all.
    const cls = item.classification || {};
    if (scored(item) && cls.kind) {
      out.push(<span key="kind" className="ptr-tag"
        title="lexical first pass - open the refinements for the rules that fired">{cls.kind}</span>);
    }
    if (item.assets && item.assets.length) {
      out.push(<span key="assets" className="ptr-tag">
        {item.assets.length} asset{item.assets.length === 1 ? "" : "s"}</span>);
    }
    const near = realEvents(item);
    if (near.length) {
      out.push(<span key="lead" className="lead">while saying this:</span>);
      near.slice(0, 4).forEach((e, i) => {
        const d = e.data || {};
        out.push(<span key={`ev${i}`} className="ptr-tag ev">
          {e.kind === "setting_changed"
            ? `${String(d.prop || d.key || "setting")} -> ${String(d.value)}`
            : e.kind}</span>);
      });
      if (near.length > 4) out.push(<span key="more" className="lead">+{near.length - 4} more</span>);
    }
    return out;
  }

  /* PLAIN FUNCTIONS, NOT INNER COMPONENTS. A component defined inside render
     has a new identity every paint, so React would unmount and remount every
     row on each reload — the open <select>, the half-decoded frame images and
     the <details> state would all go with it, which is the exact flash the
     classic panel's signature guard existed to stop. */
  const more = (item: Item) => (
    <button className="ptr-b only-ico" type="button"
      aria-expanded={drawers.has(item.id)}
      title="Seat, kind, merge, linked assets, and why it was classified that way"
      aria-label={`Refinements for item ${item.id}`}
      onClick={() => { toggleDrawer(item.id); setCursor(item.id); }}>
      <Icon name="more" />
    </button>
  );

  function rowActions(item: Item) {
    if (item.merged_into_id) {
      return <>
        <span className="ptr-tag info">merged into #{item.merged_into_id}</span>
        <button className="ptr-b only-ico" type="button"
          title="Show the item this was folded into"
          onClick={() => jump(Number(item.merged_into_id))}><Icon name="select" /></button>
      </>;
    }
    if (item.status === "promoted") {
      /* "accepted", not "filed". The item has been judged real and given an
         owner; nothing has been queued. */
      return <><span className="ptr-tag good">accepted</span>{more(item)}</>;
    }
    if (item.status === "dismissed") {
      return <><span className="ptr-tag">binned</span>{more(item)}</>;
    }
    // Bug 3: the recommendation moves the button, it does not add a badge.
    const suggested = item.director_recommendation === "promote";
    const routed = Boolean(item.seat && item.seat !== "unassigned");
    return <>
      <button className={`ptr-b primary${suggested ? " go" : ""}`} type="button" disabled={busy}
        title={(routed
          ? `File this as work for the ${item.seat} seat.`
          : `Nothing routed this one - pick the seat that owns it.`)
          + (suggested ? " The director suggests this." : "")}
        onClick={() => { setCursor(item.id); void promote(item.id); }}>
        <Icon name="task" />{routed ? `Promote to ${item.seat}` : "Choose owner…"}
      </button>
      <button className="ptr-b bin" type="button" disabled={busy}
        title="Not worth acting on. It stays in the record."
        onClick={() => { setCursor(item.id); void dismiss(item.id); }}>
        <Icon name="close" />Bin
      </button>
      {more(item)}
    </>;
  }

  /* Built ONLY when open. The asset select alone is three hundred options on
     a real project; one per row, closed, for every item was thousands of DOM
     nodes nobody had asked to see. */
  function drawer(item: Item) {
    if (!drawers.has(item.id)) return null;
    const cls = item.classification || {};
    const scores = cls.scores || {};
    const isScored = Object.keys(scores).length > 0;
    const open = item.status === "new" && !item.merged_into_id;
    const targets = items.filter((t) =>
      t.id !== item.id && t.status !== "dismissed" && !t.merged_into_id);
    const h = heard(item.transcript_confidence);
    const p = pick(item.id);
    const options = data.asset_options || [];
    return (
      <div className="ptr-drawer">
        {open && <>
          <div className="ptr-f"><span className="lb">route to</span>
            <select value={p.seat ?? (item.seat || "unassigned")}
              ref={(el) => { if (el) seatPickers.current.set(item.id, el); else seatPickers.current.delete(item.id); }}
              className={needsSeat === item.id ? "needs-seat" : undefined}
              onChange={(e) => setPick(item.id, { seat: e.target.value })}>
              {SEATS.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <select value={p.kind ?? item.kind}
              onChange={(e) => setPick(item.id, { kind: e.target.value })}>
              {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <span className="ptr-why">applied when you promote</span>
          </div>
          <div className="ptr-f"><span className="lb">duplicate of</span>
            <select value={p.merge || ""} onChange={(e) => setPick(item.id, { merge: e.target.value })}>
              <option value="">nothing - it stands on its own</option>
              {targets.map((t) => <option key={t.id} value={t.id}>
                #{t.id} {String(t.text || "").slice(0, 60)}</option>)}
            </select>
            <button className="ptr-b" type="button" disabled={busy}
              onClick={() => void merge(item.id)}>merge</button>
          </div>
        </>}
        {options.length > 0 && (
          <div className="ptr-f"><span className="lb">about asset</span>
            <select value={p.asset ?? String(options[0].artifact_id)}
              onChange={(e) => setPick(item.id, { asset: e.target.value })}>
              {options.map((a) => <option key={a.artifact_id} value={a.artifact_id}>{a.logical_name}</option>)}
            </select>
            <button className="ptr-b" type="button" disabled={busy}
              onClick={() => void link(item.id)}>link</button>
            {(item.assets || []).length > 0 && (
              <span className="ptr-why">linked: {(item.assets || []).map((a) => a.logical_name).join(", ")}</span>
            )}
          </div>
        )}
        <div className="ptr-why">
          <b>#{item.id}</b> at {clock(item.t)} - {item.status}<br />
          {isScored
            ? <>classifier: <b>{cls.kind}</b> at {Math.round(Number(cls.confidence || 0) * 100)}% of
                the weight it scored ({Object.entries(scores).map(([k, v]) => `${k} ${v}`).join(", ")})</>
            : <>classifier: no rule matched, so the kind fell back to "note"</>}<br />
          {cls.seat && cls.seat !== "unassigned"
            ? <>router: <b>{cls.seat}</b></> : <>router: no seat matched</>}
          {item.transcript_confidence != null && <><br />
            whisper: {Number(item.transcript_confidence).toFixed(2)} log prob
            {h ? <> - <b>{h.word}</b></> : " - heard cleanly"}</>}
        </div>
        {item.work ? (
          <div className="ptr-work">
            <b>queue #{item.work.id}</b><span>{item.work.seat} - {item.work.status}</span>
            {item.work.result && <span className="res">{item.work.result}</span>}
            <button className="ptr-b" type="button" onClick={() => showWork(item.work!.id)}>show work</button>
          </div>
        ) : item.status === "promoted" ? (
          /* Nothing is being created. The seat sees this in its brief the next
             time it runs, and somebody still has to give it work to do. */
          <div className="ptr-work"><span>accepted for {item.seat || "a seat"} -
            it shows in that seat's brief. No work item is created by
            promoting; queue one when you want it acted on.</span></div>
        ) : null}
      </div>
    );
  }

  const row = (item: Item, settled?: boolean) => (
    <article key={item.id}
      className={`ptr-row${settled ? " settled" : ""}${cursor === item.id ? " cur" : ""}${flash === item.id ? " flash" : ""}`}
      id={`feedback-${item.id}`} data-id={item.id}>
      <button className="ptr-ev" type="button"
        title={`Play the recording from ${clock(item.t)}`}
        onClick={() => { setCursor(item.id); playAt(Number(item.t) || 0); }}>
        {item.frame_rel
          ? <img src={previewURL(item.frame_rel)} alt={`the frame captured at ${clock(item.t)}`} />
          : <span className="noimg">no frame</span>}
        <span className="t">{clock(item.t)}</span>
      </button>
      <div className="ptr-body">
        <p className="ptr-say">{item.text}</p>
        <div className="ptr-meta">{tags(item)}</div>
      </div>
      <div className="ptr-do">{rowActions(item)}</div>
      {drawer(item)}
    </article>
  );

  const rows = (list: Item[], settled?: boolean) => (
    <div className="ptr-rows">{list.map((i) => row(i, settled))}</div>
  );

  const fold = (k: string, label: string, why: string, list: Item[], settled?: boolean) =>
    list.length ? (
      <details key={k} className="ptr-fold" data-g={k} open={folds.get(k) === true}
        onToggle={(e) => setFold(k, (e.currentTarget as HTMLDetailsElement).open)}>
        <summary>{label}<span className="ptr-n">{list.length}</span>
          <span className="why">{why}</span>
          <span className="caret"><Icon name="select" size={11} /></span></summary>
        {rows(list, settled)}
      </details>
    ) : null;

  return (
    <div ref={host} id="pt-triage">
      <div className="ptr">
        <div className="ptr-hd"><Icon name="playtests" size={15} /><h3>Review feedback</h3>
          <span className={`ptr-n${todo ? "" : " good"}`}>{todo ? `${todo} to decide` : "all decided"}</span>
          <span className="ptr-keys"><kbd>j</kbd>/<kbd>k</kbd> move ·{" "}
            <kbd>p</kbd> promote · <kbd>x</kbd> bin ·{" "}
            <kbd>enter</kbd> refine · <kbd>space</kbd> play the moment</span></div>
        {!items.length ? (
          <div className="ptr-empty">Nothing was extracted from this session - no
            speech was transcribed and no notes were typed.</div>
        ) : <>
          {g.open.length ? (
            <div className="ptr-g"><div className="ptr-gh">To decide
              <span className="ptr-n">{g.open.length}</span></div>
              {openGroups
                ? openGroups.map((grp) => (
                  <div key={grp.kind} className="ptr-g">
                    <div className="ptr-gh">{KIND_LABEL[grp.kind] || grp.kind}
                      <span className="ptr-n">{grp.of.length}</span></div>
                    {rows(grp.of)}
                  </div>))
                : rows(g.open)}
            </div>
          ) : todo === 0 ? (
            <div className="ptr-empty">Everything in this session has been decided.</div>
          ) : null}
          {/* Bug 4. Named, counted, and one click from full weight - never
              filtered, because a real session can be nothing but these. */}
          {fold("quiet", "low signal",
            "nothing heard clearly, no seat, no classifier match, no telemetry", g.quiet)}
          {fold("praise", "praise", "nothing to action - worth reading", g.praise)}
          {/* NOT "filed as work". Promotion assigns the item to a seat and
              authors NO work item - a label that claims work exists is the
              reason somebody waits for agents that were never dispatched. */}
          {fold("filed", "accepted for a seat",
            "assigned to a seat, and waiting to be turned into work - promoting does not queue anything by itself",
            g.filed, true)}
          {fold("binned", "binned and merged", "kept in the record, out of the way", g.binned, true)}
        </>}
      </div>
    </div>
  );
}
