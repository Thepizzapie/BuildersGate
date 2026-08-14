import { useState } from "react";
import { Ti } from "../Ti";
import { mutate, toast } from "../../bridge";
import { Head, Nothing, Tag, ReadError } from "./prims";
import { useJSON, ago } from "./api";
import type { SeatBodyProps } from "./types";
import "./gameplay.css";

/* GAMEPLAY — the knob, the measurement, and the sentence a player said.
 *
 * The seat's brief: when feedback says "floaty", READ THE TELEMETRY NUMBERS
 * NEXT TO IT before touching a tunable, and randomness lives in ONE declared
 * seeded stream or nowhere. Both are layout decisions. Feedback is drawn with
 * its session's numbers on the same card, and the tunable table has a column
 * for the measurement whether or not one exists.
 *
 * THE MEASURED COLUMN IS REAL NOW, AND IT IS A JOIN, NOT A GUESS. `/api/tunables`
 * puts three things together that were all already recorded and never joined:
 * the tunable snapshot every iteration takes, the playtest sessions that ran
 * while that snapshot was in force, and the telemetry those sessions emitted.
 * So a row reads "0.35 — 3 sessions, 41 deaths / 0.4 — 1 session, 6 deaths".
 *
 * WHAT IT STILL REFUSES TO DRAW: a recommendation. Two playthroughs is not an
 * experiment, and a delta printed as a verdict is the invented number this
 * screen must never show — somebody would tune against it. The verdict column
 * is about EVIDENCE (`measured` / `one sample` / `not measured`), and the
 * history beneath it is the counts a human reads before turning the knob.
 *
 * ENFORCEMENT, NOT DISPLAY. Four things this panel used to render as neutral
 * facts are verdicts, and it now draws them as verdicts:
 *
 *   · ZERO TELEMETRY IS NOT A NUMBER. A session that emitted no events cannot
 *     support any statement about how the game felt, so "0 telemetry events"
 *     is drawn as the warning it is rather than as a stat beside the others.
 *     Every session in the reference project is 0, and the panel used to say so
 *     in the same grey as everything else.
 *   · MORE THAN ONE RNG STREAM IS A FAILURE. The doctrine is ONE declared
 *     seeded stream or none; a list that puts a green tick beside each of three
 *     streams is the UI approving what the seat forbids. And a stream with no
 *     seed is not a declaration, it is a name.
 *   · OTHER SEATS' FEEDBACK IS NOT THIS SEAT'S FEEDBACK. `playtest_item.seat`
 *     is a routed field the panel threw away, so 56 items — audio, art,
 *     unassigned — rendered here identically to the four that are gameplay's.
 *   · A KEY TWO ACTIONS CLAIM IS A BUG. The input map is drawn as a flat list,
 *     and the two rows that fight over Space are never adjacent.
 */

type Stream = { name?: string; seed?: number | string };
type Slot = {
  value: string;
  iterations: number[];
  sessions: { id: number; name: string; duration_s?: number; started_at?: string }[];
  events: Record<string, number>;
};
type Tunable = {
  key: string; file: string; name: string; current?: string | null;
  history: Slot[]; sessions: number; verdict: string;
};
type Gate = { gate: string; fails_with_reason?: string | null; detail_fields?: string[] };
type System = {
  name: string; source?: string; order_verified: boolean; actor_key?: string;
  landed?: string; opens_on?: string[]; terminals?: string[]; refusals?: string[];
  ladder: Gate[];
  measured: {
    session?: string | null; attempts: number; success_rate?: number | null;
    by_outcome: Record<string, number>; by_failed_gate: Record<string, number>;
    worst_gate?: string | null; order_verified: boolean; warning?: string | null;
  };
};
type Session = {
  id: number; name: string; status: string; duration_s?: number;
  items?: number; untriaged?: number; telemetry_events?: number; has_video?: boolean;
  started_at?: string; build_ref?: string; iteration_id?: number | null;
};
type Note = {
  id: number; session_id: number; session_name?: string; kind?: string; seat?: string;
  status?: string; text?: string; clock?: string; has_repro?: boolean;
  director_recommendation?: string; author?: string;
  typed?: boolean; from_chat?: boolean;
};
type Control = { action: string; keys?: string[]; buttons?: string[] };

/* The seat's own items plus the ones the router could not place. `unassigned`
   is deliberately included: an unrouted complaint is nobody's until somebody
   reads it, and the seat that owns the core loop is where most of them land. */
const MINE = (role: string) => (n: Note) => n.seat === role || n.seat === "unassigned";

export function Gameplay({ seat, active, tab }: SeatBodyProps) {
  /* BOTH ARE ENVELOPED AND readJSON UNWRAPS THE ENVELOPE — the fields land at
     the top level, not under `data`. Reading `.data.tunables` here produced a
     table that was permanently empty against an API returning 300 rows, which
     is indistinguishable from a project with no tunables. */
  const tun = useJSON<{ tunables?: Tunable[]; iterations?: number; sessions?: number }>(
    "/api/tunables", {}, 20000, active && tab === "tunables");
  const sys = useJSON<{ systems?: System[]; session?: { name?: string } | null;
                        events?: number; next?: Record<string, string> | null }>(
    "/api/systems", {}, 20000, active && tab === "systems");
  /* THE WORKSPACE DOC IS *NOT* ENVELOPED — `/api/workspace/{seat}/{key}` returns
     `{seat, key, data}` with no `ok`, so unwrap() leaves it alone and the doc
     really is under `.data`. It is the one read on this panel that is, which is
     why it says so here. `_version` rides inside the doc and a write has to
     hand it back or it clobbers whoever saved while this tab was open. */
  const rng = useJSON<{ data?: { streams?: Stream[]; _version?: string } }>(
    `/api/workspace/${seat.role}/randomness`, {}, 20000, active && tab === "tunables");
  /* Enveloped, and the page unwraps it — the three lists are top level. */
  const qa = useJSON<{ sessions?: Session[]; untriaged?: Note[]; needs_repro?: Note[];
                       counts?: Record<string, number>; note?: string }>(
    "/api/playtest/qa-queue", {}, 8000, active);
  const play = useJSON<{ built?: boolean; stale?: boolean; reason?: string;
                         newest_source?: string; controls?: Control[] }>(
    "/api/play/status", {}, 10000, active && tab === "input");

  /* Panel-local view state. None of it is a preference worth persisting; all of
     it is "which half of a list am I reading right now". */
  const [measuredOnly, setMeasuredOnly] = useState(false);
  const [allSeats, setAllSeats] = useState(false);
  const [name, setName] = useState("");
  const [seed, setSeed] = useState("");
  const [busy, setBusy] = useState(false);
  /* The optimistic copy of the randomness doc. usePoll's effect keys on the
     interval, not on the path, so a write is otherwise invisible for up to
     twenty seconds and the button looks broken. Held only until the poll comes
     back carrying the version we wrote. */
  const [wrote, setWrote] = useState<{ version: string; streams: Stream[] } | null>(null);

  const tunables = tun.tunables || [];
  const doc = rng.data || {};
  const streams = wrote && wrote.version !== String(doc._version || "")
    ? wrote.streams : (doc.streams || []);
  const sessions = qa.sessions || [];
  const untriaged = qa.untriaged || [];
  const needsRepro = qa.needs_repro || [];
  const sessionOf = (id: number) => sessions.find((s) => s.id === id);

  /* THE WRITE. `/api/workspace/{seat}/{key}` has always accepted a POST and no
     panel ever sent one, so the rule this seat is built around had no way to be
     complied with from the screen that enforces it. The stored `_version` goes
     back out with the body: a 409 means another tab saved first and the honest
     answer is to say so, not to merge. */
  async function saveStreams(next: Stream[], label: string) {
    setBusy(true);
    const r = await mutate<{ _version?: string }>(
      `/api/workspace/${seat.role}/randomness`,
      { body: { data: { ...doc, streams: next } }, quiet: true });
    setBusy(false);
    if (!r.ok) { toast(r.error || `could not ${label} the stream`, "bad"); return; }
    setWrote({ version: String(r.data?._version || ""), streams: next });
    toast(label === "declare" ? `declared ${next[next.length - 1]?.name}` : "undeclared", "ok");
  }

  /* SYSTEMS — the resolution ladders the game's own rules are made of, read
     from .bgate/causal_specs.json and folded over the latest session's
     telemetry. The ladder is the DESIGN and is worth reading before anyone has
     played; the counts beside it are what playing did to it. */
  if (tab === "systems") {
    const systems = sys.systems || [];
    const ran = sys.session;
    const events = sys.events ?? 0;
    /* The route hands back the telemetry contract's own next step when there is
       no spec. The component used to fetch that, type it, and then print a
       shorter paraphrase of it that omitted the one convention that matters
       most (a reason on every failure). The server's text is the text. */
    const next = sys.next || null;
    return (
      <div className="bgs-pad">
        <Head label="Systems"
              hint="the order your checks run in — a failure at gate N means 1..N-1 passed"
              right={ran
                ? <Tag tone={events ? "good" : "warn"}
                       title={events ? "" : "the session was ingested and emitted nothing — " +
                              "every count below is zero because nothing was recorded, " +
                              "not because nothing failed"}>
                    {ran.name} · {events} events
                  </Tag>
                : <Tag tone="off">no session ingested</Tag>} />
        <ReadError error={sys.__error} what="the system specs" />
        {!systems.length && !sys.__error && (
          <>
            <Nothing what="no system declared"
                     how={"a system is a resolution ladder in .bgate/causal_specs.json. " +
                          "Gate ORDER cannot be inferred from telemetry — it lives in your " +
                          "source, which is why the last step here is yours."} />
            {next && (
              <div className="bgg-next">
                {next.what && <div className="bgs-reasons"><div>{next.what}</div></div>}
                {Object.keys(next)
                  .filter((k) => /^\d/.test(k))
                  .sort()
                  .map((k, i) => (
                    <div className="step" key={k}>
                      <span className="n">{i + 1}</span>
                      <span>{next[k]}</span>
                    </div>
                  ))}
                {next.then && (
                  <div className="step"><span className="n">→</span><span>{next.then}</span></div>
                )}
              </div>
            )}
          </>
        )}
        {systems.map((s) => {
          const m = s.measured;
          const fails = Object.entries(m.by_failed_gate || {});
          return (
            <div className="bgs-card" key={s.name}>
              <div className="bgs-head">
                <span className="lb">{s.name}</span>
                <span className="sp" />
                {/* THE FLAG THAT CHANGES WHAT YOU MAY CONCLUDE. An unverified
                    ladder's failure counts are sound and its passes are
                    guesses, so it must never render like a confirmed one. */}
                {s.order_verified
                  ? <Tag tone="good">order verified</Tag>
                  : <Tag tone="warn" title={m.warning || ""}>order unverified</Tag>}
              </div>
              {/* WHAT "% LANDED" MEANS. The rate was printed without ever naming
                  the terminal it counts, so a reader had to open causal_specs
                  to find out what the number was a percentage OF. */}
              <div className="kv">
                <span>lands on</span><b className="wrap">{s.landed || "—"}</b>
              </div>
              <div className="kv">
                <span>opens on</span>
                <b className="wrap">{(s.opens_on || []).join(" · ") || "—"}</b>
              </div>
              <div className="kv">
                <span>actor</span><b className="wrap">{s.actor_key || "(single-actor)"}</b>
              </div>
              {s.source && (
                <div className="kv"><span>declared in</span><b className="wrap">{s.source}</b></div>
              )}
              <div className="stats">
                <span>{m.attempts} attempts</span>
                {m.success_rate != null && (
                  <span>{Math.round(m.success_rate * 100)}% reached {s.landed || "the terminal"}</span>
                )}
                {Object.entries(m.by_outcome || {}).map(([k, n]) => (
                  <span key={k}>{k} ×{n}</span>
                ))}
                {!m.attempts && (
                  <span className="warn">nothing ran this ladder — the counts below are empty,
                    not clean</span>
                )}
              </div>
              <div className="bgs-table" style={{ ["--cols" as string]: "28px 1fr 1fr 90px" }}>
                <div className="th">
                  <span>#</span><span>gate</span><span>fails with</span>
                  <span style={{ textAlign: "right" }}>failures</span>
                </div>
                {s.ladder.map((g, i) => {
                  const n = m.by_failed_gate?.[g.gate] || 0;
                  return (
                    <div className={`tr${g.gate === m.worst_gate && n ? " bad" : ""}`} key={g.gate}>
                      <span className="mono dim">{i + 1}</span>
                      <span className="mono"
                            title={(g.detail_fields || []).length
                              ? `carries ${(g.detail_fields || []).join(", ")}` : ""}>
                        {s.order_verified ? g.gate : `~${g.gate}`}
                      </span>
                      <span className="mono dim">{g.fails_with_reason || "—"}</span>
                      <span className="right">
                        {n ? <Tag tone={g.gate === m.worst_gate ? "bad" : "warn"}>{n}</Tag>
                           : <span className="mono dim">0</span>}
                      </span>
                    </div>
                  );
                })}
              </div>
              {!fails.length && !!m.attempts && (
                <div className="rec"><Ti name="check" size={12} /> nothing failed a gate this session</div>
              )}
              {m.warning && <div className="bgs-reasons"><div>{m.warning}</div></div>}
            </div>
          );
        })}
      </div>
    );
  }

  if (tab === "input") {
    const controls = play.controls || [];
    /* WHO ELSE CLAIMS THIS KEY. A flat list cannot show a collision, because
       the two actions that collide are never next to each other — in the
       reference project Space is `gear_take_all` on row 4 and `combat_end_turn`
       on row 13, and nothing on the screen said so. Contexts may make a shared
       key deliberate, which is why this warns and does not fail. */
    const claims = new Map<string, string[]>();
    for (const c of controls) {
      for (const k of [...(c.keys || []), ...(c.buttons || [])]) {
        claims.set(k, [...(claims.get(k) || []), c.action]);
      }
    }
    const contested = [...claims.entries()].filter(([, who]) => who.length > 1);
    const clashesOf = (c: Control) =>
      [...(c.keys || []), ...(c.buttons || [])].filter((k) => (claims.get(k) || []).length > 1);
    const unbound = controls.filter((c) => !(c.keys || []).length && !(c.buttons || []).length);
    return (
      <div className="bgs-pad">
        <Head label="Input map" hint="what the build actually binds — read out of the project, not a design doc"
              right={play.built
                ? <Tag tone={play.stale ? "warn" : "good"}>{play.stale ? "build is stale" : "build is current"}</Tag>
                : <Tag tone="bad">no build</Tag>} />
        <ReadError error={play.__error} what="the build status" />
        {play.stale && play.reason && <div className="bgs-reasons"><div>{play.reason}</div></div>}
        {!controls.length && !play.__error && (
          <Nothing what="no actions bound"
                   how="the input map comes from the game's project.godot; an unbound action is a control nobody can press" />
        )}
        {!!contested.length && (
          <div className="bgg-clash">
            <div className="hd">
              {contested.length} binding{contested.length === 1 ? "" : "s"} claimed twice
            </div>
            {contested.map(([k, who]) => (
              <div className="k" key={k}>{k} → <b>{who.join(", ")}</b></div>
            ))}
          </div>
        )}
        {!!unbound.length && (
          <div className="bgs-reasons"><div>
            {unbound.length} action{unbound.length === 1 ? " has" : "s have"} no key and no
            button: <b>{unbound.map((c) => c.action).join(", ")}</b>. Nobody can press it.
          </div></div>
        )}
        {!!controls.length && (
          /* THE HEADER ROW WAS MISSING and two of the three columns were bare
             dim monospace — a reader had no way to know which column was the
             keyboard and which the pad, other than that one of them said
             "pad". */
          <div className="bgs-table" style={{ ["--cols" as string]: "1fr 1fr 1fr" }}>
            <div className="th"><span>action</span><span>keyboard</span><span>gamepad</span></div>
            {controls.map((c) => {
              const clash = clashesOf(c);
              const dead = !(c.keys || []).length && !(c.buttons || []).length;
              return (
                <div className={`tr${dead ? " bad" : ""}`} key={c.action}>
                  <span className="mono">
                    {c.action}
                    {!!clash.length && (
                      <Tag tone="warn" title={clash.map(
                        (k) => `${k}: ${(claims.get(k) || []).join(", ")}`).join(" · ")}>
                        shared
                      </Tag>
                    )}
                    {dead && <Tag tone="bad">unbound</Tag>}
                  </span>
                  <span className="mono dim">{(c.keys || []).join(" · ") || "—"}</span>
                  <span className="mono dim">{(c.buttons || []).join(" · ") || "—"}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  if (tab === "feedback") {
    /* WHOSE FEEDBACK THIS IS. Every item carries the seat feedback.py routed it
       to, and the panel used to drop that field and draw all 56 — audio, art,
       narrative — as if they were this seat's four. The default is now this
       seat plus `unassigned`, and the toggle says how many it is hiding rather
       than hiding the fact that it hides them. */
    const mine = MINE(seat.role);
    const shown = allSeats ? untriaged : untriaged.filter(mine);
    const shownRepro = allSeats ? needsRepro : needsRepro.filter(mine);
    const hidden = (untriaged.length - shown.length) + (needsRepro.length - shownRepro.length);

    /* THE NUMBERS THAT MAKE THE QUOTE READABLE. A report and its session's
       figures on one card is this seat's rule; a session with NO telemetry is
       the case the rule cares most about, because that is the report nobody can
       check. Every session in the reference project is at zero. */
    const numbers = (n: Note) => {
      const s = sessionOf(n.session_id);
      if (!s) {
        return (
          <span className="bad" title={`session ${n.session_id} is outside the queue's window`}>
            {n.session_name || `session ${n.session_id}`} — not in the queue, no numbers
          </span>
        );
      }
      return (
        <>
          {s.telemetry_events
            ? <span>{s.telemetry_events} telemetry events</span>
            : <span className="bad" title={
                "the session recorded no events at all, so there is no measurement to " +
                "read beside this — do not turn a knob on the strength of it"}>
                no telemetry
              </span>}
          {s.duration_s != null && <span>{Math.round(s.duration_s)}s session</span>}
          {s.items != null && <span>{s.items} notes this session</span>}
          {s.build_ref && <span title="the build this was said about">{s.build_ref.slice(0, 12)}</span>}
        </>
      );
    };

    const card = (n: Note, promoted?: boolean) => (
      <div className="bgs-card" key={n.id}>
        <div className="q">“{n.text}”</div>
        <div className="stats">
          {n.kind && <span>{n.kind}</span>}
          {n.seat && n.seat !== seat.role && <span className="seat">routed to {n.seat}</span>}
          {n.clock && <span>at {n.clock}</span>}
          {numbers(n)}
          {/* HOW THE WORDS GOT HERE CHANGES HOW LITERALLY TO READ THEM. Whisper
              guesses at quiet audio; a viewer in chat watched a compressed
              stream, not the game, so "it stutters" may be the encoder. Both
              flags are on the wire and neither was drawn. */}
          {n.typed ? <span>typed</span>
                   : <span className="guess" title="whisper's transcription, not a typed sentence">
                       transcribed
                     </span>}
          {n.from_chat && (
            <span className="guess" title="said by a viewer watching a compressed stream, not the game">
              from chat
            </span>
          )}
          <span className={n.has_repro ? "ok" : promoted ? "bad" : "warn"}>
            {n.has_repro ? "has repro" : "no repro"}
          </span>
          {n.author && <span>{n.author}</span>}
        </div>
        {n.director_recommendation && (
          <div className="rec"><Ti name="gavel" size={12} /> {n.director_recommendation}</div>
        )}
      </div>
    );

    return (
      <div className="bgs-pad">
        <Head label="Feedback, with its numbers attached"
              hint="a report without its session's figures is an opinion"
              right={<Tag tone={shown.length ? "warn" : "off"}>
                {shown.length} untriaged
              </Tag>} />
        <ReadError error={qa.__error} what="the playtest queue" />
        <div className="bgg-bar">
          <button className={`bgg-toggle${allSeats ? " on" : ""}`}
                  onClick={() => setAllSeats(!allSeats)}
                  title="feedback.py routes every item to a seat; this seat's queue is its own items plus the ones it could not place">
            <Ti name="filter" size={12} />
            {allSeats ? "every seat" : "this seat + unassigned"}
            {!allSeats && !!hidden && <b>· {hidden} hidden</b>}
          </button>
        </div>
        {!shown.length && !shownRepro.length && !qa.__error && (
          <Nothing what={untriaged.length ? `nothing routed to ${seat.role}` : "nothing untriaged"}
                   how={untriaged.length
                     ? `${untriaged.length} untriaged items exist and feedback.py routed all of ` +
                       `them to other seats — switch the filter to read them`
                     : "playtest_start records a session; notes typed during it land here with the session's telemetry beside them"} />
        )}
        {shown.map((n) => card(n))}
        {!!shownRepro.length && (
          <>
            {/* A PROMOTED BUG AND A RAW UTTERANCE ARE NOT THE SAME ITEM. They
                were concatenated into one list, so something a human already
                judged worth fixing rendered identically to an offhand remark
                nobody has read. */}
            <Head label="Promoted, and still nobody wrote the repro"
                  hint="somebody decided this was real; the fix seat is guessing until the steps exist" />
            {shownRepro.map((n) => card(n, true))}
          </>
        )}
        {!!sessions.length && (
          <>
            <Head label="Sessions" hint="what was measured, and when"
                  right={sessions.every((s) => !s.telemetry_events)
                    ? <Tag tone="bad" title="not one recorded session emitted a single event — the telemetry contract is not wired, so every measurement on this screen is missing by construction">
                        no session has telemetry
                      </Tag>
                    : undefined} />
            <div className="bgs-table"
                 style={{ ["--cols" as string]: "1fr 80px 70px 100px 1fr 80px" }}>
              <div className="th">
                <span>session</span><span>status</span><span>duration</span>
                <span>telemetry</span><span>build</span>
                <span style={{ textAlign: "right" }}>untriaged</span>
              </div>
              {sessions.map((s) => (
                <div className={`tr${s.status === "failed" ? " bad" : ""}`} key={s.id}>
                  <span className="mono" title={s.started_at || ""}>{s.name}</span>
                  <span className="mono dim">{s.status}</span>
                  <span className="mono dim">{s.duration_s != null ? `${Math.round(s.duration_s)}s` : "—"}</span>
                  <span className="mono dim">
                    {s.telemetry_events
                      ? s.telemetry_events
                      : <Tag tone="warn" title="no events — nothing about this session is measurable">none</Tag>}
                  </span>
                  <span className="mono dim" title={s.build_ref || ""}>
                    {s.build_ref ? s.build_ref.slice(0, 14) : "—"}
                  </span>
                  <span className="right">
                    {s.untriaged ? <Tag tone="warn">{s.untriaged}</Tag> : <span className="mono dim">0</span>}
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
        {qa.note && <div className="bgs-reasons"><div>{qa.note}</div></div>}
      </div>
    );
  }

  /* TUNABLES. `measured_only` is a filter the route has always accepted and no
     caller ever sent — on a project with three hundred exported constants and
     six sessions, the unfiltered table is a wall nobody reads to the end of.
     It is OFF by default on purpose: "nobody has played the game at this
     setting" is the answer most of the time, and hiding it would make the panel
     look better than the evidence. The filter runs here rather than on the
     wire because usePoll keys its effect on the interval, not the path, so a
     changed query string would not take effect for twenty seconds. */
  const shown = measuredOnly ? tunables.filter((t) => t.sessions) : tunables;
  const measuredCount = tunables.filter((t) => t.sessions).length;
  return (
    <div className="bgs-pad">
      <Head label="Tunables"
            hint="the measured number sits next to the knob — read it before you turn it"
            right={tunables.length
              ? <Tag tone={measuredCount ? "warn" : "bad"}
                     title="how many of this project's captured knobs anyone has actually played the game at">
                  {measuredCount} of {tunables.length} played
                </Tag>
              : undefined} />
      <ReadError error={tun.__error} what="the tunable history" />
      {!tunables.length && !tun.__error && (
        <Nothing what="no tunables captured"
                 how={`the values are read out of the game's own scripts and ` +
                      `.bgate/tunables.json, and snapshotted every time an ` +
                      `iteration opens. Open one (iteration_status) and the ` +
                      `knobs appear here with whatever the playtests under it ` +
                      `measured.`} />
      )}
      {!!tunables.length && (
        <div className="bgg-bar">
          <button className={`bgg-toggle${measuredOnly ? " on" : ""}`}
                  onClick={() => setMeasuredOnly(!measuredOnly)}
                  title="drop the knobs nobody has played the game at — they are still the majority and still the answer">
            <Ti name="filter" size={12} />
            {measuredOnly ? "played only" : "every captured knob"}
            {measuredOnly && <b>· {tunables.length - measuredCount} hidden</b>}
          </button>
        </div>
      )}
      {!!shown.length && (
        <div className="bgs-table" style={{ ["--cols" as string]: "1.1fr 90px 1fr 130px" }}>
          <div className="th">
            <span>tunable</span><span>value</span><span>what playtests measured</span>
            <span style={{ textAlign: "right" }}>verdict</span>
          </div>
          {shown.map((t) => (
            <div className={`tr${t.verdict === "not measured" ? " bad" : ""}`} key={t.key}>
              <span className="mono" title={t.file}>{t.name}</span>
              <span className="mono">{t.current ?? <em className="dim">gone</em>}</span>
              {/* NOT MEASURED IS A RESULT. A blank cell reads as "fine".
                  Every value it has ever held, with the sessions actually
                  played at it — no delta, no recommendation.
                  THE VALUES NOBODY PLAYED STAY ON THE ROW. They used to be
                  filtered out, which deleted the most useful sentence the join
                  can produce: "this was 0.5 for six iterations and not one
                  session ran at it." */}
              <span className="mono dim">
                {t.sessions
                  ? t.history.map((h) => (
                      <span className="bgs-slot" key={h.value}>
                        <b className={h.value === t.current ? "now" : ""}>{h.value}</b>
                        {" "}
                        {h.sessions.length
                          ? `${h.sessions.length} session${h.sessions.length === 1 ? "" : "s"}`
                          : `never played · held ${h.iterations.length} iteration${h.iterations.length === 1 ? "" : "s"}`}
                        {h.sessions.length
                          ? (Object.entries(h.events).length
                              ? " · " + Object.entries(h.events)
                                  .sort((a, b) => b[1] - a[1]).slice(0, 3)
                                  .map(([k, n]) => `${n} ${k}`).join(", ")
                              : " · no telemetry")
                          : ""}
                      </span>
                    ))
                  : <em className="dim">not measured</em>}
              </span>
              <span className="right">
                <Tag tone={t.verdict === "measured" ? "good"
                         : t.verdict === "one sample" ? "warn" : "off"}
                     title={t.verdict === "measured"
                       ? "played at more than one value — the counts are yours to read, not a recommendation"
                       : t.verdict === "one sample"
                       ? "only ever played at one value, so nothing here compares"
                       : "nobody has played the game at this setting"}>
                  {t.verdict}
                </Tag>
              </span>
            </div>
          ))}
        </div>
      )}
      {!!tunables.length && (
        <div className="bgs-reasons"><div>
          {tun.iterations ?? 0} iteration snapshots · {tun.sessions ?? 0} recorded
          sessions. Counts only — nothing here recommends a value, because two
          playthroughs is not an experiment.
        </div></div>
      )}

      <div className="bgs-two wide">
        <div>
          {/* RANDOMNESS. The rule is ONE declared seeded stream or none, and
              this panel used to be a read against a document that nothing in
              the codebase has ever written — so it said "no stream declared"
              for ever and pointed at an API path as if that were an
              instruction. The POST has always existed. */}
          <Head label="Randomness" hint="one declared seeded stream, or none"
                right={streams.length === 1
                  ? <Tag tone="good">declared</Tag>
                  : streams.length > 1
                  ? <Tag tone="bad" title="the rule is one stream; the extras are undeclared randomness with a name on them">
                      {streams.length} streams
                    </Tag>
                  : <Tag tone="bad">undeclared</Tag>} />
          <ReadError error={rng.__error} what="the randomness declaration" />
          {!streams.length && !rng.__error && (
            <Nothing what="no stream declared — this is a load failure, not a style choice"
                     how={"anything the game rolls without a seeded stream makes a playtest " +
                          "that cannot be replayed, so every number on this screen becomes " +
                          "unfalsifiable. Name the stream and its seed."} />
          )}
          {streams.length > 1 && (
            <div className="bgs-reasons"><div>
              {streams.length} streams are declared and the rule is one. Two seeded
              streams reproduce two things independently, which is the same as
              reproducing neither.
            </div></div>
          )}
          {streams.map((s, i) => {
            const seeded = s.seed !== undefined && s.seed !== null && String(s.seed) !== "";
            return (
              <div className="bgg-stream" key={s.name || i}>
                <Ti name={seeded && streams.length === 1 ? "check" : "alert-triangle"} size={14}
                    color={seeded && streams.length === 1 ? "var(--good)" : "var(--bad)"} />
                <span className="nm">{s.name || <em>unnamed</em>}</span>
                <span className="sp" />
                <span className="sd">
                  {seeded
                    ? `seed ${String(s.seed)}`
                    : "NO SEED — a stream without one replays differently every run"}
                </span>
                <button className="x" disabled={busy} title="undeclare this stream"
                        onClick={() => saveStreams(streams.filter((_, j) => j !== i), "undeclare")}>
                  <Ti name="trash" size={13} />
                </button>
              </div>
            );
          })}
          <div className="bgg-form">
            <input value={name} placeholder="stream name" spellCheck={false}
                   onChange={(e) => setName(e.target.value)} />
            <input value={seed} placeholder="seed" inputMode="numeric" spellCheck={false}
                   onChange={(e) => setSeed(e.target.value)} />
            <button className="bgs-btn go" disabled={busy || !name.trim() || !seed.trim()}
                    onClick={async () => {
                      await saveStreams(
                        [...streams, { name: name.trim(), seed: /^-?\d+$/.test(seed.trim())
                          ? Number(seed.trim()) : seed.trim() }],
                        "declare");
                      setName(""); setSeed("");
                    }}>
              {busy ? "saving…" : "declare"}
            </button>
            {/* THE SEED IS NOT OPTIONAL, so the button will not fire without
                one. A named stream with no seed is a label on the same
                unreplayable randomness. */}
            <span className="why">
              both fields are required — a stream with no seed is not a declaration
            </span>
          </div>
        </div>
        <div>
          <Head label="Latest sessions" hint="what the numbers above would come from" />
          {!sessions.length && !qa.__error && (
            <Nothing what="no playtest recorded" how="playtest_start records one, with telemetry" />
          )}
          {sessions.slice(0, 5).map((s) => (
            <div className="bgs-lockrow" key={s.id}>
              <Ti name="device-gamepad-2" size={13} color="var(--c-gameplay)" />
              <span className="p">{s.name}</span>
              <span className="by">
                {s.telemetry_events ? `${s.telemetry_events} ev` : "no telemetry"} · {ago(s.started_at)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
