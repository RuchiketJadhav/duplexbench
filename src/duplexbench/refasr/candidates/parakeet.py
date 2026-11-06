"""NVIDIA Parakeet-TDT 0.6B v3 and Nemotron streaming candidates via NeMo (BUILD_PLAN
M4a-T02). Both wrap `nemo.collections.asr` models; this thin wrapper runs them in
batch mode with word timestamps and `stream()` buffers (NeMo cache-aware streaming
is wired only after a human picks the model). A local `.nemo` file is restored with
`restore_from`; `from_pretrained` (a download) needs `allow_download=True`."""
from __future__ import annotations

from typing import Any

import numpy as np

from duplexbench.refasr.candidates import (BufferedStreamer, CandidateUnavailable, has_module,
                                          require, upsample)
from duplexbench.refasr.engine import Word

PIP = "nemo_toolkit[asr]"
MODEL_RATE = 16000
_MS_PER_S = 1_000
PARAKEET = "nvidia/parakeet-tdt-0.6b-v3"
NEMOTRON_STREAMING = "nvidia/nemotron-speech-streaming-en-0.6b"   # [Assumption] id unverified


def probe() -> str | None:
    return None if has_module("nemo") else f"pip install {PIP}"


class NemoASR:
    name = "parakeet-tdt-0.6b-v3"
    streaming = False
    word_timestamps = True

    def __init__(self, model: str = PARAKEET, *, model_path: str | None = None,
                 allow_download: bool = False) -> None:
        require("nemo", PIP)
        asr = require("nemo.collections.asr", PIP)
        self.model_name = model
        self.version = model if model_path is None else f"{model}@{model_path}"
        if model_path is None and not allow_download:
            raise CandidateUnavailable(
                f"{model}: no local model_path; run `duplexbench refasr-bench --yes` "
                "to approve the download")
        try:
            if model_path is not None:
                self._model = asr.models.ASRModel.restore_from(model_path)
            else:
                self._model = asr.models.ASRModel.from_pretrained(model)
        except Exception as exc:
            raise CandidateUnavailable(f"{model} could not load: {exc}") from exc

    def transcribe(self, pcm8k: np.ndarray) -> list[Word]:
        audio = upsample(pcm8k, MODEL_RATE)
        hyps = self._model.transcribe([audio], timestamps=True)
        hyp: Any = hyps[0][0] if isinstance(hyps, tuple) else hyps[0]
        ts = getattr(hyp, "timestamp", None) or {}
        out = [Word(str(w.get("word", "")).strip(),
                    int(round(float(w.get("start", 0.0)) * _MS_PER_S)),
                    int(round(float(w.get("end", 0.0)) * _MS_PER_S)))
               for w in ts.get("word", [])]
        return [w for w in out if w.text]

    def stream(self) -> BufferedStreamer:
        return BufferedStreamer(self.transcribe)


class ParakeetASR(NemoASR):
    name = "parakeet-tdt-0.6b-v3"


class NemotronStreamingASR(NemoASR):
    name = "nemotron-streaming-0.6b"
    streaming = True              # native cache-aware streaming; this wrapper's stream() buffers

    def __init__(self, model: str = NEMOTRON_STREAMING, **kw: Any) -> None:
        super().__init__(model, **kw)
