import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Checkbox, NumberInput, Select, Text, TextInput, MultiSelect, Badge,
  ScrollArea, Stack,
} from "@mantine/core";
import { Ti } from "../Ti";
import { mutate, readJSON, toast } from "../../bridge";
import { useEvents, useViewActive } from "../../hooks";
import { AgentFleet } from "./AgentFleet";
import { ProviderKeys } from "./ProviderKeys";
import { LocalGenerators } from "./LocalGenerators";
import { AgentClis } from "./AgentClis";
import { askConfirm } from "./confirm";
import { ThemeGrid } from "../ThemePicker";
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

type Field = {
  label?: string;
  key: string; group: string; kind: "bool" | "int" | "float" | "string" | "enum" | "list";
  choices: string[]; value: unknown; default: unknown; stored: unknown;
  source: "env" | "stored" | "default"; scope: string; help: string;
  min: number | null; max: number | null; env_vars: string[]; env: string;
  locked: boolean; env_override: string; human_only: boolean; guard: boolean;
  advanced: boolean;
};
type Group = { name: string; icon?: string; fields: Field[] };
type Described = { precedence: string; groups: Group[] };

const EMPTY: Described = { precedence: "", groups: [] };

/** Backend registry group that owns the provider and model defaults. */
const GENERATORS = "Generators";
const PROVIDERS = "Provider access";
const LOCAL_GENERATORS = "Local generators";
const AGENT_CLIS = "Agent CLIs";

/* THE FLEET IS A RAIL GROUP TOO, and it is not a setting at all - it is a live
   list of processes with a kill button on each. It belongs here because this is
   the screen that already holds every machine-scoped, human-only control (the
   only surface allowed to write an API key is three lines below), and because
   the board deliberately cannot show it: the board is one project and the whole
   point of this panel is the agents running against the OTHER ones. */
const FLEET = "Running agents";
const APPEARANCE = "Appearance";

/* Rail entries that are panels rather than settings registry groups. */
const PANELS: Record<string, string> = {
  [APPEARANCE]: "theme_auto",
  [PROVIDERS]: "key",
  [LOCAL_GENERATORS]: "device-desktop-cog",
  [AGENT_CLIS]: "plug-connected",
  [FLEET]: "robot",
};
const GROUP_LABELS: Record<string, string> = { [GENERATORS]: "Models" };

/* The registry groups are implementation boundaries. The rail is a human map:
   what runs work, what shapes the studio, what controls the app, and what is
   happening on this machine. Keep the backend vocabulary intact while giving
   it one level of information architecture. */
const SECTIONS = [
  { name: "Work", groups: ["Dispatch", "Gates", "Follow-up", "Limits"] },
  { name: "Studio", groups: ["Art", GENERATORS, "Modules"] },
  { name: "Connections", groups: [PROVIDERS, LOCAL_GENERATORS, AGENT_CLIS] },
  { name: "App", groups: [APPEARANCE, "Console", "Notifications", "Community", "Privacy"] },
  { name: "Machine", groups: [FLEET] },
];

const GROUP_NOTES: Record<string, string> = {
  Dispatch: "How work starts, which runner takes it, and how many can run together.",
  Gates: "The evidence and sign-off rules work must pass before it counts as done.",
  "Follow-up": "What happens after work finishes or fails.",
  Limits: "Runtime and concurrency ceilings for dispatched work. There is no money ceiling: the only budget is your provider account's balance.",
  Art: "Style training, art routing, and approval behavior.",
  Generators: "Choose the default provider and model for each kind of generation.",
  "Provider access": "Connect hosted generation services and inspect their available capabilities.",
  "Local generators": "Set up and control generation runtimes that stay on this machine.",
  "Agent CLIs": "Connect coding-agent command lines to this Builders Gate installation.",
  Modules: "Choose which parts of the studio this project uses.",
  Console: "Refresh cadence and model limits for interactive sessions.",
  Notifications: "Which events reach you, where they go, and when they stay quiet.",
  Community: "How viewer chat becomes feedback during sessions and playtests.",
  Privacy: "Machine-wide controls for what can appear on stream.",
  Appearance: "Choose the studio’s visual ground. Changes apply immediately across every workspace.",
  "Running agents": "Processes running on this machine, including other projects.",
};

const show = (v: unknown): string =>
  Array.isArray(v) ? (v.length ? v.join(", ") : "none")
  : typeof v === "boolean" ? (v ? "on" : "off")
  : v === "" || v == null ? "-" : String(v);

const sameValue = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const settingId = (key: string) => `setting-${key.replace(/[^a-z0-9_-]/gi, "-")}`;

function settingHref(key: string): string {
  const url = new URL(window.location.href);
  url.searchParams.set("setting", key);
  return `${url.pathname}${url.search}${url.hash}`;
}

function highlighted(text: string, needle: string): ReactNode {
  const q = needle.trim();
  if (!q) return text;
  const parts = text.split(new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig"));
  return parts.map((part, i) => part.toLowerCase() === q.toLowerCase()
    ? <mark key={i}>{part}</mark> : part);
}

export function Settings() {
  const host = useRef<HTMLDivElement>(null);
  const active = useViewActive(host);
  const [desc, setDesc] = useState<Described>(EMPTY);
  const [q, setQ] = useState("");
  const [onlyChanged, setOnlyChanged] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [saveState, setSaveState] = useState("");
  const [linkedKey, setLinkedKey] = useState(() => new URLSearchParams(location.search).get("setting") || "");
  /* Render one destination at a time so each section keeps a clear scope. */
  const [group, setGroup] = useState<string>("Dispatch");

  const refresh = useCallback(async () => {
    setDesc(await readJSON<Described>("/api/settings", EMPTY));
  }, []);

  /* Connection panels own their own reads and writes; registry settings stay
     isolated in this component. */
  // Settings do not move on their own; one read on arrival is the whole need.
  useEvents(refresh, { enabled: active, kinds: ["settings.*", "gate.*"], fallbackMs: 60000 });

  /* The PATCH answers with the whole description again, so a save that an env
     var overrode - or that another field's range clamped - comes back stated
     rather than guessed at. That reply is the new state; nothing is applied
     optimistically. */
  async function save(key: string, value: unknown) {
    setSaveState("Saving…");
    const r = await mutate<Described>("/api/settings", {
      method: "PATCH", body: { [key]: value }, quiet: true,
    });
    if (r.ok && r.data) { setDesc(r.data); setSaveState("Saved"); }
    else if (!r.ok) { setSaveState("Save failed"); refresh(); }
  }

  useEffect(() => {
    if (!linkedKey || !desc.groups.length) return;
    const field = desc.groups.flatMap((g) => g.fields).find((f) => f.key === linkedKey);
    if (!field) return;
    setGroup(field.group);
    if (field.advanced) setShowAdvanced(true);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      document.getElementById(settingId(linkedKey))?.scrollIntoView({ block: "center" });
    }));
  }, [desc.groups, linkedKey]);

  function selectGroup(name: string) {
    setGroup(name); setQ(""); setOnlyChanged(false); setSaveState(""); setLinkedKey("");
    const url = new URL(window.location.href);
    if (url.searchParams.has("setting")) {
      url.searchParams.delete("setting");
      window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }
  }

  async function resetSection(fields: Field[]) {
    const changedFields = fields.filter((f) => f.source !== "env" && !sameValue(f.value, f.default));
    if (!changedFields.length) return;
    const yes = await askConfirm({
      title: `Restore ${changedFields.length} setting${changedFields.length === 1 ? "" : "s"}?`,
      body: "This restores the default values for this section. Environment-controlled settings stay unchanged.",
      ok: "restore defaults", cancel: "keep values",
    });
    if (!yes) return;
    setSaveState("Saving…");
    const body = Object.fromEntries(changedFields.map((f) => [f.key, f.default]));
    const r = await mutate<Described>("/api/settings", { method: "PATCH", body, quiet: true });
    if (r.ok && r.data) { setDesc(r.data); setSaveState("Defaults restored"); toast("defaults restored", "ok"); }
    else { setSaveState("Reset failed"); toast(r.error || "defaults could not be restored", "bad"); refresh(); }
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
    return desc.groups.map((g) => {
      const groupHit = g.name.toLowerCase().includes(needle)
        || (GROUP_LABELS[g.name] || "").toLowerCase().includes(needle);
      return {
        ...g,
        fields: g.fields.filter((f) =>
          (!onlyChanged || !sameValue(f.value, f.default))
          && (!needle || groupHit
              || (f.label || "").toLowerCase().includes(needle)
              || f.key.toLowerCase().includes(needle)
              || f.help.toLowerCase().includes(needle)
              || f.env_vars.some((v) => v.toLowerCase().includes(needle)))),
      };
    });
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
    .filter((f) => !sameValue(f.value, f.default)).length;

  const orderedNames = SECTIONS.flatMap((s) => s.groups);
  const looseNames = [
    ...groups.map((g) => g.name),
    ...Object.keys(PANELS).filter((p) => !groups.some((g) => g.name === p)),
  ].filter((name) => !orderedNames.includes(name));
  const navSections = looseNames.length
    ? [...SECTIONS, { name: "Other", groups: looseNames }]
    : SECTIONS;
  const selected = groups.find((g) => g.name === group);
  const selectedRaw = desc.groups.find((g) => g.name === group);
  const selectedFields = (selected?.fields || []).filter((f) => showAdvanced || !f.advanced);
  const advancedCount = (selectedRaw?.fields || []).filter((f) => f.advanced).length;
  const resetCount = (selectedRaw?.fields || [])
    .filter((f) => f.source !== "env" && !sameValue(f.value, f.default)).length;
  const resultGroups = filtering ? groups.filter((g) => g.fields.length) : [];
  const resultCount = resultGroups.reduce((n, g) => n + g.fields.length, 0);
  const panelResults = filtering && !onlyChanged
    ? Object.keys(PANELS).filter(panelHit)
    : [];

  return (
    <div className="bg4-settings" ref={host}>
      <div className="bg4-settings-bar">
        <div className="bg4-settings-search">
          <Ti name="search" size={15} />
          <input placeholder="Find a setting" aria-label="Find a setting"
                 value={q} onChange={(e) => setQ(e.currentTarget.value)} />
          {q && <button type="button" className="clear" aria-label="Clear search"
                        onClick={() => setQ("")}><Ti name="x" size={13} /></button>}
        </div>
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
          Changed <span>{changed}</span>
        </button>
        <div className="bg4-settings-legend" title={desc.precedence}>
          <span><i className="env" /> environment</span>
          <span><i className="stored" /> project</span>
          <span><i /> default</span>
        </div>
      </div>

      <div className="bg4-settings-split">
        <nav className="bg4-settings-rail" aria-label="Settings sections">
          {/* AN ICON PER CATEGORY. Ten identical text rows are ten things you
              have to read; a glyph is the thing you actually navigate by once
              you have been here twice. The name comes from the registry
              (settings.GROUP_ICONS) so a new group cannot arrive iconless. */}
          {navSections.map((section) => (
            <div className="bg4-settings-navgroup" key={section.name}>
              <div className="bg4-settings-navlabel">{section.name}</div>
              {section.groups.map((name) => {
                const g = groups.find((x) => x.name === name);
                if (!g && !PANELS[name]) return null;
                const hits = (g?.fields.length ?? 0)
                  + (PANELS[name] && panelHit(name) ? 1 : 0);
                const cls = [name === group && !filtering ? "on" : "",
                             filtering && !hits ? "empty" : ""].filter(Boolean).join(" ");
                return (
                  <button key={name} className={cls} onClick={() => selectGroup(name)}>
                    <Ti name={PANELS[name] || g?.icon || "adjustments"} size={15} />
                    <span className="l">{GROUP_LABELS[name] || name}</span>
                    {!PANELS[name] && <span className="n">{g?.fields.length ?? 0}</span>}
                  </button>
                );
              })}
            </div>
          ))}
        </nav>

        <ScrollArea className="bg4-settings-body" type="auto">
          {filtering ? (
            <section className="bg4-settings-results">
              <CategoryHead name={onlyChanged && !q ? "Changed settings" : "Search results"}
                            note={`${resultCount} setting${resultCount === 1 ? "" : "s"} found`} />
              {!!panelResults.length && (
                <div className="bg4-settings-destinations">
                  {panelResults.map((name) => (
                    <button type="button" key={name} onClick={() => selectGroup(name)}>
                      <Ti name={PANELS[name]} size={15} />
                      <span><b>{name}</b><small>{GROUP_NOTES[name]}</small></span>
                      <Ti name="chevron-right" size={13} />
                    </button>
                  ))}
                </div>
              )}
              {resultGroups.map((g) => (
                <div className="bg4-settings-resultgroup" key={g.name}>
                  <button type="button" onClick={() => selectGroup(g.name)}>
                    <Ti name={g.icon || "adjustments"} size={13} />
                    {GROUP_LABELS[g.name] || g.name}<span>{g.fields.length}</span>
                  </button>
                  <Stack gap={0}>{g.fields.map((f) => <Row key={f.key} f={f} onSave={save} needle={q} linked={f.key === linkedKey} />)}</Stack>
                </div>
              ))}
              {!resultCount && !panelResults.length && (
                <Text size="sm" c="dimmed" ta="center" py="xl">No settings match.</Text>
              )}
            </section>
          ) : group === APPEARANCE ? (
            <section className="bg4-settings-group bg4-appearance">
              <CategoryHead name={APPEARANCE} note={GROUP_NOTES[APPEARANCE]} />
              <ThemeGrid />
            </section>
          ) : group === FLEET ? (
            /* `active` is the DECK's activity, not the rail's: the fleet polls,
               and a poller left running behind a screen the user navigated away
               from is the thing usePoll's enabled flag exists to stop. The rail
               selection is already accounted for by not rendering this at all. */
            <section className="bg4-settings-group">
              <CategoryHead name={FLEET} note={GROUP_NOTES[FLEET]} />
              <AgentFleet active={active} />
            </section>
          ) : group === PROVIDERS ? (
            <section className="bg4-settings-group">
              <CategoryHead name={PROVIDERS} note={GROUP_NOTES[PROVIDERS]} />
              <ProviderKeys active={active} />
            </section>
          ) : group === LOCAL_GENERATORS ? (
            <section className="bg4-settings-group">
              <CategoryHead name={LOCAL_GENERATORS} note={GROUP_NOTES[LOCAL_GENERATORS]} />
              <LocalGenerators active={active} />
            </section>
          ) : group === AGENT_CLIS ? (
            <section className="bg4-settings-group">
              <CategoryHead name={AGENT_CLIS} note={GROUP_NOTES[AGENT_CLIS]} />
              <AgentClis active={active} />
            </section>
          ) : group === GENERATORS ? (
            <section className="bg4-settings-group">
              <CategoryHead name={GROUP_LABELS[GENERATORS]} note={GROUP_NOTES[GENERATORS]}
                            count={selectedRaw?.fields.length}
                            actions={<SectionTools advancedCount={advancedCount}
                              showAdvanced={showAdvanced} onAdvanced={setShowAdvanced}
                              resetCount={resetCount} saveState={saveState}
                              onReset={() => resetSection(selectedRaw?.fields || [])} />} />
              {/* The registry half first: the provider and model pickers,
                  which live beside the keys they depend on. */}
              <Stack gap={0}>
                {selectedFields
                  .map((f) => <Row key={f.key} f={f} onSave={save} linked={f.key === linkedKey} />)}
              </Stack>
            </section>
          ) : (
            groups.filter((g) => g.name === group).map((g) => (
              <section key={g.name} className="bg4-settings-group">
                <CategoryHead name={g.name} note={GROUP_NOTES[g.name] || "Project settings."}
                              count={selectedRaw?.fields.length}
                              actions={<SectionTools advancedCount={advancedCount}
                                showAdvanced={showAdvanced} onAdvanced={setShowAdvanced}
                                resetCount={resetCount} saveState={saveState}
                                onReset={() => resetSection(selectedRaw?.fields || [])} />} />
                <Stack gap={0}>
                  {selectedFields.map((f) => <Row key={f.key} f={f} onSave={save} linked={f.key === linkedKey} />)}
                </Stack>
                {!selectedFields.length && advancedCount > 0 && (
                  <Text size="sm" c="dimmed" ta="center" py="xl">
                    This section only contains advanced settings.
                  </Text>
                )}
              </section>
            ))
          )}
          {/* Only for the SETTINGS groups. A panel that legitimately has no
              rows of its own - the fleet with nothing running - was being told
              "nothing matches that" under a filter box it does not use. */}
          {!filtering && !selected && !PANELS[group] && (
            <Text size="xs" c="dimmed" ta="center" py="xl">nothing matches that</Text>
          )}
        </ScrollArea>
      </div>
    </div>
  );
}

function CategoryHead({ name, note, count, actions }: {
  name: string; note: string; count?: number; actions?: ReactNode;
}) {
  return (
    <header className="bg4-settings-category">
      <div>
        <h2>{name}</h2>
        <p>{note}</p>
      </div>
      <div className="bg4-settings-category-side">
        {count != null && <span>{count} setting{count === 1 ? "" : "s"}</span>}
        {actions}
      </div>
    </header>
  );
}

function SectionTools({ advancedCount, showAdvanced, onAdvanced, resetCount, saveState, onReset }: {
  advancedCount: number; showAdvanced: boolean; onAdvanced: (value: boolean) => void;
  resetCount: number; saveState: string; onReset: () => void;
}) {
  return (
    <div className="bg4-settings-tools">
      {saveState && (
        <span className={saveState.includes("failed") ? "save-state bad" : "save-state"}>
          <Ti name={saveState === "Saving…" ? "loader-2" : saveState.includes("failed") ? "alert-circle" : "check"} size={12} />
          {saveState}
        </span>
      )}
      {!!advancedCount && (
        <button type="button" className={showAdvanced ? "on" : ""}
                aria-pressed={showAdvanced} onClick={() => onAdvanced(!showAdvanced)}>
          <Ti name="adjustments-horizontal" size={13} />
          {showAdvanced ? "Hide advanced" : `Show advanced (${advancedCount})`}
        </button>
      )}
      {!!resetCount && (
        <button type="button" className="reset" onClick={onReset}>
          <Ti name="restore" size={13} />Reset defaults
        </button>
      )}
    </div>
  );
}

function Row({ f, onSave, needle = "", linked = false }: {
  f: Field; onSave: (k: string, v: unknown) => void; needle?: string; linked?: boolean;
}) {
  const locked = f.locked || !!f.env_override;
  return (
    <div id={settingId(f.key)} className={`bg4-set ${f.source}${locked ? " locked" : ""}${linked ? " linked" : ""}`}>
      <div className="copy">
        {/* THE NAME, NOT THE IDENTIFIER. This row used to be titled
            `dispatch.allow_dirty`, which tells a reader who wrote the code
            exactly what it does and tells everybody else nothing. The key is
            still here, under the name - it is what you search for and what an
            env override is called - it is just no longer the heading. */}
        <div className="head">
          <span className="label">{highlighted(f.label || f.key, needle)}</span>
          {f.scope === "machine" && <Badge size="xs" variant="default">machine</Badge>}
          {f.guard && (
            <Badge size="xs" variant="light" color="yellow"
                   leftSection={<Ti name="lock" size={10} />}>guarded</Badge>
          )}
          {f.advanced && <Badge size="xs" variant="default">advanced</Badge>}
          <a className="bg4-setting-link" href={settingHref(f.key)}
             aria-label={`Link to ${f.label || f.key}`} title="Link to this setting">
            <Ti name="link" size={12} />
          </a>
        </div>
        <code className="key">{highlighted(f.key, needle)}</code>
        <p className="help">{highlighted(f.help, needle)}</p>
        <div className="foot">
          <span>Default <b>{show(f.default)}</b></span>
          {f.source !== "default" && <span className="drift">Current <b>{show(f.value)}</b></span>}
          {locked && (
            <span className="env">
              Forced by {f.env_vars.join(", ") || "the environment"}
              {f.env_override ? ` = ${f.env_override}` : ""}
            </span>
          )}
        </div>
      </div>
      <div className="control">{control(f, locked, onSave)}</div>
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
