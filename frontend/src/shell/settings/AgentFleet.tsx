import { useCallback, useState } from "react";
import { Badge, Button, Group, Text } from "@mantine/core";
import { Ti } from "../Ti";
import { mutate, readJSON } from "../../bridge";
import { usePoll } from "../../hooks";

/* THE MASTER AGENT VIEW - every running agent on the machine, not just this
 * project's.
 *
 * The rest of the app asks /api/agents, which reads dispatch._live: a dict of
 * process handles that dies with the dashboard, scoped to the one project you
 * have open. So an agent survived a dashboard restart, or belonged to a second
 * dashboard, or was working the game you switched away from - and appeared
 * nowhere, while still editing files and still billing. /api/agents/all reads
 * the on-disk registry instead, which outlives the process that wrote it.
 *
 * IT LIVES IN SETTINGS AND NOT ON THE BOARD, deliberately. The board is one
 * project's work; this is the machine, and the thing it offers is a kill. The
 * API behind it is human-only for the same reason no MCP tool exposes it: an
 * agent that can stop agents can stop the QA agent that is checking it.
 *
 * A ROW SAYS WHOSE IT IS BEFORE IT SAYS ANYTHING ELSE. The whole failure this
 * screen exists for is an agent running against a project you are not looking
 * at, so the grouping is the content.
 */

export type Agent = {
  pid: number; item_id: number; seat: string; root: string; runner: string;
  log: string; started_at: number; seconds: number;
  item_title: string; item_status: string;
};
export type FleetProject = {
  root: string; name: string; active: boolean; agents: Agent[];
};
export type Fleet = { projects: FleetProject[]; total: number; active_root: string };

export const EMPTY_FLEET: Fleet = { projects: [], total: 0, active_root: "" };

/** Read the fleet. Exported because the project switcher needs the same answer
 *  before it reloads the page out from under a running agent. */
export async function readFleet(): Promise<Fleet> {
  return readJSON<Fleet>("/api/agents/all", EMPTY_FLEET);
}

/** 2h 04m, 6m 12s, 41s. A runtime is read to judge whether something is stuck,
 *  so the leading unit is the only one that carries information. */
export function runtime(seconds: number): string {
  if (!seconds) return "-";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

export function AgentFleet({ active }: { active: boolean }) {
  const [fleet, setFleet] = useState<Fleet>(EMPTY_FLEET);
  const [busy, setBusy] = useState("");

  const refresh = useCallback(async () => { setFleet(await readFleet()); }, []);
  /* Five seconds. This is a list of things that are happening right now and the
     reason to open it is usually that something should not be. */
  usePoll(refresh, 5000, active);

  /* Every stop re-reads rather than removing the row optimistically. A stop can
     land on a run that had already finished, and a row that vanishes on click
     tells the operator the kill worked when nothing was killed. */
  async function stop(a: Agent) {
    setBusy(`${a.root}#${a.item_id}`);
    await mutate("/api/agents/stop", { body: { item_id: a.item_id, root: a.root } });
    setBusy("");
    refresh();
  }

  async function stopAll(root: string) {
    setBusy(root || "*");
    await mutate("/api/agents/stop-all", { body: root ? { root } : {} });
    setBusy("");
    refresh();
  }

  return (
    <section className="bg4-settings-group bg4-fleet">
      <div className="bg4-settings-head">
        <Ti name="robot" size={13} />
        <span>Running agents</span>
        <span className="n">{fleet.total}</span>
        <span style={{ flex: 1 }} />
        {fleet.total > 0 && (
          <Button size="compact-xs" variant="light" color="red"
                  loading={busy === "*"}
                  onClick={() => stopAll("")}>
            Stop everything
          </Button>
        )}
      </div>

      <p className="bg4-fleet-note">
        {/* SAY WHAT IT CAN ACTUALLY SEE. This read "every agent running on this
            machine", which is a promise the registry cannot keep: it lists what
            Builders Gate DISPATCHED and recorded. A Claude Code session you
            started yourself in a terminal was never recorded, so it is not here
            and never will be - and someone looking for the session they are
            talking to reasonably concludes the panel is broken. */}
        Every agent Builders Gate dispatched, across every project, from the
        on-disk registry - including ones this dashboard did not start and ones
        working a project you do not have open. A session you started yourself in
        a terminal is not dispatched work and does not appear here. Stopping
        kills the whole process tree (a runner starts children of its own) and
        banks the work item as failed, so the board never claims work is in
        flight that nothing is doing.
      </p>

      {!fleet.projects.length && (
        <Text size="xs" c="dimmed" py="lg">nothing is running</Text>
      )}

      {fleet.projects.map((p) => (
        <div key={p.root} className={p.active ? "bg4-fleet-proj on" : "bg4-fleet-proj"}>
          <div className="ph">
            <Ti name="folder" size={13} />
            <span className="nm">{p.name}</span>
            {p.active && <Badge size="xs" variant="light">open</Badge>}
            <span className="pa">{p.root}</span>
            <span className="n">{p.agents.length}</span>
            <Button size="compact-xs" variant="subtle" color="red"
                    loading={busy === p.root}
                    onClick={() => stopAll(p.root)}>
              Stop all here
            </Button>
          </div>

          {p.agents.map((a) => (
            <div key={`${a.root}#${a.pid}`} className="bg4-fleet-row">
              <Badge size="xs" variant="light"
                     style={{ background: `var(--c-${a.seat}, var(--solid-2))` }}>
                {a.seat || "-"}
              </Badge>
              <span className="it">
                <b>#{a.item_id}</b> {a.item_title || "(no title on the board)"}
              </span>
              <Group gap={6} wrap="nowrap" className="meta">
                <span className="rt" title="how long this run has been going">
                  {runtime(a.seconds)}
                </span>
                <span className="pid">pid {a.pid}</span>
                {a.runner && a.runner !== "claude" && (
                  <Badge size="xs" variant="default">{a.runner}</Badge>
                )}
                {/* The board status, when it disagrees with reality. A live
                    process against an item that no longer reads 'dispatched'
                    means somebody completed it and the process did not exit - which is the shape of a wedged run. */}
                {a.item_status && a.item_status !== "dispatched" && (
                  <Badge size="xs" variant="light" color="yellow">
                    board says {a.item_status}
                  </Badge>
                )}
              </Group>
              <Button size="compact-xs" variant="default"
                      loading={busy === `${a.root}#${a.item_id}`}
                      onClick={() => stop(a)}>
                Stop
              </Button>
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}
