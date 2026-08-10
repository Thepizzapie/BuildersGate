/* BGIcon — the icon system.
 *
 * Everything here descends from one mark. Builders Gate is a pipeline of gates:
 * the cut line, the QA gate, the canon gate, the approval gate, the lane hook.
 * Work does not stop at them, it PASSES THROUGH them — so the mark is a tall
 * post, a broken post, and a chevron moving between the two. That is the whole
 * vocabulary the icons are drawn from: uprights, gaps, and 45-degree movement.
 *
 * Before this the app used ~20 unrelated Unicode glyphs (⛰ ◈ ▲ ◆ ✎ ♪ ▦ ⌖ ⬡ ⚙)
 * as icons, and the brand mark was the two characters `⌐¬`. Those render from
 * whatever symbol font the OS falls back to, so stroke weight, optical size and
 * baseline all drifted per machine — no amount of CSS makes them agree. Real
 * geometry on a shared grid is the only fix.
 *
 * GRID: 24x24, content inset to 3..21, stroke 1.75, square caps, miter joins.
 * Curves are rationed — this is an architectural set, not a friendly one.
 *
 *   BGIcon("studio")                  -> <svg> string, currentColor
 *   BGIcon("studio", {size:20})       -> at a size
 *   BGIcon.logo({size:28})            -> the full mark
 *   BGIcon.upgrade(root)              -> swap [data-icon] placeholders in place
 */
(function () {
  /* The MARK's ember, which is not the interface's. --brand-chevron aliases
     --ember on the two original grounds and pins real ember on orbit, whose
     accent is not orange — a theme may restyle the UI, not the logo. */
  const EMBER = "var(--brand-chevron)";

  /* Each entry is the inner geometry of a 24x24 icon. `e` marks the one stroke
     drawn in ember — the moving part, the live part, the thing under review. */
  const P = {
    /* ---- navigation ---- */
    overview:  `<rect x="3.5" y="4.5" width="17" height="15" rx="1"/><path d="M3.5 9.5 H20.5"/><path class="e" d="M8 14.5 H12"/>`,
    agents:    `<path d="M5 20 V13 M12 20 V8"/><path class="e" d="M19 20 V4"/>`,
    studio:    `<rect x="3.5" y="5.5" width="7" height="6" rx="1"/><rect x="13.5" y="12.5" width="7" height="6" rx="1"/><path class="e" d="M10.5 8.5 H13.5 V12.5"/>`,
    seats:     `<path d="M4 6.5 H20 M4 12 H20 M4 17.5 H20"/><path class="e" d="M4 12 H10"/>`,
    playtests: `<rect x="3.5" y="5" width="17" height="12" rx="1"/><path d="M9 20 H15"/><path class="e" d="M10.5 8.5 L14.5 11 L10.5 13.5 Z"/>`,
    assets:    `<path d="M12 3.5 L20.5 8 L12 12.5 L3.5 8 Z"/><path class="e" d="M3.5 12.5 L12 17 L20.5 12.5"/>`,
    atlas:     `<rect x="3.5" y="3.5" width="7" height="7"/><rect x="13.5" y="3.5" width="7" height="7"/><rect x="3.5" y="13.5" width="7" height="7"/><rect class="e" x="13.5" y="13.5" width="7" height="7"/>`,
    world:     `<circle cx="6" cy="7" r="2.5"/><circle cx="18" cy="7" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path class="e" d="M8.5 7 H15.5 M7 9.5 L11 15.5 M17 9.5 L13 15.5"/>`,
    timeline:  `<path d="M3.5 12 H20.5"/><path d="M7 9 V15 M17 9 V15"/><path class="e" d="M12 7 V17"/>`,
    // Three sliders, one of them ember: the switches that decide how much of the
    // floor runs without you. Drawn here rather than inline in index.html
    // because every rail item is required to NAME an icon — an inline glyph
    // cannot be restyled with the set and test_icons enforces exactly that.
    settings:  `<path d="M4 6.5 H20 M4 12 H20 M4 17.5 H20"/><path d="M9 4.5 V8.5 M7 15.5 V19.5"/><path class="e" d="M15 10 V14"/>`,

    /* ---- seats ---- */
    director:  `<circle cx="12" cy="12" r="7.5"/><path class="e" d="M12 2.5 V7 M12 17 V21.5 M2.5 12 H7 M17 12 H21.5"/>`,
    narrative: `<path d="M4.5 6.5 H19.5 M4.5 11 H19.5 M4.5 15.5 H14"/><path class="e" d="M4.5 20 H10"/>`,
    gameplay:  `<path d="M8.5 4 V9 H3.5 V15 H8.5 V20 H15.5 V15 H20.5 V9 H15.5 V4 Z"/><path class="e" d="M12 9 V15"/>`,
    tech:      `<path d="M8.5 7.5 L4 12 L8.5 16.5"/><path d="M15.5 7.5 L20 12 L15.5 16.5"/><path class="e" d="M13.5 5 L10.5 19"/>`,
    art:       `<rect x="3.5" y="3.5" width="17" height="17" rx="1.5"/><path class="e" d="M3.5 16 L9 10.5 L14 15.5 L17.5 12 L20.5 15"/>`,
    audio:     `<path d="M4 10 V14 M8 7 V17 M20 10 V14"/><path class="e" d="M12 4 V20 M16 8 V16"/>`,
    /* A clapperboard: the body, the hinged slate, and the play triangle as the
       emphasis stroke. Reads at 16px, which the film-strip alternative (two
       rails of tiny perforations) does not. */
    cinematic: `<path d="M3.5 9.5 h17 v10 a1 1 0 0 1 -1 1 h-15 a1 1 0 0 1 -1 -1 z"/><path d="M3.5 9.5 L5 4.5 L20.5 6 L19 9.5 z M9 5 L7.5 9.5 M14 5.5 L12.5 9.5"/><path class="e" d="M10.5 12.5 L15 15 L10.5 17.5 z"/>`,
    qa:        `<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 L20.5 20.5"/><path class="e" d="M7.5 10.5 L9.75 12.75 L13.5 8"/>`,

    /* ---- workflow steps ---- */
    task:      `<circle cx="12" cy="12" r="8"/><path class="e" d="M12 7.5 V12.5 M12 15.5 V16.5"/>`,
    reference: `<rect x="3.5" y="4.5" width="17" height="15" rx="1"/><path class="e" d="M3.5 15 L9 10 L13 14 M13.5 8.5 h3"/>`,
    concept:   `<path d="M5 19 L8 11 L16.5 4.5 L19.5 7.5 L13 16 Z"/><path class="e" d="M5 19 L9.5 17.5"/>`,
    anchor:    `<circle cx="12" cy="6.5" r="3"/><path d="M12 9.5 V20"/><path class="e" d="M6 14 A6 6 0 0 0 18 14"/>`,
    animation: `<rect x="3.5" y="6.5" width="9" height="11" rx="1"/><path d="M15 8 V16 M18 9.5 V14.5"/><path class="e" d="M20.5 11 V13"/>`,
    edit:      `<path d="M4 20 L4.8 16 L16 4.8 L19.2 8 L8 19.2 Z"/><path class="e" d="M13.5 7.5 L16.5 10.5"/>`,
    sheet:     `<rect x="3.5" y="4.5" width="17" height="15" rx="1"/><path d="M9 4.5 V19.5 M15 4.5 V19.5 M3.5 12 H20.5"/><path class="e" d="M3.5 4.5 H9 V12 H3.5 Z"/>`,
    rig:       `<path d="M12 3.5 L20.5 8 V16 L12 20.5 L3.5 16 V8 Z"/><path class="e" d="M12 12 L20.5 8 M12 12 V20.5 M12 12 L3.5 8"/>`,
    background:`<path d="M3.5 17 L9 9.5 L13 15 L16 11 L20.5 17 Z"/><circle class="e" cx="16.5" cy="6.5" r="2.5"/>`,
    parallax:  `<path d="M3.5 8.5 H20.5 M3.5 15.5 H20.5"/><path class="e" d="M7 12 H17"/>`,
    tileset:   `<path d="M3.5 3.5 H20.5 V20.5 H3.5 Z M9.5 3.5 V20.5 M15 3.5 V20.5 M3.5 9 H20.5 M3.5 15 H20.5"/>`,
    props:     `<rect x="4" y="12" width="7" height="7"/><rect x="13" y="12" width="7" height="7"/><rect class="e" x="8.5" y="5" width="7" height="7"/>`,
    stage:     `<path d="M3.5 18.5 H20.5 M6 18.5 V11 H18 V18.5"/><path class="e" d="M12 11 V5.5 M9 8 H15"/>`,
    model:     `<path d="M12 3.5 L20.5 8 V16 L12 20.5 L3.5 16 V8 Z"/><path d="M3.5 8 L12 12.5 L20.5 8 M12 12.5 V20.5"/>`,
    gltf:      `<path d="M12 3.5 L20.5 8 V16 L12 20.5 L3.5 16 V8 Z"/><path class="e" d="M8 12 H16 M12.5 8.5 L16 12 L12.5 15.5"/>`,
    sprites:   `<rect x="3.5" y="3.5" width="7" height="7"/><rect x="13.5" y="3.5" width="7" height="7"/><rect x="3.5" y="13.5" width="7" height="7"/><path class="e" d="M15 15 L19 19 M19 15 L15 19"/>`,
    verify:    `<path d="M12 3.5 L20 7 V12.5 C20 17 16.5 19.8 12 20.5 C7.5 19.8 4 17 4 12.5 V7 Z"/><path class="e" d="M8.5 12 L11 14.5 L15.5 9.5"/>`,
    consistency:`<circle cx="12" cy="12" r="8"/><path class="e" d="M12 4 A8 8 0 0 1 12 20 Z"/>`,
    select:    `<rect x="3.5" y="4.5" width="17" height="15" rx="1"/><path class="e" d="M8 12 L11 15 L16.5 9"/>`,
    variants:  `<rect x="3.5" y="8.5" width="9" height="11" rx="1"/><path d="M7 5.5 H16 V16" /><path class="e" d="M10.5 2.5 H19.5 V13"/>`,
    note:      `<path d="M4.5 3.5 H19.5 V15 L15 20.5 H4.5 Z"/><path class="e" d="M19.5 15 H15 V20.5"/>`,

    /* ---- actions / status ---- */
    run:       `<path class="e" d="M7 4.5 L19 12 L7 19.5 Z"/>`,
    stop:      `<rect class="e" x="6.5" y="6.5" width="11" height="11"/>`,
    gate:      `<path d="M5 3.5 V20.5"/><path d="M19 3.5 V9 M19 15 V20.5"/><path class="e" d="M10 8 L14.5 12 L10 16"/>`,
    lock:      `<rect x="4.5" y="10.5" width="15" height="10" rx="1"/><path class="e" d="M8 10.5 V7.5 A4 4 0 0 1 16 7.5 V10.5"/>`,
    spend:     `<circle cx="12" cy="12" r="8"/><path class="e" d="M14.5 9 A3 3 0 0 0 9.5 10.5 C9.5 13.5 14.5 11.5 14.5 14.5 A3 3 0 0 1 9.5 15.5 M12 6.5 V17.5"/>`,
    doctor:    `<path d="M12 3.5 L20 7 V12.5 C20 17 16.5 19.8 12 20.5 C7.5 19.8 4 17 4 12.5 V7 Z"/><path class="e" d="M12 8.5 V15.5 M8.5 12 H15.5"/>`,
    // Holds the rail open. A pushpin, drawn in the set's vocabulary: a bar, a
    // pair of uprights, and the ember stroke on the part that goes in — the
    // one bit of the shape that is doing the holding.
    pin:       `<path d="M8 3.5 H16 M9.5 3.5 V9 L6.5 12 V13.5 H17.5 V12 L14.5 9 V3.5"/><path class="e" d="M12 13.5 V20.5"/>`,

    /* ---- colour ground ----
       The rail's three-way theme toggle. It was three words until now, because
       the glyphs it had before were unicode pictographs out of whatever symbol
       font the OS fell back to — geometry on this grid is the thing that ban
       was pointing AT, not away from. Sun, crescent, and a disc split down the
       middle for "whatever the OS says".

       ORBIT is the fourth: a body, a ring around it, and a gap in the ring with
       the ember stroke closing part of it. That is the set's own vocabulary —
       uprights, gaps, and the moving part in ember — rather than a fourth
       weather symbol, and it is the one shape on the row that reads as a
       PLACE you are in rather than a light level. r=8 keeps the ring inside
       the 3..21 inset once the 1.75 stroke is counted. */
    theme_light:`<circle cx="12" cy="12" r="4.5"/><path class="e" d="M12 3.5 V6 M12 18 V20.5 M3.5 12 H6 M18 12 H20.5 M6 6 L7.8 7.8 M16.2 16.2 L18 18 M18 6 L16.2 7.8 M7.8 16.2 L6 18"/>`,
    theme_dark: `<path d="M19.82 15.32 A8.5 8.5 0 1 1 8.68 4.18 A8.5 8.5 0 0 0 19.82 15.32 Z"/><path class="e" d="M18.5 4 V7 M17 5.5 H20"/>`,
    theme_auto: `<circle cx="12" cy="12" r="8"/><path class="e" d="M12 4 V20"/><path d="M6.2 8.8 H9 M4.6 12 H9 M6.2 15.2 H9"/>`,
    theme_orbit:`<circle cx="12" cy="12" r="3.25"/><path d="M6.34 6.34 A8 8 0 1 0 17.66 6.34"/><path class="e" d="M6.34 6.34 A8 8 0 0 1 12 4"/>`,

    /* ---- viewport / atlas toolbar ----
       The Atlas toolbar, the sprite editor and the audio lab were the three
       modules the glyph ban never covered, so they were still drawing
       `⊡ fit`, `⌫`, `▲z`, `↶`, `↓png` and bare `□`/`○` out of whatever symbol
       font the OS had. Everything below is on the 24 grid with the rest.

       These render at 16px in dense toolbars, not at 24 — so they are drawn
       coarser than the rail icons: fewer parts, bigger gaps, and no detail
       that survives only at full size. */
    fit:        `<path d="M3.5 8 V3.5 H8 M16 3.5 H20.5 V8 M20.5 16 V20.5 H16 M8 20.5 H3.5 V16"/><rect class="e" x="8.5" y="8.5" width="7" height="7"/>`,
    zoom_in:    `<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 L20.5 20.5"/><path class="e" d="M10.5 7.5 V13.5 M7.5 10.5 H13.5"/>`,
    zoom_out:   `<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 L20.5 20.5"/><path class="e" d="M7.5 10.5 H13.5"/>`,
    // The ember plate fills the centre cell EXACTLY — its four edges land on
    // grid lines. An earlier draw had it straddling two cells, which is what
    // "not snapped" looks like, on the icon for snapping.
    snap_grid:  `<path d="M3.5 9 H20.5 M3.5 15 H20.5 M9 3.5 V20.5 M15 3.5 V20.5"/><rect class="e" x="9" y="9" width="6" height="6"/>`,
    // A pair. `hidden` is `visible` INTERRUPTED — same footprint, top half of
    // the lens gone, ember lid where the pupil was. Not a slash through an
    // eye: the set already has a word for "this is cut", and it is a gap.
    visible:    `<path d="M3.5 12 L8 7.5 H16 L20.5 12 L16 16.5 H8 Z"/><rect class="e" x="10" y="10" width="4" height="4"/>`,
    hidden:     `<path d="M3.5 12 L8 16.5 H16 L20.5 12"/><path class="e" d="M6 11 H18"/>`,
    place:      `<rect x="7.5" y="3.5" width="9" height="7"/><path d="M3.5 20.5 H20.5"/><path class="e" d="M12 12 V17.5 M9 14.5 L12 17.5 L15 14.5"/>`,
    delete:     `<path d="M4.5 6.5 H19.5 M9 6.5 V4 H15 V6.5"/><path d="M6.5 6.5 L7.5 20.5 H16.5 L17.5 6.5"/><path class="e" d="M12 10 V17"/>`,
    // Layer order, not scroll: three plates seen edge-on, and the ember one is
    // the plate that moves — the chevron is where it is going, in the gap it
    // is about to occupy.
    z_up:       `<path d="M4 15 H20 M4 19.5 H20"/><path class="e" d="M4 10.5 H20 M8.5 7 L12 3.5 L15.5 7"/>`,
    z_down:     `<path d="M4 4.5 H20 M4 9 H20"/><path class="e" d="M4 13.5 H20 M8.5 17 L12 20.5 L15.5 17"/>`,
    // Mirrored pair, and the mirror has to survive 16px: the arrowhead sits on
    // opposite EDGES of the box, not just at opposite ends of a curve. Elbows
    // rather than arcs — a 1.75 arc at 16px is four grey pixels.
    undo:       `<path d="M20 19.5 V12 H4"/><path class="e" d="M8.5 7.5 L4 12 L8.5 16.5"/>`,
    redo:       `<path d="M4 19.5 V12 H20"/><path class="e" d="M15.5 7.5 L20 12 L15.5 16.5"/>`,
    // The frame is a picture; the ember chevron leaves through a gap in its
    // edge. Same figure as `gate` — work passing through — which is what an
    // export is. Drawn this way rather than as a down-arrow because a
    // down-arrow under a plate is already `place`, one button along.
    export_image:`<path d="M19.5 8.5 V4.5 H4.5 V19.5 H19.5 V15.5"/><path d="M4.5 17 L9 12.5 L12 15.5"/><path class="e" d="M16 8.5 L20.5 12 L16 15.5"/>`,
    // A fidelity switch, not a visibility one: one frame, one subject, drawn
    // twice — hatched schematic on the left, the real thing in ember on the
    // right.
    real_preview:`<rect x="3.5" y="5.5" width="17" height="13" rx="1"/><path d="M12 5.5 V18.5 M3.5 14 L12 5.5 M3.5 18.5 L10.5 11.5"/><path class="e" d="M12 18.5 L15.5 13.5 L20.5 18.5"/>`,

    /* ---- sprite editor ----
       `brush` and `eyedropper` are the same 45-degree shaft with the mass at
       opposite ends — fat ember tip vs fat open bulb. That inversion is what
       tells them apart in a toolbar at 16px, where both are otherwise just a
       diagonal. */
    brush:      `<path d="M19.5 5.5 L9 16"/><path d="M10.5 11.5 L13.5 14.5"/><path class="e" d="M7 14.5 L5 20 L10.5 18 Z"/>`,
    eraser:     `<path d="M8 17.5 L3.5 13 L13 3.5 L17.5 8 L8 17.5 Z"/><path d="M6 20.5 H20.5"/><path class="e" d="M8.25 8.25 L12.75 12.75"/>`,
    // Open at the mouth — the gap is what makes the diamond a bucket instead
    // of the rotated block that `eraser` is two buttons away.
    fill:       `<path d="M8 4.5 L4 11 L11 18 L18 11 L14 4.5"/><path class="e" d="M19.5 12.5 L21 16 L19.5 19 L18 16 Z"/>`,
    eyedropper: `<path d="M9.25 11.25 L12.75 14.75 L17.75 9.75 L14.25 6.25 Z"/><path d="M11 13 L7 17"/><path class="e" d="M7 17 L4 20"/>`,
    // Current frame plain, the ghosts receding behind it. Ember on the nearest
    // ghost, because the onion skin IS the ghost — putting it on the frame
    // made the whole toggle shout in a row of eight.
    onion:      `<rect x="10.5" y="6" width="10" height="12"/><path d="M4.5 10 V14"/><path class="e" d="M7.5 8 V16"/>`,
    frame_prev: `<path d="M6 4.5 V19.5"/><path class="e" d="M19 5.5 L9 12 L19 18.5 Z"/>`,
    frame_next: `<path d="M18 4.5 V19.5"/><path class="e" d="M5 5.5 L15 12 L5 18.5 Z"/>`,
    flip_h:     `<path d="M12 3.5 V20.5"/><path d="M9.5 6.5 V17.5 L4 12 Z"/><path class="e" d="M14.5 6.5 V17.5 L20 12 Z"/>`,
    flip_v:     `<path d="M3.5 12 H20.5"/><path d="M6.5 9.5 H17.5 L12 4 Z"/><path class="e" d="M6.5 14.5 H17.5 L12 20 Z"/>`,
    // The two shape tools, built from one idea: the geometry you dragged out,
    // plain, with the ember on the DRAG HANDLES at its two ends. That shared
    // handle language is what makes them read as a pair and keeps them out of
    // the way of the four freehand tools next to them — `brush` and
    // `eyedropper` are diagonals too, but their mass sits at one end, and
    // these are symmetric.
    //
    // The ember is deliberately NOT on the shape itself. An ember-filled box
    // is `stop`, three buttons away on the same screen; an ember box with two
    // grey corners is `stop` with dirt on it. Handles also happen to be the
    // literal moving part, which is what the accent is for.
    line:       `<path d="M5.5 18.5 L18.5 5.5"/><path class="e" d="M3.5 16.5 H7.5 V20.5 H3.5 Z M16.5 3.5 H20.5 V7.5 H16.5 Z"/>`,
    rect:       `<rect x="6" y="6.5" width="12" height="11"/><path class="e" d="M4 4.5 H8 V8.5 H4 Z M16 15.5 H20 V19.5 H16 Z"/>`,

    /* ---- audio lab ----
       `run` and `stop` are reused for play and stop; these are the rest of the
       transport plus the two mixer states. */
    pause:      `<path class="e" d="M9 5.5 V18.5 M15 5.5 V18.5"/>`,
    record:     `<circle cx="12" cy="12" r="8.5"/><circle class="e" cx="12" cy="12" r="4"/>`,
    loop:       `<path d="M5 8.5 H19 V15.5 H5 Z"/><path class="e" d="M9 5 L12 8.5 L9 12 M15 12 L12 15.5 L15 19"/>`,
    // `mute` and `solo` sit on the same mixer row, so they are built from
    // different parts on purpose: mute is the speaker with the output struck,
    // solo is three posts of which only one is unbroken. Nothing about their
    // silhouettes rhymes.
    mute:       `<path d="M3.5 9.5 H7 L11.5 5.5 V18.5 L7 14.5 H3.5 Z"/><path class="e" d="M15 9 L20.5 15 M20.5 9 L15 15"/>`,
    solo:       `<path d="M6 4.5 V9 M6 15 V19.5 M18 4.5 V9 M18 15 V19.5"/><path class="e" d="M12 4.5 V19.5"/>`,
    // Not `audio` again: the ember here is the ZERO AXIS running through the
    // bars, which is the one thing a waveform has and a level meter does not.
    waveform:   `<path class="e" d="M3.5 12 H20.5"/><path d="M6 8 V16 M9 4.5 V19.5 M12 7 V17 M15 5.5 V18.5 M18 9.5 V14.5"/>`,
    // Two handles, the kept span in ember, and the discarded ends left as
    // stubs across a gap. The gaps are the whole icon — without them this is
    // `timeline`.
    trim:       `<path d="M3.5 12 H6 M18 12 H20.5"/><path d="M8 6.5 V17.5 M16 6.5 V17.5"/><path class="e" d="M8 12 H16"/>`,
    // Back to the beginning: the start post, the rail the head travelled, and
    // the ember chevron arriving at the post. Deliberately NOT the media-player
    // glyph — bar-plus-solid-triangle is already `frame_prev`, and drawn that
    // way the two were the same icon. An open chevron against a post reads as
    // "returns to the start"; a solid triangle reads as "step".
    skip_start: `<path d="M5 4.5 V19.5"/><path d="M13 12 H20.5"/><path class="e" d="M13 7 L8 12 L13 17"/>`,

    /* ---- atlas scene editor ---- */
    // An indented staircase, ember on the deepest branch — the node you are
    // standing in. Flush-left branches off a single spine drew a letter E.
    outline:    `<path d="M4 5 H20 M7 5 V12 H20"/><path class="e" d="M10 12 V19 H20"/>`,
    // A node role, not a tool: two bodies brought face to face with the ember
    // contact line BROKEN between them. The break is the impact, and it is the
    // same broken-post figure the whole set is built from. Editors draw
    // collision hulls as dashed outlines, so the reading is doubly earned.
    collision:  `<path d="M3.5 6 H9.5 V18 H3.5"/><path d="M20.5 6 H14.5 V18 H20.5"/><path class="e" d="M12 5.5 V9 M12 11 V13 M12 15 V18.5"/>`,
    // This thing, again — the SAME plate at the same size, offset, with the
    // ember on the new one. Not a clipboard, not two different documents.
    duplicate:  `<rect x="3.5" y="3.5" width="12" height="12"/><rect class="e" x="8.5" y="8.5" width="12" height="12"/>`,
    rebuild:    `<rect x="4.5" y="13" width="15" height="7"/><path class="e" d="M20 8.5 H5 M8.5 5 L5 8.5 L8.5 12"/>`,
    // Two 45-degree strokes crossing. This IS the vocabulary — `mute` and
    // `sprites` already cross a pair — so the X is native here rather than
    // borrowed. Left un-embered on purpose: it is picker chrome, it should be
    // the quietest thing on screen, and the accent means "live", which a
    // dismiss control is not.
    close:      `<path d="M6 6 L18 18 M18 6 L6 18"/>`,
    // Many into one: rows folding from BOTH sides onto a single ember rail.
    // The chevrons point inward, which is what separates this from `flip_v`,
    // where they point away from the axis.
    collapse_all:`<path d="M7.5 4.5 L12 9 L16.5 4.5 M7.5 19.5 L12 15 L16.5 19.5"/><path class="e" d="M3.5 12 H20.5"/>`,
    // An emitter, the cone it throws, and the ember lit ground. `real_preview`
    // is a framed picture and `theme_light` is a sun with rays; this is neither
    // — it is light ARRIVING somewhere, which is what the toggle switches on.
    lighting:   `<path d="M9 4.5 H15"/><path d="M9 4.5 L4.5 19.5 M15 4.5 L19.5 19.5"/><path class="e" d="M4.5 19.5 H20.5"/>`,

    /* ---- generic chrome ----
       These two were being covered by `seats` and `assets`. Reusing `run` for
       play is fine because the meaning is the same; reusing a RAIL DESTINATION
       for an action is a category error — an overflow menu wearing the seats
       icon reasonably reads as "something to do with seats". Hence two cheap
       generics rather than a mislabel that spreads. */
    more:       `<path d="M4 10.25 H7.5 V13.75 H4 Z M16.5 10.25 H20 V13.75 H16.5 Z"/><path class="e" d="M10.25 10.25 H13.75 V13.75 H10.25 Z"/>`,
    // Out of the tray and away. `export_image` is the gate figure with a
    // picture in it and leaves to the RIGHT; this one leaves UP, so the two do
    // not collide when a module offers both.
    export:     `<path d="M4.5 14 V19.5 H19.5 V14"/><path class="e" d="M12 15.5 V5.5 M8 9.5 L12 5.5 L16 9.5"/>`,
  };

  // `brush` used to alias `art` — the seat icon. It is a real icon now (the
  // sprite editor's paint tool), and an alias wins over P, so leaving the row
  // here would have made the new geometry unreachable. Nothing called
  // BGIcon("brush") for the seat icon; `art` still resolves it.
  const ALIASES = { anim: "animation", ref: "reference", qa_seat: "qa" };

  function BGIcon(name, opts) {
    opts = opts || {};
    const key = ALIASES[name] || name;
    const body = P[key];
    const size = opts.size || 18;
    if (!body) {
      // A missing icon must be visibly missing, not silently blank — a blank
      // slot is how twenty mismatched glyphs survived this long.
      return `<svg class="bgi bgi-missing" width="${size}" height="${size}" viewBox="0 0 24 24"
        aria-hidden="true"><rect x="4" y="4" width="16" height="16" fill="none"
        stroke="currentColor" stroke-dasharray="2 2" opacity=".5"/></svg>`;
    }
    const label = opts.label
      ? `role="img" aria-label="${String(opts.label).replace(/"/g, "&quot;")}"`
      : `aria-hidden="true"`;
    return `<svg class="bgi" width="${size}" height="${size}" viewBox="0 0 24 24" ${label}
      fill="none" stroke="currentColor" stroke-width="1.75"
      stroke-linecap="square" stroke-linejoin="miter">${body}</svg>`;
  }

  /* The mark. Tall post, broken post, chevron through the gap.
     `flat` drops the ember so it can sit on a coloured field.

     The path data is LIFTED FROM packaging/logo.svg, not redrawn — an earlier
     version approximated it with three stroked polylines and got the
     proportions, the bar weight and the chevron's angle all slightly wrong.
     Coordinates are the artwork's own user units, so the viewBox is a square
     centred on the mark rather than a tidy 0 0 64 64; that keeps them
     copy-pasteable from the file if it ever changes.

     Solid fills, not strokes: the real mark's bars are rectangles, and stroking
     a centre line cannot reproduce the chevron, whose two arms are quads with
     square-cut ends that meet at a mitre. */
  BGIcon.logo = function (opts) {
    opts = opts || {};
    const size = opts.size || 24;
    const accent = opts.flat ? "currentColor" : EMBER;
    return `<svg class="bgi bgi-logo" width="${size}" height="${size}"
      viewBox="149.66 150 1200.02 1200.02"
      role="img" aria-label="Builders Gate" fill="none">
      <g fill="currentColor">
        <path d="M266.46 150H379.2v1200.02H266.46Z"/>
        <path d="M1120.5 150h112.37v526.79H1120.5Z"/>
        <path d="M1120.5 823.21h112.37v526.81H1120.5Z"/>
      </g>
      <g fill="${accent}">
        <path d="M584.04 499.5 655.55 428 950.75 723.2 879.25 794.7Z"/>
        <path d="M879.15 651.2 950.65 722.7 655.42 1018 583.92 946.4Z"/>
      </g>
    </svg>`;
  };

  /** Replace every <span data-icon="name"> in `root` with real geometry.
   *  Lets markup name an icon instead of pasting a glyph nobody can grep for. */
  BGIcon.upgrade = function (root) {
    (root || document).querySelectorAll("[data-icon]").forEach(el => {
      if (el.dataset.iconDone) return;
      const size = Number(el.dataset.iconSize) || 18;
      el.innerHTML = el.dataset.icon === "logo"
        ? BGIcon.logo({ size })
        : BGIcon(el.dataset.icon, { size });
      el.dataset.iconDone = "1";
    });
  };

  BGIcon.has = (name) => !!P[ALIASES[name] || name];
  BGIcon.names = () => Object.keys(P);
  window.BGIcon = BGIcon;

  if (!document.getElementById("bgi-style")) {
    const s = document.createElement("style");
    s.id = "bgi-style";
    s.textContent = `
      .bgi{display:block;flex:none;vertical-align:middle;overflow:visible}
      .bgi .e{stroke:var(--ember)}
      .bgi-missing{color:var(--ember)}
      /* The rail dims its icons until the row is live, so the ember stroke
         reads as "this is the one you are in" rather than constant noise. */
      .rail-item .bgi .e{stroke:currentColor;opacity:.6}
      /* .active, not .on — setWorkspace() has only ever written .active, so
         this rule matched nothing and the row you were standing in looked the
         same as the ten you were not. */
      .rail-item.active .bgi .e,.rail-item:hover .bgi .e{stroke:var(--ember);opacity:1}
    `;
    document.head.appendChild(s);
  }
})();
