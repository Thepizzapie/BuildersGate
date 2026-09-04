"""Optional, local music-source separation for Audio Lab.

The browser can edit and mix samples, but source separation is an inference
problem.  Keep the model stack out of the web process and invoke Demucs through
its supported command-line entry point when the optional dependency is present.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


class StemError(RuntimeError):
    """A source-separation run could not produce usable audio."""


class StemCancelled(StemError):
    """The user stopped an in-flight separation."""


PROFILES = {
    "vocals": {
        "label": "Voice + music",
        "description": "Two lanes: vocals and everything else.",
        "model": "htdemucs",
        "args": ("--two-stems", "vocals"),
        "stems": ("vocals", "no_vocals"),
    },
    "four": {
        "label": "Core four",
        "description": "Vocals, drums, bass, and the remaining instruments.",
        "model": "htdemucs",
        "args": (),
        "stems": ("vocals", "drums", "bass", "other"),
    },
    "six": {
        "label": "Expanded six",
        "description": "Adds guitar and piano lanes; piano extraction is experimental.",
        "model": "htdemucs_6s",
        "args": (),
        "stems": ("vocals", "drums", "bass", "guitar", "piano", "other"),
    },
}


def capability() -> dict:
    installed = importlib.util.find_spec("demucs") is not None
    return {
        "available": installed,
        "engine": "Demucs",
        "reason": "" if installed else
            "Install the optional stem engine with: pip install \"builders-gate[stems]\"",
        "profiles": [
            {"id": key, "label": value["label"],
             "description": value["description"], "stems": list(value["stems"])}
            for key, value in PROFILES.items()
        ],
    }


def separate(source: Path, work_dir: Path, target_dir: Path, profile: str,
             on_stage: Callable[[str], None] | None = None,
             cancelled: Callable[[], bool] | None = None) -> list[Path]:
    """Separate ``source`` and copy the resulting WAV stems to ``target_dir``."""
    spec = PROFILES.get(profile)
    if not spec:
        raise StemError(f"unknown stem profile: {profile}")
    if not capability()["available"]:
        raise StemError("the Demucs stem engine is not installed")

    model_out = work_dir / "model-output"
    model_out.mkdir(parents=True, exist_ok=True)
    if on_stage:
        on_stage("loading the separation model")
    command = [
        sys.executable, "-m", "demucs", "--out", str(model_out),
        "--name", str(spec["model"]), "--jobs", "1", *spec["args"], str(source),
    ]
    log_path = work_dir / "demucs.log"
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   text=True, creationflags=flags)
        deadline = time.monotonic() + 60 * 60
        while process.poll() is None:
            if cancelled and cancelled():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise StemCancelled("stem separation was cancelled")
            if time.monotonic() >= deadline:
                process.kill()
                raise StemError("stem separation exceeded the one-hour limit")
            time.sleep(0.25)
    if process.returncode:
        raise StemError(f"Demucs exited with code {process.returncode}")

    produced = model_out / str(spec["model"]) / source.stem
    if not produced.is_dir():
        candidates = [p for p in model_out.rglob(source.stem) if p.is_dir()]
        produced = candidates[0] if candidates else produced
    waves = {p.stem: p for p in produced.glob("*.wav")}
    expected = list(spec["stems"])
    missing = [name for name in expected if name not in waves]
    if missing:
        raise StemError("the stem engine did not produce: " + ", ".join(missing))

    if on_stage:
        on_stage("filing separated lanes into the project")
    target_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for name in expected:
        dest = target_dir / f"{name}.wav"
        shutil.copy2(waves[name], dest)
        result.append(dest)
    return result
