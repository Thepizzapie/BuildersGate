import { useEffect, useState } from "react";
import { Ti } from "../Ti";
import { previewURL, lightbox, mutate, toast, askConfirm } from "../../bridge";
import { Head, Nothing, Tag, ReadError } from "./prims";
import { useJSON } from "./api";

/* ART · STYLE: train a house style from the anchors, and choose which one
 * generation uses.
 *
 * The four routes in bgate_ui/routes/art_style.py had no caller: training was
 * reachable only by hand-typed HTTP. They are human-only on purpose (a run is
 * 5-15 minutes at a price Krea does not publish), so the panel is the door.
 *
 * TRAINING IS A RUN, NOT A CLICK. The POST returns at once and the work happens
 * on a server thread, so the panel polls GET /api/art/style while `running` says
 * so and draws the stage the adapter reports (uploading n of total, then
 * training on a job id) with the elapsed time. A run that fails keeps its error
 * on screen until the next one starts; a toast would be gone before anybody
 * came back to a 15-minute job.
 *
 * THE DATASET IS SHOWN BEFORE IT IS SPENT. The read already judges the anchors
 * (the 1024px floor, the minimum count), so the train button is disabled with
 * the reason when the set cannot train, and the dropped anchors are named.
 */

type Style = {
  style_id: string; name?: string; trigger_word?: string; model?: string;
  kind?: string; images?: number; sources?: string[]; trained_at?: string;
  strength?: number;
};
type Anchor = { name?: string; kind?: string; rel?: string; path?: string };
type Rejected = Anchor & { why?: string };
type Running = {
  status?: "running" | "done" | "failed"; name?: string; stage?: string;
  done?: number; total?: number; job_id?: string; style_id?: string;
  error?: string; started?: number; ended?: number; images?: number;
};
type StyleRead = {
  trained?: Style[]; active?: Style | null; mode?: string; strength?: number;
  source?: string;
  dataset?: {
    ok?: boolean; reason?: string; warnings?: string[]; rejected?: Rejected[];
    anchors?: Anchor[]; candidates?: number; source?: string;
  };
  running?: Running;
};

const elapsed = (from?: number, to?: number) => {
  if (!from) return "";
  const s = Math.max(0, ((to || Date.now() / 1000) - from));
  return s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`;
};

function stageText(r: Running): string {
  if (r.stage === "uploading") return `uploading image ${(r.done ?? 0) + 1} of ${r.total ?? "?"}`;
  if (r.stage === "training") return `training on Krea${r.job_id ? ` · job ${r.job_id}` : ""}`;
  return "preparing the dataset";
}

export function ArtStyle({ active }: { active: boolean }) {
  const read = useJSON<StyleRead>("/api/art/style", {}, 15000, active);
  const run = read.running || {};
  const live = run.status === "running";
  const [name, setName] = useState("");
  const [busy, setBusy] = useState("");
  /* A refused start (no key, a run already going, a dataset that changed
     under the panel) stays under the button until the next attempt. */
  const [refused, setRefused] = useState("");
  const [, tick] = useState(0);

  /* The event bus does not carry training progress, and the fallback timer is
     30s, so a live run is polled here: every 3s while the server says running,
     and the clock redraws every second so elapsed time moves between reads. */
  const refresh = read.__refresh;
  useEffect(() => {
    if (!active || !live) return;
    const poll = setInterval(() => refresh?.(), 3000);
    const clock = setInterval(() => tick((n) => n + 1), 1000);
    return () => { clearInterval(poll); clearInterval(clock); };
  }, [active, live, refresh]);

  const styles = read.trained || [];
  const current = read.active || null;
  const ds = read.dataset || {};
  const anchors = ds.anchors || [];
  const rejected = ds.rejected || [];
  const lora = read.mode === "lora";

  const train = async () => {
    const n = name.trim();
    if (!n) return;
    setBusy("train");
    setRefused("");
    const r = await mutate("/api/art/style/train", { body: { name: n }, quiet: true });
    setBusy("");
    if (r.ok) { setName(""); toast(`training ${n} started`, "ok"); }
    else setRefused(r.error || "training did not start");
    refresh?.();
  };

  const activate = async (id: string) => {
    setBusy(id || "none");
    const r = await mutate("/api/art/style/active", { body: { style_id: id }, quiet: true });
    setBusy("");
    if (r.ok) toast(id ? "active style changed" : "no style active", "ok");
    else toast(r.error || "could not change the active style", "bad");
    refresh?.();
  };

  const forget = async (s: Style) => {
    const go = await askConfirm({
      title: `Forget ${s.name || s.style_id}?`,
      body: "This removes it from the project's record. The style still exists "
          + "on your Krea account; nothing is refunded.",
      ok: "forget it", danger: true,
    });
    if (!go) return;
    setBusy(s.style_id);
    const r = await mutate(`/api/art/style/${encodeURIComponent(s.style_id)}`,
                           { method: "DELETE", quiet: true });
    setBusy("");
    if (r.ok) toast(`forgot ${s.name || s.style_id}`, "ok");
    else toast(r.error || "could not forget the style", "bad");
    refresh?.();
  };

  return (
    <div className="bgs-pad bga-style">
      <ReadError error={read.__error} what="the trained styles" />

      <Head label="Training" hint="5-15 minutes on Krea, priced by Krea" />
      {run.status && (
        <div className={`bga-run t-${run.status}`} data-testid="style-run">
          <Ti name={live ? "loader-2" : run.status === "done" ? "circle-check" : "alert-triangle"}
              size={15} />
          <div className="b">
            <b>{run.name}</b>
            {live && <span>{stageText(run)} · {elapsed(run.started)}</span>}
            {run.status === "done" && (
              <span>trained in {elapsed(run.started, run.ended)}{run.style_id ? ` · ${run.style_id}` : ""}</span>
            )}
            {run.status === "failed" && (
              <span className="err">
                failed{run.stage && run.stage !== "starting" ? ` while ${stageText(run)}` : ""}
                {": "}{run.error || "no reason recorded"}
              </span>
            )}
          </div>
          {live && typeof run.total === "number" && run.stage === "uploading" && (
            <span className="bar"><span style={{ width: `${Math.round(((run.done ?? 0) / Math.max(1, run.total)) * 100)}%` }} /></span>
          )}
        </div>
      )}

      <div className="bga-train">
        <input value={name} placeholder="name the style"
               onChange={(e) => setName(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter" && ds.ok && !live) train(); }}
               disabled={live} aria-label="style name" />
        <button className="bgs-btn go" onClick={train}
                disabled={live || !ds.ok || !name.trim() || busy === "train"}
                title={!ds.ok ? ds.reason || "this dataset cannot train" : live ? "a run is already going" : ""}>
          <Ti name="wand" size={14} /> {live ? "training…" : `Train from ${anchors.length} anchor${anchors.length === 1 ? "" : "s"}`}
        </button>
      </div>
      {refused && <div className="bgs-readerr" role="alert">training did not start: {refused}</div>}
      {!ds.ok && ds.reason && <div className="bgs-readerr">{ds.reason}</div>}
      {(ds.warnings || []).map((w, i) => <div className="bga-warn" key={i}>{w}</div>)}

      <Head label="Dataset" hint={`${ds.source || read.source || "pins"} · ${anchors.length} of ${ds.candidates ?? 0} clear the floor`} />
      {!ds.candidates && !read.__error && (
        <Nothing what="nothing to train on"
                 how="ref_pin approves an image as a reference; the pins are the dataset" />
      )}
      {!!anchors.length && (
        <div className="bga-anchors">
          {anchors.map((a, i) => (
            <button key={`${a.name}${i}`} className="bga-anchor" title={a.name}
                    onClick={() => a.rel && lightbox(a.rel)}>
              {a.rel ? <img src={previewURL(a.rel)} alt={a.name || "anchor"} /> : <span className="none" />}
              <span className="cap">{a.name}</span>
            </button>
          ))}
        </div>
      )}
      {!!rejected.length && (
        <div className="bgs-reasons">
          {rejected.map((r, i) => <div key={i}><b>{r.name || r.path}</b>: {r.why}</div>)}
        </div>
      )}

      <Head label="Trained styles"
            hint={lora ? "generation uses the active one" : "art.style_source is refs: the active style is not sent until it is lora"} />
      {!styles.length && !read.__error && (
        <Nothing what="no style trained yet" how="name one above and train it from the dataset" />
      )}
      {styles.map((s) => {
        const on = current?.style_id === s.style_id;
        return (
          <div className={`bga-styled${on ? " on" : ""}`} key={s.style_id}>
            <div className="nm">
              <b>{s.name || s.style_id}</b>
              <span>{s.style_id}{s.images ? ` · ${s.images} images` : ""}{s.trigger_word ? ` · trigger "${s.trigger_word}"` : ""}{s.trained_at ? ` · ${s.trained_at}` : ""}</span>
            </div>
            {on ? <Tag tone={lora ? "good" : "off"}>active</Tag> : null}
            {on
              ? <button className="bgs-btn" disabled={!!busy} onClick={() => activate("")}>Deactivate</button>
              : <button className="bgs-btn" disabled={!!busy} onClick={() => activate(s.style_id)}>Make active</button>}
            <button className="bgs-btn" disabled={!!busy} onClick={() => forget(s)} title="remove from this project's record">
              <Ti name="trash" size={13} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
