"""Canonical mock-database state, snapshot hashing, and the shared Backend base (BUILD_PLAN M4-T02; spec §4.2).

Canonicalization is field-typed by key name, because the spoken-text
normalizer (`metrics.normalize.normalize_entity`) is built for ASR text, not
structured values (it drops the "(555)" area code of "(555) 123-4567"):

    key token is   phone        -> digits only; a leading US "1" on 11 digits dropped
                   email        -> stripped, lower-cased
                   date / dob   -> ISO 8601 (YYYY-MM-DD, or YYYY-MM-DDTHH:MM for datetimes)
                   amount / price / total / balance / cap
                                -> decimal string with 2 places ("42.10")
    any str                     -> stripped
    dict                        -> keys sorted; tuple/list -> list

`expected_equals(state, expected)` canonicalizes both and compares
`expected` as a recursive *subset* of `state` on dicts (a scenario's
`expected_state` lists only the rows it cares about; seed rows it does not
mention may exist); lists and scalars compare exactly.

A Backend owns one domain's mutable state. `call(name, args)` dispatches to
`_t_<name>`, canonicalizes arguments first, raises `PolicyError` for a
forbidden write and `ToolArgError` for bad arguments, and returns a dict
carrying `result_anchors` (the scenario's declared anchors for that tool when
bound, otherwise the backend's own anchor strings, e.g. the slot time).
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable, Literal

from duplexbench.storage import canonical_json

_PHONE_KEYS = {"phone"}
_EMAIL_KEYS = {"email"}
_DATE_KEYS = {"date", "dob", "start", "when", "due"}
_AMOUNT_KEYS = {"amount", "price", "total", "balance", "cap"}
_US_COUNTRY = "1"
_US_LEN = 10
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d %B %Y", "%B %d %Y", "%B %d, %Y", "%b %d %Y",
                 "%b %d, %Y")
_DT_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p",
               "%Y-%m-%d %I %p")


class PolicyError(Exception):
    """A write the domain policy forbids (spec §4.2 policy-violation / auth-bypass)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ToolArgError(ValueError):
    """Unknown tool, missing/invalid argument, or unknown record id."""


@dataclass(frozen=True)
class ToolSchema:
    name: str
    kind: Literal["read", "write"]
    description: str
    parameters: dict = field(default_factory=dict)    # JSON schema (type: object)

    def to_dict(self) -> dict:
        return asdict(self)


def obj_schema(required: dict[str, str], optional: dict[str, str] | None = None) -> dict:
    props = {k: {"type": t} for k, t in {**required, **(optional or {})}.items()}
    return {"type": "object", "properties": props, "required": sorted(required)}


# -- canonicalizers ------------------------------------------------------------
def canon_phone(v: Any) -> str:
    d = re.sub(r"\D", "", str(v))
    if len(d) == _US_LEN + 1 and d.startswith(_US_COUNTRY):
        d = d[1:]
    return d


def canon_email(v: Any) -> str:
    return str(v).strip().lower()


def canon_date(v: Any) -> str:
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%dT%H:%M")
    if isinstance(v, date):
        return v.isoformat()
    s = " ".join(str(v).strip().split())
    for f in _DT_FORMATS:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return s


def canon_amount(v: Any) -> str:
    s = str(v).strip().replace("$", "").replace(",", "")
    try:
        return str(Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return s


def _kind_of(key: str) -> Callable[[Any], Any] | None:
    toks = set(re.split(r"[_\W]+", key.lower()))
    if toks & _PHONE_KEYS:
        return canon_phone
    if toks & _EMAIL_KEYS:
        return canon_email
    if toks & _AMOUNT_KEYS:
        return canon_amount
    if toks & _DATE_KEYS:
        return canon_date
    return None


def canonical_value(key: str, v: Any) -> Any:
    if isinstance(v, dict):
        return canonicalize(v)
    if isinstance(v, (list, tuple)):
        return [canonical_value(key, x) for x in v]
    if v is None or isinstance(v, bool):
        return v
    fn = _kind_of(key)
    if fn is not None:
        return fn(v)
    return v.strip() if isinstance(v, str) else v


def canonicalize(state: dict) -> dict:
    return {str(k): canonical_value(str(k), state[k]) for k in sorted(state, key=str)}


def snapshot_hash(state: dict) -> str:
    return hashlib.sha256(canonical_json(canonicalize(state)).encode()).hexdigest()


def _subset(exp: Any, got: Any) -> bool:
    if isinstance(exp, dict):
        return isinstance(got, dict) and all(k in got and _subset(v, got[k])
                                             for k, v in exp.items())
    return exp == got


def expected_equals(state: dict, expected: dict) -> bool:
    return _subset(canonicalize(expected), canonicalize(state))


# -- Backend base --------------------------------------------------------------
class Backend:
    """One domain's deterministic mock database (spec §4.2)."""

    domain: str = ""
    SEED: dict = {}

    def __init__(self, seed_state: dict | None = None, policy: dict | None = None,
                 anchors: dict[str, list[str]] | None = None) -> None:
        self.policy: dict = dict(policy or {})
        self.anchors: dict[str, list[str]] = dict(anchors or {})
        self.reset(seed_state)

    # -- public contract -------------------------------------------------
    def tools(self) -> list[ToolSchema]:
        raise NotImplementedError

    def tool(self, name: str) -> ToolSchema:
        for t in self.tools():
            if t.name == name:
                return t
        raise ToolArgError(f"unknown tool {name!r} for {self.domain}")

    def bind_anchors(self, anchors: dict[str, list[str]]) -> None:
        self.anchors = dict(anchors)

    def call(self, name: str, args: dict) -> dict:
        schema = self.tool(name)
        args = canonicalize(dict(args or {}))
        missing = [k for k in schema.parameters.get("required", []) if k not in args]
        if missing:
            raise ToolArgError(f"{name}: missing {missing}")
        before = copy.deepcopy(self._db)
        try:
            out = getattr(self, f"_t_{name}")(**args)
        except TypeError as e:
            self._db = before
            raise ToolArgError(f"{name}: {e}") from None
        except (PolicyError, ToolArgError):
            self._db = before                 # a refused write never changes state
            raise
        own = out.pop("_anchors", [])
        result = dict(out)
        result["result_anchors"] = list(self.anchors.get(name) or own)
        return result

    def state(self) -> dict:
        return canonicalize(self._db)

    def snapshot_hash(self) -> str:
        return snapshot_hash(self._db)

    def reset(self, seed_state: dict | None = None) -> None:
        self._db = canonicalize(copy.deepcopy(seed_state if seed_state is not None
                                              else self.SEED))
        self.verified: set[str] = set()

    # -- helpers for subclasses --------------------------------------------
    def _table(self, name: str) -> dict:
        return self._db.setdefault(name, {})

    def _row(self, table: str, key: str) -> dict:
        row = self._table(table).get(str(key))
        if row is None:
            raise ToolArgError(f"unknown {table} id {key!r}")
        return row

    def _next_id(self, table: str, prefix: str) -> str:
        return f"{prefix}{len(self._table(table)) + 1:04d}"
