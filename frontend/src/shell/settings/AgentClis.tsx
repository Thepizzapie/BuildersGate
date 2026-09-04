import { useCallback, useState } from "react";
import { Button } from "@mantine/core";
import { Ti } from "../Ti";
import { mutate, readJSON, toast } from "../../bridge";
import { useEvents } from "../../hooks";
import { askConfirm, copyText } from "./confirm";
import "./generators.css";

/* Agent CLIs — Settings → Generators, and NOWHERE ELSE. Deliberately absent
 * from Studio, which is about making things. The question here is PLUMBING:
 * is Claude Code installed and is Builders Gate registered with it, against
 * which interpreter. Touched once at setup and then only when something breaks.
 *
 * Ported from localsetup.js. Backed by bgate_ui/agents/agentcli.py through
 * /api/local/agents: a row per client, installed or not, the registration it
 * has and the verdict on it, the command a human would type (cli-kind clients)
 * or the block to paste (file-kind clients, which are never written from here
 * BY DESIGN — the button's absence is the promise not to merge somebody's
 * hand-edited JSON), plus one generic block for a client nobody here has heard
 * of.
 *
 * No poll: a CLI does not start answering while you watch. Read on arrival and
 * after every write. The register / remove / verify writes are human-only on
 * the server, and register asks first, because it lands in the home directory
 * and changes every future session of that CLI.
 */

type Mcp = {
  ok?: boolean; found?: boolean; state?: string; verdict?: string; kind?: string;
  command?: string; args?: string[]; path?: string; config_path?: string;
  scope_note?: string; how?: string; command_line?: string; block?: string;
  can_register?: boolean; error?: string;
};
type Runner = {
  id: string; label: string; installed: boolean; path?: string; note?: string;
  dispatches?: boolean; steerable?: boolean; cost_tracked?: boolean;
  used_for?: string; default_runner?: boolean; install_hint?: string; mcp?: Mcp;
};
type AgentsData = {
  runners: Runner[]; interpreter?: string; server?: string;
  generic_block?: string; why_absolute?: string;
};
type Verify = { ok?: boolean; detail?: string; error?: string; output?: string };

const EMPTY: AgentsData = { runners: [] };

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

function AgentCard({ r, why, onData }: {
  r: Runner; why?: string; onData: (d: AgentsData) => void;
}) {
  const m = r.mcp || {};
  const wired = !!m.ok;
  const fileKind = m.kind === "file";
  const lamp = !r.installed ? "" : (wired ? "good" : "warn");
  const word = !r.installed ? "not installed" : (wired ? "wired" : "check wiring");
  const stage = wired ? "ready" : (r.installed ? "unhealthy" : "unconfigured");
  const [busy, setBusy] = useState("");
  /* The result line under the buttons — a verify's answer, a register's
     "restart it first". Local to the card so a re-read cannot drop it. */
  const [said, setSaid] = useState<{ text: string; tone: string } | null>(null);
  const say = (text: string, tone = "") => setSaid({ text, tone });

  async function register() {
    if (busy) return;
    const yes = await askConfirm({
      title: `Register Builders Gate with ${r.id}?`,
      body: "This writes to that CLI's own config in your home directory, not to "
        + "this project. Every future session of it — in any directory, on any "
        + "project — gets the Builders Gate tools. It is pinned to the interpreter "
        + "this dashboard is running on, which is the part that is usually wrong. "
        + "You can remove it again from here.",
      ok: "register it", cancel: "not now",
    });
    if (!yes) return;
    setBusy("register");
    say("asking the CLI to register it…");
    const res = await mutate<AgentsData>(`/api/local/agents/${encodeURIComponent(r.id)}/register`,
      { method: "POST", quiet: true });
    setBusy("");
    if (!res.ok || !res.data) { say(res.error || "registration failed", "warn"); toast(res.error || "registration failed"); return; }
    onData(res.data);
    toast("registered - restart that CLI before the tools appear", "ok");
    say("registered. A CLI already running will not see it until you restart it.", "good");
  }

  async function unregister() {
    if (busy) return;
    setBusy("remove");
    const res = await mutate<AgentsData>(`/api/local/agents/${encodeURIComponent(r.id)}/register`,
      { method: "DELETE", quiet: true });
    setBusy("");
    if (!res.ok || !res.data) { say(res.error || "it could not be removed", "warn"); return; }
    onData(res.data);
    toast("removed", "ok");
  }

  async function verify() {
    if (busy) return;
    setBusy("verify");
    say("asking that interpreter whether it can load the server…");
    const res = await mutate<Verify>(`/api/local/agents/${encodeURIComponent(r.id)}/verify`,
      { method: "POST", quiet: true });
    setBusy("");
    if (!res.ok) { say(res.error || "the verify failed", "warn"); return; }
    const d = res.data || {};
    say(d.ok ? (d.detail || "ok") : `${d.error || "it could not"} - ${d.output || ""}`,
        d.ok ? "good" : "warn");
  }

  const pasteText = fileKind ? (m.block || "") : (m.command_line || "");

  return (
    <div className={`gen-card s-${stage}`} data-agent={r.id}>
      <div className="gen-top">
        <Ti name="robot" size={15} />
        <span className="gen-name">{r.label}</span>
        <span className={`gen-lamp ${lamp}`}>{word}</span>
      </div>
      <div className="gen-pills">
        {r.default_runner && <span className="gen-pill">default runner</span>}
        {r.dispatches
          ? <>
              <span className="gen-pill">{r.steerable ? "steerable mid-run" : "no live steering"}</span>
              <span className="gen-pill">{r.cost_tracked ? "cost tracked" : "cost NOT tracked"}</span>
            </>
          : <span className="gen-pill">wiring only</span>}
      </div>
      <div className="gen-help">{r.used_for} {r.note || ""}</div>
      {r.installed
        ? <KV pairs={[["found at", r.path]]} />
        : (
          <div className="gen-why">
            Not on PATH. Install it and reload this page — nothing here installs software.
            {r.install_hint && <> Install: <code className="gen-mono">{r.install_hint}</code></>}
          </div>
        )}

      <div className="gen-f">
        <div className="gen-flab">
          <span className="n">Builders Gate MCP server</span>
          <span className="v">{m.scope_note || ""}</span>
        </div>
        <div className="gen-fhelp">{m.how || ""}</div>
        {m.verdict && <div className={wired ? "gen-ok" : "gen-why"}>{m.verdict}</div>}
        {m.error && <div className="gen-why">{m.error}</div>}
        {m.command && (
          <KV pairs={[["registered command", m.command],
                      ["args", (m.args || []).join(" ")],
                      ["config file", m.path]]} />
        )}
        {!wired && why && <div className="gen-fhelp" style={{ marginTop: 8 }}>{why}</div>}
        {fileKind && (
          <div className="gen-fhelp" style={{ marginTop: 8 }}>
            {r.label} has no <code className="gen-mono">mcp add</code>; paste this into{" "}
            <code className="gen-mono">{m.config_path || m.path}</code> yourself — nothing here
            edits a file you also edit by hand.
          </div>
        )}
        {pasteText && <pre className="gen-cmd">{pasteText}</pre>}
        <div className="gen-row">
          {!fileKind && (
            <Button size="xs" variant="outline" disabled={!m.can_register}
                    loading={busy === "register"} onClick={register}>
              {m.found ? "re-register, pinned" : "register"}
            </Button>
          )}
          {m.found && !fileKind && (
            <Button size="xs" variant="default" loading={busy === "verify"} onClick={verify}>verify</Button>
          )}
          {m.found && !fileKind && (
            <Button size="xs" variant="default" loading={busy === "remove"} onClick={unregister}>remove</Button>
          )}
          {pasteText && (
            <Button size="xs" variant="default"
                    onClick={() => copyText(pasteText, toast, fileKind ? "block copied" : "command copied")}>
              {fileKind ? "copy the block" : "copy the command"}
            </Button>
          )}
        </div>
        {said && <div className="gen-fnote"><span className={said.tone}>{said.text}</span></div>}
      </div>
    </div>
  );
}

export function AgentClis({ active }: { active: boolean }) {
  const [data, setData] = useState<AgentsData & { __error?: string }>(EMPTY);
  const refresh = useCallback(async () => {
    setData(await readJSON<AgentsData>("/api/local/agents", EMPTY));
  }, []);
  /* Read on arrival; no timer. A registration changes when a button here is
     pressed, and every button re-reads. */
  useEvents(refresh, { enabled: active, kinds: ["settings.*"], fallbackMs: 0 });

  if (data.__error) {
    return <div className="gen-wrap"><div className="gen-none">could not read the coding-agent CLIs — {data.__error}</div></div>;
  }
  const rows = data.runners || [];
  const wired = rows.filter((r) => r.installed && r.mcp?.ok).length;
  const on = rows.filter((r) => r.installed).length;
  return (
    <div className="gen-wrap" data-panel="agent-clis">
      <div className={`gen-sum${wired ? " good" : ""}`}>
        <Ti name="stethoscope" size={13} />
        <span>{on} of {rows.length} installed, {wired} wired to this interpreter</span>
      </div>
      <p className="gen-note">
        Registering an MCP server writes to that CLI's own config in your home
        directory, not to this project — every future session of it, in any
        directory, gets the Builders Gate tools. It is pinned to the interpreter
        this dashboard runs on
        {data.interpreter && <> (<code>{data.interpreter}</code>)</>}, which is
        the part that is usually wrong.
      </p>
      <div className="gen-grid wide">
        {rows.length
          ? rows.map((r) => <AgentCard key={r.id} r={r} why={data.why_absolute} onData={setData} />)
          : <div className="gen-none">no coding-agent CLI is described in this build.</div>}
      </div>
      {data.generic_block && (
        <div className="gen-card" style={{ marginTop: 12 }} data-agent="generic">
          <div className="gen-top">
            <Ti name="plug" size={15} />
            <span className="gen-name">Any other MCP client</span>
          </div>
          <div className="gen-help">
            Every MCP client that is not one of the above still speaks this
            object. It names this exact interpreter, so it is the one to paste
            rather than the docs' placeholder.
          </div>
          <pre className="gen-cmd">{data.generic_block}</pre>
          <div className="gen-row">
            <Button size="xs" variant="default"
                    onClick={() => copyText(data.generic_block || "", toast, "block copied")}>
              copy the block
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
