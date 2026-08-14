import { useCallback, useState } from "react";
import { Ti } from "../Ti";
import { previewURL, lightbox, mutate, toast } from "../../bridge";
import { Head, Nothing, Tag, Banner, ReadError } from "./prims";
import { useJSON, ago } from "./api";
import { useSeatChips } from "./chips";
import type { SeatBodyProps } from "./types";
import "./cineqa.css";

/* CINEMATIC — the only seat where reading the screen wrong costs money.
 *
 * BOARD IT, THEN WRITE THE SHOT LIST, THEN BUY A FRAME. The estimate sits above
 * the shot list for that reason: the board costs a fraction of a cent and every
 * shot is a separate purchase, so the price of the whole sequence has to be
 * legible before the first generate button is anywhere near the pointer.
 *
 * THE ESTIMATE IS AN UPPER BOUND AND SOMETIMES HAS NO DOLLAR FIGURE AT ALL.
 * kie publishes no per-model price; the backend says so with `known:false`, a
 * credit count, and a note. Printing "$14.60" over that would be inventing the
 * one number on this screen a person spends against, so when usd is null the
 * banner shows credits and says no dollar rate is published.
 *
 * ANCHORS, NOT CHAINS. Each shot's `first_frame` is the approved still it
 * conditions on. A shot with no anchor is drawn as a warning: it will be
 * generated off nothing, or off the previous shot's output, which is how a
 * sequence drifts one shot at a time until the character has a different face.
 *
 * "TRANSCODED" IS MEASURED, NOT ASSUMED. cinematic's own `playable` answers from
 * the file extension, which is the one thing that cannot see the failure this
 * project has actually shipped — a libtheora build writing .ogv files Godot
 * opens and cannot decode. /api/cinematic/theora runs ffprobe over the delivered
 * files, so this screen reports the container and the video codec that are
 * really in the bytes, and reports "unmeasured" where there is no ffprobe rather
 * than drawing a zero.
 *
 * A SHOT TAKE IS NOT A DELIVERY, and confusing the two is what made this screen
 * a wall of red over a sequence that shipped correctly. cinematic.keep() is
 * explicit: a CUTSCENE is transcoded and installed because that is the asset the
 * game plays; a SHOT is approved and DELIBERATELY stays in .bgate_out as the
 * provider's own h264 .mp4, because assemble() reads the mp4 and installing a
 * per-shot .ogv was tens of megabytes nothing referenced. So "not theora", "not
 * installed" and "unwatched" are claims about the CUT. Applied to a shot take
 * they are three red badges for the correct state, and the survey's own
 * `untranscoded`/`unwatched` totals count those takes — which is why the numbers
 * on this screen are computed from the SHIPPED rows here rather than taken from
 * the envelope. See the cross-file note at the bottom of this file.
 */

type Sequence = {
  id: number; name: string; logline?: string; status?: string; model?: string;
  shot_count?: number; kept?: number; runtime_s?: number;
  aspect_ratio?: string; resolution?: string; style_label?: string;
  assembled_artifact_id?: number | null;
};
type ShotArtifact = {
  id?: number; logical_name?: string; revision?: number; path?: string;
  status?: string; installed?: boolean;
};
type Shot = {
  id: number; idx: number; slug?: string; action?: string; camera?: string;
  duration?: number; first_frame?: string; last_frame?: string;
  status?: string; note?: string; vo?: string; dialogue?: string;
  transition?: string; transition_s?: number; artifact_id?: number;
  refs?: string[]; shot_size?: string; location_text?: string;
  artifact?: ShotArtifact | null;
};
type PerShot = {
  idx: number; usd?: number | null; credits?: number | null; known?: boolean;
  seconds?: number;
};
type Estimate = {
  sequence?: string; model?: string; shots?: number; runtime_s?: number;
  credits?: number; usd?: number | null; known?: boolean;
  unknown_shots?: number[]; note?: string; basis?: string; per_shot?: PerShot[];
};
/** One delivered file as ffprobe found it — not as its name suggests. */
type Measured = {
  artifact_id: number; logical_name?: string; sequence?: string; kind?: string;
  target?: string; godot_res?: string; installed?: boolean;
  exists?: boolean; measured?: boolean; demuxed?: boolean; theora?: boolean;
  container?: string; video_codec?: string; audio_codec?: string;
  duration_s?: number | null; bytes?: number; why?: string;
  watched_at?: string; watched_by?: string;
};
type Survey = {
  probe?: boolean; why?: string; rows?: Measured[]; measured?: number;
  untranscoded?: number | null; unwatched?: number; cuts?: number;
};
/** A take as the artifact register reports it. `kind` is the whole argument of
 *  the cut tab: "cutscene" is the delivery, "shot" is an intermediate. */
type Kept = {
  artifact_id: number; logical_name?: string; path?: string; installed?: boolean;
  installed_path?: string; playable?: boolean; install_stale?: boolean;
  install_missing?: boolean; godot_res?: string; duration_s?: number | null;
  kind?: string; sequence?: string; revision?: number; status?: string;
  shot_idx?: number | null; created_at?: string; prompt?: string;
};

const SHOT_TONE = (s?: string) =>
  s === "kept" ? "good" : s === "generating" ? "warn" : s === "failed" ? "bad" : "off";

/** Does this take belong to the sequence the picker is on? The candidates
 *  endpoint takes `logical_name` and applies it to `candidates` ONLY — `kept`
 *  is always the whole project — so the tab filters here or the picker is
 *  decoration over a list that never changes. Cutscene rows carry an empty
 *  `sequence` and the sequence's name as their logical name; shot rows carry
 *  the sequence. */
const OWNED_BY = (k: Kept, name: string) =>
  !name || k.sequence === name || k.logical_name === name ||
  String(k.logical_name || "").startsWith(`${name}_`);

export function Cinematic({ seat, active, tab }: SeatBodyProps) {
  const [pick, setPick] = useState<string | null>(null);
  const [watching, setWatching] = useState(false);

  /* Every cinematic endpoint is enveloped and the page unwraps it, so the
     bodies below are the payloads themselves, not {data: …}. */
  /* THE BOARD LISTING IS AN INDEX, NOT A BOARD. It carries a name and a count;
     the frames — with their images, beats, camera notes and durations — are
     behind /api/storyboard/board/{name}, which this panel never asked for. So
     the tab drew a one-line row per board and called itself a storyboard. */
  /* `status` IS ON EVERY FRAME AND THE TAB WAS DROPPING IT. A cut frame is a
     beat somebody deleted; the endpoint keeps returning it (storyboard_frame_cut
     marks, it does not delete) and drawing it beside the live ones made a
     six-frame board out of a five-frame one — and made "6 of 6 drawn" a
     completion claim over a board with a hole in it. */
  type Frame = {
    id: number; idx: number; slug?: string; beat?: string; action?: string;
    camera?: string; dialogue?: string; duration?: number; status?: string;
    image_path?: string; source?: string; artifact_id?: number;
    refs?: string[]; note?: string;
  };
  /* `frames` MEANS TWO THINGS AND THEY ARE TWO TYPES. The index reports a
     COUNT; the single board reports the ARRAY. Same key, and modelling them as
     one type is how the count ends up rendered as an array's length or the
     other way round. */
  type BoardRow = {
    name: string; frames?: number; updated_at?: string; status?: string;
    frame_status?: Record<string, number>;
  };
  /* `ready` IS AN OBJECT, NOT A BOOLEAN — {promotable, blockers, live,
     approved}. It was typed here as a boolean, which is the shape a `ready &&`
     test would have quietly passed on forever while the blockers (the only
     thing that says WHY a board cannot become a shot list) went unread. */
  type Ready = {
    promotable?: boolean; blockers?: string[]; live?: number; approved?: number;
  };
  type Board = {
    id?: number; name?: string; updated_at?: string; sequence_id?: number | null;
    premise?: string; logline?: string; style?: string; status?: string;
    style_note?: string; cast_refs?: string[];
    aspect_ratio?: string; ready?: Ready; frames?: Frame[];
  };
  const boards = useJSON<{ boards?: BoardRow[] }>(
    "/api/storyboard/boards", { boards: [] }, 20000, active && tab === "storyboard");
  const [boardPick, setBoardPick] = useState<string | null>(null);
  const boardList = boards.boards || [];
  const boardName = boardPick && boardList.some((b) => b.name === boardPick)
    ? boardPick : boardList[0]?.name || null;
  const board = useJSON<Board>(
    boardName ? `/api/storyboard/board/${encodeURIComponent(boardName)}` : null,
    {}, 20000, active && tab === "storyboard");
  const list = useJSON<{ sequences?: Sequence[] }>(
    "/api/cinematic/sequences", {}, 15000, active);
  const sequences = list.sequences || [];
  const name = pick && sequences.some((s) => s.name === pick) ? pick : sequences[0]?.name || null;
  const q = name ? `?name=${encodeURIComponent(name)}` : "";

  const one = useJSON<{ sequence?: Sequence & { shots?: Shot[] } }>(
    name ? `/api/cinematic/sequences${q}` : null, {}, 10000, active);
  const est = useJSON<Estimate>(
    name ? `/api/cinematic/estimate${q}` : null, {}, 30000, active);
  /* NOT `?sequence=`. The endpoint's parameter is `logical_name` and it is
     applied to `candidates` only — `kept` is `_cine.kept(project)` with no name
     argument at all, so it is the whole project on every read. The old query
     string was silently ignored and the cut tab showed every sequence's takes
     under whichever chip was lit: a picker that changed nothing. The filtering
     is done here, against `kind`/`sequence`/`logical_name`. */
  const cand = useJSON<{ kept?: Kept[]; candidates?: Kept[] }>(
    "/api/cinematic/candidates", {}, 15000, active && tab === "cut");

  /* MEASURED DELIVERY, and it is read on every tab because two of the three
     header chips are computed from it. ffprobe is a process per delivered file,
     so this is the slowest poll on the seat and runs at a minute. */
  const film = useJSON<Survey>("/api/cinematic/theora", { rows: [] }, 60000, active);
  /* MONEY ALREADY SPENT THAT NOBODY COLLECTED. cinematic_recover downloads a
     generation the provider has already finished and charged for; a shot left
     sitting on one is a paid frame that will be paid for twice the moment
     somebody presses generate again. Nothing on this seat was reading it. */
  const stuckq = useJSON<{ recoverable?: number; stale?: number; shots?: {
    idx?: number; slug?: string; sequence?: string; state?: string;
    recoverable?: boolean; note?: string; age_s?: number;
  }[] }>("/api/cinematic/stuck", { shots: [] }, 45000, active);
  /* The model's own seconds ceiling — the denominator of the duration bar. A
     bar drawn against the longest shot in the list would move when the list
     changes and would say nothing about what the provider will accept. */
  const opts = useJSON<{ models?: Record<string, {
    options?: Record<string, unknown>; exclusive?: string[][][];
  }> }>("/api/cinematic/options", {}, 120000, active);

  const seq = one.sequence;
  const shots = seq?.shots || [];
  const e = est.sequence ? est : null;
  const rows = film.rows || [];
  const modelName = String(est.model || seq?.model || "");
  const model = opts.models?.[modelName];
  const ceiling = (() => {
    /* `seconds` IS A RANGE, NOT AN ENUMERATION — kie reports seedance-2's as
       [4,15], i.e. min..max, and the shape band beside it is a genuine list of
       choices. max() happens to give the ceiling either way, but only because
       the range is sorted; reading it as a set of allowed values is how a
       [4,15] becomes "you may pick 4 or 15". */
    const band = model?.options?.seconds;
    const top = Array.isArray(band) ? Math.max(...band.map(Number)) : NaN;
    return Number.isFinite(top) && top > 0 ? top : 0;
  })();
  /* WHICH INTENTS THIS MODEL REFUSES TOGETHER. seedance-2 declares
     [[["first_frame","last_frame"],["refs"]]] — an anchored shot may not also
     carry character reference images. A shot row holding both is a shot whose
     refs the provider call will drop, and the seat that anchors every shot on an
     approved still is exactly the seat that hits it. */
  const anchorExcludesRefs = (model?.exclusive || []).some(
    (group) => group.some((side) => side.includes("first_frame")) &&
               group.some((side) => side.includes("refs")));
  const perShot = new Map((e?.per_shot || []).map((p) => [Number(p.idx), p]));

  /* ── what actually SHIPPED, computed here rather than taken from the survey ──
     cinecheck.survey counts `untranscoded` over every measured row and
     `unwatched` over every row of kind "cutscene". On the live project that
     makes four un-installed provider .mp4 shot takes — the correct, intended
     state — into "5 untranscoded", and a superseded r1 cut sitting in
     .bgate_out into a permanent "nobody has watched the assembled cut" beside
     an installed r2 that was watched today. Both counts are red forever on a
     project that shipped correctly, which is a smoke alarm nobody will look at
     twice. The claim this seat makes is about the file the ENGINE LOADS. */
  const shipped = rows.filter((r) => r.installed);
  const shippedBad = shipped.filter((r) => r.measured && r.exists && !r.theora);
  const shippedGhost = rows.filter((r) => r.godot_res && !r.exists);
  /* The cut this seat is judged on: the INSTALLED assembled artifact, then any
     assembled artifact, then whatever there is. */
  const cut = rows.find((r) => r.kind === "cutscene" && r.installed) ||
              rows.find((r) => r.kind === "cutscene") || rows[0] || null;
  const cutUnwatched = !!cut && !cut.watched_at;

  const [busy, setBusy] = useState(0);
  const act = useCallback(async (
    path: string, body: Record<string, unknown>, ok: string,
  ) => {
    setBusy(Number(body.artifact_id) || -1);
    const r = await mutate(path, { body, quiet: true });
    setBusy(0);
    if (r.ok) toast(ok, "good");
    else toast(r.error || `${path} failed`, "bad");
  }, []);

  /* THE HEADER'S THREE FIGURES, and every one of them is a read. The price is
     the estimate's own total (or its credit count when the provider publishes
     no dollar rate); the transcode count is ffprobe's, and is simply absent on
     a machine that has no ffprobe rather than being drawn as zero. */
  useSeatChips(seat.role, [
    ...(e && (typeof e.usd === "number" || e.credits) ? [{
      icon: "coin",
      label: typeof e.usd === "number"
        ? `$${e.usd.toFixed(2)} to render`
        : `${e.credits} credits to render`,
      color: "var(--c-cinematic)",
      title: `${e.shots ?? 0} shot(s) of ${e.sequence} on ${e.model}` +
             (e.known === false ? " — upper bound, no published dollar rate" : ""),
    }] : []),
    ...(film.probe && shipped.length ? [{
      icon: shippedBad.length ? "alert-triangle" : "circle-check",
      label: `${shippedBad.length} of ${shipped.length} shipped not theora`,
      color: shippedBad.length ? "var(--warn)" : "var(--good)",
      title: `ffprobe measured the ${shipped.length} file(s) installed into the engine. ` +
             `Un-installed shot takes are the provider's own .mp4 by design and are not counted here.`,
    }] : []),
    ...(cut ? [{
      icon: cutUnwatched ? "eye-off" : "eye",
      label: cutUnwatched ? "cut unwatched" : "cut watched",
      color: cutUnwatched ? "var(--bad)" : "var(--good)",
      title: cutUnwatched
        ? "nothing ships until a human has watched the assembled cut"
        : `last watched ${ago(cut.watched_at)} ago${cut.watched_by ? ` by ${cut.watched_by}` : ""}`,
    }] : []),
    ...(stuckq.recoverable ? [{
      icon: "download",
      label: `${stuckq.recoverable} paid, uncollected`,
      color: "var(--bad)",
      title: "the provider finished and charged for these shots and nobody downloaded them — " +
             "pressing generate again pays for the same frame twice",
    }] : []),
  ]);

  async function watch() {
    if (!cut) return;
    const rel = cut.target || "";
    setWatching(true);
    /* OPENING IT IS THE EVENT. There is no other observable moment at which a
       human can be said to have watched this, so the record is of the open and
       the actor is stored with it — see cinecheck.mark_watched.

       NOT /api/preview. That serves the delivered bytes, and the delivered
       bytes are Ogg Theora, which no current browser decodes — Chrome dropped
       Theora in M123. This button opened a black player stuck at 0:00 over a
       cut somebody had already paid to render. /api/cinematic/watchable
       transcodes an H.264 copy beside it, cached on the source's mtime, and
       leaves the shipped file exactly as Godot needs it. */
    if (rel) {
      window.open(`/api/cinematic/watchable?rel=${encodeURIComponent(rel)}`,
                  "_blank", "noopener");
    }
    const r = await mutate(`/api/cinematic/watched`,
                           { body: { artifact_id: cut.artifact_id }, quiet: true });
    setWatching(false);
    if (!r.ok) toast(r.error || "could not record the viewing", "bad");
  }

  /* The Theora footer, which both the shot list and the cut tab end with because
     it is the same sentence in both places: what the engine will accept, what
     the bytes actually are, and whether anybody has looked. */
  const delivery = (
    <div className={`bgs-deliver${(shippedBad.length || cutUnwatched || shippedGhost.length) ? " warn" : ""}`}>
      <Ti name={cutUnwatched ? "eye-off" : "eye"} size={17}
          color={cutUnwatched ? "var(--bad)" : "var(--text-3)"} />
      <div className="b">
        Nothing ships as .mp4 — Godot plays Ogg Theora and only Ogg Theora.{" "}
        {/* A FAILED READ IS NOT AN EMPTY PROJECT. Without this branch a 404 on
            the survey renders as "nothing has been delivered yet" over a
            project that has delivered plenty. */}
        {film.__error
          ? <b>this could not be checked — {film.__error}</b>
          : film.probe === false
            ? <b>nothing here is measured</b>
            : shippedBad.length
              ? <>ffprobe found <b>{shippedBad.length} file(s) installed into the engine that are not Theora</b></>
              : shipped.length
                ? <>ffprobe read the {shipped.length} installed file(s) and all are Theora</>
                : rows.length
                  ? <>nothing is installed into the engine yet — the {rows.length} take(s)
                      below are still in <span className="mono">.bgate_out</span></>
                  : <>nothing has been delivered yet</>}
        {/* A GHOST INSTALL IS NOT A PASS. An artifact carrying a res:// path
            whose file is gone reads as "delivered" everywhere the register is
            consulted and is a broken preload in the engine. `install` is the
            one-press repair and the card below offers it. */}
        {!!shippedGhost.length && <>{" "}<b>{shippedGhost.length} take(s) claim a
          res:// path whose file is not on disk.</b></>}
        {cut
          ? (cutUnwatched
            ? <>, and <b>nobody has watched the assembled cut</b>.</>
            : <>, and the cut was watched {ago(cut.watched_at)} ago{cut.watched_by ? ` by ${cut.watched_by}` : ""}.</>)
          : <> No cut has been assembled — <span className="mono">cinematic_assemble</span> joins the kept takes.</>}
        {film.why && <span className="why">{film.why}</span>}
      </div>
      <button className="bgs-btn go bgs-watch" onClick={watch}
              disabled={!cut?.target || watching}
              title={cut?.target || "nothing has been installed to watch"}>
        watch the cut
      </button>
    </div>
  );

  /* THE COUNT ON A CHIP IS kept/planned, NOT planned. A five-shot sequence with
     one kept take and a five-shot sequence that is finished were the same chip,
     and the difference between them is four purchases. */
  const picker = (
    <div className="bgs-chips">
      {sequences.map((s) => (
        <button key={s.id} className={`bgs-chip${s.name === name ? " on" : ""}`}
                onClick={() => setPick(s.name)}
                title={`${s.kept ?? 0} of ${s.shot_count ?? 0} shot(s) kept · ${s.status || "planned"}`}>
          {s.name}<span className="n">{s.kept ?? 0}/{s.shot_count ?? 0}</span>
        </button>
      ))}
    </div>
  );


  /* STORYBOARD sits between the shot list and the cut in the reference, and in
     the pipeline: board it, THEN write the shot list, then buy a frame. The
     boards are real — /api/storyboard/boards — and this is the cheap half of
     the process, which is the whole argument for showing it next to the price. */
  if (tab === "storyboard") {
    const all = board.frames || [];
    /* A CUT FRAME IS A DELETED BEAT AND IT STILL COMES BACK. storyboard_frame_cut
       marks `status = 'cut'`; the endpoint returns it, and the board's own
       `ready.live` excludes it. Drawing it beside the live frames made a
       five-beat board look six long and made "6 of 6 drawn" a completion claim
       over a board that had had a beat pulled out of it. */
    const frames = all.filter((f) => f.status !== "cut");
    const cutFrames = all.filter((f) => f.status === "cut");
    const boarded = frames.filter((f) => f.image_path).length;
    const approved = frames.filter((f) => f.status === "approved").length;
    const ready = board.ready || {};
    const blockers = ready.blockers || [];
    const secs = frames.reduce((n, f) => n + (Number(f.duration) || 0), 0);
    return (
      /* .bgs-pad, like every other tab. Without it this panel rendered flush to
         the window edge, which is most of why it read as unbuilt. */
      <div className="bgs-pad">
        <Head label="Storyboard"
              hint="board it before you buy a frame — this half is free"
              right={frames.length ? (
                <span className="bgs-dim">
                  {boarded} of {frames.length} live frame{frames.length === 1 ? "" : "s"} drawn
                  {approved !== frames.length ? ` · ${approved} approved` : ""}
                  {cutFrames.length ? ` · ${cutFrames.length} cut` : ""}
                  {secs ? ` · ${secs}s` : ""}
                </span>
              ) : undefined} />

        {/* One chip per board, the same picker the shot list uses for
            sequences — a project boards more than one scene. */}
        {boardList.length > 1 && (
          <div className="bgs-chips">
            {boardList.map((b) => (
              <button key={b.name}
                      className={`bgs-chip${b.name === boardName ? " on" : ""}`}
                      onClick={() => setBoardPick(b.name)}
                      title={Object.entries(b.frame_status || {})
                        .map(([k, v]) => `${v} ${k}`).join(" · ") || undefined}>
                {b.name}<span className="n">{(b.frame_status || {}).approved ?? b.frames ?? 0}</span>
              </button>
            ))}
          </div>
        )}

        {!boardList.length && (
          <Nothing what="no boards yet"
                   how="storyboard_plan / storyboard_auto draw one from the beat, and a shot anchors on its frame" />
        )}

        {board.logline && (
          <div className="bgc-logline">
            <b>{board.logline}</b>
            {board.premise && <p>{board.premise}</p>}
            <div className="m">
              {board.style && <Tag tone="seat">{board.style}</Tag>}
              {board.aspect_ratio && <Tag tone="off">{board.aspect_ratio}</Tag>}
              {board.status && (
                <Tag tone={board.status === "promoted" ? "good" : "off"}>{board.status}</Tag>
              )}
              {/* THE CAST THIS BOARD CONDITIONS ON. These are the ref images
                  every frame is drawn against and the reason two boards of the
                  same premise do not look alike — the endpoint returns them and
                  the tab was throwing them away. */}
              {(board.cast_refs || []).map((r) => (
                <Tag key={r} tone="off" title="a pinned character reference this board draws against">{r}</Tag>
              ))}
            </div>
          </div>
        )}

        {/* PROMOTION IS THE STEP BETWEEN THE FREE HALF AND THE PAID ONE —
            storyboard_promote turns an approved board into the shot list every
            frame then anchors. Whether it can happen, and what is stopping it,
            is on `ready`, which this tab had typed as a boolean and never read.
            A board that cannot promote and does not say why is where the
            pipeline silently stops. */}
        {!!frames.length && (ready.promotable === false || !!blockers.length) && (
          <Banner icon="alert-triangle" tone="warn">
            <div className="t">
              This board cannot become a shot list yet
              {typeof ready.live === "number"
                ? ` — ${ready.approved ?? 0} of ${ready.live} live frame(s) approved.`
                : "."}
            </div>
            {!!blockers.length && <div className="s">{blockers.join(" · ")}</div>}
          </Banner>
        )}
        {!!frames.length && ready.promotable && board.status !== "promoted" && (
          <Banner icon="arrow-right" tone="good">
            <div className="t">
              {ready.approved ?? boarded} frame(s) approved — <b>storyboard_promote</b> writes
              the shot list from this board, and every shot anchors on the frame above it.
            </div>
          </Banner>
        )}
        {board.status === "promoted" && board.sequence_id && (
          <div className="bgs-dim bgc-promoted">
            promoted — the shot list on the Shot list tab is this board, one shot per live frame
          </div>
        )}

        {/* THE BOARD IS THE FRAMES. Each carries the still it was drawn as, the
            beat it covers, the camera note and its seconds — which is exactly
            what a shot in the list anchors on, so the two panels can be read
            against each other. An unfilled frame is drawn as a slot rather than
            omitted: the gaps are the work left. */}
        {!!frames.length && (
          <div className="bgc-board">
            {frames.map((f) => (
              <figure className={`bgc-frame${f.image_path ? "" : " empty"}`} key={f.id}>
                <div className="img">
                  {f.image_path
                    /* NOT loading="lazy". A board is a contact sheet — the
                       whole point of the tab is seeing all of it at once, and
                       six to a dozen thumbnails is not a list worth deferring.
                       Lazy also defers indefinitely wherever layout never
                       reports the image as near the viewport, which turns "the
                       board has not loaded yet" into "the board is empty". */
                    ? <img src={previewURL(f.image_path)} alt={f.beat || f.slug || ""}
                           onClick={() => lightbox(f.image_path || "")} />
                    : <span className="none"><Ti name="photo-off" size={20} /></span>}
                  <span className="n">{String(f.idx).padStart(2, "0")}</span>
                  {!!f.duration && <span className="d">{f.duration}s</span>}
                </div>
                <figcaption>
                  {f.beat && <span className="b">{f.beat}</span>}
                  {f.action && <span className="a">{f.action}</span>}
                  {/* DIALOGUE IS ON THE FRAME AND WAS BEING DROPPED. A boarded
                      line is what the voice seat records against and what the
                      shot's duration has to hold; a board that hides it looks
                      like a silent film that is not one. */}
                  {f.dialogue && <span className="q">“{f.dialogue}”</span>}
                  {f.camera && (
                    <span className="c"><Ti name="video" size={11} />{f.camera}</span>
                  )}
                  <span className="c">
                    {/* A frame drawn by hand and a frame the model produced are
                        not the same evidence, and `source` says which. */}
                    {f.status && f.status !== "approved" && (
                      <Tag tone={f.status === "draft" ? "warn" : "off"}>{f.status}</Tag>
                    )}
                    {f.source && <span className="dim">{f.source}</span>}
                  </span>
                </figcaption>
              </figure>
            ))}
          </div>
        )}

        {/* THE CUT BEATS, kept visible and kept OUT of every count. Omitting
            them entirely loses the record that a beat was considered and
            dropped; counting them makes a five-beat board claim six. */}
        {!!cutFrames.length && (
          <div className="bgc-cutrow">
            <Ti name="scissors" size={13} />
            <span>
              {cutFrames.length} beat(s) cut from this board and not counted above:{" "}
              {cutFrames.map((f) => f.slug || `#${f.idx}`).join(", ")}
            </span>
          </div>
        )}

        {!!boardList.length && !frames.length && !board.__error && (
          <Nothing what={all.length ? "every frame on this board has been cut" : "this board has no frames"}
                   how="storyboard_plan writes the beats; storyboard_frame_generate draws each one" />
        )}
        <ReadError error={boards.__error} what="the storyboards" />
        <ReadError error={board.__error} what={`the board ${boardName || ""}`} />
      </div>
    );
  }

  /* ── one take, drawn against what its KIND actually promises ─────────────
     Every badge here used to be a cutscene claim wearing a shot's name. The
     three that are kind-dependent:
       * theora   — required of a cutscene; a shot take is the provider's h264
                    .mp4 and assemble() reads that mp4, so "not theora" on a
                    shot is the correct state, not a defect.
       * installed— required of a cutscene; keep() deliberately does NOT install
                    shots, because a per-shot .ogv is tens of megabytes nothing
                    references.
       * watched  — a claim about the assembled cut only. */
  const takeCard = (k: Kept) => {
    const m = rows.find((r) => r.artifact_id === k.artifact_id);
    const isCut = k.kind === "cutscene";
    const dead = k.status === "superseded" || k.status === "rejected";
    /* A res:// path whose file is not on disk. The register still reports this
       take as delivered and the engine will fail to preload it. */
    const ghost = !!k.godot_res && !!m && m.exists === false;
    const working = busy === k.artifact_id;
    return (
      <div className={`bgs-card bgc-take${dead ? " dead" : ""}`} key={k.artifact_id}>
        <div className="h">
          <span className="t">
            {k.logical_name || `artifact ${k.artifact_id}`}
            {k.revision ? <span className="bgs-dim"> r{k.revision}</span> : null}
          </span>
          <Tag tone={isCut ? "seat" : "off"}
               title={isCut
                 ? "the assembled cut — this is the file the game loads"
                 : "an intermediate take; assemble() reads it and the engine never sees it"}>
            {isCut ? "cut" : `shot${k.shot_idx ? ` ${k.shot_idx}` : ""}`}
          </Tag>
          {/* A SUPERSEDED REVISION IS NOT WORK LEFT TO DO. r1 sitting beside an
              installed r2 was drawn with the same three red badges as a delivery
              that had genuinely failed. */}
          {dead && <Tag tone="off" title="an older revision — a newer one replaced it">{k.status}</Tag>}

          {/* THE SUFFIX IS NOT THE EVIDENCE. `k.playable` is filename-derived;
              the codec beside it is what ffprobe found in the bytes, and the two
              disagreeing is the broken-libtheora case this project has shipped. */}
          {m?.measured
            ? <Tag tone={isCut ? (m.theora ? "good" : "bad") : "off"}
                   title={m.why || `container ${m.container || "?"}` +
                     (isCut ? "" : " — a shot take is expected to be the provider's mp4")}>
                {m.theora ? "theora, measured" : m.demuxed
                  ? `${m.video_codec || "no video stream"}${isCut ? " — not theora" : ""}`
                  : "does not demux"}
              </Tag>
            : <Tag tone={m?.exists === false ? "bad" : "off"}
                   title={m?.why || "no ffprobe on this machine"}>
                {m?.exists === false ? "file is gone" : "unmeasured"}
              </Tag>}

          {/* Only a cutscene owes an install. */}
          {isCut
            ? <Tag tone={k.installed ? (k.install_stale ? "warn" : "good") : (dead ? "off" : "warn")}
                   title={k.installed_path || "cinematic_install transcodes it into the engine project"}>
                {k.installed ? (k.install_stale ? "installed, stale" : "installed") : "not installed"}
              </Tag>
            : k.installed
              ? <Tag tone="warn" title="keep() does not install shots on purpose — a per-shot .ogv is bytes nothing references">
                  installed (shots need not be)
                </Tag>
              : null}
          {ghost && <Tag tone="bad" title={k.godot_res}>res:// points at nothing</Tag>}

          {/* Only the cut is watched. */}
          {isCut && (m?.watched_at
            ? <Tag tone="good" title={m.watched_by ? `by ${m.watched_by}` : undefined}>
                watched {ago(m.watched_at)} ago
              </Tag>
            : <Tag tone={dead ? "off" : "bad"}>unwatched</Tag>)}
        </div>

        <div className="kv"><span>path</span><b className="wrap">{k.installed_path || k.path}</b></div>
        {k.godot_res && <div className="kv"><span>res://</span><b className="wrap">{k.godot_res}</b></div>}
        {m?.measured && (
          <div className="kv"><span>ffprobe</span>
            <b className="wrap">
              {m.container || "?"} · {m.video_codec || "no video"}
              {m.audio_codec ? ` + ${m.audio_codec}` : ""}
              {m.duration_s ? ` · ${m.duration_s}s` : ""}
              {m.bytes ? ` · ${Math.round(m.bytes / 1024)}kb` : ""}
            </b>
          </div>
        )}

        {/* THE DECISIONS THE BACKEND HAS ALWAYS TAKEN AND THIS SCREEN NEVER
            OFFERED. /api/cinematic/install is the documented repair verb for
            exactly the two states above — a cut that was never installed and a
            res:// whose file is gone — and /api/cinematic/discard is how a take
            is refused. Without them a broken install could be SEEN here and only
            REPAIRED from an MCP tool in another window. */}
        {!dead && (
          <div className="bgc-acts">
            {(isCut || ghost) && (
              <button className={`bgs-btn${!k.installed || ghost || k.install_stale ? " go" : ""}`}
                      disabled={working}
                      onClick={() => act("/api/cinematic/install",
                                         { artifact_id: k.artifact_id },
                                         `transcoded ${k.logical_name} into the engine`)}
                      title="transcode this approved take into the engine project — idempotent, and it does not change review state">
                {ghost ? "repair the install" : k.installed ? "re-install" : "install to engine"}
              </button>
            )}
            <button className="bgs-btn" disabled={working}
                    onClick={() => act("/api/cinematic/discard",
                                       { artifact_id: k.artifact_id },
                                       `discarded ${k.logical_name} — its shot is back to planned`)}
                    title="reject this take. The file stays; the decision is what is recorded, and the shot returns to planned">
              discard
            </button>
          </div>
        )}
      </div>
    );
  };

  if (tab === "cut") {
    /* FILTERED HERE BECAUSE THE ENDPOINT DOES NOT FILTER. See OWNED_BY. */
    const mine = (cand.kept || []).filter((k) => OWNED_BY(k, name || ""));
    const pending = (cand.candidates || []).filter((k) => OWNED_BY(k, name || ""));
    const cuts = mine.filter((k) => k.kind === "cutscene");
    const takes = mine.filter((k) => k.kind !== "cutscene");
    const live = mine.filter((k) => k.status !== "superseded" && k.status !== "rejected");
    return (
      <div className="bgs-pad">
        {picker}
        <Head label="The cut"
              hint="nothing ships as .mp4 — Godot plays Ogg Theora and only Ogg Theora"
              right={mine.length ? (
                <span className="bgs-dim">
                  {live.length} live · {mine.length - live.length} superseded
                </span>
              ) : undefined} />
        <ReadError error={cand.__error} what="the kept takes" />
        {!mine.length && !pending.length && !cand.__error && (
          <Nothing what={`nothing kept for ${name || "this sequence"}`}
                   how="cinematic_keep promotes a generated take; cinematic_assemble joins them into one .ogv" />
        )}
        <ReadError error={film.__error} what="the delivered files" />

        {/* AWAITING A DECISION. The endpoint has always returned these and the
            tab dropped the key entirely, so on a project whose approval gate is
            ON — where every generated take lands as a candidate and NOTHING is
            approved until a human says so — this tab was empty and the takes
            were invisible. */}
        {!!pending.length && (
          <>
            <Head label="Awaiting a decision"
                  hint="already paid for — keep it or discard it, but it is not free to re-generate" />
            {pending.map((k) => (
              <div className="bgs-card bgc-take" key={k.artifact_id}>
                <div className="h">
                  <span className="t">
                    {k.logical_name || `artifact ${k.artifact_id}`}
                    {k.revision ? <span className="bgs-dim"> r{k.revision}</span> : null}
                  </span>
                  <Tag tone="warn">candidate</Tag>
                </div>
                <div className="kv"><span>path</span><b className="wrap">{k.path}</b></div>
                <div className="bgc-acts">
                  <button className="bgs-btn go" disabled={busy === k.artifact_id}
                          onClick={() => act("/api/cinematic/keep",
                                             { artifact_id: k.artifact_id, async: "0" },
                                             `kept ${k.logical_name}`)}
                          title="approve this take. A cutscene is transcoded into the engine; a shot stays in .bgate_out where assemble reads it">
                    keep
                  </button>
                  <button className="bgs-btn" disabled={busy === k.artifact_id}
                          onClick={() => act("/api/cinematic/discard",
                                             { artifact_id: k.artifact_id },
                                             `discarded ${k.logical_name}`)}>
                    discard
                  </button>
                </div>
              </div>
            ))}
          </>
        )}

        {!!cuts.length && (
          <Head label="Assembled" hint="the file the game loads — this one owes Theora, an install, and a viewing" />
        )}
        {cuts.map(takeCard)}
        {!!takes.length && (
          <Head label="Shot takes"
                hint="intermediates. assemble() reads these .mp4s; keep() does not install them, on purpose" />
        )}
        {takes.map(takeCard)}
        {!!rows.length && delivery}
      </div>
    );
  }

  return (
    <div className="bgs-pad">
      {picker}
      <ReadError error={list.__error} what="the sequence list" />
      {!sequences.length && (
        <Nothing what="no sequences"
                 how="cinematic_plan writes a shot list; boarding it costs a fraction of a cent and buying a frame does not" />
      )}

      {/* MONEY THAT HAS ALREADY LEFT THE ACCOUNT, above the estimate, because
          the estimate is a forecast and this is a receipt. cinematic_recover
          downloads a generation the provider finished and charged for; pressing
          generate on one of these shots buys the same frame a second time. */}
      {!!stuckq.recoverable && (
        <Banner icon="download" tone="bad">
          <div className="t">
            <b>{stuckq.recoverable} shot(s) are sitting on a paid, uncollected generation.</b>{" "}
            The provider finished and charged for them. <b>cinematic_recover_shot</b> downloads
            what was bought; generating again buys the same frame twice.
          </div>
          <div className="s">
            {(stuckq.shots || []).filter((s) => s.recoverable)
              .map((s) => `${s.sequence || ""}#${s.idx ?? "?"}${s.state ? ` (${s.state})` : ""}`)
              .join(" · ")}
          </div>
        </Banner>
      )}

      {e && (
        <Banner icon="coin" tone="warn"
                right={<span className="bgs-price">
                  {typeof e.usd === "number" ? `$${e.usd.toFixed(2)}` : `${e.credits ?? "?"} credits`}
                </span>}>
          <div className="t">
            Board it, then write the shot list, then buy a frame. The board costs a fraction
            of a cent; <b>{e.shots ?? shots.length} shots · {e.runtime_s ?? seq?.runtime_s ?? "?"}s</b> and
            every shot is a separate purchase.
          </div>
          <div className="s">
            {/* THE ESTIMATE'S OWN CAVEAT, not a footnote somewhere else. */}
            {e.known === false
              ? "upper bound — the provider publishes no per-model dollar rate, so this is a credit count, not a price"
              : "upper bound, from the provider's published band"}
            {/* WHICH TOTAL THIS IS TRUE OF. cinematic.py sums `credits` over EVERY
              shot and excludes the unpriced ones only from `usd` — so under a
              credits figure this sentence was exactly backwards: on the live
              project it read "934 credits · 4 shot(s) NOT in this total" for a
              four-shot cut whose four shots are all in the 934. */}
          {e.unknown_shots?.length
            ? (typeof e.usd === "number"
                ? ` · ${e.unknown_shots.length} shot(s) could not be priced and are NOT in this total`
                : ` · ${e.unknown_shots.length} of ${e.shots ?? "?"} shot(s) have no published price — they ARE counted in the credits above, at the provider's own rate`)
            : ""}
          </div>
          {/* WHERE THE NUMBER CAME FROM, on the number. The backend ships a
              `note` naming the band it was derived from and the env var that
              replaces it with your invoices; a figure somebody spends against
              should be able to say who told it that. */}
          {e.note && <div className="s bgc-basis">{e.note}</div>}
        </Banner>
      )}

      <Head label="Shot list"
            hint="every shot anchors on an approved still, never on the last shot's output"
            right={<span className="bgs-dim bgc-seqmeta">
              {/* WHAT WAS ACTUALLY ORDERED. A 4k shot and a 480p one quote the
                  same credits (the estimate's own caveat says so) and cost
                  differently, so the resolution belongs beside the price. */}
              {modelName && <Tag tone="off">{modelName}</Tag>}
              {seq?.resolution && <Tag tone="off">{seq.resolution}</Tag>}
              {seq?.aspect_ratio && <Tag tone="off">{seq.aspect_ratio}</Tag>}
              {typeof seq?.kept === "number" && (
                <Tag tone={seq.kept >= (seq.shot_count ?? 0) ? "good" : "off"}>
                  {seq.kept}/{seq.shot_count ?? 0} kept
                </Tag>
              )}
              {seq?.status && <Tag tone={seq.status === "assembled" ? "good" : "off"}>{seq.status}</Tag>}
            </span>} />
      {name && !shots.length && (
        <Nothing what="this sequence has no shots" how="cinematic_plan writes them, with a first frame each" />
      )}
      {shots.map((s) => {
        const p = perShot.get(Number(s.idx));
        /* WHAT THIS SHOT ACTUALLY IS ON DISK, matched by artifact rather than by
           name. Until a take is kept there is no file and the format column says
           so — planned, not "mp4". */
        const m = s.artifact_id
          ? rows.find((r) => r.artifact_id === s.artifact_id)
          : undefined;
        const fmt = m
          ? (m.theora ? "ogv" : m.measured ? (m.video_codec || m.container || "not ogg") : "unmeasured")
          : "";
        const frac = ceiling && s.duration != null
          ? Math.max(0, Math.min(1, Number(s.duration) / ceiling)) : 0;
        const hot = frac > 0.85;
        /* THE ANCHOR AND THE REFS CANNOT BOTH BE SENT ON THIS MODEL. seedance-2
           publishes exclusive [["first_frame","last_frame"],["refs"]]: a call
           carrying an anchor has its reference images dropped. A shot list that
           anchors every shot (which is this seat's whole doctrine) and also
           carries the board's cast refs is a shot list whose character
           references silently do nothing, and "the character has a different
           face" is exactly the symptom that gets blamed on the anchor. */
        const refs = s.refs || [];
        const refConflict = anchorExcludesRefs && !!s.first_frame && refs.length > 0;
        /* Over the ceiling is not "hot", it is a call the provider refuses. */
        const overCeiling = ceiling > 0 && s.duration != null && Number(s.duration) > ceiling;
        const art = s.artifact || null;
        return (
          <div className={`bgs-shot${s.first_frame ? "" : " warn"}`} key={s.id}>
            <span className="n">{String(s.idx).padStart(2, "0")}</span>
            {s.first_frame
              ? <button className="thumb" onClick={() => lightbox(s.first_frame!)} title={s.first_frame}>
                  <img src={previewURL(s.first_frame)} alt={s.slug || `shot ${s.idx}`} />
                </button>
              : <span className="thumb none"><Ti name="photo-off" size={15} /></span>}
            <div className="b">
              <div className="t">{s.action}</div>
              <div className="m">
                <Ti name="pin" size={11} />
                {s.first_frame
                  ? <span>{s.first_frame.split(/[\\/]/).pop()}</span>
                  : <span className="warn">no anchor — this shot conditions on nothing</span>}
                {/* A LAST FRAME IS A SECOND ANCHOR and changes what the shot is
                    allowed to do between them. It was on the wire and unread. */}
                {s.last_frame && (
                  <span className="dim" title={s.last_frame}>
                    → {s.last_frame.split(/[\\/]/).pop()}
                  </span>
                )}
                {s.camera && <span className="dim">· {s.camera}</span>}
                {s.shot_size && <span className="dim">· {s.shot_size}</span>}
              </div>
              {/* The spoken line, the note, and the conflict — none of which had
                  anywhere to appear. */}
              {(s.vo || s.dialogue) && (
                <div className="m"><Ti name="message" size={11} />
                  <span className="dim">“{s.vo || s.dialogue}”</span>
                </div>
              )}
              {refConflict && (
                <div className="m bgc-conflict"><Ti name="alert-triangle" size={11} />
                  <span>
                    {refs.length} character ref(s) will be DROPPED — {modelName} refuses
                    first_frame and refs on the same call
                  </span>
                </div>
              )}
              {overCeiling && (
                <div className="m bgc-conflict"><Ti name="alert-triangle" size={11} />
                  <span>{s.duration}s is over {modelName}'s {ceiling}s ceiling — this call will be refused</span>
                </div>
              )}
              {s.note && (
                <div className="m"><Ti name="note" size={11} /><span className="dim">{s.note}</span></div>
              )}
            </div>
            <div className="durbox">
              {/* THE BAR IS AGAINST THE MODEL'S OWN CEILING and is drawn only
                  when that ceiling was read — a bar with an invented
                  denominator says nothing and looks like it says something. */}
              {ceiling > 0 && (
                <div className="track"><span className={hot ? "hot" : ""}
                                              style={{ width: `${frac * 100}%` }} /></div>
              )}
              <div className={`lb${hot ? " hot" : ""}`}>
                {s.duration != null ? `${s.duration}s` : "—"}
                {ceiling > 0 ? ` / ${ceiling}s max` : ""}
              </div>
            </div>
            {fmt && <span className={`fmt ${m?.theora ? "ok" : "warn"}`}>{fmt}</span>}
            <span className={`cost${p && p.known === false ? " unknown" : ""}`}>
              {p
                ? (typeof p.usd === "number" ? `$${p.usd.toFixed(2)}`
                   : p.credits ? `${p.credits}cr` : "—")
                : ""}
            </span>
            {s.transition && (
              <span className="mono dim" title={s.transition_s ? `${s.transition_s}s` : undefined}>
                {s.transition}
              </span>
            )}
            {/* THE SHOT'S STATUS AND ITS ARTIFACT'S REVIEW STATE ARE TWO
                DIFFERENT FACTS. A shot reading "generated" over an artifact
                still sitting at "candidate" is a take somebody has to decide on
                and pay for again if they do not — and it was drawn as one word.
                SHOT_TONE also only ever knew "kept"/"generating"/"failed", so
                "generated" — the state four of the five live shots are in —
                came out as the same grey as "planned". */}
            <Tag tone={art?.status === "candidate" ? "warn" : SHOT_TONE(s.status)}
                 title={art ? `${art.logical_name} r${art.revision} — ${art.status}` : undefined}>
              {s.status || "planned"}
            </Tag>
            {art?.status === "candidate" && (
              <Tag tone="warn" title="already paid for and not yet decided — keep or discard it on the Cut tab">
                undecided
              </Tag>
            )}
          </div>
        );
      })}
      <ReadError error={one.__error} what="this sequence" />
      {!!shots.length && delivery}
    </div>
  );
}
