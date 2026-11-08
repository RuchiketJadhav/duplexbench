"""`stimuli` CLI commands (BUILD_PLAN M2-T04). Wire with `cli_stimuli.register(app)`.

    duplexbench stimuli generate scenarios/public-dev --vendor fake --yes
    duplexbench stimuli generate scenarios/public-dev --vendor fish --model drama-3         --voices-file configs/voices/fish.yaml --default-voice delia [--yes]
    duplexbench stimuli generate scenarios/public-dev --vendor <name> --usd-per-1k-chars 0.03

Prints the projected cost (uncached characters x price) and refuses without
`--yes` (CLAUDE.md §1). Clients exist for `fake` (FakeTTS, $0) and `fish`
(FishTTS, decision B13; price from configs/prices/fish_audio.yaml via
`fish_tts.usd_per_1k_chars(model)` unless `--usd-per-1k-chars` is passed). Any
other vendor (or none) with `--yes` stops with exit code 2 before any network
call. For fish, an unknown model, an unresolvable voice or a missing
FISH_API_KEY also stop with exit code 2 before any request; a vendor error
mid-run exits 3 (clips already written stay cached).
The cache key uses the client identity (`stimuli.tts.client_id`): "fake" or "fish/<model>".

Write-back (fix-31, stimuli/writeback.py): after synthesis every scripted scenario's
YAML gets `clip: audio/tts/<sha>.wav` (relative to the YAML) and, for steps,
`speech_onset_ms` / `speech_end_ms`; step and Y-CMD overlay resumption lines are
copied to `<clip stem>_resumption.wav`. Comments, key order and line endings are
kept; a second run changes nothing. Clips already set outside the TTS dir are kept.
`--no-write-back` restores the synthesize-only behaviour. Two items that share a clip
but need different resumption lines stop with exit code 2 before any synthesis.
Write-back changes the pack hash: pre-register after generating.
"""
from __future__ import annotations

from pathlib import Path

import typer

EXIT_REFUSED = 1
EXIT_BLOCKED = 2
EXIT_TTS_ERROR = 3


def _default_fish_factory(*, model: str, voices: dict[str, str], default_voice: str | None):
    from duplexbench.stimuli.fish_tts import FishTTS
    return FishTTS(model=model, voices=voices, default_voice=default_voice)


# Test hook: replace to build the fish client differently (tests inject an httpx MockTransport).
fish_client_factory = _default_fish_factory


def register(app: typer.Typer) -> None:
    stimuli = typer.Typer(no_args_is_help=True, add_completion=False,
                          help="Stimulus generation (approve-first).")

    @stimuli.command("generate")
    def generate(
        pack_dir: Path = typer.Argument(..., help="scenario pack directory"),
        yes: bool = typer.Option(False, "--yes", help="approve the projected spend"),
        vendor: str = typer.Option("unset", "--vendor", help="TTS vendor: fish or fake"),
        model: str = typer.Option("drama-3", "--model", help="fish model header value"),
        voices_file: Path | None = typer.Option(None, "--voices-file", help="fish voice map "
                                                "(default configs/voices/fish.yaml)"),
        default_voice: str | None = typer.Option(None, "--default-voice", help="fish voice "
                                                 "used for names not in the voice map"),
        usd_per_1k_chars: float | None = typer.Option(None, "--usd-per-1k-chars",
                                                      help="price from docs/providers.md"),
        out: Path | None = typer.Option(None, "--out", help="clip cache dir "
                                        "(default <pack_dir>/audio/tts)"),
        write_back: bool = typer.Option(True, "--write-back/--no-write-back",
                                        help="write clip paths, onset/end and resumption "
                                        "clips back into the scenario YAML"),
    ) -> None:
        """Synthesize every step, resumption and overlay text of a pack (cached by sha256)."""
        from duplexbench.scenarios.loader import load_pack
        from duplexbench.stimuli.tts import cache_key, pack_items, project_usd, synthesize

        if vendor == "fish":
            from duplexbench.stimuli.fish_tts import KNOWN_MODELS
            if model not in KNOWN_MODELS:
                typer.echo(f"unknown fish model {model!r}; known: {sorted(KNOWN_MODELS)}")
                raise typer.Exit(EXIT_BLOCKED)
        pack = load_pack(pack_dir, check_tools=False)    # audio only; tool names are irrelevant
        out_dir = out or Path(pack_dir) / "audio" / "tts"
        items = pack_items(pack.scenarios)
        cid = f"fish/{model}" if vendor == "fish" else vendor     # = stimuli.tts.client_id
        plan = None
        if write_back:
            from duplexbench.stimuli.writeback import conflicts, plan_pack
            plan = plan_pack(pack, client_id=cid, out_dir=out_dir)
            clash = conflicts(plan)
            if clash:
                for c in clash:
                    typer.echo(f"write-back conflict: {c}")
                typer.echo("nothing was sent (or pass --no-write-back)")
                raise typer.Exit(EXIT_BLOCKED)
        todo = [i for i in items
                if not (out_dir / f"{cache_key(cid, i.voice, i.rate, i.text)}.wav").is_file()]
        price = 0.0 if vendor == "fake" else usd_per_1k_chars
        if vendor == "fish" and price is None:
            from duplexbench.stimuli.fish_tts import usd_per_1k_chars as _fish_price
            price = _fish_price(model)
        if price is None:
            typer.echo("price unknown: pass --usd-per-1k-chars (source: docs/providers.md)")
            raise typer.Exit(EXIT_BLOCKED)
        chars = sum(len(i.text) for i in todo)
        usd = project_usd((i.text for i in todo), price)
        typer.echo(f"vendor: {vendor}")
        if vendor == "fish":
            typer.echo(f"model: {model}")
        typer.echo(f"items: {len(items)} ({len(items) - len(todo)} cached, {len(todo)} to synthesize)")
        typer.echo(f"characters: {chars}")
        typer.echo(f"projected cost: ${usd:.4f} at ${price}/1k chars")
        if not yes:
            typer.echo("refusing to synthesize without --yes")
            raise typer.Exit(EXIT_REFUSED)
        if vendor == "fish":
            client = _build_fish(model, voices_file, default_voice, todo)
        elif vendor == "fake":
            from duplexbench.stimuli.fake_tts import FakeTTS
            client = FakeTTS()
        else:
            typer.echo(f"no TTS client for vendor {vendor!r}: the vendor is not chosen "
                       "(docs/providers.md, CLAUDE.md §6 item 2); nothing was sent")
            raise typer.Exit(EXIT_BLOCKED)
        done = 0
        for i in todo:
            try:
                synthesize(i.text, i.voice, i.rate, client, cache_dir=out_dir)
            except RuntimeError as e:          # fish_tts.TTSError (never contains the key)
                typer.echo(f"TTS error after {done} clips: {e}")
                raise typer.Exit(EXIT_TTS_ERROR)
            done += 1
        typer.echo(f"wrote {len(todo)} clips to {out_dir}")
        if plan is not None:
            from duplexbench.stimuli.writeback import apply_plan
            res = apply_plan(plan)
            for w in res.warnings:
                typer.echo(f"warning: {w}")
            if res.kept:
                typer.echo(f"kept {len(res.kept)} existing non-TTS clips: {', '.join(res.kept)}")
            for p in res.resumptions_written:
                typer.echo(f"resumption clip: {p}")
            for y in res.yaml_changed:
                typer.echo(f"wrote back: {y}")
            if any(y.endswith("(redumped)") for y in res.yaml_changed):
                typer.echo("warning: a YAML was re-dumped with PyYAML; its inner comments are lost")
            if not res.yaml_changed and not res.resumptions_written:
                typer.echo("write-back: nothing changed")
            else:
                typer.echo("the pack hash changed: run `duplexbench preregister` before results")

    app.add_typer(stimuli, name="stimuli")


def _build_fish(model: str, voices_file: Path | None, default_voice: str | None, todo):
    """Load the voice map, check every voice resolves, build the client; exit 2 before
    any request on a bad voice map, an unresolvable voice or a missing key."""
    from duplexbench.stimuli.fish_tts import (DEFAULT_VOICES_FILE, TTSUnavailable,
                                              load_voices, resolve_voice)
    try:
        voices = load_voices(voices_file or DEFAULT_VOICES_FILE)
        for v in sorted({i.voice for i in todo}):
            resolve_voice(v, voices, default_voice)
    except ValueError as e:
        typer.echo(f"{e}; nothing was sent")
        raise typer.Exit(EXIT_BLOCKED)
    try:
        return fish_client_factory(model=model, voices=voices, default_voice=default_voice)
    except TTSUnavailable as e:
        typer.echo(f"{e}; nothing was sent")
        raise typer.Exit(EXIT_BLOCKED)
