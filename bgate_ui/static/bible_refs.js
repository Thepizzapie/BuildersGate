/* bible_refs.js — reference anchors for the World bible.
 *
 * WHAT WAS MISSING. The bible describes the game in prose while every image
 * that actually defines the look lived in the art seat's pin list, connected to
 * nothing. A pillar could say "corporate-collapse satire" and the concept art
 * that settles what that MEANS was three views away, so a seat reading the
 * bible got the words and guessed the pictures. The guess is where style drift
 * starts, and after the fact nobody can point at where it began.
 *
 * WHAT IS STORED IS THE PIN NAME, NOT A PATH. Re-pinning art under the same
 * name lands as a new revision and moves the pointer, so a section that stored
 * a path would still be showing revision 1 of something that has since been
 * redrawn. Every thumbnail below is resolved server-side, at read time.
 *
 * EXISTENCE IS SHOWN, NOT FILTERED. A pin whose file went missing underneath it
 * stays in the list wearing a "file missing" chip. A list that quietly shortens
 * itself is how a section comes to look anchored when it is not.
 *
 * SELF-CONTAINED ON PURPOSE. Its own <style>, mounted at runtime into
 * #view-world BESIDE #world-root (never inside it - world.js rewrites that
 * element's innerHTML on every render and would erase this panel), so the only
 * shared-file edit this feature needed was one <script> tag. Colours are custom
 * properties only; a hardcoded hex breaks the moment the theme changes.
 *
 * Registered as window.BibleRefs.
 */
window.BibleRefs = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const KINDS = ["character", "style", "ui", "concept"];
  const POLL_MS = 15000;

  /* A pin's stored path is absolute and /api/preview only serves root-relative
     paths. Cutting everything ahead of .bgate is the rule the seat workspaces
     already use; repeated here rather than imported so this file keeps working
     if the seats bundle is not on the page. */
  const relRef = p => !p ? ""
    : String(p).replace(/^.*[\\/](?=\.bgate[\\/])/, "").replace(/\\/g, "/");
  const previewURL = p => "/api/preview?rel=" + encodeURIComponent(relRef(p));

  let host = null, mounted = false, timer = 0, loading = false;
  let state = { sections: [], pins: [], bySection: {}, suggestions: [],
                error: "", busy: "" };
  let form = { section: "", ref: "", kind: "style" };

  /* ── styles ────────────────────────────────────────────────────────────── */
  function injectStyle() {
    if (document.getElementById("brf-style")) return;
    const el = document.createElement("style");
    el.id = "brf-style";
    el.textContent = [
      ".brf{margin-top:18px}",
      ".brf-head{display:flex;gap:9px;align-items:center;margin-bottom:9px}",
      ".brf-head h3{margin:0;font-size:13.5px;color:var(--text)}",
      ".brf-n{font-family:var(--mono);font-size:var(--fs-3xs,10px);letter-spacing:.06em;text-transform:uppercase;color:var(--text-3)}",
      ".brf-sub{font-size:12px;color:var(--text-2);line-height:1.45;margin:0 0 11px}",

      /* the attach bar */
      ".brf-bar{display:flex;gap:7px;align-items:center;flex-wrap:wrap;padding:9px 11px;border:1px solid var(--line);border-radius:var(--r-sm,8px);background:var(--surface-1);margin-bottom:11px}",
      ".brf-bar select,.brf-bar input[type=text]{background:var(--surface-2,var(--surface-1));border:1px solid var(--line);border-radius:var(--r-sm,8px);color:var(--text);font:inherit;font-size:12px;padding:6px 9px;max-width:280px}",
      ".brf-bar select:focus,.brf-bar input:focus{outline:none;border-color:var(--accent)}",
      ".brf-btn{font-family:var(--mono);font-size:10px;letter-spacing:.06em;text-transform:uppercase;padding:6px 12px;border:1px solid var(--line);border-radius:999px;background:none;color:var(--text-2);cursor:pointer}",
      ".brf-btn:hover:not(:disabled){border-color:var(--accent);color:var(--text)}",
      ".brf-btn:disabled{opacity:.45;cursor:default}",
      ".brf-up{position:relative;overflow:hidden;display:inline-flex}",
      ".brf-up input{position:absolute;inset:0;opacity:0;cursor:pointer;font-size:0}",
      ".brf-msg{font-size:11.5px;color:var(--text-3)}",
      ".brf-msg.bad{color:var(--bad)}",

      /* the rescue strip: pins named in prose, offered as one-click anchors */
      ".brf-sug{border:1px solid var(--line);border-left:2px solid var(--accent);border-radius:var(--r-sm,8px);background:var(--surface-1);padding:9px 11px;margin-bottom:11px}",
      ".brf-sug-h{font-size:12px;color:var(--text-2);line-height:1.45;margin-bottom:8px}",
      ".brf-sug-row{display:flex;gap:7px;align-items:center;flex-wrap:wrap;padding:4px 0}",
      ".brf-sug-row b{font-size:12px;color:var(--text);font-weight:600;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".brf-chip{font-family:var(--mono);font-size:9.5px;letter-spacing:.05em;padding:4px 9px;border:1px solid var(--line);border-radius:999px;background:none;color:var(--text-2);cursor:pointer}",
      ".brf-chip:hover{border-color:var(--accent);color:var(--text);background:var(--accent-soft,var(--surface-3))}",
      ".brf-un{font-family:var(--mono);font-size:9.5px;color:var(--warn);}",

      /* one row per anchored section */
      ".brf-list{display:flex;flex-direction:column;gap:9px}",
      ".brf-sec{border:1px solid var(--line);border-radius:var(--r-sm,8px);background:var(--surface-1);padding:9px 11px}",
      ".brf-sec-h{display:flex;gap:8px;align-items:baseline;margin-bottom:8px}",
      ".brf-sec-h b{font-size:12.5px;color:var(--text);font-weight:600}",
      ".brf-kind{font-family:var(--mono);font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--text-3);border:1px solid var(--line);border-radius:999px;padding:1px 7px}",
      ".brf-cards{display:flex;gap:9px;flex-wrap:wrap}",
      ".brf-card{position:relative;width:104px;border:1px solid var(--line);border-radius:var(--r-sm,8px);overflow:hidden;background:var(--surface-2,var(--surface-1))}",
      ".brf-card img{display:block;width:100%;height:74px;object-fit:cover;background:var(--surface-3)}",
      ".brf-card.gone{border-color:var(--bad-line,var(--line))}",
      ".brf-card .brf-meta{padding:5px 6px;display:flex;flex-direction:column;gap:2px}",
      ".brf-card .brf-meta b{font-size:10.5px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".brf-card .brf-meta span{font-family:var(--mono);font-size:8.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-3)}",
      ".brf-card .brf-meta .gone{color:var(--bad)}",
      ".brf-x{position:absolute;top:4px;right:4px;width:19px;height:19px;line-height:17px;text-align:center;border-radius:50%;border:1px solid var(--line);background:var(--surface-1);color:var(--text-3);cursor:pointer;font-size:11px;padding:0}",
      ".brf-x:hover{color:var(--bad);border-color:var(--bad-line,var(--line))}",
      ".brf-empty{font-size:12px;color:var(--text-3);border:1px dashed var(--line);border-radius:var(--r-sm,8px);padding:13px;line-height:1.5}",
    ].join("\n");
    document.head.appendChild(el);
  }

  /* ── data ──────────────────────────────────────────────────────────────── */
  async function GET(url) {
    const res = await fetch(url);
    const body = await res.json().catch(() => ({}));
    if (res.status === 404 && url.indexOf("/refs") >= 0) {
      // The page is served from disk and the API from a process that started
      // before this feature existed. Say that, rather than showing a raw 404
      // that reads as "your anchors are gone".
      throw new Error("the running dashboard predates the anchors API - "
        + "restart bgate serve to pick it up");
    }
    if (!res.ok) throw new Error((body.error && body.error.message) || `${url} failed`);
    return body;
  }

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const [bible, pins, anchors, suggested] = await Promise.all([
        GET("/api/bible"), GET("/api/refs"), GET("/api/bible/refs"),
        GET("/api/bible/refs/suggest").catch(() => ({ suggestions: [] })),
      ]);
      const data = bible.data || bible;
      state.sections = (data.sections || []).map(
        s => ({ id: Number(s.id), kind: s.kind, title: s.title }));
      state.pins = pins.refs || [];
      state.bySection = anchors.by_section || {};
      state.suggestions = suggested.suggestions || [];
      state.error = "";
    } catch (err) {
      state.error = String(err.message || err);
    } finally {
      loading = false;
      render();
    }
  }

  /* ── render ────────────────────────────────────────────────────────────── */
  function card(section, r) {
    const gone = !r.exists;
    const img = r.resolved_path
      ? `<img src="${E(previewURL(r.resolved_path))}" alt="" loading="lazy"
              onerror="this.style.opacity=.15">`
      : `<img alt="">`;
    return `<div class="brf-card${gone ? " gone" : ""}">
      ${img}
      <button class="brf-x" title="remove this anchor"
              onclick="BibleRefs.detach(${section}, '${E(r.ref)}')">&#10005;</button>
      <div class="brf-meta">
        <b title="${E(r.ref)}">${E(r.ref)}</b>
        <span class="${gone ? "gone" : ""}">${gone ? "file missing" : E(r.kind || "")}</span>
      </div>
    </div>`;
  }

  function render() {
    if (!host) return;
    const anchored = Object.keys(state.bySection)
      .map(Number).filter(id => (state.bySection[String(id)] || []).length);
    const titles = {};
    state.sections.forEach(s => { titles[s.id] = s; });

    const secOpts = state.sections.map(s =>
      `<option value="${s.id}"${String(s.id) === form.section ? " selected" : ""}>${E(s.title)} (${E(s.kind)})</option>`).join("");
    const pinOpts = state.pins.map(p =>
      `<option value="${E(p.name)}"${p.name === form.ref ? " selected" : ""}>${E(p.name)} (${E(p.kind)})</option>`).join("");
    const kindOpts = KINDS.map(k =>
      `<option value="${k}"${k === form.kind ? " selected" : ""}>${k}</option>`).join("");

    const rows = anchored.sort((a, b) => a - b).map(id => {
      const sec = titles[id] || { title: `section ${id}`, kind: "" };
      const list = state.bySection[String(id)] || [];
      return `<div class="brf-sec">
        <div class="brf-sec-h">
          <b>${E(sec.title)}</b><span class="brf-kind">${E(sec.kind)}</span>
        </div>
        <div class="brf-cards">${list.map(r => card(id, r)).join("")}</div>
      </div>`;
    }).join("");

    /* THE WORKAROUND, OFFERED BACK AS A BUTTON. Sections name their pins in
       prose ("(pinned: concept-battle / concept-battle-dark)") because there
       was nowhere else to put them. Proposed, never applied: the title is
       something a human typed, the match is a string search, and nothing here
       edits either. One click anchors one pin. */
    const suggestions = state.suggestions.filter(s => (s.propose || []).length
      || (s.unresolved || []).length);
    const suggestBlock = suggestions.length ? `
      <div class="brf-sug">
        <div class="brf-sug-h">${suggestions.length} section${suggestions.length === 1 ? "" : "s"}
          name pinned art in their own text. Anchor it and the picture travels with the words.</div>
        ${suggestions.map(s => `<div class="brf-sug-row">
          <b title="${E(s.title)}">${E(s.title)}</b>
          ${(s.propose || []).map(name =>
            `<button class="brf-chip" title="anchor ${E(name)} to this section"
               onclick="BibleRefs.attach(${s.section_id}, '${E(name)}', 'concept')">+ ${E(name)}</button>`).join("")}
          ${(s.unresolved || []).length
            ? `<span class="brf-un">no pin named ${E((s.unresolved || []).join(", "))}</span>` : ""}
        </div>`).join("")}
      </div>` : "";

    const counted = `${anchored.length} of ${state.sections.length} sections`;
    host.innerHTML = `
      <div class="brf-head">
        <h3>Reference anchors</h3>
        <span class="brf-n">${E(counted)} anchored</span>
      </div>
      <p class="brf-sub">The pictures a section MEANS. Anchor the pinned art to
        the pillar, constraint or reference it settles - every seat that reads
        the bible then gets the images with the words instead of guessing them.
        Anchors store the pin name, so re-pinning better art upgrades every
        section pointing at it.</p>
      <div class="brf-bar">
        <select id="brf-sec">${secOpts || `<option value="">no sections yet</option>`}</select>
        <select id="brf-ref">${pinOpts || `<option value="">nothing pinned yet</option>`}</select>
        <select id="brf-kind">${kindOpts}</select>
        <button class="brf-btn" id="brf-add"${state.pins.length && state.sections.length ? "" : " disabled"}>anchor</button>
        <span class="brf-btn brf-up">upload<input type="file" id="brf-file"
          accept="image/png,image/jpeg,image/webp,image/gif"></span>
        <span class="brf-msg${state.error ? " bad" : ""}" id="brf-msg">${E(state.error || state.busy)}</span>
      </div>
      ${suggestBlock}
      ${rows ? `<div class="brf-list">${rows}</div>`
             : `<div class="brf-empty">No section points at any art yet. Pick a
                  pillar and the pinned image that shows what it looks like -
                  the bible stops being prose alone at that moment.</div>`}`;

    const pick = id => host.querySelector(id);
    const secSel = pick("#brf-sec"), refSel = pick("#brf-ref"), kindSel = pick("#brf-kind");
    if (secSel) { form.section = secSel.value; secSel.onchange = () => { form.section = secSel.value; }; }
    if (refSel) { form.ref = refSel.value; refSel.onchange = () => { form.ref = refSel.value; }; }
    if (kindSel) kindSel.onchange = () => { form.kind = kindSel.value; };
    const add = pick("#brf-add");
    if (add) add.onclick = () => attach(form.section, form.ref, form.kind);
    const file = pick("#brf-file");
    if (file) file.onchange = () => upload(file.files && file.files[0]);
  }

  function say(msg, bad) {
    state.busy = bad ? "" : msg;
    state.error = bad ? msg : "";
    const el = host && host.querySelector("#brf-msg");
    if (el) { el.textContent = msg; el.classList.toggle("bad", Boolean(bad)); }
    if (window.BGWS && BGWS.toast && bad) BGWS.toast(msg, true);
  }

  async function send(url, options) {
    const res = await fetch(url, options);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      // FastAPI's HTTPException answers {detail}; the enveloped routes answer
      // {error:{message}}. Read both, or a real explanation shows as "failed".
      throw new Error(body.detail || (body.error && body.error.message)
        || `${res.status} ${res.statusText}`);
    }
    return body;
  }

  async function attach(sectionId, ref, kind) {
    if (!sectionId || !ref) { say("pick a section and a pinned ref", true); return; }
    say("anchoring…");
    try {
      await send(`/api/bible/${encodeURIComponent(sectionId)}/refs`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ref, kind }),
      });
      say("");
      await load();
    } catch (err) { say(String(err.message || err), true); }
  }

  async function detach(sectionId, ref) {
    say("removing…");
    try {
      await send(`/api/bible/${encodeURIComponent(sectionId)}/refs?ref=`
        + encodeURIComponent(ref), { method: "DELETE" });
      say("");
      await load();
    } catch (err) { say(String(err.message || err), true); }
  }

  /* Upload goes as base64 in a JSON body: the dashboard takes no new
     dependencies and FastAPI needs python-multipart for a real form post. */
  async function upload(file) {
    if (!file) return;
    if (!form.section) { say("pick a section first", true); return; }
    say("uploading…");
    try {
      const data = await new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onload = () => resolve(String(fr.result || ""));
        fr.onerror = () => reject(new Error("could not read that file"));
        fr.readAsDataURL(file);
      });
      const name = file.name.replace(/\.[^.]+$/, "").slice(0, 60) || "upload";
      await send(`/api/bible/${encodeURIComponent(form.section)}/refs/upload`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, data, kind: form.kind }),
      });
      say("");
      await load();
    } catch (err) { say(String(err.message || err), true); }
  }

  /* ── lifecycle ─────────────────────────────────────────────────────────── */

  /* The World view has two tabs and this panel belongs to the bible one. The
     tab is read off the subnav button's own onclick (World.setTab('bible'))
     rather than its label, so restyling or renaming the tab does not hide the
     panel. No subnav at all: show it, since the only other outcome is a feature
     that silently disappears. */
  function onBibleTab() {
    const nav = document.getElementById("world-subnav");
    if (!nav) return true;
    const active = nav.querySelector(".seat-tab.active");
    if (!active) return true;
    return String(active.getAttribute("onclick") || "").indexOf("bible") >= 0;
  }
  const viewActive = () => {
    const view = document.getElementById("view-world");
    return Boolean(view && (view.classList.contains("active") || !view.hidden));
  };

  function mount() {
    if (mounted) return true;
    const view = document.getElementById("view-world");
    if (!view) return false;
    injectStyle();
    host = document.createElement("div");
    host.className = "spanel brf";
    host.id = "brf";
    // APPENDED to the view, not to #world-root: world.js owns that element and
    // replaces its innerHTML on every tab render, which would delete this.
    view.appendChild(host);
    mounted = true;
    render();
    load();
    // The first tick would otherwise treat this as a newly-visible panel and
    // fetch everything a second time before the first answer lands.
    wasVisible = true;
    return true;
  }

  let wasVisible = false;
  function tick() {
    if (!mounted && !mount()) return;
    const visible = viewActive() && onBibleTab();
    host.hidden = !visible;
    if (!visible) { wasVisible = false; return; }
    const first = !wasVisible;
    wasVisible = true;
    if (document.visibilityState === "hidden") return;
    // A repaint rebuilds the bar and closes an open dropdown mid-choice, so the
    // poll stands down while somebody is using it. Otherwise refetch: pins get
    // added from the art seat and sections from the bible editor beside this,
    // and a stale list here reads as "that pin does not exist".
    const focused = document.activeElement;
    if (!first && focused && host.contains(focused)) return;
    load();
  }

  function boot() {
    if (!mount()) { setTimeout(boot, 400); return; }
    clearInterval(timer);
    timer = setInterval(tick, POLL_MS);
    tick();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  return { attach, detach, reload: load, mount };
})();
