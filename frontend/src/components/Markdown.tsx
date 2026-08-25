import { Fragment, type ReactNode } from "react";

/* AGENT PROSE IS MARKDOWN, AND IT WAS RENDERED AS TYPEWRITER OUTPUT. The
 * director's replies and every seat's final report come out of a model that
 * writes GFM — headings, **bold**, tables, fenced code — and the chat pane,
 * the inspector and the floor notes all printed the raw characters. A status
 * report full of `**Content state**` and `| # | Seat |` pipes is unreadable
 * exactly where reading it is the whole point.
 *
 * Hand-rolled rather than a dependency on purpose: the app's entire dependency
 * surface is react + mantine, the input is model-written GFM (a narrow, well
 * behaved subset), and everything here builds React elements — no innerHTML,
 * so a report that contains <script> renders as the text "<script>".
 *
 * Links: http(s) only, opened in a new tab. Anything else ("javascript:",
 * "file:") renders as plain text, because these strings come from agents.
 */

const INLINE =
  /(\*\*([^*]+)\*\*|`([^`]+)`|\*([^*\s][^*]*)\*|_([^_\s][^_]*)_|\[([^\]]+)\]\(([^)\s]+)\))/;

function inline(text: string, key = 0): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  while (rest) {
    const m = INLINE.exec(rest);
    if (!m || m.index === undefined) { out.push(rest); break; }
    if (m.index > 0) out.push(rest.slice(0, m.index));
    const k = `i${key}-${out.length}`;
    if (m[2] !== undefined) out.push(<b key={k}>{inline(m[2], key + 1)}</b>);
    else if (m[3] !== undefined) out.push(<code key={k}>{m[3]}</code>);
    else if (m[4] !== undefined) out.push(<em key={k}>{inline(m[4], key + 1)}</em>);
    else if (m[5] !== undefined) out.push(<em key={k}>{inline(m[5], key + 1)}</em>);
    else if (m[6] !== undefined) {
      const href = m[7] || "";
      out.push(/^https?:\/\//.test(href)
        ? <a key={k} href={href} target="_blank" rel="noreferrer">{m[6]}</a>
        : `${m[6]} (${href})`);
    }
    rest = rest.slice(m.index + m[0].length);
  }
  return out;
}

/** A table row's cells. `a | b` splits on unescaped pipes; edge pipes drop. */
function cells(row: string): string[] {
  const trimmed = row.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((c) => c.trim());
}

const RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

export function Markdown({ text }: { text: string }) {
  const lines = (text || "").split("\n");
  const out: ReactNode[] = [];
  let para: string[] = [];
  let k = 0;

  const flush = () => {
    if (!para.length) return;
    out.push(<p key={k++}>{para.map((ln, i) => (
      <Fragment key={i}>{i > 0 && <br />}{inline(ln, i)}</Fragment>
    ))}</p>);
    para = [];
  };

  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];

    // fenced code — verbatim until the closing fence (or the end)
    const fence = /^\s*```(\w*)/.exec(ln);
    if (fence) {
      flush();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) body.push(lines[i++]);
      out.push(<pre key={k++}><code>{body.join("\n")}</code></pre>);
      continue;
    }

    // table — a header row whose NEXT line is the |---|---| rule
    if (ln.includes("|") && i + 1 < lines.length && RULE.test(lines[i + 1])) {
      flush();
      const head = cells(ln);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|")
             && lines[i].trim() !== "") { rows.push(cells(lines[i])); i += 1; }
      i -= 1;
      out.push(
        <div className="tblwrap" key={k++}>
          <table>
            <thead><tr>{head.map((c, j) =>
              <th key={j}>{inline(c, j)}</th>)}</tr></thead>
            <tbody>{rows.map((r, ri) => (
              <tr key={ri}>{head.map((_, j) =>
                <td key={j}>{inline(r[j] ?? "", j)}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>);
      continue;
    }

    const h = /^(#{1,6})\s+(.*)$/.exec(ln);
    if (h) {
      flush();
      // Every heading level renders as one visual rank: a chat bubble has no
      // room for six sizes, and a model's # vs ### choice carries no meaning
      // a reader would miss.
      out.push(<div className="mdh" key={k++}>{inline(h[2])}</div>);
      continue;
    }

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(ln)) { flush(); out.push(<hr key={k++} />); continue; }

    const li = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(ln);
    if (li) {
      flush();
      const ordered = /\d/.test(li[2]);
      const items: ReactNode[] = [];
      while (i < lines.length) {
        const m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
        if (!m) break;
        items.push(<li key={items.length}>{inline(m[3], items.length)}</li>);
        i += 1;
      }
      i -= 1;
      out.push(ordered ? <ol key={k++}>{items}</ol> : <ul key={k++}>{items}</ul>);
      continue;
    }

    if (/^\s*>\s?/.test(ln)) {
      flush();
      const body: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      i -= 1;
      out.push(<blockquote key={k++}><Markdown text={body.join("\n")} /></blockquote>);
      continue;
    }

    if (ln.trim() === "") { flush(); continue; }
    para.push(ln);
  }
  flush();
  return <div className="bg-md">{out}</div>;
}
