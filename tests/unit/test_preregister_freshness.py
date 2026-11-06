"""fix-22: stale pre-registration records give specific refusals; newest record wins by timestamp."""
from __future__ import annotations

import json

import pytest

from duplexbench import preregister as pr

BASE = {"systems": {"a": "1111", "b": "2222"}, "pack": "p0", "langpack": "l0",
        "thresholds": "t0", "private_tree": "unmounted", "harness_commit": None}


def _write(d, name, created, hashes):
    (d / name).write_text(json.dumps({"schema": 1, "created_utc": created, "hashes": hashes}),
                          encoding="utf-8")


def test_null_commit_recorded_as_null_and_printed_as_no_commit(tmp_path):
    path = pr.write_record(dict(BASE), tmp_path)
    assert '"harness_commit": null' in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text())["hashes"]["harness_commit"] is None
    assert pr.show_value("harness_commit", None) == "no commit"
    assert pr.diff_hashes({**BASE, "harness_commit": "abc"}, BASE) == \
        ["harness_commit: abc -> no commit"]


def test_legacy_unknown_commit_equals_null():
    assert pr.diff_hashes({**BASE, "harness_commit": "unknown"}, BASE) == []
    assert pr.diff_hashes({**BASE, "harness_commit": "None"}, BASE) == []


def test_stale_record_names_every_group(tmp_path):
    _write(tmp_path, "preregister-20261105T043023877792Z.json", "2026-11-05T04:30:23.877792Z", BASE)
    cur = {"systems": {"a": "9999", "c": "3333"}, "pack": "p1", "langpack": "l1",
           "thresholds": "t1", "private_tree": "x", "harness_commit": "deadbeef"}
    diffs = pr.check_against_latest(cur, tmp_path)
    assert all(d.startswith("preregister-20261105T043023877792Z.json: ") for d in diffs)
    body = [d.split(": ", 1)[1] for d in diffs]
    assert "systems/a: 1111 -> 9999" in body
    assert "systems/b: removed (was 2222)" in body
    assert "systems/c: added 3333" in body
    assert "harness_commit: no commit -> deadbeef" in body
    msg = pr.refusal_message(diffs)
    assert msg == ("pre-registration preregister-20261105T043023877792Z.json is stale: "
                   "systems (3), pack, langpack, thresholds, private, commit differ (8 hashes)")
    assert pr.refusal_message([]) == ""


def test_single_group_refusal(tmp_path):
    _write(tmp_path, "preregister-20261105T043023877792Z.json", "2026-11-05T04:30:23.877792Z", BASE)
    msg = pr.refusal_message(pr.check_against_latest({**BASE, "thresholds": "t9"}, tmp_path))
    assert msg.endswith("is stale: thresholds differ (1 hash)")
    assert pr.refusal_message(pr.check_against_latest(BASE, tmp_path / "none")).startswith(
        "no pre-registration record")


def test_latest_by_timestamp_across_name_precisions(tmp_path):
    # lexicographic order would pick the second-precision name ("Z" > digits); time order must not
    _write(tmp_path, "preregister-20261105T043023Z.json", "2026-11-05T04:30:23Z",
           {**BASE, "pack": "older"})
    _write(tmp_path, "preregister-20261105T043023877792Z.json", "2026-11-05T04:30:23.877792Z", BASE)
    path, rec = pr.latest_record(tmp_path)
    assert path.name == "preregister-20261105T043023877792Z.json" and rec["hashes"]["pack"] == "p0"
    _write(tmp_path, "preregister-20261106T000000Z.json", "2026-11-06T00:00:00Z",
           {**BASE, "pack": "newest"})
    assert pr.latest_record(tmp_path)[1]["hashes"]["pack"] == "newest"


def test_name_timestamp_used_when_created_missing(tmp_path):
    (tmp_path / "preregister-20261107T000000Z.json").write_text(
        json.dumps({"schema": 1, "hashes": {**BASE, "pack": "named"}}), encoding="utf-8")
    _write(tmp_path, "preregister-20261105T043023877792Z.json", "2026-11-05T04:30:23.877792Z", BASE)
    assert pr.latest_record(tmp_path)[1]["hashes"]["pack"] == "named"


def test_unorderable_record_is_a_clear_error(tmp_path):
    (tmp_path / "preregister-bogus.json").write_text("{}", encoding="utf-8")
    with pytest.raises(pr.PreregisterError, match="preregister-bogus.json"):
        pr.latest_record(tmp_path)
