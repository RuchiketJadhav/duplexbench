# Copied from C:\Users\Lenovo\research\asr-bench\tests\test_scheduler_storage.py (asr-bench, same author).
# The two scheduler tests unchanged except the import; the storage tests of that file
# are not copied (duplex-bench's Store has no system_metrics table; tests/unit/test_storage.py
# covers the writer).
from duplexbench.load.scheduler import closed_loop, open_loop

def test_open_loop_rate_and_phases():
    arr = open_loop(4000, level=8, mean_session_s=4.0, seed=3)
    rate = (len(arr) - 1) / (arr[-1].t_s - arr[0].t_s)
    assert abs(rate - 2.0) / 2.0 < 0.05          # lambda = 8 / 4 s
    assert arr[0].phase == "ramp" and arr[-1].phase == "cooldown"
    assert sum(a.phase == "sustain" for a in arr) > 3900


def test_open_loop_is_a_fixed_schedule():
    # offered load is decided before any response arrives [D21]
    assert [a.t_s for a in open_loop(50, 8, 4.0, 1)] == [a.t_s for a in open_loop(50, 8, 4.0, 1)]


def test_closed_loop_all_sustain():
    arr = closed_loop(5)
    assert [a.index for a in arr] == list(range(5))
    assert {a.phase for a in arr} == {"sustain"} and {a.t_s for a in arr} == {0.0}
