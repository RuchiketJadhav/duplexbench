"""`duplexbench refasr-bench [--yes]` (BUILD_PLAN M4a-T02; spec §4.3).

Without --yes it prints what it would download and refuses (exit 1): model
downloads cost bandwidth and disk and need approval. With --yes it loads each
candidate whose packages are installed (it never pip-installs), measures WER on
`tests/fixtures/floor-mini/` and the CPU real-time factor where the fixture
exists, and rewrites docs/refasr-candidates.md. Partial-latency p90 stays
unmeasured: every wrapper buffers until a human picks a model.

floor-mini layout [Assumption until M2-T04 lands]: `<id>.wav` (8 kHz int16 mono)
beside `<id>.txt` (the reference text).
"""
from __future__ import annotations

import re
from pathlib import Path

import typer

FLOOR_MINI = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "floor-mini"
_NS_PER_S = 1_000_000_000
_LINE_RATE = 8000


def _words(s: str) -> list[str]:
    return re.sub(r"[^\w\s']", " ", s.lower()).split()


def wer(ref: str, hyp: str) -> tuple[int, int]:
    """(edit distance, reference word count) on lower-cased, punctuation-free words."""
    r, h = _words(ref), _words(hyp)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
            prev, d[j] = d[j], cur
    return d[len(h)], len(r)


def _items(root: Path) -> list[tuple[Path, str]]:
    return [(w, w.with_suffix(".txt").read_text(encoding="utf-8").strip())
            for w in sorted(root.glob("*.wav")) if w.with_suffix(".txt").is_file()]


def measure(asr, items: list[tuple[Path, str]]) -> dict:
    import time

    import soundfile as sf

    from duplexbench.refasr.engine import text_of

    errs = n = 0
    proc_ns = audio_ns = 0
    for wav, ref in items:
        pcm, sr = sf.read(str(wav), dtype="int16")
        if sr != _LINE_RATE:
            return {"wer": f"fixture {wav.name} is {sr} Hz, expected 8 kHz"}
        t0 = time.perf_counter_ns()
        hyp = text_of(asr.transcribe(pcm))
        proc_ns += time.perf_counter_ns() - t0
        audio_ns += len(pcm) * _NS_PER_S // sr
        e, k = wer(ref, hyp)
        errs, n = errs + e, n + k
    return {"wer": errs / n if n else None, "rtf": proc_ns / audio_ns if audio_ns else None}


def register(app: typer.Typer) -> None:
    @app.command("refasr-bench")
    def refasr_bench(
        yes: bool = typer.Option(False, "--yes", help="approve model downloads (bandwidth/disk)"),
        out: Path = typer.Option(None, help="table path (default docs/refasr-candidates.md)"),
        fixtures: Path = typer.Option(FLOOR_MINI, help="floor-mini fixture directory"),
    ) -> None:
        """Measure reference-ASR candidates and write docs/refasr-candidates.md (M4a-T02)."""
        import importlib

        from duplexbench.refasr import candidates_table as ct
        from duplexbench.refasr.candidates import CandidateUnavailable

        avail = {c.name: ct.probe(c) for c in ct.CANDIDATES}
        if not yes:
            typer.echo("refasr-bench downloads candidate models (bandwidth and disk); "
                       "approval required. Would load:")
            for c in ct.CANDIDATES:
                state = "packages importable" if avail[c.name] is None else f"blocked: {avail[c.name]}"
                typer.echo(f"  {c.name}: {c.model} ({c.size}) - {state}")
            typer.echo("Re-run with --yes to approve. Nothing was downloaded.")
            raise typer.Exit(1)
        items = _items(fixtures) if fixtures.is_dir() else []
        meas: dict[str, dict] = {}
        for c in ct.CANDIDATES:
            if avail[c.name] is not None:
                continue
            mod = importlib.import_module(f"duplexbench.refasr.candidates.{c.module}")
            try:
                asr = getattr(mod, c.cls)(allow_download=True)
            except CandidateUnavailable as exc:
                avail[c.name] = str(exc)
                continue
            if not items:
                meas[c.name] = {"wer": f"floor-mini fixture missing at {fixtures} (M2-T04)",
                                "rtf": "needs floor-mini fixture"}
                continue
            meas[c.name] = measure(asr, items)
            typer.echo(f"{c.name}: {meas[c.name]}")
        path = ct.write(out or ct.DOC_PATH, meas, avail)
        typer.echo(f"wrote {path}")
