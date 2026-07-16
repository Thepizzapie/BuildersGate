"""Runs in a SEPARATE process. Transcribes a wav, prints JSON to stdout.

Isolated on purpose: faster-whisper loads a multi-hundred-MB model and pins a
core for the length of the audio. Doing that inline in a FastMCP async loop
stalls the whole server and every other tool call with it.

argv: <wav_path> <model> <device> <compute_type> [language]
"""
import json
import sys

# ctranslate2's "auto" picks CUDA whenever an NVIDIA GPU is present — it does NOT
# check that the CUDA libraries are actually loadable. On a machine with a GPU but
# no CUDA toolkit (i.e. most machines), the model loads fine and then dies at
# inference with "Library cublas64_12.dll is not found". So probe by RUNNING, not
# by asking, and fall back to CPU. int8 on CPU is ~4x faster than float32 with no
# meaningful accuracy loss at this model size.
_CUDA_ERRORS = ("cublas", "cudnn", "cuda", "no kernel image", "libcu")


def _load(WhisperModel, model_name, device, compute):
    """Return (model, device, compute, fallback_reason). Never trusts 'auto'."""
    attempts = []
    if device == "auto":
        attempts = [("cuda", compute if compute != "auto" else "float16"),
                    ("cpu", compute if compute != "auto" else "int8")]
    else:
        resolved = compute
        if compute == "auto":
            resolved = "float16" if device == "cuda" else "int8"
        attempts = [(device, resolved)]

    last = None
    for dev, comp in attempts:
        try:
            model = WhisperModel(model_name, device=dev, compute_type=comp)
            # Constructing the model does NOT touch CUDA, and transcribe() returns
            # a LAZY GENERATOR — so the probe must consume it, or the encode never
            # runs and a broken CUDA reports success right up until real use.
            import numpy as np
            segments, _info = model.transcribe(np.zeros(16000, dtype=np.float32),
                                               language="en")
            list(segments)
            return model, dev, comp, None if dev == attempts[0][0] else last
        except Exception as exc:
            last = f"{dev}/{comp} unusable: {type(exc).__name__}: {str(exc)[:120]}"
            if dev == "cuda" and not any(e in str(exc).lower() for e in _CUDA_ERRORS):
                # Not a CUDA-library problem — don't mask a real error as fallback.
                raise
            continue
    raise RuntimeError(last or "no usable device")


def main():
    wav, model_name, device, compute = sys.argv[1:5]
    language = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "-" else None

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"faster-whisper unavailable: {exc}"}))
        return

    try:
        model, device, compute, fallback = _load(WhisperModel, model_name, device, compute)
        segments, info = model.transcribe(
            wav,
            language=language,
            beam_size=5,
            # A playtest is one person thinking out loud with long silences while
            # they play. Without VAD, whisper hallucinates text into the gaps —
            # confident nonsense is worse than a missing line.
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 700},
        )
        out = [{
            "t_start": round(s.start, 3),
            "t_end": round(s.end, 3),
            "text": s.text.strip(),
            # avg_logprob is a log prob; expose it as-is rather than faking a 0-1.
            "confidence": round(s.avg_logprob, 3) if s.avg_logprob is not None else None,
        } for s in segments if s.text.strip()]

        print(json.dumps({
            "ok": True,
            "segments": out,
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "duration": round(info.duration, 2),
            "device": device,
            "compute_type": compute,
            "fallback": fallback,
        }))
    except Exception as exc:
        import traceback
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "traceback": traceback.format_exc(limit=4)}))


main()
