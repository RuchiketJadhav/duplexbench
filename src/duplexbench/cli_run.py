"""`duplexbench run` (BUILD_PLAN M3-T03/T04; spec §2.3 pre-registration, §4.4 cost, §9.3, §10 item 1).

    uv run duplexbench run configs/runs/smoke_mock.yaml --yes
    uv run duplexbench run configs/runs/smoke_openai.yaml [--yes] [--max-usd 5] [--allow-unregistered]

Order of work (nothing is sent to any provider before the last step):

    1. load the run config, thresholds, pack (+ every clip), systems, prices, conditions
    2. pre-registration: `preregister.check_against_latest` must be empty, else refuse
       (exit 4) unless --allow-unregistered and every selected scenario is public-dev
    3. calibration status (latest results/calibration-*.json; printed, recorded;
       refused only when the run config says `require_calibration: true`)
    4. projection per system: calls x est. minutes x USD/min; refuse (exit 3) when the
       total exceeds max_usd; refuse (exit 1, "not started") without --yes
    5. results/<run_id>/events.sqlite + <call_id>.wav; `runs` row; then calls
       sequentially (concurrency 1 in M3), each with a Budget reservation; the run
       stops at a budget refusal or at an unknown actual cost (M3-T01)

Run config (configs/runs/*.yaml), see `RunConfig`:

    name: smoke_mock                       # run_id = <name>-<UTC ts> unless run_id is given
    pack: tests/fixtures/scenarios/smoke   # relative to the repo root (or absolute)
    scenarios: null                        # list of ids, or null = all (after track/limit)
    track: A                               # optional filter
    limit: 2                               # optional cap after filtering (sorted by id)
    systems: [mock_stops]                  # configs/systems/<id>.yaml
    systems_dir: null                      # default configs/systems
    conditions: [C0]
    trials: 1                              # trials per scenario x condition (null = scenario.trials)
    concurrency: 1                         # only 1 is supported in M3
    max_usd: 0.0
    thresholds: null                       # default configs/thresholds.yaml
    results_dir: null                      # default results/
    seed: 1
    comparisons: []                        # pre-registered, see report/aggregate.py
    baseline_condition: C0
    require_calibration: false
    simulator: {}                          # Track C (source: simulated): caller_first, refasr
    coverage: {}                           # pre-registered coverage n per metric (M8 gate)

Track C (scenarios with `source: simulated`, M4-T12): refused (exit 4, D4) unless the
latest calibration's `refasr.passed` is true. Per call: ToolServer binding for the
call token, the domain backend (its SEED state), `SimulatedCaller` attached to the
playout: it hears every played ear frame through the reference ASR (spec §7 "Hears");
every adapter `output_transcript` goes to `caller.on_provider_text` (kept apart, never
in the LLM context). `CallerVoice(FakeTTS)` unless a TTS client is injected; after the
call the backend state is logged as a `final_state` event and one `mishearing` event
(src harness, {refasr, refasr_text, provider_text}) for `mishearing_rate`.

Reference ASR for the simulated caller (fix-20): `run_command(..., sim_refasr=)` / an
injected `plan.sim_refasr` engine (tests: FakeASR), else the run config key

    simulator:
      refasr: {candidate: <refasr.candidates_table CANDIDATES name>, kwargs: {...}}

built once per run (`build_refasr`); a real model runs its push/finish in a worker
thread (`asr_threaded`). Neither given -> Track C refused (exit 2): the caller must
never hear the provider's own text. A configured engine whose `name:version` differs
from the latest calibration's `refasr.version` is printed as a warning. Each trial runs under `validation_gate.run_with_regeneration`
(whole-trial rerun up to th.sim_regen_max, call ids `<id>-r<attempt>`, every attempt
kept and costed). The simulator LLM is injected (`execute(..., sim_llm=)`); the CLI
has no paid simulator client wired yet (M4-T13), so it refuses Track C (exit 2).

`runs.config_json` shape (what report/aggregate.py and report/run_card.py read):

    {"systems": {sid: {config_id, preregistered_at, reasoning_effort, turn_detection,
                       self_hosted, components_only, cancel_event, adapter, model, price_ref}},
     "comparisons": [...], "baseline_condition": "C0",
     "scenarios": {id: <scenario JSON>},        # cli score reads the scenarios from here
     "coverage": {metric_key: n}, "planned_calls": int,   # gates.py coverage_n_met / attempts
     "run": <run config>}
"""
from __future__ import annotations

import datetime as _dt
import json
import platform
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field

from duplexbench.storage import canonical_json

REPO_ROOT = Path(__file__).resolve().parents[2]
_NS_PER_MS = 1_000_000
_MS_PER_MIN = 60_000.0          # threshold-ok: unit conversion
EXIT_NOT_STARTED, EXIT_BUDGET, EXIT_PREREG, EXIT_CONFIG = 1, 3, 4, 2


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    run_id: str | None = None
    pack: str
    scenarios: list[str] | None = None
    track: str | None = None
    limit: int | None = None
    systems: list[str]
    systems_dir: str | None = None
    conditions: list[str] = Field(default_factory=lambda: ["C0"])
    trials: int | None = 1
    concurrency: int = 1
    max_usd: float = 0.0
    thresholds: str | None = None
    results_dir: str | None = None
    seed: int = 1
    comparisons: list[dict] = Field(default_factory=list)
    baseline_condition: str = "C0"
    require_calibration: bool = False
    simulator: dict = Field(default_factory=dict)   # Track C: {caller_first: true}
    coverage: dict = Field(default_factory=dict)    # pre-registered coverage n (gates.py)

    @classmethod
    def load(cls, path: Path | str) -> "RunConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)


def _path(p: str | None, default: Path, base: Path = REPO_ROOT) -> Path:
    if p is None:
        return default
    q = Path(p)
    return q if q.is_absolute() else base / q


class RunRefused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# -- clips ----------------------------------------------------------------------------
def _read_clip(path: Path, rate: int) -> np.ndarray:
    import soundfile as sf
    data, sr = sf.read(str(path), dtype="int16", always_2d=True)
    pcm = data[:, 0].copy()
    if sr != rate:
        from duplexbench.channel.resample import Resampler
        rs = Resampler(sr, rate)
        pcm = np.concatenate([rs.push(pcm), rs.flush()]).astype(np.int16)
    return pcm


def is_simulated(scenario: Any) -> bool:
    """Track C simulated-caller scenario (schema field `source: simulated`)."""
    return getattr(scenario, "source", None) == "simulated"


def load_clips(scenario: Any, scenario_path: Path, rate: int) -> dict[str, np.ndarray]:
    """The ScriptedCaller / OverlayEngine `clips` mapping, read at the line rate.

    Keys and the resumption convention (`<clip stem>_resumption<ext>`) come from
    `scenarios.loader.clip_sources`; a simulated scenario needs no clips."""
    from duplexbench.scenarios.loader import clip_sources
    if is_simulated(scenario):
        return {}
    for st in scenario.steps:
        if not st.clip:
            raise ValueError(f"{scenario.id}: step {st.id!r} has no clip (scripted caller needs audio)")
    req, opt = clip_sources(scenario, scenario_path.parent)
    clips: dict[str, np.ndarray] = {}
    for key, p in req.items():
        if not p.is_file():
            raise ValueError(f"{scenario.id}: clip {key!r} not found: {p.name}"
                             + (" (resumption convention <clip stem>_resumption<ext>)"
                                if key.endswith("/resumption") else ""))
        clips[key] = _read_clip(p, rate)
    for key, p in opt.items():
        if p.is_file():
            clips[key] = _read_clip(p, rate)
    return clips


# -- adapters -------------------------------------------------------------------------
def make_adapter(system: Any, clock: Any, th: Any, tool_schemas: list[dict] | None = None,
                 tool_server: Any = None):
    """Build the adapter registered for `system.adapter` (systems.ADAPTERS).

    Hosted S2S adapters get the tool schemas (tools arrive as `tool_call` events);
    harness-as-carrier adapters (pipecat_cascade, dograh_stack) get the ToolServer as
    `tool_events` (their tools arrive over HTTP, never as events)."""
    from duplexbench.systems import adapter_class
    cls = adapter_class(system.adapter)
    if system.adapter == "mock_agent":
        from duplexbench.adapters.mock_agent import MockProgram
        return cls(clock, MockProgram(**(system.params.get("mock") or {})), th)
    if system.adapter in ("openai_realtime", "gemini_live", "grok_voice", "gpt_live"):
        return cls(clock, system, tool_schemas=tool_schemas)
    if system.adapter in ("pipecat_cascade", "dograh_stack"):
        return cls(clock, system, tool_events=tool_server)
    return cls(clock, system)


def adapter_rate(adapter: Any, condition: Any) -> int:
    """Sample rate of the audio the adapter emits (the return-path resampler input)."""
    prog = getattr(adapter, "program", None)
    if prog is not None:
        return int(prog.rate)
    from duplexbench.adapters._convert import MULAW_RATE
    name = getattr(adapter, "name", "")
    if name == "openai_realtime":
        from duplexbench.adapters.openai_realtime import PCM_RATE
        return MULAW_RATE if getattr(condition, "codec", True) else PCM_RATE
    if name == "gemini_live":
        from duplexbench.adapters.gemini_live import OUTPUT_RATE
        return int(adapter.acfg.get("output_rate") or OUTPUT_RATE)        # PCM16 24 kHz
    if name == "selfhosted_fd":
        return int(adapter.out_rate)                                      # 24 kHz default
    return MULAW_RATE        # grok_voice, gpt_live, pipecat_cascade, dograh_stack: μ-law 8 kHz


# -- planning -------------------------------------------------------------------------
@dataclass
class CallPlan:
    call_id: str
    system_id: str
    scenario: Any
    condition: str
    trial: int
    minutes: float
    projected_usd: float
    index: int
    is_control: bool = False            # control twin of the call above it (spec §3.4)


@dataclass
class RunPlan:
    cfg: RunConfig
    th: Any
    pack: Any
    scenarios: list
    systems: dict
    prices: dict
    conditions: dict
    clips: dict
    calls: list[CallPlan]
    results_dir: Path
    hashes: dict
    prereg_diffs: list[str]
    preregistered_at: str | None
    calibration: dict
    projection: dict[str, dict] = field(default_factory=dict)
    sim_llm: Any = None                 # Track C caller LLM (injected; no paid client in the CLI)
    sim_tts: Any = None                 # Track C TTS client (None -> FakeTTS)
    sim_refasr: Any = None              # Track C reference ASR (RefASR; see build_refasr)

    @property
    def total_usd(self) -> float:
        return sum(c.projected_usd for c in self.calls)


def calibration_status(results_dir: Path) -> dict:
    recs = sorted(results_dir.glob("calibration-*.json")) if results_dir.is_dir() else []
    if not recs:
        return {"status": "missing"}
    p = recs[-1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "unreadable", "path": str(p)}
    age_h = (_dt.datetime.now().timestamp() - p.stat().st_mtime) / 3600.0
    return {"status": "pass" if data.get("pass") else "fail", "path": p.name,
            "age_h": round(age_h, 2), "host": data.get("host"), "refasr": data.get("refasr")}


def refasr_passed(calibration: dict) -> bool:
    """D4: the latest calibration's reference-ASR stage passed (`refasr.passed` true)."""
    ra = calibration.get("refasr")
    return isinstance(ra, dict) and ra.get("passed") is True


def plan_run(cfg: RunConfig, *, results_dir: Path | None = None) -> RunPlan:
    from duplexbench import preregister
    from duplexbench.channel.chain import load_condition
    from duplexbench.metrics import cost
    from duplexbench.scenarios.loader import load_pack
    from duplexbench.systems import load_prices, load_systems
    from duplexbench.thresholds import Thresholds

    th_path = _path(cfg.thresholds, None) if cfg.thresholds else None
    th = Thresholds.load(th_path)
    if cfg.concurrency != 1:
        raise RunRefused(EXIT_CONFIG, "concurrency must be 1 in M3 (load runs arrive in M9)")
    pack_dir = _path(cfg.pack, REPO_ROOT / "scenarios" / "public-dev")
    pack = load_pack(pack_dir, th=th)
    scen = sorted(pack.scenarios, key=lambda s: s.id)
    if cfg.scenarios is not None:
        want = set(cfg.scenarios)
        missing = want - {s.id for s in scen}
        if missing:
            raise RunRefused(EXIT_CONFIG, f"scenarios not in pack {pack_dir}: {sorted(missing)}")
        scen = [s for s in scen if s.id in want]
    if cfg.track:
        scen = [s for s in scen if s.track == cfg.track]
    if cfg.limit is not None:
        scen = scen[:cfg.limit]
    if not scen:
        raise RunRefused(EXIT_CONFIG, f"no scenarios selected from {pack_dir}")
    systems_dir = _path(cfg.systems_dir, REPO_ROOT / "configs" / "systems")
    systems = load_systems(cfg.systems, systems_dir)
    prices = {sid: load_prices(s.price_ref) for sid, s in systems.items()}
    conditions = {c: load_condition(c, th) for c in cfg.conditions}
    clips = {}
    for s in scen:
        for cname, cond in conditions.items():
            clips[(s.id, cname)] = load_clips(s, pack.paths[s.id], cond.rate)
    res_dir = results_dir or _path(cfg.results_dir, REPO_ROOT / "results")

    hashes = preregister.collect_hashes(systems_dir=systems_dir, pack_dir=pack_dir,
                                        thresholds_path=th_path)
    diffs = preregister.check_against_latest(hashes, res_dir)
    latest = preregister.latest_record(res_dir)
    prereg_at = latest[1].get("created_utc") if (latest and not diffs) else None

    calls: list[CallPlan] = []
    i = 0
    for sid, system in systems.items():
        for cname in cfg.conditions:
            for s in scen:
                trials = cfg.trials if cfg.trials is not None else s.trials
                minutes = cost.est_call_minutes(s, th.eend_silence_ms)
                twin = bool(s.control_twin) and not is_simulated(s)
                for t in range(trials):
                    usd = cost.project_call_usd(minutes, prices[sid])
                    calls.append(CallPlan(f"{sid}-{s.id}-{cname}-t{t}", sid, s, cname, t,
                                          minutes, usd, i))
                    i += 1
                    if twin:        # the overlay call's control twin, same seeds (golden M2)
                        calls.append(CallPlan(f"{sid}-{s.id}-{cname}-t{t}-ctl", sid, s, cname,
                                              t, minutes, usd, i - 1, is_control=True))
    plan = RunPlan(cfg, th, pack, scen, systems, prices, conditions, clips, calls, res_dir,
                   hashes, diffs, prereg_at, calibration_status(res_dir))
    for sid in systems:
        mine = [c for c in calls if c.system_id == sid]
        plan.projection[sid] = {
            "calls": len(mine), "est_minutes": round(sum(c.minutes for c in mine), 3),
            "usd_per_min": cost.usd_per_min(prices[sid]),
            "usd": round(sum(c.projected_usd for c in mine), 4),
            "price_source": (prices[sid] or {}).get("source_url"),
            "price_checked": (prices[sid] or {}).get("checked")}
    return plan


def print_plan(plan: RunPlan, echo=typer.echo) -> None:
    echo(f"run {plan.cfg.name}: {len(plan.scenarios)} scenarios x {len(plan.cfg.conditions)} "
         f"conditions x {len(plan.systems)} systems = {len(plan.calls)} calls")
    echo(f"pack {plan.hashes.get('pack', '')[:12]}  thresholds {plan.th.hash()[:12]}")
    if plan.prereg_diffs:
        echo("preregister: NOT registered")
        for line in plan.prereg_diffs:
            echo(f"  {line}")
    else:
        echo(f"preregister: ok ({plan.preregistered_at})")
    c = plan.calibration
    echo(f"calibration: {c.get('status')}" + (f" ({c.get('path')}, {c.get('age_h')} h old)"
                                               if c.get("path") else ""))
    echo("projection (calls x est. minutes x USD/min):")
    for sid, p in plan.projection.items():
        src = f"  [{p['price_source']} checked {p['price_checked']}]" if p["price_source"] else \
            "  [unpriced: free]"
        echo(f"  {sid}: {p['calls']} calls x {p['est_minutes']:.2f} min total x "
             f"{p['usd_per_min']:.4f} USD/min = {p['usd']:.4f} USD{src}")
    echo(f"  total projected: {plan.total_usd:.4f} USD")


def check_plan(plan: RunPlan, *, max_usd: float, yes: bool, allow_unregistered: bool) -> None:
    if plan.prereg_diffs:
        public = all(s.split == "public-dev" for s in plan.scenarios)
        if not (allow_unregistered and public):
            why = "" if public else " (private-test scenarios are never run unregistered)"
            from duplexbench.preregister import refusal_message
            raise RunRefused(EXIT_PREREG, "refused: configuration differs from the latest "
                             f"pre-registration{why}; run `duplexbench preregister` or pass "
                             "--allow-unregistered for a public-dev run; "
                             + refusal_message(plan.prereg_diffs))
    from duplexbench.systems import preflight_system
    missing = [line for sys_ in plan.systems.values() for line in preflight_system(sys_)]
    if missing:   # adapter preconditions (keys, ids) are checked before any call, fix-22
        sep = chr(10) + "  "
        raise RunRefused(EXIT_CONFIG, "refused: preflight failed:" + sep + sep.join(missing))
    if plan.cfg.require_calibration and plan.calibration.get("status") != "pass":
        raise RunRefused(EXIT_PREREG, f"refused: calibration {plan.calibration.get('status')}")
    sim = sorted(s.id for s in plan.scenarios if is_simulated(s))
    if sim and not refasr_passed(plan.calibration):
        ra = plan.calibration.get("refasr")
        why = "missing" if not isinstance(ra, dict) else f"passed={ra.get('passed')!r}"
        raise RunRefused(EXIT_PREREG, f"refused (D4): simulated Track C scenarios {sim} need a "
                         f"calibration whose reference-ASR stage passed (calibration.refasr "
                         f"{why}); run `duplexbench calibrate` with the pinned reference ASR")
    if sim and plan.sim_llm is None:
        raise RunRefused(EXIT_CONFIG, f"refused: simulated Track C scenarios {sim} need a "
                         "simulator LLM; the paid simulator client is not wired into the CLI "
                         "yet (M4-T13), only injected clients (tests) run Track C")
    if plan.total_usd > max_usd + 1e-12:
        raise RunRefused(EXIT_BUDGET, f"refused: projected {plan.total_usd:.4f} USD > max_usd "
                         f"{max_usd:.4f} USD")
    if not yes:
        raise RunRefused(EXIT_NOT_STARTED, "not started: re-run with --yes to spend the "
                         "projected amount")


# -- execution ------------------------------------------------------------------------
def config_json(plan: RunPlan) -> dict:
    return {"systems": {sid: s.report_entry(plan.preregistered_at)
                        for sid, s in plan.systems.items()},
            "comparisons": plan.cfg.comparisons,
            "baseline_condition": plan.cfg.baseline_condition,
            "scenarios": {s.id: s.model_dump(mode="json") for s in plan.scenarios},
            "coverage": plan.cfg.coverage, "planned_calls": len(plan.calls),
            "run": plan.cfg.model_dump(mode="json")}


def run_card(plan: RunPlan, run_id: str, summary: dict | None = None) -> dict:
    from importlib.metadata import PackageNotFoundError, version
    from duplexbench.clock import clock_info
    try:
        ver = version("duplexbench")
    except PackageNotFoundError:
        ver = "unknown"
    vendors = sorted({s.adapter for s in plan.systems.values()
                      if not s.self_hosted and s.adapter != "mock_agent"})
    return {
        "date": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "harness_version": ver,
        "run_id": run_id,
        "systems": {sid: {"adapter": s.adapter, "model": s.model, "price_ref": s.price_ref,
                          "price_source_url": (plan.prices[sid] or {}).get("source_url"),
                          "price_checked": (plan.prices[sid] or {}).get("checked"),
                          "projection": plan.projection.get(sid)}
                    for sid, s in plan.systems.items()},
        "vendors_audio": vendors,
        "instance_type": f"{platform.node()} {platform.platform()}",
        "clock": clock_info(),
        "refasr": None, "simulator": None, "iteration_counts": None, "network_baseline": None,
        "preregister_diffs": plan.prereg_diffs,
        "summary": summary,
    }


def _call_row(db: Path, call_id: str) -> dict | None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


async def _one_call(plan: RunPlan, cp: CallPlan, store: Any, run_id: str, out_dir: Path,
                    tool_server: Any) -> tuple[Any, Any]:
    """Build and run one call on a fresh RealClock; returns (CallResult, adapter).

    Scripted (Track A/D): `overlays.scripted_run.run_scripted_call` -- ScriptedCaller
    (or its control twin), OverlayEngine via `make_engine`, runner -- the same call
    sequence as the golden M2. Simulated (Track C): `_simulated_call`."""
    from duplexbench.clock import RealClock
    from duplexbench.overlays.scripted_run import run_scripted_call

    th, cond, s = plan.th, plan.conditions[cp.condition], cp.scenario
    system = plan.systems[cp.system_id]
    clock = RealClock()
    seed_fwd, seed_back = plan.cfg.seed + 2 * cp.index, plan.cfg.seed + 2 * cp.index + 1

    async def body():
        tools, schemas = None, None
        if s.tools and tool_server is not None:
            tools, schemas = _register_tools(plan, cp, tool_server, store, clock, run_id)
        try:
            if is_simulated(s):
                return await _simulated_call(plan, cp, store, run_id, out_dir, tool_server,
                                             clock, tools, schemas, seed_fwd, seed_back)
            adapter = make_adapter(system, clock, th, schemas, tool_server)
            res = await run_scripted_call(
                clock, store, s, plan.clips[(s.id, cp.condition)], adapter, th, out_dir,
                run_id=run_id, condition=cp.condition, trial=cp.trial,
                is_control=cp.is_control, system_id=cp.system_id, seed_fwd=seed_fwd,
                seed_back=seed_back, call_id=cp.call_id, tool_token=cp.call_id, tools=tools,
                projected_usd=cp.projected_usd, back_rate=adapter_rate(adapter, cond), cond=cond)
            return res, adapter
        finally:
            if tools is not None:
                tool_server.unregister_call(cp.call_id)

    return await clock.run(body())


def _register_tools(plan: RunPlan, cp: CallPlan, tool_server: Any, store: Any, clock: Any,
                    run_id: str, seed: int | None = None) -> tuple[Any, list[dict]]:
    """Bind this call's token to a fresh domain backend on the ToolServer."""
    from duplexbench.tools.mock_backends import get_backend
    s = cp.scenario
    backend = _make_backend(get_backend(s.domain), s, seed)
    tool_server.register_call(cp.call_id, backend, s.tools, store, clock, run_id=run_id,
                              call_id=cp.call_id)
    tools = tool_server.client(cp.call_id)
    return tools, tools.tools()


def _make_backend(cls: Any, s: Any, seed: int | None) -> Any:
    import inspect
    kw: dict[str, Any] = {"policy": s.policy}
    if seed is not None and "seed" in inspect.signature(cls).parameters:
        kw["seed"] = seed
    return cls(**kw)


def build_refasr(sim_cfg: dict) -> Any:
    """The reference-ASR engine named by `simulator.refasr` ({candidate, kwargs}), or None.

    Raises ValueError for an unknown candidate; the wrapper raises CandidateUnavailable
    when its package or model is missing (no download is made here)."""
    import importlib

    from duplexbench.refasr.candidates_table import CANDIDATES
    spec = (sim_cfg or {}).get("refasr")
    if not spec:
        return None
    if not isinstance(spec, dict) or "candidate" not in spec:
        raise ValueError("simulator.refasr must be {candidate: <name>, kwargs: {...}}")
    by_name = {c.name: c for c in CANDIDATES}
    c = by_name.get(spec["candidate"])
    if c is None:
        raise ValueError(f"simulator.refasr.candidate {spec['candidate']!r} not in "
                         f"{sorted(by_name)}")
    mod = importlib.import_module(f"duplexbench.refasr.candidates.{c.module}")
    return getattr(mod, c.cls)(**dict(spec.get("kwargs") or {}))


def refasr_id(engine: Any) -> str:
    """`name:version`, the form calibrate.refasr_stage records."""
    return f"{getattr(engine, 'name', '?')}:{getattr(engine, 'version', '?')}"


class AgentTextTap:
    """Adapter proxy that hands every `output_transcript` text to `on_text` (the
    SimulatedCaller's `on_provider_text`: kept for mishearing_rate, never heard by the
    caller) and passes all events through unchanged."""

    def __init__(self, inner: Any, on_text: Any):
        self.inner, self.on_text = inner, on_text

    def __getattr__(self, k: str) -> Any:        # name, caps, config, usage_log, program, ...
        return getattr(self.inner, k)

    async def open(self, call: Any) -> None:
        await self.inner.open(call)

    async def send_frame(self, mulaw_bytes: bytes, t_ns: int) -> None:
        await self.inner.send_frame(mulaw_bytes, t_ns)

    async def tool_result(self, tool_call_id: str, result: dict) -> None:
        await self.inner.tool_result(tool_call_id, result)

    async def close(self) -> None:
        await self.inner.close()

    def events(self):
        return self._events()

    async def _events(self):
        async for ev in self.inner.events():
            if ev.kind == "output_transcript":
                self.on_text(str(ev.payload.get("text") or ""))
            yield ev

    def safe_events(self, call: Any):
        from duplexbench.adapters.base import Adapter
        return Adapter.safe_events(self, call)       # the base wrapper, over our events()


async def _simulated_call(plan: RunPlan, cp: CallPlan, store: Any, run_id: str, out_dir: Path,
                          tool_server: Any, clock: Any, tools: Any, schemas: Any,
                          seed_fwd: int, seed_back: int) -> tuple[Any, Any]:
    """One Track C call: SimulatedCaller + adapter (transcript tap) + runner, no overlays."""
    from duplexbench.channel.chain import ChannelChain
    from duplexbench.context import CallContext
    from duplexbench.playout import Playout
    from duplexbench.recorder import Recorder
    from duplexbench.runner import run_call
    from duplexbench.simulator.audio import CallerVoice
    from duplexbench.simulator.simulated_caller import SimulatedCaller

    th, cond, s = plan.th, plan.conditions[cp.condition], cp.scenario
    sim_cfg = dict(plan.cfg.simulator or {})
    tts = plan.sim_tts
    if tts is None:
        from duplexbench.stimuli.fake_tts import FakeTTS
        tts = FakeTTS()
    voice = CallerVoice(tts, s.caller_voice, s.speaking_rate, threaded=False)
    from duplexbench.refasr.engine import FakeASR
    engine = plan.sim_refasr
    if engine is None:
        raise RunRefused(EXIT_CONFIG, "Track C: no reference ASR for the simulated caller "
                                      "(simulator.refasr or an injected engine; spec §7)")
    caller = SimulatedCaller(s, plan.sim_llm, voice, clock, th, store, run_id=run_id,
                             call_id=cp.call_id, rng_seed=seed_fwd,
                             tool_server=tool_server if tools is not None else None,
                             tool_token=cp.call_id,
                             caller_first=bool(sim_cfg.get("caller_first", True)),
                             rate=cond.rate, refasr=engine,
                             asr_threaded=not isinstance(engine, FakeASR))
    inner = make_adapter(plan.systems[cp.system_id], clock, th, schemas, tool_server)
    adapter = AgentTextTap(inner, caller.on_provider_text)
    ctx = CallContext(run_id=run_id, call_id=cp.call_id, scenario=s, trial=cp.trial,
                      condition=cond, system_id=cp.system_id, is_control=False, clock=clock,
                      store=store, tool_token=cp.call_id)
    rec = Recorder(out_dir / f"{cp.call_id}.wav", cond.rate, origin_ns=clock.now_ns())
    playout = Playout(clock, store, rec, th, run_id=run_id, call_id=cp.call_id, rate=cond.rate)
    caller.attach(playout)
    chain = ChannelChain(cond, seed_fwd, seed_back)
    chain.back_delay_ms(adapter_rate(inner, cond))
    try:
        res = await run_call(ctx, adapter, caller, None, playout, chain, tools, None, th=th,
                             projected_usd=cp.projected_usd)
    finally:
        caller.close()
        await caller.finish_ear()
    store.put("events", {"run_id": run_id, "call_id": cp.call_id, "t_ns": clock.now_ns(),
                         "seq": playout.next_seq(), "kind": "mishearing", "src": "harness",
                         "payload_json": canonical_json({
                             "refasr": refasr_id(engine), "refasr_text": caller.refasr_text,
                             "provider_text": caller.provider_text})})
    if tools is not None:
        store.put("events", {"run_id": run_id, "call_id": cp.call_id, "t_ns": clock.now_ns(),
                             "seq": playout.next_seq(), "kind": "final_state", "src": "harness",
                             "payload_json": canonical_json(tool_server.state(cp.call_id))})
    spoken = [r for r in caller.rows if not r.get("discarded")]
    inner.sim_result = {
        "caller_turns": [t for who, t in caller.transcript if who == "caller"],
        "audio_ok": bool(spoken) and caller.error is None,
        "flags": dict(caller.flags),
        "error": repr(caller.error) if caller.error is not None else None}
    return res, inner


async def execute(plan: RunPlan, *, max_usd: float, echo=typer.echo) -> dict:
    """Run every planned call sequentially; returns the run summary."""
    import dataclasses

    from duplexbench.budget import Budget, BudgetExceeded
    from duplexbench.metrics import cost
    from duplexbench.simulator.validation_gate import TrialResult, run_with_regeneration
    from duplexbench.storage import Store

    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = plan.cfg.run_id or f"{plan.cfg.name}-{ts}"
    out_dir = plan.results_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / "events.sqlite"
    store = Store(db)
    started = _dt.datetime.now(_dt.timezone.utc)
    runs_row = {"run_id": run_id, "started_ns": int(started.timestamp() * 1e9),
                "config_json": canonical_json(config_json(plan)),
                "hashes_json": canonical_json(plan.hashes),
                "calibration_json": canonical_json(plan.calibration),
                "run_card_json": canonical_json(run_card(plan, run_id))}
    store.put("runs", runs_row)
    await store.flush()
    tool_server = None
    if any(s.tools for s in plan.scenarios):
        from duplexbench.tools.server import ToolServer
        tool_server = ToolServer()
    budget = Budget(max_usd)
    outcomes: dict[str, int] = {}
    usd_total, results = 0.0, []
    state = {"stopped": None}
    echo(f"run_id {run_id} -> {out_dir}")

    def _put_flags(call_id: str, extra: dict, outcome: str | None = None) -> None:
        row = _call_row(db, call_id)
        if not row:
            return
        flags = json.loads(row.get("flags_json") or "{}")
        flags.update(extra)
        row["flags_json"] = canonical_json(flags)
        if outcome is not None:
            row["outcome"] = outcome
        store.put("calls", row)

    async def attempt(cp: CallPlan) -> tuple[Any, Any, float | None] | None:
        """One call with its Budget reservation; None when the budget refused it."""
        nonlocal usd_total
        try:
            resv = budget.reserve(cp.projected_usd)
        except BudgetExceeded as e:
            store.put("calls", {
                "call_id": cp.call_id, "run_id": run_id, "scenario_id": cp.scenario.id,
                "trial": cp.trial, "condition": cp.condition, "system_id": cp.system_id,
                "is_control": int(cp.is_control), "outcome": "budget_refused",
                "started_ns": None, "ended_ns": None, "wav_path": None,
                "usd_projected": cp.projected_usd, "usd_actual": None,
                "flags_json": canonical_json({"budget": str(e)})})
            outcomes["budget_refused"] = outcomes.get("budget_refused", 0) + 1
            echo(f"{cp.call_id}: budget_refused ({e}); run stopped")
            state["stopped"] = "budget"
            return None
        res, adapter = await _one_call(plan, cp, store, run_id, out_dir, tool_server)
        await store.flush()
        row = _call_row(db, cp.call_id) or {}
        minutes = ((row.get("ended_ns") or 0) - (row.get("started_ns") or 0)) / \
            _NS_PER_MS / _MS_PER_MIN
        usage = list(getattr(adapter, "usage_log", []) or [])
        prices = plan.prices[cp.system_id]
        has_usage = hasattr(adapter, "usage_log")
        actual = (cost.price_per_call(usage if has_usage else None, prices, minutes=minutes)
                  if prices else 0.0)
        # A dropped call may have been billed for a response whose usage never arrived:
        # its actual is a lower bound, so the budget is charged the projection instead.
        lower_bound = res.outcome == "transport_error" and prices is not None
        budget.settle(resv, max(actual or 0.0, cp.projected_usd) if lower_bound else actual)
        if row:
            flags = json.loads(row.get("flags_json") or "{}")
            flags["usage_responses"] = len(usage)
            if lower_bound:
                flags["usd_lower_bound"] = True
            if usage:
                flags["usage_tokens"] = cost.sum_usage(usage)
            for k in ("unmapped", "warnings", "dropped_deltas"):
                v = getattr(adapter, k, None)
                if v:
                    flags[k] = v
            for attr in ("live", "stream"):        # the LiveStream, when there is one
                stream = getattr(adapter, attr, None)
                if callable(getattr(stream, "stats", None)):
                    flags["stream"] = stream.stats()
                    break
            sim = getattr(adapter, "sim_result", None)
            if sim:
                flags["simulator"] = {k: v for k, v in sim.items() if k != "caller_turns"}
            row.update(usd_actual=actual, flags_json=canonical_json(flags),
                       is_control=int(cp.is_control))
            store.put("calls", row)
            await store.flush()
        outcomes[res.outcome] = outcomes.get(res.outcome, 0) + 1
        usd_total += actual or 0.0
        err = (res.flags.get("error") or {}).get("message", "")
        usd_s = "null" if actual is None else f"{actual:.4f}"
        echo(f"{cp.call_id}: {res.outcome}  projected {cp.projected_usd:.4f} USD  "
             f"actual {usd_s} USD" + (f"  ({err[:120]})" if err else ""))
        results.append((cp.call_id, res.outcome, actual))
        if actual is None:
            echo("actual cost unknown (usd: null): run stopped before the next call")
            state["stopped"] = "unknown_cost"
        return res, adapter, actual

    async def simulated_trial(cp: CallPlan) -> None:
        """Track C: whole-trial regeneration (validation_gate), every attempt kept."""
        goal = [str(e.get("value")) for e in (cp.scenario.entities or []) if e.get("value")]

        async def run_trial(n: int) -> TrialResult:
            cpn = cp if n == 0 else dataclasses.replace(cp, call_id=f"{cp.call_id}-r{n}")
            if state["stopped"]:
                raise _Stop()
            got = await attempt(cpn)
            if got is None or state["stopped"]:
                raise _Stop()
            res, adapter, actual = got
            sim = getattr(adapter, "sim_result", None) or {}
            return TrialResult(cpn.call_id, list(sim.get("caller_turns") or []),
                               bool(sim.get("audio_ok")), res.outcome == "timeout", goal,
                               outcome=res.outcome, usd_actual=actual or 0.0)

        try:
            trials = await run_with_regeneration(run_trial, plan.th)
        except _Stop:
            return
        for t in trials:
            _put_flags(t.call_id, {"regenerated": t.regenerated, "gate_reasons": t.reasons,
                                   "scored": t.scored},
                       outcome="validation_failed" if t.outcome == "validation_failed" else None)
        last = trials[-1]
        if last.outcome == "validation_failed":
            outcomes["validation_failed"] = outcomes.get("validation_failed", 0) + 1
            echo(f"{last.call_id}: validation_failed {last.reasons}")
        await store.flush()

    try:
        for cp in plan.calls:
            if is_simulated(cp.scenario):
                await simulated_trial(cp)
            else:
                await attempt(cp)
            if state["stopped"]:
                break
        stopped = state["stopped"]
        summary = {"outcomes": outcomes, "usd_actual_total": round(usd_total, 6),
                   "usd_projected_total": round(plan.total_usd, 6), "stopped": stopped,
                   "calls_run": len(results)}
        runs_row["run_card_json"] = canonical_json(run_card(plan, run_id, summary))
        store.put("runs", runs_row)
        await store.flush()
    finally:
        store.close()
    echo(f"done: {outcomes}  actual {usd_total:.4f} USD"
         + (f"  (stopped: {stopped})" if stopped else ""))
    return {"run_id": run_id, "out_dir": out_dir, "db": db, **summary}


class _Stop(Exception):
    """A budget refusal / unknown cost inside a regeneration loop: stop the run."""


def _resolve_refasr(cfg: RunConfig) -> Any:
    from duplexbench.refasr.candidates import CandidateUnavailable
    try:
        engine = build_refasr(cfg.simulator)
    except (ValueError, CandidateUnavailable) as e:
        raise RunRefused(EXIT_CONFIG, f"config error: simulator.refasr: {e}") from e
    if engine is None:
        raise RunRefused(EXIT_CONFIG, "Track C needs a reference ASR for the simulated caller "
                                      "(spec §7 'Hears'): set simulator.refasr "
                                      "{candidate, kwargs} in the run config")
    return engine


def run_command(config: Path, *, yes: bool, max_usd: float | None, allow_unregistered: bool,
                results_dir: Path | None = None, echo=typer.echo, sim_llm: Any = None,
                sim_tts: Any = None, sim_refasr: Any = None) -> dict:
    """`sim_llm` / `sim_tts` / `sim_refasr`: Track C simulator clients (tests inject fakes)."""
    from duplexbench import _loop
    from duplexbench.scenarios.loader import PackError
    from duplexbench.systems import SystemError_
    try:
        cfg = RunConfig.load(config)
        plan = plan_run(cfg, results_dir=results_dir)
    except (PackError, SystemError_, ValueError, FileNotFoundError) as e:
        raise RunRefused(EXIT_CONFIG, f"config error: {e}") from e
    plan.sim_llm, plan.sim_tts = sim_llm, sim_tts
    print_plan(plan, echo)
    limit = cfg.max_usd if max_usd is None else max_usd
    check_plan(plan, max_usd=limit, yes=yes, allow_unregistered=allow_unregistered)
    if any(is_simulated(s) for s in plan.scenarios):
        plan.sim_refasr = sim_refasr if sim_refasr is not None else _resolve_refasr(cfg)
        cal = (plan.calibration.get("refasr") or {}).get("version")
        if cal and cal != refasr_id(plan.sim_refasr):
            echo(f"warning: simulator reference ASR {refasr_id(plan.sim_refasr)} differs from "
                 f"the calibrated one {cal}")
    return _loop.run(execute(plan, max_usd=limit, echo=echo))


def register(app: typer.Typer) -> None:
    @app.command()
    def run(
        config: Path = typer.Argument(..., help="run config YAML (configs/runs/*.yaml)"),
        yes: bool = typer.Option(False, "--yes", help="spend the projected amount and start"),
        max_usd: float = typer.Option(None, "--max-usd", help="override the config's max_usd"),
        allow_unregistered: bool = typer.Option(
            False, "--allow-unregistered", help="public-dev only: run without a matching "
                                                "pre-registration"),
        results_dir: Path = typer.Option(None, "--results-dir", help="default: config or results/"),
    ) -> None:
        """Run calls against systems under test (prints the projection; needs --yes)."""
        try:
            run_command(config, yes=yes, max_usd=max_usd, allow_unregistered=allow_unregistered,
                        results_dir=results_dir)
        except RunRefused as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=e.code)


if __name__ == "__main__":
    _app = typer.Typer(no_args_is_help=True, add_completion=False)

    @_app.callback()
    def _main() -> None:
        """duplexbench run command."""

    register(_app)
    _app(sys.argv[1:])
