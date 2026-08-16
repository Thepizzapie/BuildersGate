/* ptreview.js — triage for a recorded playtest.
 *
 * THE COMPLAINT THIS ANSWERS: "the review of the playtest and ui/ux there just
 * feels like the app doesn't know what it wants to do." It did not. One line of
 * feedback — a mic check, sixty-eight seconds, no telemetry — rendered with
 * three badges and eight controls (seat, kind, promote, dismiss, a merge
 * target, merge, a THREE-HUNDRED-option asset select, link) all at the same
 * weight, on a card the same size as a reproducible crash.
 *
 * ── THE DECISION THE SCREEN IS FOR ─────────────────────────────────────────
 * A person reading playtest feedback answers ONE question per line: is this
 * worth acting on? Yes, it becomes work. No, it goes in the bin. Assigning a
 * seat, correcting the kind, folding a duplicate into another item and linking
 * an asset are all REFINEMENTS OF "YES" — they are what you do after you have
 * decided, and while you are deciding they are noise wearing the same clothes
 * as the decision. So: two buttons on the face of every row, everything else
 * behind one disclosure, and the row is a row rather than a card because the
 * job is to read down a list rather than to dwell on one.
 *
 * ── WHY THE GROUPING IS BY DECISION AND NOT BY KIND ────────────────────────
 * Grouping by the classifier's kind sounds right and is not, because the
 * classifier is a lexical first pass that fires on no rules at all for most
 * spoken lines (see feedback.classify: no match returns "note", {}). Sorting a
 * list by a field that is empty for half of it produces one enormous "note"
 * bucket, which is the flat list again with extra furniture. What the reader
 * actually needs partitioned is what is STILL OPEN versus what is settled — so
 * the groups are: to decide, low signal, praise, filed, binned. Kind becomes a
 * subhead inside "to decide", and only once there are enough items for a
 * subhead to save the reader anything (KIND_SUBHEAD_AT).
 *
 * ── THE FOUR MEASURED BUGS, AND WHAT HAPPENED TO EACH ──────────────────────
 *
 * 1. "SPEECH CONFIDENCE -1.17" WAS A LOG PROBABILITY WEARING THE WORD
 *    CONFIDENCE. _whisper_runner.py stores whisper's avg_logprob raw and says
 *    in its own comment that it exposes it as-is rather than faking a 0-1.
 *    That is correct at the adapter and unreadable at the UI: the number is
 *    always <= 0, so a human reads a negative confidence and learns nothing.
 *    The stored value and the adapter are untouched. heard() translates at the
 *    render boundary, the scale lives in the tooltip for anyone who wants it,
 *    and a line whisper heard cleanly says NOTHING — a chip that fires on every
 *    row is a background texture, not a warning.
 *
 * 2. "CLASSIFIER 0% - NOTE" WAS A LABEL WITH NOTHING BEHIND IT. The threshold
 *    is not a tuning choice; it falls out of the classifier. brief() computes
 *    confidence as max(scores)/sum(scores), so 0.0 happens in exactly one
 *    situation: `scores` is empty, no rule matched, and "note" is the fallback
 *    branch rather than a finding. When nothing was classified the row says
 *    nothing about kind. Above zero it shows the kind as a word; the ratio goes
 *    in the drawer next to the scores that produced it, where it is evidence
 *    instead of decoration.
 *
 * 3. "DIRECTOR RECOMMENDS REVIEW" NAMED NO ACTION. 'review' means "a human has
 *    to look at this", which is the definition of being in this list — it was
 *    printed on every row and could not distinguish between any two of them.
 *    It is gone. The recommendation now moves the BUTTON instead of adding a
 *    badge: 'promote' fills the primary action and marks it suggested, 'keep'
 *    (praise) moves the row into a group that says there is nothing to action,
 *    and 'review' does nothing at all, which is what it always meant.
 *
 * 4. A MIC CHECK GOT THE SAME NINE CONTROLS AS A CRASH. quiet() names the
 *    combination that means "the system found nothing here": not typed by a
 *    human, no classifier hit, no seat, no telemetry within the window, and
 *    either badly heard or too short to be a sentence. Those fold into one
 *    counted, honestly-labelled row you can open. They are not filtered away —
 *    a hidden item is a lost item, and session 19 is entirely made of these, so
 *    a filter would have rendered an empty screen for a real recording.
 *
 * ── TWO THINGS THAT ARE NOT COSMETIC ───────────────────────────────────────
 * PROMOTING AN ITEM NO LONGER RESTARTS THE VIDEO. Every triage action used to
 * call openSession(), which rebuilds the entire overlay body including the
 * <video> — so the evidence you were watching jumped back to 0:00 and your
 * scroll position went with it. This panel owns its own subtree and repaints
 * only that, behind the _sig() guard from seats/cinematic.js, so the recording
 * keeps playing at the second you left it.
 *
 * THE RECORDING IS PINNED. The evidence for a line is the moment it describes,
 * and the review used to make you scroll up to find it. The video stage sticks
 * to the top of the scroller and shrinks once triage scrolls under it, every
 * timestamp and thumbnail seeks it, and the frame captured at that second sits
 * on the row itself.
 *
 * Registered as window.PtReview. Injects its own <style> — app.css belongs to
 * the design pass and this touches none of it.
 */
window.PtReview = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const icon = (n, size) => {
    try { return (window.BGIcon && BGIcon.has(n)) ? BGIcon(n, { size: size || 13 }) : ""; }
    catch (e) { return ""; }
  };

  const SEATS = ["unassigned", "director", "narrative", "gameplay", "tech",
                 "art", "audio", "cinematic", "qa"];
  const KINDS = ["like", "fix", "add", "change", "question", "note"];
  /* Below this many open items a kind subhead costs a line of chrome and saves
     nobody a scan. Two items do not need a taxonomy. */
  const KIND_SUBHEAD_AT = 6;
  /* An unclassified, unrouted, telemetry-free line shorter than this is a mic
     check or a grunt. Six words is roughly the shortest actionable sentence
     anyone says while playing ("the jump feels bad here"). */
  const MIN_WORDS = 6;
  /* whisper avg_logprob bands. <= 0 always; the numbers are the adapter's, the
     words are ours. */
  const HEARD_FAIR = -0.5;
  const HEARD_POOR = -1.0;

  const KIND_LABEL = {
    fix: "to fix", add: "to add", change: "to change",
    question: "questions", like: "praise", note: "notes",
  };

  let host = null;          // our container inside #review-body
  let data = null;          // the whole /api/playtest/<id> payload
  let seek = null;          // the shell's seekReview
  let sig = "";             // what is already painted. See _sig().
  let busy = false;         // a mutation is in flight; do not repaint over it
  const drawers = new Set();  // item ids whose refinements are open
  const folds = new Map();    // group key -> open?
  let cursor = null;          // item id under the keyboard
  let keysBound = false;

  /* ── styles ───────────────────────────────────────────────────────────── */
  function injectStyle() {
    if (document.getElementById("ptreview-style")) return;
    const s = document.createElement("style");
    s.id = "ptreview-style";
    s.textContent = [
      /* THE RECORDING STAYS ON SCREEN while you read down the list.
         .review-body is the scroller (overflow:auto), so sticky resolves
         against it — but only once it is NOT a grid. A sticky grid item's
         containing block is its own grid area, which in a single-column auto
         -rows grid is exactly the item's own box, so it has nowhere to travel
         and position:sticky silently does nothing. Measured: the stage stayed
         at its document position with the class never applying. Block layout
         plus a lobotomised-owl margin is the same stack with the same gap. */
      ".review-body{display:block}",
      ".review-body > * + *{margin-top:var(--s-7)}",
      /* THE BAND SPANS THE SCROLLER; THE PICTURE DOES NOT. .video-stage is
         min(900px,100%) wide, and once it was pinned the triage header slid
         underneath it in the 400px to its right, in full view. The stage box
         goes full width so its opaque background covers the row, and the video
         and its marker track keep the width they always had. */
      ".review-body .video-stage{position:sticky;top:calc(-1 * var(--s-5));z-index:2;",
      "  width:auto;background:var(--solid-1);padding:var(--s-5) 0 var(--s-4);",
      "  transition:none}",
      ".review-body .video-stage .review-video,",
      ".review-body .video-stage .marker-track{width:min(900px,100%)}",
      ".review-body .video-stage.ptr-stuck{box-shadow:0 10px 18px -14px rgba(0,0,0,.9)}",
      ".review-body .video-stage.ptr-stuck .review-video{max-height:190px}",
      ".review-body .video-stage .review-video{transition:max-height var(--dur) var(--ease)}",
      "@media (prefers-reduced-motion:reduce){",
      "  .review-body .video-stage .review-video{transition:none}}",

      ".ptr{display:flex;flex-direction:column;gap:var(--s-5)}",

      /* One band at the top of the surface, same device as .sec-h: a header is
         a different SURFACE, not a bigger font. */
      ".ptr-hd{display:flex;align-items:center;gap:var(--s-4);min-height:var(--s-8);",
      "  padding:var(--s-4) var(--s-5);background:var(--surface-4);",
      "  border:1px solid var(--line);border-radius:var(--r-md);}",
      ".ptr-hd > .bgi{color:var(--text-2);flex:none}",
      ".ptr-hd h3{margin:0;font-family:var(--mono);font-size:var(--fs-2xs);",
      "  font-weight:var(--fw-semi);letter-spacing:var(--track-label);",
      "  text-transform:uppercase;color:var(--text);white-space:nowrap}",
      ".ptr-n{flex:none;font-family:var(--mono);font-size:var(--fs-3xs);line-height:1;",
      "  color:var(--text-2);background:var(--surface-1);border:1px solid var(--line);",
      "  border-radius:var(--r-full);padding:var(--s-2) var(--s-3);",
      "  font-variant-numeric:tabular-nums}",
      ".ptr-n.good{color:var(--good);border-color:var(--good-line);background:var(--good-soft)}",
      ".ptr-keys{margin-left:auto;font-family:var(--mono);font-size:var(--fs-3xs);",
      "  color:var(--text-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".ptr-keys kbd{border:1px solid var(--line);border-radius:var(--r-xs);",
      "  padding:0 3px;color:var(--text-2)}",

      /* ── a group ─────────────────────────────────────────────────────── */
      ".ptr-g{display:flex;flex-direction:column;gap:var(--s-3)}",
      ".ptr-gh{display:flex;align-items:center;gap:var(--s-3);",
      "  font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);",
      "  text-transform:uppercase;color:var(--text-2)}",
      ".ptr-gh .why{text-transform:none;letter-spacing:0;color:var(--text-3)}",
      /* The folded groups. A <details> because the browser already knows how to
         do this and a hand-rolled toggle would be one more thing to get wrong
         with the keyboard. */
      "details.ptr-fold{border:1px solid var(--line);border-radius:var(--r-md);",
      "  background:var(--surface-1)}",
      "details.ptr-fold > summary{list-style:none;cursor:pointer;display:flex;",
      "  align-items:center;gap:var(--s-3);padding:var(--s-4) var(--s-5);",
      "  font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);",
      "  text-transform:uppercase;color:var(--text-2)}",
      "details.ptr-fold > summary::-webkit-details-marker{display:none}",
      "details.ptr-fold > summary:hover{color:var(--text)}",
      "details.ptr-fold > summary .why{margin-left:var(--s-3);text-transform:none;",
      "  letter-spacing:0;color:var(--text-3);min-width:0;overflow:hidden;",
      "  text-overflow:ellipsis;white-space:nowrap}",
      "details.ptr-fold > summary .caret{margin-left:auto;flex:none;color:var(--text-3)}",
      "details.ptr-fold[open] > summary{border-bottom:1px solid var(--line-soft)}",
      "details.ptr-fold[open] > summary .caret{transform:rotate(90deg)}",
      "details.ptr-fold .ptr-rows{padding:var(--s-4);display:flex;",
      "  flex-direction:column;gap:var(--s-3)}",
      ".ptr-rows{display:flex;flex-direction:column;gap:var(--s-3)}",

      /* ── a row ───────────────────────────────────────────────────────── */
      ".ptr-row{display:grid;grid-template-columns:auto 1fr auto;gap:var(--s-5);",
      "  align-items:start;padding:var(--s-4) var(--s-5);background:var(--surface-2);",
      "  border:1px solid var(--line);border-left:2px solid transparent;",
      "  border-radius:var(--r-sm)}",
      ".ptr-row.cur{border-left-color:var(--accent);background:var(--surface-3)}",
      ".ptr-row.flash{box-shadow:0 0 0 2px var(--info)}",
      ".ptr-row.settled{opacity:.72}",
      ".ptr-row.settled:hover{opacity:1}",

      /* The evidence. Thumbnail AND clock are one button: both mean "show me
         this moment", and two adjacent controls for one intent is the pattern
         this whole panel exists to stop. */
      ".ptr-ev{display:flex;flex-direction:column;align-items:center;gap:var(--s-2);",
      "  padding:0;background:none;border:0;cursor:pointer;font:inherit;flex:none}",
      ".ptr-ev img,.ptr-ev .noimg{width:104px;height:60px;border-radius:var(--r-xs);",
      "  border:1px solid var(--line);object-fit:cover;background:var(--bg);display:block}",
      ".ptr-ev .noimg{display:grid;place-items:center;color:var(--text-3);",
      "  font-family:var(--mono);font-size:var(--fs-3xs);text-align:center;line-height:1.3}",
      ".ptr-ev .t{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-2);",
      "  font-variant-numeric:tabular-nums}",
      ".ptr-ev:hover img,.ptr-ev:hover .noimg{border-color:var(--accent)}",
      ".ptr-ev:hover .t{color:var(--accent)}",

      ".ptr-body{min-width:0;display:flex;flex-direction:column;gap:var(--s-3)}",
      ".ptr-say{margin:0;font-size:var(--fs-md);line-height:var(--lh-snug);",
      "  color:var(--text);overflow-wrap:anywhere}",
      ".ptr-meta{display:flex;gap:var(--s-3);flex-wrap:wrap;align-items:center}",
      ".ptr-tag{font-family:var(--mono);font-size:var(--fs-3xs);line-height:1;",
      "  color:var(--text-3);border:1px solid var(--line);border-radius:var(--r-full);",
      "  padding:var(--s-2) var(--s-3);white-space:nowrap}",
      ".ptr-tag.warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}",
      ".ptr-tag.info{color:var(--info);border-color:var(--info-line);background:var(--info-soft)}",
      ".ptr-tag.good{color:var(--good);border-color:var(--good-line);background:var(--good-soft)}",
      ".ptr-tag.ev{color:var(--accent);border-color:var(--accent-line)}",
      ".ptr-meta .lead{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3)}",

      /* ── the decision ────────────────────────────────────────────────── */
      ".ptr-do{display:flex;align-items:center;gap:var(--s-3);flex:none}",
      ".ptr-b{display:inline-flex;align-items:center;gap:var(--s-3);cursor:pointer;",
      "  border:1px solid var(--line);border-radius:var(--r-sm);background:var(--surface-1);",
      "  color:var(--text-2);font:inherit;font-family:var(--mono);font-size:var(--fs-2xs);",
      "  letter-spacing:.06em;text-transform:uppercase;padding:var(--s-4) var(--s-5);",
      "  min-height:32px;white-space:nowrap}",
      ".ptr-b:hover{color:var(--text);border-color:var(--accent-line)}",
      /* Filled ONLY when the director recommended it. The old surface filled
         PROMOTE on every row, which is how a page ends up with no primary
         action: emphasis that is everywhere is emphasis nowhere. */
      ".ptr-b.go{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}",
      ".ptr-b.go:hover{background:var(--accent-hover);border-color:var(--accent-hover);color:var(--accent-fg)}",
      ".ptr-b.bin:hover{color:var(--bad);border-color:var(--bad-line)}",
      ".ptr-b.only-ico{padding:var(--s-4);min-width:32px;justify-content:center}",
      ".ptr-b[aria-expanded='true']{color:var(--text);border-color:var(--accent-line);",
      "  background:var(--surface-3)}",
      ".ptr-b:disabled{opacity:.45;cursor:not-allowed}",

      /* ── the refinements ─────────────────────────────────────────────── */
      ".ptr-drawer{grid-column:1 / -1;display:flex;flex-direction:column;gap:var(--s-4);",
      "  margin-top:var(--s-3);padding-top:var(--s-4);border-top:1px solid var(--line-soft)}",
      ".ptr-drawer[hidden]{display:none}",
      ".ptr-f{display:flex;align-items:center;gap:var(--s-3);flex-wrap:wrap}",
      ".ptr-f > .lb{font-family:var(--mono);font-size:var(--fs-3xs);",
      "  letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text-3);",
      "  min-width:76px}",
      ".ptr-drawer select{background:var(--surface-1);border:1px solid var(--line);",
      "  color:var(--text);font:inherit;font-family:var(--mono);font-size:var(--fs-2xs);",
      "  padding:var(--s-3) var(--s-4);border-radius:var(--r-xs);min-height:30px;",
      "  max-width:min(420px,52vw)}",
      ".ptr-drawer select.needs-seat{border-color:var(--bad);color:var(--bad);",
      "  animation:throb .7s 3}",
      ".ptr-why{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);",
      "  line-height:var(--lh-snug);overflow-wrap:anywhere}",
      ".ptr-why b{color:var(--text-2)}",
      ".ptr-work{display:flex;align-items:center;gap:var(--s-4);flex-wrap:wrap;",
      "  padding:var(--s-3) var(--s-4);border-left:2px solid var(--info);",
      "  background:var(--surface-1);font-family:var(--mono);font-size:var(--fs-3xs);",
      "  color:var(--text-2)}",
      ".ptr-work b{color:var(--text)}",
      ".ptr-work .res{color:var(--good)}",
      ".ptr-empty{font-size:var(--fs-sm);color:var(--text-3);padding:var(--s-5) 0}",

      "@media (max-width:820px){",
      "  .ptr-row{grid-template-columns:1fr}",
      "  .ptr-ev{flex-direction:row;gap:var(--s-4);align-items:center}",
      "  .ptr-do{flex-wrap:wrap}",
      "  .ptr-keys{display:none}}",
    ].join("");
    document.head.appendChild(s);
  }

  /* ── reading the numbers out loud ─────────────────────────────────────── */

  /* Whisper's avg_logprob, in words. Returns null when the line was heard
     cleanly — a warning that fires on every row is wallpaper. Exported so the
     transcript renders the same scale from the same place; two translations of
     one number is how they drift apart. */
  function heard(value) {
    if (value == null) return null;
    const v = Number(value);
    if (!Number.isFinite(v) || v > HEARD_FAIR) return null;
    const tip = `whisper scored this ${v.toFixed(2)} - it is a log probability, `
              + `always 0 or below. Above -0.5 is a clean hearing, -1.0 and `
              + `below is poor. The words may not be the words that were said.`;
    return v <= HEARD_POOR
      ? { word: "probably misheard", tone: "warn", tip }
      : { word: "may be misheard", tone: "", tip };
  }

  const clock = t => {
    const s = Math.max(0, Number(t) || 0);
    return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  };

  const words = s => String(s || "").trim().split(/\s+/).filter(Boolean).length;
  const realEvents = item => (item.events || []).filter(e => e.kind !== "fps");

  /* Did anything in the pipeline find something here? See note 4 in the header:
     this is a conjunction of misses, not a score, so it cannot quietly demote a
     line that any one stage recognised. */
  function quiet(item) {
    if (item.source === "typed" || item.source === "chat") return false;
    const scored = Object.keys((item.classification || {}).scores || {}).length > 0;
    if (scored) return false;
    if (item.seat && item.seat !== "unassigned") return false;
    if (realEvents(item).length) return false;
    const h = Number(item.transcript_confidence);
    if (Number.isFinite(h) && h <= HEARD_POOR) return true;
    return words(item.text) < MIN_WORDS;
  }

  const praise = item =>
    item.kind === "like" || item.director_recommendation === "keep";

  /* ── grouping ─────────────────────────────────────────────────────────── */
  function partition(items) {
    const g = { open: [], quiet: [], praise: [], filed: [], binned: [] };
    for (const item of items) {
      if (item.merged_into_id) { g.binned.push(item); continue; }
      if (item.status === "dismissed") { g.binned.push(item); continue; }
      if (item.status === "promoted") { g.filed.push(item); continue; }
      if (praise(item)) { g.praise.push(item); continue; }
      if (quiet(item)) { g.quiet.push(item); continue; }
      g.open.push(item);
    }
    return g;
  }

  /* ── one row ──────────────────────────────────────────────────────────── */
  function tags(item) {
    const out = [];
    if (item.source === "typed") {
      out.push(`<span class="ptr-tag">typed</span>`);
    } else if (item.source === "chat") {
      out.push(`<span class="ptr-tag info">from chat${
        item.author ? ` - ${E(item.author)}` : ""}</span>`);
    }
    // Bug 1: words, not a log probability. Silent on a clean hearing.
    const h = heard(item.transcript_confidence);
    if (h) out.push(`<span class="ptr-tag ${h.tone}" title="${E(h.tip)}">${E(h.word)}</span>`);
    // Bug 2: a classification with nothing behind it says nothing at all.
    const cls = item.classification || {};
    const scored = Object.keys(cls.scores || {}).length > 0;
    if (scored && cls.kind) {
      out.push(`<span class="ptr-tag" title="lexical first pass - open the `
        + `refinements for the rules that fired">${E(cls.kind)}</span>`);
    }
    if (item.assets && item.assets.length) {
      out.push(`<span class="ptr-tag">${item.assets.length} asset${
        item.assets.length === 1 ? "" : "s"}</span>`);
    }
    const near = realEvents(item);
    if (near.length) {
      out.push(`<span class="lead">while saying this:</span>`);
      for (const e of near.slice(0, 4)) {
        out.push(e.kind === "setting_changed"
          ? `<span class="ptr-tag ev">${E((e.data || {}).prop || (e.data || {}).key
              || "setting")} -&gt; ${E(String((e.data || {}).value))}</span>`
          : `<span class="ptr-tag ev">${E(e.kind)}</span>`);
      }
      if (near.length > 4) out.push(`<span class="lead">+${near.length - 4} more</span>`);
    }
    return out.join("");
  }

  function actions(item) {
    if (item.merged_into_id) {
      return `<span class="ptr-tag info">merged into #${item.merged_into_id}</span>
        <button class="ptr-b only-ico" data-a="jump" data-to="${item.merged_into_id}"
          title="Show the item this was folded into">${icon("select") || "&#8594;"}</button>`;
    }
    if (item.status === "promoted") {
      /* "accepted", not "filed". The item has been judged real and given an
         owner; nothing has been queued. */
      return `<span class="ptr-tag good">accepted</span>
        ${more(item)}`;
    }
    if (item.status === "dismissed") {
      return `<span class="ptr-tag">binned</span>${more(item)}`;
    }
    // Bug 3: the recommendation moves the button, it does not add a badge.
    const suggested = item.director_recommendation === "promote";
    const routed = item.seat && item.seat !== "unassigned";
    const label = routed ? `promote &rarr; ${E(item.seat)}` : "promote&hellip;";
    return `
      <button class="ptr-b${suggested ? " go" : ""}" data-a="promote"
        title="${routed
          ? `File this as work for the ${E(item.seat)} seat.`
          : `Nothing routed this one - pick the seat that owns it.`}${
          suggested ? " The director suggests this." : ""}">
        ${icon("task")}${label}</button>
      <button class="ptr-b bin" data-a="dismiss"
        title="Not worth acting on. It stays in the record.">${icon("close")}bin</button>
      ${more(item)}`;
  }

  const more = item => `<button class="ptr-b only-ico" data-a="more"
      aria-expanded="${drawers.has(item.id) ? "true" : "false"}"
      title="Seat, kind, merge, linked assets, and why it was classified that way"
      aria-label="Refinements for item ${item.id}">${icon("more") || "&hellip;"}</button>`;

  function optionList(values, current) {
    return values.map(v =>
      `<option value="${E(v)}"${v === current ? " selected" : ""}>${E(v)}</option>`).join("");
  }

  /* Built ONLY when open. The asset select alone is three hundred options on
     this project; rendering one per row, closed, for every item in a session
     was a few thousand DOM nodes nobody had asked to see. */
  function drawer(item) {
    if (!drawers.has(item.id)) return `<div class="ptr-drawer" hidden></div>`;
    const cls = item.classification || {};
    const scores = cls.scores || {};
    const scored = Object.keys(scores).length > 0;
    const open = item.status === "new" && !item.merged_into_id;
    const targets = (data.items || []).filter(t =>
      t.id !== item.id && t.status !== "dismissed" && !t.merged_into_id);
    const h = heard(item.transcript_confidence);

    const why = [];
    why.push(`<b>#${item.id}</b> at ${E(clock(item.t))} - ${E(item.status)}`);
    why.push(scored
      ? `classifier: <b>${E(cls.kind)}</b> at ${
          Math.round(Number(cls.confidence || 0) * 100)}% of the weight it scored `
        + `(${E(Object.entries(scores).map(([k, v]) => `${k} ${v}`).join(", "))})`
      : `classifier: no rule matched, so the kind fell back to "note"`);
    why.push(cls.seat && cls.seat !== "unassigned"
      ? `router: <b>${E(cls.seat)}</b>`
      : `router: no seat matched`);
    if (item.transcript_confidence != null) {
      why.push(`whisper: ${Number(item.transcript_confidence).toFixed(2)} log prob`
        + `${h ? ` - <b>${E(h.word)}</b>` : ` - heard cleanly`}`);
    }

    return `<div class="ptr-drawer">
      ${open ? `
      <div class="ptr-f"><span class="lb">route to</span>
        <select data-f="seat">${optionList(SEATS, item.seat || "unassigned")}</select>
        <select data-f="kind">${optionList(KINDS, item.kind)}</select>
        <span class="ptr-why">applied when you promote</span>
      </div>
      <div class="ptr-f"><span class="lb">duplicate of</span>
        <select data-f="merge">
          <option value="">nothing - it stands on its own</option>
          ${targets.map(t => `<option value="${t.id}">#${t.id} ${
            E(String(t.text || "").slice(0, 60))}</option>`).join("")}
        </select>
        <button class="ptr-b" data-a="merge">merge</button>
      </div>` : ""}
      ${(data.asset_options || []).length ? `
      <div class="ptr-f"><span class="lb">about asset</span>
        <select data-f="asset">
          ${(data.asset_options || []).map(a =>
            `<option value="${a.artifact_id}">${E(a.logical_name)}</option>`).join("")}
        </select>
        <button class="ptr-b" data-a="link">link</button>
        ${(item.assets || []).length
          ? `<span class="ptr-why">linked: ${(item.assets || []).map(a =>
              E(a.logical_name)).join(", ")}</span>` : ""}
      </div>` : ""}
      <div class="ptr-why">${why.join("<br>")}</div>
      ${item.work ? `<div class="ptr-work">
        <b>queue #${item.work.id}</b><span>${E(item.work.seat)} - ${E(item.work.status)}</span>
        ${item.work.result ? `<span class="res">${E(item.work.result)}</span>` : ""}
        <button class="ptr-b" data-a="work" data-to="${item.work.id}">show work</button>
      </div>` : item.status === "promoted"
        /* THIS SAID "the queue item is being created". Nothing was being
           created, then or ever - it is the sentence that made a dead end look
           like a queue. What is true: the seat sees this in its brief the next
           time it runs, and somebody still has to give it work to do. */
        ? `<div class="ptr-work"><span>accepted for ${E(item.seat || "a seat")} -
             it shows in that seat's brief. No work item is created by
             promoting; queue one when you want it acted on.</span></div>` : ""}
    </div>`;
  }

  function row(item, settled) {
    const frame = item.frame_rel
      ? `<img src="/api/preview?rel=${encodeURIComponent(item.frame_rel)}"
           alt="the frame captured at ${E(clock(item.t))}">`
      : `<span class="noimg">no frame</span>`;
    return `<article class="ptr-row${settled ? " settled" : ""}${
        cursor === item.id ? " cur" : ""}" id="feedback-${item.id}" data-id="${item.id}">
      <button class="ptr-ev" data-a="seek" data-t="${Number(item.t) || 0}"
        title="Play the recording from ${E(clock(item.t))}">
        ${frame}<span class="t">${E(clock(item.t))}</span></button>
      <div class="ptr-body">
        <p class="ptr-say">${E(item.text)}</p>
        <div class="ptr-meta">${tags(item)}</div>
      </div>
      <div class="ptr-do">${actions(item)}</div>
      ${drawer(item)}
    </article>`;
  }

  /* ── groups ───────────────────────────────────────────────────────────── */
  function rowsHtml(items, settled) {
    return items.map(i => row(i, settled)).join("");
  }

  /* Kind only earns a subhead once the list is long enough that a reader would
     otherwise be scanning for one. See KIND_SUBHEAD_AT. */
  function openHtml(items) {
    if (!items.length) return "";
    const kinds = [...new Set(items.map(i => i.kind))];
    if (items.length < KIND_SUBHEAD_AT || kinds.length < 2) {
      return `<div class="ptr-rows">${rowsHtml(items)}</div>`;
    }
    const order = ["fix", "change", "add", "question", "note", "like"];
    return order.filter(k => kinds.includes(k)).map(k => {
      const of = items.filter(i => i.kind === k);
      return `<div class="ptr-g">
        <div class="ptr-gh">${E(KIND_LABEL[k] || k)}<span class="ptr-n">${of.length}</span></div>
        <div class="ptr-rows">${rowsHtml(of)}</div></div>`;
    }).join("");
  }

  function fold(key, label, why, items, settled) {
    if (!items.length) return "";
    const open = folds.get(key) === true;
    return `<details class="ptr-fold" data-g="${E(key)}"${open ? " open" : ""}>
      <summary>${E(label)}<span class="ptr-n">${items.length}</span>
        <span class="why">${E(why)}</span>
        <span class="caret">${icon("select", 11) || "&rsaquo;"}</span></summary>
      <div class="ptr-rows">${rowsHtml(items, settled)}</div></details>`;
  }

  /* ── the repaint guard ────────────────────────────────────────────────── */
  /* Only what a repaint would actually SHOW. A mutation refetches the session,
     and without this the panel would rebuild every row — including a <select>
     somebody has open and the frame images, which flash — for a payload that
     came back identical. Same device as SeatWork._sig and cinematic's. */
  function _sig() {
    const items = (data && data.items) || [];
    return [
      data && data.session ? data.session.id : "",
      items.map(i => [i.id, i.status, i.seat, i.kind, i.merged_into_id || "",
                      (i.assets || []).length, i.work ? i.work.status : ""].join(":")).join(","),
      [...drawers].sort().join("."),
      [...folds.entries()].map(([k, v]) => k + (v ? "1" : "0")).sort().join("."),
      cursor == null ? "" : String(cursor),
    ].join("|");
  }

  function paint(force) {
    if (!host || !data) return;
    const next = _sig();
    if (!force && next === sig && host.firstChild) return;
    sig = next;

    const items = data.items || [];
    const g = partition(items);
    const todo = g.open.length + g.quiet.length + g.praise.length;

    const body = !items.length
      ? `<div class="ptr-empty">Nothing was extracted from this session - no
           speech was transcribed and no notes were typed.</div>`
      : [
          g.open.length
            ? `<div class="ptr-g"><div class="ptr-gh">to decide
                 <span class="ptr-n">${g.open.length}</span></div>${openHtml(g.open)}</div>`
            : (todo === 0
                ? `<div class="ptr-empty">Everything in this session has been
                     decided.</div>`
                : ""),
          /* Bug 4. Named, counted, and one click from full weight - never
             filtered, because session 19 is nothing but these. */
          fold("quiet", "low signal",
               "nothing heard clearly, no seat, no classifier match, no telemetry",
               g.quiet),
          fold("praise", "praise", "nothing to action - worth reading", g.praise),
          /* NOT "filed as work", WHICH WAS FALSE. Promotion assigns the item
             to a seat and authors NO work item - playtest.promote says so in as
             many words, and a check of this project's database found zero work
             items with source='playtest' behind six items labelled "filed". A
             label that claims work exists is worse than no label: it is the
             reason somebody waits for agents that were never dispatched. */
          fold("filed", "accepted for a seat",
               "assigned to a seat, and waiting to be turned into work - promoting "
               "does not queue anything by itself",
               g.filed, true),
          fold("binned", "binned and merged", "kept in the record, out of the way",
               g.binned, true),
        ].filter(Boolean).join("");

    host.innerHTML = `<div class="ptr">
      <div class="ptr-hd">${icon("playtests", 15)}<h3>triage</h3>
        <span class="ptr-n${todo ? "" : " good"}">${todo ? `${todo} to decide` : "all decided"}</span>
        <span class="ptr-keys"><kbd>j</kbd>/<kbd>k</kbd> move &middot;
          <kbd>p</kbd> promote &middot; <kbd>x</kbd> bin &middot;
          <kbd>enter</kbd> refine &middot; <kbd>space</kbd> play the moment</span></div>
      ${body}</div>`;
  }

  /* ── evidence ─────────────────────────────────────────────────────────── */
  function playAt(t) {
    if (typeof seek === "function") seek(t);
    const stage = document.querySelector(".review-body .video-stage");
    // Sticky already keeps it on screen once it has been passed; this covers
    // the case where the reader has scrolled ABOVE it.
    if (stage && stage.getBoundingClientRect().top < 0) {
      stage.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  /* Shrink the pinned video once triage has scrolled under it.
   *
   * A SENTINEL AND AN IntersectionObserver WAS THE FIRST ATTEMPT AND IT NEVER
   * FIRED ONCE. Measured in the live overlay: a 1px sentinel above the stage,
   * observed with root:#review-body, produced an empty callback log across a
   * full scroll from 0 to the bottom and back. .review-overlay carries
   * backdrop-filter, which makes it a containing block, and the observer's
   * root clip does not end up where the API implies. Rather than fight it,
   * this measures the thing it actually wants to know: sticky has engaged
   * exactly when the stage's top has reached the scroller's top. rAF-throttled
   * and the class is only touched when it changes, so a scroll costs one
   * getBoundingClientRect pair per frame and no layout write. */
  function watchStick() {
    const stage = document.querySelector(".review-body .video-stage");
    const scroller = document.getElementById("review-body");
    if (!stage || !scroller || scroller.dataset.ptrStick === "1") return;
    scroller.dataset.ptrStick = "1";
    let queued = false;
    const check = () => {
      queued = false;
      const live = scroller.querySelector(".video-stage");
      if (!live) return;
      /* AGAINST THE PADDING BOX, NOT THE BORDER BOX. A sticky offset resolves
         against the SCROLLPORT, which is the scroll container's padding box —
         .review-body carries 20px of it, so a stage parked at top:-12px sits 8px
         BELOW the element's own client rect top and a naive comparison there is
         never true. Measured: the difference held at exactly 8.01px while the
         stage was demonstrably parked. */
      const style = getComputedStyle(scroller);
      const port = scroller.getBoundingClientRect().top
                 + (parseFloat(style.paddingTop) || 0);
      const on = live.getBoundingClientRect().top <= port - 1;
      if (on !== live.classList.contains("ptr-stuck")) {
        live.classList.toggle("ptr-stuck", on);
      }
    };
    scroller.addEventListener("scroll", () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(check);
    }, { passive: true });
    check();
  }

  /* ── mutations ────────────────────────────────────────────────────────── */
  const itemById = id => (data.items || []).find(i => i.id === Number(id)) || null;
  const rowEl = id => host && host.querySelector(`.ptr-row[data-id="${id}"]`);
  const field = (id, name) => {
    const r = rowEl(id);
    return r ? r.querySelector(`[data-f="${name}"]`) : null;
  };

  /* Refetch and repaint OUR SUBTREE. openSession() rebuilds the whole overlay,
     which throws away the <video> mid-playback and the reader's scroll — the
     thing that made triaging three items feel like three separate sittings. */
  async function reload() {
    const fresh = await readJSON(`/api/playtest/${data.session.id}`, null);
    if (fresh && !fresh.__error && fresh.items) {
      data = fresh;
      paint(true);
    }
    try { if (window.pollState) pollState(); } catch (e) { /* shell not up */ }
    try { if (window.pollQueue) pollQueue(); } catch (e) { /* shell not up */ }
  }

  async function promote(id) {
    const item = itemById(id);
    if (!item) return;
    let seat = item.seat;
    const picker = field(id, "seat");
    if (picker) seat = picker.value;
    if (!seat || seat === "unassigned") {
      // An unrouted item stays visibly unrouted rather than being filed under
      // whichever seat the browser happened to select first.
      drawers.add(Number(id));
      paint();
      const now = field(id, "seat");
      if (now) {
        now.classList.add("needs-seat");
        now.focus();
        setTimeout(() => now.classList.remove("needs-seat"), 2500);
      }
      say("nothing routed this one - pick the seat that owns it, then promote");
      return;
    }
    const kindEl = field(id, "kind");
    busy = true;
    const r = await mutate(`/api/playtest/items/${id}/promote`, {
      body: { seat, kind: kindEl ? kindEl.value : item.kind },
      ok: `#${id} accepted for ${seat} - not queued yet` });
    busy = false;
    if (!r.ok) return;
    drawers.delete(Number(id));
    await reload();
  }

  async function dismiss(id) {
    busy = true;
    const r = await mutate(`/api/playtest/items/${id}/dismiss`, { ok: `#${id} binned` });
    busy = false;
    if (!r.ok) return;
    drawers.delete(Number(id));
    await reload();
  }

  async function merge(id) {
    const select = field(id, "merge");
    const target = select ? select.value : "";
    if (!target) { say("pick the item this one duplicates first"); if (select) select.focus(); return; }
    // One-way on the backend: it writes status='dismissed' plus merged_into_id
    // and there is no unmerge endpoint. So it confirms rather than firing on a
    // single click.
    const label = select.options[select.selectedIndex]
      ? select.options[select.selectedIndex].text : `#${target}`;
    let go = true;
    try {
      go = await askConfirm({
        title: `Merge #${id} into ${label}?`,
        body: `#${id} stops being actionable on its own and its text lives on `
            + `under the target. There is no undo yet - this cannot be reversed `
            + `from the dashboard.`,
        ok: "merge", danger: true });
    } catch (e) { go = window.confirm(`Merge #${id} into ${label}? This cannot be undone.`); }
    if (!go) return;
    busy = true;
    const r = await mutate(`/api/playtest/items/${id}/merge`, {
      body: { target_id: Number(target) }, ok: `#${id} merged into #${target}` });
    busy = false;
    if (!r.ok) return;
    drawers.delete(Number(id));
    await reload();
  }

  async function link(id) {
    const select = field(id, "asset");
    const artifactId = select ? select.value : "";
    if (!artifactId) { say("pick an asset to link"); return; }
    busy = true;
    const r = await mutate(`/api/artifacts/${artifactId}/feedback/${id}`,
      { body: {}, ok: "asset linked" });
    busy = false;
    if (!r.ok) return;
    await reload();
  }

  function jump(id) {
    const target = rowEl(id);
    if (!target) return;
    cursor = Number(id);
    paint();
    const painted = rowEl(id);
    if (!painted) return;
    // A row inside a folded group cannot be scrolled to while it is folded.
    const box = painted.closest("details.ptr-fold");
    if (box && !box.open) { box.open = true; folds.set(box.dataset.g, true); }
    painted.scrollIntoView({ behavior: "smooth", block: "center" });
    painted.classList.add("flash");
    setTimeout(() => painted.classList.remove("flash"), 1400);
  }

  /* ── events ───────────────────────────────────────────────────────────── */
  function onClick(event) {
    const fold = event.target.closest("details.ptr-fold > summary");
    if (fold) {
      const box = fold.parentElement;
      // The click has not toggled it yet, so record the state it is heading to.
      folds.set(box.dataset.g, !box.open);
      sig = "";                       // the fold is in the signature
      return;
    }
    const button = event.target.closest("[data-a]");
    if (!button || !host.contains(button)) return;
    const row = button.closest(".ptr-row");
    const id = row ? Number(row.dataset.id) : 0;
    const act = button.dataset.a;
    if (act === "seek") { cursor = id; paint(); playAt(Number(button.dataset.t)); return; }
    if (act === "more") {
      if (drawers.has(id)) drawers.delete(id); else drawers.add(id);
      cursor = id;
      paint();
      return;
    }
    if (act === "promote") { cursor = id; promote(id); return; }
    if (act === "dismiss") { cursor = id; dismiss(id); return; }
    if (act === "merge") { merge(id); return; }
    if (act === "link") { link(id); return; }
    if (act === "jump") { jump(Number(button.dataset.to)); return; }
    if (act === "work") {
      const workId = button.dataset.to;
      try { closeReview(); } catch (e) { /* shell not up */ }
      const card = document.getElementById(`work-${workId}`);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
  }

  /* Every row the keyboard can reach, in the order they are painted. Rows
     inside a folded group are genuinely not on screen and are skipped. */
  function visibleRows() {
    if (!host) return [];
    return [...host.querySelectorAll(".ptr-row")].filter(r => {
      const box = r.closest("details.ptr-fold");
      return !box || box.open;
    });
  }

  function move(delta) {
    const rows = visibleRows();
    if (!rows.length) return;
    let at = rows.findIndex(r => Number(r.dataset.id) === cursor);
    at = at < 0 ? (delta > 0 ? 0 : rows.length - 1)
                : Math.min(rows.length - 1, Math.max(0, at + delta));
    cursor = Number(rows[at].dataset.id);
    paint();
    const painted = rowEl(cursor);
    if (painted) painted.scrollIntoView({ block: "nearest" });
  }

  function onKey(event) {
    if (!host || !host.isConnected || busy) return;
    const overlay = document.getElementById("review-overlay");
    if (!overlay || overlay.style.display === "none") return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    // Never steal a key from something that collects text, from an open select,
    // or from the video's own controls.
    const on = event.target;
    if (on && (on.closest("input, textarea, select, [contenteditable='true'], video")
               || on.isContentEditable)) return;

    const key = event.key;
    if (key === "j" || key === "ArrowDown") { event.preventDefault(); move(1); return; }
    if (key === "k" || key === "ArrowUp") { event.preventDefault(); move(-1); return; }
    if (cursor == null) return;
    const item = itemById(cursor);
    if (!item) return;
    if (key === " ") { event.preventDefault(); playAt(Number(item.t) || 0); return; }
    if (key === "Enter") {
      event.preventDefault();
      if (drawers.has(cursor)) drawers.delete(cursor); else drawers.add(cursor);
      paint();
      return;
    }
    if (item.status !== "new" || item.merged_into_id) return;
    if (key === "p") { event.preventDefault(); promote(cursor); return; }
    if (key === "x") { event.preventDefault(); dismiss(cursor); return; }
  }

  /* ── entry point ──────────────────────────────────────────────────────── */
  /* Called by openSession once per session load. State that belongs to the
     PREVIOUS session (which drawer was open, where the cursor was) is dropped;
     state that belongs to this one is rebuilt from the payload. */
  function mount(container, payload, options) {
    injectStyle();
    const sameSession = data && payload && data.session && payload.session
                     && data.session.id === payload.session.id;
    host = container;
    data = payload;
    seek = (options || {}).seek || window.seekReview || null;
    if (!sameSession) {
      drawers.clear();
      folds.clear();
      cursor = null;
    }
    sig = "";
    paint(true);
    watchStick();
    if (!keysBound) {
      document.addEventListener("click", onClick);
      document.addEventListener("keydown", onKey);
      keysBound = true;
    }
  }

  return { mount, heard, clock, quiet };
})();
