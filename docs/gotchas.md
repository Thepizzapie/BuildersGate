# Gotchas found the hard way

2026-07-27, extended 2026-08-10 with the third-party API failures. Each of these
cost real time — or real money — on a real machine. They are recorded because the
diagnosis is the useful part.

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

## kie's 403 is Cloudflare refusing the User-Agent, not kie refusing the key

Cloudflare fronts both kie hosts and answers a banned agent with HTTP 403 and a
body reading `error code: 1010` / `browser_signature_banned`. urllib's default
agent is on that list. This was already measured and fixed on the *download*
path — `DOWNLOAD_UA`, `bgate_adapters/kie.py:1960` — and never applied to the API
path, so `file-base64-upload` 403'd for every anchored shot while the key was
valid. Measured: identical POST with no UA gives 1010, with a UA the endpoint
answers normally. `_request` now sends one on every call (`kie.py:1536`).

The 403 arrives at the transport layer with no `code` in the body, so it never
reaches kie's business-code table and the table had nothing to say about it;
there is a row for it now (`kie.py:214`) for the day the rule widens again.

Krea sends no User-Agent on any of its three HTTP paths (`krea.py:469`, `:720`,
`:999`). If Krea ever enables the same rule, every Krea call fails at once and
reads as an auth problem. Nothing is broken today; this is where to look.

## seedance-2 takes an anchor frame or reference images, never both

Its own words on the 422: "The reference image and the first and last frames are
mutually exclusive, and only one scene can be selected." Every field is
individually legal — only the combination is refused, which no field-by-field
validation can catch, and the refusal arrives *after* the anchors have been
uploaded. A shot list carrying a storyboard still and a pinned cast hit it every
time.

The model table declares it as `exclusive` (`kie.py:389`), `cinematic._fit_intent`
resolves it before spending (anchor wins, references are dropped and reported,
never silently), and `kie.build_input` refuses the combination outright.

## kie's reference lists a field as optional and the API returns 500 without it

`flux-2-pro-edit` answers HTTP 500 "resolution is required" although the
reference lists `resolution` as merely supported. Measured, not guessed. The
model table now carries a `defaults` block (`kie.py:310`) that fills a field the
endpoint demands and the documentation does not, per model. A 500 from kie is
worth reading as a missing input before it is read as an outage.

## kie's default image model takes no reference images at all

`nano-banana` — `kie.DEFAULT_IMAGE_MODEL` — declares no reference field, so
defaulting to it and then asking for anchored work refuses every time, which from
the outside is indistinguishable from "kie cannot do anchored work". It can:
`kie.upload_file` mints a hosted URL from a local file, which is what makes a
pinned anchor reachable at all. Anchored work through kie belongs on
`flux-2-pro-edit` (a list; the adapter caps it at nine, kie publishes no cap) or
`qwen-edit` (exactly one), and
`chroma.generate` now upgrades the model itself when references are present and
the caller named none (`bgate_core/chroma.py:868`). An explicitly named model is
still obeyed, and still refused — silently overriding a stated choice is how you
buy the wrong thing.

The docstrings claiming "kie cannot condition on a local pinned anchor" were true
of the raw endpoint and false of the adapter. The minted URL dies in three days
(`UPLOAD_TTL_DAYS`), so it is stamped with `expires_at` and must never be cached
as if it were an asset.

## A kie generation is charged at SUBMIT

The poll loop, the download, and this process surviving the ten minutes it takes
are all *after* the money moved. A seat whose only option is to press generate
again pays twice. The task id is therefore written the moment it exists rather
than when the call returns (`cinematic.py:1069`), and `cinematic_stuck_shots`
reports paid work kie is still holding while `cinematic_recover_shot` collects
it. Recovery claims no cost: the charge already happened, so a balance delta
measured now would be meaningless.

## A sprite-sheet prompt is refused on purpose

`imagegen._reject_multi_pose` refuses any prompt asking for rows, grids or
multiple poses in one image — "sheet generations are where character consistency
dies" (`bgate_adapters/imagegen.py:216`). It reads like a bug the first time you
hit it. It is a measured rule: one API call per frame, chained against the
anchor. Rewrite the prompt to describe one frame, or call `image_sprites` with
one entry in `poses` per frame and let it stitch the sheet.

## Krea spend never reached the budget ceiling

There was no `spend.record` anywhere in the Krea adapter, and `chroma.generate`
does not write one either, so `spend.check` — which sums `spend_event` — could
never reach a per-day or per-project ceiling on Krea spend. The ledger read $0.00
while the money left, which is worse than an unenforced ceiling because the
number shown is confident and wrong. Fixed in `krea._account`
(`bgate_adapters/krea.py:1011`); an unknown cost is recorded as 0.0 *with the
reason in the detail* rather than skipped. It mattered most on the busiest paid
path, because character work now routes to Krea by default.
