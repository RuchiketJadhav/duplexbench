"""duplexbench command line (Typer). One function per command, no shared state.

Add a command by appending a function decorated with `@app.command()`; import
what it needs inside the function so a broken optional module cannot break
`doctor`.
"""
from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def _main() -> None:
    """Dograh Duplex Bench harness."""


@app.command()
def doctor() -> None:
    """Print the environment facts every run depends on (spec §1.1, §10 item 1, §11)."""
    import os
    import platform
    import sys
    from pathlib import Path

    from duplexbench.clock import clock_info
    from duplexbench.thresholds import DEFAULT_PATH, Thresholds

    typer.echo(f"python: {sys.version.split()[0]} ({platform.platform()})")
    typer.echo(f"clock: {clock_info()}")
    priv = os.environ.get("DUPLEX_PRIVATE_DIR", "").strip()
    if not priv:
        typer.echo("DUPLEX_PRIVATE_DIR: not set")
    else:
        typer.echo(f"DUPLEX_PRIVATE_DIR: {priv} (exists: {Path(priv).is_dir()})")
    src = DEFAULT_PATH if DEFAULT_PATH.exists() else "defaults (no configs/thresholds.yaml)"
    typer.echo(f"thresholds: {src}")
    typer.echo(f"thresholds hash: {Thresholds.load().hash()}")


def _register_optional(module: str) -> None:
    """Attach a milestone's commands; a broken module never breaks `doctor`."""
    import importlib

    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == module:
            return  # milestone not built yet
        typer.echo(f"warning: {module} not loaded: {exc!r}", err=True)
        return
    except Exception as exc:  # pragma: no cover - only on a broken install
        typer.echo(f"warning: {module} not loaded: {exc!r}", err=True)
        return
    mod.register(app)


for _m in (
    "duplexbench.cli_calibrate",
    "duplexbench.cli_stimuli",
    "duplexbench.cli_tools",
    "duplexbench.cli_report",
    "duplexbench.cli_score",
    "duplexbench.cli_run",
    "duplexbench.cli_load",
    "duplexbench.cli_refasr",
):
    _register_optional(_m)
