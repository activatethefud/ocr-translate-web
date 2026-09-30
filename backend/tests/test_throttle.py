from __future__ import annotations

import threading
import time

from ocrtran.throttle import Throttle


def test_rate_limit_halves_and_cools_down():
    t = Throttle(ceiling=8, quiet=100, ramp_interval=100, default_cooldown=0.3)
    t.set_limit(8)

    class _Inflight:
        pass

    # simulate 4 calls in flight when a 429 arrives
    for _ in range(4):
        t.acquire()
    new = t.note_rate_limited(retry_after=0.3)
    assert new <= 4  # limit was cut
    assert t.events == 1
    assert t.stats()["rate_limit_events"] == 1
    # finish the in-flight calls, then a new acquire must wait out the cooldown
    for _ in range(4):
        t.release()
    t0 = time.monotonic()
    assert t.acquire(timeout=2) is True
    assert time.monotonic() - t0 >= 0.25
    t.release()


def test_limit_never_drops_below_one():
    t = Throttle(ceiling=4, default_cooldown=0.01)
    t.set_limit(1)
    assert t.note_rate_limited() == 1
    assert t.note_rate_limited() == 1


def test_acquire_never_exceeds_limit():
    t = Throttle(ceiling=16)
    t.set_limit(2)
    lock = threading.Lock()
    active = peak = 0

    def worker():
        nonlocal active, peak
        with t:
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert peak <= 2


def test_success_ramps_back_up_after_quiet_period():
    t = Throttle(ceiling=8, quiet=0.0, ramp_interval=0.0)
    t.set_limit(1)
    t.note_success()
    assert t.limit == 2  # one slot restored
    for _ in range(20):
        t.note_success()
    assert t.limit == 8  # capped at the ceiling


def test_quiet_after_limit_blocks_ramp():
    t = Throttle(ceiling=8, quiet=100, ramp_interval=0.0)
    t.set_limit(1)
    t.note_rate_limited()  # sets last_down = now (quiet not elapsed)
    t.note_success()
    assert t.limit == 1  # still quiet -> no ramp


def test_ensure_ceiling_raises_but_never_lowers():
    t = Throttle(ceiling=4)
    t.ensure_ceiling(8)
    assert t.ceiling == 8
    assert t.limit == 8  # limit was at the old ceiling -> raised too
    t.set_limit(2)
    t.ensure_ceiling(3)
    assert t.ceiling == 8  # never lowered
    assert t.limit == 2


def test_reset_restores_defaults():
    t = Throttle(ceiling=8)
    t.set_limit(1)
    t.note_rate_limited()
    t.reset()
    assert t.limit == 8
    assert t.events == 0
    assert t.inflight == 0
