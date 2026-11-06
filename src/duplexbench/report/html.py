"""HTML report (spec §10 items 1-10; BUILD_PLAN M7-T04).

    aggregate(store, run) --+                     out_dir/index.html
    run_card.build(...)   --+--> render(...) -->  out_dir/aggregate.json   (verify diffs this)
    failure_clips(...)    --+    (Jinja2)         out_dir/run_card.json
                                                  out_dir/clips/*          (not under --public)

Sections (anchor ids): run-card, profile, comparisons, floor-control, latency, tasks,
entities, frontier, deltas, exclusions, clips, caveats. Charts are inline SVG made
here, deterministic to the byte. `--public` (`public=True`) drops the clip browser
and every scenario id (`aggregate.redact_public`). There is no composite score.
`report/clips.py` (`cut`) is imported lazily and only when clips are rendered.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Iterable

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape

from duplexbench.report import caveats as caveats_mod
from duplexbench.report.aggregate import (PERCENTILE, _connect, metric_name, redact_public,
                                          to_json)
from duplexbench.thresholds import Thresholds

TEMPLATES = Path(__file__).resolve().parent / "templates"

EAR_LATENCIES = ("t_turn_gap_ear", "t_stop_ear", "t_respond_ear", "t_tool_to_info_ear",
                 "t_dead_air")
MODEL_TWIN = {"t_turn_gap_ear": "t_ttfa_recv", "t_stop_ear": "t_stop_model"}
TASK_ROWS = ("pass_at_1", "pass_pow_3", "stale_action_rate", "preemptive_tool_rate",
             "unsupported_claim_rate", "policy_violation_rate", "auth_bypass_rate",
             "tool_wait_pass", "filler_count", "filler_s", "dead_air_in_tool_s",
             "repeated_prompt_rate", "response_rate_per_turn", "t_tool_offset",
             "t_tool_to_info_ear", "anchor_offset_in_utt_ms", "call_outcome")
TASK_DIMS = ("domain", "bucket")
ENTITY_ROWS = ("capture_first", "capture_final", "recovery_request_rate", "recovery_success",
               "readback_presence", "readback_error_rate", "codec_penalty")
ENTITY_DIM = "type"
# failure classes for the clip browser: rate metrics where 0 is a failure / where 1 is
FAIL_ON_ZERO = ("yield_rate", "hold_speaking_rate", "hold_listening_rate", "response_rate",
                "wait_compliance", "pass_at_1", "capture_first", "capture_final")
FAIL_ON_ONE = ("stale_action_rate", "preemptive_tool_rate", "policy_violation_rate",
               "auth_bypass_rate", "unsupported_claim_rate")
CLIPS_PER_CLASS = 5
PALETTE = ("#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2", "#65a30d")

# chart geometry (pixels; not spec thresholds)
W, H, M = 640, 400, 56
GUIDE = "#9ca3af"


# ---- lookup helpers --------------------------------------------------------------

def _get(agg: dict, keys: Iterable[str], sid: str, cond: str) -> dict | None:
    for k in keys:
        s = agg["metrics"].get(k, {}).get(sid, {}).get(cond)
        if s is not None:
            return s
    return None


def _val(s: dict | None) -> float | None:
    return None if s is None else s.get("value")


def _points(agg: dict) -> list[tuple[str, str]]:
    return [(sid, c) for sid in sorted(agg["systems"]) for c in agg["conditions"]]


def _color(agg: dict, sid: str) -> str:
    return PALETTE[sorted(agg["systems"]).index(sid) % len(PALETTE)]


def _f(x: float) -> str:
    return f"{x:.1f}"


# ---- SVG -------------------------------------------------------------------------

def _frame(title: str, xlabel: str, ylabel: str, x0: float, x1: float, y0: float, y1: float,
           body: list[str]) -> Markup:
    pw, ph = W - 2 * M, H - 2 * M
    ticks = []
    for i in range(5):
        fx = x0 + (x1 - x0) * i / 4
        fy = y0 + (y1 - y0) * i / 4
        px = M + pw * i / 4
        py = H - M - ph * i / 4
        ticks.append(f'<text x="{_f(px)}" y="{_f(H - M + 16)}" text-anchor="middle" '
                     f'class="tick">{fx:.3g}</text>')
        ticks.append(f'<text x="{_f(M - 6)}" y="{_f(py + 4)}" text-anchor="end" '
                     f'class="tick">{fy:.3g}</text>')
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="{escape(title)}" class="chart">',
           f'<rect x="{M}" y="{M}" width="{pw}" height="{ph}" class="plot"/>',
           f'<text x="{W // 2}" y="{M // 2}" text-anchor="middle" class="title">{escape(title)}</text>',
           f'<text x="{W // 2}" y="{H - 12}" text-anchor="middle" class="axis">{escape(xlabel)}</text>',
           f'<text x="14" y="{H // 2}" text-anchor="middle" class="axis" '
           f'transform="rotate(-90 14 {H // 2})">{escape(ylabel)}</text>',
           *ticks, *body, "</svg>"]
    return Markup("\n".join(svg))


def _scale(v: float, a: float, b: float, lo: float, hi: float) -> float:
    return lo if b == a else lo + (v - a) / (b - a) * (hi - lo)


def scatter(agg: dict, title: str, xlabel: str, ylabel: str,
            pts: list[dict], size_label: str | None = None) -> Markup:
    """pts: {label, sid, x, y, size (0..1 or None)}; axes are rates in [0, 1]."""
    body = []
    pw, ph = W - 2 * M, H - 2 * M
    for p in pts:
        if p["x"] is None or p["y"] is None:
            continue
        cx = _scale(p["x"], 0, 1, M, M + pw)
        cy = _scale(p["y"], 0, 1, H - M, M)
        r = 4 + 12 * (p["size"] if p.get("size") is not None else 0)
        body.append(f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(r)}" fill="{_color(agg, p["sid"])}" '
                    f'fill-opacity="0.6"><title>{escape(p["label"])}</title></circle>')
        body.append(f'<text x="{_f(cx + r + 3)}" y="{_f(cy + 4)}" class="pt">{escape(p["label"])}</text>')
    if size_label:
        body.append(f'<text x="{W - M}" y="{M - 6}" text-anchor="end" class="tick">'
                    f'marker size = {escape(size_label)}</text>')
    return _frame(title, xlabel, ylabel, 0, 1, 0, 1, body)


def pareto(pts: list[dict]) -> list[dict]:
    """Points no other point beats on both axes (x lower is better, y higher is better)."""
    ok = [p for p in pts if p["x"] is not None and p["y"] is not None]
    front = []
    for p in ok:
        dominated = any(q is not p and q["x"] <= p["x"] and q["y"] >= p["y"]
                        and (q["x"] < p["x"] or q["y"] > p["y"]) for q in ok)
        if not dominated:
            front.append(p)
    return sorted(front, key=lambda p: (p["x"], -p["y"], p["label"]))


def frontier(agg: dict, title: str, xlabel: str, pts: list[dict]) -> tuple[Markup, list[str]]:
    """x lower is better, y = Pass@1; gray dashed perpendicular guides at the best value per axis."""
    ok = [p for p in pts if p["x"] is not None and p["y"] is not None]
    if not ok:
        return _frame(title, xlabel, "Pass@1", 0, 1, 0, 1, []), []
    xs = [p["x"] for p in ok]
    xa, xb = min(0.0, min(xs)), max(xs) * 1.1 if max(xs) > 0 else 1.0
    pw, ph = W - 2 * M, H - 2 * M
    body = []
    bx = _scale(min(xs), xa, xb, M, M + pw)
    by = _scale(max(p["y"] for p in ok), 0, 1, H - M, M)
    body.append(f'<line x1="{_f(bx)}" y1="{M}" x2="{_f(bx)}" y2="{H - M}" stroke="{GUIDE}" '
                'stroke-dasharray="4 4" class="guide"/>')
    body.append(f'<line x1="{M}" y1="{_f(by)}" x2="{M + pw}" y2="{_f(by)}" stroke="{GUIDE}" '
                'stroke-dasharray="4 4" class="guide"/>')
    for p in ok:
        cx, cy = _scale(p["x"], xa, xb, M, M + pw), _scale(p["y"], 0, 1, H - M, M)
        body.append(f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="6" fill="{_color(agg, p["sid"])}">'
                    f'<title>{escape(p["label"])}</title></circle>')
        body.append(f'<text x="{_f(cx + 9)}" y="{_f(cy + 4)}" class="pt">{escape(p["label"])}</text>')
    return _frame(title, xlabel, "Pass@1", xa, xb, 0, 1, body), [p["label"] for p in pareto(ok)]


def latency_bars(agg: dict, metric: str) -> Markup | None:
    rows = []
    for sid, cond in _points(agg):
        s = agg["metrics"].get(metric, {}).get(sid, {}).get(cond)
        if not s or s.get("kind") != "percentile":
            continue
        pc = s["percentiles"]
        p50, p90, p95 = (pc[k]["value"] for k in ("p50", "p90", "p95"))
        if p50 is None:
            continue
        rows.append((f"{sid} {cond}", sid, p50, p90, p95, s["n"]))
    if not rows:
        return None
    lo = min(0.0, min(r[2] for r in rows))
    hi = max(r[4] for r in rows) or 1.0
    rh = 26
    h = M + rh * len(rows) + 32
    left, right = 150, W - 24
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" role="img" '
           f'aria-label="{metric} p50 to p95" class="chart bars">',
           f'<text x="{left}" y="20" class="title">{metric}: bar p50 &#8594; p95, tick p90 (ms)</text>']
    for i, (label, sid, p50, p90, p95, n) in enumerate(rows):
        y = 36 + i * rh
        x50, x90, x95 = (_scale(v, lo, hi, left, right) for v in (p50, p90, p95))
        out.append(f'<text x="{left - 6}" y="{y + 13}" text-anchor="end" class="pt">'
                   f'{escape(label)} (n={n})</text>')
        out.append(f'<rect x="{_f(x50)}" y="{y + 3}" width="{_f(max(1.0, x95 - x50))}" height="14" '
                   f'fill="{_color(agg, sid)}" fill-opacity="0.7"/>')
        out.append(f'<line x1="{_f(x90)}" y1="{y}" x2="{_f(x90)}" y2="{y + 20}" stroke="#111827"/>')
        out.append(f'<text x="{_f(x95 + 4)}" y="{y + 13}" class="tick">{p50:.0f}/{p90:.0f}/{p95:.0f}</text>')
    out.append(f'<text x="{left}" y="{h - 8}" class="tick">{lo:.0f}</text>')
    out.append(f'<text x="{right}" y="{h - 8}" text-anchor="end" class="tick">{hi:.0f}</text>')
    out.append("</svg>")
    return Markup("\n".join(out))


# ---- view model --------------------------------------------------------------------

def _variant(base: str, ms: int) -> list[str]:
    return [f"{base}@{ms}", base]


def build_view(agg: dict, card: dict, th: Thresholds, public: bool, clips: list[dict]) -> dict:
    yield_k = _variant("yield_rate", th.yield_headline_ms)
    hold_k = _variant("hold_speaking_rate", th.hold_window_ms)
    resp_ms = max(th.response_windows_ms)
    resp_k = _variant("response_rate", resp_ms)
    listen_k = ["hold_listening_rate"]

    fc1, fc2 = [], []
    for sid, cond in _points(agg):
        lab = f"{sid} {cond}"
        y, x, sz = (_val(_get(agg, k, sid, cond)) for k in (yield_k, hold_k, resp_k))
        fc1.append({"label": lab, "sid": sid, "x": x, "y": y, "size": sz})
        fc2.append({"label": lab, "sid": sid, "y": _val(_get(agg, listen_k, sid, cond)),
                    "x": sz, "size": None})
    chart1 = scatter(agg, f"Yield @{th.yield_headline_ms} ms vs pooled Hold-while-speaking",
                     "Hold-while-speaking (pooled)", f"Yield @{th.yield_headline_ms} ms", fc1,
                     f"Response rate @{resp_ms} ms")
    chart2 = scatter(agg, "Hold-while-listening vs Response rate",
                     f"Response rate @{resp_ms} ms", "Hold-while-listening", fc2)

    base = agg["baseline_condition"]
    cost_pts, gap_pts = [], []
    for sid in sorted(agg["systems"]):
        p1 = _val(_get(agg, ["pass_at_1"], sid, base))
        cost_pts.append({"label": sid, "sid": sid, "y": p1,
                         "x": _val(_get(agg, ["cost_per_min"], sid, base))})
        gap_pts.append({"label": sid, "sid": sid, "y": p1,
                        "x": _val(_get(agg, ["t_turn_gap_ear"], sid, base))})
    fr_cost, front_cost = frontier(agg, "Cost per connected minute vs Pass@1",
                                   "cost per connected minute (USD)", cost_pts)
    fr_gap, front_gap = frontier(agg, "T_turn_gap_ear p50 vs Pass@1",
                                 "T_turn_gap_ear p50 (ms)", gap_pts)

    latency = []
    for m in EAR_LATENCIES:
        twin = MODEL_TWIN.get(m)
        rows = []
        for sid, cond in _points(agg):
            s = agg["metrics"].get(m, {}).get(sid, {}).get(cond)
            if s is None:
                continue
            rows.append({"sid": sid, "cond": cond, "s": s,
                         "twin": agg["metrics"].get(twin, {}).get(sid, {}).get(cond) if twin else None,
                         "penalty": agg["metrics"].get("playout_penalty", {}).get(sid, {}).get(cond)
                         if m == "t_stop_ear" else None,
                         "backlog": agg["metrics"].get("backlog_ms", {}).get(sid, {}).get(cond)
                         if m == "t_stop_ear" else None})
        latency.append({"metric": m, "twin": twin, "bars": latency_bars(agg, m), "rows": rows})

    def table(names: Iterable[str]) -> list[dict]:
        out = []
        for key in sorted(agg["metrics"], key=lambda k: (list(names).index(metric_name(k))
                                                         if metric_name(k) in names else 99, k)):
            if metric_name(key) not in names:
                continue
            for sid in sorted(agg["metrics"][key]):
                for cond in sorted(agg["metrics"][key][sid]):
                    out.append({"key": key, "sid": sid, "cond": cond,
                                "s": agg["metrics"][key][sid][cond]})
        return out

    floors = ((card.get("refasr") or {}).get("floors") or {}) if isinstance(card.get("refasr"), dict) else {}
    profile = table([k for k in sorted({metric_name(k) for k in agg["metrics"]})])
    return {
        "agg": agg, "card": card, "public": public, "th": th,
        "profile": profile,
        "chart1": chart1, "chart2": chart2,
        "latency": latency, "percentile_metrics": PERCENTILE,
        "tasks": table(TASK_ROWS), "task_dims": TASK_DIMS,
        "entities": table(ENTITY_ROWS), "entity_dim": ENTITY_DIM, "floors": floors,
        "fr_cost": fr_cost, "front_cost": front_cost, "fr_gap": fr_gap, "front_gap": front_gap,
        "caveats": caveats_mod.caveats(agg, th),
        "clips": [] if public else clips,
        "clip_classes": sorted({c["failure_class"] for c in clips}) if not public else [],
    }


# ---- Jinja filters -------------------------------------------------------------

def _num(x: Any) -> str:
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        return f"{x:.4g}"
    return str(x)


def _ci_f(ci: Any) -> str:
    if not ci:
        return "—"
    return f"[{_num(ci[0])}, {_num(ci[1])}]"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True,
                      undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True,
                      keep_trailing_newline=True)
    env.filters["num"] = _num
    env.filters["ci"] = _ci_f
    env.filters["pretty"] = lambda o: json.dumps(o, sort_keys=True, indent=1, ensure_ascii=False)
    return env


# ---- clips -----------------------------------------------------------------------

def failure_clips(store: Any, run_id: str, th: Thresholds,
                  per_class: int = CLIPS_PER_CLASS) -> list[dict]:
    """Failure candidates for the clip browser (spec §10 item 9), not yet cut.

    Each: {failure_class, call_id, scenario_id, system_id, condition, event_id,
    wav_path, t0_ns, t1_ns, events}; t0/t1 are ns relative to the call start.
    """
    conn = _connect(store)
    try:
        obs = conn.execute(
            "SELECT o.*, c.wav_path, c.started_ns, c.ended_ns FROM observations o "
            "JOIN calls c ON c.call_id = o.call_id WHERE o.run_id = ? AND o.valid = 1 "
            "AND COALESCE(c.is_control, 0) = 0 "
            "ORDER BY o.metric, o.system_id, o.condition, o.scenario_id, o.trial, o.event_id",
            (run_id,)).fetchall()
        out: list[dict] = []
        taken: dict[str, int] = {}
        pad = th.hold_window_ms * 1_000_000
        for o in obs:
            m = o["metric"]
            bad = (m in FAIL_ON_ZERO and o["value"] == 0) or (m in FAIL_ON_ONE and o["value"] == 1)
            if not bad or taken.get(m, 0) >= per_class or not o["wav_path"]:
                continue
            start = o["started_ns"] or 0
            t0, t1 = start, o["ended_ns"] if o["ended_ns"] is not None else start
            if o["event_id"]:
                ov = conn.execute("SELECT onset_ns, end_ns FROM overlays WHERE call_id = ? "
                                  "AND overlay_id = ?", (o["call_id"], o["event_id"])).fetchone()
                if ov and ov["onset_ns"] is not None:
                    t0 = max(start, ov["onset_ns"] - pad)
                    t1 = (ov["end_ns"] or ov["onset_ns"]) + pad
            evs = [{"t_ns": e["t_ns"] - start, "kind": e["kind"], "src": e["src"]}
                   for e in conn.execute("SELECT t_ns, kind, src FROM events WHERE call_id = ? "
                                         "AND t_ns BETWEEN ? AND ? ORDER BY t_ns, seq",
                                         (o["call_id"], t0, t1))]
            out.append({"failure_class": m, "call_id": o["call_id"],
                        "scenario_id": o["scenario_id"], "system_id": o["system_id"],
                        "condition": o["condition"], "event_id": o["event_id"],
                        "wav_path": o["wav_path"], "t0_ns": t0 - start, "t1_ns": t1 - start,
                        "events": evs})
            taken[m] = taken.get(m, 0) + 1
    finally:
        conn.close()
    return out


def _cut_clips(cands: list[dict], out_dir: Path) -> list[dict]:
    from duplexbench.report.clips import cut      # lazy: owned by report/clips.py
    clip_dir = out_dir / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, c in enumerate(cands):
        # clips.cut takes ms from the recording's first sample; candidates carry ns
        # relative to the call start (= the recording origin), events likewise.
        clip_path, timeline = cut(Path(c["wav_path"]), c["t0_ns"] / 1e6, c["t1_ns"] / 1e6,
                                  c["events"], out_dir=clip_dir)
        clip_path = Path(clip_path)
        name = f"{i:03d}_{clip_path.name}"
        dest = clip_dir / name
        if clip_path.resolve() != dest.resolve():
            shutil.copyfile(clip_path, dest)
        if isinstance(timeline, str):
            try:
                timeline = json.loads(timeline)
            except ValueError:
                timeline = [timeline]
        out.append({**{k: v for k, v in c.items() if k != "events"},
                    "src": f"clips/{name}", "timeline": timeline})
    return out


# ---- entry points ----------------------------------------------------------------

def render(agg: dict, card: dict, out_dir: Path | str, th: Thresholds, *,
           public: bool = False, clips: list[dict] | None = None) -> Path:
    """Write index.html, aggregate.json and run_card.json to out_dir; returns index.html.

    `clips`: candidates from `failure_clips()`; cut with report/clips.py unless public.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    view_agg = redact_public(agg) if public else agg
    cut = [] if public or not clips else _cut_clips(clips, out_dir)
    view = build_view(view_agg, card, th, public, cut)
    html = _env().get_template("report.html.j2").render(**view)
    (out_dir / "aggregate.json").write_text(to_json(view_agg), encoding="utf-8", newline="\n")
    (out_dir / "run_card.json").write_text(
        json.dumps(card, sort_keys=True, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n")
    index = out_dir / "index.html"
    index.write_text(html, encoding="utf-8", newline="\n")
    return index


def report(store: Any, run_id: str, out_dir: Path | str, th: Thresholds, *,
           public: bool = False, netcheck: dict | None = None) -> Path:
    """aggregate + run card + clips + render, as `cli report` runs it."""
    from duplexbench.report.aggregate import aggregate
    from duplexbench.report.run_card import build
    agg = aggregate(store, run_id, th)
    card = build(store, run_id, th, netcheck)
    clips = None if public else failure_clips(store, run_id, th)
    return render(agg, card, out_dir, th, public=public, clips=clips)
