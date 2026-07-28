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
  const EMBER = "var(--ember)";

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

    /* ---- seats ---- */
    director:  `<circle cx="12" cy="12" r="7.5"/><path class="e" d="M12 2.5 V7 M12 17 V21.5 M2.5 12 H7 M17 12 H21.5"/>`,
    narrative: `<path d="M4.5 6.5 H19.5 M4.5 11 H19.5 M4.5 15.5 H14"/><path class="e" d="M4.5 20 H10"/>`,
    gameplay:  `<path d="M8.5 4 V9 H3.5 V15 H8.5 V20 H15.5 V15 H20.5 V9 H15.5 V4 Z"/><path class="e" d="M12 9 V15"/>`,
    tech:      `<path d="M8.5 7.5 L4 12 L8.5 16.5"/><path d="M15.5 7.5 L20 12 L15.5 16.5"/><path class="e" d="M13.5 5 L10.5 19"/>`,
    art:       `<rect x="3.5" y="3.5" width="17" height="17" rx="1.5"/><path class="e" d="M3.5 16 L9 10.5 L14 15.5 L17.5 12 L20.5 15"/>`,
    audio:     `<path d="M4 10 V14 M8 7 V17 M20 10 V14"/><path class="e" d="M12 4 V20 M16 8 V16"/>`,
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
  };

  const ALIASES = { anim: "animation", ref: "reference", qa_seat: "qa", brush: "art" };

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
     `flat` drops the ember so it can sit on a coloured field. */
  BGIcon.logo = function (opts) {
    opts = opts || {};
    const size = opts.size || 24;
    const accent = opts.flat ? "currentColor" : EMBER;
    return `<svg class="bgi bgi-logo" width="${size}" height="${size}" viewBox="0 0 64 64"
      role="img" aria-label="Builders Gate" fill="none" stroke-linecap="square">
      <path d="M16 8 V56" stroke="currentColor" stroke-width="4" opacity=".75"/>
      <path d="M48 8 V26 M48 42 V56" stroke="currentColor" stroke-width="4" opacity=".75"/>
      <path d="M28 22 L42 34 L28 46" stroke="${accent}" stroke-width="5"/>
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
      .rail-item.on .bgi .e,.rail-item:hover .bgi .e{stroke:var(--ember);opacity:1}
    `;
    document.head.appendChild(s);
  }
})();
