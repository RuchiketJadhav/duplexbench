"""M8 release-gate checks on a scored run (CLAUDE.md M8 acceptance; BUILD_PLAN §1 row 9, M8-T03).

    check_run(store, run_id, th) -> GateReport
    print_gate(report) -> str          (also what `cli gate <run_id>` prints)

The gates, in order (each a `GateCheck(name, passed, detail)`):

  run_card_complete      report/run_card.build(...)["missing"] is empty (spec §10 item 1)
  headline_n_ci          every headline row of the aggregate has n > 0 and a CI (spec §9.1)
  power_notes            the planner's power note exists for every headline row and every
                         pre-registered comparison declares `delta` and `discordant`
                         (spec §9.1 planner; notes are in `GateReport.power_notes`)
  calibration_passed     runs.calibration_json says passed (`passed`/`pass` true or
                         `status: "pass"`; spec §1.4, on the instance)
  harness_invalid_share  calls that failed on the harness side (outcome transport_error,
                         pacing slip p99 > th.pacing_slip_p99_ms or flag `pacing_fail`,
                         flag `load_invalid`) / all calls <= th.harness_invalid_max
  attempts_preserved     every call referenced by an observation has a `calls` row with an
                         outcome; no call row lacks an outcome; `planned_calls` (when the run
                         config declares it) <= calls in the store
  exclusion_rates        ambiguous_end / not_injected / unmatched_control / contested rates
                         are reported for every system. These are expected outcomes, never a
                         failure: the gate fails only when a system has no exclusion entry.
  coverage_n_met         run config `coverage: {metric_key: n}` (pre-registered) is met by
                         every system under the baseline condition; a run without a
                         pre-registered coverage block fails this gate.

The gate never changes a threshold or a result; it reads the store and the aggregate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from duplexbench.report.aggregate import PERCENTILES, _connect, aggregate
from duplexbench.report.run_card import build as build_card
from duplexbench.stats.planner import half_width, pairs_needed, percentile_min_n
from duplexbench.thresholds import Thresholds

GATES = ("run_card_complete", "headline_n_ci", "power_notes", "calibration_passed",
         "harness_invalid_share", "attempts_preserved", "exclusion_rates", "coverage_n_met")
EXCLUSION_TAGS = ("ambiguous_end", "not_injected", "unmatched_control", "contested")
HARNESS_OUTCOMES = ("transport_error",)
PCT = 100


@dataclass
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class GateReport:
    run_id: str
    passed: bool
    checks: list[GateCheck]
    failures: list[str]
    power_notes: list[str] = field(default_factory=list)
    exclusion_rates: dict[str, dict[str, Any]] = field(default_factory=dict)
    harness_invalid: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    run_card_missing: list[str] = field(default_factory=list)

    def check(self, name: str) -> GateCheck:
        return next(c for c in self.checks if c.name == name)


def _j(s: str | None, default: Any) -> Any:
    return default if not s else json.loads(s)


def calibration_passed(calib: dict) -> bool:
    """`passed: true` (calibrate record via card-fill), `pass: true` (calibration-<ts>.json)
    or `status: "pass"` (cli_run's calibration status)."""
    if not calib:
        return False
    return calib.get("passed") is True or calib.get("pass") is True or calib.get("status") == "pass"


def harness_invalid_reason(call: dict, th: Thresholds) -> str | None:
    """Why a call is invalid on the harness side, or None (CLAUDE.md M8)."""
    if call.get("outcome") in HARNESS_OUTCOMES:
        return str(call["outcome"])
    flags = _j(call.get("flags_json"), {})
    if flags.get("load_invalid"):
        return "load_invalid"
    slip = flags.get("pacing_slip_p99_ms")
    if flags.get("pacing_fail") or (slip is not None and slip > th.pacing_slip_p99_ms):
        return "pacing_fail"
    return None


def _power_notes(agg: dict, config: dict) -> tuple[list[str], list[str]]:
    notes, problems = [], []
    base = agg.get("baseline_condition", "C0")
    for key in sorted(agg.get("metrics", {})):
        for sid in sorted(agg["metrics"][key]):
            s = agg["metrics"][key][sid].get(base)
            if not s or not s.get("headline"):
                continue
            n = s.get("n") or 0
            if s.get("kind") == "rate":
                hw = f"±{half_width(n) * PCT:.1f} pts" if n else "n/a"
                notes.append(f"{key} [{sid}, {base}]: n={n}; Wilson half-width at p=0.5 {hw}")
            elif s.get("kind") == "percentile":
                need = ", ".join(f"p{p} {percentile_min_n(p)}" for p in PERCENTILES)
                low = ", ".join(s.get("low_n") or []) or "none"
                notes.append(f"{key} [{sid}, {base}]: n={n}; minimum n {need}; below: {low}")
            else:
                notes.append(f"{key} [{sid}, {base}]: n={n}; mean with clustered bootstrap CI")
    comps = config.get("comparisons") or []
    done = {(c["metric"], c["a"], c["b"], c["condition"]): c for c in agg.get("comparisons", [])}
    for c in comps:
        key = c["metric"] + (f"@{c['variant']}" if c.get("variant") else "")
        cond = c.get("condition", base)
        res = done.get((key, c["a"], c["b"], cond), {})
        label = f"{key} {c['a']} vs {c['b']} [{cond}]"
        if c.get("delta") is None or c.get("discordant") is None:
            problems.append(f"{label}: no pre-registered delta/discordant")
            continue
        try:
            need = pairs_needed(float(c["delta"]), float(c["discordant"]))
        except ValueError as e:
            problems.append(f"{label}: {e}")
            continue
        have = res.get("n") or 0
        verdict = "powered" if have >= need else "UNDERPOWERED: do not call it distinguishable"
        notes.append(f"{label}: delta {c['delta']}, discordant {c['discordant']}: "
                     f"needs {need} pairs, has {have} ({verdict})")
    if not notes:
        problems.append("no headline rows to plan")
    return notes, problems


def check_run(store: Any, run_id: str, th: Thresholds) -> GateReport:
    conn = _connect(store)
    try:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(f"run not in store: {run_id}")
        run = dict(run)
        calls = [dict(r) for r in conn.execute(
            "SELECT * FROM calls WHERE run_id = ? ORDER BY call_id", (run_id,))]
        obs_calls = sorted({r[0] for r in conn.execute(
            "SELECT DISTINCT call_id FROM observations WHERE run_id = ?", (run_id,))})
    finally:
        conn.close()
    config = _j(run.get("config_json"), {})
    calib = _j(run.get("calibration_json"), {})
    agg = aggregate(store, run_id, th)
    card = build_card(store, run_id, th)
    checks: list[GateCheck] = []

    # 1 run card
    missing = list(card.get("missing") or [])
    checks.append(GateCheck("run_card_complete", not missing,
                            "complete" if not missing else "missing: " + ", ".join(missing)))

    # 2 headline n and CI
    bad, n_head = [], 0
    for key in sorted(agg.get("metrics", {})):
        for sid in sorted(agg["metrics"][key]):
            for cond, s in sorted(agg["metrics"][key][sid].items()):
                if not s.get("headline"):
                    continue
                n_head += 1
                if not s.get("n") or s.get("ci") is None:
                    bad.append(f"{key} [{sid}, {cond}] n={s.get('n')} ci={s.get('ci')}")
    ok = n_head > 0 and not bad
    checks.append(GateCheck("headline_n_ci", ok, f"{n_head} headline rows" + (
        "" if ok else "; " + ("no headline rows" if not n_head else "; ".join(bad)))))

    # 3 planner power notes
    notes, problems = _power_notes(agg, config)
    checks.append(GateCheck("power_notes", not problems,
                            f"{len(notes)} notes" + ("" if not problems else "; " + "; ".join(problems))))

    # 4 calibration
    passed = calibration_passed(calib)
    checks.append(GateCheck("calibration_passed", passed,
                            "passed" if passed else ("no calibration recorded" if not calib
                                                     else f"calibration not passed: {sorted(calib)}")))

    # 5 harness-invalid share
    reasons: dict[str, int] = {}
    by_sys: dict[str, dict[str, int]] = {}
    for c in calls:
        r = harness_invalid_reason(c, th)
        e = by_sys.setdefault(str(c.get("system_id")), {"n": 0, "k": 0})
        e["n"] += 1
        if r:
            e["k"] += 1
            reasons[r] = reasons.get(r, 0) + 1
    n_calls = len(calls)
    k_bad = sum(reasons.values())
    share = (k_bad / n_calls) if n_calls else None
    hi = {"n": n_calls, "k": k_bad, "share": share, "max": th.harness_invalid_max,
          "reasons": dict(sorted(reasons.items())), "by_system": dict(sorted(by_sys.items()))}
    ok = share is not None and share <= th.harness_invalid_max
    checks.append(GateCheck("harness_invalid_share", ok,
                            f"{k_bad}/{n_calls} = {share if share is None else round(share, 4)} "
                            f"(max {th.harness_invalid_max}) {hi['reasons']}"))

    # 6 attempts preserved
    ids = {c["call_id"] for c in calls}
    orphans = [cid for cid in obs_calls if cid not in ids]
    no_outcome = [c["call_id"] for c in calls if not c.get("outcome")]
    planned = config.get("planned_calls")
    short = planned is not None and n_calls < int(planned)
    probs = []
    if orphans:
        probs.append(f"{len(orphans)} observed call(s) without a calls row: {orphans[:5]}")
    if no_outcome:
        probs.append(f"{len(no_outcome)} call(s) without an outcome: {no_outcome[:5]}")
    if short:
        probs.append(f"planned_calls {planned} > stored {n_calls}")
    checks.append(GateCheck("attempts_preserved", not probs,
                            f"{n_calls} calls stored" + ("" if not probs else "; " + "; ".join(probs))))

    # 7 exclusion rates per system (reported, never a failure by themselves)
    rates: dict[str, dict[str, Any]] = {}
    exc = agg.get("exclusions", {})
    lacking = []
    for sid in sorted(agg.get("systems", {})):
        e = exc.get(sid)
        if not e:
            lacking.append(sid)
            continue
        units = e.get("units") or 0
        rates[sid] = {"units": units, **{t: {"k": e["counts"].get(t, 0),
                                             "rate": (e["counts"].get(t, 0) / units) if units else None}
                                         for t in EXCLUSION_TAGS}}
    checks.append(GateCheck("exclusion_rates", not lacking and bool(rates),
                            f"reported for {sorted(rates)}" + (f"; missing {lacking}" if lacking else "")))

    # 8 pre-registered coverage n
    cov_cfg = config.get("coverage") or {}
    base = agg.get("baseline_condition", "C0")
    cov: dict[str, Any] = {}
    short_cov = []
    for key, need in sorted(cov_cfg.items()):
        for sid in sorted(agg.get("systems", {})):
            s = agg.get("metrics", {}).get(key, {}).get(sid, {}).get(base)
            have = (s or {}).get("n") or 0
            cov.setdefault(key, {})[sid] = {"need": int(need), "have": have}
            if have < int(need):
                short_cov.append(f"{key} [{sid}] {have} < {need}")
    if not cov_cfg:
        checks.append(GateCheck("coverage_n_met", False, "no pre-registered coverage in run config"))
    else:
        checks.append(GateCheck("coverage_n_met", not short_cov,
                                f"{len(cov_cfg)} metrics" + ("" if not short_cov else "; " + "; ".join(short_cov))))

    failures = [c.name for c in checks if not c.passed]
    return GateReport(run_id, not failures, checks, failures, notes, rates, hi, cov, missing)


def print_gate(report: GateReport) -> str:
    """ASCII text (cp1252 consoles): one line per gate, then the power notes and rates."""
    lines = [f"gate {report.run_id}: {'PASS' if report.passed else 'FAIL'}"]
    for c in report.checks:
        lines.append(f"  [{'ok' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    if report.power_notes:
        lines.append("power notes (planner, spec 9.2):")
        lines += [f"  {n}" for n in report.power_notes]
    if report.exclusion_rates:
        lines.append("exclusion rates per system (expected outcomes, never failures):")
        for sid, e in report.exclusion_rates.items():
            parts = [f"{t} {e[t]['k']}/{e['units']}" for t in EXCLUSION_TAGS]
            lines.append(f"  {sid}: " + ", ".join(parts))
    text = "\n".join(lines).replace("±", "+/-")
    return text
