"""Transcription against REAL whisper, on REAL synthesized speech.

No mic is needed: Windows SAPI writes a wav, whisper reads it back. Mocking the
model would test nothing — the risk lives in the subprocess boundary, the JSON
contract, and whether timestamps come back usable.

Slow (model load + inference), so it's marked and skipped when unavailable.
"""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import pytest

from bgate_adapters import transcribe

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not transcribe.available().get("available"),
                       reason="faster-whisper not installed"),
]

SPOKEN = "The jump feels really floaty. I do not like it."


def _say(text: str, out: Path) -> bool:
    """Synthesize speech to a 16kHz mono wav via Windows SAPI."""
    if sys.platform != "win32":
        return False
    script = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, 'Sixteen', 'Mono')
$s.SetOutputToWaveFile('{out}', $fmt)
$s.Rate = -1
$s.Speak('{text}')
$s.Dispose()
"""
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)
    return out.exists() and out.stat().st_size > 1024


@pytest.fixture(scope="module")
def spoken_wav(tmp_path_factory):
    out = tmp_path_factory.mktemp("tts") / "spoken.wav"
    if not _say(SPOKEN, out):
        pytest.skip("no SAPI voice available to synthesize test audio")
    return out


class TestDeviceFallback:
    """ctranslate2's 'auto' picks CUDA on any NVIDIA box, then dies at inference
    if the CUDA libs aren't installed — which is most machines. The runner must
    fall back to CPU rather than fail the session."""

    def test_auto_lands_on_a_working_device(self, spoken_wav):
        got = transcribe.transcribe(str(spoken_wav), model="base", device="auto")
        assert got["ok"] is True, got.get("error")
        assert got["device"] in ("cuda", "cpu")

    def test_fallback_is_reported_not_silent(self, spoken_wav):
        got = transcribe.transcribe(str(spoken_wav), model="base", device="auto")
        if got["device"] == "cpu" and got.get("fallback"):
            assert "unusable" in got["fallback"]


class TestAvailability:
    def test_reports_python_and_version(self):
        got = transcribe.available()
        assert got["available"] is True
        assert got["version"]

    def test_missing_interpreter_is_explained(self, monkeypatch):
        monkeypatch.setenv("BGATE_WHISPER_PYTHON", "definitely-not-a-python")
        assert transcribe.available()["available"] is False


class TestTranscribe:
    def test_transcribes_real_speech(self, spoken_wav):
        got = transcribe.transcribe(str(spoken_wav), model="base")
        assert got["ok"] is True, got.get("error")
        assert got["segments"], "whisper returned no segments for real speech"

        text = " ".join(s["text"] for s in got["segments"]).lower()
        # Don't demand a perfect transcript — demand the words that carry meaning.
        assert "jump" in text
        assert "float" in text  # floaty / floating
        assert got["language"] == "en"

    def test_timestamps_are_ordered_and_within_the_audio(self, spoken_wav):
        with wave.open(str(spoken_wav)) as wf:
            duration = wf.getnframes() / wf.getframerate()

        got = transcribe.transcribe(str(spoken_wav), model="base")
        last_end = 0.0
        for seg in got["segments"]:
            assert seg["t_start"] >= 0
            assert seg["t_end"] <= duration + 1.0
            assert seg["t_start"] >= last_end - 0.5  # monotonic-ish
            last_end = seg["t_end"]

    def test_output_feeds_the_classifier(self, spoken_wav):
        """The real contract: whisper output -> feedback items, no adapter layer."""
        from bgate_core import feedback

        got = transcribe.transcribe(str(spoken_wav), model="base")
        items = feedback.extract([{**s, "id": i} for i, s in enumerate(got["segments"])])
        assert items, "real speech produced no feedback items"
        assert any(i["kind"] == "fix" for i in items), [i["text"] for i in items]
        assert any(i["seat"] == "gameplay" for i in items)


class TestFailures:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            transcribe.transcribe(str(tmp_path / "ghost.wav"))

    def test_empty_audio_is_reported_not_crashed(self, tmp_path):
        empty = tmp_path / "empty.wav"
        empty.write_bytes(b"RIFF")
        got = transcribe.transcribe(str(empty))
        assert got["ok"] is False
        assert "empty" in got["error"]

    def test_bad_model_rejected(self, tmp_path):
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"0" * 2048)
        with pytest.raises(ValueError, match="model"):
            transcribe.transcribe(str(wav), model="gpt-4")

    def test_silent_audio_yields_no_hallucinated_segments(self, tmp_path):
        """VAD must suppress whisper's 'Thanks for watching!' over silence."""
        silent = tmp_path / "silent.wav"
        with wave.open(str(silent), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000 * 3)  # 3s of digital silence

        got = transcribe.transcribe(str(silent), model="base")
        assert got["ok"] is True
        from bgate_core import feedback
        real = [s for s in got["segments"] if not feedback.is_noise(s["text"])]
        assert real == [], f"hallucinated over silence: {real}"
