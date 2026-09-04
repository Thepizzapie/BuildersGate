import { Modal, Progress } from "@mantine/core";
import { useAppState } from "../store";
import { Ti } from "./Ti";

export function Readiness({ opened, onClose, onScreen }: {
  opened: boolean; onClose(): void; onScreen(id: string): void;
}) {
  const state = useAppState();
  const drift = (state.verify.counts?.modified || 0) + (state.verify.counts?.missing || 0) + (state.verify.counts?.pending || 0);
  const review = state.asset_groups.reduce((n, g) => n + (g.candidates?.length || 0), 0);
  const checks = [
    { ok: !!state.project, label: "Project connected", detail: state.project?.name || "Open or create a project", screen: "settings" },
    { ok: state.asset_groups.length > 0, label: "Assets indexed", detail: state.asset_groups.length ? `${state.asset_groups.length} asset families` : "Rescan the asset library", screen: "assets" },
    { ok: drift === 0, label: "Tracked files clean", detail: drift ? `${drift} files need reconciliation` : "No drift detected", screen: "assets" },
    { ok: review === 0, label: "Reviews cleared", detail: review ? `${review} candidates await review` : "No candidates waiting", screen: "assets" },
    { ok: !!state.controls?.length, label: "Controls exposed", detail: state.controls?.length ? `${state.controls.length} inputs available to playtest` : "Add the game input map", screen: "playtests" },
    { ok: state.sessions.length > 0, label: "Playtest recorded", detail: state.sessions.length ? `${state.sessions.length} sessions captured` : "Run and record the first playtest", screen: "playtests" },
  ];
  const done = checks.filter((c) => c.ok).length;
  return <Modal opened={opened} onClose={onClose} title="Project readiness" centered size="md"
                classNames={{ content: "bg4-command", header: "bg4-command-head" }}>
    {!state.hydrated ? <div className="bg4-state loading"><Ti name="loader-2" size={18} /><b>Checking the project</b><span>Reading assets, controls, playtests, and tracked files.</span></div> : <>
    <div className="bg4-ready-score"><strong>{done}/{checks.length}</strong><span>production checks complete</span></div>
    <Progress value={(done / checks.length) * 100} mt="xs" mb="md" radius="xs" />
    <div className="bg4-ready-list">{checks.map((check) => <button key={check.label}
      className={check.ok ? "ok" : "todo"} onClick={() => { onScreen(check.screen); onClose(); }}>
      <Ti name={check.ok ? "circle-check-filled" : "circle-dashed"} size={17} />
      <span><b>{check.label}</b><small>{check.detail}</small></span><Ti name="chevron-right" size={14} />
    </button>)}</div></>}
  </Modal>;
}
