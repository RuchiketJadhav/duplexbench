"""faster-whisper `large-v3-turbo` candidate (BUILD_PLAN M4a-T02). Batch engine with
word timestamps; `stream()` buffers (no partials). Model files load with
`local_files_only=True` unless `allow_download=True` (approval: refasr-bench --yes)."""
from __future__ import annotations

import numpy as np

from duplexbench.refasr.candidates import (BufferedStreamer, CandidateUnavailable, has_module,
                                          require, upsample)
from duplexbench.refasr.engine import Word

PIP = "faster-whisper"
MODEL_RATE = 16000
_MS_PER_S = 1_000


def probe() -> str | None:
    """None when importable, else the reason (no import, no download)."""
    return None if has_module("faster_whisper") else f"pip install {PIP}"


class FasterWhisperASR:
    name = "faster-whisper"
    streaming = False
    word_timestamps = True

    def __init__(self, model: str = "large-v3-turbo", *, device: str = "cpu",
                 compute_type: str = "int8", download_root: str | None = None,
                 allow_download: bool = False) -> None:
        self._fw = require("faster_whisper", PIP)
        self.model_name = model
        self.version = f"{getattr(self._fw, '__version__', '?')}/{model}/{compute_type}"
        try:
            self._model = self._fw.WhisperModel(model, device=device, compute_type=compute_type,
                                                download_root=download_root,
                                                local_files_only=not allow_download)
        except Exception as exc:          # model not cached locally
            raise CandidateUnavailable(
                f"faster-whisper model {model!r} not available locally ({exc}); "
                "run `duplexbench refasr-bench --yes` to approve the download") from exc

    def transcribe(self, pcm8k: np.ndarray) -> list[Word]:
        audio = upsample(pcm8k, MODEL_RATE)
        segments, _info = self._model.transcribe(audio, language="en", word_timestamps=True,
                                                 beam_size=1, vad_filter=False)
        out: list[Word] = []
        for seg in segments:
            for w in seg.words or ():
                out.append(Word(w.word.strip(), int(round(w.start * _MS_PER_S)),
                                int(round(w.end * _MS_PER_S)), float(w.probability)))
        return [w for w in out if w.text]

    def stream(self) -> BufferedStreamer:
        return BufferedStreamer(self.transcribe)
