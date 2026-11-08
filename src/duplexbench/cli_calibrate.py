"""`duplexbench calibrate` (spec §1.4; BUILD_PLAN M1-T14). Wired into cli.py via `register(app)`.

    uv run duplexbench calibrate [--config configs/runs/calibrate.yaml] [--calls N] [--virtual]

Prints the calibration table and writes results/calibration-<ts>.json; exits 2 on
fail (a Windows laptop may fail the event-loop lag gate; that is a report, not a bug).

    uv run duplexbench calibrate --refasr-config configs/runs/<run>.yaml [--yes] [--keep-refasr-log]

builds the reference ASR named by that run config's `simulator.refasr` (cli_run.build_refasr)
and runs the D4 reference-ASR stage on the calibration clip (calibrate.DEFAULT_REFASR_CLIP,
195 s since fix-41). A paid engine (`paid = True`, e.g. `deepgram`, decision B11) prints
the projected cost, computed from that clip's duration, and needs --yes (exit 1 without it).
A failed reference-ASR stage writes calibration-<ts>.refasr_log.json beside the JSON
(fix-41); --keep-refasr-log writes it on a pass too.
"""
from __future__ import annotations

from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    @app.command()
    def calibrate(
        config: Path = typer.Option(None, help="calibration run config (default configs/runs/calibrate.yaml)"),
        calls: int = typer.Option(None, help="override the number of mock calls"),
        virtual: bool = typer.Option(False, help="virtual clock (deterministic; checks logic, not timing)"),
        out_dir: Path = typer.Option(None, help="where calibration-<ts>.json is written (default results/)"),
        refasr_config: Path = typer.Option(
            None, "--refasr-config",
            help="run config whose simulator.refasr names the reference ASR to calibrate (D4)"),
        yes: bool = typer.Option(False, "--yes", help="approve a paid reference-ASR stage"),
        keep_refasr_log: bool = typer.Option(
            False, "--keep-refasr-log",
            help="write the reference-ASR diagnostics log on a pass too (always on a fail)"),
    ) -> None:
        """Calibrate the harness against the mock agent (spec §1.4)."""
        from duplexbench import _loop
        from duplexbench.calibrate import CalibConfig, calibrate as run, format_table

        cfg = CalibConfig.load(config, **({"calls": calls} if calls else {}))
        asr = _refasr(refasr_config, yes) if refasr_config else None
        res = _loop.run(run(cfg, virtual=virtual, out_dir=out_dir, asr=asr,
                            keep_refasr_log=keep_refasr_log))
        typer.echo(format_table(res))
        typer.echo(f"written: {res['path']}")
        if not res["pass"]:
            raise typer.Exit(code=2)


def _refasr(run_config: Path, yes: bool):
    """The engine named by `simulator.refasr` in `run_config`; paid engines need --yes."""
    import importlib

    import soundfile as sf
    import yaml

    from duplexbench.calibrate import DEFAULT_REFASR_CLIP
    from duplexbench.cli_run import build_refasr
    from duplexbench.refasr.candidates import CandidateUnavailable

    raw = yaml.safe_load(Path(run_config).read_text(encoding="utf-8")) or {}
    try:
        asr = build_refasr(raw.get("simulator") or {})
    except (ValueError, CandidateUnavailable) as e:
        typer.echo(f"config error: simulator.refasr: {e}", err=True)
        raise typer.Exit(2)
    if asr is None:
        typer.echo(f"{run_config}: no simulator.refasr", err=True)
        raise typer.Exit(2)
    if getattr(asr, "paid", False):
        secs = sf.info(str(DEFAULT_REFASR_CLIP)).duration
        mod = importlib.import_module(type(asr).__module__)
        typer.echo(f"reference ASR {asr.name} is a paid API: projected "
                   f"{mod.projected_usd(secs):.4f} USD for {secs:.1f} s of calibration audio")
        if not yes:
            typer.echo("Re-run with --yes to approve. No paid API was called.")
            raise typer.Exit(1)
    return asr


if __name__ == "__main__":
    _app = typer.Typer(no_args_is_help=True, add_completion=False)

    @_app.callback()
    def _main() -> None:
        """duplexbench calibration commands."""

    register(_app)
    _app()
