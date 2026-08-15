/* OPEN A TERMINAL ON THIS PROJECT.
 *
 * The per-run version of this lives in the inspector: pick an item, continue
 * its session. That is the right shape for CONTINUING, which is about one run
 * and has to name it - and the wrong shape for STARTING, which is about the
 * project. Requiring a run to exist first meant a console with nothing in it
 * yet, which is exactly when somebody wants a terminal, had no way to open one.
 *
 * SO IT SITS IN THE COMPOSER, beside dispatch and brainstorm, because those are
 * the other three things you can do with an empty console and this is the
 * fourth. It needs nothing selected and nothing to have happened.
 *
 * IT SPENDS NOTHING BY ITSELF. Opening a window costs nothing; the tokens start
 * when the person types into it, which is why this is a plain button rather
 * than something behind a confirmation.
 */
import { Button } from "@mantine/core";
import { useState } from "react";
import { mutate, toast } from "../../bridge";
import { Ti } from "../Ti";

export function OpenCli() {
  const [busy, setBusy] = useState(false);

  async function open() {
    setBusy(true);
    const res = await mutate<{ opened?: string }>("/api/session/open", {
      method: "POST", quiet: true,
    });
    setBusy(false);
    if (res?.ok) {
      toast(`opened ${res.data?.opened || "a terminal"}`, "ok");
    } else {
      /* THE SERVER'S OWN REASON. It refuses for things worth reading - no
         claude on PATH, a platform with no terminal it can drive - and each
         names its own fix. */
      toast(res?.error || "could not open a terminal");
    }
  }

  return (
    <Button variant="default" size="compact-xs" disabled={busy} onClick={open}
            leftSection={<Ti name="terminal-2" size={13} />}
            title="open a terminal on this project as a new Claude session">
      open in CLI
    </Button>
  );
}
