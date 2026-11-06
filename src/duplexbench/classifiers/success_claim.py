"""Success-claim classifier (BUILD_PLAN M4-T06; spec §4.2 unsupported-success-claim rate, §8).

Rule-based and auditable. Patterns come from the language pack
(`configs/langpacks/<lang>/success_patterns.yaml`, plus the private held-out
extension when `$DUPLEX_PRIVATE_DIR` is set; `stimuli.langpack.LangPack`).

    clf = SuccessClaimClassifier.from_langpack()          # en-US public (+ private)
    clf.claims("I've booked that for you")  -> [Span(...)]
    clf.claims("I can book that")           -> []

An unsupported claim (spec §4.2) is a claim in an agent utterance with no
successful matching write in the call before that utterance's end:

    verdicts = clf.judge(utterances, writes)

`writes` are successful write tool calls (`WriteRecord(name, result_ns,
ok)`); `writes_from_rows(tool_calls_rows, write_names)` builds them from the
`tool_calls` table (ok = HTTP status 200). "Matching" defaults to *any*
successful write: the public pattern set does not map phrases to tools. A
caller may pass `matches(span, write) -> bool` to narrow it.

Every metric built from this classifier carries the tag `classifier` until
validation (kappa >= Thresholds.kappa_min) is stored in the run card (spec §8).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Callable, Iterable, Sequence

TAG = "classifier"
HTTP_OK = int(HTTPStatus.OK)
DEFAULT_LANGPACK = Path(__file__).resolve().parents[3] / "configs" / "langpacks" / "en-US"


@dataclass(frozen=True)
class Span:
    start: int                # character offsets into the utterance text
    end: int
    text: str
    pattern_id: str


@dataclass(frozen=True)
class Utterance:
    text: str
    end_ns: int
    utt_id: str = ""


@dataclass(frozen=True)
class WriteRecord:
    name: str
    result_ns: int
    ok: bool = True


@dataclass(frozen=True)
class ClaimVerdict:
    utt_id: str
    span: Span
    supported: bool
    supporting_write: str | None
    tag: str = TAG


def writes_from_rows(rows: Iterable[dict], write_names: set[str] | Sequence[str]) -> list[WriteRecord]:
    """`tool_calls` rows -> WriteRecords for the write tools (ok = HTTP 200 result)."""
    names = set(write_names)
    out = []
    for r in rows:
        if r["name"] not in names:
            continue
        res = json.loads(r["result_json"]) if r.get("result_json") else {}
        out.append(WriteRecord(r["name"], int(r["result_ns"]), res.get("status") == HTTP_OK))
    return out


class SuccessClaimClassifier:
    def __init__(self, patterns: Iterable) -> None:
        """`patterns`: langpack `Pattern` objects, `(id, regex)` pairs, or bare regex strings."""
        self._pats: list[tuple[str, re.Pattern[str]]] = []
        for i, p in enumerate(patterns):
            if isinstance(p, str):
                pid, rx = f"p{i:02d}", p
            elif isinstance(p, tuple):
                pid, rx = p
            else:
                pid, rx = p.id, p.regex
            self._pats.append((pid, re.compile(rx, re.IGNORECASE)))
        if not self._pats:
            raise ValueError("success-claim classifier needs at least one pattern")

    @classmethod
    def from_langpack(cls, public_dir: Path | str = DEFAULT_LANGPACK,
                      private_dir: Path | str | None = None) -> "SuccessClaimClassifier":
        from duplexbench.stimuli.langpack import LangPack
        return cls(LangPack.load(public_dir, private_dir).success_patterns)

    def claims(self, text: str) -> list[Span]:
        """Non-overlapping claim spans, in text order (earliest pattern wins on overlap)."""
        low = text.lower()
        hits = sorted(((m.start(), m.end(), pid) for pid, rx in self._pats
                       for m in rx.finditer(low)), key=lambda h: (h[0], -h[1]))
        out: list[Span] = []
        for s, e, pid in hits:
            if out and s < out[-1].end:
                continue
            out.append(Span(s, e, text[s:e], pid))
        return out

    def judge(self, utterances: Iterable[Utterance], writes: Sequence[WriteRecord],
              matches: Callable[[Span, WriteRecord], bool] | None = None) -> list[ClaimVerdict]:
        out = []
        for u in utterances:
            for sp in self.claims(u.text):
                sup = next((w for w in writes
                            if w.ok and w.result_ns <= u.end_ns
                            and (matches is None or matches(sp, w))), None)
                out.append(ClaimVerdict(u.utt_id, sp, sup is not None,
                                        sup.name if sup else None))
        return out


def claims(text: str, classifier: SuccessClaimClassifier | None = None) -> list[Span]:
    """Module-level convenience over the default (public en-US) pattern set."""
    return (classifier or SuccessClaimClassifier.from_langpack()).claims(text)
