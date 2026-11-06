"""Kyutai STT candidate (BUILD_PLAN M4a-T02). Kyutai's model is natively streaming
(the `moshi` package carries the streaming runtime). This thin wrapper uses the
Hugging Face `transformers` port in batch mode; that path emits text without word
times, so `word_timestamps = False` and words carry 0 times. The native streaming
path (partial latency) is wired only after a human picks the model."""
from __future__ import annotations

import numpy as np

from duplexbench.refasr.candidates import (BufferedStreamer, CandidateUnavailable, has_module,
                                          require, upsample)
from duplexbench.refasr.engine import Word

PIP = "moshi transformers torch"
MODEL_RATE = 24000
DEFAULT_MODEL = "kyutai/stt-2.6b-en-trfs"     # [Assumption] HF id; verify before download


def probe() -> str | None:
    missing = [m for m in ("moshi", "transformers", "torch") if not has_module(m)]
    return None if not missing else f"pip install {PIP} (missing: {', '.join(missing)})"


class KyutaiASR:
    name = "kyutai-stt"
    streaming = True              # native; this wrapper's stream() buffers
    word_timestamps = False

    def __init__(self, model: str = DEFAULT_MODEL, *, allow_download: bool = False) -> None:
        reason = probe()
        if reason:
            raise CandidateUnavailable(reason)
        tf = require("transformers", PIP)
        self._torch = require("torch", PIP)
        self.model_name = model
        self.version = f"transformers-{tf.__version__}/{model}"
        try:
            kw = {"local_files_only": not allow_download}
            self._proc = tf.KyutaiSpeechToTextProcessor.from_pretrained(model, **kw)
            self._model = tf.KyutaiSpeechToTextForConditionalGeneration.from_pretrained(model, **kw)
        except Exception as exc:
            raise CandidateUnavailable(
                f"{model} not available locally ({exc}); run `duplexbench refasr-bench --yes` "
                "to approve the download") from exc

    def transcribe(self, pcm8k: np.ndarray) -> list[Word]:
        audio = upsample(pcm8k, MODEL_RATE)
        inputs = self._proc(audio, return_tensors="pt")
        with self._torch.no_grad():
            ids = self._model.generate(**inputs)
        text = self._proc.batch_decode(ids, skip_special_tokens=True)[0]
        return [Word(t, 0, 0) for t in text.split()]

    def stream(self) -> BufferedStreamer:
        return BufferedStreamer(self.transcribe)
