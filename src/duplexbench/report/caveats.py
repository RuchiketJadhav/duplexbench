"""Auto-generated caveats (spec §10 item 10; BUILD_PLAN M7-T04).

One line per condition found in the aggregate (report/aggregate.py):

  low_n            percentiles below the spec §9.2 minimum n
  exclusion        ambiguous_end / not_injected / unmatched_control / contested counts
  exclusion        no_refasr / step_not_reached counts (harness-side gaps; fix-23, fix-32)
  simulator        slow_caller and unscripted_overlap counts; mishearing and regeneration rates;
                   speculation_from_provisional / provisional_mismatches / provisional_replies
                   counts per system (fix-42; aggregate "simulator_counts", one line each)
  phone_path       metrics whose paired-difference sign disagrees with live stream
  classifier       classifier validation status (spec §8)
  cancel           systems with no provider cancel event (T_stop_model absent; spec §1.3)

`caveats()` returns `[(kind, text), ...]` in a stable order; `lines()` is the text only.
"""
from __future__ import annotations

from duplexbench.report.aggregate import PERCENTILES, SIMULATOR_COUNTERS
from duplexbench.stats.planner import percentile_min_n
from duplexbench.thresholds import Thresholds

EXCLUSION_ORDER = ("ambiguous_end", "not_injected", "unmatched_control", "contested")
SIMULATOR_TAGS = ("slow_caller", "unscripted_overlap")
# harness-side gaps (fix-23, fix-32): the observation could not be scored, so it is out of n
# fix-42: simulated-caller speculation counters (aggregate "simulator_counts")
SIMULATOR_COUNTER_TEXT = {
    "speculation_from_provisional": "speculative replies started from provisional "
                                    "(not yet final) reference-ASR words",
    "provisional_mismatches": "provisional-word speculations discarded because the final "
                              "words differed (one extra caller-LLM call each)",
    "provisional_replies": "caller replies spoken before the reference-ASR final words of "
                           "the agent turn arrived (generated from provisional words)",
}
HARNESS_GAP_TAGS = {
    "no_refasr": "no reference-ASR words for the ear audio (cli score without a reference "
                 "ASR); excluded from the word-dependent metrics, not an agent failure.",
    "step_not_reached": "the caller step carrying the entity never ended; excluded from the "
                        "entity metrics, not an agent failure.",
}


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def caveats(agg: dict, th: Thresholds) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    metrics = agg.get("metrics", {})
    mins = ", ".join(f"p{p} {percentile_min_n(p)}" for p in PERCENTILES)

    for key in sorted(metrics):
        for sid in sorted(metrics[key]):
            for cond in sorted(metrics[key][sid]):
                s = metrics[key][sid][cond]
                if s.get("low_n"):
                    out.append(("low_n",
                                f"Low n: {key} [{sid}, {cond}] n={s['n']}; "
                                f"{', '.join(s['low_n'])} below the minimum ({mins}); "
                                "read these percentiles as indicative only."))

    for sid in sorted(agg.get("exclusions", {})):
        e = agg["exclusions"][sid]
        for tag in EXCLUSION_ORDER:
            c = e["counts"].get(tag, 0)
            if c:
                out.append(("exclusion",
                            f"{sid}: {c} of {e['units']} scored events tagged {tag}; "
                            "excluded from the metrics they are invalid for "
                            "(an expected outcome, reported per system)."))
        for tag, why in HARNESS_GAP_TAGS.items():
            c = e["counts"].get(tag, 0)
            if c:
                out.append(("exclusion", f"{sid}: {c} of {e['units']} scored events tagged "
                                         f"{tag}: {why}"))
        for tag in SIMULATOR_TAGS:
            c = e["counts"].get(tag, 0)
            if c:
                out.append(("simulator",
                            f"{sid}: {c} of {e['units']} scored events tagged {tag} "
                            "(simulated-caller artefact; excluded from latency / Track A metrics)."))

    for sid in sorted(agg.get("simulator_counts", {})):
        sc = agg["simulator_counts"][sid]
        for k in SIMULATOR_COUNTERS:
            if sc.get(k):
                out.append(("simulator", f"{sid}: {sc[k]} {SIMULATOR_COUNTER_TEXT[k]} "
                                         f"({k})."))

    for key, label in (("mishearing_rate", "simulator mishearing rate"),
                       ("regeneration_rate", "simulator regeneration rate")):
        for sid in sorted(metrics.get(key, {})):
            for cond in sorted(metrics[key][sid]):
                s = metrics[key][sid][cond]
                text = f"{sid} [{cond}]: {label} {_fmt(s.get('value'))} (n={s['n']})"
                if key == "regeneration_rate" and s.get("value") is not None \
                        and s["value"] > th.sim_regen_review_rate:
                    text += f"; above the review rate {th.sim_regen_review_rate}, scenarios need review"
                out.append(("simulator", text + "."))

    for m in agg.get("phone_path", {}).get("disagreements", []):
        out.append(("phone_path",
                    f"Phone path disagrees with live stream on the sign of the paired difference "
                    f"for {m}."))

    for m in sorted(agg.get("classifiers", {})):
        c = agg["classifiers"][m]
        if not c["validated"]:
            k = "not measured" if c["kappa"] is None else f"{c['kappa']:.3f}"
            out.append(("classifier",
                        f"{m}: classifier not validated (kappa {k}, needs >= {th.kappa_min}); "
                        "carries the classifier marker and stays out of headline rows."))
        else:
            out.append(("classifier", f"{m}: classifier validated (kappa {c['kappa']:.3f})."))

    for sid in sorted(agg.get("systems", {})):
        s = agg["systems"][sid]
        stop_ear = metrics.get("t_stop_ear", {}).get(sid)
        stop_model = metrics.get("t_stop_model", {}).get(sid, {})
        no_model = stop_ear is not None and not any(v["n"] for v in stop_model.values())
        if s.get("cancel_event") is False or no_model:
            out.append(("cancel",
                        f"{sid}: no provider cancel event; T_stop_model is absent and the ear "
                        "hears the full backlog (no flush is invented; see playout penalty)."))
    return out


def lines(agg: dict, th: Thresholds) -> list[str]:
    return [t for _, t in caveats(agg, th)]
