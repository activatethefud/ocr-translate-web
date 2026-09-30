from __future__ import annotations

import pytest

from ocrtran.concurrency import parallel_map


def test_parallel_preserves_order():
    assert parallel_map(lambda x: x * 2, [1, 2, 3, 4], workers=4) == [2, 4, 6, 8]


def test_parallel_workers_one_is_sequential():
    assert parallel_map(lambda x: x + 1, [1, 2, 3], workers=1) == [2, 3, 4]


def test_parallel_empty():
    assert parallel_map(lambda x: x, [], workers=4) == []


def test_parallel_single_item():
    assert parallel_map(lambda x: x, ["a"], workers=8) == ["a"]


def test_parallel_propagates_exceptions():
    def boom(x):
        if x == 2:
            raise ValueError("nope")
        return x

    with pytest.raises(ValueError):
        parallel_map(boom, [1, 2, 3], workers=3)


def test_parallel_zero_workers_falls_back():
    assert parallel_map(lambda x: x, [1, 2], workers=0) == [1, 2]


def test_parallel_uses_up_to_eight_workers_and_stays_in_order():
    import threading
    import time

    lock = threading.Lock()
    active = 0
    peak = 0

    def fn(x):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return x

    out = parallel_map(fn, list(range(20)), workers=8)
    assert out == list(range(20))  # order preserved
    assert 1 < peak <= 8  # actually parallel, never above the cap


def test_parallel_single_worker_is_never_parallel():
    import threading
    import time

    lock = threading.Lock()
    active = 0
    peak = 0

    def fn(x):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.005)
        with lock:
            active -= 1
        return x

    parallel_map(fn, list(range(5)), workers=1)
    assert peak == 1
