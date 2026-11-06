"""Language pack: overlay token lists, pattern sets, normalizer rules (BUILD_PLAN M2-T02;
spec §3.2 overlay taxonomy, §8 classifiers, §11 data governance).

    lp = LangPack.load(Path("configs/langpacks/en-US"))      # public only
    lp.ycmd_tokens   # ('no', 'stop', 'wait', 'hold on', 'hang on', 'one second', 'sorry?')
    lp.wait_class    # ('wait', 'hold on', 'hang on', 'one second')
    lp.hbc_tokens    # ('mm-hm', 'uh-huh', 'yeah', 'right', 'okay', 'got it', 'sure')
    lp.hash          # sha256 over the parsed public (+ private) content

Private extensions (spec §11, eng review D5) are held-out variants that live in a
separate private repo mounted at `$DUPLEX_PRIVATE_DIR`. They are merged ONLY when
that variable is set: `private_dir` defaults to `$DUPLEX_PRIVATE_DIR/langpacks/<lang>`,
and an explicit `private_dir` is ignored (`private_ignored=True`) when the variable
is unset. A private dir inside the harness repo is refused. Private files use the
same five filenames; each may hold any subset of the keys. Lists are appended
(duplicates dropped), pattern ids must be unique across public and private.

The hash is computed over the parsed content (not file bytes), so line endings
and comments do not change it; any token, pattern, version or rule does.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from duplexbench.storage import canonical_json

PRIVATE_ENV = "DUPLEX_PRIVATE_DIR"
FILES = ("ycmd_tokens.yaml", "hbc_tokens.yaml", "success_patterns.yaml",
         "recovery_patterns.yaml", "normalizer_rules.yaml")
_REPO_ROOT = Path(__file__).resolve().parents[3]


class LangPackError(ValueError):
    pass


@dataclass(frozen=True)
class Pattern:
    id: str
    regex: str
    note: str = ""
    private: bool = False

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.regex, re.IGNORECASE)


@dataclass(frozen=True)
class LangPack:
    lang: str
    ycmd_tokens: tuple[str, ...]
    wait_class: tuple[str, ...]
    hbc_tokens: tuple[str, ...]
    success_patterns: tuple[Pattern, ...]
    recovery_patterns: tuple[Pattern, ...]
    normalizer_rules: dict[str, Any]
    hash: str
    public_dir: Path
    private_dir: Path | None = None          # the dir actually merged, or None
    private_ignored: bool = False            # a private dir was given but the env var is unset
    public_hash: str = ""
    sources: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, public_dir: Path | str, private_dir: Path | str | None = None) -> "LangPack":
        pub = Path(public_dir)
        if not pub.is_dir():
            raise LangPackError(f"language pack not found: {pub}")
        public = {name: _read(pub / name, required=True) for name in FILES}
        lang = public["ycmd_tokens.yaml"].get("lang") or pub.name

        env = os.environ.get(PRIVATE_ENV, "").strip()
        priv: Path | None = None
        ignored = False
        if private_dir is not None and not env:
            ignored = True
        elif env:
            priv = Path(private_dir) if private_dir is not None else Path(env) / "langpacks" / lang
            if _is_under(priv, _REPO_ROOT):
                raise LangPackError(f"private language pack inside the harness repo: {priv} "
                                    f"(spec §11: private files live under ${PRIVATE_ENV})")
            if not priv.is_dir():
                priv = None                 # env set, but this pack has no private extension
        private = ({name: _read(priv / name, required=False) for name in FILES}
                   if priv is not None else {})

        ycmd = _merge_tokens(public, private, "ycmd_tokens.yaml", "tokens")
        wait = _merge_tokens(public, private, "ycmd_tokens.yaml", "wait_class")
        missing = [w for w in wait if w not in ycmd]
        if missing:
            raise LangPackError(f"wait_class items not in Y-CMD tokens: {missing}")
        hbc = _merge_tokens(public, private, "hbc_tokens.yaml", "tokens")
        succ = _merge_patterns(public, private, "success_patterns.yaml")
        reco = _merge_patterns(public, private, "recovery_patterns.yaml")
        rules = dict(public["normalizer_rules.yaml"])
        rules.update(private.get("normalizer_rules.yaml") or {})

        pub_hash = _hash({"public": public, "private": None})
        full_hash = (_hash({"public": public, "private": {k: private[k] for k in sorted(private)}})
                     if priv is not None and any(private.values()) else pub_hash)
        sources = {"public": [str(pub / n) for n in FILES],
                   "private": [str(priv / n) for n in FILES if private.get(n)] if priv else []}
        return cls(lang=lang, ycmd_tokens=ycmd, wait_class=wait, hbc_tokens=hbc,
                   success_patterns=succ, recovery_patterns=reco, normalizer_rules=rules,
                   hash=full_hash, public_dir=pub, private_dir=priv,
                   private_ignored=ignored, public_hash=pub_hash, sources=sources)


# -- helpers -----------------------------------------------------------------
def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _read(p: Path, *, required: bool) -> dict[str, Any]:
    if not p.is_file():
        if required:
            raise LangPackError(f"language pack file missing: {p}")
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise LangPackError(f"{p}: expected a mapping")
    return data


def _norm(tok: Any) -> str:
    if not isinstance(tok, str):
        raise LangPackError(f"token must be a string, got {tok!r} (quote YAML words like 'no')")
    return " ".join(tok.lower().split())


def _merge_tokens(public: dict, private: dict, fname: str, key: str) -> tuple[str, ...]:
    out: list[str] = []
    for src in (public.get(fname) or {}, private.get(fname) or {}):
        for t in src.get(key) or []:
            n = _norm(t)
            if n and n not in out:
                out.append(n)
    return tuple(out)


def _merge_patterns(public: dict, private: dict, fname: str) -> tuple[Pattern, ...]:
    out: list[Pattern] = []
    seen: set[str] = set()
    for is_priv, src in ((False, public.get(fname) or {}), (True, private.get(fname) or {})):
        for item in src.get("patterns") or []:
            pid, rx = str(item.get("id", "")), item.get("regex")
            if not pid or not isinstance(rx, str):
                raise LangPackError(f"{fname}: pattern needs id and regex: {item!r}")
            if pid in seen:
                raise LangPackError(f"{fname}: duplicate pattern id {pid!r}")
            try:
                re.compile(rx, re.IGNORECASE)
            except re.error as e:
                raise LangPackError(f"{fname}/{pid}: bad regex: {e}") from e
            seen.add(pid)
            out.append(Pattern(id=pid, regex=rx, note=str(item.get("note", "")), private=is_priv))
    return tuple(out)


def _hash(content: dict) -> str:
    return hashlib.sha256(canonical_json(content).encode()).hexdigest()
