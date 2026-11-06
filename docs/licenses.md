# Reference repo licenses (M0-T02)

Cloned with `git clone --depth 1` into `reference/` on 2026-11-04. No code was run or copied.
SPDX ids are read from each repo's top-level `LICENSE` file text. "Reuse" means copying code into this harness
(Dograh is a commercial company; any non-commercial term blocks reuse).

| Repo | URL | Commit (HEAD) | SPDX (LICENSE text) | Date checked | Reuse allowed | Notes |
|---|---|---|---|---|---|---|
| Full-Duplex-Bench | https://github.com/DanielLin94144/Full-Duplex-Bench | `3e799c45a045256f47d5f1c9cda90157e2d2ec9e` | CC-BY-NC-4.0 ("Attribution-NonCommercial 4.0 International") | 2026-11-04 | **no** (read only) | Non-commercial. Re-implement the TOR rule from its description with attribution in docs; do not copy code. |
| tau2-bench | https://github.com/sierra-research/tau2-bench | `6fe66e9b10bec6c582841e1961bf835287a6ac28` | MIT ("MIT License, Copyright (c) 2025 Sierra Research") | 2026-11-04 | yes | Copy with attribution header. Domain data and noise audio under `data/` were not audited separately; check before importing data. |
| turnbench | https://github.com/SesameAILabs/turnbench | `38a6f874322430cb3ca71d8a52aa1e636e88bad8` | MIT | 2026-11-04 | code-only | README: "The data is hosted on Hugging Face and licensed separately with a non-commercial license that prohibits voice cloning." Data: no. |
| stt-benchmark | https://github.com/pipecat-ai/stt-benchmark | `94f567299a21f8d617039bc69f137a131f5dedd5` | BSD-2-Clause ("BSD 2-Clause License, Copyright (c) 2025-2026, Daily") | 2026-11-04 | yes | **Correction:** CLAUDE.md §3 says MIT; the LICENSE file and README ("## License / BSD 2-Clause") say BSD-2-Clause. |
| pipecat | https://github.com/pipecat-ai/pipecat | `0c6826ac11a0dae9e67189aa0b538613836e310a` | BSD-2-Clause ("BSD 2-Clause License, Copyright (c) 2024–2026, Daily") | 2026-11-04 | yes | Confirms the belief in CLAUDE.md §3. |
| Silero VAD | https://github.com/snakers4/silero-vad | `066409f3d9db45a30d21dfcbf9147826ffdd7052` | MIT ("MIT License, Copyright (c) 2020-present Silero Team") | 2026-11-04 | yes | README badge alt-text says "CC BY-NC 4.0" but the badge, link target and LICENSE file say MIT; the LICENSE file governs. Use via the PyPI package, not the clone. |

Clone log: Full-Duplex-Bench, tau2-bench, silero-vad cloned on the first attempt; turnbench, stt-benchmark and
pipecat failed once with `fatal: unable to access ...: Could not resolve host: github.com` (transient DNS) and
cloned on retry. `GIT_LFS_SKIP_SMUDGE=1` was set, so LFS objects (if any) were not downloaded.

Silero VAD model file (M1-T13, 2026-11-04): `silero_vad.onnx` from snakers4/silero-vad commit `066409f3d9db45a30d21dfcbf9147826ffdd7052`, path `src/silero_vad/data/silero_vad.onnx` (MIT), 2,327,524 bytes, sha256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`. Read from the M0 clone in `reference/`; if the clone is absent, download that single file from https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx to `tests/fixtures/silero_vad.onnx`. `metrics/silero_check.py` refuses any file whose sha256 differs. Loaded with `onnxruntime` (MIT); offline cross-check only, never imported by the live loop.
