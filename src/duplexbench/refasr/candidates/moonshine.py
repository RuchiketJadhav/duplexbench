"""Moonshine candidate (BUILD_PLAN M4a-T02) via the ONNX runtime package
(`useful-moonshine-onnx`, module `moonshine_onnx`). Batch engine, text only (no word
times: `word_timestamps = False`, words carry 0 times); `stream()` buffers."""
from __future__ import annotations

import numpy as np

from duplexbench.refasr.candidates import (BufferedStreamer, CandidateUnavailable, has_module,
                                          require, upsample)
from duplexbench.refasr.engine import Word

PIP = "useful-moonshine-onnx"
MODEL_RATE = 16000
DEFAULT_MODEL = "moonshine/base"


def probe() -> str | None:
    return None if has_module("moonshine_onnx") else f"pip install {PIP}"


class MoonshineASR:
    name = "moonshine"
    streaming = False
    word_timestamps = False

    def __init__(self, model: str = DEFAULT_MODEL, *, allow_download: bool = False) -> None:
        self._m = require("moonshine_onnx", PIP)
        if not allow_download:
            raise CandidateUnavailable(
                f"{model}: moonshine_onnx fetches weights on load; run "
                "`duplexbench refasr-bench --yes` to approve the download")
        self.model_name = model
        self.version = f"{getattr(self._m, '__version__', '?')}/{model}"
        self._model = self._m.MoonshineOnnxModel(model_name=model)
        self._tok = self._m.load_tokenizer()

    def transcribe(self, pcm8k: np.ndarray) -> list[Word]:
        audio = upsample(pcm8k, MODEL_RATE)[None, :]
        tokens = self._model.generate(audio)
        text = self._tok.decode_batch(tokens)[0]
        return [Word(t, 0, 0) for t in text.split()]

    def stream(self) -> BufferedStreamer:
        return BufferedStreamer(self.transcribe)
