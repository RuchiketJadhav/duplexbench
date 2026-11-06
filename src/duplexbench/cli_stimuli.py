"""`stimuli` CLI commands (BUILD_PLAN M2-T04). Wire with `cli_stimuli.register(app)`.

    duplexbench stimuli generate scenarios/public-dev --vendor fake --yes
    duplexbench stimuli generate scenarios/public-dev --vendor <name> --usd-per-1k-chars 0.03

Prints the projected cost (uncached characters x price) and refuses without
`--yes` (CLAUDE.md §1). Only the `fake` vendor has a client: a real vendor is a
human decision (CLAUDE.md §6 item 2) and its client is not built yet, so a real
vendor with `--yes` stops with exit code 2 before any network call.
"""
from __future__ import annotations

from pathlib import Path

import typer

EXIT_REFUSED = 1
EXIT_BLOCKED = 2


def register(app: typer.Typer) -> None:
    stimuli = typer.Typer(no_args_is_help=True, add_completion=False,
                          help="Stimulus generation (approve-first).")

    @stimuli.command("generate")
    def generate(
        pack_dir: Path = typer.Argument(..., help="scenario pack directory"),
        yes: bool = typer.Option(False, "--yes", help="approve the projected spend"),
        vendor: str = typer.Option("unset", "--vendor", help="TTS vendor; only 'fake' is wired"),
        usd_per_1k_chars: float | None = typer.Option(None, "--usd-per-1k-chars",
                                                      help="price from docs/providers.md"),
        out: Path | None = typer.Option(None, "--out", help="clip cache dir "
                                        "(default <pack_dir>/audio/tts)"),
    ) -> None:
        """Synthesize every step, resumption and overlay text of a pack (cached by sha256)."""
        from duplexbench.scenarios.loader import load_pack
        from duplexbench.stimuli.tts import cache_key, pack_items, project_usd, synthesize

        pack = load_pack(pack_dir)
        out_dir = out or Path(pack_dir) / "audio" / "tts"
        items = pack_items(pack.scenarios)
        todo = [i for i in items
                if not (out_dir / f"{cache_key(vendor, i.voice, i.rate, i.text)}.wav").is_file()]
        price = 0.0 if vendor == "fake" else usd_per_1k_chars
        if price is None:
            typer.echo("price unknown: pass --usd-per-1k-chars (source: docs/providers.md)")
            raise typer.Exit(EXIT_BLOCKED)
        chars = sum(len(i.text) for i in todo)
        usd = project_usd((i.text for i in todo), price)
        typer.echo(f"vendor: {vendor}")
        typer.echo(f"items: {len(items)} ({len(items) - len(todo)} cached, {len(todo)} to synthesize)")
        typer.echo(f"characters: {chars}")
        typer.echo(f"projected cost: ${usd:.4f} at ${price}/1k chars")
        if not yes:
            typer.echo("refusing to synthesize without --yes")
            raise typer.Exit(EXIT_REFUSED)
        if vendor != "fake":
            typer.echo(f"no TTS client for vendor {vendor!r}: the vendor is not chosen "
                       "(docs/providers.md, CLAUDE.md §6 item 2); nothing was sent")
            raise typer.Exit(EXIT_BLOCKED)
        from duplexbench.stimuli.fake_tts import FakeTTS
        client = FakeTTS()
        for i in todo:
            synthesize(i.text, i.voice, i.rate, client, cache_dir=out_dir)
        typer.echo(f"wrote {len(todo)} clips to {out_dir}")

    app.add_typer(stimuli, name="stimuli")
