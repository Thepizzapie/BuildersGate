import { useCallback, useMemo, useRef, useState } from "react";
import {
  Checkbox, Group, NumberInput, Select, Text, TextInput, MultiSelect, Badge,
  ScrollArea, Stack,
} from "@mantine/core";
import { Ti } from "../Ti";
import { mutate, readJSON } from "../../bridge";
import { usePoll, useViewActive } from "../../hooks";
import { useEffect } from "react";
import { AgentFleet } from "./AgentFleet";
import "./settings.css";

/* 7b · Settings as a FORM.
 *
 * The writing here is the best in the app - every setting says what it does and
 * why the default is the default. The old layout spent it badly: 42 rows all
 * equally loud, each one a card holding a key, two or three badges, a
 * paragraph, a default line and two links, which put the control about 900px
 * from its own label. The ten settings you changed hid among the thirty-two you
 * did not.
 *
 * Three changes, and every word is kept:
 *
 *  1. A ROW IS A LABEL AND ITS OWN CONTROL, side by side. No card chrome.
 *  2. PRECEDENCE IS A LEFT-EDGE STRIPE, not a chip. env > stored > default is
 *     the page's headline idea, and as a small badge per row it could only be
 *     read one row at a time. As a stripe it is legible down the whole page
 *     without reading a word: which layer won, forty rows at a glance.
 *  3. THE DEFAULT SITS BESIDE THE VALUE, so drift is visible rather than
 *     recalled.
 *
 * Env-forced rows are locked and greyed, because a control that silently does
 * nothing is worse than one that says why it cannot.
 */

declare global {
  interface Window {
    ProviderKeys?: { activate?(host?: HTMLElement | null): Promise<boolean> | boolean };
    LocalSetup?: {
      activate?(host?: HTMLElement | null): Promise<boolean> | boolean;
      activateAgents?(host?: HTMLElement | null): Promise<boolean> | boolean;
    };
  }
}

type Field = {
  label?: string;
  key: string; group: string; kind: "bool" | "int" | "float" | "string" | "enum" | "list";
  choices: string[]; value: unknown; default: unknown; stored: unknown;
  source: "env" | "stored" | "default"; scope: string; help: string;
  min: number | null; max: number | null; env_vars: string[]; env: string;
  locked: boolean; env_override: string; human_only: boolean; guard: boolean;
};
type Group = { name: string; icon?: string; fields: Field[] };
type Described = { precedence: string; groups: Group[] };

const EMPTY: Described = { precedence: "", groups: [] };

/** The generator panels are a group in the rail like any other. */
const GENERATORS = "Generators";

/* THE FLEET IS A RAIL GROUP TOO, and it is not a setting at all - it is a live
   list of processes with a kill button on each. It belongs here because this is
   the screen that already holds every machine-scoped, human-only control (the
   only surface allowed to write an API key is three lines below), and because
   the board deliberately cannot show it: the board is one project and the whole
   point of this panel is the agents running against the OTHER ones. */
const FLEET = "Running agents";

/* Rail entries that are panels rather than settings groups. Listed once so the
   rail, the icon lookup and the count suppression cannot disagree - they did,
   and Generators rendered a "0" beside itself for a while. */
const PANELS: Record<string, string> = { [GENERATORS]: "sparkles", [FLEET]: "robot" };

const show = (v: unknown): string =>
  Array.isArray(v) ? (v.length ? v.join(", ") : "none")
  : typeof v === "boolean" ? (v ? "on" : "off")
  : v === "" || v == null ? "-" : String(v);

export function Settings() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [desc, setDesc] = useState<Described>(EMPTY);
  const [q, setQ] = useState("");
  const [onlyChanged, setOnlyChanged] = useState(false);
  /* ONE GROUP AT A TIME. Every row rendered at once is 9,000px of page, and
     the three Generator panels - which are whole classic UIs, ~2,500px between
     them - were sitting at the TOP of it, so opening Settings showed a wall of
     provider cards and pushed the first actual setting three screens down.
     The reference has a group rail for exactly this reason. */
  const [group, setGroup] = useState<string>("Dispatch");

  const refresh = useCallback(async () => {
    setDesc(await readJSON<Described>("/api/settings", EMPTY));
  }, []);

  /* THE TWO PANELS THIS SCREEN HOSTS BUT DOES NOT OWN.
     providerkeys.js is the ONLY surface in the product that may write an API
     key - deliberately not an MCP tool, because an agent that can write
     credentials can hand itself a provider nobody paid for - and localsetup.js
     is the local-generation half of the same question. Both mount themselves
     into an id and both used to ride SettingsView.activate(), which this screen
     replaced. React renders their hosts EMPTY and never diffs what they put
     inside, then asks each one to paint. Same contract as #asset-lib-root. */
  useEffect(() => {
    if (!active || group !== GENERATORS) return;
    window.ProviderKeys?.activate?.();
    window.LocalSetup?.activate?.();
    /* THREE PANELS, NOT TWO. localsetup.js has a second surface - the Agent
       CLIs pane, which reports whether the coding-agent binaries this product
       spawns are actually installed - and its own header says it lives at
       #ag-host "and NOWHERE ELSE". It used to be painted by a wrapper around
       SettingsView.activate(); that wrapper still exists, still checks for
       window.SettingsView, and has silently bailed since the day this screen
       replaced it. */
    window.LocalSetup?.activateAgents?.();
  }, [active, group]);
  // Settings do not move on their own; one read on arrival is the whole need.
  usePoll(refresh, 60000, active);

  /* The PATCH answers with the whole description again, so a save that an env
     var overrode - or that another field's range clamped - comes back stated
     rather than guessed at. That reply is the new state; nothing is applied
     optimistically. */
  async function save(key: string, value: unknown) {
    const r = await mutate<Described>("/api/settings", {
      method: "PATCH", body: { [key]: value }, quiet: true,
    });
    if (r.ok && r.data) setDesc(r.data);
    else if (!r.ok) refresh();
  }

  /* FILTERS NARROW WHAT IS IN A CATEGORY. THEY DO NOT REMOVE THE CATEGORY.
     `.filter((g) => g.fields.length)` used to drop any group with no surviving
     field, and the rail is built from this list - so pressing `changed` deleted
     every untouched category out of the NAVIGATION. Community has nothing
     changed in it by default, so the one control that says "show me what I
     touched" was also the control that made Community unreachable, including
     while you were standing on it. A field-level filter had been given
     authority over the map. */
  const groups = useMemo(() => {
    const needle = q.toLowerCase().trim();
    return desc.groups.map((g) => ({
      ...g,
      fields: g.fields.filter((f) =>
        (!onlyChanged || f.source !== "default")
        && (!needle
            || f.key.toLowerCase().includes(needle)
            || f.help.toLowerCase().includes(needle))),
    }));
  }, [desc, q, onlyChanged]);

  /* Whether a filter is doing anything, and therefore whether the rail should
     say where the matches are. With no filter every count is just "how many
     settings live here", which is noise. */
  const filtering = !!q.trim() || onlyChanged;

  /* PANELS MATCH BY NAME, because they have no fields to match by. Typing
     "agent f" left both panels sitting there looking like results, since they
     were appended to the rail outside the filter entirely. They stay reachable
     - nothing here removes a destination - but they now dim like everything
     else that does not match, so the rail stops lying about what it found. */
  const panelHit = (name: string) =>
    !filtering || (!onlyChanged && !!q.trim()
                   && name.toLowerCase().includes(q.toLowerCase().trim()));

  const changed = desc.groups.flatMap((g) => g.fields)
    .filter((f) => f.source !== "default").length;

  return (
    <div className="bg4-settings" ref={host}>
      <Group className="bg4-settings-bar" gap="sm" wrap="nowrap">
        <TextInput size="xs" placeholder="filter key or help text" style={{ flex: 1 }}
                   value={q} onChange={(e) => setQ(e.currentTarget.value)}
                   leftSection={<Ti name="search" size={13} />} />
        {/* IT IS A TOGGLE, AND IT READ AS A STATISTIC. "changed 17" next to a
            search box looks like a count of something, so the one control on
            this bar that filters was the one nobody could tell was a control -
            the first question it got was "what does changed 17 even mean".
            Saying "only" makes it a verb, the checkbox glyph makes it a switch,
            and the title says what changed means, which is not obvious either:
            not on its built-in default, i.e. stored for this project. */}
        <button className={onlyChanged ? "bg4-filterchip on" : "bg4-filterchip"}
                aria-pressed={onlyChanged}
                title={`Show only the ${changed} settings that are not on their `
                       + "built-in default (stored for this project, or set in "
                       + "the environment)"}
                onClick={() => setOnlyChanged((v) => !v)}>
          <Ti name={onlyChanged ? "square-check" : "square"} size={12} />
          only changed ({changed})
        </button>
        {/* The precedence rule, said once, at the top of the thing it governs - and then shown as the stripe on every row below it. */}
        <Text size="xs" c="dimmed" ff="var(--mono)">{desc.precedence}</Text>
      </Group>

      <div className="bg4-settings-split">
        <nav className="bg4-settings-rail">
          {/* AN ICON PER CATEGORY. Ten identical text rows are ten things you
              have to read; a glyph is the thing you actually navigate by once
              you have been here twice. The name comes from the registry
              (settings.GROUP_ICONS) so a new group cannot arrive iconless. */}
          {[...groups.map((g) => g.name), ...Object.keys(PANELS)].map((name) => {
            const g = groups.find((x) => x.name === name);
            const hits = PANELS[name] ? (panelHit(name) ? 1 : 0) : (g?.fields.length ?? 0);
            /* Dimmed, never hidden. A destination that disappears while you are
               reading it is worse than one that says "nothing in here matches",
               and the count is how you find the group your search DID land in
               without clicking through ten of them. */
            const cls = [name === group ? "on" : "",
                         filtering && !hits ? "empty" : ""].filter(Boolean).join(" ");
            return (
              <button key={name} className={cls} onClick={() => setGroup(name)}>
                <Ti name={PANELS[name] || g?.icon || "adjustments"} size={15} />
                <span className="l">{name}</span>
                {!PANELS[name] && (
                  <span className="n">{g?.fields.length ?? 0}</span>
                )}
              </button>
            );
          })}
        </nav>

        <ScrollArea className="bg4-settings-body" type="auto">
          {group === FLEET ? (
            /* `active` is the DECK's activity, not the rail's: the fleet polls,
               and a poller left running behind a screen the user navigated away
               from is the thing usePoll's enabled flag exists to stop. The rail
               selection is already accounted for by not rendering this at all. */
            <AgentFleet active={active} />
          ) : group === GENERATORS ? (
            <section className="bg4-settings-group">
              {/* THREE CLASSIC PANELS, HOSTED NOT REBUILT. providerkeys.js is
                  the only surface in the product that may write an API key - deliberately not an MCP tool - and localsetup.js owns both the
                  local-generation and the agent-CLI panes. React renders the
                  hosts empty and never diffs what they put inside. */}
              <div id="pv-host" />
              <div id="lc-host" />
              <div id="ag-host" />
            </section>
          ) : (
            groups.filter((g) => g.name === group || q).map((g) => (
              <section key={g.name} className="bg4-settings-group">
                {q && (
                  <div className="bg4-settings-head">
                    <Ti name={g.icon || "adjustments"} size={13} />
                    <span>{g.name}</span><span className="n">{g.fields.length}</span>
                  </div>
                )}
                <Stack gap={0}>
                  {g.fields.map((f) => <Row key={f.key} f={f} onSave={save} />)}
                </Stack>
              </section>
            ))
          )}
          {/* Only for the SETTINGS groups. A panel that legitimately has no
              rows of its own - the fleet with nothing running - was being told
              "nothing matches that" under a filter box it does not use. */}
          {!groups.length && !PANELS[group] && (
            <Text size="xs" c="dimmed" ta="center" py="xl">nothing matches that</Text>
          )}
        </ScrollArea>
      </div>
    </div>
  );
}

function Row({ f, onSave }: { f: Field; onSave: (k: string, v: unknown) => void }) {
  const locked = f.locked || !!f.env_override;
  return (
    <div className={`bg4-set ${f.source}${locked ? " locked" : ""}`}>
      <div className="head">
        {/* THE NAME, NOT THE IDENTIFIER. This row used to be titled
            `dispatch.allow_dirty`, which tells a reader who wrote the code
            exactly what it does and tells everybody else nothing. The key is
            still here, under the name - it is what you search for and what an
            env override is called - it is just no longer the heading. */}
        <span className="label">{f.label || f.key}</span>
        {f.scope === "machine" && <Badge size="xs" variant="default">machine</Badge>}
        {f.guard && (
          <Badge size="xs" variant="light" color="yellow"
                 leftSection={<Ti name="lock" size={10} />}>guarded</Badge>
        )}
        <span className="spacer" />
        <div className="control">{control(f, locked, onSave)}</div>
      </div>
      <code className="key">{f.key}</code>
      {/* EVERY WORD KEPT. The help is the reason this page is worth reading;
          what changed is that it no longer sits between a label and its own
          control. */}
      <p className="help">{f.help}</p>
      <div className="foot">
        <span>default <b>{show(f.default)}</b></span>
        {f.source !== "default" && <span className="drift">now <b>{show(f.value)}</b></span>}
        {locked && (
          <span className="env">
            forced by {f.env_vars.join(", ") || "the environment"}
            {f.env_override ? ` = ${f.env_override}` : ""}
          </span>
        )}
      </div>
    </div>
  );
}

function control(f: Field, locked: boolean, save: (k: string, v: unknown) => void) {
  const common = { size: "xs" as const, disabled: locked };
  switch (f.kind) {
    case "bool":
      return <Checkbox {...common} checked={!!f.value}
                       onChange={(e) => save(f.key, e.currentTarget.checked)} />;
    case "int":
    case "float":
      return <NumberInput {...common} value={Number(f.value)} w={110}
                          min={f.min ?? undefined} max={f.max ?? undefined}
                          step={f.kind === "float" ? 0.05 : 1}
                          decimalScale={f.kind === "float" ? 2 : 0}
                          onBlur={(e) => {
                            const n = Number(e.currentTarget.value);
                            if (Number.isFinite(n) && n !== f.value) save(f.key, n);
                          }} />;
    case "enum":
      return <Select {...common} data={f.choices} value={String(f.value)} w={150}
                     allowDeselect={false}
                     onChange={(v) => v != null && save(f.key, v)} />;
    case "list":
      return <MultiSelect {...common} data={f.choices} w={260} searchable
                          value={(f.value as string[]) || []}
                          onChange={(v) => save(f.key, v)} />;
    default:
      // A string field that ships choices is a MODEL PICKER: the backend
      // filled them from the live model catalog (configured providers only).
      // Searchable because the lists run long; clearable because "" means
      // "the provider's own default" for every one of these.
      if (f.choices && f.choices.length)
        return <Select {...common} data={f.choices} value={String(f.value ?? "")}
                       w={220} searchable clearable
                       placeholder="provider default"
                       onChange={(v) => save(f.key, v ?? "")} />;
      return <TextInput {...common} defaultValue={String(f.value ?? "")} w={220}
                        onBlur={(e) => {
                          if (e.currentTarget.value !== String(f.value ?? ""))
                            save(f.key, e.currentTarget.value);
                        }} />;
  }
}
