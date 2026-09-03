import { useCallback, useEffect, useRef, useState } from "react";
import { previewURL, toast } from "../../bridge";
import { useEvents } from "../../hooks";
import {
  refAttach, refDetach, refsRead, refUpload, relRef,
  type RefsState, type Section,
} from "./api";

/* Reference anchors for the World bible.
 *
 * WHAT WAS MISSING. The bible describes the game in prose while every image
 * that actually defines the look lived in the art seat's pin list, connected to
 * nothing. A pillar could say "corporate-collapse satire" and the concept art
 * that settles what that MEANS was three views away, so a seat reading the
 * bible got the words and guessed the pictures. The guess is where style drift
 * starts.
 *
 * WHAT IS STORED IS THE PIN NAME, NOT A PATH. Re-pinning art under the same
 * name lands as a new revision and moves the pointer. Every thumbnail below is
 * resolved server-side, at read time.
 *
 * EXISTENCE IS SHOWN, NOT FILTERED. A pin whose file went missing underneath it
 * stays in the list wearing a "file missing" chip. A list that quietly shortens
 * itself is how a section comes to look anchored when it is not. */

const KINDS = ["character", "style", "ui", "concept"];
/* Pins get added from the art seat and sections from the bible editor beside
   this, and a stale list here reads as "that pin does not exist". No event
   kind describes either write, so the bus's fallback timer is the refresh. */
const REFRESH_MS = 15000;

const EMPTY: RefsState = { pins: [], bySection: {}, suggestions: [] };

export function Refs({ active, sections }: { active: boolean; sections: Section[] }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<RefsState>(EMPTY);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [form, setForm] = useState({ section: "", ref: "", kind: "style" });
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    const { state: next, error: err } = await refsRead();
    setState(next);
    setError(err || "");
  }, []);
  const refresh = useCallback(() => {
    /* A repaint would close an open dropdown mid-choice, so the refresh stands
       down while somebody is using the bar. */
    if (host.current && host.current.contains(document.activeElement)) return;
    void load();
  }, [load]);
  useEvents(refresh, { enabled: active, kinds: [], fallbackMs: REFRESH_MS });

  /* The selects need a real value even before anyone touches them. */
  useEffect(() => {
    setForm((f) => ({
      ...f,
      section: sections.some((s) => String(s.id) === f.section) ? f.section : String(sections[0]?.id ?? ""),
      ref: state.pins.some((p) => p.name === f.ref) ? f.ref : (state.pins[0]?.name ?? ""),
    }));
  }, [sections, state.pins]);

  const say = (msg: string, bad?: boolean) => {
    setBusy(bad ? "" : msg);
    setError(bad ? msg : "");
    if (bad) toast(msg, "warn");
  };

  const attach = async (sectionId: number | string, ref: string, kind: string) => {
    if (!sectionId || !ref) { say("pick a section and a pinned ref", true); return; }
    say("anchoring…");
    const res = await refAttach(sectionId, ref, kind);
    if (!res.ok) { say(res.error || "anchor failed", true); return; }
    say("");
    await load();
  };

  const detach = async (sectionId: number | string, ref: string) => {
    say("removing…");
    const res = await refDetach(sectionId, ref);
    if (!res.ok) { say(res.error || "remove failed", true); return; }
    say("");
    await load();
  };

  const upload = async (file: File | null | undefined) => {
    if (!file) return;
    if (!form.section) { say("pick a section first", true); return; }
    say("uploading…");
    let data = "";
    try {
      data = await new Promise<string>((resolve, reject) => {
        const fr = new FileReader();
        fr.onload = () => resolve(String(fr.result || ""));
        fr.onerror = () => reject(new Error("could not read that file"));
        fr.readAsDataURL(file);
      });
    } catch (e) { say(String((e as Error).message || e), true); return; }
    const name = file.name.replace(/\.[^.]+$/, "").slice(0, 60) || "upload";
    const res = await refUpload(form.section, { name, data, kind: form.kind });
    if (fileRef.current) fileRef.current.value = "";
    if (!res.ok) { say(res.error || "upload failed", true); return; }
    say("");
    await load();
  };

  const titles = new Map(sections.map((s) => [s.id, s]));
  const anchored = Object.keys(state.bySection).map(Number)
    .filter((id) => (state.bySection[String(id)] || []).length).sort((a, b) => a - b);
  /* THE WORKAROUND, OFFERED BACK AS A BUTTON. Sections name their pins in
     prose ("(pinned: concept-battle / concept-battle-dark)") because there was
     nowhere else to put them. Proposed, never applied: one click anchors one
     pin. */
  const suggestions = state.suggestions.filter((s) => (s.propose || []).length || (s.unresolved || []).length);
  const canAnchor = state.pins.length > 0 && sections.length > 0;

  return (
    <div className="spanel brf" id="brf" ref={host}>
      <div className="brf-head">
        <h3>Reference anchors</h3>
        <span className="brf-n">{anchored.length} of {sections.length} sections anchored</span>
      </div>
      <p className="brf-sub">The pictures a section MEANS. Anchor the pinned art to
        the pillar, constraint or reference it settles - every seat that reads
        the bible then gets the images with the words instead of guessing them.
        Anchors store the pin name, so re-pinning better art upgrades every
        section pointing at it.</p>
      <div className="brf-bar">
        <select value={form.section} aria-label="Section"
                onChange={(e) => setForm({ ...form, section: e.currentTarget.value })}>
          {sections.length
            ? sections.map((s) => <option key={s.id} value={String(s.id)}>{s.title} ({s.kind})</option>)
            : <option value="">no sections yet</option>}
        </select>
        <select value={form.ref} aria-label="Pinned ref"
                onChange={(e) => setForm({ ...form, ref: e.currentTarget.value })}>
          {state.pins.length
            ? state.pins.map((p) => <option key={p.name} value={p.name}>{p.name} ({p.kind})</option>)
            : <option value="">nothing pinned yet</option>}
        </select>
        <select value={form.kind} aria-label="Anchor kind"
                onChange={(e) => setForm({ ...form, kind: e.currentTarget.value })}>
          {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
        <button className="brf-btn" disabled={!canAnchor}
                onClick={() => void attach(form.section, form.ref, form.kind)}>anchor</button>
        <label className="brf-btn brf-up" style={{ position: "relative", overflow: "hidden", display: "inline-flex" }}>
          upload
          <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp,image/gif"
                 style={{ position: "absolute", inset: 0, opacity: 0, cursor: "pointer", fontSize: 0 }}
                 onChange={(e) => void upload(e.currentTarget.files?.[0])} />
        </label>
        <span className={`brf-msg${error ? " bad" : ""}`}>{error || busy}</span>
      </div>
      {suggestions.length > 0 && (
        <div className="brf-sug">
          <div className="brf-sug-h">{suggestions.length} section{suggestions.length === 1 ? "" : "s"} name
            pinned art in their own text. Anchor it and the picture travels with the words.</div>
          {suggestions.map((s) => (
            <div className="brf-sug-row" key={s.section_id}>
              <b title={s.title}>{s.title}</b>
              {(s.propose || []).map((name) => (
                <button key={name} className="brf-chip" title={`anchor ${name} to this section`}
                        onClick={() => void attach(s.section_id, name, "concept")}>+ {name}</button>
              ))}
              {(s.unresolved || []).length > 0 && (
                <span className="brf-un">no pin named {(s.unresolved || []).join(", ")}</span>
              )}
            </div>
          ))}
        </div>
      )}
      {anchored.length ? (
        <div className="brf-list">
          {anchored.map((id) => {
            const sec = titles.get(id) || { title: `section ${id}`, kind: "" };
            const list = state.bySection[String(id)] || [];
            return (
              <div className="brf-sec" key={id}>
                <div className="brf-sec-h">
                  <b>{sec.title}</b><span className="brf-kind">{sec.kind}</span>
                </div>
                <div className="brf-cards">
                  {list.map((r) => {
                    const gone = !r.exists;
                    return (
                      <div className={`brf-card${gone ? " gone" : ""}`} key={r.ref}>
                        {r.resolved_path
                          ? <img src={previewURL(relRef(r.resolved_path))} alt="" loading="lazy"
                                 onError={(e) => e.currentTarget.classList.add("dead")} />
                          : <img alt="" />}
                        <button className="brf-x" title="remove this anchor"
                                onClick={() => void detach(id, r.ref)}>✕</button>
                        <div className="brf-meta">
                          <b title={r.ref}>{r.ref}</b>
                          <span className={gone ? "gone" : ""}>{gone ? "file missing" : (r.kind || "")}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="brf-empty">No section points at any art yet. Pick a
          pillar and the pinned image that shows what it looks like -
          the bible stops being prose alone at that moment.</div>
      )}
    </div>
  );
}
