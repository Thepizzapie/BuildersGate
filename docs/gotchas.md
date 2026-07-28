# Gotchas found the hard way

2026-07-27. Each of these cost real time on a real machine. They are recorded
because the diagnosis is the useful part.

## GPU cold start will eat your first render

Measured here, Blender 4.5 on Windows: the first EEVEE render after a cold boot
blew past a 240s timeout. Every run after took 1 to 12 seconds. The *same script*
that timed out later ran in 1.4s.

Clearing Blender's own `gl-shader-cache` did not bring the stall back, so the
warmup lives below Blender, in the GPU driver shader cache or the OS first-loading
Blender's GPU DLLs. Root cause unconfirmed. The cost is real and reproducible.

Mitigation: call `blender_warmup()` once per boot to pay it deliberately. The
first GPU-engine render gets `COLD_START_TIMEOUT` regardless of the caller's
timeout, so an agent's real render is never the one that stalls. Iterate on
`BLENDER_WORKBENCH` (~1s) and switch to EEVEE or Cycles only for a beauty pass.

## bpy.ops.uv.smart_project needs EDIT mode

In OBJECT mode it fails `poll()`. In EDIT mode it is fine headless, around 0.5s.
It does not hang, despite the folklore.

## Subprocesses from a stdio MCP server MUST use stdin=DEVNULL

The server's stdin *is* the client's protocol channel. A child that inherits it
blocks forever at roughly 0% CPU and can corrupt the session.

This presents as a *slow* render and gets misdiagnosed as a GPU stall. The tell:
it works standalone, where stdin is a terminal, and hangs under the server.
Diagnose by CPU time, not wall clock. An idle child is blocked; a busy one is
genuinely slow. This cost an hour on the Blender adapter.

## Godot's plain .exe does not lose stdout when piped

Measured on 4.7.1: both it and `_console.exe` deliver identical output. The
console variant is a ~200KB launcher that only attaches a console *window* for
double-clicking. We prefer the main exe. Same output, one less process to leak on
a kill.

## A failed unzip leaves a 0-byte .exe

It looks installed and fails with "not recognized as a program". Discovery
rejects stubs under 64KB.

## ctranslate2's device="auto" picks CUDA on any NVIDIA box

It does so without checking that the CUDA libraries load, then dies at inference
with `cublas64_12.dll is not found`.

Worse, `WhisperModel(...)` construction touches no CUDA, and `transcribe()`
returns a lazy generator, so a naive probe "succeeds" without running an encode.
The runner consumes the generator to force a real encode, then falls back to
CPU/int8 and reports why.

## Whisper segments are not utterances

One segment routinely holds several remarks: *"the jump feels floaty. I do not
like it. But I love the music here."*

Classified whole, that becomes ONE item routed to **audio**, because the word
"music" wins. A physics complaint lands on the wrong seat and the compliment
vanishes. Segments are split per sentence with interpolated timestamps.

## The game's clock and the recorder's clock are unrelated

The game may have been running an hour before you hit record. Telemetry therefore
carries `ts`, a unix wall clock, and `playtest_session.started_epoch` anchors the
conversion.

A raw "seconds since game start" silently offsets every join by however long the
game had been up. If an event arrives without `ts`, ingest says so rather than
quietly assuming the clocks agree.

## Uninitialized telemetry lies plausibly

The template player spawns in mid-air. With `_peak_y` initialized only on jump,
the opening drop reported `peak_height: 302` for a 24px player, and no jump had
happened.

Nonsense that looks like a measurement is worse than a missing field. It sends an
agent chasing physics that never occurred. Airborne state is now stamped on every
entry (`spawn`, `jump`, `fall`) and `cause` rides along on every landing.

## Speech-to-text does not preserve your word choice

"floaty" comes back as "floating". `\benemy\b` silently misses "the enemies are
too fast". Match stems, not the adjective you imagined.

Short pronoun remarks ("I do not like it") carry no routable noun and inherit the
previous seat, but only within a segment. Across a pause, "it" is anyone's guess.
