/* HAND THIS RUN BACK TO A TERMINAL.
 *
 * Every dispatched agent IS a Claude Code session, and Claude keeps that
 * session's transcript on disk - so a run this dashboard is drawing as cards
 * can be picked up in a terminal exactly where it left off, with its whole
 * context. That is what somebody who would rather work in the CLI wants, and
 * what "read the log" is a poor substitute for. The session id was in the
 * agent's log the whole time; nothing was reading it out.
 *
 * IT HANDS OVER A COMMAND, IT DOES NOT RUN ONE. `claude --resume` is
 * interactive and this dashboard is a server that may share no terminal with
 * anybody, so a button that spawned it would start something the user cannot
 * see. Resuming also spends tokens on their account, which is their call to
 * make deliberately rather than by clicking something in a side panel.
 *
 * IT ASKS WHETHER THE SESSION IS STILL THERE. Claude's transcripts are cleaned
 * up eventually; offering a resume for one that is gone is a command that fails
 * in the user's terminal quoting an id they have never seen. The server checks
 * and says why not, and this draws the reason instead of the button.
 *
 * ONE READ, ON DEMAND. It is behind a disclosure rather than fetched with the
 * panel because most people opening an item want to read its steps, not move to
 * a terminal - and this is a filesystem probe per item.
 */
import { useEffect, useState } from "react";
import { readJSON, toast } from "../bridge";
import { Ti } from "./Ti";

type Session = {
  session_id?: string;
  resumable?: boolean;
  reason?: string;
  command?: string;
  cwd?: string;
  running?: boolean;
};

export function ResumeInCli({ itemId }: { itemId: number }) {
  const [info, setInfo] = useState<Session | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  /* FORGET THE LAST ITEM WHEN THE SELECTION MOVES.
   *
   * This panel is one component instance that stays mounted while the reader
   * clicks from item to item, so its cached answer outlived the item it was
   * about: selecting a run with no session and then one with a session showed
   * the FIRST item's "cannot be resumed", which is a wrong answer stated
   * confidently. Observed exactly that way - item 4 had never run, item 2 had,
   * and item 2 was reported unresumable.
   *
   * Cleared here rather than by keying the element at the usage site, so the
   * component is correct wherever it is mounted rather than only where somebody
   * remembered the key. */
  useEffect(() => {
    setInfo(null);
    setOpen(false);
    setBusy(false);
  }, [itemId]);

  async function reveal() {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (info) return;
    setBusy(true);
    setInfo(await readJSON<Session>(`/api/agents/${itemId}/session`, {}));
    setBusy(false);
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast("command copied", "ok");
    } catch {
      /* A denied clipboard is not a failure worth a red box: the command is on
         screen and can be selected by hand. */
      toast("select the command to copy it");
    }
  }

  return (
    <div className="bg4-cli">
      <button className="bg4-cli-toggle" onClick={reveal}
              aria-expanded={open}
              title="pick this run up in a terminal, with its context">
        <Ti name="terminal-2" size={13} />
        <span>Resume in CLI</span>
        <Ti name={open ? "chevron-up" : "chevron-down"} size={12} />
      </button>

      {open && (
        <div className="bg4-cli-body">
          {busy && <span className="bg4-cli-note">looking for the session…</span>}

          {!busy && info && !info.resumable && (
            <span className="bg4-cli-note">
              {info.reason || "this run cannot be resumed"}
            </span>
          )}

          {!busy && info?.resumable && (
            <>
              {/* THE RUNNING CASE IS CALLED OUT rather than refused. Resuming a
                  session while its agent is still working means two clients on
                  one transcript, which is confusing rather than dangerous - and
                  a reader who knows that may still want to look. */}
              {info.running && (
                <span className="bg4-cli-warn">
                  <Ti name="alert-triangle" size={12} />
                  this agent is still running - resuming now puts you in a
                  session it is still writing to
                </span>
              )}
              <code className="bg4-cli-cmd" onClick={() => copy(info.command || "")}
                    title="click to copy">
                {info.command}
              </code>
              <span className="bg4-cli-note">
                run it from <b>{info.cwd}</b> - Claude scopes sessions to the
                directory they started in.
              </span>
            </>
          )}
        </div>
      )}
    </div>
  );
}
