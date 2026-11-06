"""systems.py: system config schema, config_id parity with preregister, price refs."""
from __future__ import annotations

import pytest
import yaml

from duplexbench import preregister
from duplexbench.storage import config_id
from duplexbench.systems import SYSTEMS_DIR, SystemError_, load_prices, load_system, parse_system


def test_shipped_systems_load():
    o = load_system("openai_realtime")
    assert o.adapter == "openai_realtime" and o.model == "gpt-realtime-2.1"
    assert o.turn_detection["type"] == "server_vad" and o.cancel_event and not o.self_hosted
    assert load_prices(o.price_ref)["source_url"]
    m = load_system("mock_stops")
    assert m.adapter == "mock_agent" and m.price_ref is None and load_prices(m.price_ref) is None


def test_config_id_equals_preregister_hash():
    hashes = preregister.systems_hashes(SYSTEMS_DIR)
    for name in ("openai_realtime", "mock_stops"):
        s = load_system(name)
        raw = yaml.safe_load((SYSTEMS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        assert s.config_id == config_id(raw) == hashes[name]


def test_report_entry_shape():
    e = load_system("openai_realtime").report_entry("2026-11-05T00:00:00Z")
    for k in ("config_id", "preregistered_at", "reasoning_effort", "turn_detection",
              "self_hosted", "components_only", "cancel_event"):
        assert k in e
    assert e["preregistered_at"] == "2026-11-05T00:00:00Z"


def _raw(**over):
    raw = yaml.safe_load((SYSTEMS_DIR / "mock_stops.yaml").read_text(encoding="utf-8"))
    raw.update(over)
    return raw


def test_unknown_key_and_adapter_refused():
    with pytest.raises(SystemError_):
        parse_system(_raw(temprature=0.2))
    with pytest.raises(SystemError_, match="unknown adapter"):
        parse_system(_raw(adapter="nope"))
    raw = _raw()
    del raw["cancel_event"]
    with pytest.raises(SystemError_):
        parse_system(raw)


def test_system_id_must_match_stem(tmp_path):
    (tmp_path / "other.yaml").write_text(yaml.safe_dump(_raw()), encoding="utf-8")
    with pytest.raises(SystemError_, match="file stem"):
        load_system("other", tmp_path)
    with pytest.raises(SystemError_, match="not found"):
        load_system("missing", tmp_path)


def test_any_change_changes_config_id():
    a = parse_system(_raw())
    b = parse_system(_raw(params={"mock": {"behaviour": "never_stops"}}))
    assert a.config_id != b.config_id


def test_every_system_config_loads_and_resolves_to_an_adapter():
    """Every configs/systems/*.yaml parses (params:, no adapter_config:) and names a
    registered adapter class (systems.ADAPTERS, cli_run.make_adapter)."""
    from duplexbench.adapters.base import Adapter
    from duplexbench.systems import ADAPTERS, SYSTEMS_DIR, adapter_class, load_system
    files = sorted(SYSTEMS_DIR.glob("*.yaml"))
    assert len(files) >= 8
    seen = set()
    for f in files:
        s = load_system(f)
        cls = adapter_class(s.adapter)
        assert issubclass(cls, Adapter), f.name
        assert "adapter_config" not in s._raw, f.name
        seen.add(s.adapter)
    assert seen == set(ADAPTERS)


def test_adapter_rate_per_adapter():
    from duplexbench.cli_run import adapter_rate
    from duplexbench.systems import adapter_class, load_system
    cond = type("C", (), {"codec": True, "rate": 8000})()
    want = {"gemini_live": 24000, "selfhosted_moshi": 24000, "grok_voice": 8000,
            "gpt_live": 8000, "pipecat_cascade": 8000}
    for sid, rate in want.items():
        s = load_system(sid)
        a = adapter_class(s.adapter)(None, s)
        assert adapter_rate(a, cond) == rate, sid
