/* The page's own confirm dialog (frontend/public/ask.js), from React.
 *
 * `danger: true` dresses the confirm in --bad and drops the Enter binding — the
 * keystroke that dismissed the last dialog must not also approve a write with
 * no undo endpoint behind it. Falls back to window.confirm outside the shell,
 * and to "no" where not even that exists: no dialog is a cancel, never a yes. */

export type ConfirmOptions = {
  title?: string; body?: string; ok?: string; cancel?: string; danger?: boolean;
};

declare global {
  interface Window {
    askConfirm?(opts: ConfirmOptions): Promise<boolean>;
  }
}

export async function askConfirm(opts: ConfirmOptions): Promise<boolean> {
  if (typeof window.askConfirm === "function") return window.askConfirm(opts);
  if (typeof window.confirm === "function") {
    return window.confirm([opts.title, opts.body].filter(Boolean).join("\n\n"));
  }
  return false;
}

/** Copy to the clipboard, saying so either way. */
export async function copyText(text: string, said: (m: string, k?: string) => void,
                               done = "copied"): Promise<void> {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    said(done, "ok");
  } catch {
    said("could not reach the clipboard - select it by hand");
  }
}
