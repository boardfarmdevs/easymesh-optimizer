from __future__ import annotations

import time


def timing_mark():
    return {"at": time.time(), "monotonic_ns": time.monotonic_ns()}


def timed_call(timings, stage, operation, *arguments, **keywords):
    timing = timings[stage] = {"started": timing_mark()}
    try:
        return operation(*arguments, **keywords)
    except Exception as error:
        timing["error"] = str(error)
        timing["error_type"] = type(error).__name__
        raise
    finally:
        timing["finished"] = timing_mark()
        timing["elapsed_ms"] = (
            timing["finished"]["monotonic_ns"] - timing["started"]["monotonic_ns"]
        ) / 1e6
