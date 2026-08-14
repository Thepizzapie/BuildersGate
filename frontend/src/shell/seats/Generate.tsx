import { useEffect, useRef, useState } from "react";
import { Ti } from "../Ti";
import { mutate } from "../../bridge";
import { Head, Nothing } from "./prims";
import { RECIPES, type Recipe } from "./recipes";
import type { SeatBodyProps } from "./types";

/* GENERATE — the seat's own craft, as things you can ask for.
 *
 * THE FIRST VERSION OF THIS TAB WAS THE NODE LIBRARY AND THAT WAS WRONG. A
 * workflow template is a graph with typed ports and a config panel per node;
 * running one meant opening a canvas, finding the node that takes the prompt,
 * opening its inspector and typing into it. So the seat's most common act —
 * "make me a concept sheet" — was its most expensive interaction, and nothing
 * on the canvas said which node to touch first.
 *
 * SO THE TAB IS TWO HALVES, IN THE ORDER PEOPLE NEED THEM.
 *
 *   RECIPES      the handful of things this craft actually makes, each a form
 *                of two to four fields with an example in every placeholder.
 *                This is the default and it is at the top.
 *   CUSTOM       the node builder, unchanged, one click away, for assembling
 *                something the recipes do not cover. It is genuinely better at
 *                that and genuinely worse at everything above it.
 *
 * WHAT A RECIPE PRODUCES IS A BRIEF. Generation lives behind MCP tools held by
 * the seat agent, not by this page — that is the execution model, and it is why
 * the node graph also compiles to queue items rather than calling generators
 * itself. A form that reached around the board would be a second execution path
 * with no lane check, no QA gate and no spend row. See recipes.ts.
 */

declare global {
  interface Window {
    WF?: {
      open(host: HTMLElement, api?: unknown, opts?: { seat?: string }): Promise<void>;
      templates?: { id: string; category?: string }[];
    };
  }
}

type Mode = "recipes" | "custom";

export function Generate({ seat, active }: SeatBodyProps) {
  const host = useRef<HTMLDivElement>(null);
  const [mode, setMode] = useState<Mode>("recipes");
  const [open, setOpen] = useState<string>("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState("");

  const recipes = RECIPES[seat.role] || [];

  useEffect(() => {
    if (mode !== "custom") return;
    const el = host.current;
    if (!el || !active || !window.WF) return;
    void window.WF.open(el, {}, { seat: seat.role });
  }, [mode, seat.role, active]);

  /* Switching seats must not carry a half-filled form across — the fields of
     one craft's recipe mean nothing in another's. */
  useEffect(() => {
    setOpen(""); setValues({}); setErr(""); setDone(""); setMode("recipes");
  }, [seat.role]);

  const recipe: Recipe | undefined = recipes.find((r) => r.id === open);

  function pick(r: Recipe) {
    setOpen(r.id === open ? "" : r.id);
    setErr(""); setDone("");
    /* Defaults come from the first option of every fixed-vocabulary field, so a
       form is submittable the moment its free-text fields are filled. */
    const seed: Record<string, string> = {};
    for (const f of r.fields) if (f.options?.length) seed[f.key] = f.options[0];
    setValues(seed);
  }

  const missing = (recipe?.fields || [])
    .filter((f) => !f.optional && !(values[f.key] || "").trim());

  async function run() {
    if (!recipe) return;
    setBusy(true); setErr(""); setDone("");

    if (recipe.post) {
      const { url, body } = recipe.post(values);
      const r = await mutate(url, { body, quiet: true });
      setBusy(false);
      if (!r.ok) { setErr(r.error || "the generator refused"); return; }
      setDone(`${recipe.label} started — it lands in this seat's own tab when it finishes.`);
      setOpen("");
      return;
    }

    const links = recipe.chain?.(values) || [];
    /* ONE LINK IS AN ITEM; TWO OR MORE IS A CHAIN. Filing a dependent pair as
       two independent items lets autodeploy start both in the same tick, and
       the second writes against a file the first has not produced yet — the
       exact failure the chain endpoint exists to prevent. */
    const r = links.length > 1
      ? await mutate("/api/queue/chain", { body: { links }, quiet: true })
      : await mutate("/api/queue", { body: links[0], quiet: true });
    setBusy(false);
    if (!r.ok) { setErr(r.error || "could not file the work"); return; }
    setDone(links.length > 1
      ? `Filed ${links.length} linked items — the second does not start until the first is done.`
      : "Filed on the board. The Queue tab shows it, and the seat picks it up when the board dispatches.");
    setOpen("");
  }

  if (mode === "custom") {
    return (
      <div className="bgs-genwrap">
        <div className="bgs-genswitch">
          <button className="bgs-btn" onClick={() => setMode("recipes")}>
            <Ti name="arrow-left" size={12} /> recipes
          </button>
          <span className="hint">
            the canvas is for assembling a pipeline the recipes do not cover —
            it compiles to the same board items they file
          </span>
        </div>
        {window.WF
          ? <div className="bgs-wfhost" ref={host} />
          : <div className="bgs-none">
              wf.js is not loaded on this build — the workflow library, the step
              registry and Run all live in it.
            </div>}
      </div>
    );
  }

  return (
    <div className="bgs-pad">
      <Head label="Generate"
            hint="the things this craft makes — filled in, then filed"
            right={
              <button className="bgs-btn" onClick={() => setMode("custom")}>
                <Ti name="topology-star-3" size={12} /> custom workflow
              </button>
            } />

      {!recipes.length && (
        <Nothing what="this seat has no recipes yet"
                 how="recipes.ts declares them per seat; the custom workflow canvas is above" />
      )}

      {done && (
        <div className="bgs-ok">
          <Ti name="check" size={15} color="var(--good)" />{done}
        </div>
      )}

      <div className="bgs-recipes">
        {recipes.map((r) => (
          <div className={`bgs-recipe${r.id === open ? " on" : ""}`} key={r.id}>
            <button className="h" onClick={() => pick(r)}>
              <Ti name={r.icon} size={16} color={`var(--c-${seat.role})`} />
              <span className="t">{r.label}</span>
              <span className="s">{r.hint}</span>
              <Ti name={r.id === open ? "chevron-up" : "chevron-down"} size={14} />
            </button>

            {r.id === open && (
              <div className="b">
                {r.fields.map((f) => (
                  <label key={f.key}>
                    <span className="k">
                      {f.label}{f.optional ? " (optional)" : ""}
                    </span>
                    {f.hint && <span className="hint">{f.hint}</span>}
                    {f.options ? (
                      <select value={values[f.key] || f.options[0]}
                              onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}>
                        {f.options.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                    ) : f.lines ? (
                      <textarea rows={f.lines} placeholder={f.placeholder}
                                value={values[f.key] || ""}
                                onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
                    ) : (
                      <input type="text" placeholder={f.placeholder}
                             value={values[f.key] || ""}
                             onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
                    )}
                  </label>
                ))}

                {err && <div className="bgs-readerr">{err}</div>}

                <div className="acts">
                  {/* WHAT YOU GET, BEFORE YOU PRESS IT. Half of these spend
                      money or agent time; a button whose outcome is only
                      discoverable by pressing it is how a cinematic seat buys
                      a frame nobody meant to buy. */}
                  <span className="yield">
                    <Ti name="arrow-narrow-right" size={12} />{r.yields}
                  </span>
                  <span className="sp" />
                  <button className="bgs-btn" onClick={() => setOpen("")} disabled={busy}>
                    cancel
                  </button>
                  <button className="bgs-btn go"
                          style={{ ["--btn" as string]: `var(--c-${seat.role})` }}
                          onClick={run}
                          disabled={busy || !!missing.length}
                          title={missing.length
                            ? `fill in ${missing.map((f) => f.label).join(", ")}`
                            : undefined}>
                    {busy ? "…" : r.post ? "generate" : "file it"}
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
