import type { Artifact } from "../../store";
import { previewURL } from "../../bridge";
import { IMG_RE } from "./categorise";

/* One side of the approved-vs-selected comparison. */

function HealthChip({ label, value }: { label: string; value: unknown }) {
  if (!value || (typeof value === "object" && !Object.keys(value as object).length))
    return <span className="chip">not checked · {label}</span>;
  const v = value as { ok?: boolean; consistent?: boolean; imported?: boolean };
  const explicit = v.ok ?? v.consistent ?? v.imported;
  // A check that ran but reports no verdict is not a pass and not a failure.
  if (explicit == null) return <span className="chip health-good">{label} · checked</span>;
  const ok = Boolean(explicit);
  return (
    <span className={ok ? "chip health-good" : "chip health-bad"}>
      {label} · {ok ? "pass" : "fail"}
    </span>
  );
}

function Preview({ a }: { a: Artifact | null }) {
  if (!a) {
    return <div className="revision-preview"><span className="empty">none approved yet</span></div>;
  }
  return IMG_RE.test(a.path) ? (
    <div className="revision-preview">
      <img src={previewURL(a.path)} alt={`${a.logical_name} revision ${a.revision}`} />
    </div>
  ) : (
    <div className="revision-preview">
      <span className="chip">{a.kind}</span>
      <span className="empty">{a.path}</span>
    </div>
  );
}

export function RevisionPane({ a, label, candidate = false, onReview, onRegenerate }: {
  a: Artifact | null;
  label: string;
  candidate?: boolean;
  onReview: (id: number, status: string) => void;
  onRegenerate: (id: number) => void;
}) {
  if (!a) {
    return (
      <div className="revision-pane">
        <div className="revision-label"><span>{label}</span></div>
        <Preview a={null} />
      </div>
    );
  }
  return (
    <div className={candidate ? "revision-pane candidate" : "revision-pane"}>
      <div className="revision-label">
        <span>{label} · r{a.revision}</span><span>{a.status}</span>
      </div>
      <Preview a={a} />
      <div className="revision-meta">
        <div><strong>{a.producer || "unknown producer"}</strong> · {a.model || a.kind}</div>
        <div>{a.profile || "no generation profile"} · refs {a.refs?.length || 0}</div>
        <div className="health-row">
          <HealthChip label="consistency" value={a.consistency} />
          <HealthChip label="engine import" value={a.engine_import} />
          {a.used_in_current_build
            ? <span className="chip health-good">in build</span>
            : <span className="chip">not in build</span>}
        </div>
        {a.lock?.seat && (
          <div>lock <strong>{a.lock.seat}</strong> · work {a.lock.work_item_id || "-"}</div>
        )}
        {a.work_item && <div>work #{a.work_item.id} · {a.work_item.status}</div>}
      </div>
      {candidate && (
        <div className="revision-actions">
          <button className="qbtn small" onClick={() => onReview(a.id, "approved")}>approve</button>
          <button className="qbtn small ghost" onClick={() => onReview(a.id, "rejected")}>reject</button>
          <button className="qbtn small ghost" onClick={() => onRegenerate(a.id)}>regenerate</button>
          <button className="qbtn small ghost" onClick={() => onReview(a.id, "superseded")}>supersede</button>
        </div>
      )}
    </div>
  );
}
