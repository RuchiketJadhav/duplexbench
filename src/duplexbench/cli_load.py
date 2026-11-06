"""`duplexbench load` and `duplexbench gate` (BUILD_PLAN M9-T03/T04, M8-T03; spec §4.4, CLAUDE.md M8).

    uv run duplexbench load configs/runs/<run>.yaml --concurrency 8 [--calls-per-level 100] [--yes]
    uv run duplexbench gate <run_id> [--db results/<run_id>/events.sqlite]

`load` plans the run with cli_run.plan_run (same pre-registration, calibration and
price checks), prints the projection for every level, and refuses:
  * exit 3 when the projection exceeds max_usd,
  * exit 1 ("not started") without --yes whenever the projection is above 0 USD
    (priced systems), exactly like `run`. A $0 (mock) projection runs without --yes.
Levels: every `--concurrency` value given; a single value N > 1 is run as (1, N) so the
latency shift has its c = 1 reference. Arrivals are open loop (load/runner.py). Calls go
through cli_run's CallRunner path; the report lands in results/<run_id>/load_<run_id>.json
and SysMon samples in results/<run_id>/sysmon_<run_id>.json.

`card-fill` writes netcheck / loopback / sysmon / calibration / instance type into
the run card (run_card_fill.fill). `gate` runs gates.check_run and prints one line per gate (exit 1 on any failure).
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
from pathlib import Path
from typing import Any, Optional

import typer

RESULTS = Path("results")
EXIT_NOT_STARTED = 1
EXIT_BUDGET = 3


def levels_for(concurrency: list[int]) -> tuple[int, ...]:
    vals = sorted(set(int(c) for c in concurrency))
    if any(v < 1 for v in vals):
        raise typer.BadParameter("concurrency must be >= 1")
    if len(vals) == 1 and vals[0] > 1:
        vals = [1, vals[0]]
    return tuple(vals)


def projected_usd(plan: Any, n_calls: int) -> float:
    if not plan.calls:
        return 0.0
    return n_calls * sum(c.projected_usd for c in plan.calls) / len(plan.calls)


def check_spend(total: float, max_usd: float, yes: bool) -> tuple[int, str] | None:
    """(exit code, message) when the load must not start; None to go ahead."""
    if total > max_usd + 1e-12:
        return EXIT_BUDGET, f"refused: projected {total:.4f} USD > max_usd {max_usd:.4f} USD"
    if total > 0 and not yes:
        return EXIT_NOT_STARTED, "not started: re-run with --yes to spend the projected amount"
    return None


def register(app: typer.Typer) -> None:

    @app.command("load")
    def load_cmd(run_config: Path,
                 concurrency: list[int] = typer.Option([1, 8], "--concurrency", "-c",
                                                       help="level(s); repeat for several"),
                 calls_per_level: int = typer.Option(100, "--calls-per-level",
                                                     help="spec 4.4: >= 100 events per level"),
                 mean_session_s: Optional[float] = typer.Option(
                     None, "--mean-session-s", help="default: mean planned call length"),
                 yes: bool = typer.Option(False, "--yes", help="approve the projected spend"),
                 max_usd: Optional[float] = typer.Option(None, "--max-usd"),
                 allow_unregistered: bool = typer.Option(False, "--allow-unregistered")) -> None:
        """Open-loop concurrency levels on Track A (spec 4.4); refuses priced runs without --yes."""
        from duplexbench import _loop, cli_run
        from duplexbench.load.runner import run_load

        cfg = cli_run.RunConfig.model_validate(
            __import__("yaml").safe_load(run_config.read_text(encoding="utf-8")))
        try:
            plan = cli_run.plan_run(cfg)
        except cli_run.RunRefused as e:
            typer.echo(str(e))
            raise typer.Exit(e.code)
        cli_run.print_plan(plan)
        levels = levels_for(concurrency)
        n_total = calls_per_level * len(levels)
        total = projected_usd(plan, n_total)
        cap = cfg.max_usd if max_usd is None else max_usd
        typer.echo(f"load: levels {list(levels)} x {calls_per_level} calls = {n_total} calls; "
                   f"projected {total:.4f} USD (max_usd {cap:.4f})")
        try:
            cli_run.check_plan(plan, max_usd=float("inf"), yes=True,
                               allow_unregistered=allow_unregistered)
        except cli_run.RunRefused as e:
            typer.echo(str(e))
            raise typer.Exit(e.code)
        stop = check_spend(total, cap, yes)
        if stop:
            typer.echo(stop[1])
            raise typer.Exit(stop[0])
        if not plan.calls:
            typer.echo("refused: the plan has no calls")
            raise typer.Exit(EXIT_NOT_STARTED)

        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{cfg.name}-load-{ts}"
        out = plan.results_dir / run_id
        out.mkdir(parents=True, exist_ok=True)
        mean_s = mean_session_s or max(
            1e-3, sum(c.minutes for c in plan.calls) / len(plan.calls) * 60.0)
        report = _loop.run(_execute(plan, run_id, out, levels, calls_per_level, mean_s,
                                      run_load, cli_run))
        for s in report["levels"]:
            typer.echo(f"c={s['level']}: n={s['n']} error {s['error_rate']['k']}/{s['n']} "
                       f"429 {s['rate_429']['k']}/{s['n']} disconnect "
                       f"{s['disconnect_rate']['k']}/{s['n']} slip p99 {s['pacing_slip_p99_ms']} ms"
                       + ("  LOAD_INVALID " + ",".join(s["invalid_reasons"]) if s["load_invalid"]
                          else ""))
        typer.echo(f"load report: {report.get('report_path')}")

    @app.command("card-fill")
    def card_fill_cmd(run_id: str,
                      db: Optional[Path] = typer.Option(None, "--db", help="sqlite event log"),
                      netcheck: Optional[Path] = typer.Option(None, "--netcheck",
                                                              help="JSON printed by `netcheck`"),
                      loopback: Optional[Path] = typer.Option(None, "--loopback",
                                                              help="loopback baseline JSON"),
                      sysmon: Optional[Path] = typer.Option(None, "--sysmon",
                                                            help="sysmon_<run>.json"),
                      calibration: Optional[Path] = typer.Option(
                          None, "--calibration", help="results/calibration-<ts>.json"),
                      instance_type: Optional[str] = typer.Option(None, "--instance-type")) -> None:
        """Write run-card facts collected outside the call loop into the runs row."""
        import json

        from duplexbench.run_card_fill import fill

        def rd(p: Optional[Path]) -> Any:
            return None if p is None else json.loads(p.read_text(encoding="utf-8"))
        sm = rd(sysmon)
        if isinstance(sm, dict) and "summary" in sm:
            sm = sm["summary"]
        cal = rd(calibration)
        if isinstance(cal, dict) and "passed" not in cal and "pass" in cal:
            cal = {**cal, "passed": bool(cal["pass"])}
        path = db if db is not None else RESULTS / run_id / "events.sqlite"
        fill(path, run_id, calibration=cal, netcheck=rd(netcheck), loopback=rd(loopback),
             sysmon=sm, instance_type=instance_type)
        typer.echo(f"run card updated: {run_id}")

    @app.command("gate")
    def gate_cmd(run_id: str,
                 db: Optional[Path] = typer.Option(None, "--db", help="sqlite event log"),
                 thresholds: Optional[Path] = typer.Option(None, "--thresholds")) -> None:
        """M8 release gate: run card, n+CI, power notes, calibration, harness-invalid share,
        attempts, exclusion rates, pre-registered coverage."""
        from duplexbench.gates import check_run, print_gate
        from duplexbench.thresholds import Thresholds
        path = db if db is not None else RESULTS / run_id / "events.sqlite"
        rep = check_run(path, run_id, Thresholds.load(thresholds))
        typer.echo(print_gate(rep))
        raise typer.Exit(0 if rep.passed else 1)


async def _execute(plan: Any, run_id: str, out: Path, levels: tuple[int, ...],
                   calls_per_level: int, mean_s: float, run_load: Any, cli_run: Any) -> dict:
    """Live path: one shared RealClock for arrivals; each call is cli_run's CallRunner call."""
    from duplexbench.clock import RealClock
    from duplexbench.storage import Store, canonical_json

    store = Store(out / "events.sqlite")
    clock = RealClock()
    store.put("runs", {"run_id": run_id, "started_ns": 0,
                       "config_json": canonical_json(cli_run.config_json(plan)),
                       "hashes_json": canonical_json(plan.hashes),
                       "calibration_json": canonical_json(plan.calibration),
                       "run_card_json": canonical_json(cli_run.run_card(plan, run_id))})

    async def factory(ctx):
        cp = plan.calls[(ctx.level * calls_per_level + ctx.index) % len(plan.calls)]
        cp = dataclasses.replace(cp, call_id=ctx.call_id)
        res, _adapter = await cli_run._one_call(plan, cp, store, run_id, out, None)
        return {"outcome": res.outcome, "flags": res.flags, "call_id": ctx.call_id}

    try:
        return await clock.run(run_load(clock, factory, th=plan.th, levels=levels,
                                        calls_per_level=calls_per_level, mean_session_s=mean_s,
                                        seed=plan.cfg.seed, store=store, run_id=run_id,
                                        out_dir=out))
    finally:
        store.close()
