"""Procedural game SFX — synthesis, and the recipe that rebuilds the render.

WHY SYNTHESIS AND NOT A PROVIDER. The audio seat's mission opens "Own SFX and
music hooks" and every audio tool it had was a paid one — hosted music, hosted
speech — so a project with no key could not produce a single game sound. SFX are
the one asset class where that is not a compromise: a coin pickup, a laser, a
jump are four oscillator parameters and an envelope, and a synthesized one is
SHORTER, cleaner, loopable, and adjustable by the knob you wanted to move. A
generated one is a 3-second stereo clip of a room with a coin in it. So this
module has no provider, no key, no network, and no failure mode that begins
"the API".

THE RECIPE IS THE ASSET; THE WAV IS A RENDER OF IT. dispatch.SEAT_RULES["audio"]
requires every synthesized asset to ship a ``<name>.synth.json`` sidecar holding
the full parametric recipe, because a .wav whose knobs are lost is a dead end —
nobody can nudge the decay of a file. Everything needed to reproduce the exact
bytes lives in that sidecar: sample rate, master peak, seed, and every layer's
wave/sweep/envelope/filter. :func:`rerender` takes the sidecar ALONE and gets
the same file back, which is the property that makes the rule real rather than
aspirational.

DETERMINISM. The only non-arithmetic input is the noise stream, and it is a
``random.Random(seed)`` derived per layer from the recipe's seed — never the
module-level RNG, whose sequence depends on everything else the process did.
The seed itself defaults to a hash of ``kind:name``, so regenerating "coin"
tomorrow produces the identical file rather than a near-miss nobody can diff.
The recipe carries no timestamp for the same reason: two runs must produce two
identical sidecars, or the sidecar is not a recipe, it is a log entry.

Pure stdlib (``wave``, ``math``, ``random``, ``array``). 16-bit mono PCM,
because that is what Godot's WAV importer accepts — AudioStreamWAV is 8/16-bit
PCM (or IMA-ADPCM/QOA), and a float WAV imports as silence with no error.

Waveforms are NAIVE, not band-limited. The aliasing is the sound: a band-limited
square at 200 Hz is a polite sine, and what a 2D game wants is the buzz.
"""
from __future__ import annotations

import array
import hashlib
import io
import json
import math
import os
import random
import sys
import wave
from pathlib import Path
from typing import Optional

from .project import game_dir

RECIPE_VERSION = 1
GENERATOR = "bgate.sfx/1"

DEFAULT_RATE = 44100
# 44100 rather than a retro 22050: these files get mixed and trimmed next to
# music in the audio lab, and one rate through the whole project means nothing
# downstream has to resample (audiolab's mixer sessions default to 44100 too).

WAVES = ("sine", "square", "saw", "triangle", "noise")
SWEEPS = ("exp", "lin")

MAX_SECONDS = 10.0            # an SFX past this is music; use the music path
MAX_LAYERS = 12
MIN_RATE, MAX_RATE = 8000, 48000
MAX_NAME = 48

SIDECAR_SUFFIX = ".synth.json"


class SfxError(ValueError):
    """A recipe or a name this module refuses to render."""


# ---------------------------------------------------------------------------
# Presets — the kinds a game actually asks for
# ---------------------------------------------------------------------------
# Each entry is (description, nominal seconds, base Hz, layer list). The layers
# are TEMPLATES: base_hz scales every tonal frequency and duration_s scales
# every time, so `sfx_generate("laser", base_hz=600)` is a bigger gun rather
# than a different tool. Frequencies below are written as MULTIPLES of the
# preset's base so that scaling is one multiplication and not a table of
# special cases.
_PRESETS: dict[str, dict] = {
    "blip": {
        "about": "UI select/confirm — one short square beep. Menus, cursors.",
        "base_hz": 880.0,
        "layers": [
            {"wave": "square", "duty": 0.5, "start_s": 0.0, "duration_s": 0.075,
             "freq_start": 1.0, "attack_s": 0.002, "release_s": 0.05,
             "sustain": 0.85, "gain": 0.7},
        ],
    },
    "pickup": {
        "about": "Coin/pickup — a short blip that steps up a fifth and rings.",
        "base_hz": 660.0,
        "layers": [
            {"wave": "square", "duty": 0.5, "start_s": 0.0, "duration_s": 0.055,
             "freq_start": 1.0, "attack_s": 0.002, "release_s": 0.012,
             "gain": 0.6},
            {"wave": "square", "duty": 0.5, "start_s": 0.052, "duration_s": 0.30,
             "freq_start": 1.5, "attack_s": 0.002, "release_s": 0.24,
             "sustain": 0.8, "gain": 0.6},
        ],
    },
    "jump": {
        "about": "Jump — square with a fast rising sweep. Platformer takeoff.",
        "base_hz": 200.0,
        "layers": [
            {"wave": "square", "duty": 0.45, "start_s": 0.0, "duration_s": 0.22,
             "freq_start": 1.0, "freq_end": 3.2, "sweep": "exp",
             "attack_s": 0.004, "release_s": 0.13, "sustain": 0.75, "gain": 0.7},
        ],
    },
    "laser": {
        "about": "Laser/shoot — falling saw sweep under a closing filter.",
        "base_hz": 1400.0,
        "layers": [
            {"wave": "saw", "start_s": 0.0, "duration_s": 0.28,
             "freq_start": 1.0, "freq_end": 0.13, "sweep": "exp",
             "attack_s": 0.002, "release_s": 0.22, "sustain": 0.7,
             "lowpass_start_hz": 6000.0, "lowpass_end_hz": 900.0, "gain": 0.65},
        ],
    },
    "explosion": {
        "about": "Explosion — filtered noise burst over a falling sine thump.",
        "base_hz": 120.0,
        "layers": [
            {"wave": "noise", "start_s": 0.0, "duration_s": 0.90,
             "attack_s": 0.004, "decay_s": 0.10, "sustain": 0.55,
             "release_s": 0.75,
             "lowpass_start_hz": 4200.0, "lowpass_end_hz": 180.0, "gain": 0.9},
            {"wave": "sine", "start_s": 0.0, "duration_s": 0.50,
             "freq_start": 1.0, "freq_end": 0.33, "sweep": "exp",
             "attack_s": 0.002, "release_s": 0.46, "gain": 0.8},
        ],
    },
    "hit": {
        "about": "Hit/thud — a clipped noise transient on a low triangle body.",
        "base_hz": 170.0,
        "layers": [
            {"wave": "noise", "start_s": 0.0, "duration_s": 0.085,
             "attack_s": 0.001, "release_s": 0.07, "sustain": 0.5,
             "lowpass_start_hz": 3200.0, "lowpass_end_hz": 700.0, "gain": 0.7},
            {"wave": "triangle", "start_s": 0.0, "duration_s": 0.16,
             "freq_start": 1.0, "freq_end": 0.42, "sweep": "exp",
             "attack_s": 0.001, "release_s": 0.14, "gain": 0.85},
        ],
    },
    "powerup": {
        "about": "Powerup — an ascending arpeggio that lands on a ringing top.",
        "base_hz": 330.0,
        "layers": [
            {"wave": "square", "duty": 0.5, "start_s": 0.00, "duration_s": 0.07,
             "freq_start": 1.0, "attack_s": 0.002, "release_s": 0.02, "gain": 0.5},
            {"wave": "square", "duty": 0.5, "start_s": 0.07, "duration_s": 0.07,
             "freq_start": 1.26, "attack_s": 0.002, "release_s": 0.02, "gain": 0.5},
            {"wave": "square", "duty": 0.5, "start_s": 0.14, "duration_s": 0.07,
             "freq_start": 1.50, "attack_s": 0.002, "release_s": 0.02, "gain": 0.5},
            {"wave": "square", "duty": 0.5, "start_s": 0.21, "duration_s": 0.36,
             "freq_start": 2.0, "attack_s": 0.002, "release_s": 0.30,
             "sustain": 0.8, "vibrato_hz": 14.0, "vibrato_cents": 25.0,
             "gain": 0.55},
        ],
    },
    "sweep": {
        "about": "Rising whoosh — filtered noise sweeping up. Transitions, dashes.",
        "base_hz": 300.0,
        "layers": [
            {"wave": "noise", "start_s": 0.0, "duration_s": 0.55,
             "attack_s": 0.20, "sustain": 1.0, "release_s": 0.18,
             "lowpass_start_hz": 400.0, "lowpass_end_hz": 5200.0, "gain": 0.8},
        ],
    },
}

# The words people reach for that are not the preset's name. A kind nobody can
# spell is a kind nobody uses, and "shoot" is what a designer writes on a card.
ALIASES = {
    "select": "blip", "beep": "blip", "click": "blip", "menu": "blip",
    "coin": "pickup", "collect": "pickup", "gem": "pickup",
    "shoot": "laser", "shot": "laser", "zap": "laser", "fire": "laser",
    "boom": "explosion", "blast": "explosion", "noise_burst": "explosion",
    "thud": "hit", "punch": "hit", "impact": "hit", "hurt": "hit",
    "power_up": "powerup", "levelup": "powerup", "level_up": "powerup",
    "whoosh": "sweep", "dash": "sweep", "transition": "sweep",
}

KINDS = tuple(_PRESETS)


def kinds() -> list[dict]:
    """Every kind, what it is for, and how long it comes out by default."""
    out = []
    for kind, preset in _PRESETS.items():
        length = max(l.get("start_s", 0.0) + l.get("duration_s", 0.0)
                     for l in preset["layers"])
        out.append({
            "kind": kind,
            "about": preset["about"],
            "seconds": round(length, 3),
            "base_hz": preset["base_hz"],
            "layers": len(preset["layers"]),
            "aliases": sorted(a for a, k in ALIASES.items() if k == kind),
        })
    return out


def resolve_kind(kind: str) -> str:
    """Preset name for what the caller asked for. Raises naming the options."""
    key = str(kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    key = ALIASES.get(key, key)
    if key not in _PRESETS:
        raise SfxError(f"no SFX kind {kind!r} — known kinds: {', '.join(KINDS)} "
                       f"(aliases: {', '.join(sorted(ALIASES))})")
    return key


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------
def default_seed(kind: str, name: str) -> int:
    """A seed derived from what this sound IS, not from the clock.

    Regenerating "coin" next week has to produce the identical file — otherwise
    every re-run is a diff nobody asked for, and the sidecar stops being a
    recipe. sha256 rather than hash(): PYTHONHASHSEED randomises str hashing per
    process, so hash() would have made the DEFAULT non-deterministic across runs
    while looking fine inside one.
    """
    digest = hashlib.sha256(f"{kind}:{name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def _num(raw: dict, key: str, lo: float, hi: float, default: float,
         where: str) -> float:
    try:
        value = float(raw.get(key, default))
    except (TypeError, ValueError):
        raise SfxError(f"{where}.{key} is not a number")
    if not lo <= value <= hi:
        raise SfxError(f"{where}.{key} is out of range [{lo}, {hi}]")
    return value


def _layer(raw: dict, index: int) -> dict:
    """One validated layer. Every bound here is real — an unclamped release
    longer than the layer silently produced a click at the join."""
    where = f"layers[{index}]"
    if not isinstance(raw, dict):
        raise SfxError(f"{where} must be an object")
    shape = str(raw.get("wave") or "sine")
    if shape not in WAVES:
        raise SfxError(f"{where}.wave must be one of {WAVES}")
    sweep = str(raw.get("sweep") or "exp")
    if sweep not in SWEEPS:
        raise SfxError(f"{where}.sweep must be one of {SWEEPS}")
    # freq_end 0 means "no sweep" rather than "sweep to DC": an exponential
    # sweep to zero has no finite ratio, and a linear one ends in silence.
    freq_start = _num(raw, "freq_start", 1.0, 20000.0, 440.0, where)
    freq_end = _num(raw, "freq_end", 0.0, 20000.0, 0.0, where)
    return {
        "wave": shape,
        "start_s": _num(raw, "start_s", 0.0, MAX_SECONDS, 0.0, where),
        "duration_s": _num(raw, "duration_s", 0.001, MAX_SECONDS, 0.2, where),
        "freq_start": freq_start,
        "freq_end": freq_end,
        "sweep": sweep,
        "duty": _num(raw, "duty", 0.05, 0.95, 0.5, where),
        "gain": _num(raw, "gain", 0.0, 1.0, 0.7, where),
        "attack_s": _num(raw, "attack_s", 0.0, MAX_SECONDS, 0.005, where),
        "decay_s": _num(raw, "decay_s", 0.0, MAX_SECONDS, 0.0, where),
        "sustain": _num(raw, "sustain", 0.0, 1.0, 1.0, where),
        "release_s": _num(raw, "release_s", 0.0, MAX_SECONDS, 0.05, where),
        "noise_mix": _num(raw, "noise_mix", 0.0, 1.0, 0.0, where),
        "vibrato_hz": _num(raw, "vibrato_hz", 0.0, 200.0, 0.0, where),
        "vibrato_cents": _num(raw, "vibrato_cents", 0.0, 1200.0, 0.0, where),
        "lowpass_start_hz": _num(raw, "lowpass_start_hz", 0.0, 20000.0, 0.0, where),
        "lowpass_end_hz": _num(raw, "lowpass_end_hz", 0.0, 20000.0, 0.0, where),
        # 0 = off. Anything from 3 to 8 is the retro sound on purpose.
        "bits": int(_num(raw, "bits", 0, 16, 0, where)),
    }


def normalise(data: dict) -> dict:
    """Validate a recipe. This is the copy that runs when the payload did not
    come from :func:`recipe` — a hand-edited sidecar is a supported input."""
    if not isinstance(data, dict):
        raise SfxError("a recipe must be an object")
    version = int(data.get("version") or RECIPE_VERSION)
    if version > RECIPE_VERSION:
        raise SfxError(
            f"recipe version {version} was written by a newer Builders Gate "
            f"than this one (understands {RECIPE_VERSION})")
    raw_layers = data.get("layers") or []
    if not raw_layers:
        raise SfxError("a recipe needs at least one layer")
    if len(raw_layers) > MAX_LAYERS:
        raise SfxError(f"{len(raw_layers)} layers; the cap is {MAX_LAYERS}")
    layers = [_layer(raw, i) for i, raw in enumerate(raw_layers)]

    rate = int(data.get("sample_rate") or DEFAULT_RATE)
    if not MIN_RATE <= rate <= MAX_RATE:
        raise SfxError(f"sample rate {rate} is outside {MIN_RATE}-{MAX_RATE}")
    total = max(l["start_s"] + l["duration_s"] for l in layers)
    if total > MAX_SECONDS:
        raise SfxError(f"{total:.2f}s of sound; the cap is {MAX_SECONDS:.0f}s — "
                       "anything longer is music, not an effect")
    kind = str(data.get("kind") or "custom")
    return {
        "version": RECIPE_VERSION,
        "generator": GENERATOR,
        "kind": kind,
        "name": str(data.get("name") or kind)[:MAX_NAME],
        "sample_rate": rate,
        "seed": int(data.get("seed", default_seed(kind, str(data.get("name") or ""))))
                & 0xFFFFFFFF,
        "peak": _num(data, "peak", 0.05, 1.0, 0.89, "recipe"),
        # Every render ends on a ramp to zero. A hard cut at a non-zero sample
        # is a click, and a click on a sound that fires ten times a second is
        # the thing that makes a game sound cheap.
        "tail_fade_s": _num(data, "tail_fade_s", 0.0, 0.5, 0.004, "recipe"),
        "seconds": round(total, 4),
        "layers": layers,
    }


def recipe(kind: str, *, name: str = "", seed: Optional[int] = None,
           base_hz: float = 0.0, duration_s: float = 0.0, gain: float = 1.0,
           sample_rate: int = DEFAULT_RATE, bits: int = 0) -> dict:
    """A full, self-contained recipe for one preset, with the knobs applied.

    base_hz and duration_s SCALE the preset rather than replacing it: every
    tonal frequency is a multiple of the preset's base and every time is a
    fraction of its nominal length, so a laser at base_hz=600 is the same
    gesture from a bigger gun instead of an unrelated sound.
    """
    resolved = resolve_kind(kind)
    preset = _PRESETS[resolved]
    label = str(name or resolved)[:MAX_NAME]

    base = float(base_hz) if base_hz and base_hz > 0 else float(preset["base_hz"])
    nominal = max(l.get("start_s", 0.0) + l.get("duration_s", 0.0)
                  for l in preset["layers"])
    stretch = (float(duration_s) / nominal) if duration_s and duration_s > 0 else 1.0
    if stretch <= 0:
        raise SfxError("duration_s must be positive")
    level = float(gain) if gain and gain > 0 else 1.0

    layers = []
    for template in preset["layers"]:
        layer = dict(template)
        for key in ("start_s", "duration_s", "attack_s", "decay_s", "release_s"):
            if key in layer:
                layer[key] = layer[key] * stretch
        for key in ("freq_start", "freq_end"):
            if layer.get(key):
                layer[key] = layer[key] * base
        # A noise layer has no pitch, and _layer's floor on freq_start would
        # reject the 0.0 the template omits — give it the base and ignore it.
        layer.setdefault("freq_start", base)
        layer["gain"] = min(1.0, layer.get("gain", 0.7) * level)
        if bits:
            layer["bits"] = int(bits)
        layers.append(layer)

    return normalise({
        "kind": resolved,
        "name": label,
        "sample_rate": int(sample_rate or DEFAULT_RATE),
        "seed": default_seed(resolved, label) if seed is None else int(seed),
        "layers": layers,
    })


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------
def _envelope(t: float, dur: float, attack: float, decay: float,
              sustain: float, release: float) -> float:
    """ADSR sampled at t seconds into a layer of `dur` seconds.

    A/D/R are scaled down together when they overrun the layer, rather than
    truncated: truncating the release is exactly the hard cut that clicks.
    """
    span = attack + decay + release
    if span > dur and span > 0:
        scale = dur / span
        attack, decay, release = attack * scale, decay * scale, release * scale
    if attack > 0 and t < attack:
        return t / attack
    if decay > 0 and t < attack + decay:
        return 1.0 + (sustain - 1.0) * ((t - attack) / decay)
    # No decay stage means the sustain level applies the moment the attack
    # finishes, which is the shape every one-shot preset here wants: a blip is
    # a step, not a ramp to a plateau it never reaches.
    level = sustain
    tail = dur - release
    if release > 0 and t >= tail:
        return level * max(0.0, (dur - t) / release)
    return level


def _osc(shape: str, phase: float, duty: float, rng: random.Random) -> float:
    if shape == "noise":
        return rng.uniform(-1.0, 1.0)
    p = phase % 1.0
    if shape == "sine":
        return math.sin(2.0 * math.pi * p)
    if shape == "square":
        return 1.0 if p < duty else -1.0
    if shape == "saw":
        return 2.0 * p - 1.0
    return 4.0 * abs(p - 0.5) - 1.0            # triangle


def _layer_samples(layer: dict, rate: int, seed: int, index: int) -> list[float]:
    """Render one layer to float samples in [-1, 1]."""
    count = max(1, int(round(layer["duration_s"] * rate)))
    # Per-layer stream, derived from the recipe seed. Never the module RNG:
    # its sequence depends on everything else the process did, which would make
    # two identical recipes render two different explosions.
    rng = random.Random((seed * 1000003 + index) & 0xFFFFFFFF)
    noise_rng = random.Random((seed * 2654435761 + index * 97 + 7) & 0xFFFFFFFF)

    shape = layer["wave"]
    f0 = layer["freq_start"]
    f1 = layer["freq_end"]
    swept = f1 > 0.0 and f1 != f0
    exp_sweep = layer["sweep"] == "exp"
    duty, gain = layer["duty"], layer["gain"]
    vib_hz, vib_cents = layer["vibrato_hz"], layer["vibrato_cents"]
    lp0, lp1 = layer["lowpass_start_hz"], layer["lowpass_end_hz"]
    filtered = lp0 > 0.0 or lp1 > 0.0
    if filtered:                       # one end left at 0 means "hold the other"
        lp0 = lp0 or lp1
        lp1 = lp1 or lp0
    quant = (1 << (layer["bits"] - 1)) if layer["bits"] >= 2 else 0

    dt = 1.0 / rate
    out: list[float] = []
    phase = 0.0
    filter_state = 0.0
    span = max(1, count - 1)
    for i in range(count):
        t = i * dt
        progress = i / span
        if swept:
            freq = (f0 * (f1 / f0) ** progress if exp_sweep
                    else f0 + (f1 - f0) * progress)
        else:
            freq = f0
        if vib_hz > 0.0 and vib_cents > 0.0:
            freq *= 2.0 ** ((vib_cents / 1200.0)
                            * math.sin(2.0 * math.pi * vib_hz * t))
        value = _osc(shape, phase, duty, rng)
        phase += freq * dt
        if layer["noise_mix"] > 0.0:
            value = ((1.0 - layer["noise_mix"]) * value
                     + layer["noise_mix"] * noise_rng.uniform(-1.0, 1.0))
        if filtered:
            cutoff = lp0 + (lp1 - lp0) * progress
            # One-pole RC lowpass, coefficient recomputed per sample so the
            # cutoff can sweep — the closing filter IS the explosion.
            rc = 1.0 / (2.0 * math.pi * max(cutoff, 1.0))
            alpha = dt / (rc + dt)
            filter_state += alpha * (value - filter_state)
            value = filter_state
        value *= _envelope(t, layer["duration_s"], layer["attack_s"],
                           layer["decay_s"], layer["sustain"],
                           layer["release_s"]) * gain
        if quant:
            value = round(value * quant) / quant
        out.append(value)
    return out


def render(data: dict) -> bytes:
    """Recipe -> 16-bit mono PCM WAV bytes. The whole render path, no I/O."""
    rec = normalise(data)
    rate = rec["sample_rate"]
    total = int(round(rec["seconds"] * rate)) + 1
    buf = [0.0] * total

    for index, layer in enumerate(rec["layers"]):
        start = int(round(layer["start_s"] * rate))
        for offset, value in enumerate(_layer_samples(layer, rate, rec["seed"],
                                                      index)):
            at = start + offset
            if 0 <= at < total:
                buf[at] += value

    peak = max((abs(v) for v in buf), default=0.0)
    if peak > 0.0:
        # Normalised rather than clipped. Summed layers routinely exceed 1.0,
        # and clipping a mix is the difference between a punchy hit and a
        # crunch — one gain stage at the end keeps every kind at one loudness.
        scale = rec["peak"] / peak
        buf = [v * scale for v in buf]

    fade = min(int(round(rec["tail_fade_s"] * rate)), total)
    for n in range(fade):
        buf[total - fade + n] *= (fade - 1 - n) / max(fade - 1, 1)

    pcm = array.array(
        "h", (max(-32768, min(32767, int(round(v * 32767.0)))) for v in buf))
    if sys.byteorder == "big":
        pcm.byteswap()               # WAV is little-endian, this host may not be
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return out.getvalue()


# ---------------------------------------------------------------------------
# Files — the wav, and the sidecar that can rebuild it
# ---------------------------------------------------------------------------
def sidecar_path(wav: str | os.PathLike[str]) -> Path:
    """coin.wav -> coin.synth.json.

    Keyed on the STEM, unlike audiolab's mix sessions which key on the whole
    filename to survive a wav/ogg pair of one track. The audio house rule spells
    this one ``<name>.synth.json`` verbatim, and an SFX has exactly one render,
    so there is no pair to collide.
    """
    return Path(wav).with_suffix(SIDECAR_SUFFIX)


def safe_name(name: str) -> str:
    """A filename that cannot escape the directory it was aimed at."""
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_"
                      for c in str(name or "").strip())[:MAX_NAME].strip("_")
    if not cleaned:
        raise SfxError("a name is required — it becomes the .wav filename")
    return cleaned


def sfx_dir(root: str | os.PathLike[str]) -> Path:
    """Where SFX land: inside the AUDIO SEAT'S OWN LANE, for both layouts.

    ``bgate init`` puts project.godot at the root and godot_scaffold puts it in
    ``<root>/game``; seats.lanes_for_layout re-roots the lane globs to match, so
    the same directory relative to the GODOT project is inside the lane either
    way. Hardcoding "game/assets/audio" would have written outside the lane —
    and been refused by the write hook — for every CLI-created project.
    """
    base = game_dir(root) or (Path(root) / "game")
    return base / "assets" / "audio" / "sfx"


def write(rec: dict, wav_path: str | os.PathLike[str]) -> dict:
    """Render a recipe to disk and drop its ``<name>.synth.json`` beside it.

    The sidecar is written FIRST-CLASS, not best-effort: an audio asset without
    its recipe violates the seat's house rule and is a file nobody can adjust,
    so a sidecar that cannot be written fails the whole call rather than leaving
    an orphan wav behind.
    """
    rec = normalise(rec)
    target = Path(wav_path)
    if target.suffix.lower() != ".wav":
        raise SfxError(f"{target.name} — synthesis writes .wav (16-bit mono PCM, "
                       "which is what Godot's WAV importer accepts)")
    audio = render(rec)
    target.parent.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_path(target)
    try:
        target.write_bytes(audio)
        sidecar.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise SfxError(f"could not write {target.name}: {exc}") from exc
    return {
        "path": str(target),
        "recipe_path": str(sidecar),
        "bytes": len(audio),
        "seconds": rec["seconds"],
        "sample_rate": rec["sample_rate"],
        "seed": rec["seed"],
        "recipe": rec,
    }


def generate(root: str | os.PathLike[str], kind: str, name: str, *,
             out_dir: Optional[str] = None, seed: Optional[int] = None,
             base_hz: float = 0.0, duration_s: float = 0.0, gain: float = 1.0,
             sample_rate: int = DEFAULT_RATE, bits: int = 0) -> dict:
    """Synthesize one SFX into the project, with its recipe beside it."""
    label = safe_name(name)
    rec = recipe(kind, name=label, seed=seed, base_hz=base_hz,
                 duration_s=duration_s, gain=gain, sample_rate=sample_rate,
                 bits=bits)
    directory = Path(out_dir) if out_dir else sfx_dir(root)
    if not directory.is_absolute():
        directory = Path(root) / directory
    result = write(rec, directory / f"{label}.wav")
    result["kind"] = rec["kind"]
    result["name"] = label
    result["rel_path"] = _relative(root, result["path"])
    result["recipe_rel_path"] = _relative(root, result["recipe_path"])
    # The res:// path is what a designer pastes into a scene, and computing it
    # from the GODOT project (not the bgate root) is the only version that is
    # right for both layouts.
    result["res_path"] = _res_path(root, result["path"])
    return result


def rerender(recipe_path: str | os.PathLike[str],
             out_path: Optional[str] = None) -> dict:
    """Rebuild the wav from the SIDECAR ALONE — the property the rule demands.

    Reports ``identical``: whether the bytes match what is already on disk. That
    is the assertion that keeps the recipe honest, because a recipe that renders
    something ELSE is worse than no recipe at all — it looks like provenance.
    """
    path = Path(recipe_path)
    if not path.is_file():
        raise SfxError(f"no recipe at {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SfxError(f"{path.name} is unreadable: {exc}") from exc
    rec = normalise(raw)

    target = (Path(out_path) if out_path
              else path.with_name(path.name[:-len(SIDECAR_SUFFIX)] + ".wav"))
    before = target.read_bytes() if target.is_file() else b""
    audio = render(rec)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(audio)
    return {
        "path": str(target),
        "recipe_path": str(path),
        "bytes": len(audio),
        "seconds": rec["seconds"],
        "kind": rec["kind"],
        "name": rec["name"],
        "identical": bool(before) and before == audio,
        "had_previous": bool(before),
        "recipe": rec,
    }


def list_sfx(root: str | os.PathLike[str],
             directory: Optional[str] = None) -> list[dict]:
    """Every synthesized effect in the project's SFX directory, recipe or not.

    A wav with no sidecar is REPORTED rather than hidden: it is precisely the
    file the house rule exists to prevent, and the seat can only fix what it can
    see.
    """
    base = Path(directory) if directory else sfx_dir(root)
    if not base.is_dir():
        return []
    out = []
    for wav in sorted(base.glob("*.wav")):
        sidecar = sidecar_path(wav)
        entry = {"name": wav.stem, "path": str(wav),
                 "rel_path": _relative(root, str(wav)),
                 "bytes": wav.stat().st_size,
                 "has_recipe": sidecar.is_file(), "kind": "", "seconds": None}
        if sidecar.is_file():
            try:
                rec = json.loads(sidecar.read_text(encoding="utf-8"))
                entry["kind"] = str(rec.get("kind") or "")
                entry["seconds"] = rec.get("seconds")
                entry["recipe_path"] = str(sidecar)
            except (OSError, ValueError):
                entry["has_recipe"] = False
        out.append(entry)
    return out


def _relative(root: str | os.PathLike[str], path: str) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def _res_path(root: str | os.PathLike[str], path: str) -> str:
    base = game_dir(root)
    if base is None:
        return ""
    try:
        return "res://" + Path(path).resolve().relative_to(
            Path(base).resolve()).as_posix()
    except ValueError:
        return ""
