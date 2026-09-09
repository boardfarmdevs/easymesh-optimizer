from contextlib import contextmanager
import threading

import pytest

from room_demo.client_wifi import parallel_disconnections, parallel_reconnections


@pytest.mark.parametrize("fail_enter, fail_commit", [(False, False), (True, False), (False, True)])
def test_all_disconnects_finish_before_commit_and_all_successes_rollback(fail_enter, fail_commit):
    barrier = threading.Barrier(4)
    entered = set()
    rolled_back = set()

    @contextmanager
    def disconnect(role):
        barrier.wait(timeout=1)
        if fail_enter and role == 2:
            raise RuntimeError("disconnect failed")
        entered.add(role)
        try:
            yield
        except BaseException:
            rolled_back.add(role)
            raise

    try:
        with parallel_disconnections(disconnect(role) for role in range(4)):
            assert entered == set(range(4))
            if fail_commit:
                raise RuntimeError("commit failed")
    except RuntimeError:
        assert fail_enter or fail_commit
    assert entered == (set(range(4)) - {2} if fail_enter else set(range(4)))
    assert rolled_back == (entered if fail_enter or fail_commit else set())


def test_reconnections_are_bounded_and_finish_even_if_one_fails():
    barrier = threading.Barrier(4)
    completed = set()
    def reconnect(role):
        barrier.wait(timeout=1)
        completed.add(role)
        if role == 2:
            raise RuntimeError("reconnect failed")
    with pytest.raises(RuntimeError, match="reconnect failed"):
        parallel_reconnections(reconnect, range(4))
    assert completed == set(range(4))
