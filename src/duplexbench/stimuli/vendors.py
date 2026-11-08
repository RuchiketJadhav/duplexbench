"""TTS client factory from a config block (harness task tts-fish, decision B13).

    tts = build_tts({"vendor": "fish", "model": "drama-3",
                     "voices_file": "configs/voices/fish.yaml", "default_voice": "delia"})
    tts = build_tts({"vendor": "fake"})          # FakeTTS (extra keys go to its constructor)
    tts = build_tts(None)                        # None: the caller keeps its default

Used by cli_run for the run-config block `simulator.tts`. `vendor` absent or None ->
None; an unknown vendor -> ValueError. Building a fish client needs the API key
(FISH_API_KEY or the block's `api_key_env`), else `fish_tts.TTSUnavailable`; building
never sends a request.
"""
from __future__ import annotations

from duplexbench.stimuli.tts import TTSClient

VENDORS = ("fish", "fake")


def build_tts(cfg: dict | None, *, transport=None) -> TTSClient | None:
    """`cfg["vendor"]`: "fish" -> FishTTS.from_config, "fake" -> FakeTTS, None -> None.
    `transport` (httpx) is passed to the fish client (tests only)."""
    if not cfg or cfg.get("vendor") is None:
        return None
    vendor = cfg["vendor"]
    if vendor == "fish":
        from duplexbench.stimuli.fish_tts import FishTTS
        return FishTTS.from_config(cfg, transport=transport)
    if vendor == "fake":
        from duplexbench.stimuli.fake_tts import FakeTTS
        return FakeTTS(**{k: v for k, v in cfg.items() if k != "vendor"})
    raise ValueError(f"unknown TTS vendor {vendor!r}; known: {list(VENDORS)}")
