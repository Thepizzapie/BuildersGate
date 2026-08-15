/* HOW THIS SEAT LOOKS ON THE STUDIO FLOOR.
 *
 * The floor draws a room per seat, and until now every visual fact about that
 * room was keyed to the seat's NAME inside the renderer: which character walks
 * around it, what the floor is made of, the word under the nameplate. This is
 * the panel that makes those facts data - the seat table stores them, the floor
 * reads them, and a project can move any of it without touching code.
 *
 * IT IS DELIBERATELY THE ONLY PART OF THE SEAT TABLE THIS FORM CAN WRITE. The
 * rest of seat_config is permissions: `write_globs` is the lane the seat may
 * write in, `enabled` decides whether its QA runs at all, and the MCP tool
 * refuses both when an agent asks, because a seat that can widen its own lanes
 * has no lanes. None of that applies to a carpet. `mission` is left out for a
 * different reason - it is the brief the agent is actually given, and editing
 * it from a panel about decoration would be a text box that quietly changes
 * what an agent does.
 *
 * THE CHOICES COME FROM THE SERVER, not from a list typed in here. A dropdown
 * built from a local array is a second copy of the tuple in the route, and the
 * first time a cast is added it is wrong in one of the two places.
 *
 * AUTOFILL IS TURNED OFF ON EVERY FREE-TEXT FIELD, and that is a bug fix
 * rather than tidiness. These are one-word boxes with generic labels, so a
 * browser offers saved values from unrelated forms - and because each control
 * saves on blur, accepting one silently renames a seat. Observed: a director's
 * nameplate became "Colitis" from a suggestion nobody typed.
 *
 * EVERY CONTROL SAVES ON ITS OWN. The endpoint merges key by key, so there is
 * no save button and no way to lose the other two fields by changing one - and
 * clearing a field resets it to the code default rather than storing an empty
 * value that would draw a blank nameplate.
 */
import { useEffect, useState } from "react";
import { mutate, readJSON, toast } from "../../bridge";
import { Ti } from "../Ti";
import { Head } from "./prims";

type Persona = {
  style?: string; name?: string; lines?: string[];
  cast?: string; surface?: string; vibe?: string;
};
type Choices = {
  cast: string[]; surface: string[];
  vibe_max: number; name_max: number; style_max: number;
  line_max: number; lines_max: number;
};

/* What each surface is for, in the language the plan used. The floor's
   materials are not decoration: a studio does not carpet its server room, and
   saying so is what makes the picker a decision rather than a colour swatch. */
const SURFACE_NOTE: Record<string, string> = {
  carpet: "soft and warm — writers' rooms, edit bays",
  tile: "hard and institutional",
  wood: "boards — the corner office, the lounge",
  vinyl: "wipe-clean, because paint",
  concrete: "raised floor over cable trays",
};

export function Persona({ seat, active }: { seat: string; active: boolean }) {
  const [persona, setPersona] = useState<Persona>({});
  const [choices, setChoices] = useState<Choices | null>(null);
  const [busy, setBusy] = useState("");

  /* READ ON OPEN, NOT ON A TIMER. A persona changes when somebody in this panel
     changes it; polling it would be a request every few seconds for a value
     only this form writes. */
  useEffect(() => {
    if (!active) return;
    let live = true;
    readJSON<{ seats?: { role: string; persona: Persona }[]; choices?: Choices }>(
      "/api/seats/persona", {}).then((d) => {
        if (!live) return;
        setChoices(d.choices || null);
        const row = (d.seats || []).find((s) => s.role === seat);
        setPersona(row?.persona || {});
      });
    return () => { live = false; };
  }, [seat, active]);

  async function save(patch: Persona, label: string) {
    setBusy(label);
    /* OPTIMISTIC, because the control is a dropdown and a select that snaps
       back for 200ms reads as broken. The server's answer replaces it below,
       so a refusal still wins. */
    const before = persona;
    setPersona({ ...persona, ...patch });
    const res = await mutate<{ persona?: Persona }>(
      `/api/seats/${seat}/persona`, { method: "POST", body: patch, quiet: true });
    setBusy("");
    if (res?.ok && res.data?.persona) {
      setPersona(res.data.persona);
      toast(`${seat}: ${label}`, "good");
    } else {
      setPersona(before);
      toast(res?.error || `could not change ${label}`, "bad");
    }
  }

  if (!choices) {
    return (
      <div className="bgs-persona">
        <Head label="On the floor" hint="reading this seat's look…" />
      </div>
    );
  }

  return (
    <div className="bgs-persona">
      {/* THE ONLY FIELD HERE THAT REACHES A REAL AGENT, so it goes first and
          says so. Everything below it is decoration; this is appended to the
          dispatch prompt of every agent spawned into this seat. */}
      <Head label="Character"
            hint="appended to this seat's dispatch prompt, so agents spawned here actually behave this way" />

      <label className="bgs-pfield bgs-pstyle">
        <span className="k"><Ti name="mood-smile" size={13} /> How it carries itself</span>
        <textarea rows={3} maxLength={choices.style_max} disabled={!!busy}
                  autoComplete="off"
                  defaultValue={persona.style || ""} key={`s:${persona.style || ""}`}
                  placeholder="Blunt, allergic to meetings, explains things twice."
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    if (v !== (persona.style || "")) save({ style: v }, "character");
                  }} />
        <span className="n">
          Manner only. The prompt tells the agent this changes its tone and not
          its job, and that its mission wins wherever the two disagree, so it
          cannot be used to widen a lane or skip a gate. It rides on every
          dispatch for this seat, so keep it to a couple of sentences.
          Up to {choices.style_max} characters; empty removes it.
        </span>
      </label>

      <Head label="On the floor"
            hint="how this seat looks in the studio view. Nothing below changes what the agent does." />

      <div className="bgs-pgrid">
        <label className="bgs-pfield">
          <span className="k"><Ti name="id-badge" size={13} /> Goes by</span>
          <input type="text" maxLength={choices.name_max} disabled={!!busy}
                 autoComplete="off" spellCheck={false}
                 defaultValue={persona.name || ""} key={`n:${persona.name || ""}`}
                 placeholder="default for this seat"
                 onBlur={(e) => {
                   const v = e.target.value.trim();
                   if (v !== (persona.name || "")) save({ name: v }, "name");
                 }} />
          <span className="n">the name on the room's plate</span>
        </label>

        {/* THE CAST. `generic` is offered explicitly rather than hidden as a
            fallback: "look like nobody in particular" is a real choice, and a
            seat a project invents has no art of its own anyway. */}
        <label className="bgs-pfield">
          <span className="k"><Ti name="user" size={13} /> Character</span>
          <select value={persona.cast || ""} disabled={!!busy}
                  onChange={(e) => save({ cast: e.target.value }, "character")}>
            <option value="">default for this seat</option>
            {choices.cast.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
          <span className="n">which sprite walks around the room</span>
        </label>

        <label className="bgs-pfield">
          <span className="k"><Ti name="layout-grid" size={13} /> Flooring</span>
          <select value={persona.surface || ""} disabled={!!busy}
                  onChange={(e) => save({ surface: e.target.value }, "flooring")}>
            <option value="">default for this seat</option>
            {choices.surface.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <span className="n">
            {SURFACE_NOTE[persona.surface || ""] || "what the room's floor is made of"}
          </span>
        </label>

        {/* THE ONE WORD UNDER THE NAMEPLATE. Saved on blur and on Enter rather
            than per keystroke: a POST per character would be a request per
            letter for a field somebody is still thinking about. */}
        <label className="bgs-pfield">
          <span className="k"><Ti name="typography" size={13} /> Nameplate word</span>
          <input type="text" maxLength={choices.vibe_max} disabled={!!busy}
                 autoComplete="off" spellCheck={false}
                 defaultValue={persona.vibe || ""} key={persona.vibe || ""}
                 placeholder="default for this seat"
                 onBlur={(e) => {
                   const v = e.target.value.trim();
                   if (v !== (persona.vibe || "")) save({ vibe: v }, "nameplate");
                 }}
                 onKeyDown={(e) => {
                   if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                 }} />
          <span className="n">
            what the room is FOR, in the studio's own language.
            Up to {choices.vibe_max} characters; empty resets it.
          </span>
        </label>
      </div>

      {/* THE SEAT'S OWN LOUNGE LINES. Same two rules the shared pool is held to
          and the server enforces both: short enough for a bubble, and no em
          dashes, which are the loudest tell that a line was machine written. */}
      <label className="bgs-pfield">
        <span className="k"><Ti name="message-2" size={13} /> Things it says in the lounge</span>
        <textarea rows={4} disabled={!!busy} autoComplete="off"
                  defaultValue={(persona.lines || []).join("\n")}
                  key={`l:${(persona.lines || []).join("|")}`}
                  placeholder={"One line per row." + "\n" + "Who moved the good chair." + "\n" + "It was like that already."}
                  onBlur={(e) => {
                    const next = e.target.value.split("\n")
                      .map((l) => l.trim()).filter(Boolean);
                    const now = persona.lines || [];
                    if (next.join("|") !== now.join("|")) save({ lines: next }, "lines");
                  }} />
        <span className="n">
          Said in a speech bubble when this character is the one talking, and
          only while nothing is running. Up to {choices.lines_max} lines of
          {" "}{choices.line_max} characters; empty falls back to the shared pool.
        </span>
      </label>

      <p className="bgs-pnote">
        <Ti name="info-circle" size={12} />
        Stored on this project's seat, so two games can look different.
        Open <b>Orchestration → Floor</b> to see it.
      </p>
    </div>
  );
}
