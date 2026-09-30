"""The room session's lock: reentrant, and granted in arrival order."""
from __future__ import annotations

import threading
import time
import unittest

from room_demo.interactions import FairRLock


class FairRLockTests(unittest.TestCase):
    def test_the_owner_may_enter_again(self):
        lock = FairRLock()
        with lock:
            with lock:
                self.assertTrue(lock.acquire(blocking=False))
                lock.release()
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_another_thread_waits_and_a_non_blocking_attempt_fails(self):
        lock = FairRLock()
        results = []
        with lock:
            thread = threading.Thread(target=lambda: results.append(lock.acquire(blocking=False)))
            thread.start()
            thread.join()
        self.assertEqual(results, [False])

    def test_waiters_get_the_lock_in_arrival_order(self):
        lock = FairRLock()
        order = []
        lock.acquire()
        threads = []
        for name in ("snapshot", "renewal", "steering"):
            thread = threading.Thread(target=lambda name=name: (lock.acquire(), order.append(name), lock.release()))
            thread.start()
            threads.append(thread)
            time.sleep(0.05)  # each waits before the next arrives
        lock.release()
        for thread in threads:
            thread.join(2)
        self.assertEqual(order, ["snapshot", "renewal", "steering"])

    def test_a_timed_wait_gives_up_and_leaves_the_queue(self):
        lock = FairRLock()
        results = []
        with lock:
            thread = threading.Thread(target=lambda: results.append(lock.acquire(timeout=0.1)))
            thread.start()
            thread.join(2)
        self.assertEqual(results, [False])
        self.assertTrue(lock.acquire(blocking=False))  # nobody left queued ahead
        lock.release()

    def test_only_the_owner_may_release(self):
        lock = FairRLock()
        with self.assertRaises(RuntimeError):
            lock.release()


if __name__ == "__main__":
    unittest.main()
