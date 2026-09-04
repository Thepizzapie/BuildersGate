import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Group, ScrollArea, Select, Textarea } from "@mantine/core";
import { Ti } from "../Ti";
import { Markdown } from "../../components/Markdown";
import { useEvents, FALLBACK_MS } from "../../hooks";
import { toast } from "../../bridge";
import { directorChat, directorConfigure, directorNew, directorSay,
         directorUsageConnect, directorUsageDisconnect, directorApprove,
         directorDispatchMode,
         type ChatMsg, type ChatState, type DirectorApproval } from "./api";

/* A CLAUDE CODE SESSION, IN A PANE. Nothing else.
 *
 * What it replaced: a composer with a mode switch (dispatch / brainstorm), a
 * tag menu (one running agent / every agent / a seat / the director), an asset
 * linker, and a transcript of work-item cards carrying cost, step counts and a
 * resume-in-CLI button — four ways to send a sentence and none of them a chat.
 *
 * The messages come from the session's own transcript: what you said, what it
 * said, and the tools it called on the way. Work reaches the board only when
 * the director calls queue_add, which is visible in this pane as a tool line
 * and on the rail as a card.
 */

const POLL_MS = 1200;

export function DirectorChat({ active, onSent }: {
  active: boolean;
  /** The board is a different poll; a filed item should not wait for it. */
  onSent?: () => void;
}) {
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [running, setRunning] = useState(false);
  const [waiting, setWaiting] = useState("");
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [runner, setRunner] = useState("claude");
  const [model, setModel] = useState("opus");
  const [dispatchMode, setDispatchMode] = useState<"structured" | "chaos">("structured");
  const [runners, setRunners] = useState<NonNullable<ChatState["runners"]>>([]);
  const [models, setModels] = useState<NonNullable<ChatState["models"]>>({});
  const [usage, setUsage] = useState<NonNullable<ChatState["usage"]>>({});
  const [usageBridge, setUsageBridge] = useState<NonNullable<ChatState["usage_bridge"]>>({});
  const [approvals, setApprovals] = useState<DirectorApproval[]>([]);
  const [approvalBusy, setApprovalBusy] = useState("");
  const [bridgeBusy, setBridgeBusy] = useState(false);
  const seen = useRef(0);
  const feed = useRef<HTMLDivElement>(null);
  const wasRunning = useRef(false);

  const poll = useCallback(async () => {
    const got = await directorChat(seen.current);
    if (got.__error) return;
    const fresh = got.messages || [];
    if (fresh.length) {
      seen.current = fresh[fresh.length - 1].n;
      setMsgs((was) => [...was, ...fresh]);
    }
    setRunning(!!got.running);
    setWaiting(String(got.waiting || ""));
    if (got.runner) setRunner(got.runner);
    if (got.model) setModel(got.model);
    if (got.dispatch_mode) setDispatchMode(got.dispatch_mode);
    if (got.runners) setRunners(got.runners);
    if (got.models) setModels(got.models);
    if (got.usage) setUsage(got.usage);
    if (got.usage_bridge) setUsageBridge(got.usage_bridge);
    setApprovals(got.approvals || []);
    // The board only changes when a turn ends, so refresh it then rather than
    // on every tick of this poll.
    if (wasRunning.current && !got.running) onSent?.();
    wasRunning.current = !!got.running;
  }, [onSent]);
  /* A running turn is a stream, not a change to notice: the transcript grows
     between events, so it keeps its fast tick only while a turn is open. */
  useEvents(poll, { enabled: active, kinds: ["director.*", "agent.*", "item.*"],
                    fallbackMs: running ? POLL_MS : FALLBACK_MS });

  /* Follow the tail unless the reader has scrolled up to read something. */
  useEffect(() => {
    const el = feed.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (near) el.scrollTop = el.scrollHeight;
  }, [msgs.length, approvals.length, running]);

  /* The one verb other screens need: atlas and the asset library both offer
     "deploy a task about this", and both prefill this box. */
  useEffect(() => {
    const prev = window.BGCompose;
    window.BGCompose = ({ seat, title, brief }) =>
      setText([seat ? `@${seat}` : "", title, brief].filter(Boolean).join(" — "));
    return () => { window.BGCompose = prev; };
  }, []);

  async function send() {
    const said = text.trim();
    if (!said || sending) return;
    setSending(true);
    setText("");
    const r = await directorSay(said);
    setSending(false);
    if (!r.ok) { setText(said); return; }
    poll();
  }

  async function fresh() {
    const r = await directorNew();
    if (!r.ok) return;
    seen.current = 0;
    setMsgs([]);
    toast("new session", "ok");
  }

  async function choose(nextRunner: string, nextModel: string) {
    if (running || sending) return;
    setSending(true);
    const r = await directorConfigure(nextRunner, nextModel);
    setSending(false);
    if (!r.ok || !r.data) return;
    if (r.data.runner) setRunner(r.data.runner);
    if (r.data.model) setModel(r.data.model);
    if (r.data.usage) setUsage(r.data.usage);
    if (r.data.models) setModels(r.data.models);
  }

  async function chooseDispatchMode(mode: "structured" | "chaos") {
    if (sending || mode === dispatchMode) return;
    setSending(true);
    const r = await directorDispatchMode(mode);
    setSending(false);
    if (r.ok) {
      setDispatchMode(mode);
      toast(`${mode} dispatch mode`, "ok");
      onSent?.();
    }
  }

  async function toggleUsageBridge() {
    if (bridgeBusy) return;
    setBridgeBusy(true);
    const r = usageBridge.enabled
      ? await directorUsageDisconnect()
      : await directorUsageConnect();
    setBridgeBusy(false);
    if (!r.ok || !r.data) return;
    setUsageBridge(r.data);
    if (!r.data.enabled) setUsage((was) => ({ ...was, five_hour: {}, weekly: {} }));
    poll();
  }

  async function answerApproval(id: string, decision: string) {
    if (approvalBusy) return;
    setApprovalBusy(id);
    const r = await directorApprove(id, decision);
    setApprovalBusy("");
    if (r.ok) setApprovals((rows) => rows.filter((row) => row.id !== id));
    poll();
  }

  return (
    <div className="bg4-console-main">
      <ScrollArea className="bg4-transcript" type="auto" viewportRef={feed}>
        {msgs.length === 0 && !running && (
          <div className="bg4-console-quiet">
            <b>Director session</b>
            <span>
              A native coding session in this project, holding the builders-gate
              tools. Ask it something, or tell it what to build and it files
              the work for a seat.
            </span>
          </div>
        )}
        {msgs.map((m) => <Line key={m.n} msg={m} />)}
        {approvals.map((approval) => (
          <ApprovalCard key={approval.id} approval={approval}
                        busy={approvalBusy === approval.id}
                        onAnswer={answerApproval} />
        ))}
        {running && (
          <div className="bg4-msg dir live">
            <div className="who">director</div>
            <div className="txt thinking">{waiting || "working…"}</div>
          </div>
        )}
      </ScrollArea>

      <div className="bg4-composer">
        <div className="bg4-director-strip">
          <Group gap={6} wrap="nowrap" className="bg4-director-pickers">
            <Select aria-label="Director coding tool" size="xs" allowDeselect={false}
                    value={runner} disabled={running || sending}
                    data={runners.map((r) => ({ value: r.value, label: r.label,
                                                disabled: !r.installed }))}
                    onChange={(value) => {
                      if (!value || value === runner) return;
                      const rows = models[value] || [];
                      const preferred = rows.find((r) => r.default) || rows[0];
                      choose(value, preferred?.value || "");
                    }} />
            <Select aria-label="Director model" size="xs" allowDeselect={false}
                    searchable value={model} disabled={running || sending}
                    data={(models[runner] || []).map((m) => ({
                      value: m.value, label: m.label,
                    }))}
                    onChange={(value) => {
                      if (value && value !== model) choose(runner, value);
                    }} />
            <Select aria-label="Director dispatch mode" size="xs" allowDeselect={false}
                    value={dispatchMode} disabled={sending}
                    data={[{ value: "structured", label: "Structured" },
                           { value: "chaos", label: "Chaos" }]}
                    onChange={(value) => {
                      if (value === "structured" || value === "chaos")
                        void chooseDispatchMode(value);
                    }} />
          </Group>
          <div className="bg4-director-usage" aria-label="Director usage">
            <Usage label="context" value={usage.context?.limit
              ? Math.round(100 * Number(usage.context.used || 0) / usage.context.limit)
              : undefined}
              text={formatTokens(usage.context?.used, usage.context?.limit)} />
            <Usage label="5h" value={usage.five_hour?.used_percent}
                   text={formatWindow(usage.five_hour)} />
            <Usage label="week" value={usage.weekly?.used_percent}
                   text={formatWindow(usage.weekly)} />
          </div>
        </div>
        {runner === "claude" && (
          <div className={`bg4-usage-bridge${usageBridge.enabled ? " on" : ""}`}>
            <Ti name={usageBridge.enabled ? "shield-check" : "shield"} size={13} />
            <span>{usageBridge.enabled
              ? usageBridge.has_snapshot
                ? "Claude usage is linked locally"
                : "Linked locally — use Claude once, then restart Claude Code if needed"
              : "Show Claude limits locally without sharing credentials"}</span>
            <Button variant="subtle" size="compact-xs" loading={bridgeBusy}
                    onClick={toggleUsageBridge}>
              {usageBridge.enabled ? "disconnect" : "connect"}
            </Button>
          </div>
        )}
        <Group gap="xs" align="flex-end" wrap="nowrap">
          <div style={{ flex: 1, minWidth: 0 }}>
            <Textarea autosize minRows={1} maxRows={10} variant="unstyled"
                      value={text} onChange={(e) => setText(e.currentTarget.value)}
                      placeholder="ask the director, or say what to build"
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          send();
                        }
                      }} />
          </div>
          <Button variant="default" size="compact-xs" onClick={fresh}>new</Button>
          <Button size="xs" onClick={send} loading={sending}
                  disabled={!text.trim()}>send</Button>
        </Group>
      </div>
    </div>
  );
}

function ApprovalCard({ approval, busy, onAnswer }: {
  approval: DirectorApproval; busy: boolean;
  onAnswer(id: string, decision: string): void;
}) {
  const detail = approval.command || approval.reason || approval.server ||
    (approval.permissions ? JSON.stringify(approval.permissions) : "Codex requests approval");
  const has = (decision: string) => approval.available_decisions.includes(decision);
  return (
    <div className="bg4-codex-approval">
      <div className="who">Codex approval · {approval.kind.replace("_", " ")}</div>
      <code>{detail}</code>
      {approval.cwd && <span className="cwd">{approval.cwd}</span>}
      <Group gap={6}>
        {has("accept") && <Button size="compact-xs" loading={busy}
          onClick={() => onAnswer(approval.id, "accept")}>approve</Button>}
        {has("acceptForSession") && <Button size="compact-xs" variant="default"
          disabled={busy} onClick={() => onAnswer(approval.id, "acceptForSession")}>session</Button>}
        {has("decline") && <Button size="compact-xs" color="red" variant="subtle"
          disabled={busy} onClick={() => onAnswer(approval.id, "decline")}>deny</Button>}
      </Group>
    </div>
  );
}

function Usage({ label, value, text }: { label: string; value?: number; text: string }) {
  const safe = Math.max(0, Math.min(100, Number(value || 0)));
  return (
    <div className="bg4-usage" title={`${label}: ${text}`}>
      <span><b>{label}</b>{text}</span>
      <i><em style={{ width: `${safe}%` }} /></i>
    </div>
  );
}

function formatTokens(used?: number, limit?: number): string {
  if (!used && !limit) return " —";
  const short = (n: number) => n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
  return ` ${short(Number(used || 0))}${limit ? ` / ${short(limit)}` : ""}`;
}

function formatWindow(window?: { used_percent?: number; resets_at?: number }): string {
  if (window?.used_percent == null) return " —";
  const reset = window.resets_at
    ? ` · ${new Date(window.resets_at * 1000).toLocaleTimeString([], {
        hour: "numeric", minute: "2-digit",
      })}` : "";
  return ` ${window.used_percent}%${reset}`;
}

/** One message. A tool call is a single dim line — the name and what it was
 *  about — because that is what a terminal session shows and it is the only
 *  thing that makes a long silent turn readable. */
function Line({ msg }: { msg: ChatMsg }) {
  if (msg.role === "tool") {
    return (
      <div className="bg4-toolline">
        <Ti name="tool" size={11} />
        <b>{msg.tool}</b>
        <span>{msg.text}</span>
      </div>
    );
  }
  if (msg.role === "user") {
    return (
      <div className="bg4-msg you">
        <div className="who">you</div>
        <div className="txt">{msg.text}</div>
      </div>
    );
  }
  const bad = msg.role === "error";
  return (
    <div className={bad ? "bg4-msg dir bad" : "bg4-msg dir"}>
      <div className="who">director</div>
      {/* The director writes markdown; "you" stays verbatim because what a
          person typed should read back exactly as typed. */}
      <div className="txt"><Markdown text={msg.text} /></div>
    </div>
  );
}
