import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Button, Checkbox, TextInput } from "@mantine/core";
import { Ti } from "../Ti";
import { mutate, readJSON, toast } from "../../bridge";
import { useEvents } from "../../hooks";
import { askConfirm } from "./confirm";
import "./generators.css";

/* The art-generation API keys, from the two places you are standing when you
 * need to. Ported from providerkeys.js; the rules it stated still hold:
 *
 * ONE CAPABILITY, TWO DOORS, DELIBERATELY DIFFERENT SHAPES.
 *   Settings → the management surface. Every provider, every state, set and
 *              clear, and the sentence about where the key is stored.
 *   Studio   → the "Generators" tab, organised by WHAT YOU WANTED TO MAKE. You
 *              are here because a generator will not run; the card that cannot
 *              run says why and carries the fix. Same card underneath, so the
 *              two cannot drift.
 *
 * THE PANEL NEVER RECEIVES A KEY, so it can never render one. GET /api/providers
 * returns presence, a last-4 fingerprint and the adapter's own reason it cannot
 * run — there is no field on the wire that widens to the value. The input is
 * write-only: it is cleared the instant a save lands, whether or not it
 * worked, and its value goes nowhere but the POST body. This project committed
 * an API key once already; the rule that follows from that is that the value
 * has exactly one journey, keystrokes → POST body, and no branch off it.
 *
 * THE ROW REPAINTS FROM THE RESPONSE, never from what was sent. A key can save
 * correctly and the provider still be unusable — the `openai` package missing,
 * a shell variable shadowing the .env — and a card that painted "ready" out of
 * its own optimism would send somebody off to debug a generator that was never
 * the problem.
 *
 * WRITING IS HUMAN-ONLY. Both writes go through the page's mutate(), which
 * carries the bearer token; the server refuses a dispatched agent's session.
 * This is deliberately not an MCP tool.
 */

export type Provider = {
  id: string; label: string; env: string; help: string; reason?: string;
  available: boolean; configured: boolean; in_env_file?: boolean; in_global_file?: boolean;
  scope?: string; source?: string; last4?: string; key_url?: string;
  powers?: string[]; power_labels?: string[];
};
export type ProvidersData = {
  providers: Provider[];
  capabilities?: Record<string, string>;
  env_gitignored?: boolean;
  global_env?: string;
  scratch_root?: string; scratch_exists?: boolean; scratch_active?: boolean;
};
type Balance = { id: string; keyed?: boolean; balance?: number | null; balance_unit?: string };
type Balances = Record<string, Balance>;

const EMPTY: ProvidersData = { providers: [] };

/* Why a provider is or is not usable, as one word plus a colour. `key set`
   rather than `ready` for a key that saved into an adapter that still cannot
   run: "the key is fine, the leg is not" is the distinction that stops somebody
   re-pasting a working key three times. */
const STATE = {
  ready: { word: "ready", tone: "good" },
  blocked: { word: "key set", tone: "warn" },
  unset: { word: "no key", tone: "" },
} as const;

function stateOf(row: Provider): keyof typeof STATE {
  if (row.available) return "ready";
  return row.configured || row.in_env_file ? "blocked" : "unset";
}

/** The providers read, shared by the Settings panel and the Studio tab. */
export function useProviders(active: boolean) {
  const [data, setData] = useState<ProvidersData & { __error?: string }>(EMPTY);
  const [balances, setBalances] = useState<Balances>({});
  const balBusy = useRef(false);

  const refresh = useCallback(async () => {
    setData(await readJSON<ProvidersData>("/api/providers", EMPTY));
  }, []);
  /* A card is not live data: no server event describes a key, so the read is
     on arrival, after a write, and on the slow safety-net. */
  useEvents(refresh, { enabled: active, kinds: ["settings.*"], fallbackMs: 60000 });

  /* THE MONEY ROW. Separate from the status read because it probes the
     network per provider (the gateway caches ~2 minutes server-side); the key
     panel paints offline-fast and the balances arrive as a second coat.
     fresh=true is the button a human presses after topping an account up. */
  const loadBalances = useCallback(async (fresh: boolean) => {
    if (balBusy.current) return;
    balBusy.current = true;
    try {
      const d = await readJSON<{ providers?: Balance[] }>(
        "/api/providers/balances" + (fresh ? "?fresh=1" : ""), {});
      if (d && !d.__error && d.providers) {
        const next: Balances = {};
        d.providers.forEach((r) => { next[r.id] = r; });
        setBalances(next);
      }
    } finally { balBusy.current = false; }
  }, []);
  useEffect(() => { if (active) loadBalances(false); }, [active, loadBalances]);

  return { data, setData, refresh, balances, loadBalances };
}

/* Where the key goes, said once and said plainly. This is the product's answer
   to the incident in CLAUDE.md, so it names the file and the fact that it is
   ignored rather than leaving either to be assumed. */
export function StorageNote({ data }: { data: ProvidersData }) {
  const bad = data.env_gitignored === false;
  return (
    <>
      <p className="gen-note">
        Keys are written to a <code>.env</code> file — never into the database,
        the board, or this dashboard's own files — and take effect immediately,
        with no restart. Nothing here ever reads a key back: a saved key shows
        as a state and its last four characters, and that is all the server
        will send.
      </p>
      <p className="gen-note">
        There are two places to put one. This game's own <code>.env</code> keeps
        it to this project. Ticking <b>save for every project on this
        machine</b> writes <code>{data.global_env || "~/.bgate/.env"}</code>{" "}
        instead, which every project inherits and which is the only store that
        exists when you are not in a project at all. A project's own key wins
        over it, and a variable exported in your shell wins over both.
      </p>
      {data.scratch_root && (
        <p className="gen-note">
          Generations made with no project open land in{" "}
          <code>{data.scratch_root}</code>
          {data.scratch_exists ? "" : " (created the first time something needs it)"}
          {" "}— a real project, so they get the same artifact registry and
          review queue as anything else.
          {data.scratch_active && <b> This dashboard is looking at it right now.</b>}
        </p>
      )}
      {bad && (
        <div className="gen-warn">
          <Ti name="alert-triangle" size={15} />
          <span>
            <b>This project's <code>.env</code> is not gitignored.</b> Saving a
            key will add the ignore rule first — but check <code>git status</code>{" "}
            before you commit, in case a key is already tracked.
          </span>
        </div>
      )}
    </>
  );
}

function BalanceNote({ bal }: { bal?: Balance }) {
  /* null balance is UNKNOWN (openai never says; krea's only surfaces as a 402
     at call time) and must never render as zero — an agent already made that
     exact mistake and hand-rolled a sprite over it. */
  if (!bal || !bal.keyed) return null;
  if (bal.balance == null) return <span>· balance: provider won't say</span>;
  const unit = bal.balance_unit || "credits";
  return Number(bal.balance) <= 0
    ? <span className="gen-drained">· DRAINED — 0 {unit} left</span>
    : <span>· {bal.balance} {unit} left</span>;
}

type CardProps = {
  row: Provider;
  data: ProvidersData;
  balance?: Balance;
  onData: (d: ProvidersData) => void;
};

export function ProviderCard({ row, data, balance, onData }: CardProps) {
  const st = stateOf(row), lamp = STATE[st];
  /* Write-only. State rather than a ref so the field can be blanked the
     instant the save returns; never copied anywhere else. */
  const [key, setKey] = useState("");
  const [global, setGlobal] = useState(row.scope === "global" && !row.in_env_file);
  const [busy, setBusy] = useState("");
  useEffect(() => { setGlobal(row.scope === "global" && !row.in_env_file); },
            [row.scope, row.in_env_file]);

  async function save() {
    if (busy) return;
    if (!key.trim()) { toast("paste the key first"); return; }
    const scope = global ? "global" : "project";
    /* Read BEFORE the write: the "we edited your .gitignore" notice is derived
       from the change in this flag. `env_gitignored` rides on every providers
       read, so false -> true is the fact, from a key that survives. */
    const wasIgnored = data.env_gitignored !== false;
    setBusy("save");
    const r = await mutate<ProvidersData>(`/api/providers/${encodeURIComponent(row.id)}/key`,
      { method: "POST", body: { key, scope }, quiet: true });
    /* CLEARED WHETHER OR NOT IT WORKED, and before anything else runs. A key
       left sitting in a focused input survives a screen share, a screenshot
       and the browser's own form restore on reload. */
    setKey("");
    setBusy("");
    if (!r.ok || !r.data) { toast(r.error || "the save failed"); return; }
    onData(r.data);
    const now = (r.data.providers || []).find((p) => p.id === row.id) || ({} as Partial<Provider>);
    const nowIgnored = r.data.env_gitignored !== false;
    const name = now.label || row.id;
    if (!wasIgnored && nowIgnored) {
      toast("saved - and added the .env ignore rule to .gitignore first", "ok");
    } else if (now.available && scope === "global" && now.source === "env_file") {
      /* The write landed and something else is still winning. Silence here
         reads as "it did not work", and the next thing anyone does is paste it
         again into the same box. */
      toast(`saved for every project - but THIS project's own .env still supplies `
            + `${name}, so the global key applies everywhere else`, "ok");
    } else if (now.available) {
      toast(scope === "global"
            ? `${name} is ready, for every project on this machine`
            : `${name} is ready`, "ok");
    } else {
      /* Saved but still not usable. Saying "saved" alone here is the lie this
         panel exists to avoid — the reason is the actionable half. */
      toast(`key saved, but ${name} still cannot run: ${now.reason || "unknown"}`);
    }
  }

  async function clear() {
    if (busy) return;
    /* Clear the store the key is ACTUALLY IN, not whatever the save toggle
       happens to be set to — "clear" means "make this stop being in force",
       and deleting from the empty store would report success and change
       nothing. */
    const scope = row.scope || "project";
    const file = scope === "global" ? (data.global_env || "~/.bgate/.env") : "the project's .env";
    const alsoGlobal = scope === "project" && row.in_global_file;
    const yes = await askConfirm({
      title: `Forget the ${row.label || row.id} key?`,
      body: `${row.env || ""} is removed from ${file} and from this running dashboard. `
        + (alsoGlobal
          ? "Your machine-wide key stays, and takes over here — this only removes this project's override."
          : `Anything that generates with ${row.label || row.id} stops until you paste it again — `
            + "and this cannot be undone from here, because nothing here ever held a copy."),
      ok: "clear it", cancel: "keep it", danger: true,
    });
    if (!yes) return;
    setBusy("clear");
    const r = await mutate<ProvidersData>(
      `/api/providers/${encodeURIComponent(row.id)}/key?scope=${encodeURIComponent(scope)}`,
      { method: "DELETE", quiet: true });
    setBusy("");
    if (!r.ok || !r.data) { toast(r.error || "the clear failed"); return; }
    onData(r.data);
    const now = (r.data.providers || []).find((p) => p.id === row.id);
    toast(now?.configured
          ? `${row.label || row.id} override cleared - the machine-wide key applies here now`
          : `${row.label || row.id} key cleared`, "ok");
  }

  /* Enter saves. Without it the only way to commit is the button, and "I
     pasted it and pressed enter" is the most common way to believe a
     credential was stored when it was not. */
  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") { e.preventDefault(); save(); }
  }

  const src = row.configured
    ? (row.source === "environment"
      ? "from the shell environment, not this project's .env"
      : "in .env")
    : "";

  return (
    <div className={`gen-card s-${st}`} data-provider={row.id}>
      <div className="gen-top">
        <Ti name="lock" size={15} />
        <span className="gen-name">{row.label}</span>
        <span className={`gen-lamp ${lamp.tone}`}>{lamp.word}</span>
      </div>
      <div className="gen-pills">
        {(row.power_labels || []).map((p) => <span key={p} className="gen-pill">{p}</span>)}
      </div>
      <div className="gen-help">{row.help}</div>
      {row.reason && <div className="gen-why">{row.reason}</div>}
      <div className="gen-row">
        <TextInput size="xs" type="password" className="grow gen-key"
                   value={key} onChange={(e) => setKey(e.currentTarget.value)}
                   onKeyDown={onKey}
                   autoComplete="off" autoCapitalize="off" autoCorrect="off"
                   spellCheck={false} aria-label={`${row.label} API key`}
                   placeholder={row.configured ? "paste a new key to replace it" : `paste ${row.env}`} />
        <Button size="xs" variant="outline" loading={busy === "save"} onClick={save}>save</Button>
        {(row.in_env_file || row.in_global_file) && (
          <Button size="xs" variant="default" loading={busy === "clear"} onClick={clear}>clear</Button>
        )}
      </div>
      <Checkbox size="xs" className="gen-scope" checked={global}
                onChange={(e) => setGlobal(e.currentTarget.checked)}
                label="save for every project on this machine"
                title={"Writes ~/.bgate/.env instead of this game's. Every project on this "
                       + "machine inherits it, and it is the only store that exists when you "
                       + "are not in a project at all. A project's own key still wins over it."} />
      <div className="gen-foot plain">
        <span>{row.env}</span>
        {/* The fingerprint is the ONLY thing about the value that is ever
            drawn, and four characters is what it takes to answer "is this the
            key I think it is" against the provider's own dashboard. */}
        {row.last4 && <span className="gen-fp">····{row.last4}</span>}
        {src && <span>· {src}</span>}
        <BalanceNote bal={balance} />
        {row.key_url && (
          <a className="gen-link" href={row.key_url} target="_blank" rel="noopener noreferrer">
            get a key ↗
          </a>
        )}
      </div>
    </div>
  );
}

/* ---- Settings: the management surface ---- */
export function ProviderKeys({ active }: { active: boolean }) {
  const { data, setData, balances, loadBalances } = useProviders(active);
  if (data.__error) {
    return <div className="gen-wrap"><div className="gen-none">could not read the providers — {data.__error}</div></div>;
  }
  const configured = data.providers.filter((p) => p.configured).length;
  const ready = data.providers.filter((p) => p.available).length;
  return (
    <div className="gen-wrap" data-panel="provider-keys">
      <div className="gen-toolbar">
        <button type="button" className="gen-link gen-balbtn"
                title="Balances are cached for ~2 minutes — press after topping an account up."
                onClick={() => loadBalances(true)}>
          Check balances
        </button>
      </div>
      <div className={`gen-sum${ready ? " good" : ""}`}>
        <Ti name="stethoscope" size={13} />
        <span>{ready} of {data.providers.length} ready, {configured} configured</span>
      </div>
      <StorageNote data={data} />
      <div className="gen-grid">
        {data.providers.map((r) => (
          <ProviderCard key={r.id} row={r} data={data} balance={balances[r.id]} onData={setData} />
        ))}
      </div>
    </div>
  );
}

/* ---- Studio: GROUPED BY CAPABILITY, NOT BY VENDOR. On Studio the question is
   "why can I not make a 3D model", and the answer is a list of the providers
   that make 3D models with their state on it. ---- */
export function ProviderStudio({ active }: { active: boolean }) {
  const { data, setData, balances } = useProviders(active);
  if (data.__error) {
    return <div className="gen-wrap"><div className="gen-none">could not read the providers — {data.__error}</div></div>;
  }
  const caps = data.capabilities || {};
  const rows = data.providers;
  return (
    <div className="gen-wrap" data-panel="provider-studio">
      <div className="gen-head">
        <span className="gen-eyebrow">What can run right now</span>
        <h3>Generators</h3>
      </div>
      <StorageNote data={data} />
      {Object.keys(caps).map((capId) => {
        const mine = rows.filter((r) => (r.powers || []).includes(capId));
        const live = mine.filter((r) => r.available).length;
        return (
          <div key={capId} className="gen-sec">
            <div className="gen-sech">
              <span>{caps[capId]}</span>
              <span className="n">{mine.length ? `${live} of ${mine.length} ready` : "no provider wired yet"}</span>
            </div>
            {mine.length ? (
              <div className="gen-grid">
                {mine.map((r) => (
                  <ProviderCard key={r.id} row={r} data={data} balance={balances[r.id]} onData={setData} />
                ))}
              </div>
            ) : (
              <div className="gen-none">
                Nothing here generates {caps[capId]} yet. When a provider does, it
                appears here — the list comes from the registry, not from this page.
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
