"""System-under-test configs (spec §2.1 "what each adapter must record", §10 item 1; BUILD_PLAN M3).

    sys = load_system("openai_realtime")           # configs/systems/openai_realtime.yaml
    sys.config_id                                   # storage.config_id(raw YAML mapping)
    sys.report_entry(preregistered_at)              # the per-system dict runs.config_json carries
    preflight_system(sys)                           # [] or one line per missing precondition

YAML schema (every key required except `params`; unknown keys are refused so a
typo can never silently change behaviour without changing the hash):

    system_id:        str     must equal the file stem
    adapter:          str     key in ADAPTERS (mock_agent | openai_realtime | dograh_stack |
                              gemini_live | grok_voice | gpt_live | pipecat_cascade | selfhosted_fd)
    model:            str | null
    reasoning_effort: str | null
    turn_detection:   mapping | null   provider turn-detection block, sent verbatim
    self_hosted:      bool    flagged in every table (spec §2.1)
    components_only:  bool    cascade measured as components (no end-to-end vendor)
    cancel_event:     bool    provider emits a cancel/truncate the harness can flush on
    price_ref:        str | null       configs/prices/<price_ref>.yaml; null = free (mock)
    params:           mapping           adapter-specific extras (endpoint, voice, instructions,
                                        mock program, api_key_env); hashed like everything else

`config_id` is computed over the raw mapping exactly as `preregister.systems_hashes`
does, so the id in a run equals the pre-registered id of the same file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from duplexbench.storage import config_id as _config_id

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMS_DIR = REPO_ROOT / "configs" / "systems"
PRICES_DIR = REPO_ROOT / "configs" / "prices"

# adapter key -> "module:Class" (imported lazily: adapter modules pull in websockets/httpx)
ADAPTERS: dict[str, str] = {
    "mock_agent": "duplexbench.adapters.mock_agent:MockAgent",
    "openai_realtime": "duplexbench.adapters.openai_realtime:OpenAIRealtimeAdapter",
    "dograh_stack": "duplexbench.adapters.dograh_stack:DograhStack",
    "gemini_live": "duplexbench.adapters.gemini_live:GeminiLive",
    "grok_voice": "duplexbench.adapters.grok_voice:GrokVoice",
    "gpt_live": "duplexbench.adapters.gpt_live:GptLive",
    "pipecat_cascade": "duplexbench.adapters.pipecat_cascade:PipecatCascade",
    "selfhosted_fd": "duplexbench.adapters.selfhosted_fd:SelfHostedFD",
}


def adapter_class(adapter: str) -> type:
    """The adapter class registered under `adapter` (SystemError_ when unknown)."""
    import importlib
    try:
        mod, _, cls = ADAPTERS[adapter].partition(":")
    except KeyError:
        raise SystemError_(f"unknown adapter {adapter!r}; have {list(ADAPTERS)}") from None
    return getattr(importlib.import_module(mod), cls)


def preflight_system(system: "SystemConfig") -> list[str]:
    """Plan-time preconditions of `system`'s adapter (`Adapter.preflight`); [] = ready.

    Imports the adapter module but makes no call and spends nothing."""
    cls = adapter_class(system.adapter)
    check = getattr(cls, "preflight", None)
    if check is None:
        return []
    return list(check(system._raw or system.model_dump(mode="json")))


class SystemError_(ValueError):
    """A system config cannot be loaded."""


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    system_id: str
    adapter: str
    model: str | None
    reasoning_effort: str | None
    turn_detection: dict[str, Any] | None
    self_hosted: bool
    components_only: bool
    cancel_event: bool
    price_ref: str | None
    params: dict[str, Any] = Field(default_factory=dict)

    _raw: dict = PrivateAttr(default_factory=dict)
    _path: Path | None = PrivateAttr(default=None)

    @property
    def config_id(self) -> str:
        return _config_id(self._raw or self.model_dump(mode="json"))

    @property
    def path(self) -> Path | None:
        return self._path

    def report_entry(self, preregistered_at: str | None) -> dict[str, Any]:
        """Per-system entry of `runs.config_json["systems"]` (report/run_card.py SYSTEM_FIELDS)."""
        return {"config_id": self.config_id, "preregistered_at": preregistered_at,
                "reasoning_effort": self.reasoning_effort, "turn_detection": self.turn_detection,
                "self_hosted": self.self_hosted, "components_only": self.components_only,
                "cancel_event": self.cancel_event, "adapter": self.adapter, "model": self.model,
                "price_ref": self.price_ref}


def parse_system(raw: dict, path: Path | None = None) -> SystemConfig:
    if not isinstance(raw, dict):
        raise SystemError_(f"{path}: top level must be a mapping")
    try:
        cfg = SystemConfig.model_validate(raw)
    except Exception as e:  # pydantic ValidationError -> one readable error type
        raise SystemError_(f"{path or '<system>'}: {e}") from e
    if cfg.adapter not in ADAPTERS:
        raise SystemError_(f"{path or cfg.system_id}: unknown adapter {cfg.adapter!r}; "
                           f"have {list(ADAPTERS)}")
    if path is not None and cfg.system_id != path.stem:
        raise SystemError_(f"{path}: system_id {cfg.system_id!r} != file stem {path.stem!r}")
    cfg._raw = dict(raw)
    cfg._path = path
    return cfg


def load_system(name_or_path: str | Path, systems_dir: Path | None = None) -> SystemConfig:
    """Load `configs/systems/<name>.yaml` (or a path to a YAML file)."""
    p = Path(name_or_path)
    if p.suffix not in (".yaml", ".yml"):
        p = Path(systems_dir or SYSTEMS_DIR) / f"{name_or_path}.yaml"
    if not p.is_file():
        raise SystemError_(f"system config not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return parse_system(raw, p)


def load_systems(names: list[str], systems_dir: Path | None = None) -> dict[str, SystemConfig]:
    out: dict[str, SystemConfig] = {}
    for n in names:
        s = load_system(n, systems_dir)
        if s.system_id in out:
            raise SystemError_(f"system {s.system_id!r} listed twice")
        out[s.system_id] = s
    return out


def load_prices(price_ref: str | None, prices_dir: Path | None = None) -> dict | None:
    """`configs/prices/<price_ref>.yaml`, or None for an unpriced (free) system.

    Every price file must carry `source_url` and `checked` (spec §4.4 "price source URL
    and check date stored")."""
    if price_ref is None:
        return None
    p = Path(prices_dir or PRICES_DIR) / f"{price_ref}.yaml"
    if not p.is_file():
        raise SystemError_(f"price file not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    for k in ("source_url", "checked"):
        if not raw.get(k):
            raise SystemError_(f"{p}: price file needs {k!r}")
    raw.setdefault("price_ref", price_ref)
    return raw
