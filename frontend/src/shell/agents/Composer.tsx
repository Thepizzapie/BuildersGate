import { Button, Group, Menu, SegmentedControl, Text, Textarea } from "@mantine/core";
import { Ti } from "../Ti";
import { SEAT_COLOR, SEAT_ICON } from "../nav";

/** A running agent you can tag. */
export type Target = { item_id: number; seat: string; title: string };
/** Who the next message is aimed at:
 *    null      the director — it answers, then delegates. The default.
 *    number    one RUNNING agent, by item id: this steers a run mid-step.
 *    "all"     every agent running right now.
 *    {seat}    a seat, running or not: the work is filed for that seat
 *              directly instead of going through the director.
 *
 *  The seat form is why this is not just a list of live agents. Nothing running
 *  meant nothing to tag, so a menu called "tag an agent" offered one entry —
 *  the director — which is the thing you were trying not to go through. */
export type Aim = null | number | "all" | { seat: string };

/* THE COMPOSER IS ONE ELEMENT IN BOTH STATES.
 *
 * That is the whole reason the console reads as one screen rather than two: the
 * same box you typed into in the centre of an idle page slides to the foot of
 * the transcript when work is accepted, and keeps its dispatch/brainstorm tabs
 * on the way. Two separate composers — one for the empty state, one for the
 * live one — would be two things to keep in step and a visible cut between
 * them.
 *
 * `variant` changes the framing, never the controls: hero is the page's centre
 * of gravity, foot is a strip under a transcript.
 */
export function Composer({
  variant, mode, onMode, value, onValue, onSend, sending, autoDeploy, onClear,
  targets = [], seats = [], aim = null, onAim,
}: {
  variant: "hero" | "foot";
  mode: "dispatch" | "brainstorm";
  onMode: (m: "dispatch" | "brainstorm") => void;
  value: string;
  onValue: (v: string) => void;
  onSend: () => void;
  sending: boolean;
  autoDeploy?: boolean;
  onClear?: () => void;
  /** The agents running right now, tagging targets for this box. */
  targets?: Target[];
  /** Every seat on the project, tag-able whether or not one is running. */
  seats?: string[];
  aim?: Aim;
  onAim?: (aim: Aim) => void;
}) {
  const hero = variant === "hero";

  /* TAGGING — the box says WHO IT IS TALKING TO, and it changes what send does.
     Untagged, this reaches the director, which answers and delegates: new work.
     Tagged, it interrupts an agent that is ALREADY RUNNING. Typing
     "@narrative — do it this way instead" into an untagged box filed a second
     item against work in flight, and the director said so politely while the
     original run carried on doing the wrong thing. */
  const aimed = targets.find((t) => t.item_id === aim);
  const aimedSeat = aim && typeof aim === "object" ? aim.seat : "";
  const steering = aim === "all" || !!aimed;
  const addressing = !!aimedSeat;
  const tag = onAim && mode === "dispatch" ? (
    <Menu position="top-start" shadow="md" width={300}>
      <Menu.Target>
        <button className={steering ? "bg4-tag on" : "bg4-tag"} type="button">
          <Ti name="at" size={13} />
          {aim === "all" ? "every agent"
            : aimed ? `#${aimed.item_id} ${aimed.seat}`
            : aimedSeat || "tag a seat"}
        </button>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Interrupt something already running</Menu.Label>
        {targets.map((t) => (
          <Menu.Item key={t.item_id} onClick={() => onAim(t.item_id)}
                     leftSection={<Ti name={SEAT_ICON[t.seat] || "user"} size={14}
                                      color={SEAT_COLOR[t.seat]} />}>
            #{t.item_id} {t.seat}
            <span className="bg4-tag-sub">{t.title}</span>
          </Menu.Item>
        ))}
        {!targets.length && (
          <Menu.Item disabled>nothing is running — there is nobody to steer</Menu.Item>
        )}
        {/* STEER ALL, in the same menu as the one-agent tags, because it is the
            same act aimed wider: the art direction changed, the file everybody
            is about to touch is moving. Retyping that four times means the last
            agent hears it a minute after the first. */}
        <Menu.Divider />
        <Menu.Item onClick={() => onAim("all")} disabled={!targets.length}
                   leftSection={<Ti name="broadcast" size={14} />}
                   rightSection={targets.length
                     ? <span className="bg4-menun">{targets.length}</span> : undefined}>
          every agent running
          <span className="bg4-tag-sub">one sentence to all of them at once</span>
        </Menu.Item>
        {/* A SEAT, WHETHER OR NOT IT IS RUNNING. This files the work for that
            seat and dispatches it, instead of asking the director to decide
            whose job it is — which is the whole point of typing a seat's name
            in the first place. */}
        <Menu.Divider />
        <Menu.Label>Send it straight to a seat</Menu.Label>
        {seats.map((s) => (
          <Menu.Item key={s} onClick={() => onAim({ seat: s })}
                     leftSection={<Ti name={SEAT_ICON[s] || "user"} size={14}
                                      color={SEAT_COLOR[s]} />}>
            {s}
          </Menu.Item>
        ))}
        <Menu.Divider />
        <Menu.Item onClick={() => onAim(null)}
                   leftSection={<Ti name="user-star" size={14} />}>
          the director
          <span className="bg4-tag-sub">answers, then delegates — this files work</span>
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  ) : null;

  const aimNote = addressing ? (
    <div className="bg4-aimed">
      <Ti name="at" size={13} />
      <b>{aimedSeat}</b>
      <span>
        filed for that seat and dispatched to it, in your words — the director
        does not read it first.
      </span>
      <button className="x" onClick={() => onAim?.(null)}
              title="talk to the director instead">×</button>
    </div>
  ) : steering ? (
    <div className="bg4-aimed">
      <Ti name="at" size={13} />
      <b>{aim === "all" ? `all ${targets.length} running` : `#${aimed?.item_id} ${aimed?.seat}`}</b>
      <span>
        {aim === "all"
          ? "reads it when its current step ends. It reaches seats working on unrelated things — aim carefully."
          : "goes to that run mid-step. It corrects what is happening; it does not file anything."}
      </span>
      <button className="x" onClick={() => onAim?.(null)}
              title="talk to the director instead">×</button>
    </div>
  ) : null;
  const send = (
    <Button onClick={onSend} loading={sending} disabled={!value.trim()}
            size={hero ? "sm" : "xs"} className="bg4-sendbtn"
            /* The verb IS the difference. "send" next to a box aimed at a
               running agent hides that this interrupts rather than files. */
            color={steering ? "orange" : undefined}>
      {steering ? "steer" : addressing ? `send to ${aimedSeat}` : "send"}
    </Button>
  );

  const tabs = (
    <SegmentedControl size="xs" value={mode} className="bg4-modes"
                      onChange={(v) => onMode(v as "dispatch" | "brainstorm")}
                      data={[
                        { value: "dispatch",
                          label: <span><Ti name="send" size={12} /> dispatch</span> },
                        { value: "brainstorm",
                          label: <span><Ti name="bulb" size={12} /> brainstorm</span> },
                      ]} />
  );

  const field = (
    <Textarea autosize minRows={hero ? 3 : 1} maxRows={hero ? 10 : 6}
              variant="unstyled" value={value}
              onChange={(e) => onValue(e.currentTarget.value)}
              placeholder={mode === "brainstorm"
                ? "think out loud — nothing is filed until you press Deploy"
                : addressing
                ? `tell the ${aimedSeat} seat what to do — this files work for it`
                : steering
                ? (aim === "all"
                   ? "one correction, to every agent running right now"
                   : `correct #${aimed?.item_id} mid-run — it reads this between steps`)
                : "tell the director what you want — it answers, then delegates"}
              onKeyDown={(e) => {
                // Enter sends, Shift+Enter is a newline. A chat box that
                // occasionally takes a paragraph, not an editor.
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSend(); }
              }} />
  );

  if (hero) {
    return (
      <div className="bg4-composer-hero">
        {aimNote}
        {field}
        <Group gap="sm" wrap="nowrap" className="bg4-composer-bar">
          {tabs}
          {tag}
          <span style={{ flex: 1 }} />
          {/* AUTO-DEPLOY IS STATED, NOT ASSUMED. Queued work sitting still
              because a switch is off, with nothing on screen saying so, is the
              single most confusing state this product has. */}
          <Text size="xs" c="dimmed" ff="var(--mono)">
            auto-deploy {autoDeploy ? "on" : "off"}
          </Text>
          {send}
        </Group>
      </div>
    );
  }

  return (
    <div className="bg4-composer">
      {aimNote}
      <Group gap="xs" mb={8} wrap="nowrap">
        {tabs}
        {tag}
        <span style={{ flex: 1 }} />
        {onClear && (
          <Button variant="default" size="compact-xs" onClick={onClear}>clear</Button>
        )}
      </Group>
      <Group gap="xs" align="flex-end" wrap="nowrap">
        <div style={{ flex: 1, minWidth: 0 }}>{field}</div>
        {send}
      </Group>
    </div>
  );
}
