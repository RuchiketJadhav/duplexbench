"""One scripted Track A call with the overlay engine, under any Clock (tests, golden M2, mock smoke).

    run_scripted_call(clock, store, scenario, clips, agent, cond, ...) -> CallResult
        ScriptedCaller(scenario | its control twin) --> runner.run_call(overlays=OverlayEngine)

`clips` maps step ids, `<step>/resumption`, overlay ids and `<overlay>/resumption`
to 8 kHz int16 PCM. The control twin (spec §3.4) runs the same caller audio with
`make_twin(scenario)` and `is_control=True`; the engine still runs on it so the
twin's P-PAUSE / E-END geometry rows exist.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import soundfile as sf

from duplexbench.channel.chain import ChannelChain, load_condition
from duplexbench.context import CallContext
from duplexbench.overlays.engine import make_engine
from duplexbench.playout import Playout
from duplexbench.recorder import Recorder
from duplexbench.runner import CallResult, run_call
from duplexbench.scenarios.control_twin import make_twin
from duplexbench.stimuli.scripted_caller import ScriptedCaller

if TYPE_CHECKING:
    from duplexbench.adapters.base import Adapter
    from duplexbench.clock import Clock
    from duplexbench.scenarios.schema import Scenario
    from duplexbench.thresholds import Thresholds

_NS_PER_MS = 1_000_000


def read_pcm(path: Path | str) -> np.ndarray:
    data, rate = sf.read(str(path), dtype="int16", always_2d=False)
    if rate != 8000:  # threshold-ok: line rate of the fixture clips (frame contract §2.2)
        raise ValueError(f"{path}: {rate} Hz, expected 8 kHz")
    return np.asarray(data, dtype=np.int16).ravel()


def call_id_for(scenario_id: str, condition: str, trial: int, is_control: bool) -> str:
    return f"{scenario_id}-{condition}-t{trial}-{'ctl' if is_control else 'ovl'}"


async def run_scripted_call(clock: "Clock", store: Any, scenario: "Scenario",
                            clips: Mapping[str, np.ndarray], agent: "Adapter", th: "Thresholds",
                            out_dir: Path, *, run_id: str, condition: str = "C0", trial: int = 0,
                            is_control: bool = False, system_id: str = "mock",
                            seed_fwd: int = 11, seed_back: int = 12, caller: Any = None,
                            engine_kw: Mapping[str, Any] | None = None,
                            call_id: str | None = None, tool_token: str = "tok",
                            tools: Any = None, projected_usd: float = 0.0,
                            back_rate: int | None = None, cond: Any = None) -> CallResult:
    """`call_id` / `tool_token` / `tools` / `projected_usd` / `back_rate` / `cond` are for
    `duplexbench run` (cli_run); the defaults reproduce the golden M2 calls exactly.
    `back_rate` builds the return-path resampler for the adapter's output rate before
    the call starts; `cond` is a loaded Condition (else `load_condition(condition, th)`)."""
    sc = make_twin(scenario) if is_control else scenario
    cond = cond if cond is not None else load_condition(condition, th)
    call_id = call_id or call_id_for(scenario.id, condition, trial, is_control)
    ctx = CallContext(run_id, call_id, sc, trial, cond, system_id, is_control, clock, store,
                      tool_token)
    caller = caller or ScriptedCaller(sc, clips, clock, th, rate=cond.rate)
    rec = Recorder(Path(out_dir) / f"{call_id}.wav", cond.rate, origin_ns=clock.now_ns())
    po = Playout(clock, store, rec, th, run_id=run_id, call_id=call_id, rate=cond.rate)
    chain = ChannelChain(cond, seed_fwd, seed_back)
    if back_rate is not None:
        chain.back_delay_ms(back_rate)
    eng = make_engine(clock, sc, po, caller, store, th, clips,
                      send_delay_ns=chain.delay_frames * th.frame_ms * _NS_PER_MS,
                      **dict(engine_kw or {}))
    res = await run_call(ctx, agent, caller, eng, po, chain, tools, None, th=th,
                         projected_usd=projected_usd)
    eng.finish()            # idempotent; the runner cancels the engine task at call end
    await store.flush()
    return res
