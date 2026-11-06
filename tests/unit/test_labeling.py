"""M2b-T03: resumable two-labeler sessions."""
import json

import numpy as np
import pytest
import soundfile as sf

from duplexbench.labeling import coverage, label_path, read_labels, run_session


def _manifest(tmp_path, n=15):
    p = tmp_path / "manifest.jsonl"
    p.write_text("".join(json.dumps({"item_id": f"it{i:02d}", "text": f"text {i}",
                                     "clip": f"clip{i}.wav"}) + "\n" for i in range(1, n + 1)),
                 encoding="utf-8")
    return p


class Feed:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = 0

    def __call__(self, _prompt):
        self.prompts += 1
        return self.answers.pop(0)


def test_resume_after_10_continues_at_11(tmp_path):
    m = _manifest(tmp_path)
    out_dir = tmp_path / "labels"
    shown, played = [], []
    n = run_session(m, "respond_resume", "alice", out_dir=out_dir, input_fn=Feed(["respond"] * 10 + ["q"]),
                    output_fn=shown.append, player=played.append)
    assert n == 10 and len(played) == 11
    shown2 = []
    n2 = run_session(m, "respond_resume", "alice", out_dir=out_dir, input_fn=Feed(["resume"] * 5),
                     output_fn=shown2.append, player=lambda p: None)
    assert n2 == 5
    first = next(s for s in shown2 if s.startswith("--- item"))
    assert first == "--- item 11/15: it11"
    assert "text: text 11" in shown2
    labs = read_labels(label_path("respond_resume", out_dir))
    assert [r["item_id"] for r in labs] == [f"it{i:02d}" for i in range(1, 16)]


def test_two_labelers_per_item_and_no_third(tmp_path):
    m = _manifest(tmp_path, 4)
    out_dir = tmp_path / "labels"
    for who, lab in (("alice", "claim"), ("bob", "no_claim")):
        run_session(m, "success_claim", who, out_dir=out_dir, input_fn=Feed([lab] * 4),
                    output_fn=lambda s: None)
    cov = coverage(label_path("success_claim", out_dir))
    assert cov == {f"it{i:02d}": {"alice", "bob"} for i in range(1, 5)}
    feed = Feed([])
    assert run_session(m, "success_claim", "carol", out_dir=out_dir, input_fn=feed,
                       output_fn=lambda s: None) == 0
    assert feed.prompts == 0
    rec = read_labels(label_path("success_claim", out_dir))[0]
    assert set(rec) == {"item_id", "set", "label", "labeler_id", "t_utc"}


def test_invalid_input_replay_skip(tmp_path):
    m = _manifest(tmp_path, 2)
    played = []
    n = run_session(m, "recovery_request", "a", out_dir=tmp_path / "l",
                    input_fn=Feed(["bogus", "r", "request", "s"]), output_fn=lambda s: None,
                    player=played.append)
    assert n == 1 and len(played) == 3            # item 1 twice (replay), item 2 once
    with pytest.raises(ValueError):
        run_session(m, "nope", "a", out_dir=tmp_path / "l")


def test_clip_is_cut_from_wav(tmp_path):
    wav = tmp_path / "call.wav"
    sf.write(str(wav), np.zeros((8000, 2), dtype=np.int16), 8000, subtype="PCM_16")
    m = tmp_path / "m.jsonl"
    m.write_text(json.dumps({"item_id": "x", "text": "t", "wav": str(wav), "t0_ms": 100,
                             "t1_ms": 600}) + "\n", encoding="utf-8")
    played = []
    run_session(m, "respond_resume", "a", out_dir=tmp_path / "l", input_fn=Feed(["respond"]),
                output_fn=lambda s: None, player=played.append, clip_dir=tmp_path / "clips")
    assert sf.info(str(played[0])).frames == 4000
