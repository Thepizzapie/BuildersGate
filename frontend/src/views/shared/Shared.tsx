/* SHARED: what every game in the studio can draw on.
 *
 * One read of /api/studio-index: the studio's games, the modules under
 * shared/modules (with the versions on disk and which game pins which), and
 * the shared asset store. Read only. Publishing a module is kit_extract and
 * moving a game in is `bgate studio adopt`; neither is a dashboard click.
 *
 * NAMED "SHARED", NOT "STUDIO", because the Build area already has a Studio
 * screen (the generator graph, flows.js). Two rail entries with one name
 * would be a guess every time.
 */
import { useCallback, useRef, useState } from "react";
import { readJSON } from "../../bridge";
import { useEvents, useViewActive } from "../../hooks";
import "./shared.css";

type Game = { name: string; path: string; exists: boolean };
type Pin = { game: string; version: string };
type Module = { engine: string; name: string; title: string; summary: string;
  versions: string[]; pinned_by: Pin[] };
type Asset = { id: string; name: string; kind: string; collection?: string;
  tags?: string[]; bytes?: number; dims?: number[] | null };
type Assets = { home: string; count: number; bytes: number;
  kinds: Record<string, number>; recent: Asset[] };
type Index = { set: boolean; ok: boolean; root: string | null; error?: string;
  games: Game[]; modules: Module[]; assets: Assets | null };

const EMPTY: Index = { set: false, ok: false, root: null, games: [], modules: [], assets: null };

function size(bytes = 0): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function Shared() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [data, setData] = useState<Index>(EMPTY);
  const [readError, setReadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const got = await readJSON<Index>("/api/studio-index", EMPTY);
    setReadError(got.__error || null);
    if (!got.__error) setData(got);
  }, []);
  useEvents(refresh, { enabled: active });

  /* ONE ROOT ELEMENT, always. The host carries the ref useViewActive reads,
     and swapping the island's root between a <div> and a <main> as the data
     arrived broke React's reconcile against the deck (removeChild on a node
     it no longer owned), leaving the screen blank. */
  return <main ref={host} className="sh-page">{body(data, readError)}</main>;
}

function body(data: Index, readError: string | null) {
  if (readError) return <div className="sh-empty err">Could not read the studio: {readError}</div>;
  if (!data.set) {
    return (
      <div className="sh-empty">
        No studio is set. Run <code>bgate studio init &lt;folder&gt;</code> to give your games
        one place to share modules and assets.
      </div>
    );
  }
  if (!data.ok) {
    return <div className="sh-empty err">The studio at <code>{data.root}</code> is {data.error}.</div>;
  }
  const assets = data.assets;
  return (
    <>
      <p className="sh-root"><code>{data.root}</code></p>

      <section className="sh-panel">
        <h3>Games <span>{data.games.length}</span></h3>
        {data.games.length ? (
          <ul className="sh-rows">
            {data.games.map((g) => (
              <li key={g.name}>
                <b>{g.name}</b>
                <code>{g.path}</code>
                {!g.exists && <em className="sh-bad">missing</em>}
              </li>
            ))}
          </ul>
        ) : <p className="sh-none">No games yet. <code>bgate studio adopt &lt;project&gt;</code> brings one in.</p>}
      </section>

      <section className="sh-panel">
        <h3>Modules <span>{data.modules.length}</span></h3>
        {data.modules.length ? (
          <ul className="sh-rows">
            {data.modules.map((m) => (
              <li key={`${m.engine}/${m.name}`}>
                <b>{m.title || m.name}</b>
                <span className="sh-tag">{m.engine}</span>
                <span className="sh-versions">{m.versions.join(", ")}</span>
                <span className="sh-pins">
                  {m.pinned_by.length
                    ? m.pinned_by.map((p) => `${p.game}@${p.version}`).join(", ")
                    : "not pinned"}
                </span>
                {m.summary && <small>{m.summary}</small>}
              </li>
            ))}
          </ul>
        ) : <p className="sh-none">No modules yet. <code>kit_extract</code> publishes one from a game.</p>}
      </section>

      <section className="sh-panel">
        <h3>Assets <span>{assets?.count ?? 0}</span></h3>
        {assets && assets.count ? (
          <>
            <p className="sh-sub">
              {size(assets.bytes)} in <code>{assets.home}</code> ·{" "}
              {Object.entries(assets.kinds).map(([k, n]) => `${n} ${k}`).join(", ")}
            </p>
            <ul className="sh-rows">
              {assets.recent.map((a) => (
                <li key={a.id}>
                  <b>{a.name}</b>
                  <span className="sh-tag">{a.kind}</span>
                  <span className="sh-versions">{a.dims ? `${a.dims[0]}×${a.dims[1]}` : size(a.bytes)}</span>
                  <span className="sh-pins">{[a.collection, ...(a.tags || [])].filter(Boolean).join(", ")}</span>
                </li>
              ))}
            </ul>
          </>
        ) : <p className="sh-none">Nothing shared yet. <code>library_publish</code> adds files from a game.</p>}
      </section>
    </>
  );
}
