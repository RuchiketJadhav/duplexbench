"""Report-side CLI commands (BUILD_PLAN M7-T02/T04/T05): report, verify, planner, netcheck.

Wiring: `from duplexbench.cli_report import register; register(app)` in cli.py.
Imports happen inside each command so a broken optional module cannot break others.
Default store: results/<run_id>/events.sqlite; default report dir:
results/<run_id>/report (report-public under --public).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

RESULTS = Path("results")


def _db(run_id: str, db: Optional[Path]) -> Path:
    return db if db is not None else RESULTS / run_id / "events.sqlite"


def register(app: typer.Typer) -> None:

    @app.command("report")
    def report_cmd(run_id: str,
                   public: bool = typer.Option(False, "--public", help="no clips, no scenario ids"),
                   db: Optional[Path] = typer.Option(None, "--db", help="sqlite event log"),
                   out: Optional[Path] = typer.Option(None, "--out", help="report directory"),
                   thresholds: Optional[Path] = typer.Option(None, "--thresholds")) -> None:
        """Render the HTML report for a run (spec §10)."""
        from duplexbench.report.html import report
        from duplexbench.thresholds import Thresholds
        th = Thresholds.load(thresholds)
        out_dir = out or RESULTS / run_id / ("report-public" if public else "report")
        index = report(_db(run_id, db), run_id, out_dir, th, public=public)
        typer.echo(f"report: {index}")

    @app.command("verify")
    def verify_cmd(run_id: str, report_dir: Path,
                   db: Optional[Path] = typer.Option(None, "--db"),
                   thresholds: Optional[Path] = typer.Option(None, "--thresholds")) -> None:
        """Recompute every number from the store and diff the rendered aggregate.json."""
        from duplexbench.report.verify import verify
        from duplexbench.thresholds import Thresholds
        diffs = verify(_db(run_id, db), run_id, report_dir, Thresholds.load(thresholds))
        for d in diffs:
            typer.echo(d)
        typer.echo(f"verify: {len(diffs)} difference(s)")
        raise typer.Exit(1 if diffs else 0)

    @app.command("planner")
    def planner_cmd(n: list[int] = typer.Option([50, 100, 200, 400], "--n"),
                    delta: Optional[float] = typer.Option(None, "--delta"),
                    discordant: Optional[float] = typer.Option(None, "--discordant"),
                    pairs: Optional[int] = typer.Option(None, "--pairs",
                                                        help="planned pairs for the comparison")) -> None:
        """Print the spec §9.2 sample-size table; refuse 'distinguishable' for an underpowered design."""
        from duplexbench.stats.planner import half_width, pairs_needed, percentile_min_n
        typer.echo("Rate CI half-width at p = 0.5 (Wilson 95%):")
        for k in n:
            typer.echo(f"  n={k:>5}: +/-{100 * half_width(k):.1f} pts")
        typer.echo("Percentile minimum n (usable bootstrap CI):")
        for p in (50, 90, 95, 99):
            typer.echo(f"  p{p}: {percentile_min_n(p)}")
        for d, disc in ((0.10, 0.30), (0.20, 0.40)):
            typer.echo(f"Paired difference {d:.0%}, {disc:.0%} discordant, 80% power, alpha 0.05: "
                       f"{pairs_needed(d, disc)} pairs")
        if delta is not None and discordant is not None:
            need = pairs_needed(delta, discordant)
            typer.echo(f"Requested: delta {delta}, discordant {discordant}: {need} pairs needed")
            if pairs is not None:
                if pairs < need:
                    typer.echo(f"REFUSED: {pairs} planned pairs < {need}; this design cannot "
                               "label the comparison 'distinguishable'.")
                    raise typer.Exit(2)
                typer.echo(f"OK: {pairs} planned pairs >= {need}.")

    @app.command("netcheck")
    def netcheck_cmd(endpoints: list[str],
                     samples: int = typer.Option(20, "--samples")) -> None:
        """Median TCP connect / TLS handshake / round trip per endpoint (spec §4.4)."""
        import json

        from duplexbench.netcheck import netcheck
        typer.echo(json.dumps(netcheck(endpoints, samples), indent=1, sort_keys=True))
