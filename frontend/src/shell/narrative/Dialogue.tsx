import { useCallback, useEffect, useMemo, useState } from "react";
import { ScrollArea } from "@mantine/core";
import { Ti } from "../Ti";
import { usePoll } from "../../hooks";
import { compose } from "../../bridge";
import {
  dialogueList, dialogueRead, dialogueValidate, EMPTY_TREE,
  type DValidate, type Tree, type TreeRow,
} from "./api";
import { clip, layout, NODE_H, NODE_W, type Problem, type ProblemKind } from "./graph";
import type { HeadSlot } from "./head";

/* 10b · DIALOGUE — the graph is the editor.
 *
 * dialogue.py refuses a write for three things, and all three are properties of
 * the SHAPE of the conversation rather than of any line in it: a choice
 * pointing nowhere, a node nothing reaches, a node with no ending beyond it.
 * In the JSON they are invisible — every one of those files is valid JSON with
 * plausible-looking prose in it. Drawn, they are a stub going off into space,
 * an island in its own column, and a pocket with no way out.
 *
 * So the tree is drawn, every failing node is marked ON the drawing with the
 * reason, and the strip along the bottom repeats each problem NAMING ITS NODE —
 * which is how dialogue.py phrases its refusals, and the reason its refusals
 * are actionable where a schema error is not.
 *
 * THE DRAWING IS THE CLIENT'S; THE VERDICT IS THE SERVER'S. graph.ts computes
 * the three shape failures because they have to be positioned ON the picture,
 * and no endpoint can say where. What it must not do is decide: the writer
 * refuses for more than three things, so `GET /api/dialogue/{name}/validate` —
 * which runs dialogue.validate() and returns its refusal as data — is asked on
 * every open and its answer is what the strip reports. When the two disagree,
 * both are shown and the server's is the one labelled as the writer's. A tree
 * this screen calls clean and dialogue_write refuses is the failure that
 * reconciliation exists to make impossible.
 *
 * CLICKING A CHOICE IN THE INSPECTOR SELECTS ITS TARGET. That is "walk it" from
 * the design, and it needs no backend: following the choices IS the playthrough.
 * A choice whose target does not exist cannot be followed, and the row says so
 * rather than doing nothing, because doing nothing is what the bug does in the
 * game.
 */

const LIST_MS = 20000;

const PROBLEM_LABEL: Record<ProblemKind, string> = {
  dangling: "points nowhere",
  orphan: "unreachable",
  trapped: "no way out",
  "end-with-choices": "end with choices",
};

/** The three the module refuses on, versus the adjacent one. The strip says
 *  which is which so "3 problems" is never quietly four different things. */
const THE_THREE = new Set<ProblemKind>(["dangling", "orphan", "trapped"]);

export function Dialogue({ head, active }: { head: HeadSlot; active: boolean }) {
  const [trees, setTrees] = useState<TreeRow[]>([]);
  const [listError, setListError] = useState<string | undefined>();
  const [name, setName] = useState("");
  const [tree, setTree] = useState<Tree>(EMPTY_TREE);
  const [treeError, setTreeError] = useState("");
  const [server, setServer] = useState<DValidate | null>(null);
  const [sel, setSel] = useState("");

  const refresh = useCallback(async () => {
    const { trees: rows, error } = await dialogueList();
    setListError(error);
    setTrees(rows);
    setName((n) => n || rows[0]?.name || "");
  }, []);
  usePoll(refresh, LIST_MS, active);

  /* The name whose READ came back without an error, so "this tree is empty" can
     be told apart from "no tree is open". They rendered identically before, and
     the shared rendering was "pick a tree on the left" — printed underneath the
     tree that was already picked and highlighted. */
  const [loaded, setLoaded] = useState("");

  useEffect(() => {
    if (!name) {
      setTree(EMPTY_TREE); setServer(null); setTreeError(""); setLoaded("");
      return;
    }
    let live = true;
    /* Both in one round trip's worth of waiting. The validator reads the same
       file; asking for it after the graph has drawn would show a clean strip
       and then correct itself, which is the version of this the reader stops
       trusting. */
    void Promise.all([dialogueRead(name), dialogueValidate(name)])
      .then(([t, v]) => {
        if (!live) return;
        const err = (t as unknown as { __error?: string }).__error;
        setTreeError(err || "");
        setLoaded(err ? "" : name);
        setTree(err ? EMPTY_TREE : { ...t, nodes: t.nodes || [] });
        setServer(err ? null : v);
        setSel("");
      });
    return () => { live = false; };
  }, [name]);

  const g = useMemo(() => layout(tree), [tree]);
  const selected = useMemo(
    () => g.nodes.find((n) => n.id === sel) || null, [g.nodes, sel]);

  const three = g.problems.filter((p) => THE_THREE.has(p.kind));

  /* The server answered about THIS tree. `__error` means the route did not
     answer at all, which is not a verdict and must never be read as one. */
  const says = server && !server.__error ? server : null;
  const refused = !!says && !says.ok;
  /* Clean means the writer accepts it AND the drawing found nothing. Either
     one alone is a partial answer. */
  const clean = tree.nodes.length > 0 && !refused && g.problems.length === 0;
  /* The writer accepts a tree the drawing calls broken on one of THE THREE —
     the two implementations of the same rule have drifted. Worth its own
     sentence rather than a silent preference for either. */
  const drift = !!says && says.ok && three.length > 0;

  const chips = (
    <>
      {tree.nodes.length > 0 && (
        <span className="bg4-narchip">
          {tree.rel_path || `${tree.name}.dialogue.json`} · {tree.nodes.length} nodes
        </span>
      )}
      {tree.nodes.length > 0 && (
        <span className="bg4-narchip"
              style={{ color: clean ? "var(--good, #4ec98f)" : "var(--bad, #ff7a6b)" }}>
          <Ti name={clean ? "circle-check" : "alert-hexagon"} size={13} />
          {refused ? "dialogue.validate refuses this"
           : clean ? (says ? "dialogue.validate accepts this"
                           : "the three structural checks pass — the writer has more")
           : `${g.problems.length} problem${g.problems.length === 1 ? "" : "s"}`}
        </span>
      )}
    </>
  );

  return (
    <div className="bg4-nar-dlg">
      <div className="bg4-narlist">
        <div className="bg4-narlist-head">
          <Ti name="messages" size={14} />
          <span className="ro">{trees.length} trees</span>
        </div>
        <ScrollArea className="bg4-narscroll">
          {/* The rail says the SHORT version and the canvas says the long one.
              Both used to carry a paragraph, so the same explanation wrapped
              twice — once into a 260px column where it was unreadable. */}
          {!trees.length && (
            <div className="bg4-empty">
              {listError
                ? `no dialogue API on this build — ${listError}`
                : "none yet — see the canvas"}
            </div>
          )}
          {trees.map((t) => (
            /* THE LISTING ALREADY CARRIES THE REASON. list_dialogues re-reads
               and re-validates every file and puts the refusal in `error`; the
               row printed "does not validate" and threw it away, so the one
               place a broken tree is cheapest to notice told you the least
               about it. `title` was declared and unread too — a tree with a
               human title was shown as a filename. */
            <button key={t.name} className={`bg4-narrow${t.name === name ? " on" : ""}`}
                    title={t.error || t.title || t.rel_path || t.name}
                    onClick={() => setName(t.name)}>
              <span className="l">
                <Ti name={t.ok === false ? "alert-hexagon" : "message-2"} size={14}
                    color={t.ok === false ? "var(--bad, #ff7a6b)" : undefined} />
                <span className="s">{t.title || t.name}</span>
              </span>
              <span className={`k${t.ok === false && t.error ? " err" : ""}`}>
                {t.ok === false
                  ? (t.error || "does not validate")
                  : `${t.nodes ?? 0} nodes${t.start ? ` · from ${t.start}` : ""}`}
              </span>
            </button>
          ))}
        </ScrollArea>
      </div>

      <div className="bg4-narmain">
        {head(chips)}

        {/* THE GRID IS THE GRAPH'S GROUND, so it is only drawn when there is a
            graph. Painted under an empty canvas it reads as a surface you can
            draw on, which this is not — the screenshot that started this was a
            thousand pixels of ruled paper with one grey sentence on it. */}
        <div className={`bg4-narcanvas${tree.nodes.length ? "" : " bare"}`}>
          {!tree.nodes.length && (
            /* THREE DIFFERENT EMPTIES, because they need three different next
               moves. The one that shipped said "pick a tree on the left" in all
               of them — including when the left holds nothing to pick, which is
               an instruction the reader cannot follow. */
            <div className="bg4-narempty">
              {treeError ? (
                <>
                  <b>that tree could not be read</b>
                  <span>{treeError}</span>
                </>
              ) : loaded && loaded === name ? (
                /* READ FINE, AND EMPTY. This used to fall through to "pick a
                   tree on the left" while the tree WAS picked and highlighted —
                   an instruction the reader had already followed. A file with a
                   `nodes: []` is a real state and the writer refuses it, so it
                   gets its own sentence. */
                <>
                  <b>{name} has no nodes</b>
                  <span>
                    The file parses and contains an empty <code>nodes</code>{" "}
                    list, so there is no conversation to draw and nothing for the
                    three checks to fail on. <code>dialogue_write</code> refuses
                    a tree with no ending, and a tree with no nodes has none.
                  </span>
                </>
              ) : trees.length ? (
                <>
                  <b>pick a tree on the left</b>
                  <span>
                    it opens as a graph, because the three ways a dialogue fails
                    — a choice pointing nowhere, a node nothing reaches, a node
                    with no ending beyond it — are shapes, not lines
                  </span>
                </>
              ) : (
                <>
                  <b>no dialogue trees yet</b>
                  <span>
                    Writing one is an MCP call on purpose: <code>dialogue_write</code>{" "}
                    is where canon_check runs and where the lane and the QA gate
                    bite, and an HTTP write from this page would be a second,
                    quieter path to the same files with none of that around it.
                    So this screen reads and proposes; the seat writes.
                  </span>
                  <div className="acts">
                    <button className="bgs-btn go"
                            onClick={() => compose({
                              seat: "narrative",
                              title: "write the first dialogue tree",
                              brief:
                                "Write a dialogue tree with dialogue_write and " +
                                "run canon_check on it before it lands. Every " +
                                "node needs a way out: a choice that points at " +
                                "no node, a node nothing reaches, and a node " +
                                "with no ending beyond it are the three the " +
                                "writer refuses on.",
                            })}>
                      brief the narrative seat
                    </button>
                    <span className="hint">
                      opens the director's box with the task filled in
                    </span>
                  </div>
                </>
              )}
            </div>
          )}
          {tree.nodes.length > 0 && (
            <svg width={g.width} height={g.height} className="bg4-nargraph"
                 role="img" aria-label={`${tree.name}: ${tree.nodes.length} nodes`}>
              {/* Edges under the nodes, so a line never crosses a label. */}
              {g.edges.map((e) => (
                <g key={e.key}>
                  <path d={e.d} fill="none" strokeWidth={1.25}
                        strokeDasharray={e.back ? "4 3" : e.missing ? "2 3" : undefined}
                        stroke={e.missing ? "var(--bad, #ff7a6b)" : "var(--line-strong)"} />
                  {e.missing && e.tip && (
                    /* The stub's dead end, named. A branch that stops in blank
                       canvas is the failure drawn literally. */
                    <>
                      <circle r={3} fill="var(--bad, #ff7a6b)" cx={e.tip.x} cy={e.tip.y} />
                      <text className="miss" x={e.tip.x + 6} y={e.tip.y + 3}>
                        {clip(e.to, 16)}?
                      </text>
                    </>
                  )}
                </g>
              ))}
              {g.nodes.map((p) => {
                const problems = g.byNode.get(p.id) || [];
                const isStart = p.id === g.start;
                const cls = ["nd",
                  problems.length ? "bad" : p.node.end ? "end" : "",
                  isStart ? "start" : "", p.id === sel ? "on" : ""].join(" ").trim();
                return (
                  <g key={p.id} className={cls} transform={`translate(${p.x},${p.y})`}
                     role="button" tabIndex={0} aria-label={p.id}
                     onClick={() => setSel(p.id)}
                     onKeyDown={(ev) => {
                       if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); setSel(p.id); }
                     }}>
                    <title>
                      {problems.length
                        ? problems.map((x) => `${x.node}: ${x.text}`).join("\n")
                        : `${p.id}${p.node.end ? " · ending" : ""}`}
                    </title>
                    <rect width={NODE_W} height={NODE_H} rx={6} />
                    <text className="id" x={10} y={18}>{clip(p.id, 20)}</text>
                    <text className="badge" x={NODE_W - 10} y={18} textAnchor="end">
                      {problems.length ? PROBLEM_LABEL[problems[0].kind]
                       : isStart ? "start" : p.node.end ? "end" : ""}
                    </text>
                    <text className="tx" x={10} y={36}>
                      {clip(p.node.speaker ? `${p.node.speaker}: ${p.node.text || ""}`
                                           : (p.node.text || ""), 30)}
                    </text>
                    <text className="tx dim" x={10} y={51}>
                      {p.node.end ? "ending"
                       : `${(p.node.choices || []).length} choice(s)`}
                    </text>
                  </g>
                );
              })}
            </svg>
          )}
        </div>

        {selected && (
          <div className="bg4-narnode">
            <div className="h">
              <Ti name="message-2" size={15} />
              <b>{selected.id}</b>
              {selected.node.speaker && <span className="sp2">{selected.node.speaker}</span>}
              <span className="sp" />
              <button className="x" onClick={() => setSel("")} aria-label="close">
                <Ti name="x" size={14} />
              </button>
            </div>
            {selected.node.text && <p className="t">{selected.node.text}</p>}
            <div className="cs">
              {(selected.node.choices || []).map((c, i) => {
                const exists = g.nodes.some((n) => n.id === c.goto);
                return (
                  <button key={i} className={`c${exists ? "" : " dead"}`}
                          onClick={() => exists && setSel(c.goto)}
                          title={exists ? `go to ${c.goto}`
                                        : `${c.goto} is not a node in this dialogue`}>
                    <Ti name={exists ? "arrow-narrow-right" : "circle-x"} size={13} />
                    <span className="l">{c.text}</span>
                    <span className="g">{c.goto}</span>
                  </button>
                );
              })}
              {!(selected.node.choices || []).length && (
                <span className="bg4-narnone">
                  {selected.node.end ? "an ending — the conversation stops here, on purpose"
                   : "no choices and not marked end — the conversation stops here by accident"}
                </span>
              )}
            </div>
          </div>
        )}

        {tree.nodes.length > 0 && (
          <div className={`bg4-narrefuse${clean ? " ok" : ""}`}>
            <div className="h">
              <Ti name={clean ? "circle-check" : "alert-hexagon"} size={17} />
              <b>
                {refused
                  /* THE WRITER'S OWN SENTENCE. Not inferred, not a subset:
                     /api/dialogue/{name}/validate ran dialogue.validate() and
                     this is what it said. */
                  ? "Write refused — dialogue.validate says so"
                  : g.problems.length
                  ? `${g.problems.length} problem`
                    + `${g.problems.length === 1 ? "" : "s"}, each naming its node`
                  : says
                  /* Earned: the authority answered, about this tree. */
                  ? "dialogue.validate accepts this tree"
                  /* NOT "dialogue.write would accept this". This computes the three
                     SHAPE failures the graph exists to show; bgate_core/dialogue.py
                     also refuses duplicate ids, an over-long id, a choice with no
                     text or no goto, a start that names no node, and a tree with no
                     end at all. Claiming the writer's verdict from a subset of the
                     writer's rules is how a green screen turns into a refused
                     write. */
                  : "Three structural checks pass — the writer runs more"}
              </b>
              <span className="sp" />
              <span className="m">
                {refused
                  ? "nothing would land — dialogue.write validates before it writes"
                  : clean
                  ? `${g.nodes.length} nodes, ${(says?.ends ?? tree.ends)?.length ?? g.nodes.filter((n) => n.node.end).length} ending(s), everything reachable`
                  : "drawn from the file — not the writer's verdict"}
              </span>
            </div>
            {refused && (
              <div className="srv">
                <span className="n">dialogue.validate</span>
                <span className="t">{says?.problem || "refused without a reason"}</span>
              </div>
            )}
            {drift && (
              /* Two implementations of one rule, disagreeing. Silence here
                 would mean picking a winner without saying so, and the reader
                 has no way to find out which was picked. */
              <div className="srv drift">
                <span className="n">disagreement</span>
                <span className="t">
                  dialogue.validate accepts this tree, and the drawing below finds{" "}
                  {three.length} of the three it also checks. The writer is the
                  authority on whether a write lands; this screen's copy of the
                  rule has drifted and graph.ts is where it lives.
                </span>
              </div>
            )}
            {!says && (
              <div className="srv drift">
                <span className="n">unverified</span>
                <span className="t">
                  the validator did not answer{server?.__error ? ` — ${server.__error}` : ""},
                  so everything below is this screen's own reading of three of the
                  writer's rules and not the writer's verdict
                </span>
              </div>
            )}
            {g.problems.map((p: Problem, i) => (
              <button className={`p${THE_THREE.has(p.kind) ? "" : " adj"}`} key={i}
                      onClick={() => setSel(p.node)}>
                <span className="n">{p.node}</span>
                <span className="t">{p.text}</span>
                <span className="k">{PROBLEM_LABEL[p.kind]}</span>
              </button>
            ))}
            {three.length > 0 && three.length !== g.problems.length && (
              <div className="foot">
                {three.length} of these are the three checks dialogue.py names; the
                rest are refusals from the same pass
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
