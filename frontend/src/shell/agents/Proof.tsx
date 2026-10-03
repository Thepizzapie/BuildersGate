import { useEffect, useState } from "react";
import { Ti } from "../Ti";
import { mutate, readJSON } from "../../bridge";

/* PROOF — what the harness recorded for a ticket, not what the agent said.
 *
 * USER DIRECTIVE: "agent statements are not evidence to me". Commits the
 * harness made (with the diff), suite runs from the test runner's own history,
 * check tools' returned verdicts, the images they produced, and a button that
 * runs the suite from here. The agent's note is shown last, labelled a claim.
 */

type FileRow = { path: string; added: number | null; removed: number | null };
type Commit = { sha: string; at: string; subject: string; files: FileRow[] };
type TestRun = { at: string; by: string; item: number | null; ok: boolean; passed: number;
  failures: number; scripts_run: number; scripts_failed: number; seconds: number | null;
  failing: string[]; error: string };
type Check = { item: number; tool: string; ok: boolean | null; summary: string; images: string[] };
type Shot = { path: string; at: string; by: string; errors: string[] };
type ProofData = { item_id: number; related: number[]; commits: Commit[]; tests: TestRun[];
  checks: Check[]; images: string[]; shots?: Shot[]; visible?: boolean; claim: string; __error?: string };

const EMPTY: ProofData = { item_id: 0, related: [], commits: [], tests: [], checks: [], images: [], claim: "" };

function who(by: string, id: number): string {
  if (by.startsWith("human")) return "you, from the dashboard";
  const n = Number(by.replace("agent:item-", ""));
  return n === id ? "this ticket's agent" : `QA gate #${n}`;
}

function when(at: string): string {
  const t = Date.parse(at);
  return Number.isNaN(t) ? at : new Date(t).toLocaleString();
}

function Verdict({ ok }: { ok: boolean | null }) {
  if (ok === null) return <span className="bgp-v">?</span>;
  return <span className={`bgp-v ${ok ? "pass" : "fail"}`}>{ok ? "PASS" : "FAIL"}</span>;
}

export function Proof({ id, compact = false }: { id: number; compact?: boolean }) {
  const [data, setData] = useState<ProofData | null>(null);
  const [diff, setDiff] = useState<{ sha: string; text: string } | null>(null);
  const [running, setRunning] = useState(false);
  const [shooting, setShooting] = useState(false);
  const [shotErr, setShotErr] = useState("");
  const [big, setBig] = useState<string | null>(null);

  async function load() {
    setData(await readJSON<ProofData>(`/api/lifecycle/proof/${id}`, EMPTY));
  }
  useEffect(() => { setData(null); setDiff(null); void load(); }, [id]);   // eslint-disable-line react-hooks/exhaustive-deps

  async function showDiff(sha: string) {
    if (diff?.sha === sha) { setDiff(null); return; }
    const got = await readJSON<{ diff: string }>(`/api/lifecycle/proof/${id}/diff?sha=${sha}`, { diff: "" });
    setDiff({ sha, text: got.__error ? `could not read the diff: ${got.__error}` : got.diff });
  }
  async function runNow() {
    setRunning(true);
    await mutate(`/api/lifecycle/proof/${id}/run`, { quiet: true });
    setRunning(false);
    void load();
  }

  async function shootNow() {
    setShooting(true);
    setShotErr("");
    const r = await mutate<{ ok: boolean; error?: string }>(`/api/lifecycle/proof/${id}/shot`, { quiet: true });
    setShooting(false);
    if (r.data && !r.data.ok) setShotErr(r.data.error || "screenshot failed");
    void load();
  }

  if (!data) return <div className="bgp"><div className="bgp-dim">reading the record…</div></div>;
  if (data.__error) return <div className="bgp"><div className="bgp-dim">could not read the record: {data.__error}</div></div>;
  const ownTests = data.tests.filter((t) => t.item === id && !t.by.startsWith("human"));

  return (
    <div className={`bgp ${compact ? "compact" : ""}`}>
      <div className="bgp-h">
        <b>Evidence</b><span className="bgp-dim">recorded by the harness, not written by the agent</span>
        <span style={{ flex: 1 }} />
        <button className="bgs-btn" disabled={running} onClick={runNow}>
          <Ti name="player-play" size={12} /> {running ? "running the suite…" : "Run the tests now"}</button>
        <button className="bgs-btn" disabled={shooting} onClick={shootNow}>
          <Ti name="camera" size={12} /> {shooting ? "photographing…" : "Take a screenshot now"}</button>
      </div>
      {shotErr && <div className="bgp-fail">{shotErr}</div>}

      <div className="bgp-sec">
        <div className="bgp-label">Test runs</div>
        {!data.tests.length && <div className="bgp-miss">No test run is recorded for this ticket.</div>}
        {!!data.tests.length && !ownTests.length &&
          <div className="bgp-miss">This ticket's own agent never ran the suite.</div>}
        {data.tests.map((t, i) => (
          <div key={i} className="bgp-row">
            <Verdict ok={t.ok} />
            <span>{t.passed} passed, {t.failures} failed across {t.scripts_run} file{t.scripts_run === 1 ? "" : "s"}</span>
            <span className="bgp-dim">{who(t.by, id)} · {when(t.at)}{t.seconds != null ? ` · ${t.seconds}s` : ""}</span>
            {!!t.failing.length && <div className="bgp-fail">failing: {t.failing.join(", ")}</div>}
            {t.error && <div className="bgp-fail">{t.error}</div>}
          </div>
        ))}
      </div>

      <div className="bgp-sec">
        <div className="bgp-label">Commits</div>
        {!data.commits.length && <div className="bgp-miss">Nothing committed under this ticket.</div>}
        {data.commits.map((c) => (
          <div key={c.sha} className="bgp-row">
            <button className="bgp-sha" onClick={() => showDiff(c.sha)}>{c.sha.slice(0, 8)}</button>
            <span className="bgp-dim">{when(c.at)}</span>
            <div className="bgp-files">
              {c.files.map((f) => <div key={f.path}>
                <span className="add">+{f.added ?? "bin"}</span> <span className="del">-{f.removed ?? "bin"}</span> {f.path}</div>)}
            </div>
            {diff?.sha === c.sha && <pre className="bgp-diff">{diff.text.split("\n").map((l, i) =>
              <span key={i} className={l.startsWith("+") && !l.startsWith("+++") ? "add"
                : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : ""}>{l}{"\n"}</span>)}</pre>}
          </div>
        ))}
      </div>

      <div className="bgp-sec">
        <div className="bgp-label">Checks the run called</div>
        {!data.checks.length && <div className="bgp-miss">No check tool was called.</div>}
        {data.checks.map((c, i) => (
          <div key={i} className="bgp-row">
            <Verdict ok={c.ok} /> <code>{c.tool}</code>
            <span className="bgp-dim">{c.item === id ? "this ticket's agent" : `QA gate #${c.item}`}</span>
            {c.summary && <div className="bgp-dim mono">{c.summary}</div>}
          </div>
        ))}
      </div>

      {(!!data.images.length || data.visible) && (
        <div className="bgp-sec">
          <div className="bgp-label">Images</div>
          {!data.images.length && <div className="bgp-miss">No screenshot of this on-screen ticket yet.</div>}
          <div className="bgp-shots">
            {data.images.map((p) => {
              const src = `/api/lifecycle/proof-image?path=${encodeURIComponent(p)}`;
              const shot = (data.shots || []).find((s) => s.path === p);
              return <figure key={p} className="bgp-fig">
                <button className="bgp-shot" onClick={() => setBig(src)} title={p}>
                  <img src={src} alt={p} loading="lazy" /></button>
                <figcaption className="bgp-dim">
                  {shot ? `${shot.by === "human" ? "you" : "harness"} · ${when(shot.at)}` : "from a check tool"}
                  {shot && shot.errors.length ? <span className="bgp-fail"> · {shot.errors.length} console error(s)</span> : null}
                </figcaption>
              </figure>;
            })}
          </div>
        </div>
      )}
      {big && <div className="bgp-big" onClick={() => setBig(null)}><img src={big} alt="" /></div>}

      {data.claim && (
        <details className="bgp-claim">
          <summary>What the agent says it did (a claim, not evidence)</summary>
          <div>{data.claim}</div>
        </details>
      )}
    </div>
  );
}
