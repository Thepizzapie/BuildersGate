/* THE THINGS A SEAT ACTUALLY MAKES, as forms rather than as a graph.
 *
 * WHAT WAS WRONG WITH THE NODE LIBRARY IN A SEAT. A workflow template is a
 * seven-node graph with typed ports, per-node config panels and a compile step.
 * That is the right tool for ASSEMBLING a pipeline and the wrong one for
 * running the pipeline you already have: to get one concept sheet you opened a
 * canvas, found the node that takes the prompt, opened its inspector, typed
 * into it, and pressed Run. Every seat's most common act was its most expensive
 * interaction, and the graph taught you nothing about which node to touch
 * first — which is exactly the "blindly designed, makes no sense how to use it"
 * complaint. The canvas is kept, one tab across, for the case it is good at.
 *
 * A RECIPE IS THE COMMON PATH, NAMED, WITH ITS INPUTS ON SCREEN. Two to four
 * fields, an example in every placeholder, and one button. No node, no port, no
 * inspector.
 *
 * HOW A RECIPE RUNS, AND WHY MOST OF THEM FILE BOARD ITEMS.
 * The generators live behind MCP tools (image_generate, sprite_plan,
 * sfx_generate, item_generate) and are held by the SEAT AGENT, not by the
 * dashboard — that is the product's whole execution model, and it is why the
 * node graph compiles to one queue item per agent step rather than calling a
 * generator directly. A form here that bypassed the board would be a second,
 * quieter execution path with no lane check and no QA gate. So a
 * recipe files the same well-formed work the graph would have filed, and the
 * board runs it.
 *
 * WHERE A REAL HTTP GENERATOR EXISTS IT IS CALLED DIRECTLY — music has
 * /api/music/generate and a job to poll, and routing that through an agent
 * would be ceremony around a call the dashboard can already make.
 *
 * THE BRIEF IS THE PRODUCT OF THIS FILE. An agent receives prose, so a recipe's
 * real output is a paragraph that names the craft's own rules — pin the
 * reference, condition every frame on it, measure the result — assembled from
 * what the person typed. A form that filed "make a sprite: goblin" would be a
 * worse brief than the person would have written by hand, and that is the bar.
 */

export type Field = {
  key: string;
  label: string;
  placeholder?: string;
  hint?: string;
  lines?: number;
  optional?: boolean;
  /** A fixed vocabulary renders as a select. First entry is the default. */
  options?: string[];
};

export type Recipe = {
  id: string;
  label: string;
  icon: string;
  /** One line: what you get, not what it does. */
  hint: string;
  fields: Field[];
  /** What the person will have when it finishes, stated before they start. */
  yields: string;
  /** A direct endpoint, when the dashboard can do the work itself. */
  post?: (v: Record<string, string>) => { url: string; body: Record<string, unknown> };
  /** Otherwise: the board item(s) this files, in order. */
  chain?: (v: Record<string, string>) => { seat: string; title: string; brief: string }[];
};

const trim = (v: Record<string, string>, k: string) => (v[k] || "").trim();

/* Each brief below ends with the seat's own doctrine, because the agent that
   picks the item up is held to it and a brief that omits it invites the work
   that gets rejected at the gate. */
const ART_RULE =
  "Pin the reference first and condition every frame on it — consistency is " +
  "enforced, never requested. Lock the binary before editing it. LOOK at the " +
  "frames before calling it done, and report what the alpha/chroma audit " +
  "measured rather than asserting it is clean.";

const AUDIO_RULE =
  "Bind the sound to a real game event — a sound the game never asks for is a " +
  "sound nobody hears. Target -14 LUFS and report the measured loudness. Lock " +
  "the binary before editing: audio files do not merge.";

const VIDEO_RULE =
  "Board it, then write the shot list, then buy a frame. Every shot anchors on " +
  "an approved still, never on the previous shot's output. Nothing ships as " +
  ".mp4 — Godot plays Ogg Theora and only Ogg Theora, so transcode and then " +
  "watch the assembled cut before calling it delivered.";

export const RECIPES: Record<string, Recipe[]> = {
  art: [
    {
      id: "concept",
      label: "Concept sheet",
      icon: "palette",
      hint: "explore the look before anything is locked",
      yields: "a set of unpinned concepts to choose from",
      fields: [
        { key: "subject", label: "what", placeholder: "a hunched accounting wizard in a cheap suit" },
        { key: "style", label: "look", optional: true,
          placeholder: "flat vector, two-tone, high contrast",
          hint: "left empty, the seat uses the project's active art style" },
        { key: "count", label: "how many", options: ["4", "2", "6", "8"] },
      ],
      chain: (v) => [{
        seat: "art",
        title: `Concepts: ${trim(v, "subject").slice(0, 60)}`,
        brief:
          `Generate ${trim(v, "count") || "4"} concept variations of: ` +
          `${trim(v, "subject")}.` +
          (trim(v, "style") ? ` Look: ${trim(v, "style")}.` : " Use the project's active art style.") +
          ` These are EXPLORATORY — do not pin anything yet, and do not promote ` +
          `into the game. File them as candidates so a human can pick one. ` +
          ART_RULE,
      }],
    },
    {
      id: "anchor",
      label: "Character anchor",
      icon: "pin",
      hint: "lock the one image every later frame is measured against",
      yields: "one approved, pinned reference",
      fields: [
        { key: "name", label: "character", placeholder: "accounting_wizard" },
        { key: "desc", label: "who they are", lines: 2,
          placeholder: "middle-aged, exhausted, carries a ledger he never puts down" },
      ],
      chain: (v) => [{
        seat: "art",
        title: `Anchor: ${trim(v, "name")}`,
        brief:
          `Produce the canonical reference image for ${trim(v, "name")}: ` +
          `${trim(v, "desc")}. This image becomes THE identity target — call ` +
          `ref_pin on the approved revision so every later generation ` +
          `conditions on it. Without a pin every generation is a fresh guess. ` +
          ART_RULE,
      }],
    },
    {
      id: "sheet",
      label: "Animation sheet",
      icon: "photo",
      hint: "poses from a plan, conditioned on the pin",
      yields: "a stitched sheet with a per-frame audit",
      fields: [
        { key: "name", label: "character", placeholder: "accounting_wizard" },
        { key: "action", label: "action", placeholder: "hurt" },
        { key: "frames", label: "frames", options: ["6", "4", "8", "12"] },
      ],
      /* TWO LINKS, NOT ONE, and the order is the point: sprite_plan decides the
         poses and the sheet is generated FROM that plan. Filed as a chain so the
         second cannot start before the first has produced what it reads. */
      chain: (v) => [
        {
          seat: "art",
          title: `Plan poses: ${trim(v, "name")} ${trim(v, "action")}`,
          brief:
            `Call sprite_plan for ${trim(v, "name")} performing "${trim(v, "action")}" ` +
            `across ${trim(v, "frames") || "6"} frames. Decide the poses first — a ` +
            `sheet generated without a plan is six variations of the same pose.`,
        },
        {
          seat: "art",
          title: `Sheet: ${trim(v, "name")} ${trim(v, "action")}`,
          brief:
            `Generate the ${trim(v, "frames") || "6"}-frame ${trim(v, "action")} sheet for ` +
            `${trim(v, "name")} from the poses sprite_plan just filed, ` +
            `conditioned on the pinned reference. ` + ART_RULE,
        },
      ],
    },
    {
      id: "item",
      label: "Item + variants",
      icon: "sword",
      hint: "one object, then its rarities",
      yields: "a base item and its variant set",
      fields: [
        { key: "item", label: "item", placeholder: "stapler of binding" },
        { key: "variants", label: "variants", options: ["3", "0", "5"] },
      ],
      chain: (v) => [{
        seat: "art",
        title: `Item: ${trim(v, "item").slice(0, 60)}`,
        brief:
          `Generate the item "${trim(v, "item")}"` +
          (trim(v, "variants") !== "0"
            ? ` and ${trim(v, "variants")} variants of it, using item_variants so the ` +
              `variants share a silhouette and differ readably.`
            : ".") +
          ` Keep it readable at the size it is actually drawn in the game. ` + ART_RULE,
      }],
    },
  ],

  audio: [
    {
      id: "music",
      label: "Music track",
      icon: "music",
      hint: "a loop or a piece, generated and auditioned",
      yields: "candidates to keep one of",
      fields: [
        { key: "prompt", label: "the music", lines: 2,
          placeholder: "slow, hopeful synth loop for the hub screen — no drums, no vocals" },
        { key: "name", label: "call it", optional: true, placeholder: "hub_theme" },
      ],
      /* THE ONE RECIPE THAT DOES NOT GO THROUGH THE BOARD. /api/music/generate
         exists, returns a job id and the dashboard already polls jobs — filing
         an agent to press a button the browser can press is ceremony. */
      post: (v) => ({
        url: "/api/music/generate",
        body: { prompt: trim(v, "prompt"), name: trim(v, "name") },
      }),
    },
    {
      id: "sfx",
      label: "Sound effect",
      icon: "wave-sine",
      hint: "one event, one sound, bound to the event",
      yields: "an sfx file wired to the hook",
      fields: [
        { key: "event", label: "game event", placeholder: "combat.hit",
          hint: "the event the game already emits — the Hooks tab lists the unbound ones" },
        { key: "sound", label: "what it sounds like",
          placeholder: "a wet, short meat impact with no tail" },
      ],
      chain: (v) => [{
        seat: "audio",
        title: `SFX: ${trim(v, "event")}`,
        brief:
          `Generate a sound for the event ${trim(v, "event")}: ${trim(v, "sound")}. ` +
          `Then BIND it to that event — an unbound file is a sound the game ` +
          `never asks for. ` + AUDIO_RULE,
      }],
    },
    {
      id: "voice",
      label: "Voice line",
      icon: "microphone-2",
      hint: "one spoken line, in a character's voice",
      yields: "a rendered line, bound where it is used",
      fields: [
        { key: "who", label: "character", placeholder: "hr_bard" },
        { key: "line", label: "the line", lines: 2,
          placeholder: "Your wellness check is overdue. Again." },
      ],
      chain: (v) => [{
        seat: "audio",
        title: `Voice: ${trim(v, "who")}`,
        brief:
          `Render the line "${trim(v, "line")}" as ${trim(v, "who")} with ` +
          `voice_speak, and say which voice you used so the next line matches. ` +
          AUDIO_RULE,
      }],
    },
  ],

  cinematic: [
    {
      id: "cutscene",
      label: "Cutscene",
      icon: "movie",
      hint: "premise → board → shot list → render → cut",
      yields: "a boarded sequence, priced before a frame is bought",
      fields: [
        { key: "name", label: "call it", placeholder: "cold_open" },
        { key: "premise", label: "what happens", lines: 3,
          placeholder: "The lift climbs through an empty lobby. The accounting wizard signs a form. The quarter closes." },
        { key: "frames", label: "beats", options: ["6", "4", "8", "12"] },
      ],
      /* THE CHAIN IS THE SEAT'S DOCTRINE, IN ORDER. Boarding is nearly free and
         the shot list is free; only the last link spends. Filed as a chain so a
         generate cannot start before a human has seen what it would buy. */
      chain: (v) => [
        {
          seat: "cinematic",
          title: `Board: ${trim(v, "name")}`,
          brief:
            `Call storyboard_plan for "${trim(v, "name")}" with ` +
            `${trim(v, "frames") || "6"} beats, from this premise: ` +
            `${trim(v, "premise")}. Then generate the board frames. The board ` +
            `costs a fraction of a cent — this is the cheap place to argue with ` +
            `the sequence, so do it here.`,
        },
        {
          seat: "cinematic",
          title: `Shot list: ${trim(v, "name")}`,
          brief:
            `Write the shot list for "${trim(v, "name")}" with cinematic_plan, ` +
            `anchoring every shot on an approved board frame. Do NOT generate ` +
            `any shot yet — stop here and report the estimate so a human sees ` +
            `the price before a frame is bought. ` + VIDEO_RULE,
        },
      ],
    },
    {
      id: "trailer",
      label: "Attract loop",
      icon: "device-tv",
      hint: "a short looping piece for the title screen",
      yields: "a boarded loop and its price",
      fields: [
        { key: "name", label: "call it", placeholder: "attract_loop" },
        { key: "beats", label: "what it shows", lines: 2,
          placeholder: "three quick looks at the tower, ending on the lich's desk" },
      ],
      chain: (v) => [{
        seat: "cinematic",
        title: `Attract loop: ${trim(v, "name")}`,
        brief:
          `Board and plan a short looping attract sequence "${trim(v, "name")}": ` +
          `${trim(v, "beats")}. It loops, so the last frame has to cut back to ` +
          `the first without a visible seam — say how you handled that. Stop ` +
          `after the shot list and report the estimate. ` + VIDEO_RULE,
      }],
    },
  ],
};
