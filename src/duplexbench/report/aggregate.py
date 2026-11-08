"""Aggregation of scored observations into the report's numbers (spec §9.1, §10; BUILD_PLAN M7-T03).

    observations (+ calls, runs)  --aggregate()-->  one JSON-able dict
                                                    |-> report/html.py renders it
                                                    |-> report/verify.py recomputes and diffs it

Per metric x system x condition: n, value, CI (Wilson for rates; bootstrap
clustered by scenario for percentiles and means), percentiles p50/p90/p95 and
p99 only when n >= th.p99_min_n (else "insufficient n"), completion rate beside
percentiles, tag counts, markers (`classifier` / `judged` / `diagnostic`) and
the `self_hosted` / `components_only` flags of the system config.

Observation tag conventions read here (written by `cli score`):
  * `@<v>`   a variant of the metric (e.g. `@1000` = Yield at B = 1.0 s); the
             variant gets its own row under the key `metric@<v>`.
  * `k:v`    a breakdown dimension (e.g. `domain:billing`, `bucket:2000`,
             `type:phone`, `cls:H-BC`); summarised under `breakdowns[k][v]`.
             A key may repeat on one observation (`code:a`, `code:b` on
             policy_violation_rate, G10): the row then counts under each value.
  * the exclusion tags of metrics/base.TAGS; `judged` for LLM-judged values.

Control-twin calls (`calls.is_control = 1`) never enter the main rows; they feed
only the interrupted-vs-clean split (spec §4.2, §3.4).

Runs whose pack / langpack / normalizer / refasr / simulator / thresholds hashes
differ never aggregate (spec §11): `MixedHashError`. Metric names outside
`metrics/names.py` raise `UnknownMetricError` (BUILD_PLAN §2.11).
There is no composite anywhere (spec §10, decision D7).

`simulator_counts` (fix-42, only when non-zero): per system, the simulated caller's
`speculation_from_provisional` / `provisional_mismatches` / `provisional_replies` summed
over non-control calls from `calls.flags_json["simulator"]["flags"]`. Counts, not metrics.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from duplexbench.metrics.names import Metric
from duplexbench.stats.paired_bootstrap import SEED, _resample_ids, bootstrap_ci, holm, paired_diff
from duplexbench.stats.planner import percentile_min_n
from duplexbench.stats.wilson import wilson
from duplexbench.thresholds import Thresholds

SCHEMA_VERSION = "duplexbench.aggregate/1"
# simulated-caller counters summed per system under "simulator_counts" (fix-42)
SIMULATOR_COUNTERS = ("speculation_from_provisional", "provisional_mismatches",
                      "provisional_replies")

HASH_KEYS = ("pack", "langpack", "normalizer", "refasr", "simulator", "thresholds")

# ---- metric kinds ------------------------------------------------------------
RATE = frozenset({
    "yield_rate", "respond_resume", "wait_compliance", "hold_speaking_rate",
    "content_leakage_rate", "hold_listening_rate", "response_rate", "pass_at_1", "pass_pow_3",
    "stale_action_rate", "preemptive_tool_rate", "unsupported_claim_rate",
    "policy_violation_rate", "auth_bypass_rate", "tool_wait_pass", "repeated_prompt_rate",
    "response_rate_per_turn", "call_outcome", "capture_first", "capture_final",
    "recovery_request_rate", "recovery_success", "readback_presence", "readback_error_rate",
    "tor", "error_rate_at_c", "mishearing_rate", "regeneration_rate",
})
PERCENTILE = frozenset({
    "t_stop_ear", "t_respond_ear", "t_turn_gap_ear", "t_ttfa_recv", "t_stop_model",
    "t_tool_offset", "t_tool_to_info_ear", "t_dead_air", "t_caller_think", "backlog_ms",
    "playout_penalty", "anchor_offset_in_utt_ms",
})
MEAN = frozenset({
    "talk_over_share", "filler_count", "filler_s", "dead_air_in_tool_s", "cost_per_min",
    "codec_penalty",
})
assert RATE | PERCENTILE | MEAN == {m.value for m in Metric}
assert not (RATE & PERCENTILE or RATE & MEAN or PERCENTILE & MEAN)

# spec §8: rule-based classifiers; `classifier` marker until validated at kappa_min
CLASSIFIER_METRICS = frozenset({"respond_resume", "unsupported_claim_rate",
                                "recovery_request_rate", "recovery_success"})
# spec §4.5 (+ §4.1 leakage, §4.2 D6, §7): reported, never headline
DIAGNOSTIC_METRICS = frozenset({"t_ttfa_recv", "t_stop_model", "backlog_ms", "playout_penalty",
                                "tor", "anchor_offset_in_utt_ms", "content_leakage_rate",
                                "mishearing_rate", "regeneration_rate", "t_caller_think"})
# spec §4.2: Track C rates split interrupted vs clean against the control twins
TRACK_C_SPLIT = ("pass_at_1", "stale_action_rate", "preemptive_tool_rate",
                 "unsupported_claim_rate", "policy_violation_rate", "auth_bypass_rate")
# exclusions remove a row from the completion denominator (spec §3.3, §3.4, §7)
EXCLUDE_TAGS = frozenset({"ambiguous_end", "not_injected", "unmatched_control", "contested",
                          "slow_caller", "unscripted_overlap",
                          # harness-side gaps, not agent failures (fix-23, fix-32)
                          "no_refasr", "step_not_reached"})
PERCENTILES = (50, 90, 95)
P99 = 99
PASS_POW_K = 3          # spec §4.2: Pass^3, k = 3 [Fact: draft default]
INSUFFICIENT = "insufficient n"
# spec §4.2 (D6): share of anchor_offset_in_utt_ms above 1.0 s, reported beside p50
ANCHOR_SHARE_MS = 1000  # threshold-ok spec 4.2 D6 reporting cut; no Thresholds field exists yet
ROUND = 6


class AggregateError(ValueError):
    pass


class MixedHashError(AggregateError):
    pass


class UnknownMetricError(AggregateError):
    pass


class TooManyComparisonsError(AggregateError):
    pass


# ---- small numeric helpers -----------------------------------------------------

def _r(x: float | None) -> float | None:
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return round(float(x), ROUND) + 0.0      # +0.0 folds -0.0


def _ci(lo_hi: tuple | None) -> list | None:
    if lo_hi is None or lo_hi[0] is None:
        return None
    return [_r(lo_hi[0]), _r(lo_hi[1])]


def percentile(sorted_vals: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile (numpy's default) of an already sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    pos = (n - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo]) + (float(sorted_vals[hi]) - float(sorted_vals[lo])) * frac


def _pct_stat(q: float) -> Callable[[list], float | None]:
    return lambda xs: percentile(sorted(xs), q)


def _mean(xs: list) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _median(xs: list) -> float | None:
    return percentile(sorted(xs), 50)


def metric_name(key: str) -> str:
    """`yield_rate@1000` -> `yield_rate`."""
    return key.split("@", 1)[0]


def check_metric(name: str) -> Metric:
    try:
        return Metric(name)
    except ValueError:
        raise UnknownMetricError(f"metric {name!r} is not in metrics/names.py") from None


# ---- loading -------------------------------------------------------------------

def _connect(store: Any) -> sqlite3.Connection:
    """A read connection on a Store (flushed first) or a path to its sqlite file."""
    if hasattr(store, "flush_sync") and hasattr(store, "path"):
        store.flush_sync()
        path = Path(store.path)
    else:
        path = Path(store)
    if not path.exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _loads(s: str | None, default: Any) -> Any:
    if s in (None, ""):
        return default
    return json.loads(s)


def load_run(store: Any, run_ids: Sequence[str]) -> dict:
    """Rows needed by aggregate(): runs, calls and observations of `run_ids`."""
    conn = _connect(store)
    try:
        q = ",".join("?" * len(run_ids))
        runs = [dict(r) for r in conn.execute(
            f"SELECT * FROM runs WHERE run_id IN ({q}) ORDER BY run_id", list(run_ids))]
        calls = [dict(r) for r in conn.execute(
            f"SELECT * FROM calls WHERE run_id IN ({q}) ORDER BY call_id", list(run_ids))]
        obs = [dict(r) for r in conn.execute(
            f"SELECT * FROM observations WHERE run_id IN ({q}) "
            "ORDER BY metric, system_id, condition, scenario_id, trial, event_id, call_id",
            list(run_ids))]
    finally:
        conn.close()
    found = {r["run_id"] for r in runs}
    missing = [r for r in run_ids if r not in found]
    if missing:
        raise KeyError(f"run(s) not in store: {missing}")
    return {"runs": runs, "calls": calls, "observations": obs}


def _check_hashes(runs: list[dict]) -> dict:
    hashes = [_loads(r["hashes_json"], {}) for r in runs]
    common: dict[str, Any] = {}
    for k in HASH_KEYS:
        vals = {json.dumps(h.get(k), sort_keys=True) for h in hashes}
        if len(vals) > 1:
            raise MixedHashError(
                f"runs {[r['run_id'] for r in runs]} differ in the {k!r} hash; results across "
                "different hashes never aggregate (spec §11)")
    for h in hashes:
        for k, v in h.items():
            common.setdefault(k, v)
    return dict(sorted(common.items()))


# ---- per-group summaries -------------------------------------------------------

def _parse_tags(tags: Iterable[str]) -> tuple[str | None, dict[str, tuple[str, ...]], list[str]]:
    variant, dims, plain = None, {}, []
    for t in tags:
        if t.startswith("@"):
            variant = t[1:]
        elif ":" in t:
            k, v = t.split(":", 1)
            if v not in dims.get(k, ()):
                dims[k] = (*dims.get(k, ()), v)
        else:
            plain.append(t)
    return variant, dims, plain


def _kind_of(metric: str, values: list[float]) -> str:
    if metric in PERCENTILE:
        return "percentile"
    if metric in MEAN:
        return "mean"
    if all(v in (0.0, 1.0) for v in values):
        return "rate"
    return "mean"           # e.g. readback_error_rate carried as CER


def _summary(metric: str, rows: list[dict], th: Thresholds, full: bool = True) -> dict:
    """Numbers for one group of rows (already one metric key/system/condition)."""
    valid = [r for r in rows if r["valid"] and r["value"] is not None]
    values = [float(r["value"]) for r in valid]
    clusters = [r["scenario_id"] for r in valid]
    kind = _kind_of(metric, values)
    n = len(values)
    out: dict[str, Any] = {"kind": kind, "n": n, "n_rows": len(rows)}
    if kind == "rate":
        k = int(sum(values))
        out["k"] = k
        out["value"] = _r(k / n) if n else None
        out["ci"] = _ci(wilson(k, n)) if n else None
    elif kind == "mean":
        out["value"] = _r(_mean(values)) if n else None
        out["ci"] = (_ci(bootstrap_ci(values, clusters, _mean, th.bootstrap_resamples, SEED))
                     if n and full else None)
        if full:
            out["p90"] = _r(percentile(sorted(values), 90)) if n else None
    else:
        pcts: dict[str, Any] = {}
        low_n: list[str] = []
        for p in PERCENTILES:
            v = percentile(sorted(values), p) if n else None
            ci = (_ci(bootstrap_ci(values, clusters, _pct_stat(p), th.bootstrap_resamples, SEED))
                  if n and full else None)
            pcts[f"p{p}"] = {"value": _r(v), "ci": ci}
            if n < percentile_min_n(p):
                low_n.append(f"p{p}")
        if n >= th.p99_min_n:
            ci = (_ci(bootstrap_ci(values, clusters, _pct_stat(P99), th.bootstrap_resamples, SEED))
                  if full else None)
            pcts["p99"] = {"value": _r(percentile(sorted(values), P99)), "ci": ci}
        else:
            pcts["p99"] = INSUFFICIENT
        out["percentiles"] = pcts
        out["value"] = pcts["p50"]["value"]
        out["ci"] = pcts["p50"]["ci"]
        out["low_n"] = low_n
        incomplete = [r for r in rows if not (r["valid"] and r["value"] is not None)
                      and not (set(r["tags"]) & EXCLUDE_TAGS)]
        denom = n + len(incomplete)
        out["completion_rate"] = _r(n / denom) if denom else None
        out["share_negative"] = _r(sum(v < 0 for v in values) / n) if n else None
        if metric == "anchor_offset_in_utt_ms":
            out["share_over_1s"] = _r(sum(v > ANCHOR_SHARE_MS for v in values) / n) if n else None
    tags: dict[str, int] = defaultdict(int)
    for r in rows:
        for t in r["tags"]:
            if not t.startswith("@") and ":" not in t:
                tags[t] += 1
    out["tags"] = dict(sorted(tags.items()))
    if full:
        dims: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for r in rows:
            for k, vs in r["dims"].items():
                for v in vs:
                    dims[k][v].append(r)
        out["breakdowns"] = {
            k: {v: _summary(metric, sub, th, full=False) for v, sub in sorted(vals.items())}
            for k, vals in sorted(dims.items())}
    return out


# ---- paired differences ----------------------------------------------------------

def _paired_p(kept: list[tuple], stat: Callable, resamples: int, seed: int) -> float | None:
    """Two-sided bootstrap p-value on exactly the resamples `paired_diff` draws."""
    if not kept:
        return None
    ids = list(range(len(kept)))
    cl = {i: kept[i][2] for i in ids}
    rng = random.Random(seed)
    diffs = []
    for _ in range(resamples):
        rs = _resample_ids(ids, cl, rng)
        a, b = stat([kept[i][0] for i in rs]), stat([kept[i][1] for i in rs])
        if a is not None and b is not None:
            diffs.append(a - b)
    if not diffs:
        return None
    le = sum(d <= 0 for d in diffs) / len(diffs)
    ge = sum(d >= 0 for d in diffs) / len(diffs)
    return min(1.0, 2 * min(le, ge))


def _pair(a_rows: list[dict], b_rows: list[dict], key: Callable[[dict], tuple],
          metric: str, th: Thresholds) -> dict:
    """Paired difference stat(A) - stat(B) on the pair key; drops counted per side."""
    def side(rows):
        d: dict[tuple, dict] = {}
        for r in rows:
            d.setdefault(key(r), r)
        return d
    sa, sb = side(a_rows), side(b_rows)
    pairs = []
    for k in sorted(set(sa) | set(sb), key=lambda t: json.dumps(t, default=str)):
        ra, rb = sa.get(k), sb.get(k)
        va = float(ra["value"]) if ra and ra["valid"] and ra["value"] is not None else None
        vb = float(rb["value"]) if rb and rb["valid"] and rb["value"] is not None else None
        cluster = (ra or rb)["scenario_id"]
        pairs.append((va, vb, cluster))
    stat = _median if metric in PERCENTILE else _mean
    pd = paired_diff(pairs, stat, th.bootstrap_resamples, SEED)
    kept = [p for p in pairs if p[0] is not None and p[1] is not None]
    p = _paired_p(kept, stat, th.bootstrap_resamples, SEED) if pd.diff is not None else None
    return {"stat": "p50" if metric in PERCENTILE else "mean", "diff": _r(pd.diff),
            "ci": _ci((pd.lo, pd.hi)), "n": pd.n, "dropped": pd.dropped,
            "dropped_a": pd.dropped_a, "dropped_b": pd.dropped_b, "verdict": pd.verdict,
            "p": _r(p)}


def _scenario_means(rows: list[dict]) -> list[dict]:
    """One row per scenario: the mean of its valid trials (scenario-level split, spec §4.2)."""
    by: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by.setdefault(r["scenario_id"], [])
        if r["valid"] and r["value"] is not None:
            by[r["scenario_id"]].append(float(r["value"]))
    return [{"scenario_id": sc, "value": _mean(v), "valid": bool(v)} for sc, v in sorted(by.items())]


def _key_event(r: dict) -> tuple:
    return (r["scenario_id"], r["event_id"] or "", r["trial"])


# ---- main ----------------------------------------------------------------------

def aggregate(store: Any, run_id: str | Sequence[str], th: Thresholds) -> dict:
    """Every number the report shows, as a JSON-able dict (see module docstring)."""
    run_ids = [run_id] if isinstance(run_id, str) else sorted(run_id)
    data = load_run(store, run_ids)
    hashes = _check_hashes(data["runs"]) if len(run_ids) > 1 else \
        dict(sorted(_loads(data["runs"][0]["hashes_json"], {}).items()))
    configs = [_loads(r["config_json"], {}) for r in data["runs"]]
    cards = [_loads(r["run_card_json"], {}) for r in data["runs"]]
    config: dict[str, Any] = {}
    for c in configs:
        for k, v in c.items():
            config.setdefault(k, v)
    card: dict[str, Any] = {}
    for c in cards:
        for k, v in c.items():
            card.setdefault(k, v)
    baseline = config.get("baseline_condition", "C0")

    call_info = {c["call_id"]: c for c in data["calls"]}
    rows: list[dict] = []
    for o in data["observations"]:
        check_metric(o["metric"])
        tags = tuple(_loads(o["tags_json"], []))
        variant, dims, plain = _parse_tags(tags)
        ci = call_info.get(o["call_id"], {})
        rows.append({
            "metric": o["metric"], "key": o["metric"] + (f"@{variant}" if variant else ""),
            "call_id": o["call_id"], "scenario_id": o["scenario_id"], "trial": o["trial"],
            "condition": o["condition"], "system_id": o["system_id"], "event_id": o["event_id"],
            "value": o["value"], "valid": bool(o["valid"]), "tags": tuple(plain),
            "dims": dims, "is_control": bool(ci.get("is_control") or 0),
        })
    main = [r for r in rows if not r["is_control"]]

    # systems and their flags
    sys_cfg: dict[str, dict] = {}
    for c in configs:
        for sid, s in (c.get("systems") or {}).items():
            sys_cfg.setdefault(sid, s or {})
    system_ids = sorted({r["system_id"] for r in rows} | set(sys_cfg)
                        | {c["system_id"] for c in data["calls"]})
    systems = {sid: {"self_hosted": bool(sys_cfg.get(sid, {}).get("self_hosted", False)),
                     "components_only": bool(sys_cfg.get(sid, {}).get("components_only", False)),
                     "cancel_event": sys_cfg.get(sid, {}).get("cancel_event"),
                     "config_id": sys_cfg.get(sid, {}).get("config_id")}
               for sid in system_ids}
    conditions = sorted({r["condition"] for r in rows} | {c["condition"] for c in data["calls"]})

    # classifier validation status (spec §8; filled by M7b into the run card)
    cv = card.get("classifier_validation") or {}
    present = {r["metric"] for r in rows}
    classifiers = {}
    for m in sorted(CLASSIFIER_METRICS & present | set(cv)):
        check_metric(m)
        e = cv.get(m) or {}
        kappa = e.get("kappa")
        classifiers[m] = {"kappa": _r(kappa), "validated": bool(
            kappa is not None and kappa >= th.kappa_min and e.get("validated", True))}

    def markers(metric: str, grp: list[dict]) -> list[str]:
        tagset = {t for r in grp for t in r["tags"]}
        out = []
        if (metric in CLASSIFIER_METRICS or "classifier" in tagset) and \
                not classifiers.get(metric, {}).get("validated", False):
            out.append("classifier")
        if "judged" in tagset:
            out.append("judged")
        if metric in DIAGNOSTIC_METRICS or "diagnostic" in tagset:
            out.append("diagnostic")
        return out

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in main:
        groups[(r["key"], r["system_id"], r["condition"])].append(r)
    metrics: dict[str, dict] = {}
    for (key, sid, cond), grp in sorted(groups.items()):
        m = metric_name(key)
        s = _summary(m, grp, th)
        mk = markers(m, grp)
        s.update({"spec": Metric(m).spec, "markers": mk,
                  "headline": bool(Metric(m).headline and not mk),
                  "self_hosted": systems[sid]["self_hosted"],
                  "components_only": systems[sid]["components_only"]})
        if m == "call_outcome":
            dist: dict[str, int] = defaultdict(int)
            for cid in sorted({r["call_id"] for r in grp}):
                dist[str(call_info.get(cid, {}).get("outcome"))] += 1
            s["distribution"] = dict(sorted(dist.items()))
        metrics.setdefault(key, {}).setdefault(sid, {})[cond] = s

    # Pass^3 by scenario from Pass@1 trials (spec §4.2), unless scored directly
    if "pass_pow_3" not in metrics:
        p1: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for r in main:
            if r["key"] == "pass_at_1" and r["valid"] and r["value"] is not None:
                p1[(r["system_id"], r["condition"])][r["scenario_id"]].append(
                    (r["trial"], float(r["value"])))
        for (sid, cond), scen in sorted(p1.items()):
            by_scen: dict[str, bool] = {}
            short = 0
            for sc, trials in sorted(scen.items()):
                if len(trials) < PASS_POW_K:
                    short += 1
                    continue
                by_scen[sc] = all(v == 1.0 for _, v in sorted(trials)[:PASS_POW_K])
            vals = [1.0 if v else 0.0 for v in by_scen.values()]
            n = len(vals)
            s = {"kind": "rate_by_scenario", "n": n, "n_rows": n + short, "k": int(sum(vals)),
                 "value": _r(sum(vals) / n) if n else None,
                 "ci": _ci(bootstrap_ci(vals, None, _mean, th.bootstrap_resamples, SEED))
                 if n else None,
                 "scenarios_short": short, "by_scenario": by_scen, "tags": {},
                 "breakdowns": {}, "spec": Metric.pass_pow_3.spec, "markers": [],
                 "headline": True, "self_hosted": systems[sid]["self_hosted"],
                 "components_only": systems[sid]["components_only"]}
            metrics.setdefault("pass_pow_3", {}).setdefault(sid, {})[cond] = s

    # pre-registered comparisons (spec §9.1): paired, clustered, Holm across <= max
    comps_cfg = config.get("comparisons") or []
    if len(comps_cfg) > th.holm_max_comparisons:
        raise TooManyComparisonsError(
            f"{len(comps_cfg)} pre-registered comparisons > holm_max_comparisons "
            f"{th.holm_max_comparisons}")
    comparisons = []
    for c in comps_cfg:
        m = check_metric(c["metric"]).value
        key = m + (f"@{c['variant']}" if c.get("variant") else "")
        cond = c.get("condition", baseline)
        a_rows = [r for r in main if r["key"] == key and r["system_id"] == c["a"]
                  and r["condition"] == cond]
        b_rows = [r for r in main if r["key"] == key and r["system_id"] == c["b"]
                  and r["condition"] == cond]
        res = _pair(a_rows, b_rows, _key_event, m, th)
        res.update({"metric": key, "a": c["a"], "b": c["b"], "condition": cond})
        comparisons.append(res)
    pv = [c["p"] for c in comparisons]
    rej = holm([p if p is not None else 1.0 for p in pv]) if comparisons else []
    for c, rj in zip(comparisons, rej):
        excl0 = c["ci"] is not None and (c["ci"][0] > 0 or c["ci"][1] < 0)
        c["holm_reject"] = bool(rj)
        c["distinguishable"] = bool(rj and excl0)

    # condition deltas: every other condition minus the baseline, paired (spec §10 item 8)
    deltas: dict[str, dict] = {}
    for key in sorted(metrics):
        if key == "pass_pow_3" and "pass_pow_3" not in {r["key"] for r in main}:
            continue
        m = metric_name(key)
        for sid in sorted(metrics[key]):
            conds = metrics[key][sid]
            if baseline not in conds:
                continue
            base = [r for r in main if r["key"] == key and r["system_id"] == sid
                    and r["condition"] == baseline]
            for cond in sorted(conds):
                if cond == baseline:
                    continue
                other = [r for r in main if r["key"] == key and r["system_id"] == sid
                         and r["condition"] == cond]
                d = _pair(other, base, _key_event, m, th)
                d.update({"minus": baseline})
                deltas.setdefault(key, {}).setdefault(sid, {})[cond] = d

    # interrupted vs clean (control twins), Track C rates at scenario level (spec §4.2)
    split: dict[str, dict] = {}
    ctrl = [r for r in rows if r["is_control"]]
    if ctrl:
        for m in TRACK_C_SPLIT:
            for sid in system_ids:
                for cond in conditions:
                    b = [r for r in ctrl if r["key"] == m and r["system_id"] == sid
                         and r["condition"] == cond]
                    if not b:
                        continue
                    scen = {r["scenario_id"] for r in b}
                    a = [r for r in main if r["key"] == m and r["system_id"] == sid
                         and r["condition"] == cond and r["scenario_id"] in scen]
                    sa, sb = _scenario_means(a), _scenario_means(b)
                    d = _pair(sa, sb, lambda r: (r["scenario_id"],), m, th)
                    d.update({"interrupted": _r(_mean([r["value"] for r in sa if r["valid"]])),
                              "clean": _r(_mean([r["value"] for r in sb if r["valid"]])),
                              "n_scenarios": len(scen)})
                    split.setdefault(m, {}).setdefault(sid, {})[cond] = d

    # exclusions per system: distinct (call, event) units carrying each tag (spec §3.4)
    exclusions: dict[str, dict] = {}
    for sid in system_ids:
        units = {(r["call_id"], r["event_id"] or "") for r in main if r["system_id"] == sid}
        per: dict[str, int] = {}
        for tag in sorted(EXCLUDE_TAGS):
            per[tag] = len({(r["call_id"], r["event_id"] or "") for r in main
                            if r["system_id"] == sid and tag in r["tags"]})
        exclusions[sid] = {"units": len(units), "counts": per}

    calls_summary: dict[str, dict] = {}
    for c in data["calls"]:
        e = calls_summary.setdefault(c["system_id"], {}).setdefault(
            c["condition"], {"n_calls": 0, "n_controls": 0, "outcomes": {}})
        e["n_calls"] += 1
        e["n_controls"] += int(bool(c["is_control"]))
        o = str(c["outcome"])
        e["outcomes"][o] = e["outcomes"].get(o, 0) + 1
    for sid in calls_summary:
        for cond in calls_summary[sid]:
            calls_summary[sid][cond]["outcomes"] = dict(sorted(
                calls_summary[sid][cond]["outcomes"].items()))

    # simulated-caller speculation counters per system (fix-42), from
    # calls.flags_json["simulator"]["flags"]; non-control calls; present only when non-zero
    sim_counts: dict[str, dict[str, int]] = {}
    for c in data["calls"]:
        if c.get("is_control"):
            continue
        sf = (_loads(c.get("flags_json"), {}).get("simulator") or {}).get("flags") or {}
        for k in SIMULATOR_COUNTERS:
            v = int(sf.get(k) or 0)
            if v:
                e = sim_counts.setdefault(c["system_id"], {})
                e[k] = e.get(k, 0) + v

    phone = card.get("phone_path") or {}
    out = {
        "schema": SCHEMA_VERSION,
        "run_ids": run_ids,
        "hashes": hashes,
        "thresholds_hash": th.hash(),
        "baseline_condition": baseline,
        "systems": systems,
        "conditions": conditions,
        "n_observations": len(rows),
        "metrics": metrics,
        "comparisons": comparisons,
        "condition_deltas": deltas,
        "interrupted_vs_clean": split,
        "exclusions": exclusions,
        "calls": calls_summary,
        "classifiers": classifiers,
        "phone_path": {"disagreements": sorted(phone.get("disagreements") or [])},
    }
    if sim_counts:                  # absent when zero: older aggregates stay byte-identical
        out["simulator_counts"] = sim_counts
    return _sorted(out)


def _sorted(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sorted(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_sorted(x) for x in obj]
    return obj


def redact_public(agg: dict) -> dict:
    """The `--public` view: no scenario ids anywhere (spec §11 publication policy)."""
    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items() if k != "by_scenario"}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    out = walk(agg)
    out["public"] = True
    return out


def to_json(agg: dict) -> str:
    """Canonical, stable text written to report_dir/aggregate.json."""
    return json.dumps(agg, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
