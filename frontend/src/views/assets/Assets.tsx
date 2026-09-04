import { useMemo, useState, type ReactNode } from "react";
import { Badge, Button, Group, Table, TextInput } from "@mantine/core";
import { useAppState, type AssetGroup } from "../../store";
import { askText, mutate, pollQueue, pollState, previewURL, seatColor, toast, vaultChip } from "../../bridge";
import { setUrlParams, urlParam } from "../../shell/urlState";
import { Ti } from "../../shell/Ti";
import { AssetDrawer } from "./AssetDrawer";
import { assetCategory, catOrder, groupStatus, groupThumb } from "./categorise";
import "./assets.css";

type Filter = "all" | "review" | "approved" | "rejected";
type Mode = "review" | "library" | "integrity";
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" }, { id: "review", label: "To review" },
  { id: "approved", label: "Approved" }, { id: "rejected", label: "Rejected" },
];

function VaultState({ a, modified, missing, pending }: {
  a: { path: string; lock_seat?: string | null };
  modified: Set<string>; missing: Set<string>; pending: Set<string>;
}) {
  if (a.lock_seat) return <Badge variant="filled" style={{ background: seatColor(a.lock_seat), color: "var(--accent-fg)" }}>{a.lock_seat}</Badge>;
  const drift = modified.has(a.path) ? "drift" : missing.has(a.path) ? "missing" : pending.has(a.path) ? "pending" : null;
  return drift ? <Badge variant="light" color="red">{drift}</Badge> : <Badge variant="default">clean</Badge>;
}

export default function Assets() {
  const { asset_groups: groups, assets, verify, canon } = useAppState();
  const [mode, setMode] = useState<Mode>("review");
  const [filter, setFilter] = useState<Filter>("all");
  const [category, setCategory] = useState("all");
  const [search, setSearch] = useState("");
  const [drawer, setDrawer] = useState<string | null>(() => urlParam("asset") || null);
  const [auditing, setAuditing] = useState(false);

  const counts = useMemo(() => {
    const value = { all: groups.length, approved: 0, review: 0, rejected: 0 };
    groups.forEach((group) => { value[groupStatus(group)]++; });
    return value;
  }, [groups]);

  const categories = useMemo(() => {
    const found: Record<string, number> = {};
    groups.forEach((group) => {
      const name = assetCategory(group.logical_name, canon);
      found[name] = (found[name] || 0) + 1;
    });
    return catOrder(Object.keys(found)).map((name) => ({ name, count: found[name] }));
  }, [groups, canon]);

  const buckets = useMemo(() => {
    const needle = search.toLowerCase().trim();
    let shown = groups;
    if (filter !== "all") shown = shown.filter((group) => groupStatus(group) === filter);
    if (needle) shown = shown.filter((group) => group.logical_name.toLowerCase().includes(needle));
    const byCategory: Record<string, AssetGroup[]> = {};
    shown.forEach((group) => { (byCategory[assetCategory(group.logical_name, canon)] ||= []).push(group); });
    return catOrder(Object.keys(byCategory)).filter((name) => category === "all" || category === name)
      .map((name) => [name, byCategory[name]] as const);
  }, [groups, filter, category, search, canon]);

  const open = drawer ? groups.find((group) => group.logical_name === drawer) || null : null;
  const visibleCount = buckets.reduce((total, [, list]) => total + list.length, 0);

  async function review(id: number, status: string) {
    let note = "";
    if (status === "rejected") {
      const said = await askText({ title: "Reject this revision", label: "Reason for rejection (becomes precedent)",
        placeholder: "What is off-model, and against which reference?", ok: "Reject" });
      if (said == null) return;
      note = said;
    }
    const result = await mutate(`/api/artifacts/${id}/review`, { body: { status, note }, ok: `revision ${id} → ${status}` });
    if (result.ok) pollState();
  }

  async function regenerate(id: number) {
    const reason = await askText({ title: "Regenerate this revision", body: "This queues a paid image generation.",
      label: "What should the next revision improve?", ok: "Queue regeneration" });
    if (reason == null) return;
    const result = await mutate(`/api/artifacts/${id}/regenerate`, { body: { reason }, ok: "regeneration queued" });
    if (result.ok) { pollState(); pollQueue(); }
  }

  async function audit() {
    setAuditing(true); vaultChip("hashing…");
    let payload: { counts?: Record<string, number> } | null = null;
    let failed = "";
    try {
      const response = await fetch("/api/assets/verify", { method: "POST" });
      if (!response.ok) failed = `the integrity audit could not run · ${response.status}`;
      else payload = await response.json();
    } catch { failed = "backend unreachable — is the dashboard still running?"; }
    setAuditing(false);
    if (failed) { vaultChip("audit failed"); toast(failed); return; }
    const next = payload?.counts || {};
    const off = (next.modified || 0) + (next.missing || 0) + (next.pending || 0);
    toast(off ? `${off} tracked file(s) need attention — ${next.modified || 0} changed, ${next.missing || 0} missing, ${next.pending || 0} pending`
      : "integrity audit clean — every tracked file matches its record", off ? "" : "ok");
    pollState();
  }

  const vc = verify.counts || {};
  const drift = (vc.modified || 0) + (vc.missing || 0) + (vc.pending || 0);
  const modified = new Set((verify.modified || []).map((entry) => entry.path));
  const missing = new Set(verify.missing || []);
  const pending = new Set(verify.untracked_hash || []);
  const closeInspector = () => { setDrawer(null); setUrlParams({ asset: null }); };
  const switchMode = (next: Mode) => {
    setMode(next);
    if (next === "library") requestAnimationFrame(() => window.AssetLib?.activate?.());
  };

  return <div className="asset-desk">
    <header className="asset-desk-head">
      <div className="asset-desk-title"><h2>Assets</h2><span>{groups.length} production assets</span>
        {counts.review > 0 && <b>{counts.review} need review</b>}</div>
      <Group gap={6} wrap="nowrap" className="asset-desk-actions">
        <Button variant="default" size="compact-xs" leftSection={<Ti name="photo" size={13} />} onClick={() => window.SpriteEdit?.pick()}>Sprite editor</Button>
        <Button variant="default" size="compact-xs" leftSection={<Ti name="wave-sine" size={13} />} onClick={() => window.AudioLab?.pick()}>Audio lab</Button>
        <Button variant="default" size="compact-xs" leftSection={<Ti name="box" size={13} />} onClick={() => window.ModelEdit?.pick()}>3D viewer</Button>
      </Group>
    </header>

    <nav className="asset-modes" aria-label="Asset workspace">
      <ModeButton active={mode === "review"} icon="checks" label="Review" count={counts.review} onClick={() => switchMode("review")} />
      <ModeButton active={mode === "library"} icon="library" label="Library" count={groups.length} onClick={() => switchMode("library")} />
      <ModeButton active={mode === "integrity"} icon="shield-check" label="Integrity" count={drift} warn={drift > 0} onClick={() => switchMode("integrity")} />
    </nav>

    <section className="asset-mode asset-mode-library" hidden={mode !== "library"}>
      <div className="asset-mode-bar"><div><b>Project library</b><span>Files discovered across the current game project.</span></div>
        <Button variant="default" size="compact-xs" leftSection={<Ti name="refresh" size={13} />} onClick={() => window.AssetLib?.refresh()}>Rescan project</Button></div>
      <div id="asset-lib-root" />
    </section>

    {mode === "review" && <section className="asset-review-desk">
      <aside className="asset-filter-rail" aria-label="Review filters">
        <FilterSection label="Status">{FILTERS.map((item) => <FilterButton key={item.id} active={filter === item.id}
          label={item.label} count={counts[item.id]} dot={item.id} onClick={() => setFilter(item.id)} />)}</FilterSection>
        <FilterSection label="Type"><FilterButton active={category === "all"} label="Everything" count={groups.length} onClick={() => setCategory("all")} />
          {categories.map((item) => <FilterButton key={item.name} active={category === item.name} label={item.name}
            count={item.count} onClick={() => setCategory(item.name)} />)}</FilterSection>
      </aside>
      <main className="asset-review-main">
        <div className="asset-review-bar"><TextInput placeholder="Search production assets" value={search}
          leftSection={<Ti name="search" size={14} />} onChange={(event) => setSearch(event.currentTarget.value)} aria-label="Search production assets" />
          <span>{visibleCount} shown</span></div>
        <div className="asset-scroll">
          {!buckets.length && <div className="asset-empty"><Ti name="photo-off" size={22} /><b>No assets match</b>
            <span>Clear a filter or rescan the project library.</span></div>}
          {buckets.map(([name, list]) => <AssetSection key={name} name={name} groups={list} selected={drawer}
            onSelect={(logicalName) => { setDrawer(logicalName); setUrlParams({ asset: logicalName }); }} />)}
        </div>
      </main>
      {open ? <AssetDrawer group={open} inline onClose={closeInspector} onReview={review} onRegenerate={regenerate}
        onOpenSprite={(rel) => window.SpriteEdit?.open(rel)} onOpenModel={(rel) => window.ModelEdit?.open(rel)} />
      : <aside className="asset-inspector asset-inspector-empty"><Ti name="versions" size={26} /><b>Select an asset</b>
          <span>Compare revisions, inspect generation context, and approve the version that belongs in the game.</span></aside>}
    </section>}

    {mode === "integrity" && <section className="asset-integrity">
      <div className="asset-mode-bar"><div><b>{drift ? `${drift} files need attention` : "Every tracked file matches"}</b>
        <span>Compare project files with the hashes recorded by the vault.</span></div>
        <Button variant={drift ? "filled" : "default"} size="compact-sm" onClick={audit} loading={auditing}
          leftSection={<Ti name="shield-search" size={14} />}>Run integrity audit</Button></div>
      <div className="asset-integrity-summary"><span><b>{assets.length}</b> tracked</span><span><b>{vc.modified || 0}</b> changed</span>
        <span><b>{vc.missing || 0}</b> missing</span><span><b>{vc.pending || 0}</b> pending</span></div>
      <div className="vault-scroll"><Table stickyHeader highlightOnHover>
        <Table.Thead><Table.Tr><Table.Th>Path</Table.Th><Table.Th>Kind</Table.Th><Table.Th>Size</Table.Th><Table.Th>State</Table.Th></Table.Tr></Table.Thead>
        <Table.Tbody>{assets.map((asset) => <Table.Tr key={asset.path}><Table.Td className="path" title={asset.path}>{asset.path}</Table.Td>
          <Table.Td>{asset.kind}</Table.Td><Table.Td>{asset.bytes ? `${(asset.bytes / 1024).toFixed(1)}k` : "—"}</Table.Td>
          <Table.Td><VaultState a={asset} modified={modified} missing={missing} pending={pending} /></Table.Td></Table.Tr>)}
          {!assets.length && <Table.Tr><Table.Td colSpan={4}>No files are tracked yet.</Table.Td></Table.Tr>}
        </Table.Tbody>
      </Table></div>
    </section>}
    <div className="gallery" id="gallery" hidden />
  </div>;
}

function ModeButton({ active, icon, label, count, warn, onClick }: {
  active: boolean; icon: string; label: string; count: number; warn?: boolean; onClick: () => void;
}) {
  return <button className={active ? "on" : ""} onClick={onClick}><Ti name={icon} size={15} />
    <span>{label}</span><b className={warn ? "warn" : ""}>{count}</b></button>;
}

function FilterSection({ label, children }: { label: string; children: ReactNode }) {
  return <div className="asset-rail-section"><span>{label}</span>{children}</div>;
}

function FilterButton({ active, label, count, dot, onClick }: {
  active: boolean; label: string; count: number; dot?: string; onClick: () => void;
}) {
  return <button className={active ? "on" : ""} onClick={onClick}>
    {dot && <span className={`sdot ${dot === "all" ? "neutral" : dot}`} />}<span>{label}</span><b>{count}</b></button>;
}

function AssetSection({ name, groups, selected, onSelect }: {
  name: string; groups: AssetGroup[]; selected: string | null; onSelect: (name: string) => void;
}) {
  return <div className="asset-section"><div className="asset-section-head">{name}<span className="n">{groups.length}</span></div>
    <div className="asset-grid">{groups.map((group) => {
      const status = groupStatus(group); const thumb = groupThumb(group);
      const candidates = group.candidates?.length || 0; const revisions = group.revisions?.length || 0;
      return <button key={group.logical_name} type="button" className={`asset-tile ${status}${selected === group.logical_name ? " sel" : ""}`}
        onClick={() => onSelect(group.logical_name)}>
        {candidates > 0 && <span className="asset-badge">{candidates} new</span>}
        <div className="asset-thumb">{thumb ? <img src={previewURL(thumb)} alt="" loading="lazy" />
          : <span className="none">{group.revisions?.[0]?.kind || "asset"}</span>}</div>
        <div className="asset-cap"><div className="asset-name" title={group.logical_name}>{group.logical_name}</div>
          <div className="asset-sub"><span className={`sdot ${status}`} />{status === "review" ? "needs review" : status}
            <span>{revisions} rev{revisions === 1 ? "" : "s"}</span></div></div>
      </button>;
    })}</div>
  </div>;
}
