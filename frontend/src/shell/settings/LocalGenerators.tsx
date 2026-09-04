import { useCallback, useState, type KeyboardEvent } from "react";
import { Button, NativeSelect, TextInput } from "@mantine/core";
import { Ti } from "../Ti";
import { mutate, readJSON, toast } from "../../bridge";
import { useEvents } from "../../hooks";
import "./generators.css";

/* Local generators — ComfyUI and whatever else answers on a loopback socket.
 * Ported from localsetup.js; what it stood for still stands:
 *
 * IT DOES NOT START ANYTHING, AND THAT IS THE DESIGN. The dashboard is not a
 * process manager for services the user owns. The command is unknowable (a
 * conda env, a portable build, a .bat with a dozen flags), every interesting
 * failure is on the far side of it, and an orphan holding 8 GB of VRAM is worse
 * than a sentence telling you to start it yourself. So the loop is: CONFIGURE
 * HERE → START IT YOURSELF → THIS NOTICES. The noticing is a gentle read
 * against the probe endpoint, on the event hook's fallback timer — no server
 * event describes a ComfyUI coming up, so the timer IS the feature here.
 *
 * REASONS ARE PRINTED VERBATIM. The adapters already write a sentence worth
 * showing ("nothing answered at http://127.0.0.1:8188 …"); collapsing that to a
 * lamp is what made this opaque. A lamp AND the sentence, always.
 *
 * Drafts live in component state, so a refresh that lands mid-typing does not
 * throw away what is in the path field — the reason the old module compared a
 * signature before repainting.
 */

export type RtField = {
  label: string; env: string; kind?: string; choices?: string[]; value: string;
  placeholder?: string; exists?: boolean | null; source?: string;
  using_default?: boolean; default?: string; required?: boolean; help: string;
};
export type Runtime = {
  id: string; label: string; stage: string; stage_label?: string; tone?: string;
  powers: string[]; power_labels?: string[]; what: string; reason?: string;
  url?: string; start?: string[]; fields?: RtField[]; software?: string;
  docs_url?: string; available: boolean;
};
export type RuntimesData = {
  runtimes: Runtime[];
  capabilities?: Record<string, string>;
  summary?: { detail?: string; available?: boolean };
};
type InjectedNode = {
  id: string | number; class_type: string; title?: string;
  injected: { field: string; what: string }[];
};
type Workflow = {
  label: string; error?: string; runs_when?: string; path?: string; format?: string;
  node_count?: number; weights?: string[]; injected?: InjectedNode[];
  nodes?: InjectedNode[]; warnings?: string[];
};
type Inspect = {
  __error?: string;
  server?: { ok?: boolean; verdict?: string; error?: string; comfyui_version?: string;
             pytorch_version?: string; python_version?: string; os?: string;
             devices?: { name?: string; type?: string; vram_total_gb?: number }[] };
  queue?: { ok?: boolean; verdict?: string };
  workflows?: Workflow[];
  licence?: { model?: string; code?: string; summary?: string; url?: string } | null;
  catalogue?: { groups?: Record<string, { items?: string[]; help?: string }> };
  history?: { runs?: { images: { url: string; filename: string }[] }[] };
};

const EMPTY: RuntimesData = { runtimes: [] };

/* A local socket answers in single-digit milliseconds, so this can be brisk —
   but it is still somebody's machine and this is a background panel. Six
   seconds is fast enough that starting ComfyUI in another window feels like
   the page noticed by itself, and slow enough to be invisible. */
const POLL_MS = 6000;

const TONE_WORD: Record<string, string> = {
  ready: "ready", unreachable: "not running", unhealthy: "problem",
  unconfigured: "not set up", configured: "not checked", unavailable: "unsupported",
};

export function useRuntimes(active: boolean) {
  const [data, setData] = useState<RuntimesData & { __error?: string }>(EMPTY);
  const refresh = useCallback(async () => {
    if (document.hidden) return;   // nobody is watching; do not probe their socket
    setData(await readJSON<RuntimesData>("/api/local/runtimes?probe=1", EMPTY));
  }, []);
  useEvents(refresh, { enabled: active, kinds: ["settings.*"], fallbackMs: POLL_MS });
  return { data, setData };
}

/* ── small renderers ─────────────────────────────────────────────────── */

function KV({ pairs }: { pairs: [string, unknown][] }) {
  const rows = pairs.filter((p) => p[1] !== "" && p[1] != null);
  if (!rows.length) return null;
  return (
    <div className="gen-kv">
      {rows.map(([k, v]) => (
        <span key={k} style={{ display: "contents" }}>
          <span className="k">{k}</span><span className="v">{String(v)}</span>
        </span>
      ))}
    </div>
  );
}

function Chips({ items, cap = 14 }: { items?: string[]; cap?: number }) {
  const list = items || [];
  if (!list.length) return <div className="gen-none">none reported</div>;
  const rest = list.length - cap;
  return (
    <div className="gen-chips">
      {list.slice(0, cap).map((v, i) => <span key={i} className="gen-chip">{v}</span>)}
      {rest > 0 && <span className="gen-chip more">+{rest} more</span>}
    </div>
  );
}

/* ── the inspect panel: everything the app knew and never showed ─────── */

function WorkflowBlock({ wf }: { wf: Workflow }) {
  if (wf.error === "not set") {
    return (
      <div className="gen-ib"><p className="gen-ih">{wf.label}</p>
        <div className="gen-none">not set yet — once it is, this is where you can see
          what is in it and which parts of it get overwritten</div></div>
    );
  }
  if (wf.error) {
    return <div className="gen-ib"><p className="gen-ih">{wf.label}</p><div className="gen-why">{wf.error}</div></div>;
  }
  const injected = wf.injected || [];
  const others = (wf.nodes || []).filter((n) => !n.injected.length);
  return (
    <div className="gen-ib">
      <p className="gen-ih">{wf.label} — runs {wf.runs_when}</p>
      <KV pairs={[
        ["file", wf.path],
        ["format", wf.format === "api" ? "API format (correct)" : wf.format],
        ["nodes", wf.node_count == null ? "" : String(wf.node_count)],
        ["loads", (wf.weights || []).join(", ")],
      ]} />
      {injected.length > 0 && (
        <>
          <p className="gen-ih" style={{ marginTop: 9 }}>Builders Gate overwrites these before every run</p>
          {injected.map((n) => (
            <div key={String(n.id)} className="gen-node">
              <span className="id">#{n.id}</span>
              <span>
                <span className="cls">{n.class_type}</span>
                {n.title && n.title !== n.class_type && <span className="ttl"> · {n.title}</span>}
                {n.injected.map((m, i) => <div key={i} className="gen-inj">↳ {m.field} ← {m.what}</div>)}
              </span>
            </div>
          ))}
        </>
      )}
      {others.length > 0 && (
        <>
          <p className="gen-ih" style={{ marginTop: 9 }}>the rest of the graph, untouched</p>
          <Chips items={others.map((n) => `#${n.id} ${n.class_type}`)} cap={18} />
        </>
      )}
      {(wf.warnings || []).map((w, i) => <div key={i} className="gen-why">{w}</div>)}
    </div>
  );
}

function InspectBody({ d }: { d: Inspect | null }) {
  if (!d) return <div className="gen-insp"><div className="gen-none">reading…</div></div>;
  if (d.__error) return <div className="gen-insp"><div className="gen-why">{d.__error}</div></div>;
  const s = d.server || {};
  const cat = d.catalogue?.groups || {};
  const lic = d.licence || null;
  const groups = Object.keys(cat).filter((k) => (cat[k].items || []).length);
  const runs = (d.history?.runs || []).filter((r) => r.images.length);
  return (
    <div className="gen-insp">
      {s.ok ? (
        <div className="gen-ib"><p className="gen-ih">the server</p>
          <div className="gen-help" style={{ marginBottom: 6 }}>{s.verdict}</div>
          <KV pairs={[["ComfyUI", s.comfyui_version], ["PyTorch", s.pytorch_version],
                      ["Python", s.python_version], ["OS", s.os]]} />
          {(s.devices || []).length > 0 && (
            <Chips items={(s.devices || []).map((x) =>
              `${x.name || x.type}${x.vram_total_gb ? ` · ${x.vram_total_gb} GB` : ""}`)} />
          )}
        </div>
      ) : (
        <div className="gen-ib"><p className="gen-ih">the server</p>
          <div className="gen-why">{s.error || "not reachable"}</div></div>
      )}
      {d.queue?.ok && (
        <div className="gen-ib"><p className="gen-ih">right now</p>
          <div className="gen-help">{d.queue.verdict}. A local generator that looks
            frozen is usually third in a queue.</div></div>
      )}
      {(d.workflows || []).map((wf, i) => <WorkflowBlock key={i} wf={wf} />)}
      {lic && (
        <div className="gen-ib"><p className="gen-ih">what you may ship</p>
          <KV pairs={[["declared model", lic.model || "(none)"], ["licence", lic.code], ["means", lic.summary]]} />
          {lic.url && (
            <div className="gen-chips">
              <a className="gen-link" href={lic.url} target="_blank" rel="noopener noreferrer">the licence itself ↗</a>
            </div>
          )}
        </div>
      )}
      {/* Only asked when the server answered its stats. An absent catalogue on
          a server that is down is not a version difference and must not be
          reported as one. */}
      {s.ok && (groups.length ? (
        <div className="gen-ib"><p className="gen-ih">what this install can see</p>
          {groups.map((k) => (
            <div key={k} style={{ marginBottom: 7 }}>
              <div className="gen-fhelp" style={{ marginBottom: 3 }}><b>{k}</b> — {cat[k].help}</div>
              <Chips items={cat[k].items} />
            </div>
          ))}
        </div>
      ) : (
        <div className="gen-ib"><p className="gen-ih">what this install can see</p>
          <div className="gen-none">this build did not answer the node query — that
            is a difference in ComfyUI versions, not a fault in your setup</div></div>
      ))}
      {runs.length > 0 && (
        <div className="gen-ib"><p className="gen-ih">the last few things it made</p>
          <div className="gen-shots">
            {runs.slice(0, 4).flatMap((r, ri) => r.images.slice(0, 2).map((im, ii) => (
              <img key={`${ri}-${ii}`} loading="lazy" src={im.url} alt={im.filename} title={im.filename} />
            )))}
          </div>
          <div className="gen-fnote">served straight from ComfyUI's own output folder —
            everything it made, not only what Builders Gate asked for</div></div>
      )}
    </div>
  );
}

/* ── one field of a runtime ───────────────────────────────────────────── */

function FieldRow({ rt, f, busy, onSave, onClear }: {
  rt: Runtime; f: RtField; busy: string;
  onSave: (env: string, value: string) => void; onClear: (env: string) => void;
}) {
  const id = `${rt.id}::${f.env}`;
  const [draft, setDraft] = useState<string | null>(null);
  const value = draft ?? (f.value || "");

  /* WHICH LAYER IS IN FORCE, said out loud. `shadowed` is the one that costs
     an afternoon: the .env has a value and a shell variable set to empty is
     beating it, so the page shows a path and the adapter sees nothing. */
  let note: React.ReactNode;
  if (f.source === "environment") {
    note = <span className="warn">set in your shell environment, not this project's .env —
      this page can overwrite it for the .env, but the shell keeps winning until you unset it and restart</span>;
  } else if (f.source === "shadowed") {
    note = <span className="warn">{f.env} is set to an EMPTY value in this shell, which beats
      the .env — unset it and restart the dashboard</span>;
  } else if (f.source === "env_file") {
    note = <>saved in this project's <code>.env</code></>;
  } else if (f.using_default) {
    note = <>not set - using the default <code>{f.default}</code></>;
  } else {
    note = <>not set</>;
  }
  let exists: React.ReactNode = null;
  if (f.exists === false && f.value) exists = <><span className="warn">that file does not exist</span> · </>;
  else if (f.exists === true) exists = <><span className="good">file found</span> · </>;

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") { e.preventDefault(); onSave(f.env, value); setDraft(null); }
  }

  return (
    <div className="gen-f" data-field={id}>
      <div className="gen-flab">
        <span className="n">{f.label}</span>
        {f.required && <span className="gen-req">required</span>}
        <span className="v">{f.env}</span>
      </div>
      <div className="gen-fhelp">{f.help}</div>
      <div className="gen-row">
        {f.kind === "choice" ? (
          <NativeSelect size="xs" className="grow" value={value}
                        aria-label={f.label}
                        onChange={(e) => setDraft(e.currentTarget.value)}
                        data={[{ value: "", label: "— not declared —" },
                               ...(f.choices || []).map((c) => ({ value: c, label: c }))]} />
        ) : (
          <TextInput size="xs" className={`grow${f.exists === false ? " bad" : ""}`}
                     value={value} onChange={(e) => setDraft(e.currentTarget.value)}
                     onKeyDown={onKey} spellCheck={false} autoComplete="off"
                     aria-label={f.label} placeholder={f.placeholder || ""} />
        )}
        <Button size="xs" variant="outline" loading={busy === `save:${id}`}
                onClick={() => { onSave(f.env, value); setDraft(null); }}>save</Button>
        {f.value && (
          <Button size="xs" variant="default" loading={busy === `clear:${id}`}
                  onClick={() => { onClear(f.env); setDraft(null); }}>clear</Button>
        )}
      </div>
      <div className="gen-fnote">{exists}{note}</div>
    </div>
  );
}

/* ── the runtime card ─────────────────────────────────────────────────── */

export function RuntimeCard({ rt, onData }: { rt: Runtime; onData: (d: RuntimesData) => void }) {
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState(false);
  const [insp, setInsp] = useState<Inspect | null>(null);
  const lamp = rt.tone === "good" ? "good" : (rt.tone === "warn" ? "warn" : "");

  /* The start instructions appear exactly when they are the next thing to do:
     the setup is complete and nothing is answering. Showing them on a card that
     is already generating is noise; showing them on a card with no workflow set
     is the wrong instruction. */
  const showStart = (rt.stage === "unreachable" || rt.stage === "configured") && (rt.start || []).length > 0;

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (!next) return;
    setInsp(null);
    const d = await readJSON<Inspect>(`/api/local/runtimes/${encodeURIComponent(rt.id)}/inspect`, {});
    setInsp(d && Object.keys(d).length ? d : { __error: "the inspect read returned nothing" });
  }

  async function save(env: string, value: string) {
    if (busy) return;
    const id = `${rt.id}::${env}`;
    setBusy(`save:${id}`);
    const r = await mutate<RuntimesData>(`/api/local/runtimes/${encodeURIComponent(rt.id)}/config`,
      { method: "POST", body: { env, value }, quiet: true });
    setBusy("");
    if (!r.ok || !r.data) { toast(r.error || "the save failed"); return; }
    onData(r.data);
    setInsp(null);
    const now = (r.data.runtimes || []).find((x) => x.id === rt.id);
    if (!value.trim()) toast("cleared", "ok");
    else if (now?.stage === "ready") toast(`${now.label} is ready`, "ok");
    else toast(`saved - ${now?.reason || now?.stage_label || "still not ready"}`);
  }

  async function clear(env: string) {
    if (busy) return;
    const id = `${rt.id}::${env}`;
    setBusy(`clear:${id}`);
    const r = await mutate<RuntimesData>(
      `/api/local/runtimes/${encodeURIComponent(rt.id)}/config?env=${encodeURIComponent(env)}`,
      { method: "DELETE", quiet: true });
    setBusy("");
    if (!r.ok || !r.data) { toast(r.error || "the clear failed"); return; }
    onData(r.data);
    setInsp(null);
    toast("cleared", "ok");
  }

  return (
    <div className={`gen-card s-${rt.stage}`} data-runtime={rt.id}>
      <div className="gen-top">
        <Ti name={rt.powers.includes("model_3d") ? "cube" : "palette"} size={15} />
        <span className="gen-name">{rt.label}</span>
        <span className={`gen-lamp ${lamp}`}>{TONE_WORD[rt.stage] || rt.stage}</span>
      </div>
      <div className="gen-pills">
        {(rt.power_labels || []).map((p) => <span key={p} className="gen-pill">{p}</span>)}
        <span className="gen-pill free">$0 · stays on this machine</span>
      </div>
      <div className="gen-help">{rt.what}</div>
      {rt.reason && <div className="gen-why">{rt.reason}</div>}
      {rt.stage === "ready" && (
        <div className="gen-ok">Answering at {rt.url}. Generations through this cost nothing and send nothing anywhere.</div>
      )}
      {showStart && (
        <div className="gen-start">
          <b><Ti name="player-play" size={12} /> Then start it yourself — Builders Gate does not launch it</b>
          <ol>{(rt.start || []).map((s, i) => <li key={i}>{s}</li>)}</ol>
        </div>
      )}
      {rt.stage !== "unavailable" && (rt.fields || []).map((f) => (
        <FieldRow key={f.env} rt={rt} f={f} busy={busy} onSave={save} onClear={clear} />
      ))}
      <div className="gen-foot">
        <span>{rt.url || ""}</span>
        {rt.software === "comfy" && rt.stage !== "unavailable" && (
          <button type="button" className="gen-link" onClick={toggle}>
            {open ? "hide details" : "what is it doing? ↓"}
          </button>
        )}
        {rt.docs_url && (
          <a className="gen-link" href={rt.docs_url} target="_blank" rel="noopener noreferrer">docs ↗</a>
        )}
      </div>
      {open && <InspectBody d={insp} />}
    </div>
  );
}

function SummaryLine({ d }: { d: RuntimesData }) {
  /* ONE LINE, AND IT IS THE SAME LINE `bgate doctor` PRINTS — built by
     localruntimes.summary and shipped in the payload rather than re-derived
     here. Two phrasings of one fact is its own kind of confusion. */
  const s = d.summary || {};
  if (!s.detail) return null;
  return (
    <div className={`gen-sum${s.available ? " good" : ""}`}>
      <Ti name="stethoscope" size={13} /><span>{s.detail}</span>
    </div>
  );
}

function StorageNote() {
  return (
    <p className="gen-note">
      These are addresses and file paths, so unlike an API key they are shown
      back to you in full — a path you cannot read is a path you cannot check
      for the typo. They are written to <code>.env</code> at the game project
      root, the same file the keys live in, and take effect immediately with no
      restart. <b>Nothing here starts or stops anything.</b> Builders Gate talks
      to software you run; it does not run it, so it can never leave a model
      loaded in your GPU after you close this page.
    </p>
  );
}

/* ---- Settings → Local generators ---- */
export function LocalGenerators({ active }: { active: boolean }) {
  const { data, setData } = useRuntimes(active);
  if (data.__error) {
    return <div className="gen-wrap"><div className="gen-none">could not read the local setup — {data.__error}</div></div>;
  }
  const rows = data.runtimes || [];
  return (
    <div className="gen-wrap" data-panel="local-generators">
      <SummaryLine d={data} />
      <StorageNote />
      <div className="gen-grid wide">
        {rows.length
          ? rows.map((r) => <RuntimeCard key={r.id} rt={r} onData={setData} />)
          : <div className="gen-none">no local runtimes are registered in this build.</div>}
      </div>
    </div>
  );
}

/* ---- Studio: appended under the hosted providers, same question ----
   GROUPED BY WHAT IT MAKES, matching the providers' Studio framing, because on
   Studio the question is "why can I not make a 3D model" and the answer is
   every generator that makes one — rented or local — with its state. */
export function LocalStudio({ active }: { active: boolean }) {
  const { data, setData } = useRuntimes(active);
  if (data.__error) {
    return <div className="gen-wrap"><div className="gen-none">could not read the local setup — {data.__error}</div></div>;
  }
  const caps = data.capabilities || {};
  const rows = data.runtimes || [];
  const secs = Object.keys(caps).map((capId) => {
    const mine = rows.filter((r) => (r.powers || []).includes(capId));
    if (!mine.length) return null;
    const live = mine.filter((r) => r.available).length;
    return (
      <div key={capId} className="gen-sec">
        <div className="gen-sech">
          <span>{caps[capId]}</span>
          <span className="n">{live} of {mine.length} running here</span>
        </div>
        <div className="gen-grid wide">
          {mine.map((r) => <RuntimeCard key={r.id} rt={r} onData={setData} />)}
        </div>
      </div>
    );
  }).filter(Boolean);
  return (
    <div className="gen-wrap" data-panel="local-studio">
      <div className="gen-head">
        <span className="gen-eyebrow">No key, no bill, nothing leaves the machine</span>
        <h3>On this machine</h3>
      </div>
      <p className="gen-note">
        The other half of the answer above. A capability is available if EITHER
        a provider has a key or something local is running — these are the local
        ones. Set them up in <b>Settings → Generators</b>.
      </p>
      {secs.length ? secs : <div className="gen-none">nothing local is registered.</div>}
    </div>
  );
}
