"""Audio, the parts a browser cannot do — probing, Godot import params, sessions.

The DSP lives in the browser. WebAudio already decodes ogg/wav/mp3, resamples,
mixes and renders offline, and moving PCM over HTTP to do the same work twice
would be slower and worse. So this module is deliberately the small half:

  * PROBE. What a file IS — rate, channels, duration — for a listing that has to
    be right before anything is decoded. ``wave`` handles WAV; Ogg Vorbis gets a
    real header/granule read here because "duration unknown" on every music
    track is the kind of gap that makes a panel feel broken.

  * GODOT IMPORT PARAMS. The single highest-value thing this can touch. A
    looping music track whose ``.import`` says ``loop=false`` plays once and
    stops, and NOTHING about the audio file says so — the setting lives in a
    sidecar the engine owns. This project ships exactly that bug on both music
    tracks. The wav and ogg importers spell looping completely differently
    (``edit/loop_mode`` + frame offsets vs ``loop`` + seconds), so both spellings
    live here rather than in a UI that would get one of them wrong.

  * SESSIONS. A mixdown is worth nothing if it cannot be re-opened and nudged.
    ``<name>.<ext>.mix.json`` records the tracks, offsets and gains that produced a
    file, next to the file, so a mix is a document rather than a one-shot.

  * WAV WRITING. The browser encodes 16-bit PCM WAV; this validates it before it
    lands on disk. Anything else (.ogg) needs ffmpeg, and its absence is
    reported as a fact rather than discovered as a traceback.

Pure stdlib. ``audioop`` is deliberately not used — it was removed in Python
3.13 and this has to run there.
"""
from __future__ import annotations

import io
import json
import os
import re
import struct
import time
import wave
from pathlib import Path
from typing import Iterable, Optional
from ..runtime import ffmpegbin as _ffmpegbin
from ..runtime.proc import run as _run

AUDIO_SUFFIXES = frozenset({".wav", ".ogg", ".mp3"})
# What the editor can WRITE. mp3 is playable and readable but never a write
# target: it is not what these projects ship, and re-encoding a lossy source to
# lossy output is a quality loss with nothing to show for it.
WRITABLE = frozenset({".wav", ".ogg"})

MAX_WAV_BYTES = 120 * 1024 * 1024      # ~11 minutes of 44.1k stereo 16-bit
MAX_SECONDS = 900.0

# Godot's AudioStreamWAV loop modes, spelled as the importer writes them.
LOOP_MODES = {"disabled": 0, "forward": 1, "pingpong": 2, "backward": 3}
LOOP_MODE_NAMES = {v: k for k, v in LOOP_MODES.items()}


class AudioError(ValueError):
    """A file or payload this module refuses to work with."""


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
def _probe_wav(path: Path) -> dict:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        frames = w.getnframes()
        return {
            "sample_rate": rate,
            "channels": w.getnchannels(),
            "sample_width": w.getsampwidth(),
            "frames": frames,
            "seconds": round(frames / rate, 4) if rate else None,
        }


def _ogg_pages(data: bytes) -> Iterable[int]:
    """Offsets of every ``OggS`` capture pattern. Used only near the ends."""
    start = 0
    while True:
        i = data.find(b"OggS", start)
        if i < 0:
            return
        yield i
        start = i + 4


def _probe_ogg(path: Path) -> dict:
    """Sample rate from the identification header, length from the last granule.

    Only the first 64 KB and the last 64 KB are read: a granule position is an
    absolute sample count, so the final page carries the whole duration and
    there is never a reason to walk a 6 MB file to find it.
    """
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(min(size, 65536))
        if size > 65536:
            fh.seek(max(0, size - 65536))
            tail = fh.read()
        else:
            tail = head
    if not head.startswith(b"OggS"):
        raise AudioError("not an Ogg stream")

    # Identification header: packet type 0x01 then "vorbis"; rate is 4 bytes at
    # offset 12 of the packet.
    ident = head.find(b"\x01vorbis")
    if ident < 0:
        raise AudioError("no Vorbis identification header")
    pkt = head[ident:ident + 30]
    if len(pkt) < 16:
        raise AudioError("truncated Vorbis header")
    channels = pkt[11]
    rate = struct.unpack_from("<I", pkt, 12)[0]

    last = -1
    for off in _ogg_pages(tail):
        if off + 14 <= len(tail):
            last = off
    granule = None
    if last >= 0:
        granule = struct.unpack_from("<q", tail, last + 6)[0]
    seconds = (granule / rate) if (granule and granule > 0 and rate) else None
    return {
        "sample_rate": rate,
        "channels": channels,
        "sample_width": None,
        "frames": granule if (granule or 0) > 0 else None,
        "seconds": round(seconds, 4) if seconds else None,
    }


def probe(path: str | os.PathLike[str]) -> dict:
    """What this audio file is. Never raises for an unreadable file — an
    unprobeable clip still belongs in the listing, it just says less."""
    path = Path(path)
    base = {"sample_rate": None, "channels": None, "sample_width": None,
            "frames": None, "seconds": None, "probe_error": None}
    suffix = path.suffix.lower()
    try:
        if suffix == ".wav":
            return {**base, **_probe_wav(path)}
        if suffix == ".ogg":
            return {**base, **_probe_ogg(path)}
    except (OSError, wave.Error, AudioError, struct.error, IndexError) as exc:
        return {**base, "probe_error": f"{type(exc).__name__}: {exc}"}
    return base            # .mp3 — the browser decodes it, we do not guess


# ---------------------------------------------------------------------------
# WAV validation
# ---------------------------------------------------------------------------
def validate_wav(blob: bytes) -> dict:
    """Refuse anything that is not a sane PCM WAV, before it reaches disk."""
    if len(blob) > MAX_WAV_BYTES:
        raise AudioError(f"{len(blob)} bytes is past the {MAX_WAV_BYTES} cap")
    try:
        with wave.open(io.BytesIO(blob), "rb") as w:
            rate, channels = w.getframerate(), w.getnchannels()
            width, frames = w.getsampwidth(), w.getnframes()
    except (wave.Error, EOFError) as exc:
        raise AudioError(f"not a readable WAV: {exc}") from exc
    if not 1000 <= rate <= 192000:
        raise AudioError(f"sample rate {rate} is out of range")
    if channels not in (1, 2):
        raise AudioError(f"{channels} channels — mono or stereo only")
    if width not in (1, 2, 3, 4):
        raise AudioError(f"{width * 8}-bit samples are not supported")
    seconds = frames / rate if rate else 0
    if seconds > MAX_SECONDS:
        raise AudioError(f"{seconds:.0f}s is past the {MAX_SECONDS:.0f}s cap")
    return {"sample_rate": rate, "channels": channels, "sample_width": width,
            "frames": frames, "seconds": round(seconds, 4)}


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------
def ffmpeg_path() -> Optional[str]:
    return _ffmpegbin.resolve()


def encode_ogg(wav_bytes: bytes, out_path: str | os.PathLike[str], *,
               quality: int = 6, timeout: float = 120.0) -> dict:
    """WAV bytes -> Ogg Vorbis on disk, via ffmpeg.

    The browser can only hand back PCM, and .ogg is what Godot streams music
    from — so this hop is not optional for music. Its absence is a reported
    fact: a UI that offers an .ogg save it cannot perform is worse than one
    that greys the option out and says why.
    """
    exe = ffmpeg_path()
    if not exe:
        raise AudioError(
            "ffmpeg is not on PATH — needed to write .ogg. Save as .wav "
            "instead, or install ffmpeg and try again.")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    proc = _run(
        [exe, "-y", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
         "-c:a", "libvorbis", "-q:a", str(int(quality)), "-f", "ogg", str(tmp)],
        input=wav_bytes, capture_output=True, timeout=timeout)
    if proc.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise AudioError(
            "ffmpeg could not encode the ogg: "
            + (proc.stderr.decode("utf-8", "replace")[-300:] or "no output"))
    os.replace(tmp, out)
    return {"path": str(out), "bytes": out.stat().st_size, "quality": quality}


# ---------------------------------------------------------------------------
# Godot import params — where looping actually lives
# ---------------------------------------------------------------------------
def import_path(audio: str | os.PathLike[str]) -> Path:
    p = Path(audio)
    return p.with_name(p.name + ".import")


_PARAM_RE = re.compile(r"^(?P<key>[A-Za-z0-9_/]+)=(?P<value>.*)$")


def read_import(audio: str | os.PathLike[str]) -> dict:
    """The ``[params]`` block of a Godot .import, as a dict of raw strings.

    Returns ``{}`` when there is no .import — an asset Godot has not yet seen
    is a normal state, not an error.
    """
    path = import_path(audio)
    if not path.is_file():
        return {}
    params: dict[str, str] = {}
    in_params = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_params = stripped == "[params]"
            continue
        if not in_params or not stripped:
            continue
        m = _PARAM_RE.match(stripped)
        if m:
            params[m.group("key")] = m.group("value").strip()
    return params


def loop_state(audio: str | os.PathLike[str], *,
               info: Optional[dict] = None) -> dict:
    """How this clip loops today, in ONE vocabulary regardless of importer.

    seconds, not frames, on the way out — the editor works in seconds and the
    wav importer's frame offsets are an implementation detail of that importer.
    """
    suffix = Path(audio).suffix.lower()
    params = read_import(audio)
    info = info or probe(audio)
    rate = info.get("sample_rate") or 0
    out = {"supported": suffix in (".wav", ".ogg"), "importer": suffix.lstrip("."),
           "enabled": False, "mode": "forward", "begin_s": 0.0,
           "end_s": None, "has_import": bool(params)}
    if suffix == ".ogg":
        out["enabled"] = params.get("loop", "false").lower() == "true"
        try:
            out["begin_s"] = float(params.get("loop_offset", 0) or 0)
        except ValueError:
            out["begin_s"] = 0.0
        out["end_s"] = None            # ogg loops to the end of the stream
        out["mode"] = "forward"
        return out
    if suffix == ".wav":
        try:
            mode = int(params.get("edit/loop_mode", 0) or 0)
        except ValueError:
            mode = 0
        out["enabled"] = mode != 0
        out["mode"] = LOOP_MODE_NAMES.get(mode, "forward")
        try:
            begin = int(params.get("edit/loop_begin", 0) or 0)
            end = int(params.get("edit/loop_end", -1) or -1)
        except ValueError:
            begin, end = 0, -1
        out["begin_s"] = round(begin / rate, 4) if rate else 0.0
        out["end_s"] = round(end / rate, 4) if (rate and end >= 0) else None
        return out
    return out


def write_loop(audio: str | os.PathLike[str], *, enabled: bool,
               begin_s: float = 0.0, end_s: Optional[float] = None,
               mode: str = "forward", info: Optional[dict] = None) -> dict:
    """Set the loop parameters Godot actually reads, in the importer's own spelling.

    Requires an existing .import: writing one from scratch means inventing a
    uid and a cache path, and a hand-written .import that disagrees with the
    engine's cache is a worse failure than "open it in Godot once first".
    """
    path = Path(audio)
    suffix = path.suffix.lower()
    if suffix not in (".wav", ".ogg"):
        raise AudioError(f"{suffix or 'this file'} has no Godot loop settings")
    imp = import_path(path)
    if not imp.is_file():
        raise AudioError(
            f"{path.name} has no .import yet — open the project in Godot once "
            "so the engine writes one, then set the loop here")

    info = info or probe(path)
    rate = info.get("sample_rate") or 0
    if mode not in LOOP_MODES:
        raise AudioError(f"loop mode must be one of {sorted(LOOP_MODES)}")
    if begin_s < 0:
        raise AudioError("loop start cannot be negative")
    total = info.get("seconds")
    if total and begin_s > total + 0.001:
        raise AudioError(f"loop starts at {begin_s:.2f}s, past the clip's "
                         f"{total:.2f}s")
    if end_s is not None and end_s <= begin_s:
        raise AudioError("loop end must be after loop start")

    if suffix == ".ogg":
        updates = {"loop": "true" if enabled else "false",
                   "loop_offset": _fmt_number(begin_s)}
        ignored = ["loop end (an ogg loops to the end of the stream)"] \
            if end_s is not None else []
    else:
        if not rate:
            raise AudioError("cannot convert seconds to frames — the WAV's "
                             "sample rate could not be read")
        updates = {
            # loop_state() reports a non-looping clip as mode "disabled", and
            # clients round-trip that back here — writing LOOP_MODES["disabled"]
            # (0) would mean "enable looping" silently left it off.
            "edit/loop_mode": str((LOOP_MODES.get(mode, 1) or 1) if enabled else 0),
            "edit/loop_begin": str(int(round(begin_s * rate))),
            "edit/loop_end": str(int(round(end_s * rate))) if end_s is not None else "-1",
        }
        ignored = []

    text = imp.read_text(encoding="utf-8", errors="replace")
    for key, value in updates.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(f"{key}={value}", text, count=1)
        else:
            text = _append_param(text, key, value)
    tmp = imp.with_suffix(imp.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, imp)
    return {"import": imp.name, "written": updates, "ignored": ignored,
            "loop": loop_state(path, info=info)}


def _fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.4f}".rstrip("0")


def _append_param(text: str, key: str, value: str) -> str:
    """Add a key to the [params] block, creating the block if it is absent."""
    if "[params]" not in text:
        return text.rstrip("\n") + f"\n\n[params]\n\n{key}={value}\n"
    head, _, tail = text.partition("[params]")
    lines = tail.split("\n")
    at = len(lines)
    for i, line in enumerate(lines):
        if line.strip().startswith("[") and i:
            at = i
            break
    lines.insert(at, f"{key}={value}")
    return head + "[params]" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Mix sessions
# ---------------------------------------------------------------------------
SESSION_VERSION = 1


def session_path(audio: str | os.PathLike[str]) -> Path:
    """Where a mix session lives: music.wav -> music.wav.mix.json.

    Keyed on the full filename, the way .import is. A stem-keyed name collided
    a WAV master with its streaming OGG — a normal pairing in a Godot project,
    and the exact thing "save as .ogg" produces — so one save silently
    overwrote the other's session.
    """
    p = Path(audio)
    return p.with_name(p.name + ".mix.json")


def _legacy_session_path(audio: str | os.PathLike[str]) -> Path:
    """The old stem-keyed name. Read-only, so existing sidecars still load."""
    p = Path(audio)
    return p.with_suffix("").with_name(p.stem + ".mix.json")


def existing_session_path(audio: str | os.PathLike[str]) -> Optional[Path]:
    """The session file on disk for this audio, preferring the new name."""
    for candidate in (session_path(audio), _legacy_session_path(audio)):
        if candidate.is_file():
            return candidate
    return None


def _track_num(raw: dict, i: int, key: str, lo: float, hi: float,
               default: float) -> float:
    """One numeric track field, range-checked, named in the error.

    Module level rather than a closure rebuilt on every track: it captured the
    loop's `raw` and `i`, which is only correct because it happens to be called
    inside the same iteration — a shape that reads as a latent bug and is one
    the moment anybody stores the callable.
    """
    try:
        v = float(raw.get(key, default))
    except (TypeError, ValueError):
        raise AudioError(f"tracks[{i}].{key} is not a number")
    if not lo <= v <= hi:
        raise AudioError(f"tracks[{i}].{key} is out of range [{lo}, {hi}]")
    return v


def normalise_session(data: dict) -> dict:
    """Validate a mixer session. Sources are project-relative and must stay so."""
    if not isinstance(data, dict):
        raise AudioError("a session must be an object")
    tracks = []
    for i, raw in enumerate(data.get("tracks") or []):
        if not isinstance(raw, dict):
            raise AudioError(f"tracks[{i}] must be an object")
        src = str(raw.get("source") or "").replace("\\", "/").strip()
        if not src:
            raise AudioError(f"tracks[{i}] has no source")
        if src.startswith("/") or ".." in src.split("/"):
            raise AudioError(f"tracks[{i}] source escapes the project: {src}")
        # in_s/out_s are seconds into the SOURCE file, not the timeline: the
        # kept region is [in_s, out_s) and it lands at offset_s. out_s is None
        # for "to the end", which is what every session written before this
        # existed means. Trimming stays non-destructive; the file is untouched.
        in_s = _track_num(raw, i, "in_s", 0.0, MAX_SECONDS, 0.0)
        raw_out = raw.get("out_s")
        if raw_out is None or raw_out == "":
            out_s = None
        else:
            try:
                out_s = float(raw_out)
            except (TypeError, ValueError):
                raise AudioError(f"tracks[{i}].out_s is not a number")
            if not 0.0 <= out_s <= MAX_SECONDS:
                raise AudioError(
                    f"tracks[{i}].out_s is out of range [0.0, {MAX_SECONDS}]")
            if out_s <= in_s:
                raise AudioError(f"tracks[{i}].out_s must be after in_s")
        tracks.append({
            "source": src,
            "name": str(raw.get("name") or Path(src).name)[:80],
            "offset_s": _track_num(raw, i, "offset_s", 0.0, MAX_SECONDS, 0.0),
            "in_s": in_s,
            "out_s": out_s,
            "gain_db": _track_num(raw, i, "gain_db", -60.0, 12.0, 0.0),
            "pan": _track_num(raw, i, "pan", -1.0, 1.0, 0.0),
            "muted": bool(raw.get("muted")),
            "solo": bool(raw.get("solo")),
            "reverse": bool(raw.get("reverse")),
        })
    if len(tracks) > 32:
        raise AudioError(f"{len(tracks)} tracks; the cap is 32")
    return {
        "version": SESSION_VERSION,
        "tracks": tracks,
        "sample_rate": int(data.get("sample_rate") or 44100),
        "notes": str(data.get("notes") or "")[:2000],
        "updated_at": None,
    }


# ---------------------------------------------------------------------------
# Beat sessions — the pattern, not the render
# ---------------------------------------------------------------------------
# A rendered beat is a WAV. A beat SESSION is the thing you actually want back
# tomorrow: the tempo, the grid, which steps are on, which voice each row is.
# Storing only the render is how a project ends up with forty loops nobody can
# change. The session is a sidecar next to the audio it renders to, so the two
# travel together.
BEAT_VERSION = 1
DRUM_VOICES = ("kick", "snare", "hat_closed", "hat_open", "clap", "tom",
               "rim", "cowbell")
SYNTH_VOICES = ("sine", "square", "saw", "triangle")
TRACK_KINDS = ("drum", "synth", "sample")

MAX_STEPS = 64
MAX_TRACKS = 16
MAX_PATTERNS = 8
MAX_SONG = 64


def beat_path(audio: str | os.PathLike[str]) -> Path:
    """music.wav -> music.wav.beat.json. Same stem-collision fix as
    session_path(); see the note there."""
    p = Path(audio)
    return p.with_name(p.name + ".beat.json")


def _legacy_beat_path(audio: str | os.PathLike[str]) -> Path:
    p = Path(audio)
    return p.with_suffix("").with_name(p.stem + ".beat.json")


def existing_beat_path(audio: str | os.PathLike[str]) -> Optional[Path]:
    for candidate in (beat_path(audio), _legacy_beat_path(audio)):
        if candidate.is_file():
            return candidate
    return None


def _beat_track(raw: dict, where: str, steps: int) -> dict:
    if not isinstance(raw, dict):
        raise AudioError(f"{where} must be an object")
    kind = str(raw.get("kind") or "drum")
    if kind not in TRACK_KINDS:
        raise AudioError(f"{where}.kind must be one of {TRACK_KINDS}")
    voice = str(raw.get("voice") or "")
    if kind == "drum" and voice not in DRUM_VOICES:
        raise AudioError(f"{where}.voice must be one of {DRUM_VOICES}")
    if kind == "synth" and voice not in SYNTH_VOICES:
        raise AudioError(f"{where}.voice must be one of {SYNTH_VOICES}")
    source = str(raw.get("source") or "").replace("\\", "/").strip()
    if kind == "sample":
        if not source:
            raise AudioError(f"{where} is a sample track with no source")
        if source.startswith("/") or ".." in source.split("/"):
            raise AudioError(f"{where}.source escapes the project: {source}")

    def _num(key, lo, hi, default):
        try:
            v = float(raw.get(key, default))
        except (TypeError, ValueError):
            raise AudioError(f"{where}.{key} is not a number")
        if not lo <= v <= hi:
            raise AudioError(f"{where}.{key} is out of range [{lo}, {hi}]")
        return v

    cells = raw.get("steps") or []
    if len(cells) > MAX_STEPS:
        raise AudioError(f"{where} has {len(cells)} steps; the cap is {MAX_STEPS}")
    out_steps = []
    for i in range(steps):
        cell = cells[i] if i < len(cells) and isinstance(cells[i], dict) else {}
        try:
            vel = float(cell.get("vel", 1.0))
            note = int(cell.get("note", 0))
        except (TypeError, ValueError):
            raise AudioError(f"{where}.steps[{i}] is malformed")
        if not 0.0 <= vel <= 1.0:
            raise AudioError(f"{where}.steps[{i}].vel is out of range [0, 1]")
        if not -36 <= note <= 36:
            raise AudioError(f"{where}.steps[{i}].note is out of range [-36, 36]")
        out_steps.append({"on": bool(cell.get("on")), "vel": vel, "note": note})

    return {
        "name": str(raw.get("name") or voice or "track")[:40],
        "kind": kind, "voice": voice, "source": source,
        "gain_db": _num("gain_db", -60.0, 12.0, 0.0),
        "pan": _num("pan", -1.0, 1.0, 0.0),
        "pitch": _num("pitch", -24.0, 24.0, 0.0),
        "decay": _num("decay", 0.05, 4.0, 1.0),
        "muted": bool(raw.get("muted")),
        "solo": bool(raw.get("solo")),
        "steps": out_steps,
    }


def normalise_beat(data: dict) -> dict:
    """Validate a beat session. Every bound here is a real one the UI enforces
    too — this is the copy that runs when the payload did not come from the UI."""
    if not isinstance(data, dict):
        raise AudioError("a beat session must be an object")
    try:
        bpm = float(data.get("bpm", 120))
        swing = float(data.get("swing", 0.0))
        steps = int(data.get("steps", 16))
        resolution = int(data.get("resolution", 4))
    except (TypeError, ValueError) as exc:
        raise AudioError(f"malformed tempo/grid: {exc}") from exc
    if not 20 <= bpm <= 300:
        raise AudioError(f"bpm {bpm} is outside 20-300")
    if not 0.0 <= swing <= 0.7:
        raise AudioError("swing is outside 0-0.7")
    if not 1 <= steps <= MAX_STEPS:
        raise AudioError(f"steps must be 1-{MAX_STEPS}")
    if resolution not in (1, 2, 3, 4, 6, 8):
        raise AudioError("resolution must be 1, 2, 3, 4, 6 or 8 steps per beat")

    patterns = []
    for pi, raw in enumerate(data.get("patterns") or []):
        if not isinstance(raw, dict):
            raise AudioError(f"patterns[{pi}] must be an object")
        tracks = [_beat_track(t, f"patterns[{pi}].tracks[{ti}]", steps)
                  for ti, t in enumerate(raw.get("tracks") or [])]
        if len(tracks) > MAX_TRACKS:
            raise AudioError(f"patterns[{pi}] has {len(tracks)} tracks; "
                             f"the cap is {MAX_TRACKS}")
        patterns.append({"name": str(raw.get("name") or chr(65 + pi))[:8],
                         "tracks": tracks})
    if not patterns:
        raise AudioError("a beat session needs at least one pattern")
    if len(patterns) > MAX_PATTERNS:
        raise AudioError(f"{len(patterns)} patterns; the cap is {MAX_PATTERNS}")

    names = [p["name"] for p in patterns]
    song = [str(s) for s in (data.get("song") or [])][:MAX_SONG]
    unknown = [s for s in song if s not in names]
    if unknown:
        raise AudioError(f"the song names patterns that do not exist: "
                         f"{sorted(set(unknown))}")
    return {
        "version": BEAT_VERSION, "bpm": bpm, "swing": swing, "steps": steps,
        "resolution": resolution,
        "master_gain_db": max(-60.0, min(12.0, float(data.get("master_gain_db", 0)))),
        "patterns": patterns, "song": song or [names[0]],
        "notes": str(data.get("notes") or "")[:2000],
        "updated_at": None,
    }


def beat_seconds(session: dict) -> float:
    """How long the song is, from the grid — the number the UI must agree with."""
    per_step = 60.0 / session["bpm"] / session["resolution"]
    return per_step * session["steps"] * max(1, len(session["song"]))


def load_beat(audio: str | os.PathLike[str]) -> Optional[dict]:
    path = existing_beat_path(audio)
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioError(f"{path.name} is unreadable: {exc}") from exc
    out = normalise_beat(raw)
    out["updated_at"] = raw.get("updated_at")
    return out


def save_beat(audio: str | os.PathLike[str], data: dict) -> dict:
    out = normalise_beat(data)
    out["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = beat_path(audio)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return out


def load_session(audio: str | os.PathLike[str]) -> Optional[dict]:
    path = existing_session_path(audio)
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioError(f"{path.name} is unreadable: {exc}") from exc
    out = normalise_session(raw)
    out["updated_at"] = raw.get("updated_at")
    return out


def save_session(audio: str | os.PathLike[str], data: dict) -> dict:
    out = normalise_session(data)
    out["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = session_path(audio)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return out
