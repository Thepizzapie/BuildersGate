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


def frozen() -> bool:
    """True inside the PyInstaller build, where sys.executable is the app."""
    return bool(getattr(sys, "frozen", False))


def whisper_python() -> str:
    """Which interpreter runs whisper. BGATE_WHISPER_PYTHON overrides.

    sys.executable is only an INTERPRETER when we are running from source. In
    the frozen build it is BuildersGate.exe, and handing that to
    `subprocess.run([exe, "-c", ...])` re-launches the whole application —
    which is exactly what happened: the doctor's whisper probe spawned thirteen
    copies of the app, each blocking for the full 60s timeout, and /api/doctor
    hung behind them. Frozen, there is no bundled interpreter to ask, so the
    honest answer is "" and the caller reports it as unavailable.
    """
    override = os.environ.get("BGATE_WHISPER_PYTHON")
    if override:
        return override
    # FROZEN, THIS RETURNS "" AND THAT IS DELIBERATE. sys.executable is
    # BuildersGate.exe in a frozen build, and this value is read by callers
    # that treat it as a PYTHON INTERPRETER — `[exe, "-c", ...]`. Handing them
    # the app means every probe launches another copy of the whole
    # application: it is exactly how a single /api/doctor call once put
    # thirteen windows on screen, and returning sys.executable here briefly
    # reintroduced it (ten windows, observed).
    #
    # The bundled runner is still reachable, just not through this function —
    # runner_cmd() spells the app + subcommand out explicitly, which cannot be
    # mistaken for an interpreter by anything else.
    return "" if frozen() else sys.executable


def runner_cmd(args: list[str]) -> list[str]:
    """The command line that runs the whisper runner, frozen or from source.

    Frozen there is no interpreter to hand a script path to, so the app calls
    itself with a subcommand. From source it is the ordinary
    ``python _whisper_runner.py ...``.
    """
    if frozen():
        # sys.executable directly, NOT whisper_python() — that returns "" when
        # frozen precisely so nothing else can spawn the app as an interpreter.
        return [sys.executable, "whisper", *args]
    return [whisper_python(), str(RUNNER), *args]


def available() -> dict:
    """Can we transcribe at all? Checked without loading a model.

    FROZEN IS CHECKED FIRST. whisper_python() answers "" in a frozen build by
    design (it must never hand the app out as an interpreter), so asking it
    before asking whether the bundle carries faster-whisper would report the
    feature missing on the one build that actually ships it.
    """
    if frozen():
        # No interpreter to probe with, and none needed: faster-whisper is IN
        # the bundle, so the question is simply whether it imports.
        try:
            import faster_whisper                                # noqa: F401
            from importlib.metadata import version
            return {"available": True, "python": "(bundled)",
                    "version": version("faster-whisper")}
        except Exception as exc:                                 # noqa: BLE001
            return {"available": False, "python": "",
                    "reason": f"speech-to-text is not in this build ({exc})"}

    exe = whisper_python()
    if not exe:
        return {
            "available": False,
            "python": "",
            # NO INSTALL INSTRUCTIONS HERE. This used to read "set
            # BGATE_WHISPER_PYTHON to an interpreter that has it, or run
            # Builders Gate from a source checkout" — advice a packaged user
            # cannot act on, telling them the install they chose was the wrong
            # one. Speech-to-text is genuinely not in the download (it is torch
            # and CUDA, hundreds of MB, for a transcript), so the honest
            # statement is what is unavailable and what still works.
            "reason": "speech-to-text is not included in the app download. "
                      "Recording still captures video and audio — there will "
                      "just be no searchable transcript.",
        }
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

    cmd = runner_cmd([str(wav_path), model, device, compute_type, language or "-"])
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
