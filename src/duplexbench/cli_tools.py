"""Human-input and early-report commands (BUILD_PLAN M2b-T01..T03, M2-T11).

    from duplexbench.cli_tools import register
    register(app)        # in cli.py

adds `scenario new`, `scenario validate`, `preregister`, `label`, `report-text`.
Imports happen inside each command so a broken optional module cannot break the CLI.
"""
from __future__ import annotations

from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    scenario = typer.Typer(no_args_is_help=True, help="Scenario template and validation (M2b-T01).")
    app.add_typer(scenario, name="scenario")

    @scenario.command("new")
    def scenario_new(domain: str = typer.Option(..., help="domain backend name"),
                     track: str = typer.Option(..., help="A | C | D"),
                     split: str = typer.Option("public-dev", help="public-dev | private-test"),
                     scenario_id: str = typer.Option(None, "--id", help="scenario id (default: TODO)"),
                     out: Path = typer.Option(None, help="output YAML path"),
                     force: bool = typer.Option(False, help="overwrite an existing file")) -> None:
        """Write a commented scenario YAML with every field (spec §3.1)."""
        from duplexbench.preregister import REPO_ROOT, private_dir
        from duplexbench.scenarios.loader import _is_under
        from duplexbench.scenarios.template import render_template

        text = render_template(domain, track, split, scenario_id=scenario_id)
        name = f"{scenario_id or f'{domain}_{track.lower()}_new'}.yaml"
        if out is None:
            if split == "private-test":
                priv = private_dir()
                if priv is None:
                    typer.echo("private-test templates go under $DUPLEX_PRIVATE_DIR, which is not set",
                               err=True)
                    raise typer.Exit(2)
                out = priv / "scenarios" / name
            else:
                out = REPO_ROOT / "scenarios" / "public-dev" / name
        if split == "private-test" and _is_under(out, REPO_ROOT):
            typer.echo(f"refusing to write a private-test scenario inside the repo: {out}", err=True)
            raise typer.Exit(2)
        if out.exists() and not force:
            typer.echo(f"{out} exists; pass --force to overwrite", err=True)
            raise typer.Exit(1)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}")

    @scenario.command("validate")
    def scenario_validate(directory: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
        """Validate every scenario YAML under DIRECTORY and print a table; exit 1 on any failure."""
        from duplexbench.scenarios.template import format_table, validate_dir

        results = validate_dir(directory)
        typer.echo(format_table(results, root=directory), nl=False)
        if any(not r.ok for r in results):
            raise typer.Exit(1)

    @app.command("preregister")
    def preregister_cmd(results_dir: Path = typer.Option(None, help="default: results/"),
                        pack_dir: Path = typer.Option(None, help="default: scenarios/public-dev"),
                        check: bool = typer.Option(False, help="only diff against the latest record")) -> None:
        """Hash systems, pack, langpack, thresholds, private tree and commit; write a record (M2b-T02)."""
        from duplexbench import preregister as pr

        rd = results_dir or pr.RESULTS_DIR
        hashes = pr.collect_hashes(pack_dir=pack_dir or pr.PACK_DIR)
        for k, v in hashes.items():
            typer.echo(f"{k}: {pr.show_value(k, v)}")
        diffs = pr.check_against_latest(hashes, rd)
        for d in diffs:
            typer.echo(f"DIFF {d}")
        typer.echo(pr.refusal_message(diffs) or "matches the latest pre-registration")
        if check:
            raise typer.Exit(1 if diffs else 0)
        typer.echo(f"wrote {pr.write_record(hashes, rd)}")

    @app.command("label")
    def label_cmd(manifest: Path = typer.Option(..., exists=True, dir_okay=False),
                  set_name: str = typer.Option(..., "--set", help="respond_resume | success_claim | recovery_request"),
                  labeler: str = typer.Option(..., help="labeler id"),
                  out_dir: Path = typer.Option(None, help="default: results/labels")) -> None:
        """Label clips from a JSONL manifest; resumable, two labelers per item (M2b-T03)."""
        from duplexbench.labeling import LABELS_DIR, run_session

        run_session(manifest, set_name, labeler, out_dir=out_dir or LABELS_DIR)

    @app.command("report-text")
    def report_text(run_id: str,
                    db: Path = typer.Option(None, help="default: results/<run_id>/events.sqlite")) -> None:
        """Print the early fixed-width text report of a run's observations (M2-T11)."""
        from duplexbench.preregister import RESULTS_DIR
        from duplexbench.report.text import render

        path = db or RESULTS_DIR / run_id / "events.sqlite"
        if not path.is_file():
            typer.echo(f"no store at {path}", err=True)
            raise typer.Exit(2)
        typer.echo(render(path, run_id), nl=False)
