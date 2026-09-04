import { useEffect, useState } from "react";
import { Button, Drawer, Tabs } from "@mantine/core";
import type { AssetGroup } from "../../store";
import { RevisionPane } from "./RevisionPane";
import { groupStatus } from "./categorise";

/* The detail drawer: every revision of one logical asset, approved beside
 * selected, with the review actions on the candidate.
 *
 * MANTINE OWNS THE PANEL, not this file. What it replaced was a pair of
 * elements toggled by hand: `hidden` off, then an `.open` class on the next
 * animation frame (a class applied in the same frame has nothing to animate
 * from), then on the way out `.open` off and a 240ms setTimeout before `hidden`
 * went back on, because React unmounts instantly and the CSS transition needs
 * the node to still exist. It also had NO KEYBOARD EXIT — the only ways out
 * were the × and the backdrop.
 *
 * Drawer brings the transition, the overlay, Escape, the focus trap and the
 * scroll lock, and none of that is code this project has to keep correct. The
 * contents below are unchanged and still wear app.css's classes.
 */

const SPRITE_RE = /\.(png|webp)$/i;
const MODEL_RE = /\.(glb|gltf|obj)$/i;

function Fold({ label, text }: { label: string; text?: string | null }) {
  if (!text) return null;
  return (
    <details className="drawer-fold">
      <summary>{label}</summary>
      <div className="fold-body">{text}</div>
    </details>
  );
}

export function AssetDrawer({ group, onClose, onReview, onRegenerate, onOpenSprite, onOpenModel, inline = false }: {
  group: AssetGroup | null;
  onClose: () => void;
  onReview: (id: number, status: string) => void;
  onRegenerate: (id: number) => void;
  onOpenSprite: (rel: string) => void;
  onOpenModel: (rel: string) => void;
  inline?: boolean;
}) {
  /* Which revision the operator clicked. Dropped when the drawer changes asset:
     a stale id falls through to the default pick and reads as a dead click. */
  const [picked, setPicked] = useState<number | null>(null);
  useEffect(() => { setPicked(null); }, [group?.logical_name]);

  /* Mantine unmounts the panel on close, so the contents have to outlive the
     `group` that produced them for the length of the exit transition. */
  const [shown, setShown] = useState<AssetGroup | null>(group);
  useEffect(() => { if (group) setShown(group); }, [group]);

  const g = group || shown;
  if (!g) return null;

  const st = groupStatus(g);
  const selected = g.revisions.find((r) => r.id === picked)
    || g.candidates?.[0]
    || g.revisions.find((r) => r.id !== g.approved?.id)
    || g.revisions[g.revisions.length - 1]
    || null;

  const contents = <>
      <div className="drawer-head">
        <div>
          <h3>{g.logical_name}</h3>
          <div className="st">
            <span className={`sdot ${st}`} />
            {st === "review" ? "needs review" : st} · {g.revisions.length}{" "}
            revision{g.revisions.length === 1 ? "" : "s"}
          </div>
        </div>
        <button className="drawer-x" onClick={onClose} title="Close">×</button>
      </div>
      <div className="drawer-body">
        <div className="drawer-sec">
          {/* Real tabs: the revision strip was a row of buttons with an .active
              class, which announced nothing about being one choice among many. */}
          <Tabs value={String(selected?.id ?? "")}
                onChange={(v) => setPicked(v ? Number(v) : null)}
                classNames={{ list: "revision-tabs" }}>
            <Tabs.List>
              {g.revisions.map((r) => (
                <Tabs.Tab key={r.id} value={String(r.id)} className="revision-tab">
                  r{r.revision} · {r.status}
                </Tabs.Tab>
              ))}
            </Tabs.List>
          </Tabs>
          <div className="asset-compare">
            <RevisionPane a={g.approved || null} label="approved"
                          onReview={onReview} onRegenerate={onRegenerate} />
            <RevisionPane a={selected} label="selected"
                          candidate={selected?.status === "candidate"}
                          onReview={onReview} onRegenerate={onRegenerate} />
          </div>
          <div className="revision-meta" style={{ marginTop: 11 }}>
            <div title={selected?.path || ""}>{selected?.path || ""}</div>
          </div>
          {SPRITE_RE.test(selected?.path || "") && (
            <Button variant="default" size="compact-sm" mt={9}
                    onClick={() => onOpenSprite(selected!.path)}>
              open in sprite editor
            </Button>
          )}
          {MODEL_RE.test(selected?.path || "") && (
            <Button variant="default" size="compact-sm" mt={9}
                    onClick={() => onOpenModel(selected!.path)}>
              open in 3D viewer
            </Button>
          )}
          <Fold label="Generation prompt" text={selected?.prompt} />
          <Fold label="Work note" text={selected?.work_item?.result} />
          <Fold label="Review decision" text={selected?.review_note} />
        </div>
        <div className="asset-feedback">
          <strong>
            {g.feedback.length} linked playtest note{g.feedback.length === 1 ? "" : "s"}
          </strong>
          {g.feedback.length ? g.feedback.slice(0, 6).map((f, i) => (
            <div key={i} className="asset-feedback-item">
              session {f.session_id} · {f.kind} · {f.text} · {f.status}
            </div>
          )) : (
            <div className="asset-feedback-item">
              No playtest feedback linked to this asset.
            </div>
          )}
        </div>
      </div>
    </>;

  if (inline) return (
    <aside className="asset-inspector" aria-label={`${g.logical_name} revisions`}>
      {contents}
    </aside>
  );

  return (
    <Drawer opened={!!group} onClose={onClose} position="right" size="min(760px, 92vw)"
            withCloseButton={false} padding={0} classNames={{ content: "asset-drawer-panel" }}
            overlayProps={{ backgroundOpacity: 0.55, blur: 2 }}
            aria-label={`${g.logical_name} revisions`}>
      {contents}
    </Drawer>
  );
}
