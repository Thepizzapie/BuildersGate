import { useMemo, useState } from "react";
import {
  Accordion, Badge, Button, Group, SegmentedControl, Table, Text, TextInput,
} from "@mantine/core";
import { useAppState, type AssetGroup } from "../../store";
import {
  askText, mutate, pollQueue, pollState, previewURL, seatColor, toast, vaultChip,
} from "../../bridge";
import { assetCategory, catOrder, groupStatus, groupThumb } from "./categorise";
import { AssetDrawer } from "./AssetDrawer";

/* The Assets deck.
 *
 * Three surfaces, and only two of them are React:
 *
 *   · the LIBRARY (#asset-lib-root) is still assetlib.js — a self-contained
 *     module with its own scan endpoint, its own injected style and its own
 *     view state. React owns the existence of that node and NOTHING inside it;
 *     the element is rendered with no children and never re-keyed, so the
 *     module's innerHTML survives every render here. Same contract as Icon.tsx.
 *   · the REVIEW QUEUE and the VAULT table are this file. They were ~250 lines
 *     of template strings in index.html with three hand-written signature
 *     caches to stop a 3s poll from destroying half-decoded <img>s, snapping
 *     open <details> folds shut, and losing text selections mid-drag. All three
 *     caches are gone: that is what a reconciler is for.
 *   · the GALLERY (#gallery) is still the shell's renderGallery.
 *
 * Data arrives on the shell's existing /api/state poll (see store.ts) — this
 * view does not fetch it a second time.
 */

type Filter = "all" | "review" | "approved" | "rejected";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "review", label: "To review" },
  { id: "approved", label: "Approved" },
  { id: "rejected", label: "Rejected" },
];

/* What the vault thinks of one tracked file. A seat's lock outranks drift:
   knowing WHO is holding it is what tells you whether the change is expected. */
function VaultState({ a, modified, missing, pending }: {
  a: { path: string; lock_seat?: string | null };
  modified: Set<string>; missing: Set<string>; pending: Set<string>;
}) {
  if (a.lock_seat) {
    // The seat's own colour, which is a project token and not a Mantine one.
    const c = seatColor(a.lock_seat);
    return (
      <Badge variant="filled" style={{ background: c, color: "var(--accent-fg)" }}>
        {a.lock_seat}
      </Badge>
    );
  }
  const drift = modified.has(a.path) ? "drift"
    : missing.has(a.path) ? "missing"
    : pending.has(a.path) ? "pending" : null;
  return drift
    ? <Badge variant="light" color="red">{drift}</Badge>
    : <Badge variant="default">clean</Badge>;
}

export default function Assets() {
  const { asset_groups: groups, assets, verify, canon } = useAppState();
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [drawer, setDrawer] = useState<string | null>(null);
  const [auditing, setAuditing] = useState(false);

  const counts = useMemo(() => {
    const c = { all: groups.length, approved: 0, review: 0, rejected: 0 };
    groups.forEach((g) => { c[groupStatus(g)]++; });
    return c;
  }, [groups]);

  const buckets = useMemo(() => {
    const q = search.toLowerCase().trim();
    let out = groups;
    if (filter !== "all") out = out.filter((g) => groupStatus(g) === filter);
    if (q) out = out.filter((g) => g.logical_name.toLowerCase().includes(q));
    const by: Record<string, AssetGroup[]> = {};
    out.forEach((g) => {
      (by[assetCategory(g.logical_name, canon)] ||= []).push(g);
    });
    return catOrder(Object.keys(by)).map((cat) => [cat, by[cat]] as const);
  }, [groups, filter, search, canon]);

  // The drawer follows the data: an asset that disappears from the poll closes
  // it rather than leaving a panel describing something that is gone.
  const open = drawer ? groups.find((g) => g.logical_name === drawer) || null : null;

  /* Both of these used to collapse cancel into "" with `|| ""`, so Escape still
     submitted — a blank-reason rejection persisted as precedent, and a paid
     regeneration queued off a dialog the operator had backed out of. */
  async function review(id: number, status: string) {
    let note = "";
    if (status === "rejected") {
      const said = await askText({
        title: "Reject this revision",
        label: "Reason for rejection (becomes precedent)",
        placeholder: "what is off-model, and against which reference",
        ok: "reject",
      });
      if (said == null) return;
      note = said;
    }
    const r = await mutate(`/api/artifacts/${id}/review`, {
      body: { status, note }, ok: `revision ${id} → ${status}`,
    });
    if (r.ok) pollState();
  }

  async function regenerate(id: number) {
    const reason = await askText({
      title: "Regenerate this revision",
      body: "This queues a paid image generation.",
      label: "What should the next revision improve?",
      ok: "queue regeneration",
    });
    if (reason == null) return;
    const r = await mutate(`/api/artifacts/${id}/regenerate`, {
      body: { reason }, ok: "regeneration queued",
    });
    if (r.ok) { pollState(); pollQueue(); }
  }

  /* THE AUDIT REPORTED ITS OWN SUCCESS AS FAILURE, and did so on exactly the
     projects that need it. It went through mutate(), which treats `ok:false` in
     a payload as a refused request — but this endpoint's `ok` is the VAULT's
     verdict ("nothing has drifted"), not the request's. Any project with a
     single drifted file therefore got "audit failed" in the status bar, and
     pollState() was never reached, so the count never refreshed. Measured: 196
     drifted files, audit completed in under 120ms, button said "audit failed".
     So this talks to the endpoint directly and keeps the two meanings of "ok"
     apart: HTTP status is whether the audit RAN, the payload is what it FOUND. */
  async function audit() {
    setAuditing(true);
    vaultChip("hashing…");
    let payload: { counts?: Record<string, number> } | null = null;
    let failed = "";
    try {
      const r = await fetch("/api/assets/verify", { method: "POST" });
      if (!r.ok) failed = `the integrity audit could not run · ${r.status}`;
      else payload = await r.json();
    } catch {
      failed = "backend unreachable - is the dashboard still running?";
    }
    setAuditing(false);
    if (failed) { vaultChip("audit failed"); toast(failed); return; }
    const c = payload?.counts || {};
    const off = (c.modified || 0) + (c.missing || 0) + (c.pending || 0);
    toast(off
      ? `${off} tracked file(s) no longer match their record - ${c.modified || 0} `
        + `changed, ${c.missing || 0} missing, ${c.pending || 0} never hashed`
      : "integrity audit clean - every tracked file matches its record",
      off ? "" : "ok");
    pollState();
  }

  const vc = verify.counts || {};
  const drift = (vc.modified || 0) + (vc.missing || 0) + (vc.pending || 0);
  const auditTitle = drift
    ? `${drift} tracked file(s) no longer match the hash on record (${vc.modified || 0} `
      + `changed, ${vc.missing || 0} missing, ${vc.pending || 0} never hashed). This `
      + "re-hashes every tracked binary and reports what it finds - it reads and "
      + "reports, it does not touch your files."
    : "Every tracked file matches its recorded hash. Re-hashes them all and "
      + "reports - it reads and reports, it does not touch your files.";

  const modified = new Set((verify.modified || []).map((m) => m.path));
  const missing = new Set(verify.missing || []);
  const pending = new Set(verify.untracked_hash || []);

  return (
    <>
      {/* WHAT WILL THIS BUTTON DO. "run integrity audit" carried no title, no
          description and no relation to the "vault N to reconcile" badge in the
          status bar that it is the only control for — the owner's words were
          "no idea what these do". A title attribute is not the fix on its own:
          hover is a thing you do to a control you have already decided to risk
          pressing. So the actions are split by what they touch (open an editor
          vs go and do work), the work ones say their effect in the sentence
          below them, and the audit is named after the number it clears and
          carries that number on its face. */}
      <div className="view-heading">
        <div><span className="eyebrow">Library</span><h2>Assets</h2></div>
        <div className="act-cluster">
          <Group gap="xs" wrap="nowrap">
            <span className="act-lab">open</span>
            <Button variant="default" size="xs" onClick={() => window.SpriteEdit?.pick()}
                    title="Pick a sprite sheet and open it in the pixel and rig editor">
              sprite editor
            </Button>
            <Button variant="default" size="xs" onClick={() => window.AudioLab?.pick()}
                    title="Pick a sound or track and open it in the audio lab">
              audio lab
            </Button>
            <Button variant="default" size="xs" onClick={() => window.ModelEdit?.pick()}
                    title="Pick a 3D model and open it in the viewer">
              3D viewer
            </Button>
          </Group>
          <Group gap="xs" wrap="nowrap" mt="xs">
            <span className="act-lab">check</span>
            {/* rescan LIVES HERE, not in the filter rail below. It walks the
                whole project off disk, and as a pill among fifteen family
                filters it was the same shape and colour as `tiles` — nothing
                said it would go and do something. */}
            <Button variant="default" size="xs" onClick={() => window.AssetLib?.refresh()}
                    title="Walk the project off disk again and rebuild the library below. Reads only.">
              rescan library
            </Button>
            {/* The count rides on the button's face: it is named after the badge
                in the status bar, and a button named after a number that does
                not carry it is still a button nobody connects to the badge. */}
            <Button variant="default" size="xs" onClick={audit} loading={auditing}
                    title={auditTitle}
                    rightSection={drift > 0
                      ? <Badge size="sm" variant="filled">{drift}</Badge>
                      : undefined}>
              reconcile vault
            </Button>
          </Group>
          <Text component="p" size="xs" c="dimmed" className="act-note">
            <b>rescan library</b> re-reads the project from disk.{" "}
            <b>reconcile vault</b> re-hashes tracked files and reports what drifted -
            it is what clears the vault count in the status bar. Neither one writes
            to your game.
          </Text>
        </div>
      </div>

      {/* assetlib.js writes here, and REACT RENDERS IT EMPTY ON PURPOSE. A
          placeholder child in this JSX is a child React believes it owns: every
          poll reconciles it, and the moment anything about it changes React
          restores its own copy over the library the module had just drawn. The
          module prints its own "reading the library…" anyway. */}
      <div id="asset-lib-root" />

      {/* AN ACCORDION, NOT TWO <details>. Both panels are long, both are
          optional, and a <details> pair gives no indication that they are the
          same kind of thing — the chevron, the transition and the keyboard
          handling all had to be taken on faith from app.css. multiple=true
          keeps the old behaviour that either can be open on its own. */}
      <Accordion multiple variant="separated" mt="md" chevronPosition="left">
        <Accordion.Item value="review">
          <Accordion.Control>
            <Group gap="sm">
              Review queue · generated candidates by logical name
              {counts.review > 0 && <Badge variant="light" size="sm">{counts.review} to review</Badge>}
            </Group>
          </Accordion.Control>
          <Accordion.Panel>
        <div className="asset-toolbar">
          <TextInput placeholder="Search assets…" value={search}
                     onChange={(e) => setSearch(e.currentTarget.value)}
                     aria-label="Search assets" style={{ flex: "1 1 240px" }} />
          {/* A SEGMENTED CONTROL, NOT FOUR BUTTONS. These four are one exclusive
              choice and were four independent <button>s carrying an .active
              class — nothing in the markup said only one could be on, so no
              screen reader could say it either. Mantine's is a radiogroup with
              roving focus and arrow-key movement for free. */}
          <SegmentedControl value={filter} onChange={(v) => setFilter(v as Filter)}
                            data={FILTERS.map((f) => ({
                              value: f.id,
                              label: (
                                <Group gap={6} wrap="nowrap" justify="center">
                                  {f.label}
                                  <Badge size="xs" variant="light">{counts[f.id]}</Badge>
                                </Group>
                              ),
                            }))} />
        </div>
        <div className="asset-scroll">
          {!buckets.length && <div className="empty">no assets match this filter</div>}
          {buckets.map(([cat, list]) => (
            <div key={cat} className="asset-section">
              <div className="asset-section-head">{cat} <span className="n">{list.length}</span></div>
              <div className="asset-grid">
                {list.map((g) => {
                  const st = groupStatus(g);
                  const thumb = groupThumb(g);
                  const nCand = g.candidates?.length || 0;
                  const nRev = g.revisions?.length || 0;
                  return (
                    <div key={g.logical_name}
                         className={`asset-tile ${st}${drawer === g.logical_name ? " sel" : ""}`}
                         onClick={() => setDrawer(g.logical_name)}>
                      {nCand > 0 && <span className="asset-badge">{nCand} new</span>}
                      <div className="asset-thumb">
                        {thumb
                          ? <img src={previewURL(thumb)} alt={g.logical_name} loading="lazy" />
                          : <span className="none">{g.revisions?.[0]?.kind || "asset"}</span>}
                      </div>
                      <div className="asset-cap">
                        <div className="asset-name" title={g.logical_name}>{g.logical_name}</div>
                        <div className="asset-sub">
                          <span className={`sdot ${st}`} />
                          {st === "review" ? "needs review" : st} · {nRev} rev{nRev === 1 ? "" : "s"}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
          </Accordion.Panel>
        </Accordion.Item>

        <Accordion.Item value="vault">
          <Accordion.Control>
            <Group gap="sm">
              Tracked binaries · integrity
              <Badge variant="default" size="sm">{assets.length}</Badge>
              {drift > 0 && <Badge variant="light" color="red" size="sm">{drift} drifted</Badge>}
            </Group>
          </Accordion.Control>
          <Accordion.Panel>
        <div className="vault-scroll">
          {/* A REAL <thead>. This was a <tr> of <th> inside the table body, so
              the header scrolled away with the rows and no assistive technology
              could associate a cell with its column. */}
          <Table stickyHeader>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>path</Table.Th><Table.Th>kind</Table.Th>
                <Table.Th>size</Table.Th><Table.Th>state</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {assets.map((a) => (
                <Table.Tr key={a.path}>
                  <Table.Td className="path" title={a.path}>{a.path}</Table.Td>
                  <Table.Td>{a.kind}</Table.Td>
                  <Table.Td>{a.bytes ? (a.bytes / 1024).toFixed(1) + "k" : "-"}</Table.Td>
                  <Table.Td><VaultState a={a} modified={modified} missing={missing} pending={pending} /></Table.Td>
                </Table.Tr>
              ))}
              {!assets.length && (
                <Table.Tr><Table.Td colSpan={4} style={{ color: "var(--text-3)" }}>
                  nothing tracked yet - asset_track / asset_lock
                </Table.Td></Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </div>
          </Accordion.Panel>
        </Accordion.Item>
      </Accordion>

      {/* The shell's renderGallery writes here. */}
      <div className="gallery" id="gallery" hidden />

      <AssetDrawer group={open} onClose={() => setDrawer(null)}
                   onReview={review} onRegenerate={regenerate}
                   onOpenSprite={(rel) => window.SpriteEdit?.open(rel)}
                   onOpenModel={(rel) => window.ModelEdit?.open(rel)} />
    </>
  );
}
