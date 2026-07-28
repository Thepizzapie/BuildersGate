"""Speech-to-text via faster-whisper, always in a subprocess.

Never import faster_whisper into the server process. It loads a large model and
pins a core for the duration of the audio; inline in FastMCP's async loop that
stalls every other tool call. The runner is a separate process by design.

Model choice: 'base' is the default because a playtest is one close-mic voice
saying ordinary words, and base transcribes ~10 minutes in well under a minute on
CPU. Reach for 'small' when jargon and proper nouns matter.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

RUNNER = Path(__file__).with_name("_whisper_runner.py")
MODELS = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT_MODEL = "base"


def whisper_python() -> str:
    """Which interpreter runs whisper. BGATE_WHISPER_PYTHON overrides."""
    return os.environ.get("BGATE_WHISPER_PYTHON") or sys.executable


def available() -> dict:
    """Can we transcribe at all? Checked without loading a model."""
    exe = whisper_python()
    probe = ("import importlib.metadata as m;"
             "print(m.version('faster-whisper'))")
    try:
        proc = subprocess.run([exe, "-c", probe], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {
            "available": False,
            "python": exe,
            "reason": "faster-whisper not installed for this interpreter — "
                      "pip install faster-whisper, or set BGATE_WHISPER_PYTHON",
        }
    return {"available": True, "python": exe, "version": proc.stdout.strip()}


def transcribe(wav_path: str, *, model: str = DEFAULT_MODEL, device: str = "auto",
               compute_type: str = "auto", language: Optional[str] = None,
               timeout: int = 1800) -> dict:
    """Transcribe a wav into timestamped segments.

    Returns {ok, segments:[{t_start,t_end,text,confidence}], language, ...}.
    Timestamps are relative to the START OF THE WAV — callers must add the
    session's audio_offset_s to put them on the session clock.

    First call downloads the model (~150MB for base) from HuggingFace and caches
    it in ~/.cache/huggingface. Subsequent calls are offline.
    """
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}, got {model!r}")
    if not Path(wav_path).exists():
        raise FileNotFoundError(f"no audio at {wav_path}")
    if Path(wav_path).stat().st_size < 1024:
        return {"ok": False, "error": "audio file is empty — nothing was recorded"}

    cmd = [whisper_python(), str(RUNNER), str(wav_path), model, device,
           compute_type, language or "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "error": f"transcription timed out after {timeout}s",
                "hint": "first run downloads the model — retry, or use a smaller one"}

    if not proc.stdout.strip():
        return {"ok": False, "error": "transcriber produced no output",
                "stderr": (proc.stderr or "")[-500:], "exit_code": proc.returncode}
    try:
        # The runner prints one JSON line, but model loaders are chatty on stdout;
        # take the last line that parses.
        for line in reversed(proc.stdout.strip().splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return {"ok": False, "error": "no JSON in transcriber output",
                "stdout": proc.stdout[-500:]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
