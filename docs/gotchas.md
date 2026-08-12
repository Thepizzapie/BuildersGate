# Gotchas found the hard way

Every entry cost real time or real money on a real machine. Scan the headings
for the symptom you have.

## Video and cutscenes

### A cutscene plays as flat coloured rectangles in the game

The `.ogv` looks right in the dashboard gallery, ffmpeg exited 0, the file is
the right size, and Godot draws blocks of flat green.

**Cause.** An ffmpeg whose libtheora encodes without error and writes bitstreams
the decoder cannot read. Measured on Gyan.FFmpeg `8.1.1-full` from winget: 179 of
193 frames throwing `error in unpack_block_qpis`, and 37 decode errors from a
one-second probe. Quality (`-q:v 10` still gives 65), encoder threading, frame
size and pixel-art content were each ruled out by experiment. `7.1-essentials`
from the same packager round-trips the same command with zero errors, and
GyanD/codexffmpeg issue #200 reports the same symptom against a build BtbN
compiles fine from the same version. The rule is **prove the build, do not trust
its version string**. The known-good binary here is a gyan build too.

**Fix.** Install a different build (BtbN's gpl release on Windows, your
distribution's package on Linux) and point Builders Gate at it with
`BGATE_FFMPEG`, or drop it at `~/.bgate/bin/ffmpeg[.exe]`. Resolution order in
`bgate_core/ffmpegbin.py`, most specific first: an explicit argument,
`BGATE_FFMPEG`, `~/.bgate/bin`, PATH. A `BGATE_FFMPEG` naming something that is
not there refuses rather than falling back to the binary you were escaping.

`bgate doctor` catches this now. It round-trips one second of video through the
encoder and decodes it back, rather than checking that `libtheora` appears in
`-encoders`. Presence was the whole test for the life of the bug and the row
stayed green throughout. Any decoder stderr fails the row, because a healthy
build is silent. About a quarter of a second, cached per executable path.

### A cutscene has artifacts on an older Godot

Godot's own Theora decoder. Godot 4.4.1 carries the fix, PR #101958, and is what
this was verified against. `bgate doctor` accepts Godot 4.0+, so no row warns
you. Upgrade the engine to 4.4.1 or later.

### `Unknown encoder 'libtheora'` at the keep step

Every shot generates and none can be kept. libtheora and libvorbis are optional
build flags and several distributions ship without them. Godot's
`VideoStreamPlayer` supports Ogg Theora and nothing else in core (H.264 and H.265
are patent-encumbered, WebM was removed in 4.0), so a keep transcodes rather than
copies. Install a fuller build.

The `ffmpeg` doctor row stays green without libtheora: capture, frame extraction
and recording all work without it, so the missing codec rides in the version
string instead of turning the row red.

## Paid APIs

### kie returns HTTP 403 while the key is valid

Body reads `error code: 1010` / `browser_signature_banned`, and
`file-base64-upload` 403s for every anchored shot.

**Cause.** Cloudflare fronts both kie hosts and refuses urllib's default
User-Agent. Measured: identical POST with no UA gives 1010, with a UA the
endpoint answers normally. It was already fixed on the download path
(`DOWNLOAD_UA` in `bgate_adapters/kie.py`) and never applied to the API path.

**Fix.** Applied: `_request` sends a User-Agent on every call. The 403 arrives at
the transport layer with no `code` in the body, so it never reached kie's
business-code table; there is a row for it there now.

Krea sends no User-Agent on any of its three HTTP paths. If Krea enables the same
rule, every Krea call fails at once and reads as an auth problem. Nothing is
broken today, this is where to look.

### seedance-2 returns 422 on a shot with both an anchor and refs

"The reference image and the first and last frames are mutually exclusive, and
only one scene can be selected", arriving after the anchors have been uploaded.
Every field is individually legal and only the combination is refused, which no
field-by-field validation catches. A shot list carrying a storyboard still and a
pinned cast hits it every time.

Applied: the model table declares the pair `exclusive`, `cinematic._fit_intent`
resolves it before spending (anchor wins, references are dropped and reported),
and `kie.build_input` refuses the combination outright.

### kie returns HTTP 500 for a field its reference calls optional

`flux-2-pro-edit` answers 500 "resolution is required" while the published
reference lists `resolution` as merely supported. The model table now carries a
per-model `defaults` block that fills what the endpoint demands and the
documentation does not. Read a 500 from kie as a missing input before you read it
as an outage.

### Anchored work through kie refuses every time

From the outside this looks like kie cannot do anchored work at all.
`kie.DEFAULT_IMAGE_MODEL` is `nano-banana`, which declares no reference field.

Anchored work belongs on `flux-2-pro-edit` (a list, capped at nine by the
adapter; kie publishes no cap) or `qwen-edit` (exactly one). `chroma.generate`
upgrades the model itself when references are present and the caller named none.
An explicitly named model is still obeyed, and still refused: silently overriding
a stated choice is how you buy the wrong thing.

`kie.upload_file` mints a hosted URL from a local file, which is what makes a
local pinned anchor reachable. The minted URL dies in three days
(`UPLOAD_TTL_DAYS`), is stamped with `expires_at`, and must never be cached as an
asset.

### You paid for a kie generation and have nothing

A kie generation is charged at SUBMIT. The poll loop, the download and this
process surviving the ten minutes are all after the money moved, so a seat whose
only option is to press generate again pays twice.

The task id is written the moment it exists rather than when the call returns.
`cinematic_stuck_shots` reports paid work kie is still holding and
`cinematic_recover_shot` collects it. Recovery claims no cost, because the charge
already happened.

### A sprite-sheet prompt is refused

`imagegen._reject_multi_pose` rejects any prompt asking for rows, grids or
multiple poses in one image, because sheet generations are where character
consistency dies. Rewrite the prompt to describe one frame, or call
`image_sprites` with one entry in `poses` per frame and let it stitch the sheet.
One API call per frame, each chained against the anchor.

### The Krea ledger reads $0.00 while money leaves

A per-day or per-project ceiling never triggers on Krea. There was no
`spend.record` anywhere in the Krea adapter and `chroma.generate` writes none
either, so `spend.check` had nothing to sum. It mattered most on the busiest paid
path, since character work routes to Krea by default.

Fixed in `krea._account`. An unknown cost is recorded as 0.0 with the reason in
the detail rather than skipped: a confident wrong number is worse than an
unenforced ceiling.

## Blender and 3D

### The first render after a cold boot times out

Measured on Blender 4.5, Windows: the first EEVEE render after a cold boot blew
past a 240s timeout, and the same script later ran in 1.4s. Root cause
unconfirmed. Clearing Blender's `gl-shader-cache` did not bring the stall back,
so the warmup lives below Blender, in the GPU driver shader cache or the OS
first-loading Blender's GPU DLLs.

Call `blender_warmup()` once per boot to pay it up front. The first
GPU-engine render gets `COLD_START_TIMEOUT` regardless of the caller's timeout,
so an agent's real render is never the one that stalls. Iterate on
`BLENDER_WORKBENCH` (about 1s) and switch to EEVEE or Cycles for a beauty pass.

### `bpy.ops.uv.smart_project` fails

It fails `poll()` in OBJECT mode. In EDIT mode it is fine headless, around 0.5s,
and it does not hang despite the folklore.

## Processes and capture

### A render is slow under the MCP server and fine standalone

The child sits at roughly 0% CPU, blocked forever, and it gets misdiagnosed as a
GPU stall. The stdio server's stdin *is* the client's protocol channel, and a
child that inherits it blocks on it and can corrupt the session. Every subprocess
spawned from the server passes `stdin=DEVNULL`.

Diagnose this class by CPU time, not wall clock: an idle child is blocked, a busy
one is genuinely slow. It cost an hour on the Blender adapter.

### A Godot exe reports "not recognized as a program"

A failed unzip leaves a 0-byte `.exe` that looks installed. Discovery rejects
stubs under 64KB. Re-download.

Not a problem, and worth recording: Godot's plain `.exe` does not lose stdout
when piped. Measured on 4.7.1, it and `_console.exe` deliver identical output.
The console variant is a ~200KB launcher that attaches a console window for
double-clicking, so we prefer the main exe and leak one less process on a kill.

### Whisper dies with `cublas64_12.dll is not found`

ctranslate2's `device="auto"` picks CUDA on any NVIDIA box without checking that
the CUDA libraries load. `WhisperModel(...)` construction touches no CUDA and
`transcribe()` returns a lazy generator, so a naive probe succeeds without ever
running an encode. The runner consumes the generator to force a real encode, then
falls back to CPU/int8 and reports why.

### A physics complaint lands on the audio seat

Whisper segments are not utterances. One routinely holds several remarks: *"the
jump feels floaty. I do not like it. But I love the music here."* Classified
whole, that is one item routed to audio because "music" wins, and the compliment
vanishes. Segments are split per sentence with interpolated timestamps.

### A keyword filter misses feedback that was clearly given

Speech-to-text does not preserve word choice. "floaty" comes back as "floating",
and `\benemy\b` silently misses "the enemies are too fast". Match stems, not the
adjective you imagined.

Short pronoun remarks ("I do not like it") carry no routable noun and inherit the
previous seat, but only within a segment. Across a pause, "it" is anyone's guess.

### Every telemetry event is offset by a constant

The game's clock and the recorder's clock are unrelated: the game may have been
running an hour before you hit record, so "seconds since game start" offsets every
join. Telemetry carries `ts`, a unix wall clock, and
`playtest_session.started_epoch` anchors the conversion. An event arriving without
`ts` makes ingest say so rather than assume the clocks agree.

### Telemetry reports a jump that never happened

`peak_height: 302` for a 24px player, with no jump in the session. The template
player spawns in mid-air and `_peak_y` was initialized only on jump, so the
opening drop was measured as a jump. Airborne state is now stamped on every entry
(`spawn`, `jump`, `fall`) and `cause` rides along on every landing.
