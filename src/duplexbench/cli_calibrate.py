"""`duplexbench calibrate` (spec §1.4; BUILD_PLAN M1-T14). Wired into cli.py via `register(app)`.

    uv run duplexbench calibrate [--config configs/runs/calibrate.yaml] [--calls N] [--virtual]

Prints the calibration table and writes results/calibration-<ts>.json; exits 2 on
fail (a Windows laptop may fail the event-loop lag gate; that is a report, not a bug).
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
    ) -> None:
        """Calibrate the harness against the mock agent (spec §1.4)."""
        from duplexbench import _loop
        from duplexbench.calibrate import CalibConfig, calibrate as run, format_table

        cfg = CalibConfig.load(config, **({"calls": calls} if calls else {}))
        res = _loop.run(run(cfg, virtual=virtual, out_dir=out_dir))
        typer.echo(format_table(res))
        typer.echo(f"written: {res['path']}")
        if not res["pass"]:
            raise typer.Exit(code=2)


if __name__ == "__main__":
    _app = typer.Typer(no_args_is_help=True, add_completion=False)

    @_app.callback()
    def _main() -> None:
        """duplexbench calibration commands."""

    register(_app)
    _app()
