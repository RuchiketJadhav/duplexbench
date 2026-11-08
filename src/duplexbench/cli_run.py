"""`duplexbench run` (BUILD_PLAN M3-T03/T04; spec §2.3 pre-registration, §4.4 cost, §9.3, §10 item 1).

    uv run duplexbench run configs/runs/smoke_mock.yaml --yes
    uv run duplexbench run configs/runs/smoke_openai.yaml [--yes] [--max-usd 5] [--allow-unregistered]
    uv run duplexbench run configs/runs/<dograh>.yaml [--tool-host 0.0.0.0] [--tool-port 8900]

HTTP tool server (fix-44): when a system's adapter sets `needs_http_tools` (dograh_stack),
`run_command` binds 0.0.0.0:<port of params.harness_tool_base> (8900 when the URL has no
port; 80/443 and non-http refused, exit 2; a port in use is refused, exit 2, naming the
port) before the projection and prints `tool server: listening on ...`. `execute` serves
`create_app(tool_server)` with uvicorn as a task on the run's asyncio loop from before the
first call until after the last (tools/http.py); the socket is closed on every exit path
(refusal, --yes missing, exception, Ctrl-C). The run card records `tool_server` {host,
port, bind} at the top level and beside each such system's `harness_tool_base`.

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
    simulator: {}                          # Track C (source: simulated): caller_first, refasr, llm
    scripted: {lead_in: caller_first}      # Track A/D: caller_first | agent_first (fix-34)
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
kept and costed).

Deepgram Nova-3 (refasr-deepgram, decision B11) is `{candidate: deepgram, kwargs:
{pinned_model_uuid?, api_key_env?, endpointing?, utterance_end_ms?, ...}}`; its
`version` reads `nova-3/unpinned` (or `nova-3/<pinned uuid>`) until its first stream
reports a model_uuid, then `nova-3/<uuid>`. So the pre-run comparison with the
calibration's `deepgram:nova-3/<uuid>` warns unless the run pins that uuid. After each
Track C call its flags carry `simulator.refasr` (`name:version` after that call), and
the run card summary carries `refasr_version_final` (the post-run `name:version`). A
stream whose uuid differs from `pinned_model_uuid` (RefASRVersionMismatch) stops the
run (`stopped: refasr_version_mismatch`).

Simulator LLM (sim-llm, decision B8): `run_command(..., sim_llm=)` (tests: fakes), else
the run config key

    simulator:
      llm: {kind: openai_compat, base_url | base_url_env, model, api_key_env?,
            temperature?, send_temperature?, max_tokens?, extra_body?, prompt?}

parsed before `check_plan` (`build_sim_llm`; config only, no network) into a
`CallerLLM(OpenAICompatClient(...))`. Neither given -> Track C refused (exit 2). The
endpoint (PhoneLLM Alpha 1 on vLLM / SGLang / Modal) is self-hosted: each Track C call's
flags carry `simulator.llm` {kind, model, base_url, billing: "external"}.

Simulated-caller costs (fix-28). A Track C call's projection is agent + reference ASR +
caller LLM (`apply_simulator_costs`, after plan_run, before print_plan; the Budget
reserves that total per attempt):
  * reference ASR: when the engine is paid (injected engine's `paid`, else the
    configured candidate's `Candidate.paid`), CallPlan.minutes (the scripted length
    `cost.est_call_minutes` that the agent projection also multiplies) x the engine
    module's `usd_per_audio_min()` (Deepgram: configs/prices/deepgram_nova3.yaml);
    actual = the call row's ended_ns - started_ns in minutes x the same rate.
  * caller LLM: only with `simulator.llm.price_ref` (e.g. openai_chat; rates per
    `model`): projected = (steps + 2) turns x [Assumption] 1,500 in / 60 out tokens
    (the price file's `projection` block); actual = the call's CallerLLM `usage_log`
    tokens x the rates. Without price_ref: 0 USD, flags `billing: external`.
The call row's usd_projected / usd_actual hold the totals; the split is in
flags_json["cost"] {projected: {agent, refasr, caller_llm}, actual: {...},
refasr_audio_min, caller_llm_usage}; the run summary carries usd_projected_parts /
usd_actual_parts. sim_turns has no token column, so usage is per call (flags), not
per turn. Track A/D calls are unchanged.

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
from typing import Any, Literal

import numpy as np
import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field

from duplexbench.storage import canonical_json

REPO_ROOT = Path(__file__).resolve().parents[2]
_NS_PER_MS = 1_000_000
_MS_PER_MIN = 60_000.0          # threshold-ok: unit conversion
EXIT_NOT_STARTED, EXIT_BUDGET, EXIT_PREREG, EXIT_CONFIG = 1, 3, 4, 2


class ScriptedConfig(BaseModel):
    """Track A/D scripted caller options (fix-34, WORKFLOW_SPEC gap G1).

    lead_in: caller_first (default; step 1 from the first frame, overlapping a greeting)
             | agent_first (step 1 after the agent's first end of turn: an utterance,
               then `eend_silence_ms` of ear silence; no agent onset within `stall_ms`
               -> call flag `no_greeting` and step 1 starts anyway). Call flags record
               `lead_in`, `greeting_end_ms`, `step1_start_ms`, `no_greeting`.
    The Track C counterpart is `simulator.caller_first`."""
    model_config = ConfigDict(extra="forbid")

    lead_in: Literal["caller_first", "agent_first"] = "caller_first"


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
    scripted: ScriptedConfig = Field(default_factory=ScriptedConfig)   # Track A/D (fix-34)
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
    usd_parts: dict | None = None       # Track C: {agent, refasr, caller_llm}; sum = projected_usd


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
    sim_llm: Any = None                 # Track C caller LLM (injected, or simulator.llm)
    sim_llm_info: dict | None = None    # {kind, model, base_url, billing} of simulator.llm
    sim_tts: Any = None                 # Track C TTS client (None -> FakeTTS)
    sim_refasr: Any = None              # Track C reference ASR (RefASR; see build_refasr)
    sim_cost: dict | None = None        # Track C simulator pricing (simulator_costs, fix-28)
    tool_http: dict | None = None       # fix-44: {host, port, bind, bases: {sid: base}} or None

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

    # fix-30: the run config's simulator and scripted blocks enter the pre-registration hash
    hashes = preregister.collect_hashes(systems_dir=systems_dir, pack_dir=pack_dir, run_config=cfg,
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
    for line in simulator_cost_lines(plan):
        echo(line)
    echo(f"  total projected: {plan.total_usd:.4f} USD")


def simulator_cost_lines(plan: RunPlan) -> list[str]:
    """The three per-run parts (agent, reference ASR, caller LLM) when Track C calls exist."""
    sim = [c for c in plan.calls if c.usd_parts is not None]
    if not sim or not plan.sim_cost:
        return []
    sc = plan.sim_cost
    agent = sum(c.usd_parts["agent"] if c.usd_parts else c.projected_usd for c in plan.calls)
    ra_usd = sum(c.usd_parts["refasr"] for c in sim)
    llm_usd = sum(c.usd_parts["caller_llm"] for c in sim)
    ra, llm = sc["refasr"], sc["caller_llm"]
    mins = sum(c.minutes for c in sim)
    if ra["paid"]:
        src = f" [{ra['source_url']} checked {ra['checked']}]" if ra.get("source_url") else ""
        ra_d = (f"{ra['name']}: {len(sim)} Track C calls x {mins:.2f} est. min total x "
                f"{ra['usd_per_audio_min']:.4f} USD/audio min{src}")
    else:
        ra_d = f"{ra['name']}: not a paid engine"
    if llm["billing"] == "per_token":
        turns = sum(c.usd_parts.get("caller_turns") or 0 for c in sim)
        llm_d = (f"{llm['model']}: {turns} caller turns x ({llm['input_tokens_per_turn']} in x "
                 f"{llm['input_per_1m']} + {llm['output_tokens_per_turn']} out x "
                 f"{llm['output_per_1m']}) USD/1M tokens, token counts [Assumption] "
                 f"[{llm['source_url']} checked {llm['checked']}]")
    else:
        llm_d = f"{llm.get('model') or 'injected client'}: billing external (0 USD in the harness)"
    return [f"  agent: {agent:.4f} USD (the system lines above)",
            f"  reference ASR: {ra_usd:.4f} USD  ({ra_d})",
            f"  caller LLM: {llm_usd:.4f} USD  ({llm_d})"]


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
                         "simulator LLM: set simulator.llm {kind, base_url, model} in the run "
                         "config (spec §7)")
    if plan.total_usd > max_usd + 1e-12:
        raise RunRefused(EXIT_BUDGET, f"refused: projected {plan.total_usd:.4f} USD > max_usd "
                         f"{max_usd:.4f} USD")
    if not yes:
        raise RunRefused(EXIT_NOT_STARTED, "not started: re-run with --yes to spend the "
                         "projected amount")


def http_tool_systems(plan: RunPlan) -> dict[str, str | None]:
    """{system_id: params.harness_tool_base} of the systems whose adapter calls the harness
    tools over HTTP (`Adapter.needs_http_tools`, fix-44)."""
    from duplexbench.systems import adapter_class
    out: dict[str, str | None] = {}
    for sid, s in plan.systems.items():
        if getattr(adapter_class(s.adapter), "needs_http_tools", False):
            out[sid] = (s.params or {}).get("harness_tool_base")
    return out


def plan_tool_http(plan: RunPlan, *, tool_host: str | None = None,
                   tool_port: int | None = None) -> dict | None:
    """Where the tool server listens, or None when no system needs HTTP tools.

    Port: `tool_port`, else the port of every such system's harness_tool_base (they must
    agree). Host: `tool_host`, else 0.0.0.0. Raises RunRefused(EXIT_CONFIG)."""
    from duplexbench.tools.http import (DEFAULT_TOOL_HOST, ToolPortError, check_port,
                                        port_from_base)
    bases = http_tool_systems(plan)
    if not bases:
        return None
    try:
        if tool_port is not None:
            port = check_port(int(tool_port), "--tool-port")
        else:
            ports = {sid: port_from_base(b) for sid, b in bases.items()}
            if len(set(ports.values())) > 1:
                raise ToolPortError(f"systems disagree on the harness_tool_base port {ports}; "
                                    "pass --tool-port")
            port = next(iter(ports.values()))
    except ToolPortError as e:
        raise RunRefused(EXIT_CONFIG, f"config error: {e}") from e
    host = tool_host or DEFAULT_TOOL_HOST
    return {"host": host, "port": port, "bind": f"{host}:{port}", "bases": bases}


def tool_server_card(plan: RunPlan) -> dict | None:
    th = plan.tool_http
    return None if th is None else {k: th[k] for k in ("host", "port", "bind")}


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
    ts_card = tool_server_card(plan)
    http_bases = (plan.tool_http or {}).get("bases") or {}
    systems = {sid: {"adapter": s.adapter, "model": s.model, "price_ref": s.price_ref,
                     "price_source_url": (plan.prices[sid] or {}).get("source_url"),
                     "price_checked": (plan.prices[sid] or {}).get("checked"),
                     "projection": plan.projection.get(sid)}
               for sid, s in plan.systems.items()}
    for sid, base in http_bases.items():        # fix-44: where Dograh calls, where we listen
        systems[sid].update(harness_tool_base=base, tool_server=ts_card)
    return {
        "date": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "harness_version": ver,
        "run_id": run_id,
        "systems": systems,
        "tool_server": ts_card,                  # fix-44: null when no HTTP tool server ran
        "vendors_audio": vendors,
        "instance_type": f"{platform.node()} {platform.platform()}",
        "clock": clock_info(),
        "refasr": None, "simulator": simulator_card(plan, summary),     # fix-27
        "iteration_counts": None, "network_baseline": None,
        "scripted": {"lead_in": plan.cfg.scripted.lead_in},     # fix-34 disclosure
        "preregister_diffs": plan.prereg_diffs,
        "summary": summary,
    }


def _tts_info(tts: Any) -> dict:
    """vendor / model / version / default_voice of a TTS client (None -> FakeTTS, as
    simulated_call uses)."""
    if tts is None:
        return {"vendor": "fake"}
    out = {k: getattr(tts, k, None) for k in ("vendor", "model", "version", "default_voice")}
    return {k: v for k, v in out.items() if v is not None} or {"vendor": type(tts).__name__}


def _stimulus_tts(plan: RunPlan) -> dict:
    """TTS of the scripted callers' clips: vendor / voice from each clip's `.json` sidecar
    (stimuli generate writes {vendor, voice, rate, ...}; no model is stored there)."""
    from duplexbench.scenarios.loader import clip_sources
    vendors: set[str] = set()
    voices: set[str] = set()
    paths = getattr(plan.pack, "paths", None) or {}
    for s in plan.scenarios:
        if is_simulated(s) or s.id not in paths:
            continue
        req, opt = clip_sources(s, Path(paths[s.id]).parent)
        for p in [*req.values(), *opt.values()]:
            side = p.with_suffix(".json")
            try:
                meta = json.loads(side.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("vendor"):
                vendors.add(str(meta["vendor"]))
            if meta.get("voice"):
                voices.add(str(meta["voice"]))
    return {"source": "stimulus_clips", "vendors": sorted(vendors), "voices": sorted(voices)}


def simulator_card(plan: RunPlan, summary: dict | None = None) -> dict:
    """Run card `simulator` block (fix-27; spec §7, §10 item 1).

    Scripted scenarios (Track A/D) give `scripted` {lead_in, tts (clip sidecars)}.
    Simulated scenarios (Track C) add `simulated` {caller_first, llm {kind, model,
    base_url, billing, price_ref?, prompt_sha256}, refasr `name:version` (the post-run
    one, `refasr_version_final`, once the summary exists), tts {vendor, model, version,
    voices}}. prompt_sha256 is the CallerLLM's prompt hash, else the pre-registration
    record's `run_config` field `simulator.llm.prompt_sha256` (fix-30)."""
    out: dict[str, Any] = {}
    sim_scen = [s for s in plan.scenarios if is_simulated(s)]
    if len(sim_scen) < len(plan.scenarios):
        out["scripted"] = {"lead_in": plan.cfg.scripted.lead_in, "tts": _stimulus_tts(plan)}
    if not sim_scen:
        return out
    fields = ((plan.hashes or {}).get("run_config") or {}).get("fields") or {}
    info = dict(plan.sim_llm_info or getattr(plan.sim_llm, "endpoint_info", None) or {})
    llm = {k: info.get(k) for k in ("kind", "model", "base_url", "billing")}
    if info.get("price_ref"):
        llm["price_ref"] = info["price_ref"]
    llm["prompt_sha256"] = (getattr(plan.sim_llm, "prompt_hash", None)
                            or fields.get("simulator.llm.prompt_sha256"))
    refasr = (summary or {}).get("refasr_version_final")
    if refasr is None and plan.sim_refasr is not None:
        refasr = refasr_id(plan.sim_refasr)
    tts = _tts_info(plan.sim_tts)
    tts["voices"] = sorted({s.caller_voice for s in sim_scen})
    out["simulated"] = {"caller_first": bool((plan.cfg.simulator or {}).get("caller_first", True)),
                        "llm": llm, "refasr": refasr, "tts": tts}
    return out


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
                projected_usd=cp.projected_usd, back_rate=adapter_rate(adapter, cond), cond=cond,
                lead_in=plan.cfg.scripted.lead_in)
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
    # the scenario `seed` block is merged onto the backend SEED (B15/B16, spec gaps G2/G4)
    kw: dict[str, Any] = {"policy": s.policy, "seed_overlay": getattr(s, "seed", None) or None}
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


SIM_LLM_KINDS = ("openai_compat",)


def build_sim_llm(sim_cfg: dict) -> Any:
    """The caller LLM named by `simulator.llm`, or None when the key is absent.

    Config parsing only (no network call). Raises ValueError with a one-line reason for
    an unknown kind, a missing base_url (and base_url_env unset/empty), a base_url that
    is not http(s)://, a missing model, or an api_key_env whose variable is unset/empty.
    model / temperature / send_temperature / max_tokens go into CallerLLM's config (so
    they enter config_hash); effort stays None (Anthropic-only)."""
    import os

    from duplexbench.simulator.caller_llm import DEFAULT_PROMPT, PROMPTS_DIR, CallerLLM
    from duplexbench.simulator.openai_compat import OpenAICompatClient
    spec = (sim_cfg or {}).get("llm")
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValueError("must be a mapping {kind, base_url, model, ...}")
    kind = spec.get("kind")
    if kind not in SIM_LLM_KINDS:
        raise ValueError(f"unknown kind {kind!r} (known: {list(SIM_LLM_KINDS)})")
    base_url = str(spec.get("base_url") or "").strip()
    if not base_url and spec.get("base_url_env"):
        base_url = os.environ.get(str(spec["base_url_env"]), "").strip()
        if not base_url:
            raise ValueError(f"base_url_env {spec['base_url_env']} is unset or empty")
    if not base_url:
        raise ValueError("missing base_url (or base_url_env)")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"base_url {base_url!r} must start with http:// or https://")
    model = str(spec.get("model") or "").strip()
    if not model:
        raise ValueError("missing model")
    api_key = None
    if spec.get("api_key_env"):
        api_key = os.environ.get(str(spec["api_key_env"]), "").strip()
        if not api_key:
            raise ValueError(f"api_key_env {spec['api_key_env']} is unset or empty")
    extra = spec.get("extra_body") or {}
    if not isinstance(extra, dict):
        raise ValueError("extra_body must be a mapping")
    client = OpenAICompatClient(base_url, model=model, api_key=api_key, extra_body=extra,
                                timeout_s=float(spec.get("timeout_s", 60.0)))
    config: dict[str, Any] = {"model": model, "effort": None}
    for k in ("temperature", "send_temperature", "max_tokens"):
        if k in spec:
            config[k] = spec[k]
    prompt = PROMPTS_DIR / spec["prompt"] if spec.get("prompt") else DEFAULT_PROMPT
    if not prompt.is_file():
        raise ValueError(f"prompt {spec.get('prompt')!r} not found in {PROMPTS_DIR}")
    llm = CallerLLM(client, prompt_path=prompt, config=config)
    # self-hosted endpoint: GPU time billed outside the harness, so the cost projection
    # adds 0 USD for it and the run card must disclose that (billing: external)
    llm.endpoint_info = {"kind": kind, "model": model, "base_url": client.base_url,
                         "billing": "external"}
    if spec.get("price_ref"):           # per-token API (fix-28): priced from the price file
        llm.endpoint_info.update(billing="per_token", price_ref=str(spec["price_ref"]))
    return llm


def _refasr_rate(engine_or_module: Any) -> tuple[float, str | None]:
    """(usd_per_audio_min, price_ref) of a paid engine: its own `usd_per_audio_min()`,
    else its module's (refasr/candidates/deepgram.py). ValueError when neither exists."""
    fn = getattr(engine_or_module, "usd_per_audio_min", None)
    mod = engine_or_module
    if not callable(fn):
        mod = sys.modules.get(type(engine_or_module).__module__)
        fn = getattr(mod, "usd_per_audio_min", None)
    if not callable(fn):
        raise ValueError(f"paid reference ASR {getattr(engine_or_module, 'name', '?')} has no "
                         "usd_per_audio_min() price")
    return float(fn()), (getattr(engine_or_module, "PRICE_REF", None)
                         or getattr(mod, "PRICE_REF", None))


def simulator_costs(sim_cfg: dict, sim_refasr: Any = None) -> dict:
    """Pricing of the Track C simulator (fix-28); config only, no engine is built.

    refasr: the injected engine's `paid` attribute, else the configured candidate's
    `Candidate.paid`; a paid engine's rate comes from its module's usd_per_audio_min().
    caller_llm: `simulator.llm.price_ref` -> the price file's rates for `model` plus its
    [Assumption] projection block; absent -> billing external, 0 USD.
    Raises ValueError / SystemError_ on a bad price file or an unpriced model."""
    import importlib

    from duplexbench.metrics import cost
    from duplexbench.refasr.candidates_table import CANDIDATES
    from duplexbench.systems import load_prices
    sim_cfg = sim_cfg or {}
    ra: dict[str, Any] = {"paid": False, "usd_per_audio_min": 0.0, "name": "none"}
    if sim_refasr is not None:
        ra["name"] = refasr_id(sim_refasr)
        if getattr(sim_refasr, "paid", False):
            ra["paid"] = True
            ra["usd_per_audio_min"], ra["price_ref"] = _refasr_rate(sim_refasr)
    elif isinstance(sim_cfg.get("refasr"), dict):
        name = sim_cfg["refasr"].get("candidate")
        c = {c.name: c for c in CANDIDATES}.get(name)
        ra["name"] = str(name)
        if c is not None and c.paid:
            mod = importlib.import_module(f"duplexbench.refasr.candidates.{c.module}")
            ra["paid"] = True
            ra["usd_per_audio_min"], ra["price_ref"] = _refasr_rate(mod)
    if ra.get("price_ref"):
        pr = load_prices(ra["price_ref"]) or {}
        ra.update(source_url=pr.get("source_url"), checked=pr.get("checked"))
    spec = sim_cfg.get("llm") if isinstance(sim_cfg.get("llm"), dict) else {}
    model = spec.get("model")
    llm: dict[str, Any] = {"billing": "external", "model": model, "price_ref": None}
    if spec.get("price_ref"):
        prices = load_prices(str(spec["price_ref"]))
        r = cost.chat_rates(prices, model)
        p = cost._chat_projection(prices)
        llm.update(billing="per_token", price_ref=prices["price_ref"], prices=prices,
                   input_per_1m=float(r["input_per_1m"]), output_per_1m=float(r["output_per_1m"]),
                   input_tokens_per_turn=p["input_tokens_per_turn"],
                   output_tokens_per_turn=p["output_tokens_per_turn"],
                   extra_turns_per_call=p["extra_turns_per_call"],
                   source_url=prices["source_url"], checked=prices["checked"])
    return {"refasr": ra, "caller_llm": llm}


def apply_simulator_costs(plan: RunPlan, sim_refasr: Any = None) -> None:
    """Add reference-ASR and caller-LLM projections to every simulated call (fix-28).

    projected_usd becomes agent + refasr + caller_llm (the Budget reserves it); the parts
    go to CallPlan.usd_parts and the per-system projection (usd stays the agent part).
    Idempotent: the agent part is kept in usd_parts on the first application."""
    from duplexbench.metrics import cost
    sim = [c for c in plan.calls if is_simulated(c.scenario)]
    if not sim:
        return
    sc = plan.sim_cost = simulator_costs(plan.cfg.simulator, sim_refasr)
    ra, llm = sc["refasr"], sc["caller_llm"]
    priced = llm["billing"] == "per_token"
    for c in sim:
        agent = c.usd_parts["agent"] if c.usd_parts else c.projected_usd
        n_steps = len(c.scenario.steps)
        ra_usd = cost.refasr_call_usd(c.minutes, ra["usd_per_audio_min"]) if ra["paid"] else 0.0
        llm_usd = cost.caller_llm_projected_usd(n_steps, llm["prices"], llm["model"]) \
            if priced else 0.0
        c.usd_parts = {"agent": agent, "refasr": ra_usd, "caller_llm": llm_usd,
                       "caller_turns": cost.caller_llm_turns(n_steps, llm["prices"])
                       if priced else None}
        c.projected_usd = agent + ra_usd + llm_usd
    for sid, p in plan.projection.items():
        mine = [c for c in sim if c.system_id == sid]
        if mine:
            p["usd_refasr"] = round(sum(c.usd_parts["refasr"] for c in mine), 6)
            p["usd_caller_llm"] = round(sum(c.usd_parts["caller_llm"] for c in mine), 6)
            p["usd_total"] = round(sum(c.projected_usd for c in plan.calls
                                       if c.system_id == sid), 6)


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
    usage_log = getattr(plan.sim_llm, "usage_log", None)     # shared CallerLLM: slice per call
    usage_n0 = len(usage_log) if isinstance(usage_log, list) else 0
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
        try:
            await caller.finish_ear()
        except Exception as e:  # noqa: BLE001 - reference-ASR finish failed: kept, not raised
            if caller.error is None:
                caller.error = e
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
        "error": repr(caller.error) if caller.error is not None else None,
        "refasr_version_mismatch": _is_version_mismatch(caller.error),
        "llm_usage": list(usage_log[usage_n0:]) if isinstance(usage_log, list) else []}
    return res, inner


def _is_version_mismatch(err: BaseException | None) -> bool:
    from duplexbench.refasr.candidates.deepgram import RefASRVersionMismatch
    return isinstance(err, RefASRVersionMismatch)


async def execute(plan: RunPlan, *, max_usd: float, echo=typer.echo,
                  tool_sock: Any = None) -> dict:
    """Run every planned call sequentially; returns the run summary.

    `tool_sock` (fix-44): the bound socket from `run_command` when a system needs HTTP
    tools; uvicorn serves the ToolServer app on it from before the first call until the
    `finally` below (also on an exception or cancellation)."""
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
    if any(s.tools for s in plan.scenarios) or tool_sock is not None:
        from duplexbench.tools.server import ToolServer
        tool_server = ToolServer()
    tool_http = None
    budget = Budget(max_usd)
    outcomes: dict[str, int] = {}
    usd_total, results = 0.0, []
    parts_total = {"agent": 0.0, "refasr": 0.0, "caller_llm": 0.0}
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
        sim = getattr(adapter, "sim_result", None)
        cost_flags = None
        if cp.usd_parts is not None and plan.sim_cost:    # Track C: + reference ASR + caller LLM
            ra, llm = plan.sim_cost["refasr"], plan.sim_cost["caller_llm"]
            llm_usage = list((sim or {}).get("llm_usage") or [])
            ra_usd = cost.refasr_call_usd(minutes, ra["usd_per_audio_min"]) if ra["paid"] else 0.0
            llm_usd = (cost.caller_llm_actual_usd(llm_usage, llm["prices"], llm["model"])
                       if llm["billing"] == "per_token" else 0.0)
            parts = {"agent": actual, "refasr": ra_usd, "caller_llm": llm_usd}
            cost_flags = {"projected": {k: cp.usd_parts[k] for k in parts}, "actual": parts,
                          "refasr_audio_min": round(minutes, 6),
                          "refasr_billed": bool(ra["paid"]),
                          "caller_llm_billing": llm["billing"],
                          "caller_llm_usage": cost.sum_chat_usage(llm_usage)}
            for k, v in parts.items():
                parts_total[k] += v or 0.0
            actual = None if actual is None else actual + ra_usd + llm_usd
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
            if cost_flags is not None:
                flags["cost"] = cost_flags
            if sim:
                flags["simulator"] = {k: v for k, v in sim.items()
                                      if k not in ("caller_turns", "llm_usage")}
                if plan.sim_llm_info:
                    # billing "external": self-hosted GPU time, outside the harness's
                    # cost projection (0 USD added); the run card must disclose it
                    flags["simulator"]["llm"] = dict(plan.sim_llm_info)
                if plan.sim_refasr is not None:
                    # post-call `name:version` (a streaming engine learns its uuid in-call)
                    flags["simulator"]["refasr"] = refasr_id(plan.sim_refasr)
                tts_ = plan.sim_tts
                flags["simulator"]["tts"] = (getattr(tts_, "version", None)
                                             or getattr(tts_, "vendor", None) or "fake")
                if sim.get("refasr_version_mismatch"):
                    echo(f"{cp.call_id}: reference ASR version mismatch ({sim.get('error')}); "
                         "run stopped")
                    state["stopped"] = "refasr_version_mismatch"
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
        if tool_sock is not None:
            from duplexbench.tools.http import ToolHttp, ToolPortError
            tool_http = ToolHttp(tool_server.app, tool_sock)
            try:
                await tool_http.start()
            except ToolPortError as e:
                tool_http = None
                raise RunRefused(EXIT_CONFIG, str(e)) from e
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
        if plan.sim_refasr is not None:
            summary["refasr_version_final"] = refasr_id(plan.sim_refasr)
        if any(c.usd_parts is not None for c in plan.calls):
            summary["usd_projected_parts"] = {
                k: round(sum((c.usd_parts or {"agent": c.projected_usd}).get(k) or 0.0
                             for c in plan.calls), 6)
                for k in ("agent", "refasr", "caller_llm")}
            summary["usd_actual_parts"] = {k: round(v, 6) for k, v in parts_total.items()}
        runs_row["run_card_json"] = canonical_json(run_card(plan, run_id, summary))
        store.put("runs", runs_row)
        await store.flush()
    finally:
        if tool_http is not None:
            await tool_http.stop()
            echo(f"tool server: stopped ({tool_http.host}:{tool_http.port})")
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


def _resolve_sim_llm(cfg: RunConfig) -> Any:
    """`simulator.llm` -> CallerLLM, or None (check_plan then refuses Track C)."""
    try:
        return build_sim_llm(cfg.simulator)
    except ValueError as e:
        raise RunRefused(EXIT_CONFIG, f"config error: simulator.llm: {e}") from e


def _resolve_sim_tts(cfg: RunConfig) -> Any:
    """`simulator.tts` -> a TTSClient (stimuli.vendors.build_tts), or None (-> FakeTTS).

    Config parsing only: building the client sends no request (tts-fish, decision B13).
    A missing key, an unknown vendor or an unknown field is refused before any call."""
    from duplexbench.stimuli.vendors import build_tts
    try:
        return build_tts((cfg.simulator or {}).get("tts"))
    except (ValueError, RuntimeError) as e:          # RuntimeError: TTSUnavailable (no key)
        raise RunRefused(EXIT_CONFIG, f"config error: simulator.tts: {e}") from e


def run_command(config: Path, *, yes: bool, max_usd: float | None, allow_unregistered: bool,
                results_dir: Path | None = None, echo=typer.echo, sim_llm: Any = None,
                sim_tts: Any = None, sim_refasr: Any = None, tool_host: str | None = None,
                tool_port: int | None = None) -> dict:
    """`sim_llm` / `sim_tts` / `sim_refasr`: Track C simulator clients (tests inject fakes;
    otherwise simulator.llm / FakeTTS / simulator.refasr). `tool_host` / `tool_port`:
    override the HTTP tool server bind (fix-44; default 0.0.0.0 and the port of
    harness_tool_base)."""
    from duplexbench.scenarios.loader import PackError
    from duplexbench.systems import SystemError_
    try:
        cfg = RunConfig.load(config)
        plan = plan_run(cfg, results_dir=results_dir)
    except (PackError, SystemError_, ValueError, FileNotFoundError) as e:
        raise RunRefused(EXIT_CONFIG, f"config error: {e}") from e
    plan.sim_llm, plan.sim_tts = sim_llm, sim_tts
    if plan.sim_tts is None and any(is_simulated(s) for s in plan.scenarios):
        plan.sim_tts = _resolve_sim_tts(cfg)        # None -> FakeTTS in _simulated_call
    if plan.sim_llm is None and any(is_simulated(s) for s in plan.scenarios):
        plan.sim_llm = _resolve_sim_llm(cfg)        # config parsing only, no network
        plan.sim_llm_info = getattr(plan.sim_llm, "endpoint_info", None)
    if any(is_simulated(s) for s in plan.scenarios):
        from duplexbench.systems import SystemError_ as _SysErr
        try:                                  # fix-28: + reference ASR + caller LLM per call
            apply_simulator_costs(plan, sim_refasr)
        except (ValueError, _SysErr) as e:
            raise RunRefused(EXIT_CONFIG, f"config error: simulator cost: {e}") from e
    plan.tool_http = plan_tool_http(plan, tool_host=tool_host, tool_port=tool_port)
    sock = None
    if plan.tool_http is not None:       # fix-44: bind first, so a busy port fails fast
        from duplexbench.tools.http import ToolPortError, bind_socket
        try:
            sock = bind_socket(plan.tool_http["host"], plan.tool_http["port"])
        except ToolPortError as e:
            raise RunRefused(EXIT_CONFIG, str(e)) from e
        th_ = plan.tool_http
        th_["port"] = sock.getsockname()[1]              # --tool-port 0: the ephemeral port
        th_["bind"] = f"{th_['host']}:{th_['port']}"
        reach = sorted({b for b in th_["bases"].values() if b}) or [
            f"http://<public-host>:{th_['port']}"]
        echo(f"tool server: listening on {th_['bind']} (Dograh must reach "
             f"{' and '.join(r.rstrip('/') + '/tools/...' for r in reach)})")
    try:
        return _run_planned(plan, cfg, yes=yes, max_usd=max_usd,
                            allow_unregistered=allow_unregistered, echo=echo,
                            sim_refasr=sim_refasr, tool_sock=sock)
    finally:
        if sock is not None:
            sock.close()                 # no-op when uvicorn already closed it


def _run_planned(plan: RunPlan, cfg: RunConfig, *, yes: bool, max_usd: float | None,
                 allow_unregistered: bool, echo, sim_refasr: Any, tool_sock: Any) -> dict:
    from duplexbench import _loop
    print_plan(plan, echo)
    limit = cfg.max_usd if max_usd is None else max_usd
    check_plan(plan, max_usd=limit, yes=yes, allow_unregistered=allow_unregistered)
    if any(is_simulated(s) for s in plan.scenarios):
        plan.sim_refasr = sim_refasr if sim_refasr is not None else _resolve_refasr(cfg)
        cal = (plan.calibration.get("refasr") or {}).get("version")
        if cal and cal != refasr_id(plan.sim_refasr):
            rid = refasr_id(plan.sim_refasr)
            hint = (" (its model version is unknown until the first stream; set "
                    "kwargs.pinned_model_uuid to the calibrated uuid to make the run refuse "
                    "any other model)") if rid.endswith("/unpinned") else ""
            echo(f"warning: simulator reference ASR {rid} differs from the calibrated one "
                 f"{cal}{hint}")
    return _loop.run(execute(plan, max_usd=limit, echo=echo, tool_sock=tool_sock))


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
        tool_host: str = typer.Option(None, "--tool-host",
                                      help="HTTP tool server bind host (default 0.0.0.0); only "
                                           "for systems that call tools over HTTP (dograh_stack)"),
        tool_port: int = typer.Option(None, "--tool-port",
                                      help="HTTP tool server port (default: the port of "
                                           "params.harness_tool_base, else 8900)"),
    ) -> None:
        """Run calls against systems under test (prints the projection; needs --yes)."""
        try:
            run_command(config, yes=yes, max_usd=max_usd, allow_unregistered=allow_unregistered,
                        results_dir=results_dir, tool_host=tool_host, tool_port=tool_port)
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
