/* Reading prose that was typed as a design document.
 *
 * A lore body or a bible section is not a form field: ALL-CAPS-colon headings,
 * hard-wrapped paragraphs and "- " bullets. The panel reads first and renders
 * the structure the prose already has. Everything here returns DATA — the
 * component renders it as JSX, so nothing a human typed is ever markup. */

/** How heavy a body is, said out loud. A 6,509-char act and a 148-char note
 *  used to render identically; the collapsed row has to announce the weight or
 *  collapsing just hides it. */
export function measure(body: string): { short: string; full: string } {
  const chars = body.length;
  if (!chars) return { short: "empty", full: "nothing written yet" };
  const lines = body.split("\n").length;
  const n = chars >= 1000 ? (chars / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(chars);
  return { short: `${n} · ${lines} ln`, full: `${chars} characters · ${lines} lines` };
}

export const peek = (body: string) =>
  body.split("\n").map((l) => l.trim()).find(Boolean)
  || "empty - the seats read this, so say something";

export type Block =
  | { t: "h"; text: string }
  | { t: "p"; text: string }
  | { t: "li"; text: string }
  | { t: "f"; key: string; text: string };

const isLabel = (s: string) => s.length > 0 && s.length <= 48
  && /[A-Z]/.test(s) && s === s.toUpperCase();

export function proseBlocks(body: string): Block[] {
  const out: Block[] = [];
  let para: string | null = null;
  const flush = () => { if (para !== null) { out.push({ t: "p", text: para }); para = null; } };
  const last = () => out[out.length - 1];
  String(body || "").replace(/\r/g, "").split("\n").forEach((raw) => {
    const line = raw.trim();
    if (!line) { flush(); return; }
    const bullet = line.match(/^[-*•·]\s+(.+)$/);
    if (bullet) { flush(); out.push({ t: "li", text: bullet[1] }); return; }
    // an indented line under a bullet is that bullet's second line, not a paragraph
    const prev = last();
    if (/^\s/.test(raw) && para === null && prev && prev.t === "li") {
      prev.text += " " + line; return;
    }
    const colon = line.indexOf(":");
    if (colon > 0) {
      const key = line.slice(0, colon).trim(), rest = line.slice(colon + 1).trim();
      // "JOB / CLASS: Paladin" is a field row; "ABILITIES:" alone is a
      // heading; "DESCRIPTION: <300 characters>" is a heading with a
      // paragraph under it — a field row would squeeze prose into a column.
      if (isLabel(key)) {
        flush();
        if (!rest) out.push({ t: "h", text: key });
        else if (rest.length > 120) out.push({ t: "h", text: key }, { t: "p", text: rest });
        else out.push({ t: "f", key, text: rest });
        return;
      }
    }
    if (isLabel(line)) { flush(); out.push({ t: "h", text: line }); return; }
    // The source is hard-wrapped at ~78 columns, so consecutive lines are one
    // paragraph and get re-flowed to whatever measure the panel actually has.
    para = para === null ? line : `${para} ${line}`;
  });
  flush();
  return out;
}
