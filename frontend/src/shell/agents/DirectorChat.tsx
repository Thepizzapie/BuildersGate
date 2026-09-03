import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Group, ScrollArea, Textarea } from "@mantine/core";
import { Ti } from "../Ti";
import { Markdown } from "../../components/Markdown";
import { useEvents, FALLBACK_MS } from "../../hooks";
import { toast } from "../../bridge";
import { directorChat, directorNew, directorSay, type ChatMsg } from "./api";

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
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
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
  }, [msgs.length, running]);

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

  return (
    <div className="bg4-console-main">
      <ScrollArea className="bg4-transcript" type="auto" viewportRef={feed}>
        {msgs.length === 0 && !running && (
          <div className="bg4-console-quiet">
            <b>Director session</b>
            <span>
              A Claude Code session in this project, holding the builders-gate
              tools. Ask it something, or tell it what to build and it files the
              work for a seat.
            </span>
          </div>
        )}
        {msgs.map((m) => <Line key={m.n} msg={m} />)}
        {running && (
          <div className="bg4-msg dir live">
            <div className="who">director</div>
            <div className="txt thinking">working…</div>
          </div>
        )}
      </ScrollArea>

      <div className="bg4-composer">
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
