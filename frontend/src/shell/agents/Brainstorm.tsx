import { useState } from "react";
import { Button, Group, Modal, Code, ScrollArea, Text } from "@mantine/core";
import { Ti } from "../Ti";
import { bsDeploy, bsReset, bsSynthesize, type BsSession } from "./api";

/* The brainstorm footer: what this mode is, and the two buttons that end it.
 *
 * RESET IS THE BUG THIS FILE EXISTS FOR. In the classic console the footer was
 * built as an innerHTML string appended to a wrapper, and it only got appended
 * when a `#ck-bsfoot` lookup came back empty against a wrapper that had to
 * exist at that instant — so on the path where the wrapper rendered late, the
 * footer never went in and brainstorm mode had no way out but a reload. A
 * component cannot half-render: if this is on screen, both buttons are.
 *
 * DEPLOY IS TWO STEPS AND SHOWS ITS WORK. Synthesise, then a preview you have
 * to confirm. The endpoint takes the plan from the caller precisely so the
 * thing filed is the thing a human read — re-asking the model at confirm time
 * would file something nobody approved.
 */

export function BrainstormFoot({ session, onReset }: {
  session: BsSession | null;
  onReset: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<unknown>(null);

  if (!session) return null;
  const live = session.thinker?.live;

  async function reset() {
    if (!session) return;
    setBusy(true);
    const r = await bsReset(session.id);
    setBusy(false);
    if (r.ok) onReset();
  }

  async function preview() {
    if (!session) return;
    setBusy(true);
    const r = await bsSynthesize(session.id);
    setBusy(false);
    if (r.ok) setPlan(r.data?.plan ?? r.data);
  }

  async function file() {
    if (!session || plan == null) return;
    setBusy(true);
    const r = await bsDeploy(session.id, plan);
    setBusy(false);
    if (r.ok) { setPlan(null); onReset(); }
  }

  return (
    <>
      <Group gap="xs" wrap="nowrap" justify="space-between" className="bg4-bsfoot">
        <Text size="xs" c="dimmed" style={{ minWidth: 0 }}>
          <b style={{ color: "var(--good, #4ec98f)" }}>files nothing</b> · thinking only —
          no work item, no dispatch, until you press Deploy
        </Text>
        <Group gap="xs" wrap="nowrap">
          <Text size="xs" c="dimmed">{live ? "partner live" : "partner closed"}</Text>
          <Button variant="default" size="compact-sm" onClick={reset} loading={busy}
                  leftSection={<Ti name="rotate" size={13} />}
                  title="Stop the partner and clear this thread. Your notes and drawing are kept.">
            Reset
          </Button>
          <Button size="compact-sm" onClick={preview} loading={busy}
                  leftSection={<Ti name="send" size={13} />}>
            Deploy
          </Button>
        </Group>
      </Group>

      <Modal opened={plan != null} onClose={() => setPlan(null)} size="lg"
             title="File this plan?" centered>
        <Text size="sm" c="dimmed" mb="sm">
          This is what will be filed as work — exactly this, and nothing else.
        </Text>
        <ScrollArea.Autosize mah={420}>
          <Code block>{JSON.stringify(plan, null, 2)}</Code>
        </ScrollArea.Autosize>
        <Group justify="flex-end" mt="md">
          <Button variant="default" onClick={() => setPlan(null)}>cancel</Button>
          <Button onClick={file} loading={busy}>file it</Button>
        </Group>
      </Modal>
    </>
  );
}
