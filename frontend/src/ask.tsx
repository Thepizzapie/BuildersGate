import { useEffect, useRef, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { Button, NativeSelect, TextInput, Textarea } from "@mantine/core";
import { Themed } from "./theme";

/* ask.tsx — askText / askConfirm / askPick: the in-page replacements for
 * window.prompt and window.confirm.
 *
 * A native dialog draws through the browser chrome, not the page, so none of
 * the design system reaches it — and the desktop build (bgate_ui/desktop.py,
 * pywebview over Edge WebView2) registers no script-dialog handling at all,
 * where WebView2 commonly suppresses window.prompt outright. Every prompt() in
 * this app was therefore a DEAD BUTTON in the desktop shell. These three are
 * the replacements.
 *
 * THE CONTRACT, non-negotiable:
 *   askText  resolves the STRING on confirm and NULL on cancel — "" means
 *            confirmed-empty, null means the operator backed out. Call sites
 *            that collapse the two with `|| ""` submit a blank-reason
 *            rejection on Escape.
 *   askConfirm resolves true/false and never rejects.
 *   askPick  resolves the chosen item's value, or null.
 *
 * STILL ON WINDOW, ON PURPOSE. The classic decks (audiolab, spriteedit,
 * sceneview, wf, modeledit, agents_graph, dirtygate, seats/_core's bg.ask*)
 * reach these as window.askText / askConfirm / askPick at click time, exactly
 * as they reach toast(). main.tsx imports this module for that side effect;
 * React code imports the functions directly. The names go when the last deck
 * that calls them is ported.
 *
 * Styles live in app.css under @layer components (.ask-*): this is shell
 * chrome shared by the decks and the seats, and it sits beside .toasts.
 *
 * One question at a time. The shell has no z-order story for two of these,
 * and a second overlay would hide the one already being awaited. */

type Field = {
  name: string; type: "text" | "textarea" | "select"; label: string; value: string;
  placeholder: string; options: unknown[]; required: boolean; rows: number;
};

export type FieldSpec = Partial<Omit<Field, "name">> & { name?: string };

export type AskTextOptions = {
  title?: string; body?: string | string[]; label?: string; value?: string;
  placeholder?: string; ok?: string; cancel?: string; required?: boolean;
  multiline?: boolean; rows?: number;
  field?: { type?: Field["type"]; options?: unknown[] };
  /** Several fields in one form, keyed by name in the result. */
  fields?: FieldSpec[];
  /** Render INLINE inside this element instead of as a centred card. */
  host?: HTMLElement | null;
};

export type AskConfirmOptions = {
  title?: string; body?: string | string[]; ok?: string; cancel?: string;
  /** Dresses the confirm in --bad AND drops the Enter binding — the keystroke
   *  that dismissed the last dialog must not also approve a write that has no
   *  undo endpoint behind it. */
  danger?: boolean;
  host?: HTMLElement | null;
};

export type PickItem = {
  value: string | number; label?: string; meta?: string;
  tags?: { text: string; tone?: string }[];
};

export type AskPickOptions = {
  title?: string; items?: PickItem[];
  fetch?: (q: string) => Promise<{ items?: PickItem[]; total?: number; truncated?: boolean }>
    | { items?: PickItem[]; total?: number; truncated?: boolean };
  value?: string | number | null; placeholder?: string; empty?: string;
  actions?: { label: string; onClick(done: (v: unknown) => void): void }[];
};

let LIVE: { close(): void } | null = null;

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), '
  + 'select:not([disabled]), [tabindex="0"]';

function trapTab(e: KeyboardEvent | React.KeyboardEvent, card: HTMLElement) {
  const els = Array.from(card.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) =>
    el.getAttribute("tabindex") !== "-1" && el.offsetParent !== null);
  if (!els.length) return;
  const i = els.indexOf(document.activeElement as HTMLElement);
  const next = e.shiftKey ? (i <= 0 ? els.length - 1 : i - 1)
    : (i === -1 || i === els.length - 1 ? 0 : i + 1);
  e.preventDefault();
  els[next].focus();
}

const isBtn = (el: EventTarget | null) => !!(el && (el as HTMLElement).closest?.("button"));

/* ---- shared mount ---------------------------------------------------------
 * `host` present renders INLINE inside that element (previous instance
 * removed, scrolled into view); absent gives a centred card over --scrim.
 * The card component receives `done` and is responsible for focusing its own
 * first control. */
function mount<T>(cfg: { host?: HTMLElement | null; cancel: T; wide?: boolean; danger?: boolean },
                  render: (done: (v: T | unknown) => void) => ReactNode): Promise<T> {
  return new Promise<T>((resolve) => {
    let settled = false;
    let container: HTMLElement | null = null;
    let root: ReturnType<typeof createRoot> | null = null;
    let trigger: Element | null = null;
    try { trigger = document.activeElement; } catch { /* no document */ }

    const done = (v: unknown) => {
      if (settled) return;
      settled = true;
      if (LIVE && LIVE.close === closeSelf) LIVE = null;
      document.removeEventListener("keydown", onEsc, true);
      try { root?.unmount(); } catch { /* already gone */ }
      try { container?.remove(); } catch { /* already gone */ }
      // Focus goes back to whatever opened this; a keyboard operator must not
      // be dropped at the top of the document.
      try { (trigger as HTMLElement | null)?.focus?.(); } catch { /* detached */ }
      resolve(v as T);
    };
    const closeSelf = () => done(cfg.cancel);

    const onEsc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // A BGSelect popup owns Escape while it is open — closing the whole
      // question because someone dismissed a dropdown would lose their typing.
      if (document.querySelector(".bgs-pop")) return;
      e.preventDefault(); e.stopPropagation();
      done(cfg.cancel);
    };

    try {
      if (LIVE) LIVE.close();
      const host = cfg.host && typeof cfg.host.appendChild === "function" ? cfg.host : null;
      if (host) {
        host.querySelector(".ask-inline")?.remove();
        container = document.createElement("div");
        container.className = "ask ask-inline";
        host.appendChild(container);
      } else {
        container = document.createElement("div");
        container.className = "ask-scrim";
        container.addEventListener("mousedown", (e) => { if (e.target === container) done(cfg.cancel); });
        document.body.appendChild(container);
      }
      document.addEventListener("keydown", onEsc, true);
      LIVE = { close: closeSelf };

      root = createRoot(container);
      root.render(
        <Themed>
          {host
            ? render(done)
            : (
              <Card wide={!!cfg.wide} danger={!!cfg.danger}>{render(done)}</Card>
            )}
        </Themed>,
      );
      if (host) { try { container.scrollIntoView({ block: "nearest" }); } catch { /* fine */ } }
    } catch (e) {
      // A DOM failure must never hang a caller that is awaiting this.
      console.warn("[ask]", e);
      done(cfg.cancel);
    }
  });
}

/** The centred card. Every keystroke stays inside it: audiolab, spriteedit
 *  and sceneview bind single-key tool shortcuts on document, so without the
 *  stopPropagation typing a reason drives the editor underneath. */
function Card({ wide, danger, children }: { wide: boolean; danger: boolean; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <div ref={ref} role="dialog" aria-modal="true"
         className={`ask ${wide ? "ask-pickbox" : "ask-card"}${danger ? " ask-danger" : ""}`}
         onKeyDown={(e) => {
           e.stopPropagation();
           if (e.key === "Tab" && ref.current) trapTab(e, ref.current);
         }}>
      {children}
    </div>
  );
}

/** The inline variant keeps keystrokes to itself the same way. */
function Inline({ children }: { children: ReactNode }) {
  return <div onKeyDown={(e) => e.stopPropagation()}>{children}</div>;
}

/* A body is a string or an array of paragraphs. On a destructive confirm the
   explanation IS the safety, so nothing here shortens it. */
function Body({ body }: { body?: string | string[] }) {
  if (body == null || body === "") return null;
  const parts = (Array.isArray(body) ? body : String(body).split(/\n{2,}/))
    .filter((p) => String(p).trim());
  return (
    <>
      {parts.map((p, i) => (
        <p className="ask-p" key={i}>
          {String(p).split("\n").map((line, j, all) => (
            <span key={j}>{line}{j < all.length - 1 && <br />}</span>
          ))}
        </p>
      ))}
    </>
  );
}

/** The first control takes focus on the next tick, caret at the end. */
function useAutofocus<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  useEffect(() => {
    const t = setTimeout(() => {
      const f = ref.current as (T & { setSelectionRange?(a: number, b: number): void; value?: string }) | null;
      if (!f) return;
      f.focus();
      if (f.setSelectionRange && typeof f.value === "string") {
        try { f.setSelectionRange(f.value.length, f.value.length); } catch { /* not a text control */ }
      }
    }, 0);
    return () => clearTimeout(t);
  }, []);
  return ref;
}

/* ---- askText --------------------------------------------------------------
 * -> Promise<string|null>, or Promise<object|null> keyed by field name when
 *    `fields` is given: one form instead of a run of sequential questions. */
function normField(f: FieldSpec | undefined, name: string, defType: Field["type"]): Field {
  f = f || {};
  return {
    name, type: f.type || defType, label: f.label || "",
    value: f.value == null ? "" : String(f.value),
    placeholder: f.placeholder || "", options: f.options || [],
    required: !!f.required, rows: f.rows || 3,
  };
}

function TextForm({ opts, descs, multi, done }: {
  opts: AskTextOptions; descs: Field[]; multi: boolean; done: (v: unknown) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(
    () => Object.fromEntries(descs.map((d) => [d.name, d.value])));
  const [bad, setBad] = useState<string | null>(null);
  const first = useAutofocus<HTMLInputElement & HTMLTextAreaElement & HTMLSelectElement>();
  const hasTA = descs.some((d) => d.type === "textarea");

  const submit = () => {
    const miss = descs.find((d) => d.required && !String(values[d.name] || "").trim());
    if (miss) {
      setBad(miss.name);
      (document.getElementById("ask-f-" + miss.name) as HTMLElement | null)?.focus();
      return;
    }
    done(multi ? values : values[descs[0].name]);
  };
  const onKey = (d: Field) => (e: React.KeyboardEvent) => {
    if (e.key !== "Enter") return;
    // A textarea keeps Enter for newlines; everything else confirms on it.
    if (d.type === "textarea" && !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    submit();
  };
  const set = (name: string, v: string) => {
    setValues((vs) => ({ ...vs, [name]: v }));
    if (bad === name) setBad(null);
  };

  return (
    <>
      {opts.title && <div className="ask-title">{opts.title}</div>}
      <Body body={opts.body} />
      <div className="ask-fields">
        {descs.map((d, i) => {
          const id = "ask-f-" + d.name;
          const label = d.label ? (
            <span className="ask-label">{d.label}{!d.required && <> <span className="ask-opt">optional</span></>}</span>
          ) : undefined;
          const err = bad === d.name ? "required" : undefined;
          const ref = i === 0 ? first : undefined;
          if (d.type === "select") {
            const data = d.options.map((o) => {
              const obj = o && typeof o === "object" ? o as { value: unknown; label?: unknown } : null;
              const v = String(obj ? obj.value : o);
              return { value: v, label: String(obj ? (obj.label == null ? obj.value : obj.label) : o) };
            });
            return (
              <NativeSelect key={d.name} id={id} label={label} error={err} data={data}
                            className="ask-field" size="sm" ref={ref}
                            value={values[d.name]} onChange={(e) => set(d.name, e.currentTarget.value)}
                            onKeyDown={onKey(d)} />
            );
          }
          if (d.type === "textarea") {
            return (
              <Textarea key={d.name} id={id} label={label} error={err} rows={d.rows} autosize={false}
                        className="ask-field" size="sm" ref={ref} placeholder={d.placeholder}
                        value={values[d.name]} onChange={(e) => set(d.name, e.currentTarget.value)}
                        onKeyDown={onKey(d)} />
            );
          }
          return (
            <TextInput key={d.name} id={id} label={label} error={err} autoComplete="off"
                       className="ask-field" size="sm" ref={ref} placeholder={d.placeholder}
                       value={values[d.name]} onChange={(e) => set(d.name, e.currentTarget.value)}
                       onKeyDown={onKey(d)} />
          );
        })}
      </div>
      <div className="ask-btns">
        <Button size="xs" onClick={submit}>{opts.ok || "confirm"}</Button>
        <Button size="xs" variant="default" onClick={() => done(null)}>{opts.cancel || "cancel"}</Button>
        <span className="ask-hint">{hasTA ? "Ctrl+Enter confirms · Esc cancels" : "Enter confirms · Esc cancels"}</span>
      </div>
    </>
  );
}

export function askText(opts: AskTextOptions & { fields: FieldSpec[] }): Promise<Record<string, string> | null>;
export function askText(opts?: AskTextOptions): Promise<string | null>;
export function askText(opts: AskTextOptions = {}): Promise<unknown> {
  const multi = Array.isArray(opts.fields) && opts.fields.length > 0;
  const descs: Field[] = multi
    ? opts.fields!.map((f, i) => normField(f, f.name || ("f" + i), "text"))
    : [normField({
        type: (opts.field && opts.field.type) || (opts.multiline === false ? "text" : "textarea"),
        options: opts.field && opts.field.options,
        label: opts.label, value: opts.value, placeholder: opts.placeholder,
        required: opts.required, rows: opts.rows,
      }, "value", "textarea")];
  return mount<unknown>({ host: opts.host, cancel: null }, (done) => {
    const form = <TextForm opts={opts} descs={descs} multi={multi} done={done} />;
    return opts.host ? <Inline>{form}</Inline> : form;
  });
}

/* ---- askConfirm ----------------------------------------------------------- */
function ConfirmCard({ opts, done }: { opts: AskConfirmOptions; done: (v: unknown) => void }) {
  const danger = !!opts.danger;
  const focus = useAutofocus<HTMLButtonElement>();
  return (
    <div onKeyDown={(e) => {
      // Enter on a focused button is that button's own click, not a confirm
      // — otherwise Enter on "cancel" would confirm.
      if (danger || e.key !== "Enter" || isBtn(e.target)) return;
      e.preventDefault(); done(true);
    }} style={{ display: "contents" }}>
      {opts.title && <div className="ask-title">{opts.title}</div>}
      <Body body={opts.body} />
      <div className="ask-btns">
        {/* A destructive question opens with cancel under the cursor. */}
        <Button size="xs" color={danger ? "red" : undefined} variant={danger ? "light" : "filled"}
                className={danger ? "ask-bad" : undefined}
                ref={danger ? undefined : focus} onClick={() => done(true)}>
          {opts.ok || "confirm"}
        </Button>
        <Button size="xs" variant="default" ref={danger ? focus : undefined} onClick={() => done(false)}>
          {opts.cancel || "cancel"}
        </Button>
        <span className="ask-hint">{danger ? "Esc cancels" : "Enter confirms · Esc cancels"}</span>
      </div>
    </div>
  );
}

export function askConfirm(opts: AskConfirmOptions = {}): Promise<boolean> {
  return mount<boolean>({ host: opts.host, cancel: false, danger: !!opts.danger }, (done) => {
    const card = <ConfirmCard opts={opts} done={done} />;
    return opts.host ? <Inline>{card}</Inline> : card;
  });
}

/* ---- askPick --------------------------------------------------------------
 * The promise-returning picker: a filter, a count, optional async fetch, and
 * the roving arrow-key focus and Enter-to-take. */
function PickBox({ opts, done }: { opts: AskPickOptions; done: (v: unknown) => void }) {
  const [q, setQ] = useState("");
  const [shown, setShown] = useState<PickItem[]>([]);
  const [count, setCount] = useState("scanning…");
  const [loaded, setLoaded] = useState(false);
  const [on, setOn] = useState(0);
  const seq = useRef(0);
  const list = useRef<HTMLDivElement>(null);
  const filter = useAutofocus<HTMLInputElement>();

  const paint = (items: PickItem[], total: number, truncated: boolean) => {
    setShown(items);
    setCount((truncated || total > items.length)
      ? `${items.length} of ${total}`
      : `${items.length} ${items.length === 1 ? "item" : "items"}`);
    const pre = opts.value == null ? -1
      : items.findIndex((it) => String(it.value) === String(opts.value));
    setOn(pre >= 0 ? pre : 0);
    setLoaded(true);
  };

  useEffect(() => {
    const load = (query: string) => {
      if (!opts.fetch) {
        const all = opts.items || [];
        const n = query.toLowerCase();
        const items = !n ? all : all.filter((it) =>
          `${it.label || ""} ${it.meta || ""} ${it.value || ""}`.toLowerCase().includes(n));
        paint(items, all.length, false);
        return;
      }
      const mine = ++seq.current;
      Promise.resolve(opts.fetch(query)).then((d) => {
        // A slower earlier query must not overwrite a later one's results.
        if (mine !== seq.current) return;
        d = d || {};
        const items = d.items || [];
        paint(items, d.total == null ? items.length : d.total, !!d.truncated);
      }).catch(() => { if (mine === seq.current) paint([], 0, false); });
    };
    const t = setTimeout(() => load(q.trim()), q ? 200 : 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  useEffect(() => {
    list.current?.querySelector(".ask-row.on")?.scrollIntoView({ block: "nearest" });
  }, [on, shown]);

  const onKey = (e: React.KeyboardEvent) => {
    if (!shown.length) return;
    const last = shown.length - 1;
    if (e.key === "ArrowDown") { e.preventDefault(); setOn((i) => Math.min(last, i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setOn((i) => Math.max(0, i - 1)); }
    else if (e.key === "Home") { e.preventDefault(); setOn(0); }
    else if (e.key === "End") { e.preventDefault(); setOn(last); }
    else if (e.key === "Enter" && !isBtn(e.target)) {
      e.preventDefault();
      if (shown[on]) done(shown[on].value);
    }
  };

  return (
    <div style={{ display: "contents" }} onKeyDown={onKey}>
      <div className="ask-pickbar">
        <span className="ask-title">{opts.title || "pick one"}</span>
        <input className="ask-filter" type="search" spellCheck={false} autoComplete="off"
               aria-label="Filter" placeholder={opts.placeholder || "filter…"} ref={filter}
               value={q} onChange={(e) => setQ(e.currentTarget.value)} />
        <span className="ask-count">{count}</span>
        <span className="ask-spacer" />
        <span className="ask-pickacts">
          {(opts.actions || []).map((a, i) => (
            <Button key={i} size="xs" variant="default"
                    onClick={() => { try { a.onClick(done); } catch { done(null); } }}>
              {a.label || "action"}
            </Button>
          ))}
        </span>
        <Button size="xs" variant="default" onClick={() => done(null)}>close</Button>
      </div>
      <div className="ask-list" role="listbox" tabIndex={-1} ref={list}>
        {!loaded
          ? <div className="ask-empty">scanning…</div>
          : !shown.length
            ? <div className="ask-empty">{opts.empty || "nothing matches"}</div>
            : shown.map((it, i) => (
              <div key={i} className={`ask-row${i === on ? " on" : ""}`} role="option"
                   aria-selected={i === on} onClick={() => done(it.value)}>
                <span className="ask-row-label">{it.label == null ? String(it.value) : it.label}</span>
                {(it.tags || []).map((t, j) => (
                  <span key={j} className={`ask-tag${t.tone ? " t-" + t.tone : ""}`}>{t.text}</span>
                ))}
                {it.meta && <span className="ask-row-meta">{it.meta}</span>}
              </div>
            ))}
      </div>
    </div>
  );
}

export function askPick<T = unknown>(opts: AskPickOptions = {}): Promise<T | null> {
  return mount<T | null>({ cancel: null, wide: true }, (done) => <PickBox opts={opts} done={done} />);
}

/* Assigned rather than declared on Window: settings/confirm.ts still carries
   its own (narrower) declaration of askConfirm from when this lived in
   ask.js, and two declarations of one name do not typecheck. */
Object.assign(window, { askText, askConfirm, askPick });
